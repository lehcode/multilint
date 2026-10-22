"""Per-user state database: change detection, cached repository roots and stored reports.

Imported by lint_changed.py, which is the hook entry point.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def state_dir() -> Path:
    """Directory holding the change-tracking and settings database.

    Fixed path, no override: XDG_STATE_HOME is honoured because it is the XDG base-directory
    standard, not a multilint-specific control. MULTILINT_STATE_DIR is retired with no replacement.
    """
    xdg = os.environ.get("XDG_STATE_HOME")
    # The XDG spec makes a relative value invalid, to be ignored. Honouring it would place the
    # database relative to whatever directory the hook runs in, and as_uri() rejects it outright.
    base = Path(xdg) if xdg and Path(xdg).is_absolute() else Path.home() / ".local" / "state"
    return base / "multilint"


def digest_of(path: Path) -> str:
    """SHA-256 of a file, read in chunks so a large file does not land in memory."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def now_stamp() -> str:
    """Event timestamp: ISO-8601, UTC, second resolution.

    Text rather than an epoch integer because these rows are read by a human diagnosing why a file
    was or was not linted, and ISO-8601 sorts correctly as a string anyway.
    """
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def open_state() -> sqlite3.Connection | None:
    """Open the state database and bring its schema up to date, or None if unusable.

    Migration rather than CREATE TABLE alone: this database already exists in the field with the
    original four columns, so the added ones arrive through ALTER TABLE. A duplicate-column error
    means another process migrated first, which is a success, not a failure.
    """
    directory = state_dir()
    try:
        directory.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(directory / "changes.db", timeout=10)
    except (OSError, sqlite3.Error):
        return None

    try:
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute(
            "CREATE TABLE IF NOT EXISTS seen (path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, digest TEXT)"
        )
        existing = {row[1] for row in connection.execute("PRAGMA table_info(seen)")}
        for column, ddl in (
            ("in_git", "in_git INTEGER"),
            ("git_root", "git_root TEXT"),
            ("first_seen_at", "first_seen_at TEXT"),
            ("last_seen_at", "last_seen_at TEXT"),
            ("last_linted_at", "last_linted_at TEXT"),
            ("last_notice", "last_notice TEXT"),
        ):
            if column not in existing:
                connection.execute(f"ALTER TABLE seen ADD COLUMN {ddl}")
        # Base folder -> repository root. Every file in a directory shares its repository, so one
        # cached answer serves all of them and a later edit costs a string comparison rather than an
        # ancestor walk. git_root is NULL when the folder is in no repository, which is a cached
        # answer too, not a cache miss.
        connection.execute(
            "CREATE TABLE IF NOT EXISTS folders ("
            "folder TEXT PRIMARY KEY, git_root TEXT, resolved_at TEXT, last_used_at TEXT)"
        )
        # Host-plugin settings (image, search_ceiling) that replaced MULTILINT_IMAGE and
        # MULTILINT_SEARCH_CEILING. Same database, same migration, so there is one file and one lock.
        connection.execute("CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT, updated_at TEXT)")
        connection.commit()
    except sqlite3.Error:
        connection.close()
        return None
    return connection


def touch_folder(connection: sqlite3.Connection, key: str, stamp: str) -> None:
    """Bump last_used_at for a cached folder; a failed write costs nothing, so it is swallowed."""
    try:
        connection.execute("UPDATE folders SET last_used_at = ? WHERE folder = ?", (stamp, key))
        connection.commit()
    except sqlite3.Error:
        pass


def store_folder(connection: sqlite3.Connection, key: str, resolved: Path | None, stamp: str) -> None:
    """Upsert the resolved root for a folder; a failed write only means the next call re-resolves."""
    try:
        connection.execute(
            "INSERT INTO folders (folder, git_root, resolved_at, last_used_at) VALUES (?, ?, ?, ?) "
            "ON CONFLICT(folder) DO UPDATE SET git_root=excluded.git_root, "
            "resolved_at=excluded.resolved_at, last_used_at=excluded.last_used_at",
            (key, str(resolved) if resolved else None, stamp, stamp),
        )
        connection.commit()
    except sqlite3.Error:
        pass


