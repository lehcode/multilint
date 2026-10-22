"""Integration tests for the hook-filetype-coverage: real hook via multilint.sh.

These tests run the hook against a temporary git repository to verify that every
supported file type reaches a linter and that defects and clean files are handled correctly.
The tests require Docker, which starts an ephemeral container with the lint.sh mounted read-only.
"""

import json as _json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).parent.parent.parent / "claude-plugin" / "scripts"
HOOK_SCRIPT = str(SCRIPTS / "multilint.sh")


def _default_image():
    """The image tag pinned in the plugin's ml_settings.py."""
    out = subprocess.run(
        [
            sys.executable,
            "-c",
            "import sys; sys.path.insert(0, sys.argv[1]); import ml_settings as m; print(m.DEFAULT_IMAGE)",
            str(SCRIPTS),
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    return out.stdout.strip()


# Test-only knob: run against a local build instead of the pinned release image. Never pulled.
IMAGE = os.environ.get("MULTILINT_TEST_IMAGE") or _default_image()


def _image_present():
    if shutil.which("docker") is None:
        return False
    return subprocess.run(["docker", "image", "inspect", IMAGE], capture_output=True).returncode == 0


pytestmark = [
    pytest.mark.integration,
    pytest.mark.skipif(not _image_present(), reason=f"docker or the image {IMAGE} is not available locally"),
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


# Defective content: inline, each fails its check in the toolchain image
_DEFECTIVE_SH = "#!/bin/bash\necho $1\nif [ $x = 1 ]; then echo; fi\n"
_DEFECTIVE_BASH = _DEFECTIVE_SH
_DEFECTIVE_PY = "import os,sys\ndef f( x ):\n  return eval(x)\n"
_DEFECTIVE_MD = "# T\n#Bad heading\ntrailing   \n"
_DEFECTIVE_YAML = "a:   1\nb:\n    - x\n"
_DEFECTIVE_JSON = '{"a":1,\n   "b":2}\n'
_DEFECTIVE_TOML = "b=1\na  =   2\n"

# Clean content: inline, passes every check in the toolchain image
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
    """Environment with XDG_STATE_HOME, image and search_ceiling set for the temp state dir."""
    state = tmp_path / "state"
    state.mkdir()
    env = {**os.environ, "XDG_STATE_HOME": str(state)}
    lint_changed = str(Path(__file__).parent.parent.parent / "claude-plugin" / "scripts" / "lint_changed.py")
    subprocess.run(
        [sys.executable, lint_changed, "--set", "image", IMAGE],
        env=env,
        capture_output=True,
        check=True,
    )
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


def assert_flagged(result, expected_check):
    """The notice names the check as failing. The image carries every linter, so nothing may be skipped.

    A defective file never produces no notice: that is the silent skip these tests exist to catch.
    """
    assert result is not None, "hook produced no notice for a defective file"
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert expected_check in ctx, f"expected check {expected_check!r} not in context: {ctx}"
    assert "failed" in ctx or "failing" in ctx, f"no failure indicator in context: {ctx}"
    assert "could not lint" not in ctx, f"the container did not run: {ctx}"


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
    """A defective file of each type fails the check, or reports it as skipped when its linter is missing."""
    rel = f"sample{suffix}"
    (repo / rel).write_text(content, encoding="utf-8")
    assert_flagged(run_hook(repo, rel, hook_env), expected_check)


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
    """A clean file of each type produces no failure notice.

    The only notice allowed is a skipped check (gitleaks never runs on a single file); a failure or a
    "could not lint" notice means the container or a linter is broken.
    """
    rel = f"sample{suffix}"
    (repo / rel).write_text(content, encoding="utf-8")
    result = run_hook(repo, rel, hook_env)
    if result is None:
        return
    ctx = result["hookSpecificOutput"]["additionalContext"]
    assert "failed" not in ctx, f"unexpected failure notice for {suffix}: {ctx}"
    assert "could not lint" not in ctx, f"the container did not run for {suffix}: {ctx}"


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
    assert_flagged(run_hook(repo, relative, hook_env), expected_check)
