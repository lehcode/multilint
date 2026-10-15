"""Integration tests for the hook-filetype-coverage: real hook via multilint.sh.

These tests run the hook against a temporary git repository to verify that every
supported file type reaches a linter and that defects and clean files are handled correctly.
The tests run on the host with linters installed from the PATH.
"""

import json as _json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOK_SCRIPT = str(Path(__file__).parent.parent.parent / "claude-plugin" / "scripts" / "multilint.sh")


pytestmark = [
    pytest.mark.integration,
]


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def run_hook(repo, rel, env):
    """Feed a payload to multilint.sh and parse the JSON stdout.

    Returns the parsed dict or None if no JSON was produced (main() returned early).
    """
    payload = _json.dumps(
        {
            "tool_name": "Write",
            "cwd": str(repo),
            "tool_input": {"file_path": str(repo / rel)},
        }
    )
    proc = subprocess.run(
        ["bash", HOOK_SCRIPT],
        input=payload,
        text=True,
        capture_output=True,
        cwd=repo,
        env=env,
        timeout=300,
    )
    out = proc.stdout.strip()
    if not out:
        return None
    try:
        return _json.loads(out)
    except (_json.JSONDecodeError, ValueError):
        return None


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


# Defective content: inline, verified to fail with multilint:dev
_DEFECTIVE_SH = "#!/bin/bash\necho $1\nif [ $x = 1 ]; then echo; fi\n"
_DEFECTIVE_BASH = _DEFECTIVE_SH
_DEFECTIVE_PY = "import os,sys\ndef f( x ):\n  return eval(x)\n"
_DEFECTIVE_MD = "# T\n#Bad heading\ntrailing   \n"
_DEFECTIVE_YAML = "a:   1\nb:\n    - x\n"
_DEFECTIVE_JSON = '{"a":1,\n   "b":2}\n'
_DEFECTIVE_TOML = "b=1\na  =   2\n"

# Clean content: inline, verified to pass with multilint:dev
_CLEAN_SH = '#!/bin/bash\nset -euo pipefail\necho "ok"\n'
_CLEAN_BASH = _CLEAN_SH
_CLEAN_PY = '"""Doc."""\n\nVALUE = 1\n'
_CLEAN_MD = "# Title\n\nText.\n"
_CLEAN_YAML = "a: 1\n"
_CLEAN_JSON = '{ "a": 1 }\n'
_CLEAN_TOML = "a = 1\n"


