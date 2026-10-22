"""Unit tests for core/ml_container.py — container program, Docker argv, result parsing, and run_lint."""

# pylint: disable=redefined-outer-name,protected-access,unused-argument
import json
import os
import subprocess

import ml_container
import ml_settings
import pytest
from pathlib import Path


class TestContainerProgram:
    """The bash program run inside the container."""

    def test_program_keeps_going_after_a_failing_file(self, hook):
        """One bad file must not stop the rest from being checked, but the run must still fail."""
        program = ml_container.container_program()
        assert "|| rc=1" in program
        assert "exit $rc" in program

    def test_program_marks_both_streams(self, hook):
        """JSON lands on stdout and human findings on stderr, so both need the marker to be
        split back into per-file results."""
        program = ml_container.container_program()
        assert program.count(ml_container.FILE_MARKER) == 2
        assert ">&2" in program

    def test_program_runs_container_lint_sh(self, hook):
        """The program runs lint.sh from CONTAINER_SCRIPTS, not a host path."""
        program = ml_container.container_program()
        assert f"{ml_container.CONTAINER_SCRIPTS}/lint.sh" in program

    def test_program_has_no_host_path_reference(self, hook):
        """No /usr/local/bin or other host path in the program."""
        program = ml_container.container_program()
        assert "/usr/local/bin" not in program


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
        stdout = "".join(
            f"{ml_container.FILE_MARKER}{name}\n{json.dumps(self._document())}\n" for name in ("a.py", "b.sh")
        )
        assert sorted(ml_container.split_stream(stdout)) == ["a.py", "b.sh"]

    def test_pairs_detail_with_its_file(self, hook):
        """stderr detail must be attributed to the right file, not concatenated."""
        stderr = f"{ml_container.FILE_MARKER}a.py\n  ✗ flake8\n{ml_container.FILE_MARKER}b.sh\n  ✗ shellcheck\n"
        sections = ml_container.split_stream(stderr)
        assert "flake8" in sections["a.py"]
        assert "shellcheck" in sections["b.sh"]
        assert "shellcheck" not in sections["a.py"]

    def test_unparsable_document_is_unavailable_not_guessed(self, hook, monkeypatch, tmp_path):
        """A malformed result is never reported as a finding; it makes the file unverified."""
        (tmp_path / "lint.sh").touch()
        monkeypatch.setattr(ml_container.shutil, "which", lambda _name: "/usr/bin/docker")

        class _Proc:
            stdout = f"{ml_container.FILE_MARKER}a.py\nthis is not json\n"
            stderr = ""
            returncode = 1

        monkeypatch.setattr(ml_container.subprocess, "run", lambda *_a, **_k: _Proc())
        with pytest.raises(ml_container.LintUnavailable, match="no readable result"):
            ml_container.run_lint(tmp_path, ["a.py"])

    def test_missing_lint_sh_is_unavailable(self, hook, monkeypatch, tmp_path):
        monkeypatch.setattr(ml_container.shutil, "which", lambda _name: "/usr/bin/docker")
        monkeypatch.setattr(ml_container, "scripts_dir", lambda: tmp_path)
        with pytest.raises(ml_container.LintUnavailable, match="lint.sh not found"):
            ml_container.run_lint(Path("/p"), ["a.py"])

    def test_docker_error_is_named_in_the_notice(self, hook, monkeypatch, tmp_path):
        """A daemon or pull failure prints before any marker; that line is the reason, not lint.sh's output."""
        monkeypatch.setattr(ml_container.shutil, "which", lambda _name: "/usr/bin/docker")

        class _Proc:
            stdout = ""
            stderr = "Unable to find image 'x:1' locally\ndocker: Error response from daemon: pull access denied.\n"
            returncode = 125

        monkeypatch.setattr(ml_container.subprocess, "run", lambda *_a, **_k: _Proc())
        with pytest.raises(ml_container.LintUnavailable) as caught:
            ml_container.run_lint(tmp_path, ["a.py"])
        assert str(caught.value) == (
            "the ephemeral container produced no readable result "
            "(exit status 125: docker: Error response from daemon: pull access denied.)"
        )

    def test_missing_docker_is_unavailable(self, hook, monkeypatch):
        """When docker is missing, LintUnavailable is raised."""
        monkeypatch.setattr(ml_container.shutil, "which", lambda _name: None)
        with pytest.raises(ml_container.LintUnavailable, match="docker not found"):
            ml_container.run_lint(Path("/p"), ["a.py"])

    def test_subprocess_failure_is_unavailable(self, hook, monkeypatch, tmp_path):
        (tmp_path / "lint.sh").touch()
        monkeypatch.setattr(ml_container.shutil, "which", lambda _name: "/usr/bin/docker")

        def _boom(*_args, **_kwargs):
            raise OSError("no such binary")

        monkeypatch.setattr(ml_container.subprocess, "run", _boom)
        with pytest.raises(ml_container.LintUnavailable, match="could not be started"):
            ml_container.run_lint(tmp_path, ["a.py"])


