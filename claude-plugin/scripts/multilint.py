#!/usr/bin/env python3
"""PostToolUse hook body: lint the files that actually changed, in a throwaway container.

Reads the hook payload on stdin, works out which files changed, runs them through one ephemeral
`docker run`, and reports failing or skipped checks back to Claude.

There is no host-to-container path translation, because the bind mount is an identity mount:
`source=<scope root>,target=<scope root>`. The host path and the container path are the same string,
so there is nothing to map and nothing to get wrong. The previous design posted host paths to an
HTTP API listening on a fixed port with fixed mounts, which meant a project in an unmounted
directory was silently reported as clean, and N agents in N directories could not be served at all.

Scope root is the enclosing git repository when there is one, otherwise the edited file's directory.
It is the only thing mounted, read-only.

One container per hook invocation, not per file: container startup is ~0.36s over a warm in-process
call, so a ten-file changeset amortises it once (measured 6.35s) instead of ten times (8.05s).

The target is the file the hook reported, and only that file. An earlier version collected every
dirty and untracked file in the repository and linted up to 25 of them per edit, which meant one
`Write` in a dirty tree reported on files the user had not touched. The hook fires once per write
with one `file_path`; that file is the subject.

A SQLite table records (size, mtime_ns, sha256) per path, so re-saving identical bytes is a no-op
rather than a second identical verdict. It also caches base folder -> repository root, because every
file in a directory shares its repository and re-walking the ancestors on every edit is wasted work.
Cached roots are re-stat'ed before use: `git init`, `rm -rf .git` and a clone all change the answer,
and a stale cache would serve a wrong one.

The repository search stops at $HOME. Unbounded, it reaches `/`, and since the scope root is what
gets bind-mounted, an edit to a file in a system directory mounted that directory.

The database deliberately lives outside ~/.claude/. Writes under ~/.claude/plugins/data/ raise the
protected-directory permission prompt even under bypassPermissions
(github.com/anthropics/claude-code/issues/41156), which would prompt on every edit, and in Cowork
that path is per-conversation (issue #51398).

POSIX only. WSL counts and needs nothing extra; native Windows is not a target, so `os.getuid()` is
called directly rather than guarded.

Always exits 0. A missing `docker`, an unreadable payload or an empty changeset is a silent no-op,
matching the non-blocking behaviour of the OpenCode plugin.
"""

# Required, not stylistic. This module runs on the *host* interpreter, which the
# plugin does not control, and it annotates with PEP 604 unions (`Path | None`).
# Those are evaluated at function-definition time on Python 3.9, so importing
# this file raised TypeError there. Deferring annotations drops the floor to 3.7.
from __future__ import annotations

import hashlib
import json
import os
import shutil
import sqlite3
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

LINTABLE_SUFFIXES = {".sh", ".bash", ".py", ".md", ".yaml", ".yml", ".json", ".toml"}

# SKIP_DIRS, MAX_SCAN_FILES and MAX_LINT_TARGETS used to live here. They existed to make a tree walk
# survivable: the hook collected every dirty and untracked file in the repository, pruned the
# directories it knew to be uninteresting, hashed up to 2000 of them, and capped the result at 25 per
# container. All three are gone with the walk. The hook receives one file_path per invocation, so a
# 2000-file "changed set" never described 2000 simultaneous saves — it described a dirty repository —
# and a cap on a list that can only hold one element is not a guard.

# Published on Docker Hub, which serves anonymous pulls; ghcr.io carries the same tags but can
# require a token. Override the "image" hook-setting to run a local build instead
# (`python3 multilint.py --set image local/build:dev`).
DEFAULT_IMAGE = "lehcode/multilint:latest"

CONTAINER_LINT_SH = "/usr/local/bin/lint.sh"

# Wall-clock ceiling for the whole container, not per file. Generous because a first pull has to
# fetch ~506MB, and a timeout here means the edit is reported as unchecked rather than clean.
RUN_TIMEOUT_SECONDS = 300

