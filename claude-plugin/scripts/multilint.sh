#!/usr/bin/env bash
#
# multilint.sh - PostToolUse hook entrypoint for the multilint plugin.
#
# Flat by history rather than by necessity: it has no indented lines because
# shfmt used to run with its default tab indent while bashate E002/E003 wanted
# four spaces, so no shell file could satisfy both and a script with nothing
# indented was the only way to pass. lint.sh now runs `shfmt -i 4`, so the two
# agree and indentation is allowed again here. Left flat because it works and
# all real logic lives in lint_changed.py next to this file.
#
# Exits 0 when python3 is missing rather than failing, so the edit is never
# blocked, but prints a notice: silence would read like a clean file. Neither
# Claude Code nor OpenCode guarantees python3 is present.

set -uo pipefail

if ! command -v python3 >/dev/null 2>&1; then
    printf '%s\n' '{"hookSpecificOutput": {"hookEventName": "PostToolUse", "additionalContext": "multilint could not lint the edited file: python3 not found on PATH. Treat the file as unverified."}, "systemMessage": "multilint: could not lint"}'
    exit 0
fi

exec python3 "$(dirname -- "$0")/lint_changed.py"
