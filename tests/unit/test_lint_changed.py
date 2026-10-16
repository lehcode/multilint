"""Unit tests for the Claude Code hook body, core/lint_changed.py.

Nothing here starts a container. The argv builder is a pure function precisely so that the
invocation can be asserted on a machine with no Docker and no image, which is what CI is.
"""

# pylint: disable=redefined-outer-name,protected-access
import importlib.util
import io
import json
import os
import re
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).parent.parent.parent / "core" / "lint_changed.py"


def _load_module():
    """Import the hook by path — it is a script next to the plugin, not an installed package."""
    spec = importlib.util.spec_from_file_location("lint_changed_under_test", HOOK_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


@pytest.fixture(scope="module")
def hook():
    return _load_module()


def _set_ceiling(monkeypatch, hook_module, value):
    """Isolate search_ceiling() without a real settings database.

    MULTILINT_SEARCH_CEILING is retired with no environment replacement, so tests that used to
    monkeypatch.setenv it now monkeypatch read_setting directly -- the smallest change that does not
    require a real SQLite file per test. The settings table's own round-trip is covered separately
    in TestHookSettings.
    """
    monkeypatch.setattr(
        hook_module,
        "read_setting",
        lambda key: str(value) if key == "search_ceiling" else None,
    )


class TestLintProgram:
    """The bash program run on the host."""

    def test_program_keeps_going_after_a_failing_file(self, hook):
        """One bad file must not stop the rest from being checked, but the run must still fail."""
        program = hook.lint_program()
        assert "|| rc=1" in program
        assert "exit $rc" in program

    def test_program_marks_both_streams(self, hook):
        """JSON lands on stdout and human findings on stderr, so both need the marker to be
        split back into per-file results."""
        program = hook.lint_program()
        assert program.count(hook.FILE_MARKER) == 2
        assert ">&2" in program

    def test_program_reads_lint_sh_from_first_arg(self, hook):
        """The lint.sh path is passed as $1 and read via shift."""
        program = hook.lint_program()
        assert 'lint_sh="$1"' in program
        assert "shift" in program
        assert 'bash "$lint_sh"' in program


class TestHTTPPathIsGone:
    """Guards against the HTTP client creeping back in.

    It is not dead code to leave lying around: the path-translation layer it needed is what produced
    a passing report for a directory the container could not see.
    """

    def test_no_urllib_import(self):
        assert "import urllib" not in HOOK_PATH.read_text(encoding="utf-8")

    def test_path_translation_helpers_removed(self, hook):
        assert not hasattr(hook, "path_map")
        assert not hasattr(hook, "to_container_path")
        assert not hasattr(hook, "DEFAULT_LINT_URL")

    def test_nosec_suppression_removed(self):
        """The `# nosec B310` existed only to silence bandit about urlopen."""
        assert "nosec" not in HOOK_PATH.read_text(encoding="utf-8")


class TestResultParsing:
    @staticmethod
    def _document(return_code=0, skipped=None, checks=None):
        return {
            "summary": {"files_checked": 1, "checks_run": 16, "checks_skipped": skipped or []},
            "checks": checks or {},
            "return_code": return_code,
            "files": ["a.py"],
        }

    def test_splits_multiple_documents(self, hook):
        stdout = "".join(f"{hook.FILE_MARKER}{name}\n{json.dumps(self._document())}\n" for name in ("a.py", "b.sh"))
        assert sorted(hook.split_stream(stdout)) == ["a.py", "b.sh"]

    def test_pairs_detail_with_its_file(self, hook):
        """stderr detail must be attributed to the right file, not concatenated."""
        stderr = f"{hook.FILE_MARKER}a.py\n  ✗ flake8\n{hook.FILE_MARKER}b.sh\n  ✗ shellcheck\n"
        sections = hook.split_stream(stderr)
        assert "flake8" in sections["a.py"]
        assert "shellcheck" in sections["b.sh"]
        assert "shellcheck" not in sections["a.py"]

    def test_unparsable_document_is_unavailable_not_guessed(self, hook, monkeypatch, tmp_path):
        """A malformed result is never reported as a finding; it makes the file unverified."""
        lint_sh = tmp_path / "lint.sh"
        lint_sh.touch()
        monkeypatch.setattr(hook, "_lint_sh_path", lambda: lint_sh)
        monkeypatch.setattr(hook.shutil, "which", lambda _name: "/usr/bin/bash")

        class _Proc:
            stdout = f"{hook.FILE_MARKER}a.py\nthis is not json\n"
            stderr = ""
            returncode = 1

        monkeypatch.setattr(hook.subprocess, "run", lambda *_a, **_k: _Proc())
        with pytest.raises(hook.LintUnavailable, match="no readable result"):
            hook.run_lint(Path("/p"), ["a.py"])

    def test_missing_lint_sh_is_unavailable(self, hook, monkeypatch):
        monkeypatch.setattr(hook, "_lint_sh_path", lambda: Path("/nonexistent/lint.sh"))
        with pytest.raises(hook.LintUnavailable, match="lint.sh not found"):
            hook.run_lint(Path("/p"), ["a.py"])

    def test_missing_bash_is_unavailable(self, hook, monkeypatch, tmp_path):
        lint_sh = tmp_path / "lint.sh"
        lint_sh.touch()
        monkeypatch.setattr(hook, "_lint_sh_path", lambda: lint_sh)
        monkeypatch.setattr(hook.shutil, "which", lambda _name: None)
        with pytest.raises(hook.LintUnavailable, match="bash not found"):
            hook.run_lint(Path("/p"), ["a.py"])

    def test_subprocess_failure_is_unavailable(self, hook, monkeypatch, tmp_path):
        lint_sh = tmp_path / "lint.sh"
        lint_sh.touch()
        monkeypatch.setattr(hook, "_lint_sh_path", lambda: lint_sh)
        monkeypatch.setattr(hook.shutil, "which", lambda _name: "/usr/bin/bash")

        def _boom(*_args, **_kwargs):
            raise OSError("no such binary")

        monkeypatch.setattr(hook.subprocess, "run", _boom)
        with pytest.raises(hook.LintUnavailable, match="could not be started"):
            hook.run_lint(Path("/p"), ["a.py"])


class TestLocalRun:
    """Tests for local lint.sh execution (no Docker)."""

    def test_lint_program_contains_no_docker_reference(self, hook):
        """The program must not reference Docker."""
        program = hook.lint_program()
        assert "docker" not in program.lower()

    def test_run_lint_calls_bash_with_cwd(self, hook, monkeypatch, tmp_path):
        """run_lint invokes bash with scope_root as cwd."""
        lint_sh = tmp_path / "lint.sh"
        lint_sh.touch()
        monkeypatch.setattr(hook, "_lint_sh_path", lambda: lint_sh)

        calls = []

        def mock_run(*args, **kwargs):
            calls.append((args[0], kwargs.get("cwd")))

            class _Proc:
                stdout = f'{hook.FILE_MARKER}a.py\n{{"checks": {{}}}}\n'
                stderr = ""
                returncode = 0

            return _Proc()

        monkeypatch.setattr(hook.subprocess, "run", mock_run)
        monkeypatch.setattr(hook.shutil, "which", lambda _: "/usr/bin/bash")

        assert hook.run_lint(tmp_path, ["a.py"]) == [("a.py", {"checks": {}}, "")]

        assert calls == [(["bash", "-c", hook.lint_program(), "_", str(lint_sh), "a.py"], tmp_path)]

    def test_run_lint_handles_timeout(self, hook, monkeypatch, tmp_path):
        """A timeout makes the file unverified, never a silent pass."""
        lint_sh = tmp_path / "lint.sh"
        lint_sh.touch()
        monkeypatch.setattr(hook, "_lint_sh_path", lambda: lint_sh)
        monkeypatch.setattr(hook.shutil, "which", lambda _: "/usr/bin/bash")

        def mock_run(*_args, **_kwargs):
            raise subprocess.TimeoutExpired("bash", 120)

        monkeypatch.setattr(hook.subprocess, "run", mock_run)

        with pytest.raises(hook.LintUnavailable, match="did not finish within 120 s"):
            hook.run_lint(tmp_path, ["a.py"])


class TestSkippedChecksAreSurfaced:
    """A skipped check is not a passing check, and must reach the agent even on a zero exit."""

    @staticmethod
    def _document(skipped):
        return {"summary": {"checks_skipped": skipped}, "checks": {}, "return_code": 0}

    def test_reports_skipped_checks(self, hook):
        assert hook.skipped_checks(self._document(["markdownlint", "prettier"])) == [
            "markdownlint",
            "prettier",
        ]

    def test_gitleaks_is_no_longer_filtered(self, hook):
        """It used to be suppressed, because lint.sh gated it on <target>/.git and a single-file
        target can never satisfy that, so the skip fired after every edit and carried no
        information. lint.sh now falls back to --no-git, so a reported gitleaks skip means the
        check genuinely did not run and the user needs to hear it."""
        assert hook.skipped_checks(self._document(["gitleaks"])) == ["gitleaks"]
        assert hook.skipped_checks(self._document(["gitleaks", "mypy"])) == ["gitleaks", "mypy"]

    def test_nothing_is_structurally_suppressed(self, hook):
        assert hook.STRUCTURALLY_SKIPPED == frozenset()

    def test_tolerates_a_missing_summary(self, hook):
        assert hook.skipped_checks({"checks": {}}) == []

    def test_tolerates_a_malformed_summary(self, hook):
        assert hook.skipped_checks({"summary": {"checks_skipped": "mypy"}}) == []


class TestFailingChecks:
    def test_lists_failed_checks_with_counts(self, hook):
        document = {
            "checks": {
                "flake8": {"status": "failed", "failed": 2},
                "black": {"status": "ok", "failed": 0},
                "mypy": {"status": "skipped", "failed": 0},
            }
        }
        assert hook.failing_checks(document) == ["flake8: 2 failure(s)"]

    def test_counts_failures_even_without_a_status(self, hook):
        """Tolerates an older document shape rather than reporting a failure as clean."""
        assert hook.failing_checks({"checks": {"pylint": {"failed": 1}}}) == ["pylint: 1 failure(s)"]

    def test_empty_document_reports_nothing(self, hook):
        assert hook.failing_checks({}) == []


class TestTreeWalkIsGone:
    """The dirty-tree scan and its guards must not come back.

    The hook receives one file_path per invocation. Collecting every dirty file in the repository
    meant one Write reported on files the user had not touched, and the 2000-file scan cap existed
    only to make that survivable. A cap on a list that can hold one element is not a guard.
    """

    @pytest.mark.parametrize("name", ["SKIP_DIRS", "MAX_SCAN_FILES", "MAX_LINT_TARGETS"])
    def test_scan_constants_are_gone(self, hook, name):
        assert not hasattr(hook, name)

    @pytest.mark.parametrize(
        "name",
        ["candidates_under", "changed_under_git", "changed_via_sqlite", "select_targets"],
    )
    def test_scan_functions_are_gone(self, hook, name):
        assert not hasattr(hook, name)

    def test_source_no_longer_enumerates_the_dirty_tree(self):
        """git diff / ls-files must not appear: they are what produced the unrelated targets."""
        source = HOOK_PATH.read_text(encoding="utf-8")
        assert "ls-files" not in source
        assert "--name-only" not in source


class TestGitRootIsBoundedAtHome:
    """The ancestor walk must stop at $HOME.

    Unbounded it reaches `/`, and because the scope root is what gets bind-mounted, an edit to a file
    sitting directly in a system directory made that directory the mount. The state database ended up
    holding a full recursive walk of /etc, which can only happen if /etc became the scope root.
    """

    def test_finds_the_repository_containing_the_file(self, hook, tmp_path, monkeypatch):
        _set_ceiling(monkeypatch, hook, tmp_path)
        (tmp_path / "repo" / ".git").mkdir(parents=True)
        deep = tmp_path / "repo" / "src" / "pkg"
        deep.mkdir(parents=True)
        assert hook.find_git_root(deep) == (tmp_path / "repo").resolve()

    def test_stops_at_the_ceiling_instead_of_climbing_to_root(self, hook, tmp_path, monkeypatch):
        """A .git above the ceiling must not be claimed, even though it exists."""
        (tmp_path / ".git").mkdir()
        ceiling = tmp_path / "home"
        inner = ceiling / "loose"
        inner.mkdir(parents=True)
        _set_ceiling(monkeypatch, hook, ceiling)
        assert hook.find_git_root(inner) is None

    def test_path_outside_the_ceiling_is_not_searched(self, hook, tmp_path, monkeypatch):
        """This is the /etc case: nothing above such a path is ours to claim."""
        outside = tmp_path / "system" / "etc"
        outside.mkdir(parents=True)
        (outside / ".git").mkdir()
        (tmp_path / "home").mkdir()
        _set_ceiling(monkeypatch, hook, tmp_path / "home")
        assert hook.find_git_root(outside) is None

    def test_ceiling_itself_may_be_the_repository(self, hook, tmp_path, monkeypatch):
        _set_ceiling(monkeypatch, hook, tmp_path)
        (tmp_path / ".git").mkdir()
        assert hook.find_git_root(tmp_path) == tmp_path.resolve()

    def test_defaults_to_home_when_unset(self, hook, monkeypatch):
        monkeypatch.setattr(hook, "read_setting", lambda key: None)
        assert hook.search_ceiling() == Path.home()


class TestStateSchema:
    """Migration, not creation. This database already exists in the field."""

    def _legacy_db(self, tmp_path):
        """A database in the original four-column shape, with a row in it.

        Named "multilint" directly under tmp_path (rather than an arbitrary "state" name) so
        `XDG_STATE_HOME=tmp_path` reproduces state_dir()'s real, non-override path exactly --
        MULTILINT_STATE_DIR is retired with no environment replacement.
        """
        directory = tmp_path / "multilint"
        directory.mkdir()
        with sqlite3.connect(directory / "changes.db") as connection:
            connection.execute("CREATE TABLE seen (path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, digest TEXT)")
            connection.execute("INSERT INTO seen VALUES ('/old/file.py', 10, 20, 'deadbeef')")
        return directory

    def test_adds_columns_to_an_existing_database(self, hook, tmp_path, monkeypatch):
        self._legacy_db(tmp_path)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        connection = hook.open_state()
        assert connection is not None
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(seen)")}
        finally:
            connection.close()
        assert {"in_git", "git_root", "first_seen_at", "last_seen_at", "last_linted_at"} <= columns

    def test_migration_preserves_existing_rows(self, hook, tmp_path, monkeypatch):
        """A migration that loses history is a worse outcome than no migration."""
        self._legacy_db(tmp_path)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        connection = hook.open_state()
        try:
            row = connection.execute("SELECT digest FROM seen WHERE path = '/old/file.py'").fetchone()
        finally:
            connection.close()
        assert row == ("deadbeef",)

    def test_migration_is_idempotent(self, hook, tmp_path, monkeypatch):
        """Two sessions run this hook concurrently; the second must not fail on duplicate columns."""
        self._legacy_db(tmp_path)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        first = hook.open_state()
        first.close()
        second = hook.open_state()
        assert second is not None
        second.close()

    def test_creates_the_folder_cache_table(self, hook, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "fresh"))
        connection = hook.open_state()
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(folders)")}
        finally:
            connection.close()
        assert {"folder", "git_root", "resolved_at", "last_used_at"} <= columns


