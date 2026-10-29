"""Locate the edited file and the folder it is linted from: payload path, git root, search ceiling.

Imported by lint_changed.py, which is the hook entry point.
"""

from __future__ import annotations

import sqlite3
from pathlib import Path

import ml_settings
import ml_state

LINTABLE_SUFFIXES = {".sh", ".bash", ".py", ".md", ".yaml", ".yml", ".json", ".toml"}


def edited_file_from_payload(payload: dict) -> Path | None:
    """Pull the edited file out of the hook payload.

    tool_input.file_path is the documented field; tool_response.file_path is the fallback. Note
    snake_case: an earlier version of this hook read tool_response.filePath, which never matched.
    """
    for source in ("tool_input", "tool_response"):
        section = payload.get(source)
        if isinstance(section, dict):
            candidate = section.get("file_path")
            if isinstance(candidate, str) and candidate:
                return Path(candidate)
    return None


def search_ceiling() -> Path:
    """Highest directory the repository search may reach.

    The user's home directory, unless the "search_ceiling" hook-setting overrides it, for a
    checkout kept outside $HOME.
    """
    value = ml_settings.read_setting("search_ceiling")
    return Path(value).expanduser() if value else Path.home()


def find_git_root(start: Path) -> Path | None:
    """Nearest ancestor containing a .git entry, searching no higher than $HOME.

    The bound is the point of this function. An unbounded walk reaches `/`, and because the scope
    root is the working directory for lint.sh, an edit to a file sitting directly in a system
    directory would make that directory the scope root. That is not hypothetical: the state database
    accumulated a full recursive walk of /etc, which can only happen if /etc became the scope root.

    A path outside the ceiling has no ancestors worth searching, so None is returned without a
    single stat. Blocklisting directory names would have been the fragile version of this.
    """
    ceiling = search_ceiling().resolve()
    try:
        resolved = start.resolve()
    except OSError:
        return None
    if resolved != ceiling and ceiling not in resolved.parents:
        return None
    for directory in (resolved, *resolved.parents):
        if (directory / ".git").exists():
            return directory
        if directory == ceiling:
            break
    return None


def revalidated(
    connection: sqlite3.Connection, key: str, folder: Path, recorded: str | None, stamp: str
) -> tuple[Path | None, bool] | None:
    """The cached (root, True) when the recorded answer still holds, touching its row; None otherwise."""
    if recorded and (Path(recorded) / ".git").exists():
        ml_state.touch_folder(connection, key, stamp)
        return Path(recorded), True
    if recorded is None and find_git_root(folder) is None:
        ml_state.touch_folder(connection, key, stamp)
        return None, True
    return None


def cached_git_root(connection: sqlite3.Connection | None, folder: Path) -> tuple[Path | None, bool]:
    """Repository root for a folder, from cache when the cache is still true.

    Returns (root, from_cache). A cached root is re-stat'ed before being trusted, because the answer
    is not stable: `git init`, `rm -rf .git`, a clone or a moved directory all change it. One stat is
    cheaper than the ancestor walk it replaces and cannot serve a wrong answer. A cached "no
    repository" is re-checked the same way, since a repository can appear where there was none.
    """
    key = str(folder)
    stamp = ml_state.now_stamp()
    if connection is None:
        return find_git_root(folder), False

    row = None
    try:
        row = connection.execute("SELECT git_root FROM folders WHERE folder = ?", (key,)).fetchone()
    except sqlite3.Error:
        return find_git_root(folder), False

    if row is not None:
        cached = revalidated(connection, key, folder, row[0], stamp)
        if cached is not None:
            return cached

    resolved = find_git_root(folder)
    ml_state.store_folder(connection, key, resolved, stamp)
    return resolved, False


def resolve_edited_file(payload: dict) -> Path | None:
    """Absolute path of the lintable file the payload edited, or None when there is nothing to lint."""
    edited = edited_file_from_payload(payload)
    if edited is None or edited.suffix not in LINTABLE_SUFFIXES:
        return None

    edited = edited.expanduser()
    if not edited.is_absolute():
        edited = (Path(payload.get("cwd") or ".") / edited).resolve()

    if not edited.is_file():
        return None
    return edited


def changed_scope_root(edited: Path) -> Path | None:
    """Scope root to lint in, or None when the file's content is the same as last time."""
    # One connection for the whole invocation: the git-root cache and the digest record both use it,
    # and opening it twice would be two migrations and two locks.
    state = ml_state.open_state()
    try:
        git_root, _from_cache = cached_git_root(state, edited.parent)

        # Scope root is the repository when there is one, because lint.sh resolves .markdownlint.json
        # against the working directory and gitleaks needs .git to scan history. Outside a repository
        # it is the file's own directory, which is as narrow as the working directory can be while
        # still letting lint.sh see the file.
        scope_root = git_root if git_root is not None else edited.parent

        if not ml_state.record_and_check(state, edited, git_root):
            # Same bytes as last time. Re-linting would produce the same verdict the user has already
            # seen, so saying nothing is the correct answer rather than a missed check.
            return None
    finally:
        if state is not None:
            state.close()
    return scope_root
