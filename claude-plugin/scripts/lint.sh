#!/usr/bin/env bash
# The globals declared here are used by the sourced ml_*.sh files, which shellcheck does not
# follow without -x, so it would report them as unused.
# shellcheck disable=SC2034
#
# lint.sh — lint shell, Python, and Markdown files in the given directory.
#
# Policies:
#   - Configurable thresholds per check (zero tolerance = default)
#   - Config file: .multilint.json, resolved $PWD-first (see ml_find_config)
#   - 4-space indentation required for shell scripts
#   - Bashate E006 (line length) is excluded
#
# Config format (.multilint.json), parsed once per run:
#   {
#     "bash_syntax": 0, "shellcheck": 0, "bashate": 0, "shfmt": 0,
#     "flake8": 0, "black": 0, "pylint": 0, "mypy": 0, "bandit": 0,
#     "markdownlint": 0,
#     "yaml_prettier": 0, "json_prettier": 0, "toml_sort": 0,
#     "security_secrets": 0, "security_dangerous_patterns": 0,
#     "checks": {
#       "flake8": { "enabled": true, "threshold": 2, "args": ["--max-line-length=100"] }
#     },
#     "gitleaks": { "depth": "all", "config": ".gitleaks.toml" },
#     "bandit": { "severity": "-lll" },
#     "mypy": { "cache_dir": "/tmp/.mypy_cache" }
#   }
#   Any key omitted defaults to 0/on. The flat top-level form above remains a
#   valid threshold for every check; "checks.<name>.threshold" wins over it
#   when both are present (a warning names the redundant flat key). Precedence
#   for every setting is: .multilint.json value > built-in default. There is
#   no environment-variable layer (user decision, 2026-09-30: environment
#   variables are not a multilint configuration surface). A malformed file,
#   an unknown key, or a wrongly typed value produces a warning (stderr in
#   text mode, JSON "warnings" field) and that single setting falls back
#   rather than silently reading as 0.
#
# Thresholds gate the run verdict: the process exit code and JSON
# "return_code" are 1 if and only if at least one enabled check has failures
# strictly greater than its effective threshold; a check within its threshold
# no longer fails the run by itself.
#
# JSON output (--format json), per entry under "checks":
#   failures            files that failed this check (gitleaks: findings)
#   passed              total - failures
#   total               units actually submitted to the tool; 0 when skipped
#   threshold           configured tolerance
#   threshold_exceeded  failures > threshold
#   status              "ok" | "failed" | "skipped"
#
#   status is "skipped" when the tool is not installed or the check is switched
#   off, so a consumer can tell "nothing was wrong" from "nothing was checked".
#   summary.checks_skipped lists those names, and summary.checks_run is derived
#   from the check list rather than hardcoded.
#
#   A failed check also carries (additive, absent otherwise):
#   findings            [{file, line, rule, message}] parsed from the tool's
#                       output, at most 50 (findings_truncated: true when cut);
#                       line/rule are null when the tool gives none
#   fix                 one-line hint: the exact auto-fix command for formatters,
#                       otherwise "fix the code" (+ a docs link per rule)
#   summary.rules_violated  sorted unique rule IDs across all failed checks

# Configurable thresholds per check (zero tolerance = default)
#
# Usage:
#   bash lint.sh [dir]          # defaults to current directory
#   bash lint.sh [dir] --format json  # JSON output
#   bash lint.sh [dir] --format text  # terminal output (default)

# File-wide: every "<check>_threshold" and "<check>_enabled" variable is
# assigned dynamically via `printf -v` (built-in defaults) and `eval` (the
# .multilint.json loader's filtered output), so shellcheck cannot trace the
# assignment back to the read site. A per-line disable at every one of the
# sixteen checks' guard sites would be noise; the dynamic-assignment pattern
# itself is what SC2154 exists to flag, and it is intentional here.
# shellcheck disable=SC2154

set -euo pipefail

# Directory holding this script and its sibling helpers (ml_*.sh, ml_config_schema.py, ml_report_json.py)
ML_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"

# shellcheck disable=SC2034
TARGET_DIR="${1:-.}"
EXIT_CODE=0
OUTPUT_FORMAT="text"
FILES_CHECKED=()