class TestGitRootCache:
    """A cached root is validated, never trusted.

    `git init`, `rm -rf .git`, a clone and a moved directory all change the answer. A cache that
    cannot notice is a cache that serves a wrong scope root, and the scope root is what gets mounted.
    """

    @pytest.fixture
    def state(self, hook, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
        _set_ceiling(monkeypatch, hook, tmp_path)
        connection = hook.open_state()
        yield connection
        connection.close()

    def test_first_lookup_is_a_miss_and_is_recorded(self, hook, state, tmp_path):
        (tmp_path / "repo" / ".git").mkdir(parents=True)
        folder = tmp_path / "repo" / "src"
        folder.mkdir()
        root, from_cache = hook.cached_git_root(state, folder)
        assert root == (tmp_path / "repo").resolve()
        assert from_cache is False

    def test_second_lookup_is_served_from_cache(self, hook, state, tmp_path):
        (tmp_path / "repo" / ".git").mkdir(parents=True)
        folder = tmp_path / "repo" / "src"
        folder.mkdir()
        hook.cached_git_root(state, folder)
        root, from_cache = hook.cached_git_root(state, folder)
        assert root == (tmp_path / "repo").resolve()
        assert from_cache is True

    def test_stale_entry_is_detected_when_git_disappears(self, hook, state, tmp_path):
        """rm -rf .git after caching. Returning the dead root would mount the wrong directory."""
        git_dir = tmp_path / "repo" / ".git"
        git_dir.mkdir(parents=True)
        folder = tmp_path / "repo" / "src"
        folder.mkdir()
        hook.cached_git_root(state, folder)
        git_dir.rmdir()
        root, from_cache = hook.cached_git_root(state, folder)
        assert root is None
        assert from_cache is False

    def test_absence_is_itself_a_cached_answer(self, hook, state, tmp_path):
        """ "No repository" is worth caching too, otherwise every edit outside a repo re-walks."""
        folder = tmp_path / "loose"
        folder.mkdir()
        assert hook.cached_git_root(state, folder) == (None, False)
        assert hook.cached_git_root(state, folder) == (None, True)

    def test_cached_absence_is_revisited_when_a_repository_appears(self, hook, state, tmp_path):
        """git init after a cached miss. A cached 'no' must not outlive the fact."""
        folder = tmp_path / "loose"
        folder.mkdir()
        hook.cached_git_root(state, folder)
        (folder / ".git").mkdir()
        root, from_cache = hook.cached_git_root(state, folder)
        assert root == folder.resolve()
        assert from_cache is False

    def test_timestamps_are_recorded(self, hook, state, tmp_path):
        folder = tmp_path / "loose"
        folder.mkdir()
        hook.cached_git_root(state, folder)
        row = state.execute("SELECT resolved_at, last_used_at FROM folders").fetchone()
        assert row[0] and row[1]

    def test_works_without_a_database(self, hook, tmp_path, monkeypatch):
        """No database is a degraded mode, not a failure mode."""
        _set_ceiling(monkeypatch, hook, tmp_path)
        (tmp_path / "repo" / ".git").mkdir(parents=True)
        root, from_cache = hook.cached_git_root(None, tmp_path / "repo")
        assert root == (tmp_path / "repo").resolve()
        assert from_cache is False


class TestChangeRecording:
    """Identical bytes must be a no-op; anything else must count as changed."""

    @pytest.fixture
    def state(self, hook, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
        connection = hook.open_state()
        yield connection
        connection.close()

    def test_first_sight_of_a_file_is_a_change(self, hook, state, tmp_path):
        target = tmp_path / "a.py"
        target.write_text("x = 1\n", encoding="utf-8")
        assert hook.record_and_check(state, target, None) is True

    def test_identical_rewrite_is_not_a_change(self, hook, state, tmp_path):
        target = tmp_path / "a.py"
        target.write_text("x = 1\n", encoding="utf-8")
        hook.record_and_check(state, target, None)
        target.write_text("x = 1\n", encoding="utf-8")
        assert hook.record_and_check(state, target, None) is False

    def test_different_content_is_a_change(self, hook, state, tmp_path):
        target = tmp_path / "a.py"
        target.write_text("x = 1\n", encoding="utf-8")
        hook.record_and_check(state, target, None)
        target.write_text("x = 2\n", encoding="utf-8")
        assert hook.record_and_check(state, target, None) is True

    def test_in_git_is_recorded_with_the_root(self, hook, state, tmp_path):
        target = tmp_path / "a.py"
        target.write_text("x = 1\n", encoding="utf-8")
        hook.record_and_check(state, target, tmp_path)
        row = state.execute("SELECT in_git, git_root FROM seen WHERE path = ?", (str(target),)).fetchone()
        assert row == (1, str(tmp_path))

    def test_absence_of_a_repository_is_recorded_as_such(self, hook, state, tmp_path):
        target = tmp_path / "a.py"
        target.write_text("x = 1\n", encoding="utf-8")
        hook.record_and_check(state, target, None)
        row = state.execute("SELECT in_git, git_root FROM seen WHERE path = ?", (str(target),)).fetchone()
        assert row == (0, None)

    def test_first_seen_is_not_overwritten_by_later_writes(self, hook, state, tmp_path):
        target = tmp_path / "a.py"
        target.write_text("x = 1\n", encoding="utf-8")
        hook.record_and_check(state, target, None)
        first = state.execute("SELECT first_seen_at FROM seen").fetchone()[0]
        target.write_text("x = 2\n", encoding="utf-8")
        hook.record_and_check(state, target, None)
        assert state.execute("SELECT first_seen_at FROM seen").fetchone()[0] == first

    def test_without_a_database_every_write_counts_as_changed(self, hook, tmp_path):
        """Linting twice wastes time; skipping a real change reports a false clean."""
        target = tmp_path / "a.py"
        target.write_text("x = 1\n", encoding="utf-8")
        assert hook.record_and_check(None, target, None) is True

    def test_unreadable_file_is_not_a_change(self, hook, state, tmp_path):
        assert hook.record_and_check(state, tmp_path / "missing.py", None) is False


class TestTimestampFormat:
    def test_is_iso8601_utc_to_the_second(self, hook):
        stamp = hook.now_stamp()
        parsed = datetime.fromisoformat(stamp)
        assert parsed.tzinfo is not None
        assert parsed.microsecond == 0


class TestFailingLines:
    def test_keeps_only_marked_lines(self, hook):
        output = "  ✓ flake8\n  ✗ black\n  ⚠ pylint: 3 failures\n  📄 file.sh\n"
        assert hook.failing_lines(output) == ["  ✗ black", "  ⚠ pylint: 3 failures"]


class TestHookSettings:
    """--set/--get/--unset: the search_ceiling setting. Run as a real subprocess against a
    temporary changes.db so the argv-gated CLI branch ahead of main()'s stdin read is exercised
    for real, not just the underlying functions."""

    def _cli_env(self, tmp_path):
        return {**os.environ, "XDG_STATE_HOME": str(tmp_path)}

    def test_set_then_get_round_trips_search_ceiling(self, tmp_path):
        env = self._cli_env(tmp_path)
        ceiling = str(tmp_path / "ceiling")
        subprocess.run(
            [sys.executable, str(HOOK_PATH), "--set", "search_ceiling", ceiling],
            env=env,
            check=True,
        )
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH), "--get", "search_ceiling"],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        assert result.stdout.strip() == ceiling

    def test_unset_removes_the_value(self, tmp_path):
        env = self._cli_env(tmp_path)
        ceiling = str(tmp_path / "ceiling")
        subprocess.run(
            [sys.executable, str(HOOK_PATH), "--set", "search_ceiling", ceiling],
            env=env,
            check=True,
        )
        subprocess.run([sys.executable, str(HOOK_PATH), "--unset", "search_ceiling"], env=env, check=True)
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH), "--get", "search_ceiling"],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        assert result.stdout.strip() == ""

    def test_unknown_key_is_rejected_and_nothing_is_written(self, tmp_path):
        env = self._cli_env(tmp_path)
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH), "--set", "image", "x"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode != 0
        db_path = tmp_path / "multilint" / "changes.db"
        if db_path.exists():
            with sqlite3.connect(db_path) as connection:
                row = connection.execute("SELECT 1 FROM settings WHERE key = ?", ("image",)).fetchone()
                assert row is None

    def test_relative_xdg_state_home_is_ignored(self, hook, tmp_path, monkeypatch):
        """A relative XDG_STATE_HOME is invalid per the XDG spec; it used to crash the hook."""
        monkeypatch.setenv("XDG_STATE_HOME", "relative/state")
        monkeypatch.setenv("HOME", str(tmp_path))
        assert hook.state_dir() == tmp_path / ".local" / "state" / "multilint"
        assert hook.search_ceiling() == Path.home()

    def test_unreadable_database_warns_instead_of_failing_silently(self, tmp_path):
        """A corrupt (or locked) db falls back to the default but says so; absence stays silent."""
        (tmp_path / "multilint").mkdir()
        (tmp_path / "multilint" / "changes.db").write_bytes(b"not a sqlite database" * 100)
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH), "--get", "search_ceiling"],
            capture_output=True,
            text=True,
            env=self._cli_env(tmp_path),
        )
        assert result.returncode == 0
        assert result.stdout == ""
        assert "could not read the 'search_ceiling' setting" in result.stderr

    def test_resolvers_default_when_no_row_exists(self, hook, tmp_path, monkeypatch):
        """Absent db/table/row all resolve to the built-in default, not an error."""
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert hook.search_ceiling() == Path.home()


