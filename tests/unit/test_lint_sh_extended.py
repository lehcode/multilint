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