# Parse flags from remaining args
OUTPUT_FORMAT="text"
fmt_pending=false
for arg in "$@"; do
    case "$arg" in
        --format=*) OUTPUT_FORMAT="${arg#--format=}" ;;
        --format) fmt_pending=true ;;
        *)
            if [ "$fmt_pending" = true ]; then
                OUTPUT_FORMAT="${arg:-text}"
                fmt_pending=false
            fi
            ;;
    esac
done

# Save original stdout; redirect check output to stderr in JSON mode so JSON goes clean to stdout
exec 3>&1
if [ "$OUTPUT_FORMAT" = "json" ]; then
    exec 1>&2
fi

# Canonical check list. Single source of truth for the JSON emitter and for
# summary.checks_run, which used to be a hardcoded 11 that silently disagreed
# with the counters above. It also fixes the order of the "checks" object, which
# would otherwise follow environment-variable order and vary between runs.
ALL_CHECKS=(
    bash_syntax shellcheck bashate shfmt
    flake8 black pylint mypy bandit
    markdownlint
    yaml_prettier json_prettier toml_sort
    security_secrets security_dangerous_patterns
    gitleaks
)

# Fail counters per check (across ALL files)
declare -A check_failures
check_failures[bash_syntax]=0
check_failures[shellcheck]=0
check_failures[bashate]=0
check_failures[shfmt]=0
check_failures[flake8]=0
check_failures[black]=0
check_failures[pylint]=0
check_failures[mypy]=0
check_failures[bandit]=0
check_failures[markdownlint]=0
check_failures[yaml_prettier]=0
check_failures[json_prettier]=0
check_failures[toml_sort]=0
check_failures[security_secrets]=0
check_failures[security_dangerous_patterns]=0
check_failures[gitleaks]=0

# Per-check units actually submitted to the tool, so "passed" has a denominator.
# Without it, failures=0 is indistinguishable from "the check never ran" — the
# false-green this pair of arrays exists to close.
declare -A check_totals
# Per-check outcome: "ok" until something says otherwise, "skipped" when the
# tool is absent or the check is switched off. "failed" is derived in the
# emitter from the failure counter, so it is never stored here.
declare -A check_status
# Why a skipped check did not run ("not installed on PATH", "disabled by ..."),
# so the plugin can tell the user what to fix instead of a bare check name.
declare -A check_reason
for _ml_check in "${ALL_CHECKS[@]}"; do
    check_totals[$_ml_check]=0
    check_status[$_ml_check]="ok"
    check_reason[$_ml_check]=""
done
unset _ml_check

# shellcheck source=ml_config.sh
source "$ML_DIR/ml_config.sh"

# shellcheck source=ml_lib.sh
source "$ML_DIR/ml_lib.sh"

# shellcheck source=ml_check_shell.sh
source "$ML_DIR/ml_check_shell.sh"
# shellcheck source=ml_check_python.sh
source "$ML_DIR/ml_check_python.sh"
# shellcheck source=ml_check_docs.sh
source "$ML_DIR/ml_check_docs.sh"
# shellcheck source=ml_check_security.sh
source "$ML_DIR/ml_check_security.sh"

# ---------------------------------------------------------------------------
# Threshold-gated verdict — computed once, unconditionally, from every
# check's failures vs. its effective threshold. This is the single source
# both the JSON emitter and the text-mode summary line read from; fail()
# above no longer accumulates EXIT_CODE as a side effect of printing an
# individual finding, so there is nothing left to disagree with this.
# ---------------------------------------------------------------------------
declare -A check_threshold_exceeded
EXIT_CODE=0
for check in "${ALL_CHECKS[@]}"; do
    failures=${check_failures[$check]}
    threshold_var="${check}_threshold"
    threshold=${!threshold_var}
    if [ "$failures" -gt "$threshold" ]; then
        check_threshold_exceeded[$check]="true"
        EXIT_CODE=1
    else
        check_threshold_exceeded[$check]="false"
    fi
done

# shellcheck source=ml_report.sh
source "$ML_DIR/ml_report.sh"

exit "$EXIT_CODE"
