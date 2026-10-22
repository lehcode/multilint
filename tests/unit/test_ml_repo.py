"""Unit tests for core/ml_repo.py — git root search, cache, search_ceiling, LINTABLE_SUFFIXES."""

# pylint: disable=redefined-outer-name,protected-access,unused-argument
import os
import sqlite3
import subprocess
import sys
from pathlib import Path

import ml_repo
import ml_settings
import ml_state
import pytest

from .hook_helpers import (
    HOOK_PATH,
    _set_ceiling,
)


class TestGitRootIsBoundedAtHome:
    """The ancestor walk must stop at $HOME.

    Unbounded it reaches `/`, and because the scope root is what gets bind-mounted, an edit to a file
    sitting directly in a system directory made that directory the mount. The state database ended up
    holding a full recursive walk of /etc, which can only happen if /etc became the scope root.
    """

    def test_finds_the_repository_containing_the_file(self, hook, tmp_path, monkeypatch):
        _set_ceiling(monkeypatch, tmp_path)
        (tmp_path / "repo" / ".git").mkdir(parents=True)
        deep = tmp_path / "repo" / "src" / "pkg"
        deep.mkdir(parents=True)
        assert ml_repo.find_git_root(deep) == (tmp_path / "repo").resolve()

    def test_stops_at_the_ceiling_instead_of_climbing_to_root(self, hook, tmp_path, monkeypatch):
        """A .git above the ceiling must not be claimed, even though it exists."""
        (tmp_path / ".git").mkdir()
        ceiling = tmp_path / "home"
        inner = ceiling / "loose"
        inner.mkdir(parents=True)
        _set_ceiling(monkeypatch, ceiling)
        assert ml_repo.find_git_root(inner) is None

    def test_path_outside_the_ceiling_is_not_searched(self, hook, tmp_path, monkeypatch):
        """This is the /etc case: nothing above such a path is ours to claim."""
        outside = tmp_path / "system" / "etc"
        outside.mkdir(parents=True)
        (outside / ".git").mkdir()
        (tmp_path / "home").mkdir()
        _set_ceiling(monkeypatch, tmp_path / "home")
        assert ml_repo.find_git_root(outside) is None

    def test_ceiling_itself_may_be_the_repository(self, hook, tmp_path, monkeypatch):
        _set_ceiling(monkeypatch, tmp_path)
        (tmp_path / ".git").mkdir()
        assert ml_repo.find_git_root(tmp_path) == tmp_path.resolve()

    def test_defaults_to_home_when_unset(self, hook, monkeypatch):
        monkeypatch.setattr(ml_settings, "read_setting", lambda key: None)
        assert ml_repo.search_ceiling() == Path.home()


