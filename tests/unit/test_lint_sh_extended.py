"""Extended unit tests for lint.sh — JSON output, feature toggles, edge cases."""

# pylint: disable=redefined-outer-name,subprocess-run-check
import json
import os
import subprocess
from pathlib import Path

LINT_SH = Path(__file__).parent.parent.parent / "lint.sh"


class TestJSONOutput:
    """Tests for --format json output."""

    def test_json_format_flag(self, sample_project_empty):
        """--format json produces valid JSON output."""
        result = subprocess.run(
            ["bash", str(LINT_SH), sample_project_empty, "--format", "json"],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        data = json.loads(result.stdout)
        assert "summary" in data
        assert "checks" in data
        assert "return_code" in data
        assert "files" in data

    def test_json_summary_fields(self, sample_project_empty):
        """JSON output contains correct summary fields."""
        result = subprocess.run(
            ["bash", str(LINT_SH), sample_project_empty, "--format", "json"],
            capture_output=True,
            text=True,
        )
        data = json.loads(result.stdout)
        assert "files_checked" in data["summary"]
        assert "checks_run" in data["summary"]
        assert isinstance(data["summary"]["files_checked"], int)
        assert isinstance(data["summary"]["checks_run"], int)

    def test_json_checks_structure(self, sample_project_empty):
        """JSON checks contain per-check failure/threshold data."""
        result = subprocess.run(
            ["bash", str(LINT_SH), sample_project_empty, "--format", "json"],
            capture_output=True,
            text=True,
        )
        data = json.loads(result.stdout)
        expected_checks = [
            "bash_syntax",
            "shellcheck",
            "bashate",
            "shfmt",
            "flake8",
            "black",
            "pylint",
            "markdownlint",
        ]
        for check in expected_checks:
            assert check in data["checks"], f"Check {check} missing from JSON output"
            assert "passed" in data["checks"][check]
            assert "failed" in data["checks"][check]
            assert "threshold" in data["checks"][check]
            assert "threshold_exceeded" in data["checks"][check]

    def test_json_files_list(self, sample_project):
        """JSON output lists all checked files."""
        result = subprocess.run(
            ["bash", str(LINT_SH), sample_project, "--format", "json"],
            capture_output=True,
            text=True,
        )
        data = json.loads(result.stdout)
        assert "files" in data
        assert len(data["files"]) > 0
        file_types = set(Path(f).suffix for f in data["files"])
        assert ".sh" in file_types
        assert ".py" in file_types
        assert ".md" in file_types

    def test_json_exit_code_reflects_failures(self, sample_project):
        """JSON return_code is 1 when files fail checks."""
        result = subprocess.run(
            ["bash", str(LINT_SH), sample_project, "--format", "json"],
            capture_output=True,
            text=True,
        )
        data = json.loads(result.stdout)
        assert data["return_code"] == 1

    def test_text_format_unchanged(self, sample_project_empty):
        """Default text format still works (backward compat)."""
        result = subprocess.run(
            ["bash", str(LINT_SH), sample_project_empty],
            capture_output=True,
            text=True,
        )
        assert "✓" in result.stdout or "✗" in result.stdout
        assert "================================" in result.stdout


class TestFeatureToggles:
    """Tests for MULTILINT_* feature toggle environment variables."""

    def test_black_disabled(self, tmp_dir):
        """MULTILINT_BLACK_CHECK=off skips black check."""
        Path(tmp_dir, "test.py").write_text("x=1+1\n", encoding="utf-8")
        result = subprocess.run(
            ["bash", str(LINT_SH), tmp_dir],
            capture_output=True,
            text=True,
            env={**os.environ, "MULTILINT_BLACK_CHECK": "off"},
        )
        assert "black (disabled)" in result.stdout or "black" not in result.stdout

    def test_shfmt_disabled(self, tmp_dir):
        """MULTILINT_SHFMT_CHECK=off skips shfmt check."""
        Path(tmp_dir, "test.sh").write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
        result = subprocess.run(
            ["bash", str(LINT_SH), tmp_dir],
            capture_output=True,
            text=True,
            env={**os.environ, "MULTILINT_SHFMT_CHECK": "off"},
        )
        assert "shfmt (disabled)" in result.stdout or "shfmt" not in result.stdout

    def test_bashate_disabled(self, tmp_dir):
        """MULTILINT_BASHATE_CHECK=off skips bashate check."""
        Path(tmp_dir, "test.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\necho hi\n", encoding="utf-8")
        result = subprocess.run(
            ["bash", str(LINT_SH), tmp_dir],
            capture_output=True,
            text=True,
            env={**os.environ, "MULTILINT_BASHATE_CHECK": "off"},
        )
        assert "bashate (disabled)" in result.stdout or "bashate" not in result.stdout

    def test_all_checks_disabled(self, sample_project_empty):
        """All checks disabled → exit 0."""
        result = subprocess.run(
            ["bash", str(LINT_SH), sample_project_empty],
            capture_output=True,
            text=True,
            env={
                **os.environ,
                "MULTILINT_BLACK_CHECK": "off",
                "MULTILINT_SHFMT_CHECK": "off",
                "MULTILINT_BASHATE_CHECK": "off",
            },
        )
        assert result.returncode == 0


class TestEdgeCases:
    """Tests for edge cases and error conditions."""

    def test_empty_directory(self, tmp_dir):
        """Empty directory → exit 0, no errors."""
        result = subprocess.run(
            ["bash", str(LINT_SH), tmp_dir],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        assert "✓" in result.stdout

    def test_hidden_files_excluded(self, tmp_dir):
        """Hidden files (.git, .env) are excluded."""
        Path(tmp_dir, ".hidden.sh").write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
        (Path(tmp_dir) / ".git").mkdir()
        Path(tmp_dir, ".git", "hook.sh").write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
        result = subprocess.run(
            ["bash", str(LINT_SH), tmp_dir],
            capture_output=True,
            text=True,
        )
        assert ".hidden.sh" not in result.stdout
        assert ".git" not in result.stdout

    def test_venv_excluded(self, tmp_dir):
        """venv/ directories are excluded."""
        (Path(tmp_dir) / "venv").mkdir()
        Path(tmp_dir, "venv", "script.sh").write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
        result = subprocess.run(
            ["bash", str(LINT_SH), tmp_dir],
            capture_output=True,
            text=True,
        )
        assert "venv" not in result.stdout

    def test_special_characters_in_filename(self, tmp_dir):
        """Files with special characters in names are handled."""
        Path(tmp_dir, "file with spaces.sh").write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
        Path(tmp_dir, "file-dash.sh").write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
        result = subprocess.run(
            ["bash", str(LINT_SH), tmp_dir],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

    def test_nested_directories(self, tmp_dir):
        """Nested directories are traversed."""
        (Path(tmp_dir) / "a" / "b").mkdir(parents=True)
        Path(tmp_dir, "a", "b", "nested.sh").write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
        result = subprocess.run(
            ["bash", str(LINT_SH), tmp_dir],
            capture_output=True,
            text=True,
        )
        assert "nested.sh" in result.stdout

    def test_no_matching_files(self, tmp_dir):
        """Directory with no matching files → exit 0."""
        Path(tmp_dir, "data.txt").write_text("no matching files here\n", encoding="utf-8")
        result = subprocess.run(
            ["bash", str(LINT_SH), tmp_dir],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

    def test_binary_file_excluded(self, tmp_dir):
        """Binary files are not processed."""
        Path(tmp_dir, "binary.sh").write_bytes(b"\x00\x01\x02\x03")
        result = subprocess.run(
            ["bash", str(LINT_SH), tmp_dir],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0 or "error" in result.stdout.lower()


class TestThresholdConfig:
    """Tests for .multilint.json threshold configuration."""

    def test_missing_config_defaults_zero(self, tmp_dir):
        """No .multilint.json → all thresholds default to 0."""
        Path(tmp_dir, "test.sh").write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
        result = subprocess.run(
            ["bash", str(LINT_SH), tmp_dir],
            capture_output=True,
            text=True,
        )
        assert "threshold: 0" in result.stdout

    def test_partial_config(self, tmp_dir):
        """Only some checks configured → others default to 0."""
        Path(tmp_dir, ".multilint.json").write_text(json.dumps({"bash_syntax": 1, "shellcheck": 0}), encoding="utf-8")
        Path(tmp_dir, "test.sh").write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
        result = subprocess.run(
            ["bash", str(LINT_SH), tmp_dir],
            capture_output=True,
            text=True,
        )
        assert "bash_syntax: 0 failures (threshold: 1)" in result.stdout
        assert "threshold: 0" in result.stdout

    def test_invalid_json_config(self, tmp_dir):
        """Malformed .multilint.json → defaults to 0 (graceful)."""
        Path(tmp_dir, ".multilint.json").write_text("{ invalid json }", encoding="utf-8")
        Path(tmp_dir, "test.sh").write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
        result = subprocess.run(
            ["bash", str(LINT_SH), tmp_dir],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0 or "error" in result.stdout.lower() or "threshold: 0" in result.stdout


class TestGitleaks:
    """Tests for gitleaks git history secret detection."""

    def test_gitleaks_no_git(self, tmp_dir):
        """Gitleaks skipped when no .git directory."""
        result = subprocess.run(
            ["bash", str(LINT_SH), tmp_dir],
            capture_output=True,
            text=True,
        )
        assert "gitleaks (.git not found, skipping)" in result.stdout

    def test_gitleaks_disabled(self, sample_project_empty):
        """MULTILINT_GITLEAKS_CHECK=off skips gitleaks."""
        result = subprocess.run(
            ["bash", str(LINT_SH), sample_project_empty],
            capture_output=True,
            text=True,
            env={**os.environ, "MULTILINT_GITLEAKS_CHECK": "off"},
        )
        assert "gitleaks (disabled)" in result.stdout

    def test_gitleaks_in_json(self, sample_project_empty):
        """gitleaks appears in JSON output."""
        result = subprocess.run(
            ["bash", str(LINT_SH), sample_project_empty, "--format", "json"],
            capture_output=True,
            text=True,
        )
        data = json.loads(result.stdout)
        assert "gitleaks" in data["checks"]

    def test_gitleaks_threshold_in_json(self, sample_project_empty):
        """JSON gitleaks entry has threshold field."""
        result = subprocess.run(
            ["bash", str(LINT_SH), sample_project_empty, "--format", "json"],
            capture_output=True,
            text=True,
        )
        data = json.loads(result.stdout)
        assert "threshold" in data["checks"]["gitleaks"]

    def test_gitleaks_config_file_exists(self):
        """/.gitleaks.toml is bundled in the image."""
        config_path = Path(__file__).parent.parent.parent / ".gitleaks.toml"
        assert config_path.exists()
        content = config_path.read_text()
        assert "useDefault" in content or "extend" in content


# Every check lint.sh knows about. Kept here as a literal on purpose: the point
# is to fail when lint.sh grows a check that never reaches the JSON, which is
# exactly how mypy and bandit stayed invisible to consumers after being added.
ALL_CHECKS = (
    "bash_syntax",
    "shellcheck",
    "bashate",
    "shfmt",
    "flake8",
    "black",
    "pylint",
    "mypy",
    "bandit",
    "markdownlint",
    "yaml_prettier",
    "json_prettier",
    "toml_sort",
    "security_secrets",
    "security_dangerous_patterns",
    "gitleaks",
)


def _lint_json(target, env=None):
    """Run lint.sh --format json against target and return the parsed document."""
    result = subprocess.run(
        ["bash", str(LINT_SH), str(target), "--format", "json"],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
    )
    assert result.stdout, f"no JSON on stdout; stderr tail: {result.stderr[-500:]}"
    return json.loads(result.stdout)


class TestJSONContract:
    """The JSON contract the plugin consumes.

    These assert invariants rather than individual tool verdicts, deliberately.
    Two existing tests in this suite depend on bashate and gitleaks being
    installed and change behaviour when they are not, which makes them report on
    the environment rather than on lint.sh. Nothing below cares which linters the
    host happens to have.
    """

    def test_every_check_reaches_json(self, sample_project):
        """No check is missing from the emitted document."""
        data = _lint_json(sample_project)
        assert set(data["checks"]) == set(ALL_CHECKS)

    def test_checks_run_is_derived(self, sample_project):
        """summary.checks_run counts the checks actually emitted.

        It was hardcoded to 11 while lint.sh tracked 16, so the summary
        contradicted the object it summarised.
        """
        data = _lint_json(sample_project)
        assert data["summary"]["checks_run"] == len(data["checks"])
        assert data["summary"]["checks_run"] == len(ALL_CHECKS)

    def test_status_vocabulary(self, sample_project):
        """status only ever takes one of the three documented values."""
        for name, check in _lint_json(sample_project)["checks"].items():
            assert check["status"] in {"ok", "failed", "skipped"}, f"{name}: {check['status']}"

    def test_passed_is_consistent_with_total_and_failed(self, sample_project):
        """passed is the clean remainder, for every check."""
        checks = _lint_json(sample_project)["checks"]
        for name, check in checks.items():
            assert check["passed"] == max(check["total"] - check["failed"], 0), name
        # The loop above is vacuous if nothing ran at all, which is the state on
        # a runner with no linters installed.
        assert any(c["total"] > 0 for c in checks.values())

    def test_passed_is_not_a_second_copy_of_failed(self, tmp_dir):
        """passed counts clean units, not failures.

        Regression guard for passed and failed both being int(parts[0]).

        Built rather than taken from a fixture, because the arithmetic has to be
        unambiguous. With two files and one failure the old and new behaviour
        both yield passed == 1, so such a case proves nothing — an earlier
        version of this test asserted against `sample_project` and passed on
        CI's toolchain-free runner for exactly that reason.

        Three files, one broken: total 3, failed 1, so passed must be 2. The old
        emitter reported 1. bash -n needs no external tool, so this holds on any
        host.
        """
        proj = Path(tmp_dir) / "proj"
        proj.mkdir()
        (proj / "ok1.sh").write_text("#!/usr/bin/env bash\necho one\n")
        (proj / "ok2.sh").write_text("#!/usr/bin/env bash\necho two\n")
        (proj / "broken.sh").write_text('#!/usr/bin/env bash\nif [ "$FOO";\n  echo hi\nfi\n')

        check = _lint_json(proj)["checks"]["bash_syntax"]

        assert check["total"] == 3
        assert check["failed"] == 1
        assert check["passed"] == 2
        assert check["status"] == "failed"

    def test_skipped_checks_claim_no_work(self, sample_project):
        """A skipped check reports no units and no failures."""
        for name, check in _lint_json(sample_project)["checks"].items():
            if check["status"] == "skipped":
                assert check["total"] == 0, name
                assert check["failed"] == 0, name

    def test_failures_imply_failed_status(self, sample_project):
        """Any check with failures reports status failed, threshold or not."""
        for name, check in _lint_json(sample_project)["checks"].items():
            if check["failed"] > 0:
                assert check["status"] == "failed", name

    def test_summary_lists_skipped_checks(self, sample_project):
        """summary.checks_skipped agrees with the per-check statuses."""
        data = _lint_json(sample_project)
        expected = sorted(n for n, c in data["checks"].items() if c["status"] == "skipped")
        assert data["summary"]["checks_skipped"] == expected

    def test_disabled_check_is_skipped_not_passing(self, sample_project):
        """Switching a check off reports skipped, never a silent pass.

        Uses the documented toggle rather than PATH surgery, so the result does
        not depend on which linters the host has.
        """
        data = _lint_json(sample_project, env={"MULTILINT_BLACK_CHECK": "off"})
        assert data["checks"]["black"]["status"] == "skipped"
        assert data["checks"]["black"]["total"] == 0

    def test_check_order_is_stable(self, sample_project):
        """Key order is fixed, not inherited from the environment."""
        first = list(_lint_json(sample_project)["checks"])
        second = list(_lint_json(sample_project, env={"ML_UNRELATED": "x"})["checks"])
        assert first == second == list(ALL_CHECKS)


class TestBrokenToolIsNotAPass:
    """A tool that runs and crashes must not be reported as success.

    markdownlint shipped broken for the life of the image — the binary was copied
    without its node_modules and died with "Cannot find package 'commander'" —
    while lint.sh printed ✓ for every Markdown file. The cause was `|| true` on
    the command substitution, which made the exit status read `true`'s 0 instead
    of markdownlint's.

    The shim below reproduces that crash without needing markdownlint installed,
    so this guards the fix on any host.
    """

    @staticmethod
    def _shim(tmp_dir, name, exit_code, message):
        bin_dir = Path(tmp_dir) / "shim-bin"
        bin_dir.mkdir(exist_ok=True)
        shim = bin_dir / name
        shim.write_text(f'#!/bin/sh\necho "{message}" >&2\nexit {exit_code}\n')
        shim.chmod(0o755)
        return bin_dir

    def test_crashing_markdownlint_fails_the_check(self, tmp_dir):
        proj = Path(tmp_dir) / "proj"
        proj.mkdir()
        (proj / "doc.md").write_text("# Title\n\nBody.\n")
        bin_dir = self._shim(tmp_dir, "markdownlint", 1, "Cannot find package 'commander'")

        data = _lint_json(proj, env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})

        assert data["checks"]["markdownlint"]["status"] == "failed"
        assert data["checks"]["markdownlint"]["failed"] == 1
        assert data["return_code"] == 1

    def test_working_markdownlint_passes_the_check(self, tmp_dir):
        """The companion case, so the test above is not passing for free."""
        proj = Path(tmp_dir) / "proj"
        proj.mkdir()
        (proj / "doc.md").write_text("# Title\n\nBody.\n")
        bin_dir = self._shim(tmp_dir, "markdownlint", 0, "")

        data = _lint_json(proj, env={"PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}"})

        assert data["checks"]["markdownlint"]["status"] == "ok"
        assert data["checks"]["markdownlint"]["total"] == 1
        assert data["checks"]["markdownlint"]["failed"] == 0