class TestDockerRun:
    """Tests for docker execution."""

    def test_run_lint_calls_docker(self, hook, monkeypatch, tmp_path):
        """run_lint invokes docker with the right argv."""
        (tmp_path / "lint.sh").touch()
        calls = []

        def mock_run(*args, **kwargs):
            calls.append((args[0], kwargs))

            class _Proc:
                stdout = f'{ml_container.FILE_MARKER}a.py\n{{"checks": {{}}}}\n'
                stderr = ""
                returncode = 0

            return _Proc()

        monkeypatch.setattr(ml_container.subprocess, "run", mock_run)
        monkeypatch.setattr(ml_container.shutil, "which", lambda _: "/usr/bin/docker")

        assert ml_container.run_lint(tmp_path, ["a.py"]) == [("a.py", {"checks": {}}, "")]

        argv = calls[0][0]
        assert argv == ml_container.build_docker_argv(tmp_path, ["a.py"], name=argv[argv.index("--name") + 1])
        assert calls[0][1]["timeout"] == ml_container.RUN_TIMEOUT_SECONDS

    def test_run_lint_handles_timeout(self, hook, monkeypatch, tmp_path):
        """A timeout makes the file unverified, never a silent pass."""
        (tmp_path / "lint.sh").touch()
        monkeypatch.setattr(ml_container.shutil, "which", lambda _: "/usr/bin/docker")

        def mock_run(*_args, **_kwargs):
            raise subprocess.TimeoutExpired("docker", 300)

        monkeypatch.setattr(ml_container.subprocess, "run", mock_run)

        with pytest.raises(ml_container.LintUnavailable, match="ephemeral container did not finish within 300 s"):
            ml_container.run_lint(tmp_path, ["a.py"])

    def test_timed_out_container_is_removed_by_name(self, hook, monkeypatch, tmp_path):
        """Killing the docker client leaves the container running, so the hook removes it."""
        monkeypatch.setattr(ml_container.shutil, "which", lambda _: "/usr/bin/docker")
        calls = []

        def mock_run(argv, **_kwargs):
            calls.append(argv)
            if argv[1] == "run":
                raise subprocess.TimeoutExpired("docker", 300)

        monkeypatch.setattr(ml_container.subprocess, "run", mock_run)
        with pytest.raises(ml_container.LintUnavailable):
            ml_container.run_lint(tmp_path, ["a.py"])
        name = calls[0][calls[0].index("--name") + 1]
        assert calls[1] == ["docker", "rm", "-f", name]


class TestDockerArgv:
    """build_docker_argv() produces the exact sandboxed `docker run` for one ephemeral container."""

    def test_exact_argv(self, hook, monkeypatch, tmp_path):
        monkeypatch.setattr(ml_settings, "read_setting", lambda key: None)
        scripts = ml_container.scripts_dir()
        assert ml_container.build_docker_argv(tmp_path, ["a.py", "b.sh"], name="multilint-1-ab") == [
            "docker",
            "run",
            "--rm",
            "--name",
            "multilint-1-ab",
            "--network",
            "none",
            "--memory",
            "2g",
            "--cpus",
            "2",
            "--mount",
            f"type=bind,source={tmp_path},target={tmp_path},readonly",
            "--mount",
            f"type=bind,source={scripts},target=/opt/multilint,readonly",
            "--workdir",
            str(tmp_path),
            "--entrypoint",
            "bash",
            "--user",
            f"{os.getuid()}:{os.getgid()}",
            ml_settings.DEFAULT_IMAGE,
            "-c",
            ml_container.container_program(),
            "_",
            "a.py",
            "b.sh",
        ]

    def test_mount_spec_quotes_csv_special_characters(self, hook):
        """docker parses --mount as CSV: an unquoted ',' in a path would split it into bogus keys."""
        assert ml_container.bind_mount("/a/b", "/t") == "type=bind,source=/a/b,target=/t,readonly"
        assert ml_container.bind_mount("/a,b", "/t") == 'type=bind,"source=/a,b",target=/t,readonly'
        assert ml_container.bind_mount('/a"b', "/t") == 'type=bind,"source=/a""b",target=/t,readonly'

    def test_each_container_gets_a_unique_name(self, hook):
        assert ml_container.container_name() != ml_container.container_name()
        assert ml_container.container_name().startswith("multilint-")

    def test_scripts_dir_holds_lint_sh(self, hook):
        assert (ml_container.scripts_dir() / "lint.sh").is_file()

    def test_explicit_image_wins(self, hook, monkeypatch, tmp_path):
        monkeypatch.setattr(ml_settings, "read_setting", lambda key: "from/setting:1")
        assert "myimg:v1" in ml_container.build_docker_argv(tmp_path, ["a.py"], "myimg:v1")
        assert "from/setting:1" not in ml_container.build_docker_argv(tmp_path, ["a.py"], "myimg:v1")

    def test_image_setting_overrides_default(self, hook, monkeypatch, tmp_path):
        monkeypatch.setattr(ml_settings, "read_setting", lambda key: "local/multilint:dev" if key == "image" else None)
        argv = ml_container.build_docker_argv(tmp_path, ["a.py"])
        assert "local/multilint:dev" in argv
        assert ml_settings.DEFAULT_IMAGE not in argv
