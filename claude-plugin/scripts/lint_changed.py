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

Change detection has two modes:

- Under git, `git diff` plus `git ls-files --others` give the changed set directly, so unstaged,
  staged and untracked edits all count.
- Outside git, a SQLite table of (size, mtime_ns, sha256) per path records what was last seen, and
  files whose digest differs (or that are new) are the changed set.

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
from pathlib import Path

LINTABLE_SUFFIXES = {".sh", ".bash", ".py", ".md", ".yaml", ".yml", ".json", ".toml"}

# Directory names skipped when walking a non-git tree. Mirrors the prunes in lint.sh so the hook
# does not spend time hashing files the linters would never look at.
SKIP_DIRS = {"node_modules", "venv", ".venv", "__pycache__", "cache", "output", "dist"}

# Upper bound on files hashed in one non-git invocation, so a hook on a huge tree stays responsive.
MAX_SCAN_FILES = 2000

# Upper bound on files handed to one container. The changed set is whatever the repository has dirty,
# which is not bounded by the edit that triggered the hook: editing one file in a tree with 83 dirty
# files queues 52 lint runs, and a larger tree would run past RUN_TIMEOUT_SECONDS and report nothing
# at all. Truncating and saying so beats timing out silently. The edited file is always kept.
MAX_LINT_TARGETS = 25

# Published on Docker Hub, which serves anonymous pulls; ghcr.io carries the same tags but can
# require a token. Override with MULTILINT_IMAGE to test a local build.
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

# Read but no longer honoured. Kept only to tell the user why their configuration stopped taking
# effect, instead of silently ignoring it.
RETIRED_ENV_VARS = ("MULTILINT_URL", "MULTILINT_PATH_MAP")

# gitleaks scans git history, and lint.sh only runs it when "$TARGET_DIR/.git" exists. Targets here
# are individual files, so that test can never pass and the check is always reported skipped. That
# is structural rather than informative, so it is filtered out of the skipped set to avoid emitting
# the same non-finding after every single edit. Directory-scoped runs still exercise it.
STRUCTURALLY_SKIPPED = frozenset({"gitleaks"})

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


def resolve_image() -> str:
    """Image to run. MULTILINT_IMAGE overrides the published default."""
    return os.environ.get("MULTILINT_IMAGE") or DEFAULT_IMAGE


def retired_env_in_use() -> list[str]:
    """Names of retired variables the user still has set."""
    return [name for name in RETIRED_ENV_VARS if os.environ.get(name)]


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


def select_targets(all_targets: list[str], edited: str | None) -> tuple[list[str], int]:
    """Trim the changed set to MAX_LINT_TARGETS, keeping the edited file. Returns (kept, dropped).

    The changed set is whatever the repository has dirty, which the triggering edit does not bound.
    One edit in a tree with 83 dirty files queues 52 lint runs; a larger tree runs past the container
    timeout and reports nothing at all, which is the worst outcome available. Truncating and saying so
    is strictly better than a silent timeout.
    """
    if len(all_targets) <= MAX_LINT_TARGETS:
        return list(all_targets), 0
    if edited in all_targets:
        others = [path for path in all_targets if path != edited]
        kept = [edited] + others[: MAX_LINT_TARGETS - 1]
    else:
        kept = list(all_targets[:MAX_LINT_TARGETS])
    return kept, len(all_targets) - len(kept)


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
        scope_root = git_root
        changed = changed_under_git(git_root)
    else:
        scope_root = edited.parent
        changed = changed_via_sqlite(edited.parent)
    changed.add(edited)

    # Paths are made relative to the scope root because that is the working directory inside the
    # container. Anything outside the scope root is dropped: it is not mounted, so linting it would
    # report a missing file rather than a finding. Under git that is rare — the git root encloses the
    # changed set by construction — but an edit can still name a file elsewhere.
    targets: dict[str, None] = {}
    for candidate in sorted(changed):
        if candidate.suffix not in LINTABLE_SUFFIXES or not candidate.is_file():
            continue
        try:
            relative = candidate.resolve().relative_to(scope_root.resolve())
        except (ValueError, OSError):
            continue
        targets[relative.as_posix()] = None
    if not targets:
        return 0

    # Keep the edited file whatever else is dropped: it is the one just touched, and the only one the
    # user is certain to care about right now.
    edited_relative = None
    try:
        edited_relative = edited.resolve().relative_to(scope_root.resolve()).as_posix()
    except (ValueError, OSError):
        pass

    selected, truncated = select_targets(list(targets), edited_relative)

    results = run_lint(scope_root, selected)
    if not results:
        return 0

    findings: list[str] = []
    skipped: set[str] = set()
    failed = 0
    for name, document, detail in results:
        skipped.update(skipped_checks(document))
        if document.get("return_code") == 0:
            continue
        failed += 1
        lines = failing_lines(detail)
        if not lines:
            # Fall back to the structured counts when the human output carried no marked lines.
            lines = failing_checks(document)
        if lines:
            findings.append(f"{name}:\n" + "\n".join(lines))

    notices: list[str] = []
    if failed:
        context = "\n\n".join(findings)[:MAX_CONTEXT_CHARS]
        if not context:
            context = "multilint reported failing checks but produced no parsable output."
        notices.append(f"multilint found failing checks in {failed} changed file(s).\n\n{context}")
    if skipped:
        # Reported even when everything passed. A check that did not run is not a check that passed,
        # and the old contract could not tell the two apart.
        notices.append(
            "multilint could not run these checks, so the files are unverified for them: " + ", ".join(sorted(skipped))
        )
    if truncated:
        # Said out loud rather than silently dropped: the whole point of this change is that an
        # unchecked file must never look like a checked one.
        notices.append(
            f"multilint checked {len(selected)} of {len(targets)} changed files "
            f"({truncated} not checked, limit {MAX_LINT_TARGETS} per run). "
            "Commit or stash unrelated work to narrow the changed set."
        )
    for name in retired_env_in_use():
        notices.append(
            f"{name} is set but no longer used. multilint now runs a container per invocation "
            "instead of calling an HTTP API, so there is no URL or path map to configure. "
            "Set MULTILINT_IMAGE to choose a different image."
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


if __name__ == "__main__":
    sys.exit(main())
