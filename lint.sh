#!/usr/bin/env bash
#
# lint.sh — lint shell, Python, and Markdown files in the given directory.
#
# Policies:
#   - Configurable thresholds per check (zero tolerance = default)
#   - Config file: .multilint.json in target directory (per-check thresholds)
#   - 4-space indentation required for shell scripts
#   - Bashate E006 (line length) is excluded
#
# Config format (.multilint.json):
#   {
#     "bash_syntax": 0, "shellcheck": 0, "bashate": 0, "shfmt": 0,
#     "flake8": 0, "black": 0, "pylint": 0, "mypy": 0, "bandit": 0,
#     "markdownlint": 0,
#     "yaml_prettier": 0, "json_prettier": 0, "toml_sort": 0,
#     "security_secrets": 0, "security_dangerous_patterns": 0
#   }
#   Any key omitted defaults to 0.
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
# Feature toggles:
#   MULTILINT_BLACK_CHECK: set to "off" to skip black formatting check
#   MULTILINT_SHFMT_CHECK: set to "off" to skip shfmt formatting check
#   MULTILINT_BASHATE_CHECK: set to "off" to skip bashate indentation check
#   MULTILINT_MYPY_CHECK: set to "off" to skip mypy static type check
#   MULTILINT_BANDIT_CHECK: set to "off" to skip bandit security check
#   MULTILINT_MYPY_CACHE_DIR: mypy cache location (default: /tmp/.mypy_cache)
#   MULTILINT_BANDIT_SEVERITY: bandit severity flag (default: -ll, medium and high)
#   MULTILINT_SECURITY_CHECK: set to "off" to skip security scanning
#   MULTILINT_GITLEAKS_CHECK: set to "off" to skip gitleaks scanning
#   MULTILINT_TOML_CHECK: set to "off" to skip TOML linting
#   MULTILINT_YAML_JSON_CHECK: set to "off" to skip YAML/JSON linting
#   MULTILINT_GITLEAKS_DEPTH: set to "1" for last commit, "all" for full history (default: 1)

# Configurable thresholds per check (zero tolerance = default)
#
# Usage:
#   bash lint.sh [dir]          # defaults to current directory
#   bash lint.sh [dir] --format json  # JSON output
#   bash lint.sh [dir] --format text  # terminal output (default)

set -euo pipefail

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

# ---------------------------------------------------------------------------
# Load thresholds from .multilint.json
# ---------------------------------------------------------------------------
get_threshold() {
    local config="$TARGET_DIR/.multilint.json"
    if [ -f "$config" ]; then
        set +e
        python3 -c "
import json, sys
try:
    d = json.load(open(sys.argv[1]))
    print(d.get(sys.argv[2], 0))
except Exception:
    print(0)
" "$config" "$1"
        set -e
    else
        echo 0
    fi
}

# shellcheck disable=SC2034
bash_syntax_threshold=$(get_threshold bash_syntax)
# shellcheck disable=SC2034
shellcheck_threshold=$(get_threshold shellcheck)
# shellcheck disable=SC2034
bashate_threshold=$(get_threshold bashate)
# shellcheck disable=SC2034
shfmt_threshold=$(get_threshold shfmt)
# shellcheck disable=SC2034
flake8_threshold=$(get_threshold flake8)
# shellcheck disable=SC2034
black_threshold=$(get_threshold black)
# shellcheck disable=SC2034
pylint_threshold=$(get_threshold pylint)
# shellcheck disable=SC2034
mypy_threshold=$(get_threshold mypy)
# shellcheck disable=SC2034
bandit_threshold=$(get_threshold bandit)
# shellcheck disable=SC2034
markdownlint_threshold=$(get_threshold markdownlint)

# New check thresholds
# shellcheck disable=SC2034
yaml_prettier_threshold=$(get_threshold yaml_prettier)
# shellcheck disable=SC2034
json_prettier_threshold=$(get_threshold json_prettier)
# shellcheck disable=SC2034
toml_sort_threshold=$(get_threshold toml_sort)
# shellcheck disable=SC2034
security_secrets_threshold=$(get_threshold security_secrets)
# shellcheck disable=SC2034
security_dangerous_patterns_threshold=$(get_threshold security_dangerous_patterns)

# Gitleaks threshold
# shellcheck disable=SC2034
gitleaks_threshold=$(get_threshold gitleaks)

