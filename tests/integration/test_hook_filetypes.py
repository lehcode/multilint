"""Integration tests for the hook-filetype-coverage: real hook via multilint.sh.

These tests spin up a Docker container using the multilint image to verify that every
supported file type reaches a linter and that defects and clean files are handled correctly.

Test-only knob (do NOT confuse with plugin config):

    MULTILINT_TEST_IMAGE   Image tag to test against. Defaults to "lehcode/multilint:latest".
                           Only used by tests; does not affect the running system.
"""

import json as _json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HOOK_SCRIPT = str(Path(__file__).parent.parent.parent / "claude-plugin" / "scripts" / "multilint.sh")
DEFAULT_IMAGE = os.environ.get("MULTILINT_TEST_IMAGE", "lehcode/multilint:latest")


def _image_available(image):
    """True when docker is on PATH and the image is present locally (no pull is attempted)."""
    if shutil.which("docker") is None:
        return False
    probe = subprocess.run(["docker", "image", "inspect", image], capture_output=True, check=False)
    return probe.returncode == 0


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _image_available(DEFAULT_IMAGE), reason=f"docker or image {DEFAULT_IMAGE} unavailable"),
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
    subprocess.run(
        [sys.executable, lint_changed, "--set", "image", DEFAULT_IMAGE],
        env=env,
        capture_output=True,
        check=True,
    )
    return env


# ---------------------------------------------------------------------------
# Defective-file tests (expect a failure notice with the expected check name)
# ---------------------------------------------------------------------------


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
    """A defective file of each type produces a failure notice naming the expected check.

    The context text format differs between images:
    - multilint:dev uses structured notices with "failed" in the header.
    - lehcode/multilint:latest uses legacy text with "failing" / "✗" markers.
    This test is tolerant of both formats.
    """
    rel = f"sample{suffix}"
    (repo / rel).write_text(content, encoding="utf-8")
    result = run_hook(repo, rel, hook_env)
    assert result is not None, f"hook returned no JSON for {suffix}"
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert expected_check in ctx, f"expected check {expected_check!r} not in context: {ctx}"
    # The older image format says "failing" in the header; the newer one says "failed".
    # Accept either, as well as the "✗" marker which unambiguously signals failure.
    has_failure_keyword = "failed" in ctx or "failing" in ctx or "✗" in ctx
    assert has_failure_keyword, f"no failure indicator in context: {ctx}"


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
    """A clean file of each type produces no failure notice."""
    rel = f"sample{suffix}"
    (repo / rel).write_text(content, encoding="utf-8")
    result = run_hook(repo, rel, hook_env)
    # Result may be None (main() short-circuits when run_lint is empty) or a dict.
    if result is None:
        return  # main() returned early -- acceptable (no Docker, no findings)
    ctx = result["hookSpecificOutput"]["additionalContext"]
    # A gitleaks "could not run" skip notice is acceptable; only failure notices should be absent.
    assert "failed" not in ctx, f"unexpected failure notice for {suffix}: {ctx}"


# ---------------------------------------------------------------------------
# xfail cases: lint.sh find exclusions prevent findings from surfacing
# ---------------------------------------------------------------------------


@pytest.mark.xfail(
    strict=True, raises=AssertionError, reason="lint.sh find excludes '*/.*' — defective yaml in dot-dir not linted"
)
def test_excluded_hidden_yaml(repo, hook_env):
    """Defective YAML inside a hidden directory should be excluded by lint.sh find.

    Currently no failure notice is produced, so this test is expected to fail (xfail).
    """
    rel = "sub/.hidden/bad.yaml"
    (repo / rel).parent.mkdir(parents=True)
    (repo / rel).write_text(_DEFECTIVE_YAML, encoding="utf-8")
    result = run_hook(repo, rel, hook_env)
    assert result is not None
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "yaml_prettier" in ctx


@pytest.mark.xfail(
    strict=True,
    raises=AssertionError,
    reason="lint.sh find excludes '*/tests/*' — defective shell in tests/ not linted",
)
def test_excluded_tests_shell(repo, hook_env):
    """Defective shell inside src/tests/ should be excluded by lint.sh find.

    Currently no failure notice is produced, so this test is expected to fail (xfail).
    """
    rel = "src/tests/bad.sh"
    (repo / rel).parent.mkdir(parents=True)
    (repo / rel).write_text(_DEFECTIVE_SH, encoding="utf-8")
    result = run_hook(repo, rel, hook_env)
    assert result is not None
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "shellcheck" in ctx


@pytest.mark.xfail(
    strict=True, raises=AssertionError, reason="lint.sh find excludes '*/test/*' — defective py in test/ not linted"
)
def test_excluded_test_py(repo, hook_env):
    """Defective Python inside pkg/test/ should be excluded by lint.sh find.

    Currently no failure notice is produced, so this test is expected to fail (xfail).
    """
    rel = "pkg/test/bad.py"
    (repo / rel).parent.mkdir(parents=True)
    (repo / rel).write_text(_DEFECTIVE_PY, encoding="utf-8")
    result = run_hook(repo, rel, hook_env)
    assert result is not None
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "flake8" in ctx
