# shellcheck shell=bash
# ml_check_python.sh - sourced by lint.sh at top level, never run on its own.
# Python checks: flake8, black, pylint, mypy, bandit, and their threshold summary.
# Reads and writes lint.sh globals (TARGET_DIR, OUTPUT_FORMAT, ALL_CHECKS, check_failures,
# check_totals, check_status, check_reason, ...), so it is not wrapped in a function:
# a declare -A inside a function would be local.
# shellcheck disable=SC2034,SC2154

# ---------------------------------------------------------------------------
# Python scripts
# ---------------------------------------------------------------------------
info "Linting Python scripts..."

mapfile -t py_files < <(ml_files '*.py' -- '*/\.*' '*/test/*' '*/__pycache__/*' '*/venv/*' '*/node_modules/*' '*/fixtures/*')

if [ ${#py_files[@]} -eq 0 ]; then
    echo "  (none found)"
else
    for f in "${py_files[@]}"; do
        FILES_CHECKED+=("$f")
        echo ""
        echo "  🐍 $f"

        # flake8 (style); policy flags come from flake8_args.
        if ! check_enabled flake8; then
            skipped flake8 "disabled by checks.flake8.enabled"
            warn "flake8 (disabled)"
        elif command -v flake8 >/dev/null 2>&1; then
            ran flake8
            set +e
            pb_flake8="$(flake8 "${flake8_args[@]+"${flake8_args[@]}"}" "$f" 2>&1)"
            pb_flake8_rc=$?
            set -e
            if [ "$pb_flake8_rc" -eq 0 ]; then
                pass "flake8"
            else
                echo "    $pb_flake8"
                record_output flake8 "$f" "$pb_flake8"
                check_failures[flake8]=$(( check_failures[flake8] + 1 ))
                fail "flake8"
            fi
        else
            skipped flake8 "not installed on PATH"
            warn "flake8 (not installed, skipping)"
        fi

        # black (formatting); harness flag --check is always kept so a run
        # never rewrites the target file; policy flags come from black_args.
        if ! check_enabled black; then
            skipped black "disabled by checks.black.enabled"
            warn "black (disabled)"
        elif command -v black >/dev/null 2>&1; then
            ran black
            set +e
            pb_black="$(black --check "${black_args[@]+"${black_args[@]}"}" "$f" 2>&1)"
            pb_black_rc=$?
            set -e
            if [ "$pb_black_rc" -eq 0 ]; then
                pass "black"
            else
                echo "    $pb_black"
                record_output black "$f" "$pb_black"
                check_failures[black]=$(( check_failures[black] + 1 ))
                fail "black"
            fi
        else
            skipped black "not installed on PATH"
            warn "black (not installed, skipping)"
        fi

        # pylint (errors/warnings); harness flag --output-format=text is
        # always kept; policy flags come from pylint_args.
        if ! check_enabled pylint; then
            skipped pylint "disabled by checks.pylint.enabled"
            warn "pylint (disabled)"
        elif command -v pylint >/dev/null 2>&1; then
            ran pylint
            set +e
            # The container user has no writable home, so pylint's default cache dir (/.cache/pylint)
            # fails with a warning that would land in the findings; point it at the temp dir instead.
            pb_pylint="$(PYLINTHOME="${PYLINTHOME:-${ML_MSG_DIR:-/tmp}/.pylint}" pylint --output-format=text "${pylint_args[@]+"${pylint_args[@]}"}" "$f" 2>&1)"
            pb_pylint_rc=$?
            set -e
            if [ "$pb_pylint_rc" -eq 0 ]; then
                pass "pylint"
            else
                echo "    $pb_pylint"
                record_output pylint "$f" "$pb_pylint"
                check_failures[pylint]=$(( check_failures[pylint] + 1 ))
                fail "pylint"
            fi
        else
            skipped pylint "not installed on PATH"
            warn "pylint (not installed, skipping)"
        fi

        # mypy (static types)
        #
        # --ignore-missing-imports and --follow-imports=silent are the default
        # mypy_args (a checks.mypy.args override replaces them): the image
        # installs no project dependencies, so without them every third-party
        # import reports import-not-found and drowns out real findings. This
        # mirrors pylint running with E0401 disabled. --cache-dir and
        # --no-error-summary are harness flags and are always kept.
        if ! check_enabled mypy; then
            skipped mypy "disabled by checks.mypy.enabled"
            warn "mypy (disabled)"
        elif command -v mypy >/dev/null 2>&1; then
            ran mypy
            set +e
            pb_mypy="$(mypy --cache-dir="$MYPY_CACHE_DIR" --no-error-summary \
                "${mypy_args[@]+"${mypy_args[@]}"}" "$f" 2>&1)"
            pb_mypy_rc=$?
            set -e
            if [ "$pb_mypy_rc" -eq 0 ]; then
                pass "mypy"
            else
                echo "    $pb_mypy"
                record_output mypy "$f" "$pb_mypy"
                check_failures[mypy]=$(( check_failures[mypy] + 1 ))
                fail "mypy"
            fi
        else
            skipped mypy "not installed on PATH"
            warn "mypy (not installed, skipping)"
        fi

        # bandit (security); harness flags -q "$BANDIT_SEVERITY" -f custom
        # --msg-template are always kept (severity is the bandit.severity
        # option, not args); bandit has no default policy flags of its own.
        if ! check_enabled bandit; then
            skipped bandit "disabled by checks.bandit.enabled"
            warn "bandit (disabled)"
        elif command -v bandit >/dev/null 2>&1; then
            ran bandit
            set +e
            pb_bandit="$(bandit -q "$BANDIT_SEVERITY" -f custom \
                --msg-template "$BANDIT_TEMPLATE" "${bandit_args[@]+"${bandit_args[@]}"}" "$f" 2>&1)"
            pb_bandit_rc=$?
            set -e
            if [ "$pb_bandit_rc" -eq 0 ]; then
                pass "bandit"
            else
                echo "    $pb_bandit"
                record_output bandit "$f" "$pb_bandit"
                check_failures[bandit]=$(( check_failures[bandit] + 1 ))
                fail "bandit"
            fi
        else
            skipped bandit "not installed on PATH"
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
