# shellcheck shell=bash
# ml_check_docs.sh - sourced by lint.sh at top level, never run on its own.
# Markdown, YAML/JSON and TOML checks: markdownlint, yaml_prettier, json_prettier, toml_sort.
# Reads and writes lint.sh globals (TARGET_DIR, OUTPUT_FORMAT, ALL_CHECKS, check_failures,
# check_totals, check_status, check_reason, ...), so it is not wrapped in a function:
# a declare -A inside a function would be local.
# shellcheck disable=SC2034,SC2154

# ---------------------------------------------------------------------------
# Markdown files
# ---------------------------------------------------------------------------
info "Linting Markdown files..."

mapfile -t md_files < <(ml_files '*.md' -- '*/\.*' '*/node_modules/*' '*/cache/*' '*/output/*')

# markdownlint.rules from .multilint.json (see ml_config.sh) goes into the scratch directory, which
# ml_lib.sh creates and removes on exit. Without one, the built-in rule set in markdownlint_args
# stays in place. ml_config.sh has printed the config warnings to stderr by now, so this one is
# printed here as well as added to ml_warnings for the JSON report.
if [ -n "$ml_markdownlint_rules" ]; then
    if [ -n "$ML_MSG_DIR" ]; then
        printf '%s\n' "$ml_markdownlint_rules" >"$ML_MSG_DIR/markdownlint-rules.json"
        markdownlint_args=(-c "$ML_MSG_DIR/markdownlint-rules.json")
    else
        ml_warnings+=("markdownlint.rules ignored: no scratch directory to write them to")
        printf 'multilint: config warning: %s\n' "${ml_warnings[-1]}" >&2
    fi
fi

if [ ${#md_files[@]} -eq 0 ]; then
    echo "  (none found)"
else
    for f in "${md_files[@]}"; do
        FILES_CHECKED+=("$f")
        echo ""
        echo "  📝 $f"

        # markdownlint; policy flags come from markdownlint_args: empty when the project has its
        # own markdownlint config (markdownlint finds it in $PWD), otherwise -c with the built-in
        # rule set or the merged markdownlint.rules. The array expansion also replaces the former
        # unquoted $MD_CONFIG word-split.
        if ! check_enabled markdownlint; then
            skipped markdownlint "disabled by checks.markdownlint.enabled"
            warn "markdownlint (disabled)"
        elif command -v markdownlint >/dev/null 2>&1; then
            ran markdownlint
            # The former `|| true` on the next line was both redundant and
            # actively harmful: `set +e` already stops a non-zero exit from
            # aborting the script, while `|| true` made the command list itself
            # succeed, so md_rc read `true`'s 0 and never markdownlint's status.
            # Every Markdown file therefore reported ✓ — including while the
            # binary was crashing outright with "Cannot find package 'commander'".
            set +e
            md_output="$(markdownlint "${markdownlint_args[@]+"${markdownlint_args[@]}"}" "$f" 2>&1)"
            md_rc=$?
            set -e
            if [ "$md_rc" -eq 0 ]; then
                pass "markdownlint"
            else
                echo "    $md_output"
                record_output markdownlint "$f" "$md_output"
                check_failures[markdownlint]=$(( check_failures[markdownlint] + 1 ))
                fail "markdownlint"
            fi
        else
            skipped markdownlint "not installed on PATH"
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

mapfile -t yaml_files < <(ml_files '*.yaml' '*.yml' -- '*/\.*' '*/venv/*' '*/node_modules/*' '*/__pycache__/*')
mapfile -t json_files < <(ml_files '*.json' -- '*/\.*' '*/node_modules/*' '*/venv/*' '*/__pycache__/*')

if [ ${#yaml_files[@]} -eq 0 ] && [ ${#json_files[@]} -eq 0 ]; then
    echo "  (none found)"
elif ! check_enabled yaml_prettier && ! check_enabled json_prettier; then
    skipped yaml_prettier "disabled by checks.yaml_prettier.enabled"
    skipped json_prettier "disabled by checks.json_prettier.enabled"
    warn "YAML/JSON checks (disabled)"
elif command -v prettier >/dev/null 2>&1; then
    for f in "${yaml_files[@]}" "${json_files[@]}"; do
        FILES_CHECKED+=("$f")
        echo ""
        echo "  📋 $f"

        # yaml_prettier and json_prettier resolve independently -- each has
        # its own "checks.<name>.enabled" and "checks.<name>.args" in
        # .multilint.json. Harness flags --check --log-level error are
        # always kept.
        if [[ "$f" == *.yaml || "$f" == *.yml ]]; then
            if ! check_enabled yaml_prettier; then
                skipped yaml_prettier "disabled by checks.yaml_prettier.enabled"
                warn "yaml prettier (disabled)"
            else
                ran yaml_prettier
                set +e
                prettier_output="$(prettier --check --log-level error \
                    "${yaml_prettier_args[@]+"${yaml_prettier_args[@]}"}" "$f" 2>&1)"
                prettier_rc=$?
                set -e
                if [ "$prettier_rc" -eq 0 ]; then
                    pass "yaml prettier"
                else
                    echo "    $prettier_output"
                    record_output yaml_prettier "$f" "$prettier_output"
                    check_failures[yaml_prettier]=$(( check_failures[yaml_prettier] + 1 ))
                    fail "yaml prettier"
                fi
            fi
        elif [[ "$f" == *.json ]]; then
            if ! check_enabled json_prettier; then
                skipped json_prettier "disabled by checks.json_prettier.enabled"
                warn "json prettier (disabled)"
            else
                ran json_prettier
                set +e
                prettier_output="$(prettier --check --log-level error \
                    "${json_prettier_args[@]+"${json_prettier_args[@]}"}" "$f" 2>&1)"
                prettier_rc=$?
                set -e
                if [ "$prettier_rc" -eq 0 ]; then
                    pass "json prettier"
                else
                    echo "    $prettier_output"
                    record_output json_prettier "$f" "$prettier_output"
                    check_failures[json_prettier]=$(( check_failures[json_prettier] + 1 ))
                    fail "json prettier"
                fi
            fi
        fi
    done
else
    skipped yaml_prettier "not installed on PATH"
    skipped json_prettier "not installed on PATH"
    warn "prettier (not installed, skipping YAML/JSON checks)"
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

mapfile -t toml_files < <(ml_files '*.toml' -- '*/\.*' '*/venv/*' '*/node_modules/*' '*/__pycache__/*')

if [ ${#toml_files[@]} -eq 0 ]; then
    echo "  (none found)"
else
    if ! check_enabled toml_sort; then
        skipped toml_sort "disabled by checks.toml_sort.enabled"
        warn "TOML checks (disabled)"
    elif command -v toml-sort >/dev/null 2>&1; then
        for f in "${toml_files[@]}"; do
            FILES_CHECKED+=("$f")
            echo ""
            echo "  📝 $f"

            # Harness flag --check is always kept; policy flags come from
            # toml_sort_args (default: --sort-table-keys).
            ran toml_sort
            set +e
            toml_output="$(toml-sort --check "${toml_sort_args[@]+"${toml_sort_args[@]}"}" "$f" 2>&1)"
            toml_rc=$?
            set -e
            if [ "$toml_rc" -eq 0 ]; then
                pass "toml-sort"
            else
                echo "    $toml_output"
                record_output toml_sort "$f" "$toml_output"
                check_failures[toml_sort]=$(( check_failures[toml_sort] + 1 ))
                fail "toml-sort"
            fi
        done
    else
        skipped toml_sort "not installed on PATH"
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
