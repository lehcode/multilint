# shellcheck shell=bash
# ml_check_security.sh - sourced by lint.sh at top level, never run on its own.
# Security checks: security_secrets, security_dangerous_patterns, and gitleaks.
# Reads and writes lint.sh globals (TARGET_DIR, OUTPUT_FORMAT, ALL_CHECKS, check_failures,
# check_totals, check_status, check_reason, ...), so it is not wrapped in a function:
# a declare -A inside a function would be local.
# shellcheck disable=SC2034,SC2154

# ---------------------------------------------------------------------------
# Security scanning
# ---------------------------------------------------------------------------
info "Running security scans..."

# security_secrets and security_dangerous_patterns resolve independently --
# each has its own "checks.<name>.enabled" in .multilint.json.
if ! check_enabled security_secrets; then
    skipped security_secrets "disabled by checks.security_secrets.enabled"
    warn "security_secrets (disabled)"
else
    # --- Hardcoded secrets in shell scripts ---
    info "Checking for hardcoded secrets in shell scripts..."
    # shellcheck disable=SC2043
    for f in "${shell_files[@]}"; do
        # Skip the lint script itself (contains patterns in comments)
        [ "$(basename "$f")" = "lint.sh" ] && continue
        ran security_secrets
        set +e
        secret_rc=0
        secret_output=$(grep -Eni \
            "(password|passwd|secret|api_key|apikey|token|auth_token)[[:space:]]*=[[:space:]]*[\"'][^\"']+[\"']" \
            "$f" 2>&1) || secret_rc=$?
        set -e
        if [ "${secret_rc:-0}" -eq 0 ]; then
            check_failures[security_secrets]=$(( check_failures[security_secrets] + 1 ))
            echo "  ✗ hardcoded secrets: $f"
            echo "    $secret_output"
            record_output security_secrets "$f" "$secret_output"
            fail "security (hardcoded secrets)"
        fi
    done

    # --- Hardcoded secrets in Python files ---
    info "Checking for hardcoded secrets in Python files..."
    # shellcheck disable=SC2043
    for f in "${py_files[@]}"; do
        # Skip test/init files (docstrings/strings contain pattern keywords)
        # Was: server.py|mcp_server.py|test_*.py|__init__.py) -- the first two quieted multilint's own,
        # since removed, service files and hid secrets in any user file with those names.
        case "$(basename "$f")" in
            test_*.py|__init__.py) continue ;;
        esac
        # Skip test fixture directories
        case "$f" in
            */fixtures/*|*/test_project/*) continue ;;
        esac
        ran security_secrets
        set +e
        py_secret_rc=0
        py_secret_output=$(grep -Eni \
            "(password|passwd|secret|api_key|apikey|token|auth_token)[[:space:]]*=[[:space:]]*[\"'][^\"']+[\"']" \
            "$f" 2>&1) || py_secret_rc=$?
        set -e
        if [ "${py_secret_rc:-0}" -eq 0 ]; then
            check_failures[security_secrets]=$(( check_failures[security_secrets] + 1 ))
            echo "  ✗ hardcoded secrets: $f"
            echo "    $py_secret_output"
            record_output security_secrets "$f" "$py_secret_output"
            fail "security (hardcoded secrets)"
        fi
    done
fi

if ! check_enabled security_dangerous_patterns; then
    skipped security_dangerous_patterns "disabled by checks.security_dangerous_patterns.enabled"
    warn "security_dangerous_patterns (disabled)"
else
    # --- Dangerous shell patterns ---
    info "Checking for dangerous shell patterns..."
    # shellcheck disable=SC2043
    for f in "${shell_files[@]}"; do
        # Skip the lint script itself (contains patterns in comments)
        [ "$(basename "$f")" = "lint.sh" ] && continue
        ran security_dangerous_patterns
        set +e
        dangerous_rc=0
        dangerous_output=$(grep -Eni \
            'chmod[[:space:]]+777|curl[[:space:]].*[[:space:]]*\|[[:space:]]*.*bash|eval[[:space:]]+.*\$' \
            "$f" 2>&1) || dangerous_rc=$?
        set -e
        if [ "${dangerous_rc:-0}" -eq 0 ]; then
            check_failures[security_dangerous_patterns]=$(( check_failures[security_dangerous_patterns] + 1 ))
            echo "  ✗ dangerous patterns: $f"
            echo "    $dangerous_output"
            record_output security_dangerous_patterns "$f" "$dangerous_output"
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

if ! check_enabled gitleaks; then
    skipped gitleaks "disabled by checks.gitleaks.enabled"
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
    # gitleaks falls back to its built-in rules rather than refusing to start. The project's own
    # .gitleaks.toml comes first: existing project configuration wins (see ml_config.sh, layer 3).
    gitleaks_config=""
    for candidate in \
        "$GITLEAKS_NATIVE_CONFIG" \
        "$GITLEAKS_CONFIG_OVERRIDE" \
        /usr/local/bin/.gitleaks.toml \
        "$ML_DIR/.gitleaks.toml"; do
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
    # Project args (checks.gitleaks.args, default empty) are inserted before
    # --verbose; the harness flags around them (detect, --source/--no-git,
    # config args, --log-opts, --verbose --no-color --no-banner) are always
    # kept regardless of args.
    if [ -n "$gitleaks_repo" ]; then
        # History mode. GITLEAKS_DEPTH=all walks every commit; the default limits it to the most
        # recent one, which is what makes this affordable to run after a single edit.
        if [ "$GITLEAKS_DEPTH" = "all" ]; then
            gitleaks_output="$(gitleaks detect --source "$gitleaks_repo" \
                "${gitleaks_config_args[@]}" \
                "${gitleaks_args[@]+"${gitleaks_args[@]}"}" \
                --verbose --no-color --no-banner 2>&1)"
        else
            gitleaks_output="$(gitleaks detect --source "$gitleaks_repo" \
                "${gitleaks_config_args[@]}" \
                --log-opts="-1" \
                "${gitleaks_args[@]+"${gitleaks_args[@]}"}" \
                --verbose --no-color --no-banner 2>&1)"
        fi
    else
        # No repository, so there is no history -- but the working copy can still be scanned.
        # --no-git treats the source as an ordinary path, which may be a single file, and --log-opts
        # is documented as having no effect in this mode, so it is not passed. This is the branch that
        # turns a permanent "skipped" into a real verdict.
        gitleaks_output="$(gitleaks detect --no-git --source "$TARGET_DIR" \
            "${gitleaks_config_args[@]}" \
            "${gitleaks_args[@]+"${gitleaks_args[@]}"}" \
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
        record_output gitleaks "" "$gitleaks_output"
        check_failures[gitleaks]=$gitleaks_findings
        fail "gitleaks"
    else
        echo "$gitleaks_output" | head -10
        # The last non-empty line is gitleaks' own error ("unable to load config", ...).
        gitleaks_error="$(printf '%s\n' "$gitleaks_output" | grep -v '^[[:space:]]*$' | tail -n 1)"
        skipped gitleaks "did not complete (exit $gitleaks_rc): ${gitleaks_error:-no output}"
        warn "gitleaks (did not complete, exit $gitleaks_rc)"
    fi
else
    skipped gitleaks "not installed on PATH"
    warn "gitleaks (not installed, skipping)"
fi

echo ""
# Threshold summary for gitleaks
# shellcheck disable=SC2043
for check in gitleaks; do
    failures=${check_failures[$check]}
    threshold_var="${check}_threshold"
    threshold=${!threshold_var}
    if [ "$failures" -gt 0 ]; then
        if [ "$failures" -gt "$threshold" ]; then
            echo "  ⚠ $check: $failures findings (threshold: $threshold)"
        else
            echo "  ✓ $check: $failures findings (threshold: $threshold)"
        fi
    else
        echo "  ✓ $check: 0 findings (threshold: $threshold)"
    fi
done