def record_and_check(
    connection: sqlite3.Connection | None,
    path: Path,
    git_root: Path | None,
) -> bool:
    """Record what this file looks like now; return True when its content differs from last time.

    Applies to repository files as well as loose ones. Under git the file is dirty by definition once
    it has been written, so the digest is the only thing that distinguishes "saved with changes" from
    "saved identical content", and re-linting the latter tells the user nothing.

    The stored report survives an identical save, so it can be repeated, and is dropped when the
    content changes, because it described bytes that are gone. record_report() writes the new one
    once the lint has run.

    Without a database every write counts as a change: linting twice is wasteful, but skipping a real
    change is a false clean, and only one of those two errors is acceptable.
    """
    try:
        stat = path.stat()
        digest = digest_of(path)
    except OSError:
        return False
    if connection is None:
        return True

    key = str(path)
    stamp = now_stamp()
    try:
        row = connection.execute("SELECT digest FROM seen WHERE path = ?", (key,)).fetchone()
        changed = row is None or row[0] != digest
        connection.execute(
            "INSERT INTO seen (path, size, mtime_ns, digest, in_git, git_root, "
            "first_seen_at, last_seen_at, last_linted_at, last_notice) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns, "
            "digest=excluded.digest, in_git=excluded.in_git, git_root=excluded.git_root, "
            "first_seen_at=COALESCE(seen.first_seen_at, excluded.first_seen_at), "
            "last_seen_at=excluded.last_seen_at, "
            "last_linted_at=CASE WHEN excluded.last_linted_at IS NULL THEN seen.last_linted_at "
            "ELSE excluded.last_linted_at END, "
            "last_notice=CASE WHEN seen.digest IS excluded.digest THEN seen.last_notice ELSE NULL END",
            (
                key,
                stat.st_size,
                stat.st_mtime_ns,
                digest,
                1 if git_root is not None else 0,
                str(git_root) if git_root else None,
                stamp,
                stamp,
                stamp if changed else None,
                None,
            ),
        )
        connection.commit()
    except sqlite3.Error:
        return True
    return changed


def forget_change(path: Path) -> None:
    """Drop the digest recorded for `path`, so saving the same bytes again is linted again.

    Called when the lint could not run: the digest was recorded before linting, and keeping it would
    turn the next identical save into a silent "unchanged" for a file that was never verified.
    """
    state = open_state()
    if state is None:
        return
    try:
        state.execute("DELETE FROM seen WHERE path = ?", (str(path),))
        state.commit()
    except sqlite3.Error:
        pass
    finally:
        state.close()


def record_report(path: Path, notices: list[str], summary: str) -> None:
    """Remember the report for the content just linted; a clean verdict clears it.

    Stored as JSON {"summary", "notices"} in seen.last_notice, so an identical save repeats exactly
    what the user was told. Best effort: without the database the next identical save is linted again.
    """
    state = open_state()
    if state is None:
        return
    value = json.dumps({"summary": summary, "notices": notices}) if notices else None
    try:
        state.execute("UPDATE seen SET last_notice = ? WHERE path = ?", (value, str(path)))
        state.commit()
    except sqlite3.Error:
        pass
    finally:
        state.close()


def stored_report(path: Path) -> tuple[list[str], str] | None:
    """The (notices, summary) recorded for `path`'s current content, or None when it was clean."""
    state = open_state()
    if state is None:
        return None
    try:
        row = state.execute("SELECT last_notice FROM seen WHERE path = ?", (str(path),)).fetchone()
    except sqlite3.Error:
        return None
    finally:
        state.close()
    try:
        report = json.loads(row[0]) if row and row[0] else None
    except (json.JSONDecodeError, TypeError):
        return None
    if not isinstance(report, dict) or not isinstance(report.get("notices"), list):
        return None
    return [n for n in report["notices"] if isinstance(n, str)], str(report.get("summary", ""))