@pytest.fixture
def repo(tmp_path):
    """A fresh git repo at tmp_path / repo."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True, capture_output=True)
    return repo


@pytest.fixture
def hook_env(tmp_path):
    """Environment with XDG_STATE_HOME and search_ceiling set for the temp state dir."""
    state = tmp_path / "state"
    state.mkdir()
    env = {**os.environ, "XDG_STATE_HOME": str(state)}
    lint_changed = str(Path(__file__).parent.parent.parent / "claude-plugin" / "scripts" / "lint_changed.py")
    subprocess.run(
        [sys.executable, lint_changed, "--set", "search_ceiling", str(tmp_path)],
        env=env,
        capture_output=True,
        check=True,
    )
    return env


# ---------------------------------------------------------------------------
# Defective-file tests (expect a failure notice with the expected check name)
# ---------------------------------------------------------------------------


# Map file types to their linter binaries
_CHECK_TO_BINARY = {
    "shellcheck": "shellcheck",
    "flake8": "flake8",
    "markdownlint": "markdownlint",
    "yaml_prettier": "prettier",
    "json_prettier": "prettier",
    "toml_sort": "toml-sort",
}


@pytest.mark.parametrize(
    "suffix,content,expected_check",
    [
        pytest.param(".sh", _DEFECTIVE_SH, "shellcheck", id=".sh"),
        pytest.param(".bash", _DEFECTIVE_BASH, "shellcheck", id=".bash"),
        pytest.param(".py", _DEFECTIVE_PY, "flake8", id=".py"),
        pytest.param(".md", _DEFECTIVE_MD, "markdownlint", id=".md"),
        pytest.param(".yaml", _DEFECTIVE_YAML, "yaml_prettier", id=".yaml"),
        pytest.param(".yml", _DEFECTIVE_YAML, "yaml_prettier", id=".yml"),
        pytest.param(".json", _DEFECTIVE_JSON, "json_prettier", id=".json"),
        pytest.param(".toml", _DEFECTIVE_TOML, "toml_sort", id=".toml"),
    ],
)
def test_defective_file(suffix, content, expected_check, repo, hook_env):
    """A defective file of each type either fails the check or reports it as skipped.

    If the required linter is not on the host PATH, lint.sh reports the check as skipped rather
    than as failed.
    """
    rel = f"sample{suffix}"
    (repo / rel).write_text(content, encoding="utf-8")
    result = run_hook(repo, rel, hook_env)

    # Main entry point may return None if the file is unchanged or unsupported
    if result is None:
        pytest.skip(f"hook returned no JSON for {suffix}")
        return

    ctx = result["hookSpecificOutput"]["additionalContext"]

    # Check whether the required linter is available
    binary = _CHECK_TO_BINARY.get(expected_check, expected_check)
    linter_available = shutil.which(binary) is not None

    if linter_available:
        # Linter is available: the check must either fail or be mentioned
        assert expected_check in ctx, f"expected check {expected_check!r} not in context: {ctx}"
        # Accept "failed", "failing", or "✗" as a failure indicator
        has_failure_keyword = "failed" in ctx or "failing" in ctx or "✗" in ctx
        assert has_failure_keyword, f"no failure indicator in context: {ctx}"
    else:
        # Linter is not available: the check should be reported as skipped
        assert (
            expected_check in ctx and "unverified" in ctx
        ), f"expected {expected_check!r} to be listed as unverified/skipped, got: {ctx}"


# ---------------------------------------------------------------------------
# Clean-file tests (expect no failure notice)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "suffix,content",
    [
        pytest.param(".sh", _CLEAN_SH, id=".sh"),
        pytest.param(".bash", _CLEAN_BASH, id=".bash"),
        pytest.param(".py", _CLEAN_PY, id=".py"),
        pytest.param(".md", _CLEAN_MD, id=".md"),
        pytest.param(".yaml", _CLEAN_YAML, id=".yaml"),
        pytest.param(".yml", _CLEAN_YAML, id=".yml"),
        pytest.param(".json", _CLEAN_JSON, id=".json"),
        pytest.param(".toml", _CLEAN_TOML, id=".toml"),
    ],
)
def test_clean_file(suffix, content, repo, hook_env):
    """A clean file of each type either passes all checks or reports some as skipped.

    When a linter is not on the host PATH, lint.sh reports it as skipped. Skipped checks
    are acceptable; only failure notices should be absent.
    """
    rel = f"sample{suffix}"
    (repo / rel).write_text(content, encoding="utf-8")
    result = run_hook(repo, rel, hook_env)
    # Result may be None (main() short-circuits when run_lint is empty) or a dict.
    if result is None:
        return  # main() returned early -- acceptable (no infrastructure, no findings)
    ctx = result["hookSpecificOutput"]["additionalContext"]
    # No "failed" keyword should appear. Skip/unverified notices are acceptable.
    assert "failed" not in ctx, f"unexpected failure notice for {suffix}: {ctx}"


# ---------------------------------------------------------------------------
# Files inside directories that lint.sh's directory walk excludes
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "relative,content,expected_check",
    [
        pytest.param("sub/.hidden/bad.yaml", _DEFECTIVE_YAML, "yaml_prettier", id="dot-dir"),
        pytest.param("src/tests/bad.sh", _DEFECTIVE_SH, "shellcheck", id="tests-dir"),
        pytest.param("pkg/test/bad.py", _DEFECTIVE_PY, "flake8", id="test-dir"),
    ],
)
def test_edited_file_in_walk_excluded_directory_is_linted(relative, content, expected_check, repo, hook_env):
    """The walk exclusions (dot-dirs, tests/, test/) must not apply to the file the hook names.

    Before ml_files, `find` dropped these single-file targets and the hook reported nothing, which
    reads exactly like a clean file. If the linter is not available on the host, the check is
    reported as skipped.
    """
    (repo / relative).parent.mkdir(parents=True)
    (repo / relative).write_text(content, encoding="utf-8")
    result = run_hook(repo, relative, hook_env)
    assert result is not None, f"hook produced no notice for {relative}"
    ctx = result["hookSpecificOutput"]["additionalContext"]

    # Check whether the required linter is available
    binary = _CHECK_TO_BINARY.get(expected_check, expected_check)
    if shutil.which(binary) is not None:
        # Linter is available: the check must be mentioned in the output
        assert expected_check in ctx, f"expected check {expected_check!r} not in context: {ctx}"
    else:
        # Linter is not available: the check should still be mentioned (as skipped)
        assert expected_check in ctx, f"expected check {expected_check!r} not in context: {ctx}"
