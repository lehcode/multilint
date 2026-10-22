#!/usr/bin/env python3
"""PostToolUse hook body: lint the edited file in an ephemeral container.

Reads the hook payload on stdin, decides whether the edited file changed, lints it in one ephemeral
Docker container (`docker run --rm`), and reports failing or skipped checks back to Claude.

The container runs the plugin's own lint.sh, mounted read-only at /opt/multilint, so the engine
always matches the plugin; the image supplies only the linters. The scope root is mounted read-only
at its own path (an identity mount), so the container sees every file at the path the host calls it.

Scope root is the enclosing git repository when there is one, otherwise the edited file's directory.
It is the only source root mounted, and also the container's working directory.

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
gets bind-mounted, an edit to a file in a system directory would mount that directory.

The database deliberately lives outside ~/.claude/. Writes under ~/.claude/plugins/data/ raise the
protected-directory permission prompt even under bypassPermissions
(github.com/anthropics/claude-code/issues/41156), which would prompt on every edit, and in Cowork
that path is per-conversation (issue #51398).

POSIX only. WSL counts and needs nothing extra; native Windows is not a target, so `os.getuid()` is
called directly rather than guarded.

Always exits 0, so an edit is never blocked. An unreadable payload or an unsupported file is a silent
no-op. A file that cannot be linted (no docker, a daemon or pull failure, a timeout, no readable
result) gets a notice saying so, because silence there reads exactly like a clean file.

Docker is the single dependency. The container is sandboxed: `--network none`, `--memory 2g`,
`--cpus 2`, `--user <uid>:<gid>`, and both mounts read-only.
"""

# Required, not stylistic. This module runs on the *host* interpreter, which the
# plugin does not control, and it annotates with PEP 604 unions (`Path | None`).
# Those are evaluated at function-definition time on Python 3.9, so importing
# this file raised TypeError there. Deferring annotations drops the floor to 3.7.
from __future__ import annotations

import json
import sys
from pathlib import Path

import ml_container
import ml_repo
import ml_report
import ml_results
import ml_settings
import ml_state


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

    edited = ml_repo.resolve_edited_file(payload)
    if edited is None:
        return 0

    scope_root = ml_repo.changed_scope_root(edited)
    if scope_root is None:
        report_unchanged(edited)
    else:
        lint_and_report(edited, scope_root)
    return 0


def report_unchanged(edited: Path) -> None:
    """Same bytes as last time: repeat the stored verdict when it was not clean, otherwise stay silent.

    Silence on an identical save is only correct when the user was told nothing to begin with; a file
    that still fails would otherwise look fixed the second time it is written.
    """
    report = ml_state.stored_report(edited)
    if report is not None and report[0]:
        notices, summary = report
        emit(notices, summary + " (unchanged since last lint)")


def lint_and_report(edited: Path, scope_root: Path) -> None:
    """Lint `edited` from `scope_root`, report problems, and remember the verdict for identical saves."""
    # Relative to the scope root because that is the working directory for lint.sh.
    try:
        relative = edited.resolve().relative_to(scope_root.resolve()).as_posix()
    except (ValueError, OSError):
        return

    try:
        results = ml_container.run_lint(scope_root, [relative])
    except ml_container.LintUnavailable as exc:
        ml_state.forget_change(edited)
        emit([f"multilint could not lint {relative}: {exc}. Treat the file as unverified."], "could not lint")
        return

    findings, skipped, warnings, failed = ml_report.summarize_results(results)
    warnings = ml_settings.SETTING_WARNINGS + warnings
    notices = ml_report.build_notices(
        relative,
        findings,
        skipped,
        warnings,
        failed,
        ml_results.failed_names(results),
        ml_results.violated_rules(results),
        ml_results.skip_reasons(results),
    )
    summary = ml_report.report_summary(failed, skipped)
    ml_state.record_report(edited, notices, summary)
    if notices:
        emit(notices, summary)


def emit(notices: list[str], summary: str) -> None:
    """Write the PostToolUse hook output: every notice to the user and to the model."""
    # systemMessage is what the user sees, additionalContext what the model sees. Both carry the
    # findings: a count alone tells the user something is wrong but not what.
    msg = "multilint: " + summary
    if notices:
        msg = msg + "\n\n" + "\n\n".join(notices)
    json.dump(
        {
            "hookSpecificOutput": {
                "hookEventName": "PostToolUse",
                "additionalContext": "\n\n".join(notices),
            },
            "systemMessage": msg[: ml_report.MAX_CONTEXT_CHARS],
        },
        sys.stdout,
    )


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
            ml_settings.write_setting(rest[0], rest[1])
            return 0
        if action == "--get" and len(rest) == 1:
            ml_settings.check_setting_key(rest[0])
            value = ml_settings.read_setting(rest[0])
            if value is not None:
                print(value)
            return 0
        if action == "--unset" and len(rest) == 1:
            ml_settings.unset_setting(rest[0])
            return 0
    except ValueError as error:
        print(f"lint_changed.py: {error}", file=sys.stderr)
        return 1
    print("lint_changed.py: usage: --set KEY VALUE | --get KEY | --unset KEY", file=sys.stderr)
    return 1


def run_cli(root: str, target: str) -> int:
    """`--run ROOT TARGET`: lint one target in the same ephemeral container the hook uses.

    For the multilint agent, so the sandbox flags live in build_docker_argv() and nowhere else. Prints
    lint.sh's JSON document on stdout and its human-readable findings on stderr; exits 1 with the
    reason on stderr when the container could not produce a result.
    """
    try:
        results = ml_container.run_lint(Path(root).resolve(), [target])
    except ml_container.LintUnavailable as exc:
        print(f"lint_changed.py: could not lint {target}: {exc}", file=sys.stderr)
        return 1
    for _name, document, detail in results:
        sys.stderr.write(detail)
        print(json.dumps(document))
    return 0


if __name__ == "__main__":
    if sys.argv[1:2] == ["--run"]:
        if len(sys.argv) != 4:
            print("lint_changed.py: usage: --run ROOT TARGET", file=sys.stderr)
            sys.exit(1)
        sys.exit(run_cli(sys.argv[2], sys.argv[3]))
    if sys.argv[1:] and sys.argv[1] in ("--set", "--get", "--unset"):
        sys.exit(run_settings_cli(sys.argv[1:]))
    sys.exit(main())