# Resource caps mirroring docker-compose.yml, so the hook cannot starve the machine it runs on.
MEMORY_LIMIT = "2g"
CPU_LIMIT = "2"

# Separator printed to stdout before each file's JSON document, so one container run producing N
# documents can be split back apart. Chosen to be something no linter emits.
FILE_MARKER = "===MULTILINT-FILE==="

# The two host-plugin settings that are not linter configuration -- they shape how the plugin runs,
# not what a check does -- live in a `settings` table inside the same state database, written only
# through this CLI (`--set`/`--get`/`--unset`) and read read-only by the OpenCode plugin. Environment
# variables are not a multilint configuration surface (user decision, 2026-09-30); MULTILINT_IMAGE,
# MULTILINT_SEARCH_CEILING and MULTILINT_STATE_DIR are retired with no environment replacement.
SETTINGS_KEYS = ("image", "search_ceiling")

# Nothing is filtered out of the skipped set any more. gitleaks used to be listed here because
# lint.sh gated it on "$TARGET_DIR/.git" and a single-file target can never satisfy that, so it was
# reported skipped after every edit. lint.sh now selects `--no-git` when there is no repository, so
# the check produces a real verdict either way and a skip means something again.
STRUCTURALLY_SKIPPED: frozenset[str] = frozenset()

# Cap on the text handed back to Claude. The full lint output of a failing directory runs to several
# kilobytes; only the failing lines are worth the context.
MAX_CONTEXT_CHARS = 4000
# The message lint.sh gives a formatter finding; the check's "fix" line already says it, so it is not repeated.
FORMAT_ONLY_MESSAGE = "formatting required"


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
    built-in default, but says so on stderr and in SETTING_WARNINGS: silently linting with the
    default image would report on a different toolchain than the one configured.
    """
    database = state_dir() / "changes.db"
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
        print(f"multilint.py: {message}", file=sys.stderr)
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
    connection = open_state()
    if connection is None:
        raise ValueError(f"could not open the settings database at {state_dir() / 'changes.db'}")
    try:
        connection.execute(
            "INSERT INTO settings (key, value, updated_at) VALUES (?, ?, ?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at",
            (key, value, now_stamp()),
        )
        connection.commit()
    finally:
        connection.close()


def unset_setting(key: str) -> None:
    """Delete key's row, if present. Not an error when there was nothing to delete."""
    check_setting_key(key)
    connection = open_state()
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


def container_program() -> str:
    """The bash program run inside the container.

    One lint.sh per file, in one container. lint.sh takes a single target and has no multi-target
    mode, and adding one would change a script the image, the MCP server and the HTTP API all share.

    Each document is preceded by a marker line so the combined stdout can be split back into one
    result per file. In JSON mode lint.sh sends human-readable output to stderr and only JSON to
    stdout, so stdout stays parsable.

    `|| rc=1` rather than `set -e`: a failing file must not stop the remaining files from being
    checked, but the overall exit status should still reflect that something failed.
    """
    return (
        "rc=0\n"
        'for f in "$@"; do\n'
        f'    printf "%s%s\\n" "{FILE_MARKER}" "$f"\n'
        # Also on stderr, because that is where lint.sh sends the human-readable findings in JSON
        # mode. Without a marker there, the detail for N files arrives as one undivided blob and
        # cannot be attributed to the file it belongs to.
        f'    printf "%s%s\\n" "{FILE_MARKER}" "$f" >&2\n'
        f'    bash {CONTAINER_LINT_SH} "$f" --format json || rc=1\n'
        "done\n"
        "exit $rc\n"
    )


