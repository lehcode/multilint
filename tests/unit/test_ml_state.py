"""Unit tests for core/ml_state.py — schema migration, change recording, timestamp formatting."""

# pylint: disable=redefined-outer-name,protected-access,unused-argument
import sqlite3
from datetime import datetime

import ml_state
import pytest


class TestStateSchema:
    """Migration, not creation. This database already exists in the field."""

    def _legacy_db(self, tmp_path):
        """A database in the original four-column shape, with a row in it.

        Named "multilint" directly under tmp_path (rather than an arbitrary "state" name) so
        `XDG_STATE_HOME=tmp_path` reproduces state_dir()'s real, non-override path exactly --
        MULTILINT_STATE_DIR is retired with no environment replacement.
        """
        directory = tmp_path / "multilint"
        directory.mkdir()
        with sqlite3.connect(directory / "changes.db") as connection:
            connection.execute("CREATE TABLE seen (path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, digest TEXT)")
            connection.execute("INSERT INTO seen VALUES ('/old/file.py', 10, 20, 'deadbeef')")
        return directory

    def test_adds_columns_to_an_existing_database(self, hook, tmp_path, monkeypatch):
        self._legacy_db(tmp_path)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        connection = ml_state.open_state()
        assert connection is not None
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(seen)")}
        finally:
            connection.close()
        assert {"in_git", "git_root", "first_seen_at", "last_seen_at", "last_linted_at"} <= columns

    def test_migration_preserves_existing_rows(self, hook, tmp_path, monkeypatch):
        """A migration that loses history is a worse outcome than no migration."""
        self._legacy_db(tmp_path)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        connection = ml_state.open_state()
        try:
            row = connection.execute("SELECT digest FROM seen WHERE path = '/old/file.py'").fetchone()
        finally:
            connection.close()
        assert row == ("deadbeef",)

    def test_migration_is_idempotent(self, hook, tmp_path, monkeypatch):
        """Two sessions run this hook concurrently; the second must not fail on duplicate columns."""
        self._legacy_db(tmp_path)
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path))
        first = ml_state.open_state()
        first.close()
        second = ml_state.open_state()
        assert second is not None
        second.close()

    def test_creates_the_folder_cache_table(self, hook, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "fresh"))
        connection = ml_state.open_state()
        try:
            columns = {row[1] for row in connection.execute("PRAGMA table_info(folders)")}
        finally:
            connection.close()
        assert {"folder", "git_root", "resolved_at", "last_used_at"} <= columns


class TestChangeRecording:
    """Identical bytes must be a no-op; anything else must count as changed."""

    @pytest.fixture
    def state(self, hook, tmp_path, monkeypatch):
        monkeypatch.setenv("XDG_STATE_HOME", str(tmp_path / "xdg"))
        connection = ml_state.open_state()
        yield connection
        connection.close()

    def test_first_sight_of_a_file_is_a_change(self, hook, state, tmp_path):
        target = tmp_path / "a.py"
        target.write_text("x = 1\n", encoding="utf-8")
        assert ml_state.record_and_check(state, target, None) is True

    def test_identical_rewrite_is_not_a_change(self, hook, state, tmp_path):
        target = tmp_path / "a.py"
        target.write_text("x = 1\n", encoding="utf-8")
        ml_state.record_and_check(state, target, None)
        target.write_text("x = 1\n", encoding="utf-8")
        assert ml_state.record_and_check(state, target, None) is False

    def test_different_content_is_a_change(self, hook, state, tmp_path):
        target = tmp_path / "a.py"
        target.write_text("x = 1\n", encoding="utf-8")
        ml_state.record_and_check(state, target, None)
        target.write_text("x = 2\n", encoding="utf-8")
        assert ml_state.record_and_check(state, target, None) is True

    def test_in_git_is_recorded_with_the_root(self, hook, state, tmp_path):
        target = tmp_path / "a.py"
        target.write_text("x = 1\n", encoding="utf-8")
        ml_state.record_and_check(state, target, tmp_path)
        row = state.execute("SELECT in_git, git_root FROM seen WHERE path = ?", (str(target),)).fetchone()
        assert row == (1, str(tmp_path))

    def test_absence_of_a_repository_is_recorded_as_such(self, hook, state, tmp_path):
        target = tmp_path / "a.py"
        target.write_text("x = 1\n", encoding="utf-8")
        ml_state.record_and_check(state, target, None)
        row = state.execute("SELECT in_git, git_root FROM seen WHERE path = ?", (str(target),)).fetchone()
        assert row == (0, None)

    def test_first_seen_is_not_overwritten_by_later_writes(self, hook, state, tmp_path):
        target = tmp_path / "a.py"
        target.write_text("x = 1\n", encoding="utf-8")
        ml_state.record_and_check(state, target, None)
        first = state.execute("SELECT first_seen_at FROM seen").fetchone()[0]
        target.write_text("x = 2\n", encoding="utf-8")
        ml_state.record_and_check(state, target, None)
        assert state.execute("SELECT first_seen_at FROM seen").fetchone()[0] == first

    def test_without_a_database_every_write_counts_as_changed(self, hook, tmp_path):
        """Linting twice wastes time; skipping a real change reports a false clean."""
        target = tmp_path / "a.py"
        target.write_text("x = 1\n", encoding="utf-8")
        assert ml_state.record_and_check(None, target, None) is True

    def test_unreadable_file_is_not_a_change(self, hook, state, tmp_path):
        assert ml_state.record_and_check(state, tmp_path / "missing.py", None) is False


class TestTimestampFormat:
    def test_is_iso8601_utc_to_the_second(self, hook):
        stamp = ml_state.now_stamp()
        parsed = datetime.fromisoformat(stamp)
        assert parsed.tzinfo is not None
        assert parsed.microsecond == 0
