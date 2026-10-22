"""Shared helper functions for multilint hook tests.

These helpers are imported by multiple test files in tests/unit/.
Do NOT put fixtures here — they belong in conftest.py.
"""

# pylint: disable=redefined-outer-name

import io
import json
import subprocess
from pathlib import Path

import ml_container
import ml_settings
import ml_state

HOOK_PATH = Path(__file__).parent.parent.parent / "core" / "lint_changed.py"


def _git_repo(tmp_path):
    """A real repository under tmp_path, which is also the search ceiling."""
    repo = tmp_path / "repo"
    repo.mkdir()
    subprocess.run(["git", "init", "-q"], cwd=repo, check=True)
    return repo


def _set_ceiling(monkeypatch, value):
    """Isolate search_ceiling() without a real settings database.

    MULTILINT_SEARCH_CEILING is retired with no environment replacement, so tests that used to
    monkeypatch.setenv it now monkeypatch read_setting directly -- the smallest change that does not
    require a real SQLite file per test. The settings table's own round-trip is covered separately
    in TestHookSettings.
    """
    monkeypatch.setattr(
        ml_settings,
        "read_setting",
        lambda key: str(value) if key == "search_ceiling" else None,
    )


def _drive_hook(hook, monkeypatch, tmp_path, payload, run_lint_result=()):
    """Run main() on a payload with run_lint stubbed; return (exit code, run_lint calls, stdout).

    Isolates the state database and the search ceiling to tmp_path, so nothing reads or writes the
    user's real ~/.local/state/multilint.
    """
    calls = []

    def stub_run_lint(scope_root, relative_paths):
        calls.append((scope_root, list(relative_paths)))
        if isinstance(run_lint_result, Exception):
            raise run_lint_result
        return list(run_lint_result)

    monkeypatch.setattr(ml_state, "state_dir", lambda: tmp_path / "state")
    _set_ceiling(monkeypatch, tmp_path)
    monkeypatch.setattr(ml_container, "run_lint", stub_run_lint)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps(payload)))
    stdout = io.StringIO()
    monkeypatch.setattr("sys.stdout", stdout)
    return hook.main(), calls, stdout.getvalue()
