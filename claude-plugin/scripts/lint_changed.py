#!/usr/bin/env python3
"""PostToolUse hook body: lint the files that actually changed, via the multilint HTTP API.

Reads the hook payload on stdin, works out which files changed, translates host paths to the
container paths multilint can see, and reports only failing checks back to Claude.

Change detection has two modes:

- Under git, `git diff` plus `git ls-files --others` give the changed set directly, so unstaged,
  staged and untracked edits all count.
- Outside git, a SQLite table of (size, mtime_ns, sha256) per path records what was last seen, and
  files whose digest differs (or that are new) are the changed set.

The database deliberately lives outside ~/.claude/. Writes under ~/.claude/plugins/data/ raise the
protected-directory permission prompt even under bypassPermissions
(github.com/anthropics/claude-code/issues/41156), which would prompt on every edit, and in Cowork
that path is per-conversation (issue #51398).

Always exits 0. A missing server, an unmapped path or an empty changeset is a silent no-op, matching
the non-blocking behaviour of the OpenCode plugin.
"""


# Required, not stylistic. This module runs on the *host* interpreter, which the
# plugin does not control, and it annotates with PEP 604 unions (`Path | None`).
# Those are evaluated at function-definition time on Python 3.9, so importing
# this file raised TypeError there. Deferring annotations drops the floor to 3.7.
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

LINTABLE_SUFFIXES = {".sh", ".bash", ".py", ".md", ".yaml", ".yml", ".json", ".toml"}

# Directory names skipped when walking a non-git tree. Mirrors the prunes in lint.sh so the hook
# does not spend time hashing files the linters would never look at.
SKIP_DIRS = {"node_modules", "venv", ".venv", "__pycache__", "cache", "output", "dist"}

# Upper bound on files hashed in one non-git invocation, so a hook on a huge tree stays responsive.
MAX_SCAN_FILES = 2000

DEFAULT_LINT_URL = "http://localhost:8591/lint"
REQUEST_TIMEOUT_SECONDS = 90

# Cap on the text handed back to Claude. The full lint output of a failing directory runs to several
# kilobytes; only the failing lines are worth the context.
MAX_CONTEXT_CHARS = 4000

GIT_CHANGE_COMMANDS = (
    ("git", "diff", "--name-only", "HEAD"),
    ("git", "diff", "--name-only", "--cached"),
    ("git", "ls-files", "--others", "--exclude-standard"),
)