def build_docker_argv(scope_root: Path, relative_paths: list[str], image: str | None = None) -> list[str]:
    """Assemble the full `docker run` argv.

    Kept free of side effects so tests can assert on the exact arguments without Docker present.

    The mount is an identity mount — source and target are the same absolute path — which is the
    whole point of this design: the container sees the file at the path the host calls it, so no
    translation table exists to be wrong or out of date.

    `--mount` rather than `-v`: the `-v src:dst:opts` form is colon-delimited, and in zsh
    `"$PWD:$PWD:ro"` silently becomes `.../multilint:.../multilinto`, because `:r` is a parameter
    modifier that applies even inside double quotes. That produced a container mounted read-write at
    the wrong target, an empty working directory, and a lint run that reported success having
    examined nothing. --mount takes named keys and cannot be misread that way.
    """
    root = str(scope_root)
    argv = [
        "docker",
        "run",
        "--rm",
        # No linter needs the network. gitleaks reads local history; every other tool reads files.
        "--network",
        "none",
        "--memory",
        MEMORY_LIMIT,
        "--cpus",
        CPU_LIMIT,
        "--mount",
        f"type=bind,source={root},target={root},readonly",
        "--workdir",
        root,
        # lint.sh resolves .multilint.json and .markdownlint.json relative to the working directory,
        # so the scope root has to be the working directory for per-project config to apply.
        "--entrypoint",
        "bash",
        # Run as the invoking user so nothing in the container acts as root, whatever USER the image
        # declares. POSIX only, which includes WSL; native Windows is not a target.
        "--user",
        f"{os.getuid()}:{os.getgid()}",
    ]
    argv += [image or resolve_image(), "-c", container_program(), "_"]
    argv += relative_paths
    return argv


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
    value = read_setting("search_ceiling")
    return Path(value).expanduser() if value else Path.home()


def find_git_root(start: Path) -> Path | None:
    """Nearest ancestor containing a .git entry, searching no higher than $HOME.

    The bound is the point of this function. An unbounded walk reaches `/`, and because the scope
    root is what gets bind-mounted into the lint container, an edit to a file sitting directly in a
    system directory made that directory the mount. That is not hypothetical: the state database
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


def cached_git_root(connection: sqlite3.Connection | None, folder: Path) -> tuple[Path | None, bool]:
    """Repository root for a folder, from cache when the cache is still true.

    Returns (root, from_cache). A cached root is re-stat'ed before being trusted, because the answer
    is not stable: `git init`, `rm -rf .git`, a clone or a moved directory all change it. One stat is
    cheaper than the ancestor walk it replaces and cannot serve a wrong answer. A cached "no
    repository" is re-checked the same way, since a repository can appear where there was none.
    """
    key = str(folder)
    stamp = now_stamp()
    if connection is None:
        return find_git_root(folder), False

    row = None
    try:
        row = connection.execute("SELECT git_root FROM folders WHERE folder = ?", (key,)).fetchone()
    except sqlite3.Error:
        return find_git_root(folder), False

    if row is not None:
        recorded = row[0]
        if recorded and (Path(recorded) / ".git").exists():
            touch_folder(connection, key, stamp)
            return Path(recorded), True
        if recorded is None and find_git_root(folder) is None:
            touch_folder(connection, key, stamp)
            return None, True

    resolved = find_git_root(folder)
    store_folder(connection, key, resolved, stamp)
    return resolved, False


def record_and_check(
    connection: sqlite3.Connection | None,
    path: Path,
    git_root: Path | None,
) -> bool:
    """Record what this file looks like now; return True when its content differs from last time.

    Applies to repository files as well as loose ones. Under git the file is dirty by definition once
    it has been written, so the digest is the only thing that distinguishes "saved with changes" from
    "saved identical content", and re-linting the latter tells the user nothing.

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
            "first_seen_at, last_seen_at, last_linted_at) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(path) DO UPDATE SET size=excluded.size, mtime_ns=excluded.mtime_ns, "
            "digest=excluded.digest, in_git=excluded.in_git, git_root=excluded.git_root, "
            "first_seen_at=COALESCE(seen.first_seen_at, excluded.first_seen_at), "
            "last_seen_at=excluded.last_seen_at, "
            "last_linted_at=CASE WHEN excluded.last_linted_at IS NULL THEN seen.last_linted_at "
            "ELSE excluded.last_linted_at END",
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
            ),
        )
        connection.commit()
    except sqlite3.Error:
        return True
    return changed