# Feature toggles (default: enabled)
# MULTILINT_BLACK_CHECK: set to "off" to skip black formatting check
# MULTILINT_SHFMT_CHECK: set to "off" to skip shfmt formatting check
# MULTILINT_BASHATE_CHECK: set to "off" to skip bashate indentation check
# MULTILINT_MYPY_CHECK: set to "off" to skip mypy static type check
# MULTILINT_BANDIT_CHECK: set to "off" to skip bandit security check
BLACK_ENABLED="${MULTILINT_BLACK_CHECK:-on}"
SHFMT_ENABLED="${MULTILINT_SHFMT_CHECK:-on}"
BASHATE_ENABLED="${MULTILINT_BASHATE_CHECK:-on}"
MYPY_ENABLED="${MULTILINT_MYPY_CHECK:-on}"
BANDIT_ENABLED="${MULTILINT_BANDIT_CHECK:-on}"

# mypy writes an incremental cache beside the sources it checks. The workspace is
# mounted read-only, where that makes mypy abort with "INTERNAL ERROR", so the
# cache is redirected to a writable path. Overridable for non-container use.
MYPY_CACHE_DIR="${MULTILINT_MYPY_CACHE_DIR:-/tmp/.mypy_cache}"

# bandit reports low-severity findings (assert usage, subprocess imports) that are
# noise in this codebase; -ll limits output to medium and high severity.
BANDIT_SEVERITY="${MULTILINT_BANDIT_SEVERITY:--ll}"

# bandit's default report appends a metrics block that says nothing actionable. The
# custom format collapses each finding to a single grep-friendly line.
BANDIT_TEMPLATE="{relpath}:{line}: [{test_id}] {severity}: {msg}"
# shellcheck disable=SC2034
SECURITY_ENABLED="${MULTILINT_SECURITY_CHECK:-on}"
# shellcheck disable=SC2034
GITLEAKS_ENABLED="${MULTILINT_GITLEAKS_CHECK:-on}"
# shellcheck disable=SC2034
TOML_ENABLED="${MULTILINT_TOML_CHECK:-on}"
# shellcheck disable=SC2034
YAML_JSON_ENABLED="${MULTILINT_YAML_JSON_CHECK:-on}"

# Gitleaks depth control: "1" = last commit, "all" = full history
# shellcheck disable=SC2034
GITLEAKS_DEPTH="${MULTILINT_GITLEAKS_DEPTH:-1}"

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

# Per-check units actually submitted to the tool, so "passed" has a denominator.
# Without it, failures=0 is indistinguishable from "the check never ran" — the
# false-green this pair of arrays exists to close.
declare -A check_totals
# Per-check outcome: "ok" until something says otherwise, "skipped" when the
# tool is absent or the check is switched off. "failed" is derived in the
# emitter from the failure counter, so it is never stored here.
declare -A check_status
for _ml_check in "${ALL_CHECKS[@]}"; do
    check_totals[$_ml_check]=0
    check_status[$_ml_check]="ok"
done
unset _ml_check

info()  { echo -e "\n\033[1m→ $*\033[0m"; }
pass()  { echo "  ✓ $*"; }
fail()  { echo "  ✗ $*"; EXIT_CODE=1; }
warn()  { echo "  ~ $*"; }

# Record that $1 actually ran against one more unit of work.
ran()  { check_totals[$1]=$(( check_totals[$1] + 1 )); }
# Record that $1 did not run at all. Idempotent: the guards live inside the
# per-file loops, so this fires once per file when a tool is missing.
skipped() { check_status[$1]="skipped"; }

# ---------------------------------------------------------------------------
# Shell scripts
# ---------------------------------------------------------------------------
info "Linting shell scripts..."

mapfile -t shell_files < <(find "$TARGET_DIR" -type f \( -name '*.sh' -o -name '*.bash' \) ! -path '*/\.*' ! -path '*/tests/*' ! -path '*/test/*' ! -path '*/venv/*' ! -path '*/node_modules/*' 2>/dev/null)

