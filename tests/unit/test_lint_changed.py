"""Unit tests for the Claude Code hook body, claude-plugin/scripts/lint_changed.py.

Nothing here starts a container. The argv builder is a pure function precisely so that the
invocation can be asserted on a machine with no Docker and no image, which is what CI is.
"""

# pylint: disable=redefined-outer-name,protected-access
import importlib.util
import json
import os
import sqlite3
import subprocess
import sys
from datetime import datetime
from pathlib import Path

import pytest

HOOK_PATH = Path(__file__).parent.parent.parent / "claude-plugin" / "scripts" / "lint_changed.py"


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


class TestDockerArgv:
    """The invocation. Asserted argument by argument, because each one is load-bearing."""

    def test_mount_is_an_identity_mount(self, hook):
        """source and target are the same path, which is what removes path translation."""
        argv = hook.build_docker_argv(Path("/home/u/proj"), ["a.py"], image="img")
        mount = argv[argv.index("--mount") + 1]
        assert mount == "type=bind,source=/home/u/proj,target=/home/u/proj,readonly"

    def test_uses_mount_not_v(self, hook):
        """Regression guard for the zsh parameter-modifier trap.

        `-v "$PWD:$PWD:ro"` becomes `.../proj:.../projo` in zsh, because `:r` strips an extension
        even inside double quotes. Docker then mounts read-write at a target that does not exist,
        the working directory is created empty, and lint.sh reports success having read nothing.
        `--mount` takes named keys and cannot be misparsed that way.
        """
        argv = hook.build_docker_argv(Path("/home/u/proj"), ["a.py"], image="img")
        assert "-v" not in argv
        assert "--volume" not in argv
        assert "--mount" in argv

    def test_mount_is_read_only(self, hook):
        """Every check is check-only; nothing may be written back into the project."""
        argv = hook.build_docker_argv(Path("/p"), ["a.py"], image="img")
        assert "readonly" in argv[argv.index("--mount") + 1]

    def test_no_network(self, hook):
        """No linter needs the network, so the surface is removed rather than trusted."""
        argv = hook.build_docker_argv(Path("/p"), ["a.py"], image="img")
        assert argv[argv.index("--network") + 1] == "none"

    def test_container_is_removed(self, hook):
        argv = hook.build_docker_argv(Path("/p"), ["a.py"], image="img")
        assert "--rm" in argv

    def test_resource_caps_are_set(self, hook):
        """Mirrors docker-compose.yml so a hook cannot starve the machine it runs on."""
        argv = hook.build_docker_argv(Path("/p"), ["a.py"], image="img")
        assert argv[argv.index("--memory") + 1] == "2g"
        assert argv[argv.index("--cpus") + 1] == "2"

    def test_workdir_is_the_scope_root(self, hook):
        """lint.sh resolves .multilint.json and .markdownlint.json relative to the working
        directory, so getting this wrong silently ignores per-project configuration."""
        argv = hook.build_docker_argv(Path("/home/u/proj"), ["a.py"], image="img")
        assert argv[argv.index("--workdir") + 1] == "/home/u/proj"

    def test_targets_come_last_after_the_argv0_placeholder(self, hook):
        """`bash -c <program> _ f1 f2` puts f1/f2 in "$@"; the `_` fills $0."""
        argv = hook.build_docker_argv(Path("/p"), ["a.py", "b/c.sh"], image="img")
        assert argv[-3:] == ["_", "a.py", "b/c.sh"]

    def test_image_is_positioned_before_the_program(self, hook):
        argv = hook.build_docker_argv(Path("/p"), ["a.py"], image="my/img:tag")
        assert argv[argv.index("my/img:tag") + 1] == "-c"

    def test_entrypoint_is_overridden(self, hook):
        """The image entrypoint starts the servers in continuous mode; the hook wants one lint."""
        argv = hook.build_docker_argv(Path("/p"), ["a.py"], image="img")
        assert argv[argv.index("--entrypoint") + 1] == "bash"

    def test_program_runs_lint_sh_per_target(self, hook):
        argv = hook.build_docker_argv(Path("/p"), ["a.py"], image="img")
        program = argv[argv.index("-c") + 1]
        assert "/usr/local/bin/lint.sh" in program
        assert "--format json" in program
        assert 'for f in "$@"' in program

    def test_program_keeps_going_after_a_failing_file(self, hook):
        """One bad file must not stop the rest from being checked, but the run must still fail."""
        program = hook.container_program()
        assert "|| rc=1" in program
        assert "exit $rc" in program

    def test_program_marks_both_streams(self, hook):
        """JSON lands on stdout and human findings on stderr, so both need the marker to be
        split back into per-file results."""
        program = hook.container_program()
        assert program.count(hook.FILE_MARKER) == 2
        assert ">&2" in program