def split_stream(text: str) -> dict[str, str]:
    """Split a marker-delimited stream into {file: body}."""
    sections: dict[str, str] = {}
    for block in text.split(FILE_MARKER):
        if not block.strip():
            continue
        name, _, body = block.partition("\n")
        sections[name.strip()] = body
    return sections


def run_lint(scope_root: Path, relative_paths: list[str]) -> list[tuple[str, dict, str]]:
    """Run one container over every changed file.

    Returns (file, JSON document, human-readable detail) per file. A document that will not parse is
    dropped rather than guessed at, on the principle that a malformed result should look like
    "nothing to report" and not like a finding.

    An empty list means nothing could be determined — no Docker, no image, a timeout — and is
    deliberately indistinguishable from "no findings" to the caller, because the hook must never
    block an edit on infrastructure trouble.
    """
    if shutil.which("docker") is None:
        return []
    try:
        proc = subprocess.run(
            build_docker_argv(scope_root, relative_paths),
            capture_output=True,
            text=True,
            check=False,
            timeout=RUN_TIMEOUT_SECONDS,
        )
    except (OSError, subprocess.SubprocessError):
        return []

    details = split_stream(proc.stderr)
    results: list[tuple[str, dict, str]] = []
    for name, body in split_stream(proc.stdout).items():
        try:
            document = json.loads(body)
        except (json.JSONDecodeError, ValueError):
            continue
        if isinstance(document, dict):
            results.append((name, document, details.get(name, "")))
    return results


def failing_checks(document: dict) -> list[str]:
    """Names of checks that found something, as "name: N failure(s)"."""
    reported = []
    for name, check in sorted((document.get("checks") or {}).items()):
        if not isinstance(check, dict):
            continue
        if check.get("status") == "failed" or (check.get("failed") or 0) > 0:
            count = check.get("failed") or 0
            reported.append(f"{name}: {count} failure(s)")
    return reported


def skipped_checks(document: dict) -> list[str]:
    """Checks that did not run, excluding the ones that structurally never can.

    A skipped check is not a pass, and before this the two were indistinguishable: a missing tool
    reported zero failures exactly like a clean run. Surfacing it matters even when return_code is
    0, which is why the caller cannot simply short-circuit on a zero exit.
    """
    names = document.get("summary", {}).get("checks_skipped")
    if not isinstance(names, list):
        return []
    return [n for n in names if isinstance(n, str) and n not in STRUCTURALLY_SKIPPED]


def failing_lines(stdout: str) -> list[str]:
    """Only the lines that report a problem, so the context stays small."""
    return [line.rstrip() for line in stdout.splitlines() if "✗" in line or "⚠" in line]


def finding_location(item: dict) -> str:
    """The "file:line" of a finding, or just "file" when the tool gave no line."""
    file = item.get("file") or ""
    return f"{file}:{item['line']}" if item.get("line") is not None and file else file


def finding_line(item: dict) -> str | None:
    """One notice line for a finding; None for a formatter's bare "formatting required"."""
    if not isinstance(item, dict) or item.get("message") == FORMAT_ONLY_MESSAGE:
        return None
    parts = [item.get("rule"), item.get("symbol"), finding_location(item)]
    return "  " + " ".join(str(part) for part in parts if part) + f"  {item.get('message') or ''}".rstrip()


def check_block(name: str, check: dict) -> str:
    """The notice block of one failed check: its fix line, then one line per finding."""
    fix = check.get("fix")
    lines = [f"{name} — {fix}" if isinstance(fix, str) and fix else name]
    lines.extend(line for line in map(finding_line, check["findings"]) if line)
    if check.get("findings_truncated"):
        lines.append("  … more findings omitted")
    return "\n".join(lines)


