# shellcheck shell=bash
# ml_report.sh - sourced by lint.sh at top level, never run on its own.
# Summary output: text footer, or the JSON report rendered by ml_report_json.py.
# Reads and writes lint.sh globals (TARGET_DIR, OUTPUT_FORMAT, ALL_CHECKS, check_failures,
# check_totals, check_status, check_reason, ...), so it is not wrapped in a function:
# a declare -A inside a function would be local.
# shellcheck disable=SC2034,SC2154

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
        threshold_exceeded="${check_threshold_exceeded[$check]}"
        # A check with failures reports "failed" regardless of threshold: the
        # threshold governs whether the run fails, not whether the check found
        # anything. "skipped" wins, since a check that never ran cannot fail.
        if [ "$status" != "skipped" ] && [ "$failures" -gt 0 ]; then
            status="failed"
        fi
        export "ML_CHECK_${check}=${failures}:${threshold}:${threshold_exceeded}:${status}:${total}"
        # Separate variable: a reason can contain ":" and would break the record above.
        export "ML_REASON_${check}=${check_reason[$check]}"
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
    # Per-check message directory and the effective policy flags of the
    # formatters, which the emitter needs to spell out exact auto-fix commands.
    # printf %q keeps each flag paste-safe in the hint.
    export ML_MSG_DIR
    for check in black shfmt yaml_prettier json_prettier toml_sort; do
        declare -n ml_args_ref="${check}_args"
        if [ "${#ml_args_ref[@]}" -gt 0 ]; then
            export "ML_ARGS_${check}=$(printf '%q ' "${ml_args_ref[@]}")"
        fi
        unset -n ml_args_ref
    done
    # Emission order for the "checks" object. Reading os.environ instead would
    # make key order depend on the environment, so it varied run to run.
    ML_CHECK_ORDER="$(IFS=,; echo "${ALL_CHECKS[*]}")"
    export ML_CHECK_ORDER
    # Derived, not hardcoded. This was literally 11 while check_failures held 16
    # entries, so summary.checks_run disagreed with the checks it summarised.
    export ML_CHECKS_COUNT=${#ALL_CHECKS[@]}
    export ML_TOTAL_FILES=${#FILES_CHECKED[@]}
    export ML_EXIT_CODE=$EXIT_CODE
    # Newline-joined configuration warnings, safe to split on "\n": the
    # loader rejects any string containing a control character, so no
    # warning message can contain an embedded newline of its own.
    ML_WARNINGS="$(printf '%s\n' "${ml_warnings[@]+"${ml_warnings[@]}"}")"
    export ML_WARNINGS
    # shellcheck disable=SC2155
    _ml_json="$(
        python3 "$ML_DIR/ml_report_json.py"
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
