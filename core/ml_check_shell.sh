# shellcheck shell=bash
# ml_check_shell.sh - sourced by lint.sh at top level, never run on its own.
# Shell checks: bash_syntax, shellcheck, bashate, shfmt, and their threshold summary.
# Reads and writes lint.sh globals (TARGET_DIR, OUTPUT_FORMAT, ALL_CHECKS, check_failures,
# check_totals, check_status, check_reason, ...), so it is not wrapped in a function:
# a declare -A inside a function would be local.
# shellcheck disable=SC2034,SC2154

# ---------------------------------------------------------------------------
# Shell scripts
# ---------------------------------------------------------------------------
info "Linting shell scripts..."

mapfile -t shell_files < <(ml_files '*.sh' '*.bash' -- '*/\.*' '*/tests/*' '*/test/*' '*/venv/*' '*/node_modules/*')

if [ ${#shell_files[@]} -eq 0 ]; then
    echo "  (none found)"
else
    for f in "${shell_files[@]}"; do
        FILES_CHECKED+=("$f")
        echo ""
        echo "  📄 $f"

        # Bash syntax check — bash itself is always present; only enablement
        # (checks.bash_syntax.enabled) can skip it. args is unsupported here
        # (bash -n takes no policy flags).
        if ! check_enabled bash_syntax; then
            skipped bash_syntax "disabled by checks.bash_syntax.enabled"
            warn "bash_syntax (disabled)"
        else
            ran bash_syntax
            if bash -n "$f" >/dev/null 2>&1; then
                pass "bash syntax"
            else
                bs_output="$(bash -n "$f" 2>&1 || true)"
                echo "    $bs_output"
                record_output bash_syntax "$f" "$bs_output"
                check_failures[bash_syntax]=$(( check_failures[bash_syntax] + 1 ))
                fail "bash syntax"
            fi
        fi

        # ShellCheck — policy flags come from shellcheck_args (default:
        # excludes SC1091/SC2155/SC2086, reports down to style severity).
        # -f gcc is a harness flag, always kept after the policy flags: one
        # file:line:col line per finding, which the JSON emitter can parse.
        if ! check_enabled shellcheck; then
            skipped shellcheck "disabled by checks.shellcheck.enabled"
            warn "shellcheck (disabled)"
        elif command -v shellcheck >/dev/null 2>&1; then
            ran shellcheck
            set +e
            sc_output="$(shellcheck "${shellcheck_args[@]+"${shellcheck_args[@]}"}" -f gcc "$f" 2>&1)"
            sc_code=$?
            set -e
            if [ "$sc_code" -eq 0 ]; then
                pass "shellcheck"
            else
                echo "    $sc_output"
                record_output shellcheck "$f" "$sc_output"
                check_failures[shellcheck]=$(( check_failures[shellcheck] + 1 ))
                fail "shellcheck"
            fi
        else
            skipped shellcheck "not installed on PATH"
            warn "shellcheck (not installed, skipping)"
        fi

        # Bashate — 4-space indentation check; policy flags come from
        # bashate_args (default: excludes E006, line length).
        if ! check_enabled bashate; then
            skipped bashate "disabled by checks.bashate.enabled"
            warn "bashate (disabled)"
        elif command -v bashate >/dev/null 2>&1; then
            ran bashate
            set +e
            bashate_output="$(bashate "${bashate_args[@]+"${bashate_args[@]}"}" "$f" 2>&1)"
            bashate_rc=$?
            set -e
            if [ "$bashate_rc" -eq 0 ]; then
                pass "bashate"
            else
                echo "    $bashate_output"
                record_output bashate "$f" "$bashate_output"
                check_failures[bashate]=$(( check_failures[bashate] + 1 ))
                fail "bashate (indentation required)"
            fi
        else
            skipped bashate "not installed on PATH"
            warn "bashate (not installed, skipping)"
        fi

        # shfmt (format check only)
        #
        # -i 4 is load-bearing, not cosmetic. shfmt's default is 0, meaning TAB indents, while
        # bashate above emits E002 "Tab indents" and E003 "Indent not multiple of 4" — so with the
        # default the two checks demanded opposite things and NO shell file could pass both. Four
        # spaces is the documented project policy (see Policies in README.md) and what three of the
        # four shell files here already use, and bashate's rule cannot be configured to accept tabs,
        # so shfmt is the side that gets configured.
        #
        # The cost of not doing this was real: claude-plugin/scripts/multilint.sh was written with
        # no indented lines at all, purely so it could satisfy both checks.
        #
        # Passing a printer flag also makes shfmt ignore any .editorconfig it finds, which keeps this
        # verdict identical inside the container and on a contributor's machine.
        if ! check_enabled shfmt; then
            skipped shfmt "disabled by checks.shfmt.enabled"
            warn "shfmt (disabled)"
        elif command -v shfmt >/dev/null 2>&1; then
            ran shfmt
            # shfmt -d exits 1 on a diff; capture output and status explicitly so pipefail cannot
            # turn that into a pass. rc>1 is a genuine shfmt error, also not a pass.
            set +e
            shfmt_output="$(shfmt "${shfmt_args[@]+"${shfmt_args[@]}"}" -d "$f" 2>&1)"
            shfmt_rc=$?
            set -e
            if [ "$shfmt_rc" -ne 0 ] || [ -n "$shfmt_output" ]; then
                record_output shfmt "$f" "$shfmt_output"
                check_failures[shfmt]=$(( check_failures[shfmt] + 1 ))
                fail "shfmt (formatting required)"
            else
                pass "shfmt"
            fi
        else
            skipped shfmt "not installed on PATH"
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
