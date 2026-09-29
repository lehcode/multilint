#!/usr/bin/env bash
#
# lint-changed.sh - PostToolUse hook entrypoint for the multilint plugin.
#
# Deliberately flat: it has no indented lines, because shfmt (default) wants
# tab indents while bashate E002/E003 want four spaces, and no shell file can
# satisfy both. A script with nothing indented passes both. All real logic
# lives in lint_changed.py next to this file.
#
# Exits 0 when python3 is missing rather than failing, so a machine without
# python3 gets a silent no-op instead of a "hook error" notice in the
# transcript. Neither Claude Code nor OpenCode guarantees python3 is present.

set -uo pipefail

command -v python3 >/dev/null 2>&1 || exit 0

exec python3 "$(dirname -- "$0")/lint_changed.py"