class TestImageResolution:
    """Image comes from the "image" hook-setting now, not MULTILINT_IMAGE (see TestHookSettings)."""

    def test_default_is_the_published_tag_when_no_setting(self, hook, monkeypatch):
        monkeypatch.setattr(hook, "read_setting", lambda key: None)
        assert hook.resolve_image() == hook.DEFAULT_IMAGE


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

    def test_unparsable_document_is_dropped_not_guessed(self, hook, monkeypatch):
        """A malformed result must look like "nothing to report", never like a finding."""
        monkeypatch.setattr(hook.shutil, "which", lambda _name: "/usr/bin/docker")

        class _Proc:
            stdout = f"{hook.FILE_MARKER}a.py\nthis is not json\n"
            stderr = ""

        monkeypatch.setattr(hook.subprocess, "run", lambda *a, **k: _Proc())
        assert hook.run_lint(Path("/p"), ["a.py"]) == []

    def test_docker_absent_is_a_silent_no_op(self, hook, monkeypatch):
        """A machine without Docker gets no findings and no error, so edits are never blocked."""
        monkeypatch.setattr(hook.shutil, "which", lambda _name: None)
        assert hook.run_lint(Path("/p"), ["a.py"]) == []

    def test_subprocess_failure_is_a_silent_no_op(self, hook, monkeypatch):
        monkeypatch.setattr(hook.shutil, "which", lambda _name: "/usr/bin/docker")

        def _boom(*_args, **_kwargs):
            raise OSError("no such binary")

        monkeypatch.setattr(hook.subprocess, "run", _boom)
        assert hook.run_lint(Path("/p"), ["a.py"]) == []


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
    """--set/--get/--unset: the fixed two-key allowlist that replaced MULTILINT_IMAGE and
    MULTILINT_SEARCH_CEILING. Run as a real subprocess against a temporary changes.db so the
    argv-gated CLI branch ahead of main()'s stdin read is exercised for real, not just the
    underlying functions."""

    def _cli_env(self, tmp_path):
        return {**os.environ, "XDG_STATE_HOME": str(tmp_path)}

    def test_set_then_get_round_trips_a_value(self, tmp_path):
        env = self._cli_env(tmp_path)
        subprocess.run(
            [sys.executable, str(HOOK_PATH), "--set", "image", "local/build:dev"],
            env=env,
            check=True,
        )
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH), "--get", "image"],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        assert result.stdout.strip() == "local/build:dev"

    def test_unset_removes_the_value(self, tmp_path):
        env = self._cli_env(tmp_path)
        subprocess.run(
            [sys.executable, str(HOOK_PATH), "--set", "image", "local/build:dev"],
            env=env,
            check=True,
        )
        subprocess.run([sys.executable, str(HOOK_PATH), "--unset", "image"], env=env, check=True)
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH), "--get", "image"],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        assert result.stdout.strip() == ""

    def test_unknown_key_is_rejected_and_nothing_is_written(self, tmp_path):
        env = self._cli_env(tmp_path)
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH), "--set", "eslint", "x"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode != 0
        db_path = tmp_path / "multilint" / "changes.db"
        if db_path.exists():
            with sqlite3.connect(db_path) as connection:
                row = connection.execute("SELECT 1 FROM settings WHERE key = ?", ("eslint",)).fetchone()
                assert row is None

    def test_resolvers_default_when_no_row_exists(self, hook, tmp_path, monkeypatch):
        """Absent db/table/row all resolve to the built-in default, not an error."""
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert hook.resolve_image() == hook.DEFAULT_IMAGE
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
        baseline_image = hook.resolve_image()
        baseline_ceiling = hook.search_ceiling()
        baseline_state_dir = hook.state_dir()

        monkeypatch.setenv("MULTILINT_IMAGE", "local/other:dev")
        monkeypatch.setenv("MULTILINT_SEARCH_CEILING", str(tmp_path / "elsewhere"))
        monkeypatch.setenv("MULTILINT_STATE_DIR", str(tmp_path / "elsewhere" / "state"))

        assert hook.resolve_image() == baseline_image
        assert hook.search_ceiling() == baseline_ceiling
        assert hook.state_dir() == baseline_state_dir
