"""Unit tests for the Claude Code hook body, claude-plugin/scripts/lint_changed.py.

Nothing here starts a container. The argv builder is a pure function precisely so that the
invocation can be asserted on a machine with no Docker and no image, which is what CI is.
"""

# pylint: disable=redefined-outer-name,protected-access
import importlib.util
import json
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
    def test_env_overrides_default(self, hook, monkeypatch):
        monkeypatch.setenv("MULTILINT_IMAGE", "local/build:dev")
        assert hook.resolve_image() == "local/build:dev"

    def test_default_is_the_published_tag(self, hook, monkeypatch):
        monkeypatch.delenv("MULTILINT_IMAGE", raising=False)
        assert hook.resolve_image() == hook.DEFAULT_IMAGE

    def test_empty_env_falls_back(self, hook, monkeypatch):
        """An exported-but-empty variable is a configuration accident, not a request for ''."""
        monkeypatch.setenv("MULTILINT_IMAGE", "")
        assert hook.resolve_image() == hook.DEFAULT_IMAGE


class TestRetiredConfiguration:
    """The removed variables are reported rather than silently ignored."""

    def test_detects_retired_variables(self, hook, monkeypatch):
        monkeypatch.setenv("MULTILINT_URL", "http://localhost:8591/lint")
        monkeypatch.setenv("MULTILINT_PATH_MAP", "/a:/b")
        assert hook.retired_env_in_use() == ["MULTILINT_URL", "MULTILINT_PATH_MAP"]

    def test_quiet_when_unset(self, hook, monkeypatch):
        for name in hook.RETIRED_ENV_VARS:
            monkeypatch.delenv(name, raising=False)
        assert hook.retired_env_in_use() == []


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

    def test_filters_gitleaks(self, hook):
        """gitleaks needs <target>/.git, which a single-file target can never have, so its skip
        carries no information and would otherwise fire after every edit."""
        assert hook.skipped_checks(self._document(["gitleaks"])) == []
        assert hook.skipped_checks(self._document(["gitleaks", "mypy"])) == ["mypy"]

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


class TestTargetSelection:
    """The per-run cap.

    The changed set is everything the repository has dirty, which the triggering edit does not bound.
    Without a cap, one edit in a tree with 83 dirty files queues 52 lint runs, and a larger tree runs
    past the container timeout and reports nothing — the worst available outcome, because an
    unchecked file then looks exactly like a clean one.
    """

    def test_small_sets_pass_through_untouched(self, hook):
        targets = ["a.py", "b.py", "c.sh"]
        assert hook.select_targets(targets, "a.py") == (targets, 0)

    def test_set_at_the_limit_is_not_trimmed(self, hook):
        targets = [f"f{i}.py" for i in range(hook.MAX_LINT_TARGETS)]
        kept, dropped = hook.select_targets(targets, "f0.py")
        assert dropped == 0
        assert kept == targets

    def test_oversized_set_is_trimmed_to_the_limit(self, hook):
        targets = [f"f{i}.py" for i in range(hook.MAX_LINT_TARGETS + 10)]
        kept, dropped = hook.select_targets(targets, "f0.py")
        assert len(kept) == hook.MAX_LINT_TARGETS
        assert dropped == 10

    def test_edited_file_survives_trimming(self, hook):
        """It is the file the user just wrote; dropping it would make the hook pointless."""
        targets = [f"f{i}.py" for i in range(hook.MAX_LINT_TARGETS + 10)]
        edited = targets[-1]
        kept, _ = hook.select_targets(targets, edited)
        assert edited in kept
        assert kept[0] == edited

    def test_no_duplicate_when_edited_is_already_early(self, hook):
        targets = [f"f{i}.py" for i in range(hook.MAX_LINT_TARGETS + 5)]
        kept, _ = hook.select_targets(targets, "f0.py")
        assert kept.count("f0.py") == 1
        assert len(kept) == len(set(kept))

    def test_tolerates_an_unmappable_edited_file(self, hook):
        """edited is None when it resolved outside the scope root."""
        targets = [f"f{i}.py" for i in range(hook.MAX_LINT_TARGETS + 3)]
        kept, dropped = hook.select_targets(targets, None)
        assert len(kept) == hook.MAX_LINT_TARGETS
        assert dropped == 3


class TestFailingLines:
    def test_keeps_only_marked_lines(self, hook):
        output = "  ✓ flake8\n  ✗ black\n  ⚠ pylint: 3 failures\n  📄 file.sh\n"
        assert hook.failing_lines(output) == ["  ✗ black", "  ⚠ pylint: 3 failures"]