def finding_blocks(document: dict) -> tuple[list[str], list[str]]:
    """(failed check names, notice blocks) built from the "findings"/"fix" fields.

    Both are empty for a document from an older image, whose checks carry counts only, so the caller can
    fall back to the marker lines.
    """
    names: list[str] = []
    blocks: list[str] = []
    for name, check in (document.get("checks") or {}).items():
        if isinstance(check, dict) and check.get("status") == "failed" and "findings" in check:
            names.append(name)
            blocks.append(check_block(name, check))
    return names, blocks


def failed_names(results: list) -> list[str]:
    """Failed check names across every result, in lint.sh's check order, first appearance wins."""
    return list(dict.fromkeys(n for _file, document, _detail in results for n in finding_blocks(document)[0]))


def violated_rules(results: list) -> list[str]:
    """Sorted unique rule IDs lint.sh reported across every result."""
    rules: set[str] = set()
    for _name, document, _detail in results:
        listed = (document.get("summary") or {}).get("rules_violated")
        if isinstance(listed, list):
            rules.update(r for r in listed if isinstance(r, str))
    return sorted(rules)


def config_warnings(document: dict) -> list[str]:
    """The lint.sh "warnings" field, or [] when the field is missing or malformed.

    Independent of return_code: a malformed .multilint.json is a configuration mistake the user
    needs to hear about even on a run that otherwise passed every check.
    """
    warnings = document.get("warnings")
    if not isinstance(warnings, list):
        return []
    return [w for w in warnings if isinstance(w, str)]


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
    state = open_state()
    try:
        git_root, _from_cache = cached_git_root(state, edited.parent)

        # Scope root is the repository when there is one, because lint.sh resolves .markdownlint.json
        # against the working directory and gitleaks needs .git to scan history. Outside a repository
        # it is the file's own directory, which is as narrow as the mount can be while still letting
        # the container see the file.
        scope_root = git_root if git_root is not None else edited.parent

        if not record_and_check(state, edited, git_root):
            # Same bytes as last time. Re-linting would produce the same verdict the user has already
            # seen, so saying nothing is the correct answer rather than a missed check.
            return None
    finally:
        if state is not None:
            state.close()
    return scope_root


def summarize_results(results: list) -> tuple[list[str], set[str], list[str], int]:
    """Fold per-file lint results into (findings, skipped checks, config warnings, failed count).

    A document that carries structured findings contributes its rendered check blocks; one from an older
    image falls back to the marker lines of the human report.
    """
    findings: list[str] = []
    skipped: set[str] = set()
    warnings: list[str] = []
    failed = 0
    for name, document, detail in results:
        skipped.update(skipped_checks(document))
        warnings.extend(config_warnings(document))
        if document.get("return_code") == 0:
            continue
        failed += 1
        _names, blocks = finding_blocks(document)
        if blocks:
            findings.append("\n".join(blocks))
            continue
        lines = failing_lines(detail)
        if not lines:
            # Fall back to the structured counts when the human output carried no marked lines.
            lines = failing_checks(document)
        if lines:
            findings.append(f"{name}:\n" + "\n".join(lines))
    return findings, skipped, warnings, failed


def structured_notice(relative: str, findings: list[str], names: list[str], rules: list[str]) -> str:
    """Header, findings, Rules footer; only the findings are cut to fit MAX_CONTEXT_CHARS."""
    header = f"multilint: {relative} failed " + ", ".join(names)
    footer = ("Rules: " + " ".join(rules)) if rules else ""
    body = "\n".join(findings)
    budget = MAX_CONTEXT_CHARS - len(header) - len(footer) - 4
    if len(body) > budget:
        body = body[: max(budget - 2, 0)].rsplit("\n", 1)[0] + "\n…"
    return "\n\n".join(part for part in (header, body, footer) if part)