class TestNoEnvironmentInfluence:
    """MULTILINT_IMAGE, MULTILINT_SEARCH_CEILING and MULTILINT_STATE_DIR are retired with no
    environment replacement (user decision, 2026-09-30). Regression guard that the removal is
    complete, not partial: setting all three to values that would have changed the old behavior
    must leave every resolver's result unchanged."""

    def test_retired_variables_have_no_effect(self, hook, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        monkeypatch.delenv("MULTILINT_IMAGE", raising=False)
        monkeypatch.delenv("MULTILINT_SEARCH_CEILING", raising=False)
        monkeypatch.delenv("MULTILINT_STATE_DIR", raising=False)
        baseline_ceiling = hook.search_ceiling()
        baseline_state_dir = hook.state_dir()

        monkeypatch.setenv("MULTILINT_IMAGE", "local/other:dev")
        monkeypatch.setenv("MULTILINT_SEARCH_CEILING", str(tmp_path / "elsewhere"))
        monkeypatch.setenv("MULTILINT_STATE_DIR", str(tmp_path / "elsewhere" / "state"))

        assert hook.search_ceiling() == baseline_ceiling
        assert hook.state_dir() == baseline_state_dir


class TestFindingsNotice:
    def test_notice_is_built_from_a_findings_document(self, hook):
        document = {
            "return_code": 1,
            "summary": {"rules_violated": ["F401"]},
            "checks": {
                "flake8": {
                    "status": "failed",
                    "fix": "fix the code (policy: checks.flake8.args)",
                    "findings": [{"file": "a.py", "line": 1, "rule": "F401", "message": "'os' imported but unused"}],
                },
            },
        }
        results = [("a.py", document, "")]
        findings, skipped, warnings, failed = hook.summarize_results(results)
        names = hook.finding_blocks(document)[0]
        (notice,) = hook.build_notices("a.py", findings, skipped, warnings, failed, names, hook.violated_rules(results))
        assert notice == (
            "multilint: a.py failed flake8\n\n"
            "flake8 — fix the code (policy: checks.flake8.args)\n"
            "  F401 a.py:1  'os' imported but unused\n\n"
            "Rules: F401"
        )


REPO_ROOT = Path(__file__).parent.parent.parent


def _git_repo(tmp_path):
    """A real repository under tmp_path, which is also the search ceiling."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


def _drive_hook(hook, monkeypatch, tmp_path, payload, run_lint_result=()):
    """Run main() on a payload with run_lint stubbed; return (exit code, run_lint calls, stdout).

    Isolates the state database and the search ceiling to tmp_path, so nothing reads or writes the
    user's real ~/.local/state/multilint.
    """
    calls = []

    def stub_run_lint(scope_root, relative_paths):
        calls.append((scope_root, list(relative_paths)))
        if isinstance(run_lint_result, Exception):
            raise run_lint_result
        return list(run_lint_result)

    monkeypatch.setattr(hook, "state_dir", lambda: tmp_path / "state")
    _set_ceiling(monkeypatch, hook, tmp_path)
    monkeypatch.setattr(hook, "run_lint", stub_run_lint)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    stdout = io.StringIO()
    monkeypatch.setattr("sys.stdout", stdout)
    return hook.main(), calls, stdout.getvalue()


class TestEveryLintableTypeReachesLint:
    """A Write or Edit of every supported type inside a repository reaches the linter.

    The user could confirm only Markdown as auto-linted. These cases pin the host side of the
    contract: every suffix in LINTABLE_SUFFIXES, from either tool, tracked or not, produces exactly
    one run_lint call scoped to the repository root with the repo-relative path.
    """

    @pytest.mark.parametrize("tracked", (False, True), ids=("untracked", "tracked"))
    @pytest.mark.parametrize("tool_name", ("Write", "Edit"))
    @pytest.mark.parametrize("suffix", sorted(_load_module().LINTABLE_SUFFIXES))
    def test_reaches_run_lint(self, hook, tmp_path, monkeypatch, suffix, tool_name, tracked):
        repo = _git_repo(tmp_path)
        relative = f"src/sample{suffix}"
        (repo / "src").mkdir()
        (repo / relative).write_text("content\n", encoding="utf-8")
        if tracked:
            subprocess.run(["git", "add", relative], cwd=repo, check=True)
        payload = {"tool_name": tool_name, "cwd": str(repo), "tool_input": {"file_path": str(repo / relative)}}

        code, calls, _out = _drive_hook(hook, monkeypatch, tmp_path, payload)

        assert code == 0
        assert calls == [(repo.resolve(), [relative])]

    def test_relative_file_path_resolves_against_cwd(self, hook, tmp_path, monkeypatch):
        repo = _git_repo(tmp_path)
        (repo / "src").mkdir()
        (repo / "src" / "sample.py").write_text("content\n", encoding="utf-8")
        payload = {"tool_name": "Write", "cwd": str(repo), "tool_input": {"file_path": "src/sample.py"}}

        _code, calls, _out = _drive_hook(hook, monkeypatch, tmp_path, payload)

        assert calls == [(repo.resolve(), ["src/sample.py"])]

    @pytest.mark.parametrize("name", ("src/sample.ts", "Dockerfile", "src/sample.txt"))
    def test_unsupported_type_is_not_linted(self, hook, tmp_path, monkeypatch, name):
        """Documents the current contract: anything outside LINTABLE_SUFFIXES is ignored silently."""
        repo = _git_repo(tmp_path)
        (repo / name).parent.mkdir(parents=True, exist_ok=True)
        (repo / name).write_text("content\n", encoding="utf-8")
        payload = {"tool_name": "Write", "cwd": str(repo), "tool_input": {"file_path": str(repo / name)}}

        code, calls, out = _drive_hook(hook, monkeypatch, tmp_path, payload)

        assert (code, calls, out) == (0, [], "")

    def test_failing_result_reaches_claude(self, hook, tmp_path, monkeypatch):
        """A failed check comes back as additionalContext naming the check and the rule."""
        repo = _git_repo(tmp_path)
        (repo / "sample.md").write_text("# T  \n", encoding="utf-8")
        document = {
            "return_code": 1,
            "checks": {
                "markdownlint": {
                    "status": "failed",
                    "failed": 1,
                    "findings": [{"rule": "MD009", "file": "sample.md", "line": 1, "message": "Trailing spaces"}],
                }
            },
            "summary": {"rules_violated": ["MD009"]},
        }
        payload = {"tool_name": "Edit", "cwd": str(repo), "tool_input": {"file_path": str(repo / "sample.md")}}

        _code, _calls, out = _drive_hook(hook, monkeypatch, tmp_path, payload, [("sample.md", document, "")])

        context = json.loads(out)["hookSpecificOutput"]["additionalContext"]
        assert "markdownlint" in context
        assert "MD009" in context

    def test_unavailable_lint_is_reported_not_silent(self, hook, tmp_path, monkeypatch):
        """When lint.sh cannot run, Claude is told the file is unverified; the edit still succeeds."""
        repo = _git_repo(tmp_path)
        (repo / "sample.py").write_text("x = 1\n", encoding="utf-8")
        payload = {"tool_name": "Write", "cwd": str(repo), "tool_input": {"file_path": str(repo / "sample.py")}}

        code, _calls, out = _drive_hook(
            hook, monkeypatch, tmp_path, payload, hook.LintUnavailable("bash not found on PATH")
        )

        output = json.loads(out)
        assert code == 0
        assert output["hookSpecificOutput"]["additionalContext"] == (
            "multilint could not lint sample.py: bash not found on PATH. Treat the file as unverified."
        )
        assert output["systemMessage"] == "multilint: could not lint"

    def test_identical_save_after_unavailable_lint_is_linted_again(self, hook, tmp_path, monkeypatch):
        """The digest is recorded before linting; a failed run must not make the next save "unchanged"."""
        repo = _git_repo(tmp_path)
        (repo / "sample.py").write_text("x = 1\n", encoding="utf-8")
        payload = {"tool_name": "Write", "cwd": str(repo), "tool_input": {"file_path": str(repo / "sample.py")}}

        _drive_hook(hook, monkeypatch, tmp_path, payload, hook.LintUnavailable("bash not found on PATH"))
        _code, calls, _out = _drive_hook(hook, monkeypatch, tmp_path, payload)
        assert len(calls) == 1, "an identical save after a failed lint was treated as unchanged"
        _code, calls, _out = _drive_hook(hook, monkeypatch, tmp_path, payload)
        assert calls == [], "after a successful lint, an identical save is still skipped"

    def test_multilint_sh_without_python3_reports_a_notice(self, tmp_path):
        """No python3 on PATH: the entry script exits 0 with a notice, never silently."""
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        bash = subprocess.run(["bash", "-c", "command -v bash"], capture_output=True, text=True).stdout.strip()
        proc = subprocess.run(
            [bash, str(HOOK_PATH.parent / "multilint.sh")],
            input="{}",
            capture_output=True,
            text=True,
            env={"PATH": str(bin_dir)},
        )
        assert proc.returncode == 0
        output = json.loads(proc.stdout)
        assert "python3 not found on PATH" in output["hookSpecificOutput"]["additionalContext"]
        assert output["systemMessage"] == "multilint: could not lint"


class TestLintableTypesMatchLintSh:
    """The hook's suffix list and lint.sh's find patterns must agree.

    A type lint.sh learns to lint but the hook filters out is never linted on edit; a type the hook
    forwards but lint.sh never finds is reported as clean. Either drift is silent without this guard.
    """

    def test_suffixes_match_lint_sh_find_patterns(self, hook):
        text = (REPO_ROOT / "core" / "lint.sh").read_text(encoding="utf-8")
        calls = re.findall(r"< <\(ml_files ((?:'[^']+' )+)--", text)
        patterns = {suffix for call in calls for suffix in re.findall(r"'\*(\.\w+)'", call)}
        assert calls, "no ml_files calls found in lint.sh; update this guard with the file selection"
        assert patterns == hook.LINTABLE_SUFFIXES


class TestHookWiring:
    """hooks.json -> multilint.sh -> lint_changed.py: a break anywhere means no edit is linted."""

    def test_post_tool_use_matches_write_and_edit(self):
        hooks = json.loads((REPO_ROOT / "claude-plugin" / "hooks" / "hooks.json").read_text(encoding="utf-8"))
        entry = hooks["hooks"]["PostToolUse"][0]
        assert {"Write", "Edit"} <= set(entry["matcher"].split("|"))
        assert entry["hooks"][0]["command"].endswith('/scripts/multilint.sh"')

    def test_entrypoint_execs_hook_body(self):
        script = (REPO_ROOT / "core" / "multilint.sh").read_text(encoding="utf-8")
        assert 'exec python3 "$(dirname -- "$0")/lint_changed.py"' in script