if [ ${#shell_files[@]} -eq 0 ]; then
    echo "  (none found)"
else
    for f in "${shell_files[@]}"; do
        FILES_CHECKED+=("$f")
        echo ""
        echo "  📄 $f"

        # Bash syntax check — bash itself is always present, so no guard.
        ran bash_syntax
        if bash -n "$f" >/dev/null 2>&1; then
            pass "bash syntax"
        else
            echo "    $(bash -n "$f" 2>&1)"
            check_failures[bash_syntax]=$(( check_failures[bash_syntax] + 1 ))
            fail "bash syntax"
        fi

        # ShellCheck — excludes SC1091/SC2155/SC2086, disables style
        if command -v shellcheck >/dev/null 2>&1; then
            ran shellcheck
            set +e
            sc_output="$(shellcheck -e SC1091 -e SC2155 -e SC2086 -S style "$f" 2>&1)"
            sc_code=$?
            set -e
            if [ "$sc_code" -eq 0 ]; then
                pass "shellcheck"
            else
                echo "    $sc_output"
                check_failures[shellcheck]=$(( check_failures[shellcheck] + 1 ))
                fail "shellcheck"
            fi
        else
            skipped shellcheck
            warn "shellcheck (not installed, skipping)"
        fi

        # Bashate — 4-space indentation check, excludes E006 (line length)
        if [ "$BASHATE_ENABLED" = "off" ]; then
            skipped bashate
            warn "bashate (disabled)"
        elif command -v bashate >/dev/null 2>&1; then
            ran bashate
            set +e
            bashate_output="$(bashate -i E006 "$f" 2>&1)"
            bashate_rc=$?
            set -e
            if [ "$bashate_rc" -eq 0 ]; then
                pass "bashate"
            else
                echo "    $bashate_output"
                check_failures[bashate]=$(( check_failures[bashate] + 1 ))
                fail "bashate (indentation required)"
            fi
        else
            skipped bashate
            warn "bashate (not installed, skipping)"
        fi

        # shfmt (format check only)
        if [ "$SHFMT_ENABLED" = "off" ]; then
            skipped shfmt
            warn "shfmt (disabled)"
        elif command -v shfmt >/dev/null 2>&1; then
            ran shfmt
            if shfmt -d "$f" | grep -q .; then
                check_failures[shfmt]=$(( check_failures[shfmt] + 1 ))
                fail "shfmt (formatting required)"
            else
                pass "shfmt"
            fi
        else
            skipped shfmt
            warn "shfmt (not installed, skipping)"
        fi
    done
fi

echo ""
# Threshold summary for shell checks
for check in bash_syntax shellcheck bashate shfmt; do
    failures=${check_failures[$check]}
    threshold_var="${check}_threshold"
    threshold=${!threshold_var}
    if [ "$failures" -gt 0 ]; then
        if [ "$failures" -gt "$threshold" ]; then
            echo "  ⚠ $check: $failures failures (threshold: $threshold)"
        else
            echo "  ✓ $check: $failures failures (threshold: $threshold)"
        fi
    else
        echo "  ✓ $check: 0 failures (threshold: $threshold)"
    fi
done

# ---------------------------------------------------------------------------
# Python scripts
# ---------------------------------------------------------------------------
info "Linting Python scripts..."

mapfile -t py_files < <(find "$TARGET_DIR" -type f -name '*.py' ! -path '*/\.*' ! -path '*/test/*' ! -path '*/__pycache__/*' ! -path '*/venv/*' ! -path '*/node_modules/*' ! -path '*/fixtures/*' 2>/dev/null)

if [ ${#py_files[@]} -eq 0 ]; then
    echo "  (none found)"
else
    for f in "${py_files[@]}"; do
        FILES_CHECKED+=("$f")
        echo ""
        echo "  🐍 $f"

        # flake8 (style)
        if command -v flake8 >/dev/null 2>&1; then
            ran flake8
            set +e
            pb_flake8="$(flake8 --max-line-length=120 --extend-ignore=E203,E111,E121,E124,BLK100 "$f" 2>&1)"
            pb_flake8_rc=$?
            set -e
            if [ "$pb_flake8_rc" -eq 0 ]; then
                pass "flake8"
            else
                echo "    $pb_flake8"
                check_failures[flake8]=$(( check_failures[flake8] + 1 ))
                fail "flake8"
            fi
        else
            skipped flake8
            warn "flake8 (not installed, skipping)"
        fi

        # black (formatting)
        if [ "$BLACK_ENABLED" = "off" ]; then
            skipped black
            warn "black (disabled)"
        elif command -v black >/dev/null 2>&1; then
            ran black
            set +e
            pb_black="$(black --check --line-length=120 "$f" 2>&1)"
            pb_black_rc=$?
            set -e
            if [ "$pb_black_rc" -eq 0 ]; then
                pass "black"
            else
                echo "    $pb_black"
                check_failures[black]=$(( check_failures[black] + 1 ))
                fail "black"
            fi
        else
            skipped black
            warn "black (not installed, skipping)"
        fi

        # pylint (errors/warnings)
        if command -v pylint >/dev/null 2>&1; then
            ran pylint
            set +e
            pb_pylint="$(pylint --disable=C,R,E0401,E1123,W1510 --output-format=text "$f" 2>&1)"
            pb_pylint_rc=$?
            set -e
            if [ "$pb_pylint_rc" -eq 0 ]; then
                pass "pylint"
            else
                echo "    $pb_pylint"
                check_failures[pylint]=$(( check_failures[pylint] + 1 ))
                fail "pylint"
            fi
        else
            skipped pylint
            warn "pylint (not installed, skipping)"
        fi

        # mypy (static types)
        #
        # --ignore-missing-imports and --follow-imports=silent are required, not
        # cosmetic: the image installs no project dependencies, so without them
        # every third-party import reports import-not-found and drowns out real
        # findings. This mirrors pylint running with E0401 disabled.
        if [ "$MYPY_ENABLED" = "off" ]; then
            skipped mypy
            warn "mypy (disabled)"
        elif command -v mypy >/dev/null 2>&1; then
            ran mypy
            set +e
            pb_mypy="$(mypy --cache-dir="$MYPY_CACHE_DIR" --ignore-missing-imports \
                --follow-imports=silent --no-error-summary "$f" 2>&1)"
            pb_mypy_rc=$?
            set -e
            if [ "$pb_mypy_rc" -eq 0 ]; then
                pass "mypy"
            else
                echo "    $pb_mypy"
                check_failures[mypy]=$(( check_failures[mypy] + 1 ))
                fail "mypy"
            fi
        else
            skipped mypy
            warn "mypy (not installed, skipping)"
        fi

        # bandit (security)
        if [ "$BANDIT_ENABLED" = "off" ]; then
            skipped bandit
            warn "bandit (disabled)"
        elif command -v bandit >/dev/null 2>&1; then
            ran bandit
            set +e
            pb_bandit="$(bandit -q "$BANDIT_SEVERITY" -f custom \
                --msg-template "$BANDIT_TEMPLATE" "$f" 2>&1)"
            pb_bandit_rc=$?
            set -e
            if [ "$pb_bandit_rc" -eq 0 ]; then
                pass "bandit"
            else
                echo "    $pb_bandit"
                check_failures[bandit]=$(( check_failures[bandit] + 1 ))
                fail "bandit"
            fi
        else
            skipped bandit
            warn "bandit (not installed, skipping)"
        fi
    done
fi

echo ""
# Threshold summary for Python checks
for check in flake8 black pylint mypy bandit; do
    failures=${check_failures[$check]}
    threshold_var="${check}_threshold"
    threshold=${!threshold_var}
    if [ "$failures" -gt 0 ]; then
        if [ "$failures" -gt "$threshold" ]; then
            echo "  ⚠ $check: $failures failures (threshold: $threshold)"
        else
            echo "  ✓ $check: $failures failures (threshold: $threshold)"
        fi
    else
        echo "  ✓ $check: 0 failures (threshold: $threshold)"
    fi
done

# ---------------------------------------------------------------------------
# Markdown files
# ---------------------------------------------------------------------------
info "Linting Markdown files..."

mapfile -t md_files < <(find "$TARGET_DIR" -type f -name '*.md' ! -path '*/\.*' ! -path '*/node_modules/*' ! -path '*/cache/*' ! -path '*/output/*' 2>/dev/null)

if [ ${#md_files[@]} -eq 0 ]; then
    echo "  (none found)"
else
    for f in "${md_files[@]}"; do
        FILES_CHECKED+=("$f")
        echo ""
        echo "  📝 $f"

        # markdownlint
        if command -v markdownlint >/dev/null 2>&1; then
            ran markdownlint
            MD_CONFIG=""
            if [ -f ".markdownlint.json" ]; then
                MD_CONFIG="-c .markdownlint.json"
            fi
            # The former `|| true` on the next line was both redundant and
            # actively harmful: `set +e` already stops a non-zero exit from
            # aborting the script, while `|| true` made the command list itself
            # succeed, so md_rc read `true`'s 0 and never markdownlint's status.
            # Every Markdown file therefore reported ✓ — including while the
            # binary was crashing outright with "Cannot find package 'commander'".
            set +e
            md_output="$(markdownlint $MD_CONFIG "$f" 2>&1)"
            md_rc=$?
            set -e
            if [ "$md_rc" -eq 0 ]; then
                pass "markdownlint"
            else
                echo "    $md_output"
                check_failures[markdownlint]=$(( check_failures[markdownlint] + 1 ))
                fail "markdownlint"
            fi
        else
            skipped markdownlint
            warn "markdownlint (not installed, skipping)"
        fi
    done
fi

echo ""
# Threshold summary for Markdown checks
# shellcheck disable=SC2043
for check in markdownlint; do
    failures=${check_failures[$check]}
    threshold_var="${check}_threshold"
    threshold=${!threshold_var}
    if [ "$failures" -gt 0 ]; then
        if [ "$failures" -gt "$threshold" ]; then
            echo "  ⚠ $check: $failures failures (threshold: $threshold)"
        else
            echo "  ✓ $check: $failures failures (threshold: $threshold)"
        fi
    else
        echo "  ✓ $check: 0 failures (threshold: $threshold)"
    fi
done

# ---------------------------------------------------------------------------
# YAML and JSON files
# ---------------------------------------------------------------------------
info "Linting YAML and JSON files..."

mapfile -t yaml_files < <(find "$TARGET_DIR" -type f \( -name '*.yaml' -o -name '*.yml' \) ! -path '*/\.*' ! -path '*/venv/*' ! -path '*/node_modules/*' ! -path '*/__pycache__/*' 2>/dev/null)
mapfile -t json_files < <(find "$TARGET_DIR" -type f -name '*.json' ! -path '*/\.*' ! -path '*/node_modules/*' ! -path '*/venv/*' ! -path '*/__pycache__/*' 2>/dev/null)

if [ ${#yaml_files[@]} -eq 0 ] && [ ${#json_files[@]} -eq 0 ]; then
    echo "  (none found)"
else
    if [ "$YAML_JSON_ENABLED" = "off" ]; then
        skipped yaml_prettier
        skipped json_prettier
        warn "YAML/JSON checks (disabled)"
    elif command -v prettier >/dev/null 2>&1; then
        for f in "${yaml_files[@]}" "${json_files[@]}"; do
            FILES_CHECKED+=("$f")
            echo ""
            echo "  📋 $f"

            if [[ "$f" == *.yaml || "$f" == *.yml ]]; then
                ran yaml_prettier
                set +e
                prettier_output="$(prettier --check --log-level error "$f" 2>&1)"
                prettier_rc=$?
                set -e
                if [ "$prettier_rc" -eq 0 ]; then
                    pass "yaml prettier"
                else
                    echo "    $prettier_output"
                    check_failures[yaml_prettier]=$(( check_failures[yaml_prettier] + 1 ))
                    fail "yaml prettier"
                fi
            elif [[ "$f" == *.json ]]; then
                ran json_prettier
                set +e
                prettier_output="$(prettier --check --log-level error "$f" 2>&1)"
                prettier_rc=$?
                set -e
                if [ "$prettier_rc" -eq 0 ]; then
                    pass "json prettier"
                else
                    echo "    $prettier_output"
                    check_failures[json_prettier]=$(( check_failures[json_prettier] + 1 ))
                    fail "json prettier"
                fi
            fi
        done
    else
        skipped yaml_prettier
        skipped json_prettier
        warn "prettier (not installed, skipping YAML/JSON checks)"
    fi
fi

echo ""
# Threshold summary for YAML/JSON checks
for check in yaml_prettier json_prettier; do
    failures=${check_failures[$check]}
    threshold_var="${check}_threshold"
    threshold=${!threshold_var}
    if [ "$failures" -gt 0 ]; then
        if [ "$failures" -gt "$threshold" ]; then
            echo "  ⚠ $check: $failures failures (threshold: $threshold)"
        else
            echo "  ✓ $check: $failures failures (threshold: $threshold)"
        fi
    else
        echo "  ✓ $check: 0 failures (threshold: $threshold)"
    fi
done

# ---------------------------------------------------------------------------
# TOML files
# ---------------------------------------------------------------------------
info "Linting TOML files..."

mapfile -t toml_files < <(find "$TARGET_DIR" -type f -name '*.toml' ! -path '*/\.*' ! -path '*/venv/*' ! -path '*/node_modules/*' ! -path '*/__pycache__/*' 2>/dev/null)

if [ ${#toml_files[@]} -eq 0 ]; then
    echo "  (none found)"
else
    if [ "$TOML_ENABLED" = "off" ]; then
        skipped toml_sort
        warn "TOML checks (disabled)"
    elif command -v toml-sort >/dev/null 2>&1; then
        for f in "${toml_files[@]}"; do
            FILES_CHECKED+=("$f")
            echo ""
            echo "  📝 $f"

            ran toml_sort
            set +e
            toml_output="$(toml-sort --check --sort-keys "$f" 2>&1)"
            toml_rc=$?
            set -e
            if [ "$toml_rc" -eq 0 ]; then
                pass "toml-sort"
            else
                echo "    $toml_output"
                check_failures[toml_sort]=$(( check_failures[toml_sort] + 1 ))
                fail "toml-sort"
            fi
        done
    else
        skipped toml_sort
        warn "toml-sort (not installed, skipping TOML checks)"
    fi
fi

echo ""
# Threshold summary for TOML checks
# shellcheck disable=SC2043
for check in toml_sort; do
    failures=${check_failures[$check]}
    threshold_var="${check}_threshold"
    threshold=${!threshold_var}
    if [ "$failures" -gt 0 ]; then
        if [ "$failures" -gt "$threshold" ]; then
            echo "  ⚠ $check: $failures failures (threshold: $threshold)"
        else
            echo "  ✓ $check: $failures failures (threshold: $threshold)"
        fi
    else
        echo "  ✓ $check: 0 failures (threshold: $threshold)"
    fi
done

# ---------------------------------------------------------------------------
# Security scanning
# ---------------------------------------------------------------------------
info "Running security scans..."

if [ "$SECURITY_ENABLED" = "off" ]; then
    skipped security_secrets
    skipped security_dangerous_patterns
    warn "Security checks (disabled)"
else
    # --- Hardcoded secrets in shell scripts ---
    info "Checking for hardcoded secrets in shell scripts..."
    # shellcheck disable=SC2043
    for f in "${shell_files[@]}"; do
        # Skip the lint script itself (contains patterns in comments)
        [ "$(basename "$f")" = "lint.sh" ] && continue
        ran security_secrets
        set +e
        secret_output=$(grep -Eni \
            "(password|passwd|secret|api_key|apikey|token|auth_token)[[:space:]]*=[[:space:]]*[\"'][^\"']+[\"']" \
            "$f" 2>&1) || secret_rc=$?
        set -e
        if [ "${secret_rc:-0}" -eq 0 ]; then
            check_failures[security_secrets]=$(( check_failures[security_secrets] + 1 ))
            echo "  ✗ hardcoded secrets: $f"
            echo "    $secret_output"
            fail "security (hardcoded secrets)"
        fi
    done

    # --- Hardcoded secrets in Python files ---
    info "Checking for hardcoded secrets in Python files..."
    # shellcheck disable=SC2043
    for f in "${py_files[@]}"; do
        # Skip server/mcp/test/init files (docstrings/strings contain pattern keywords)
        case "$(basename "$f")" in
            server.py|mcp_server.py|test_*.py|__init__.py) continue ;;
        esac
        # Skip test fixture directories
        case "$f" in
            */fixtures/*|*/test_project/*) continue ;;
        esac
        ran security_secrets
        set +e
        py_secret_output=$(grep -Eni \
            "(password|passwd|secret|api_key|apikey|token|auth_token)[[:space:]]*=[[:space:]]*[\"'][^\"']+[\"']" \
            "$f" 2>&1) || py_secret_rc=$?
        set -e
        if [ "${py_secret_rc:-0}" -eq 0 ]; then
            check_failures[security_secrets]=$(( check_failures[security_secrets] + 1 ))
            echo "  ✗ hardcoded secrets: $f"
            echo "    $py_secret_output"
            fail "security (hardcoded secrets)"
        fi
    done

    # --- Dangerous shell patterns ---
    info "Checking for dangerous shell patterns..."
    # shellcheck disable=SC2043
    for f in "${shell_files[@]}"; do
        # Skip the lint script itself (contains patterns in comments)
        [ "$(basename "$f")" = "lint.sh" ] && continue
        ran security_dangerous_patterns
        set +e
        dangerous_output=$(grep -Eni \
            'chmod[[:space:]]+777|curl[[:space:]].*[[:space:]]*\|[[:space:]]*.*bash|eval[[:space:]]+.*\$' \
            "$f" 2>&1) || dangerous_rc=$?
        set -e
        if [ "${dangerous_rc:-0}" -eq 0 ]; then
            check_failures[security_dangerous_patterns]=$(( check_failures[security_dangerous_patterns] + 1 ))
            echo "  ✗ dangerous patterns: $f"
            echo "    $dangerous_output"
            fail "security (dangerous patterns)"
        fi
    done
fi

echo ""
# Threshold summary for security checks
for check in security_secrets security_dangerous_patterns; do
    failures=${check_failures[$check]}
    threshold_var="${check}_threshold"
    threshold=${!threshold_var}
    if [ "$failures" -gt 0 ]; then
        if [ "$failures" -gt "$threshold" ]; then
            echo "  ⚠ $check: $failures failures (threshold: $threshold)"
        else
            echo "  ✓ $check: $failures failures (threshold: $threshold)"
        fi
    else
        echo "  ✓ $check: 0 failures (threshold: $threshold)"
    fi
done

# ---------------------------------------------------------------------------
# Gitleaks — git history secret detection
# ---------------------------------------------------------------------------
info "Running gitleaks..."

if [ "$GITLEAKS_ENABLED" = "off" ]; then
    skipped gitleaks
    warn "gitleaks (disabled)"
elif command -v gitleaks >/dev/null 2>&1; then
    # Which directory to search for a repository. TARGET_DIR is "$1", and both plugins pass a single
    # FILE, so the previous test -- [ ! -d "$TARGET_DIR/.git" ] -- asked whether a path *underneath a
    # file* was a directory. It never was, so gitleaks reported "skipped" after every edit and in
    # practice never ran on the plugin path at all.
    if [ -d "$TARGET_DIR" ]; then
        gitleaks_probe="$TARGET_DIR"
    else
        gitleaks_probe="$(dirname "$TARGET_DIR")"
    fi

    # Nearest ancestor holding .git, so a file deep inside a repository still gets history scanned.
    # No ceiling is needed here: this runs inside the container, where the mount is the only thing
    # visible, and the loop stops at "/" regardless.
    gitleaks_repo=""
    gitleaks_probe="$(cd "$gitleaks_probe" 2>/dev/null && pwd)" || gitleaks_probe=""
    while [ -n "$gitleaks_probe" ]; do
        if [ -d "$gitleaks_probe/.git" ]; then
            gitleaks_repo="$gitleaks_probe"
            break
        fi
        [ "$gitleaks_probe" = "/" ] && break
        gitleaks_probe="$(dirname "$gitleaks_probe")"
    done

    # The config lived at a hardcoded /usr/local/bin/.gitleaks.toml, which only exists inside the
    # image. Run on a host or in CI, gitleaks died with "unable to load gitleaks config" and, because
    # of the pass condition below, that counted as a pass. Resolved against the image path first so
    # container behaviour is unchanged, then against the copy beside this script, then dropped so
    # gitleaks falls back to its built-in rules rather than refusing to start.
    gitleaks_config=""
    for candidate in \
        "${MULTILINT_GITLEAKS_CONFIG:-}" \
        /usr/local/bin/.gitleaks.toml \
        "$(dirname "${BASH_SOURCE[0]}")/.gitleaks.toml"; do
        if [ -n "$candidate" ] && [ -f "$candidate" ]; then
            gitleaks_config="$candidate"
            break
        fi
    done
    gitleaks_config_args=()
    if [ -n "$gitleaks_config" ]; then
        gitleaks_config_args=(--config "$gitleaks_config")
    fi

    ran gitleaks
    set +e
    if [ -n "$gitleaks_repo" ]; then
        # History mode. GITLEAKS_DEPTH=all walks every commit; the default limits it to the most
        # recent one, which is what makes this affordable to run after a single edit.
        if [ "$GITLEAKS_DEPTH" = "all" ]; then
            gitleaks_output="$(gitleaks detect --source "$gitleaks_repo" \
                "${gitleaks_config_args[@]}" \
                --verbose --no-color --no-banner 2>&1)"
        else
            gitleaks_output="$(gitleaks detect --source "$gitleaks_repo" \
                "${gitleaks_config_args[@]}" \
                --log-opts="-1" \
                --verbose --no-color --no-banner 2>&1)"
        fi
    else
        # No repository, so there is no history -- but the working copy can still be scanned.
        # --no-git treats the source as an ordinary path, which may be a single file, and --log-opts
        # is documented as having no effect in this mode, so it is not passed. This is the branch that
        # turns a permanent "skipped" into a real verdict.
        gitleaks_output="$(gitleaks detect --no-git --source "$TARGET_DIR" \
            "${gitleaks_config_args[@]}" \
            --verbose --no-color --no-banner 2>&1)"
    fi
    gitleaks_rc=$?
    set -e
    gitleaks_findings=$(echo "$gitleaks_output" | grep -c "Finding:" 2>/dev/null || true)
    gitleaks_findings=${gitleaks_findings:-0}
    # gitleaks exits 1 both for "leaks found" and for its own failures -- a missing config file exits
    # 1 with no findings -- so the exit status alone cannot tell them apart. The previous condition
    # was `rc -eq 0 || findings -eq 0`, which resolved that ambiguity by calling both a pass: a
    # gitleaks that never started reported ✓. Non-zero with no findings is now reported as skipped,
    # which is what it is.
    if [ "$gitleaks_rc" -eq 0 ]; then
        pass "gitleaks"
    elif [ "$gitleaks_findings" -gt 0 ]; then
        echo "$gitleaks_output" | head -50
        check_failures[gitleaks]=$gitleaks_findings
        fail "gitleaks"
    else
        echo "$gitleaks_output" | head -10
        skipped gitleaks
        warn "gitleaks (did not complete, exit $gitleaks_rc)"
    fi
else
    skipped gitleaks
    warn "gitleaks (not installed, skipping)"
fi

echo ""
# Threshold summary for gitleaks
failures=${check_failures[gitleaks]}
if [ "$failures" -gt 0 ]; then
    if [ "$failures" -gt "$gitleaks_threshold" ]; then
        echo "  ⚠ gitleaks: $failures findings (threshold: $gitleaks_threshold)"
    else
        echo "  ✓ gitleaks: $failures findings (threshold: $gitleaks_threshold)"
    fi
else
    echo "  ✓ gitleaks: 0 findings (threshold: $gitleaks_threshold)"
fi

# ---------------------------------------------------------------------------
# Summary and JSON output
# ---------------------------------------------------------------------------
echo ""

if [ "$OUTPUT_FORMAT" = "json" ]; then
    # Restore stdout for JSON output
    exec 1>&3
    # Export data for JSON generation.
    #
    # Driven by ALL_CHECKS rather than a hand-maintained list. The old literal
    # enumerated 11 names and omitted mypy, bandit, yaml_prettier, json_prettier
    # and toml_sort, so five checks could fail and never appear in the JSON that
    # the plugin consumes.
    #
    # Record layout is failures:threshold:exceeded:status:total. status and total
    # are appended after the original three fields, so the positional indices
    # anything already parsing this format relies on do not move.
    for check in "${ALL_CHECKS[@]}"; do
        failures=${check_failures[$check]}
        total=${check_totals[$check]}
        status=${check_status[$check]}
        threshold_var="${check}_threshold"
        threshold=${!threshold_var}
        threshold_exceeded="false"
        [ "$failures" -gt "$threshold" ] && threshold_exceeded="true"
        # A check with failures reports "failed" regardless of threshold: the
        # threshold governs whether the run fails, not whether the check found
        # anything. "skipped" wins, since a check that never ran cannot fail.
        if [ "$status" != "skipped" ] && [ "$failures" -gt 0 ]; then
            status="failed"
        fi
        export "ML_CHECK_${check}=${failures}:${threshold}:${threshold_exceeded}:${status}:${total}"
    done
    # Build file list as comma-separated
    ML_FILES=""
    FIRST=1
    for f in "${FILES_CHECKED[@]}"; do
        [ "$FIRST" -eq 0 ] && ML_FILES="${ML_FILES},"
        ML_FILES="${ML_FILES}${f}"
        FIRST=0
    done
    export ML_FILES
    # Emission order for the "checks" object. Reading os.environ instead would
    # make key order depend on the environment, so it varied run to run.
    ML_CHECK_ORDER="$(IFS=,; echo "${ALL_CHECKS[*]}")"
    export ML_CHECK_ORDER
    # Derived, not hardcoded. This was literally 11 while check_failures held 16
    # entries, so summary.checks_run disagreed with the checks it summarised.
    export ML_CHECKS_COUNT=${#ALL_CHECKS[@]}
    export ML_TOTAL_FILES=${#FILES_CHECKED[@]}
    export ML_EXIT_CODE=$EXIT_CODE
    # shellcheck disable=SC2155
    _ml_json="$(
        python3 <<'ML_PYTHON'
import json, os

# Record layout exported by the shell above:
#   failures : threshold : threshold_exceeded : status : total
FAILURES, THRESHOLD, EXCEEDED, STATUS, TOTAL = range(5)

checks = {}
for name in os.environ.get("ML_CHECK_ORDER", "").split(","):
    name = name.strip()
    raw = os.environ.get("ML_CHECK_" + name) if name else None
    if raw:
        parts = raw.split(":")
        failed = int(parts[FAILURES])
        total = int(parts[TOTAL])
        checks[name] = {
            # "passed" previously held int(parts[0]) — the failure count, the
            # same value as "failed". It now means what its name says: units
            # that went through this check and came back clean. max() guards
            # against gitleaks, whose counter holds findings rather than files
            # and can therefore exceed its own total of 1.
            "passed": max(total - failed, 0),
            "failed": failed,
            "total": total,
            "threshold": int(parts[THRESHOLD]),
            "threshold_exceeded": parts[EXCEEDED] == "true",
            # "ok" | "failed" | "skipped". Without this a missing tool and a
            # clean pass are both failures=0, so a check that never ran is
            # indistinguishable from one that ran and found nothing.
            "status": parts[STATUS],
        }
files = [f.strip() for f in os.environ.get("ML_FILES", "").split(",") if f.strip()]
skipped = sorted(n for n, c in checks.items() if c["status"] == "skipped")
result = {
    "summary": {
        "files_checked": len(files),
        "checks_run": int(os.environ.get("ML_CHECKS_COUNT", 8)),
        # Promoted into the summary so a consumer can react to skipped checks
        # without walking every entry in "checks".
        "checks_skipped": skipped,
    },
    "checks": checks,
    "return_code": int(os.environ.get("ML_EXIT_CODE", "0")),
    "files": files,
}
print(json.dumps(result, indent=2))
ML_PYTHON
    )"
    printf '%s\n' "$_ml_json"
else
    echo "================================"
    if [ "$EXIT_CODE" -eq 0 ]; then
        echo "  All checks passed ✓"
    else
        echo "  Some checks failed ✗"
    fi
    echo "================================"
fi

exit "$EXIT_CODE"
