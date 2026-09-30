"""Extended unit tests for lint.sh — JSON output, feature toggles, edge cases."""

# pylint: disable=redefined-outer-name,subprocess-run-check
import json
import os
import subprocess
import tempfile
from pathlib import Path

import pytest

LINT_SH = Path(__file__).parent.parent.parent / "lint.sh"


@pytest.fixture(autouse=True)
def _ml_isolated_cwd(monkeypatch):
    """Run every test in this module from an empty directory.

    lint.sh now resolves .multilint.json as $PWD/.multilint.json first
    (defect 1 fix). Without this, subprocess.run(...) here would inherit
    pytest's cwd — the repository root, which has its own .multilint.json —
    and every test that does not pass cwd= explicitly would silently pick
    that up instead of the fixture it thinks it is exercising. Tests that
    specifically exercise $PWD lookup pass cwd= explicitly and are
    unaffected by this fixture changing the process's cwd underneath them.
    """
    with tempfile.TemporaryDirectory() as empty_dir:
        monkeypatch.chdir(empty_dir)
        yield


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
    """Tests for disabling a check via .multilint.json's checks.<name>.enabled.

    Previously these used MULTILINT_*_CHECK=off environment variables; that
    control surface is retired (user decision, 2026-09-30 -- environment
    variables are not a multilint configuration surface). The behavior being
    tested -- a check can be disabled and reports status "skipped" -- is
    unchanged; only the control surface moved to .multilint.json.
    """

    def test_black_disabled(self, tmp_dir):
        """checks.black.enabled: false skips black check."""
        Path(tmp_dir, "test.py").write_text("x=1+1\n", encoding="utf-8")
        _write_config(tmp_dir, {"checks": {"black": {"enabled": False}}})
        result = subprocess.run(
            ["bash", str(LINT_SH), tmp_dir],
            capture_output=True,
            text=True,
        )
        assert "black (disabled)" in result.stdout or "black" not in result.stdout

    def test_shfmt_disabled(self, tmp_dir):
        """checks.shfmt.enabled: false skips shfmt check."""
        Path(tmp_dir, "test.sh").write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")
        _write_config(tmp_dir, {"checks": {"shfmt": {"enabled": False}}})
        result = subprocess.run(
            ["bash", str(LINT_SH), tmp_dir],
            capture_output=True,
            text=True,
        )
        assert "shfmt (disabled)" in result.stdout or "shfmt" not in result.stdout

    def test_shfmt_indent_agrees_with_bashate(self):
        """shfmt must be given -i 4, or it contradicts bashate and nothing can pass both.

        shfmt's default indent is 0, meaning TAB indents, while bashate emits E002 "Tab indents"
        and E003 "Indent not multiple of 4". With the default, every shell file in this repository
        failed exactly one of the two checks and no file could satisfy both. That cost real work:
        claude-plugin/scripts/lint-changed.sh was written with no indented lines at all purely to
        pass both.

        Asserted on the source text rather than by running the tools, because shfmt is not
        installed on the CI runner — a behavioural test would silently skip there, which is
        precisely where this regression would land unnoticed.
        """
        source = LINT_SH.read_text(encoding="utf-8")
        assert "shfmt -i 4 -d" in source, "shfmt lost its -i 4 and now contradicts bashate"
        assert "shfmt -d" not in source, "a bare `shfmt -d` reintroduces the tab/space contradiction"

    def test_bashate_disabled(self, tmp_dir):
        """checks.bashate.enabled: false skips bashate check."""
        Path(tmp_dir, "test.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\necho hi\n", encoding="utf-8")
        _write_config(tmp_dir, {"checks": {"bashate": {"enabled": False}}})
        result = subprocess.run(
            ["bash", str(LINT_SH), tmp_dir],
            capture_output=True,
            text=True,
        )
        assert "bashate (disabled)" in result.stdout or "bashate" not in result.stdout

    def test_all_checks_disabled(self, sample_project_empty):
        """All checks disabled → exit 0."""
        _write_config(
            sample_project_empty,
            {
                "checks": {
                    "black": {"enabled": False},
                    "shfmt": {"enabled": False},
                    "bashate": {"enabled": False},
                }
            },
        )
        result = subprocess.run(
            ["bash", str(LINT_SH), sample_project_empty],
            capture_output=True,
            text=True,
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

    def test_gitleaks_runs_without_git_instead_of_skipping(self, tmp_dir):
        """No .git is no longer a skip.

        It used to print "gitleaks (.git not found, skipping)". gitleaks supports --no-git, which
        "treat[s] git repo as a regular directory and scan[s] those files", so the working copy can
        be scanned even with no history — and the plugins pass a single file, which never has a .git
        beneath it, so the old gate meant gitleaks reported skipped after every edit and in practice
        never ran at all.
        """
        result = subprocess.run(
            ["bash", str(LINT_SH), tmp_dir],
            capture_output=True,
            text=True,
        )
        assert ".git not found, skipping" not in result.stdout
        assert "gitleaks: 0 findings" in result.stdout

    def test_gitleaks_finds_a_secret_without_git(self, tmp_dir):
        """The point of the --no-git branch: a real verdict, not an absence of one.

        The token is assembled at runtime rather than written as one literal. Spelled out in full it
        matches gitleaks' own github-pat rule, so this repository's gitleaks job flagged this very
        file -- correctly, since a scanner that ignores test fixtures is a scanner with a blind spot.
        Splitting the prefix keeps the source clean while the file written to disk still carries the
        complete token, which is what the assertion needs.
        """
        token = "ghp" + "_" + "aB3dE5fG7hJ9kL1mN3pQ5rS7tU9vW1xY3zA5"
        Path(tmp_dir, "leak.md").write_text(f"token: {token}\n", encoding="utf-8")
        result = subprocess.run(
            ["bash", str(LINT_SH), tmp_dir],
            capture_output=True,
            text=True,
        )
        assert "gitleaks: 1 findings" in result.stdout

    def test_gitleaks_uses_history_when_a_repository_is_present(self, tmp_dir):
        """A repository must still be scanned as a repository, not downgraded to --no-git."""
        subprocess.run(["git", "init", "-q", tmp_dir], check=True, capture_output=True)
        result = subprocess.run(
            ["bash", str(LINT_SH), tmp_dir],
            capture_output=True,
            text=True,
        )
        assert ".git not found, skipping" not in result.stdout
        assert "gitleaks: 0 findings" in result.stdout

    def test_gitleaks_finds_a_repository_from_a_file_target(self, tmp_dir):
        """The plugin path. TARGET_DIR is a file, so "$TARGET_DIR/.git" could never exist and the
        enclosing repository has to be found by walking up from the file's directory."""
        subprocess.run(["git", "init", "-q", tmp_dir], check=True, capture_output=True)
        nested = Path(tmp_dir, "src")
        nested.mkdir()
        target = nested / "a.md"
        target.write_text("# title\n", encoding="utf-8")
        result = subprocess.run(
            ["bash", str(LINT_SH), str(target)],
            capture_output=True,
            text=True,
        )
        assert ".git not found, skipping" not in result.stdout

    def test_gitleaks_disabled(self, sample_project_empty):
        """checks.gitleaks.enabled: false skips gitleaks."""
        _write_config(sample_project_empty, {"checks": {"gitleaks": {"enabled": False}}})
        result = subprocess.run(
            ["bash", str(LINT_SH), sample_project_empty],
            capture_output=True,
            text=True,
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


def _lint_json(target, env=None, cwd=None):
    """Run lint.sh --format json against target and return the parsed document."""
    result = subprocess.run(
        ["bash", str(LINT_SH), str(target), "--format", "json"],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        cwd=cwd,
    )
    assert result.stdout, f"no JSON on stdout; stderr tail: {result.stderr[-500:]}"
    return json.loads(result.stdout)


def _lint_text(target, env=None, cwd=None):
    """Run lint.sh in text mode and return the completed process."""
    return subprocess.run(
        ["bash", str(LINT_SH), str(target)],
        capture_output=True,
        text=True,
        env={**os.environ, **(env or {})},
        cwd=cwd,
    )


def _write_config(directory, document):
    """Write a .multilint.json into directory, JSON-encoding document."""
    Path(directory, ".multilint.json").write_text(json.dumps(document), encoding="utf-8")


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

        Uses .multilint.json's checks.black.enabled rather than PATH
        surgery, so the result does not depend on which linters the host has.
        """
        _write_config(sample_project, {"checks": {"black": {"enabled": False}}})
        data = _lint_json(sample_project)
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

        data = _lint_json(proj, env={"PATH": f"{bin_dir}:{os.environ['PATH']}"})

        assert data["checks"]["markdownlint"]["status"] == "failed"
        assert data["checks"]["markdownlint"]["failed"] == 1
        assert data["return_code"] == 1

    def test_working_markdownlint_passes_the_check(self, tmp_dir):
        """The companion case, so the test above is not passing for free."""
        proj = Path(tmp_dir) / "proj"
        proj.mkdir()
        (proj / "doc.md").write_text("# Title\n\nBody.\n")
        bin_dir = self._shim(tmp_dir, "markdownlint", 0, "")

        data = _lint_json(proj, env={"PATH": f"{bin_dir}:{os.environ['PATH']}"})

        assert data["checks"]["markdownlint"]["status"] == "ok"
        assert data["checks"]["markdownlint"]["total"] == 1
        assert data["checks"]["markdownlint"]["failed"] == 0


class TestSliceOneEssentials:
    """Reduced-scope Slice 1 regression coverage.

    Per an explicit user scope reduction during apply, this class covers the
    defect 1 regression, threshold-gated verdict (pass at/below threshold,
    fail above it), one configuration-warning case, and -- added by the
    Phase 1.5 amendment -- that none of the retired MULTILINT_* environment
    variables influences a config-free run any more. The broader Slice 1
    test tasks (lookup-order fallback, per-check object resolution, option
    precedence with argv shims, conflict resolution, the full warnings
    scenario matrix, hostile values, the single-parse counter, the
    verdict-consistency matrix, and grouped-toggle independence) are
    intentionally not implemented here -- see tasks.md, where each dropped
    task is marked accordingly.
    """

    def test_defect1_file_target_reads_pwd_config(self, tmp_dir):
        """Defect 1 regression: a single-file target (the plugin's
        invocation shape) must resolve $PWD/.multilint.json, not
        "$TARGET_DIR/.multilint.json" -- TARGET_DIR is a file here, so the
        old code's "$TARGET_DIR/.multilint.json" could never exist and every
        threshold silently read as 0.
        """
        proj = Path(tmp_dir) / "proj"
        proj.mkdir()
        _write_config(proj, {"shellcheck": 3})
        (proj / "a.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\necho hi\n", encoding="utf-8")

        data = _lint_json(proj / "a.sh", cwd=proj)

        assert data["checks"]["shellcheck"]["threshold"] == 3

    def test_threshold_gates_the_run_verdict(self, tmp_dir):
        """A run passes when failures stay at or below the configured
        threshold, and fails once they exceed it -- not on the mere
        presence of a finding, which was the old rule (fail() setting
        EXIT_CODE=1 as a side effect regardless of any threshold).
        """
        # Fails flake8's E741 exactly once per file and nothing else:
        # verified empirically against this host's black/pylint/mypy/bandit,
        # all of which pass this content cleanly.
        flake8_only_failure = "l = 1\nprint(l)\n"

        at_threshold = Path(tmp_dir) / "at_threshold"
        at_threshold.mkdir()
        _write_config(at_threshold, {"flake8": 2})
        (at_threshold / "a.py").write_text(flake8_only_failure, encoding="utf-8")
        (at_threshold / "b.py").write_text(flake8_only_failure, encoding="utf-8")

        data = _lint_json(at_threshold)
        assert data["checks"]["flake8"]["failed"] == 2
        assert data["checks"]["flake8"]["threshold_exceeded"] is False
        assert data["return_code"] == 0
        assert _lint_text(at_threshold).returncode == 0

        above_threshold = Path(tmp_dir) / "above_threshold"
        above_threshold.mkdir()
        _write_config(above_threshold, {"flake8": 1})
        (above_threshold / "a.py").write_text(flake8_only_failure, encoding="utf-8")
        (above_threshold / "b.py").write_text(flake8_only_failure, encoding="utf-8")

        data = _lint_json(above_threshold)
        assert data["checks"]["flake8"]["failed"] == 2
        assert data["checks"]["flake8"]["threshold_exceeded"] is True
        assert data["return_code"] == 1
        assert _lint_text(above_threshold).returncode == 1

    def test_malformed_json_warns_instead_of_silently_defaulting(self, tmp_dir):
        """A malformed .multilint.json must produce a visible warning and
        fall back to defaults, not silently resolve every threshold to 0
        with no indication anything was wrong.
        """
        proj = Path(tmp_dir) / "proj"
        proj.mkdir()
        Path(proj, ".multilint.json").write_text("{ invalid json }", encoding="utf-8")
        (proj / "test.sh").write_text("#!/usr/bin/env bash\necho hi\n", encoding="utf-8")

        data = _lint_json(proj)
        assert data["warnings"], "malformed JSON must surface a warning, not silently default"
        assert data["checks"]["shellcheck"]["threshold"] == 0

        result = _lint_text(proj)
        assert "multilint: config warning:" in result.stderr

    def test_no_env_influence_on_config_free_project(self, tmp_dir):
        """No MULTILINT_* environment variable may influence a config-free
        run any more -- environment variables are not a multilint
        configuration surface (user decision, 2026-09-30).

        Sets every one of the thirteen retired variables to a value that
        would have changed the old (25ad4c0) behavior -- every toggle to
        "off", the four options to non-default values -- runs a project with
        no .multilint.json, and asserts the resolved checks are identical to
        a run with none of them set. security_secrets and
        security_dangerous_patterns are grep-based, so this holds regardless
        of which linters the host has installed.
        """
        proj = Path(tmp_dir) / "proj"
        proj.mkdir()
        (proj / "a.sh").write_text("#!/usr/bin/env bash\nset -euo pipefail\necho hi\n", encoding="utf-8")
        (proj / "a.py").write_text("def greet(name: str) -> str:\n    return f'Hello, {name}!'\n", encoding="utf-8")

        baseline = _lint_json(proj)

        disruptive_env = {
            "MULTILINT_BLACK_CHECK": "off",
            "MULTILINT_SHFMT_CHECK": "off",
            "MULTILINT_BASHATE_CHECK": "off",
            "MULTILINT_MYPY_CHECK": "off",
            "MULTILINT_BANDIT_CHECK": "off",
            "MULTILINT_SECURITY_CHECK": "off",
            "MULTILINT_GITLEAKS_CHECK": "off",
            "MULTILINT_TOML_CHECK": "off",
            "MULTILINT_YAML_JSON_CHECK": "off",
            "MULTILINT_BANDIT_SEVERITY": "-l",
            "MULTILINT_GITLEAKS_DEPTH": "all",
            "MULTILINT_GITLEAKS_CONFIG": str(Path(tmp_dir) / "unused.toml"),
            "MULTILINT_MYPY_CACHE_DIR": str(Path(tmp_dir) / "custom-mypy-cache"),
        }
        overridden = _lint_json(proj, env=disruptive_env)

        assert overridden["checks"] == baseline["checks"], "a retired MULTILINT_* variable still has an effect"