def build_notices(
    relative: str,
    findings: list[str],
    skipped: set[str],
    warnings: list[str],
    failed: int,
    names: list[str] | None = None,
    rules: list[str] | None = None,
) -> list[str]:
    """The user-facing notices, one per kind of problem; empty when there is nothing to report.

    With failing check names the failure notice is the structured one: a header, the findings, then a
    Rules footer. Only the findings are cut to fit MAX_CONTEXT_CHARS, never the header or the footer.
    """
    notices: list[str] = []
    if failed and names:
        notices.append(structured_notice(relative, findings, names, rules or []))
    elif failed:
        context = "\n\n".join(findings)[:MAX_CONTEXT_CHARS]
        if not context:
            context = "multilint reported failing checks but produced no parsable output."
        notices.append(f"multilint found failing checks in {relative}.\n\n{context}")
    if skipped:
        # Reported even when everything passed. A check that did not run is not a check that passed,
        # and the old contract could not tell the two apart.
        notices.append(
            f"multilint could not run these checks, so {relative} is unverified for them: " + ", ".join(sorted(skipped))
        )
    if warnings:
        # Independent of return_code: a malformed .multilint.json is worth surfacing even when the
        # run otherwise passed, the same way defect 1 (a silently-ignored threshold) went unnoticed.
        notices.append("multilint: config warning: " + "; ".join(warnings))
    return notices


def read_payload() -> dict | None:
    """The hook payload from stdin, or None when it is not a JSON object."""
    try:
        payload = json.loads(sys.stdin.read())
    except (json.JSONDecodeError, ValueError):
        return None
    return payload if isinstance(payload, dict) else None


def main() -> int:
    """Read the hook payload, lint what changed, and report failures on stdout."""
    payload = read_payload()
    if payload is None:
        return 0

    edited = resolve_edited_file(payload)
    if edited is None:
        return 0

    scope_root = changed_scope_root(edited)
    if scope_root is None:
        return 0

    # Relative to the scope root because that is the container's working directory.
    try:
        relative = edited.resolve().relative_to(scope_root.resolve()).as_posix()
    except (ValueError, OSError):
        return 0

    results = run_lint(scope_root, [relative])
    if not results:
        return 0

    findings, skipped, warnings, failed = summarize_results(results)
    warnings = SETTING_WARNINGS + warnings
    notices = build_notices(
        relative, findings, skipped, warnings, failed, failed_names(results), violated_rules(results)
    )
    if not notices:
        return 0

    summary = []
    if failed:
        summary.append(f"{failed} file(s) with failing checks")
    if skipped:
        summary.append(f"{len(skipped)} check(s) skipped")

    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "\n\n".join(notices),
            },
            "systemMessage": "multilint: " + (", ".join(summary) if summary else "configuration notice"),
        },
        sys.stdout,
    )
    return 0


def run_settings_cli(argv: list[str]) -> int:
    """Dispatch --set/--get/--unset. Returns the process exit code; never touches stdin.

    Kept separate from main() so the two entry points can never be confused: the hook is always
    invoked with an empty argv and reads its payload from stdin, while the CLI is always invoked
    with a recognized flag and never reads stdin.
    """
    if not argv:
        return 1
    action, rest = argv[0], argv[1:]
    try:
        if action == "--set" and len(rest) == 2:
            write_setting(rest[0], rest[1])
            return 0
        if action == "--get" and len(rest) == 1:
            check_setting_key(rest[0])
            value = read_setting(rest[0])
            if value is not None:
                print(value)
            return 0
        if action == "--unset" and len(rest) == 1:
            unset_setting(rest[0])
            return 0
    except ValueError as error:
        print(f"multilint.py: {error}", file=sys.stderr)
        return 1
    print("multilint.py: usage: --set KEY VALUE | --get KEY | --unset KEY", file=sys.stderr)
    return 1


if __name__ == "__main__":
    if sys.argv[1:] and sys.argv[1] in ("--set", "--get", "--unset"):
        sys.exit(run_settings_cli(sys.argv[1:]))
    sys.exit(main())