class TestGitRootCache:
    """A cached root is validated, never trusted.

    `git init`, `rm -rf .git`, a clone and a moved directory all change the answer. A cache that
    cannot notice is a cache that serves a wrong scope root, and the scope root is what gets mounted.
    """

    @pytest.fixture
    def state(self, hook, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
        _set_ceiling(monkeypatch, tmp_path)
        connection = ml_state.open_state()
        yield connection
        connection.close()

    def test_first_lookup_is_a_miss_and_is_recorded(self, hook, state, tmp_path):
        (tmp_path / "repo" / ".git").mkdir(parents=True)
        folder = tmp_path / "repo" / "src"
        folder.mkdir()
        root, from_cache = ml_repo.cached_git_root(state, folder)
        assert root == (tmp_path / "repo").resolve()
        assert from_cache is False

    def test_second_lookup_is_served_from_cache(self, hook, state, tmp_path):
        (tmp_path / "repo" / ".git").mkdir(parents=True)
        folder = tmp_path / "repo" / "src"
        folder.mkdir()
        ml_repo.cached_git_root(state, folder)
        root, from_cache = ml_repo.cached_git_root(state, folder)
        assert root == (tmp_path / "repo").resolve()
        assert from_cache is True

    def test_stale_entry_is_detected_when_git_disappears(self, hook, state, tmp_path):
        """rm -rf .git after caching. Returning the dead root would mount the wrong directory."""
        git_dir = tmp_path / "repo" / ".git"
        git_dir.mkdir(parents=True)
        folder = tmp_path / "repo" / "src"
        folder.mkdir()
        ml_repo.cached_git_root(state, folder)
        git_dir.rmdir()
        root, from_cache = ml_repo.cached_git_root(state, folder)
        assert root is None
        assert from_cache is False

    def test_absence_is_itself_a_cached_answer(self, hook, state, tmp_path):
        """ "No repository" is worth caching too, otherwise every edit outside a repo re-walks."""
        folder = tmp_path / "loose"
        folder.mkdir()
        assert ml_repo.cached_git_root(state, folder) == (None, False)
        assert ml_repo.cached_git_root(state, folder) == (None, True)

    def test_cached_absence_is_revisited_when_a_repository_appears(self, hook, state, tmp_path):
        """git init after a cached miss. A cached 'no' must not outlive the fact."""
        folder = tmp_path / "loose"
        folder.mkdir()
        ml_repo.cached_git_root(state, folder)
        (folder / ".git").mkdir()
        root, from_cache = ml_repo.cached_git_root(state, folder)
        assert root == folder.resolve()
        assert from_cache is False

    def test_timestamps_are_recorded(self, hook, state, tmp_path):
        folder = tmp_path / "loose"
        folder.mkdir()
        ml_repo.cached_git_root(state, folder)
        row = state.execute("SELECT resolved_at, last_used_at FROM folders").fetchone()
        assert row[0] and row[1]

    def test_works_without_a_database(self, hook, tmp_path, monkeypatch):
        """No database is a degraded mode, not a failure mode."""
        _set_ceiling(monkeypatch, tmp_path)
        (tmp_path / "repo" / ".git").mkdir(parents=True)
        root, from_cache = ml_repo.cached_git_root(None, tmp_path / "repo")
        assert root == (tmp_path / "repo").resolve()
        assert from_cache is False


class TestHookSettings:
    """--set/--get/--unset: the image and search_ceiling settings. Run as a real subprocess against a
    temporary changes.db so the argv-gated CLI branch ahead of main()'s stdin read is exercised
    for real, not just the underlying functions."""

    def _cli_env(self, tmp_path):
        return {**os.environ, "XDG_STATE_HOME": str(tmp_path)}

    def test_set_then_get_round_trips_image(self, tmp_path):
        env = self._cli_env(tmp_path)
        image = "local/multilint:dev"
        subprocess.run(
            [sys.executable, str(HOOK_PATH), "--set", "image", image],
            env=env,
            check=True,
        )
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH), "--get", "image"],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        assert result.stdout.strip() == image

    def test_set_then_get_round_trips_search_ceiling(self, tmp_path):
        env = self._cli_env(tmp_path)
        ceiling = str(tmp_path / "ceiling")
        subprocess.run(
            [sys.executable, str(HOOK_PATH), "--set", "search_ceiling", ceiling],
            env=env,
            check=True,
        )
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH), "--get", "search_ceiling"],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        assert result.stdout.strip() == ceiling

    def test_unset_removes_image(self, tmp_path):
        env = self._cli_env(tmp_path)
        subprocess.run(
            [sys.executable, str(HOOK_PATH), "--set", "image", "local/test:img"],
            env=env,
            check=True,
        )
        subprocess.run([sys.executable, str(HOOK_PATH), "--unset", "image"], env=env, check=True)
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH), "--get", "image"],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        assert result.stdout.strip() == ""

    def test_unset_removes_search_ceiling(self, tmp_path):
        env = self._cli_env(tmp_path)
        ceiling = str(tmp_path / "ceiling")
        subprocess.run(
            [sys.executable, str(HOOK_PATH), "--set", "search_ceiling", ceiling],
            env=env,
            check=True,
        )
        subprocess.run([sys.executable, str(HOOK_PATH), "--unset", "search_ceiling"], env=env, check=True)
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH), "--get", "search_ceiling"],
            capture_output=True,
            text=True,
            env=env,
            check=True,
        )
        assert result.stdout.strip() == ""

    def test_unknown_key_is_rejected(self, tmp_path):
        env = self._cli_env(tmp_path)
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH), "--set", "nonexistent", "x"],
            capture_output=True,
            text=True,
            env=env,
        )
        assert result.returncode != 0
        db_path = tmp_path / "multilint" / "changes.db"
        if db_path.exists():
            with sqlite3.connect(db_path) as connection:
                row = connection.execute("SELECT 1 FROM settings WHERE key = ?", ("nonexistent",)).fetchone()
                assert row is None

    def test_relative_xdg_state_home_is_ignored(self, hook, tmp_path, monkeypatch):
        """A relative XDG_STATE_HOME is invalid per the XDG spec; it used to crash the hook."""
        monkeypatch.setenv("XDG_STATE_HOME", "relative/state")
        monkeypatch.setenv("HOME", str(tmp_path))
        assert ml_state.state_dir() == tmp_path / ".local" / "state" / "multilint"
        assert ml_repo.search_ceiling() == Path.home()

    def test_unreadable_database_warns_instead_of_failing_silently(self, tmp_path):
        """A corrupt (or locked) db falls back to the default but says so; absence stays silent."""
        (tmp_path / "multilint").mkdir()
        (tmp_path / "multilint" / "changes.db").write_bytes(b"not a sqlite database" * 100)
        result = subprocess.run(
            [sys.executable, str(HOOK_PATH), "--get", "image"],
            capture_output=True,
            text=True,
            env=self._cli_env(tmp_path),
        )
        assert result.returncode == 0
        assert result.stdout == ""
        assert "could not read the 'image' setting" in result.stderr

    def test_resolvers_default_when_no_row_exists(self, hook, tmp_path, monkeypatch):
        """Absent db/table/row all resolve to the built-in default, not an error."""
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        assert ml_repo.search_ceiling() == Path.home()


class TestNoEnvironmentInfluence:
    """MULTILINT_IMAGE, MULTILINT_SEARCH_CEILING and MULTILINT_STATE_DIR are retired with no
    environment replacement (user decision, 2026-09-30). Regression guard that the removal is
    complete, not partial: setting all three to values that would have changed the old behavior
    must leave every resolver's result unchanged."""

    def test_retired_variables_have_no_effect(self, hook, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        monkeypatch.delenv("MULTILINT_IMAGE", raising=False)
        monkeypatch.delenv("MULTILINT_SEARCH_CEILING", raising=False)
        monkeypatch.delenv("MULTILINT_STATE_DIR", raising=False)
        baseline_ceiling = ml_repo.search_ceiling()
        baseline_state_dir = ml_state.state_dir()
        baseline_image = ml_settings.resolve_image()

        monkeypatch.setenv("MULTILINT_IMAGE", "local/other:dev")
        monkeypatch.setenv("MULTILINT_SEARCH_CEILING", str(tmp_path / "elsewhere"))
        monkeypatch.setenv("MULTILINT_STATE_DIR", str(tmp_path / "elsewhere" / "state"))

        assert ml_repo.search_ceiling() == baseline_ceiling
        assert ml_state.state_dir() == baseline_state_dir
        assert ml_settings.resolve_image() == baseline_image
