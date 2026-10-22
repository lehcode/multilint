"""Plugin settings (image, search_ceiling) kept in the state database; lint_changed.py --set/--get/--unset.

Imported by lint_changed.py, which is the hook entry point.
"""

from __future__ import annotations

import sqlite3
import sys
from pathlib import Path

import ml_state


# SKIP_DIRS, MAX_SCAN_FILES and MAX_LINT_TARGETS used to live here. They existed to make a tree walk
# survivable: the hook collected every dirty and untracked file in the repository, pruned the
# directories it knew to be uninteresting, hashed up to 2000 of them, and capped the result at 25 per
# run. All three are gone with the walk. The hook receives one file_path per invocation, so a
# 2000-file "changed set" never described 2000 simultaneous saves — it described a dirty repository —
# and a cap on a list that can only hold one element is not a guard.

# Published on Docker Hub, which serves anonymous pulls; ghcr.io carries the same tags but can
# require a token. Override the "image" hook-setting to run a local build instead
# (`python3 lint_changed.py --set image local/build:dev`).
# Pinned to the release that ships this plugin, never :latest, so the toolchain cannot drift from
# the engine. release-please bumps the tag through the marker comment.
DEFAULT_IMAGE = "lehcode/multilint:v0.2.1"  # x-release-please-version


# The two host-plugin settings that are not linter configuration -- they shape how the plugin runs,
# not what a check does -- live in a `settings` table inside the same state database, written only
# through this CLI (`--set`/`--get`/`--unset`) and read read-only by the OpenCode plugin. Environment
# variables are not a multilint configuration surface (user decision, 2026-09-30); MULTILINT_IMAGE,
# MULTILINT_SEARCH_CEILING and MULTILINT_STATE_DIR are retired with no environment replacement.
SETTINGS_KEYS = ("image", "search_ceiling")


# Settings-read failures collected during this run, surfaced with the lint.sh config warnings.
SETTING_WARNINGS: list[str] = []


def has_control_char(value: str) -> bool:
    """True if value contains a C0 control character or DEL, mirroring the .multilint.json loader."""
    return any(ord(ch) < 0x20 or ord(ch) == 0x7F for ch in value)


def check_setting_key(key: str) -> None:
    """Raise ValueError unless key is one of SETTINGS_KEYS, so a typo never reads as success."""
    if key not in SETTINGS_KEYS:
        raise ValueError(f"unknown settings key: {key!r} (allowed: {', '.join(SETTINGS_KEYS)})")


def read_setting(key: str) -> str | None:
    """Value stored for key, or None when nothing is set or the database cannot be read.

    A missing database, table or row is the ordinary "nothing set" answer and stays silent. Any
    other failure (still locked after the timeout, corrupt file, unreadable) also falls back to the
    built-in default, but says so on stderr and in SETTING_WARNINGS.
    """
    database = ml_state.state_dir() / "changes.db"
    if not database.exists():
        return None
    try:
        # Read-only URI: a plain connect() would create an empty changes.db as a side effect of a
        # lookup.
        connection = sqlite3.connect(database.as_uri() + "?mode=ro", uri=True, timeout=10)
        try:
            row = connection.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()
        finally:
            connection.close()
    except (OSError, sqlite3.Error) as error:
        if "no such table" in str(error):
            return None
        message = f"could not read the {key!r} setting from {database}, using the default: {error}"
        SETTING_WARNINGS.append(message)
        print(f"lint_changed.py: {message}", file=sys.stderr)
        return None
    return row[0] if row is not None else None


def write_setting(key: str, value: str) -> None:
    """Validate and upsert a setting. Raises ValueError on an unknown key or an invalid value."""
    check_setting_key(key)
    if value == "" or has_control_char(value):
        raise ValueError(f"invalid value for {key!r}: must be non-empty with no control characters")
    # A relative ceiling would resolve against whatever directory the hook happens to run in.
    if key == "search_ceiling" and not Path(value).expanduser().is_absolute():
        raise ValueError(f"invalid value for {key!r}: must be an absolute path (or start with ~)")
    # The image lands in docker's argv; a leading '-' would be parsed as a docker option.
    if key == "image" and (value.startswith("-") or any(c.isspace() for c in value)):
        raise ValueError(f"invalid value for {key!r}: an image reference cannot start with '-' or contain spaces")
    connection = ml_state.open_state()
    if connection is None:
        raise ValueError(f"could not open the settings database at {ml_state.state_dir() / 'changes.db'}")
    try:
        connection.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, ml_state.now_stamp()),
        )
        connection.commit()
    finally:
        connection.close()


def unset_setting(key: str) -> None:
    """Delete key's row, if present. Not an error when there was nothing to delete."""
    check_setting_key(key)
    connection = ml_state.open_state()
    if connection is None:
        return
    try:
        connection.execute("DELETE FROM settings WHERE key = ?", (key,))
        connection.commit()
    finally:
        connection.close()


def resolve_image() -> str:
    """Image to run. The "image" hook-setting overrides the published default."""
    return read_setting("image") or DEFAULT_IMAGE
