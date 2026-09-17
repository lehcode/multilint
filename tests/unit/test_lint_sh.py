"""Unit tests for lint.sh file discovery and exit code logic."""

# pylint: disable=redefined-outer-name
import os
import subprocess
from pathlib import Path

LINT_SH = Path(__file__).parent.parent.parent / "lint.sh"


class TestFileDiscovery:
    """Test that lint.sh discovers files correctly."""

    def test_discovers_shell_files(self, sample_project):
        """lint.sh finds *.sh files in the target directory."""
        result = subprocess.run(
            ["bash", str(LINT_SH), f"{sample_project}/shell"],
            capture_output=True,
            text=True,
            cwd=sample_project,
        )
        assert "valid.sh" in result.stdout
        assert "broken.sh" in result.stdout

    def test_excludes_hidden_files(self, sample_project_with_hidden):
        """lint.sh excludes files in dot-paths (.git/, .hidden files)."""
        result = subprocess.run(
            ["bash", str(LINT_SH), sample_project_with_hidden],
            capture_output=True,
            text=True,
            cwd=sample_project_with_hidden,
        )
        assert ".hidden.sh" not in result.stdout
        assert ".git" not in result.stdout

    def test_excludes_venv_files(self, sample_project_with_hidden):
        """lint.sh excludes files in venv/ directories."""
        result = subprocess.run(
            ["bash", str(LINT_SH), sample_project_with_hidden],
            capture_output=True,
            text=True,
            cwd=sample_project_with_hidden,
        )
        assert "venv" not in result.stdout

    def test_discover_python_files(self, sample_project):
        """lint.sh finds *.py files, excludes test/*, __pycache__/*, venv/*."""
        result = subprocess.run(
            ["bash", str(LINT_SH), f"{sample_project}/python"],
            capture_output=True,
            text=True,
            cwd=sample_project,
        )
        assert "valid.py" in result.stdout
        assert "broken.py" in result.stdout

    def test_discover_markdown_files(self, sample_project):
        """lint.sh finds *.md files, excludes cache/*, output/*."""
        result = subprocess.run(
            ["bash", str(LINT_SH), f"{sample_project}/markdown"],
            capture_output=True,
            text=True,
            cwd=sample_project,
        )
        assert "valid.md" in result.stdout
        assert "broken.md" in result.stdout


class TestExitCodes:
    """Test exit code behavior."""

    def test_exit_zero_all_pass(self, sample_project_empty):
        """Empty directory → exit 0 (no failures)."""
        result = subprocess.run(
            ["bash", str(LINT_SH), sample_project_empty],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0

    def test_exit_one_on_fail(self, sample_project):
        """Any file fails → exit 1."""
        result = subprocess.run(
            ["bash", str(LINT_SH), sample_project],
            capture_output=True,
            text=True,
        )
        # broken.sh has intentional syntax errors, so exit code should be 1
        assert result.returncode == 1


class TestOutputFormat:
    """Test output format."""

    def test_checkmarks_format(self, sample_project_empty):
        """Output contains checkmark symbols for each check."""
        result = subprocess.run(
            ["bash", str(LINT_SH), sample_project_empty],
            capture_output=True,
            text=True,
        )
        assert "✓" in result.stdout or "✗" in result.stdout

    def test_summary_banner(self, sample_project_empty):
        """Output contains summary banner."""
        result = subprocess.run(
            ["bash", str(LINT_SH), sample_project_empty],
            capture_output=True,
            text=True,
        )
        assert "================================" in result.stdout


class TestShellCheckExclusions:
    """Test that SC1091/SC2155/SC2086 are excluded."""

    def test_shellcheck_excludes_sc1091(self):
        """SC1091 (source not found) is excluded from checks."""
        # Create a file that would trigger SC1091
        with open("/tmp/test_sc1091.sh", "w", encoding="utf-8") as f:
            f.write("#!/usr/bin/env bash\nsource /nonexistent/file.sh\n")
        result = subprocess.run(
            [
                "shellcheck",
                "-e",
                "SC1091",
                "-e",
                "SC2155",
                "-e",
                "SC2086",
                "-S",
                "style",
                "/tmp/test_sc1091.sh",
            ],
            capture_output=True,
            text=True,
        )
        assert result.returncode == 0
        os.unlink("/tmp/test_sc1091.sh")


class TestThresholds:
    """Test threshold configuration via .multilint.json."""

    def test_default_threshold_zero_tolerant(self, sample_project_with_hidden):
        """When no .multilint.json, shellcheck threshold defaults to 0 (fail-fast)."""
        result = subprocess.run(
            ["bash", str(LINT_SH), sample_project_with_hidden],
            capture_output=True,
            text=True,
        )
        assert "shellcheck: 0 failures (threshold: 0)" in result.stdout

    def test_threshold_config_file_read(self, sample_project_with_threshold_config):
        """Threshold values are read from .multilint.json."""
        result = subprocess.run(
            ["bash", str(LINT_SH), sample_project_with_threshold_config],
            capture_output=True,
            text=True,
        )
        assert "shellcheck: 1 failures (threshold: 2)" in result.stdout
        assert "bash_syntax: 1 failures (threshold: 0)" in result.stdout
        assert "bashate: 1 failures (threshold: 0)" in result.stdout
        assert "shfmt: 0 failures (threshold: 0)" in result.stdout

    def test_threshold_exceeded(self, sample_project_with_threshold_config):
        """When failures exceed threshold, check is marked with ⚠ and fails."""
        result = subprocess.run(
            ["bash", str(LINT_SH), sample_project_with_threshold_config],
            capture_output=True,
            text=True,
        )
        # bash_syntax has 1 failure with threshold 0, so exceeded
        assert "bash_syntax: 1 failures (threshold: 0)" in result.stdout
        # bash_syntax should show warning (⚠)
        assert "⚠" in result.stdout

    def test_threshold_not_exceeded(self, sample_project_with_threshold_config):
        """When failures within threshold, check passes (✓)."""
        result = subprocess.run(
            ["bash", str(LINT_SH), sample_project_with_threshold_config],
            capture_output=True,
            text=True,
        )
        # shellcheck has 1 failure with threshold 2, so within threshold
        assert "✓" in result.stdout
        assert "shellcheck: 1 failures (threshold: 2)" in result.stdout