def state_dir() -> Path:
    """Directory holding the change-tracking database."""
    override = os.environ.get("MULTILINT_STATE_DIR")
    if override:
        return Path(override)
    xdg = os.environ.get("XDG_STATE_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "state"
    return base / "multilint"


def path_map() -> list[tuple[Path, str]]:
    """Host-prefix to container-root pairs, longest host prefix first.

    MULTILINT_PATH_MAP overrides the default, as "host:container,host:container".
    """
    raw = os.environ.get("MULTILINT_PATH_MAP")
    if raw:
        pairs = []
        for entry in raw.split(","):
            host, _, container = entry.partition(":")
            if host and container:
                pairs.append((Path(host).expanduser(), container))
    else:
        home = Path.home()
        pairs = [
            (home / "lan-hosts", "/workspace"),
            (home / "docker-compose.d" / "multilint", "/multilint"),
        ]
    return sorted(pairs, key=lambda pair: len(str(pair[0])), reverse=True)


def to_container_path(host_path: Path) -> tuple[str, str] | None:
    """Map a host path to (container mount root, path relative to that root).

    Returns None when the path is under no mount, because the file does not exist in the container.
    """
    for host_prefix, container_root in path_map():
        try:
            relative = host_path.relative_to(host_prefix)
        except ValueError:
            continue
        return container_root, relative.as_posix()
    return None


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


def find_git_root(start: Path) -> Path | None:
    """Nearest ancestor containing a .git entry, or None."""
    for directory in (start, *start.parents):
        if (directory / ".git").exists():
            return directory
    return None


def changed_under_git(root: Path) -> set[Path]:
    """Changed, staged and untracked files known to git, as absolute paths."""
    changed: set[Path] = set()
    for command in GIT_CHANGE_COMMANDS:
        try:
            proc = subprocess.run(
                command,
                cwd=root,
                capture_output=True,
                text=True,
                check=False,
                timeout=30,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        if proc.returncode != 0:
            continue
        for line in proc.stdout.splitlines():
            if line:
                changed.add(root / line)
    return changed


def digest_of(path: Path) -> str:
    """SHA-256 of a file, read in chunks so a large file does not land in memory."""
    hasher = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            hasher.update(chunk)
    return hasher.hexdigest()


def candidates_under(directory: Path) -> list[Path]:
    """Lintable files in a tree, pruning the directories lint.sh also skips."""
    found: list[Path] = []
    for current_root, subdirs, filenames in os.walk(directory):
        subdirs[:] = [d for d in subdirs if d not in SKIP_DIRS and not d.startswith(".")]
        for filename in filenames:
            if Path(filename).suffix in LINTABLE_SUFFIXES:
                found.append(Path(current_root) / filename)
                if len(found) >= MAX_SCAN_FILES:
                    return found
    return found


def changed_via_sqlite(directory: Path) -> set[Path]:
    """Files under a non-git tree whose content differs from what was last recorded.

    The read, comparison and upsert share one transaction, and WAL is enabled, because several
    sessions can run this hook at the same time.
    """
    database = state_dir()
    try:
        database.mkdir(parents=True, exist_ok=True)
    except OSError:
        return set()

    changed: set[Path] = set()
    try:
        with sqlite3.connect(database / "changes.db", timeout=10) as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.execute(
                "CREATE TABLE IF NOT EXISTS seen ("
                "path TEXT PRIMARY KEY, size INTEGER, mtime_ns INTEGER, digest TEXT)"
            )
            rows = dict(connection.execute("SELECT path, digest FROM seen").fetchall())
            updates = []
            for candidate in candidates_under(directory):
                try:
                    stat = candidate.stat()
                    digest = digest_of(candidate)
                except OSError:
                    continue
                key = str(candidate)
                if rows.get(key) != digest:
                    changed.add(candidate)
                updates.append((key, stat.st_size, stat.st_mtime_ns, digest))
            connection.executemany(
                "INSERT INTO seen (path, size, mtime_ns, digest) VALUES (?, ?, ?, ?) "
                "ON CONFLICT(path) DO UPDATE SET size=excluded.size, "
                "mtime_ns=excluded.mtime_ns, digest=excluded.digest",
                updates,
            )
    except sqlite3.Error:
        return set()
    return changed


def lint(container_root: str, relative_path: str) -> dict | None:
    """POST one file to the multilint API. None when the service cannot be reached.

    cwd is the mount root so that lint.sh's relative config lookups resolve; markdownlint only
    picks up .markdownlint.json when it sits in the working directory.
    """
    url = os.environ.get("MULTILINT_URL", DEFAULT_LINT_URL)

    # MULTILINT_URL comes from the environment, so restrict the scheme before opening it.
    # Without this, a file:// or custom-scheme value would be honoured by urlopen.
    if urllib.parse.urlparse(url).scheme not in ("http", "https"):
        return None

    body = json.dumps({"path": relative_path, "cwd": container_root}).encode()
    request = urllib.request.Request(
        url,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        # B310 is suppressed because the scheme guard above already restricts this to http/https.
        with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT_SECONDS) as response:  # nosec B310
            return json.loads(response.read().decode())
    except (urllib.error.URLError, OSError, json.JSONDecodeError, ValueError):
        return None


def failing_lines(stdout: str) -> list[str]:
    """Only the lines that report a problem, so the context stays small."""
    return [line.rstrip() for line in stdout.splitlines() if "✗" in line or "⚠" in line]


def main() -> int:
    """Read the hook payload, lint what changed, and report failures on stdout."""
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return 0
    if not isinstance(payload, dict):
        return 0

    edited = edited_file_from_payload(payload)
    if edited is None or edited.suffix not in LINTABLE_SUFFIXES:
        return 0

    edited = edited.expanduser()
    if not edited.is_absolute():
        edited = (Path(payload.get("cwd") or ".") / edited).resolve()

    git_root = find_git_root(edited.parent)
    if git_root is not None:
        changed = changed_under_git(git_root)
    else:
        changed = changed_via_sqlite(edited.parent)
    changed.add(edited)

    targets: dict[tuple[str, str], None] = {}
    for candidate in sorted(changed):
        if candidate.suffix not in LINTABLE_SUFFIXES or not candidate.is_file():
            continue
        mapped = to_container_path(candidate)
        if mapped is not None:
            targets[mapped] = None
    if not targets:
        return 0

    findings: list[str] = []
    failed = 0
    for container_root, relative_path in targets:
        result = lint(container_root, relative_path)
        if result is None or result.get("return_code") == 0:
            continue
        failed += 1
        lines = failing_lines(result.get("stdout") or "")
        if lines:
            findings.append(f"{relative_path}:\n" + "\n".join(lines))

    if not failed:
        return 0

    context = "\n\n".join(findings)[:MAX_CONTEXT_CHARS]
    if not context:
        context = "multilint reported failing checks but produced no parsable output."
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": (
                    f"multilint found failing checks in {failed} changed file(s). "
                    f"Fix them before continuing.\n\n{context}"
                ),
            },
            "systemMessage": f"multilint: {failed} file(s) with failing checks",
        },
        sys.stdout,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
