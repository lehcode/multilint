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

info()  { echo -e "\n\033[1m→ $*\033[0m"; }
pass()  { echo "  ✓ $*"; }
fail()  { echo "  ✗ $*"; EXIT_CODE=1; }
warn()  { echo "  ~ $*"; }

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

        # Bash syntax check
        if bash -n "$f" >/dev/null 2>&1; then
            pass "bash syntax"
        else
            echo "    $(bash -n "$f" 2>&1)"
            check_failures[bash_syntax]=$(( check_failures[bash_syntax] + 1 ))
            fail "bash syntax"
        fi

        # ShellCheck — excludes SC1091/SC2155/SC2086, disables style
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

        # Bashate — 4-space indentation check, excludes E006 (line length)
        if [ "$BASHATE_ENABLED" = "off" ]; then
            warn "bashate (disabled)"
        elif command -v bashate >/dev/null 2>&1; then
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
            warn "bashate (not installed, skipping)"
        fi

        # shfmt (format check only)
        if [ "$SHFMT_ENABLED" = "off" ]; then
            warn "shfmt (disabled)"
        elif command -v shfmt >/dev/null 2>&1; then
            if shfmt -d "$f" | grep -q .; then
                check_failures[shfmt]=$(( check_failures[shfmt] + 1 ))
                fail "shfmt (formatting required)"
            else
                pass "shfmt"
            fi
        else
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

        # black (formatting)
        if [ "$BLACK_ENABLED" = "off" ]; then
            warn "black (disabled)"
        else
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
        fi

        # pylint (errors/warnings)
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

        # mypy (static types)
        #
        # --ignore-missing-imports and --follow-imports=silent are required, not
        # cosmetic: the image installs no project dependencies, so without them
        # every third-party import reports import-not-found and drowns out real
        # findings. This mirrors pylint running with E0401 disabled.
        if [ "$MYPY_ENABLED" = "off" ]; then
            warn "mypy (disabled)"
        else
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
        fi

        # bandit (security)
        if [ "$BANDIT_ENABLED" = "off" ]; then
            warn "bandit (disabled)"
        else
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
            MD_CONFIG=""
            if [ -f ".markdownlint.json" ]; then
                MD_CONFIG="-c .markdownlint.json"
            fi
            set +e
            md_output="$(markdownlint $MD_CONFIG "$f" 2>&1)" || true
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
        warn "YAML/JSON checks (disabled)"
    elif command -v prettier >/dev/null 2>&1; then
        for f in "${yaml_files[@]}" "${json_files[@]}"; do
            FILES_CHECKED+=("$f")
            echo ""
            echo "  📋 $f"

            if [[ "$f" == *.yaml || "$f" == *.yml ]]; then
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
        warn "TOML checks (disabled)"
    elif command -v toml-sort >/dev/null 2>&1; then
        for f in "${toml_files[@]}"; do
            FILES_CHECKED+=("$f")
            echo ""
            echo "  📝 $f"

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
    warn "Security checks (disabled)"
else
    # --- Hardcoded secrets in shell scripts ---
    info "Checking for hardcoded secrets in shell scripts..."
    # shellcheck disable=SC2043
    for f in "${shell_files[@]}"; do
        # Skip the lint script itself (contains patterns in comments)
        [ "$(basename "$f")" = "lint.sh" ] && continue
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
    warn "gitleaks (disabled)"
elif command -v gitleaks >/dev/null 2>&1; then
    if [ ! -d "$TARGET_DIR/.git" ]; then
        warn "gitleaks (.git not found, skipping)"
    else
        set +e
        if [ "$GITLEAKS_DEPTH" = "all" ]; then
            gitleaks_output="$(gitleaks detect --source "$TARGET_DIR" \
                --config /usr/local/bin/.gitleaks.toml \
                --verbose --no-color --no-banner 2>&1)"
        else
            gitleaks_output="$(gitleaks detect --source "$TARGET_DIR" \
                --config /usr/local/bin/.gitleaks.toml \
                --log-opts="-1" \
                --verbose --no-color --no-banner 2>&1)"
        fi
        gitleaks_rc=$?
        set -e
        gitleaks_findings=$(echo "$gitleaks_output" | grep -c "Finding:" 2>/dev/null || true)
        gitleaks_findings=${gitleaks_findings:-0}
        if [ "$gitleaks_rc" -eq 0 ] || [ "$gitleaks_findings" -eq 0 ]; then
            pass "gitleaks"
        else
            echo "$gitleaks_output" | head -50
            check_failures[gitleaks]=$gitleaks_findings
            fail "gitleaks"
        fi
    fi
else
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
    # Export data for JSON generation
    for check in bash_syntax shellcheck bashate shfmt flake8 black pylint markdownlint security_secrets security_dangerous_patterns gitleaks; do
        failures=${check_failures[$check]}
        threshold_var="${check}_threshold"
        threshold=${!threshold_var}
        threshold_exceeded="false"
        [ "$failures" -gt "$threshold" ] && threshold_exceeded="true"
        export "ML_CHECK_${check}=${failures}:${threshold}:${threshold_exceeded}"
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
    export ML_CHECKS_COUNT=11
    export ML_TOTAL_FILES=${#FILES_CHECKED[@]}
    export ML_EXIT_CODE=$EXIT_CODE
    # shellcheck disable=SC2155
    _ml_json="$(
        python3 <<'ML_PYTHON'
import json, os
checks = {}
_ml_keys = list(os.environ.keys())
for _k in _ml_keys:
    if _k.startswith("ML_CHECK_"):
        name = _k[9:]
        parts = os.environ[_k].split(":")
        checks[name] = {
            "passed": int(parts[0]),
            "failed": int(parts[0]),
            "threshold": int(parts[1]),
            "threshold_exceeded": parts[2] == "true",
        }
files = [f.strip() for f in os.environ.get("ML_FILES", "").split(",") if f.strip()]
result = {
    "summary": {
        "files_checked": len(files),
        "checks_run": int(os.environ.get("ML_CHECKS_COUNT", 8)),
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
