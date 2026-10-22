# shellcheck shell=bash
# ml_lib.sh - sourced by lint.sh at top level, never run on its own.
# Output and bookkeeping helpers shared by the check sections:
# info/pass/fail/warn, ran/skipped, record_output, check_enabled, ml_files.
# Reads and writes lint.sh globals (TARGET_DIR, OUTPUT_FORMAT, ALL_CHECKS, check_failures,
# check_totals, check_status, check_reason, ...), so it is not wrapped in a function:
# a declare -A inside a function would be local.
# shellcheck disable=SC2034,SC2154

info()  { echo -e "\n\033[1m→ $*\033[0m"; }
pass()  { echo "  ✓ $*"; }
fail()  { echo "  ✗ $*"; }
warn()  { echo "  ~ $*"; }

# Record that $1 actually ran against one more unit of work.
ran()  { check_totals[$1]=$(( check_totals[$1] + 1 )); }
# Record that $1 did not run at all. Idempotent: the guards live inside the
# per-file loops, so this fires once per file when a tool is missing.
# $2 is the human-readable reason; the first one recorded wins.
skipped() {
    check_status[$1]="skipped"
    if [ -n "${2:-}" ] && [ -z "${check_reason[$1]}" ]; then
        check_reason[$1]="$2"
    fi
}

# Raw tool output per failing unit, kept for the JSON emitter, which parses it
# into "findings". One <seq>.<check>.file/.out pair per call so the emitter can
# replay them in order and no tool output can collide with a delimiter. The
# human report above is unchanged: this only keeps a copy.
ML_MSG_DIR="$(mktemp -d 2>/dev/null)" || ML_MSG_DIR=""
if [ -n "$ML_MSG_DIR" ]; then
    trap 'rm -rf "$ML_MSG_DIR"' EXIT
fi
ml_msg_seq=0
# record_output <check> <file> <raw output>; <file> is "" for repo-wide tools.
record_output() {
    local base
    if [ -n "$ML_MSG_DIR" ]; then
        ml_msg_seq=$(( ml_msg_seq + 1 ))
        base="$ML_MSG_DIR/$(printf '%04d' "$ml_msg_seq").$1"
        if printf '%s' "$2" >"$base.file" && printf '%s\n' "$3" >"$base.out"; then
            return 0
        fi
    fi
    # Do not drop findings silently: the check still fails, only its detail is lost.
    if [ "${ml_record_warned:-0}" -eq 0 ]; then
        ml_record_warned=1
        ml_warnings+=("could not record tool output; findings detail may be missing from the JSON report")
        warn "could not record tool output; findings detail may be missing"
    fi
    return 0
}

# True unless "<check>_enabled" has been set to "off" (built-in default, then
# .multilint.json). One helper shared by every one of the sixteen checks so
# there is a single oracle for "is this check switched on".
check_enabled() {
    local v="${1}_enabled"
    [ "${!v}" != "off" ]
}

# ml_files NAME_GLOB... -- EXCLUDE_PATH_GLOB...
# Lists the files under TARGET_DIR whose basename matches one of the NAME_GLOBs.
#
# The exclusions apply to directory walks only. A file named as TARGET_DIR was
# chosen explicitly -- both editor hooks pass exactly the edited file -- so it is
# listed whenever its name matches, wherever it lives. Applying the walk
# exclusions to it made `find` drop `.github/x/ci.yml`, `src/tests/run.sh` or
# `pkg/test/t.py` and the run reported success having linted nothing.
ml_files() {
    local names=() excludes=() in_excludes=0 arg
    for arg in "$@"; do
        if [ "$arg" = "--" ]; then
            in_excludes=1
        elif [ "$in_excludes" -eq 0 ]; then
            names+=("$arg")
        else
            excludes+=("$arg")
        fi
    done

    if [ -f "$TARGET_DIR" ]; then
        local base
        base="$(basename -- "$TARGET_DIR")"
        for arg in "${names[@]}"; do
            # shellcheck disable=SC2053  # unquoted on purpose: a glob match
            if [[ "$base" == $arg ]]; then
                printf '%s\n' "$TARGET_DIR"
                return 0
            fi
        done
        return 0
    fi

    local expr=() filters=()
    for arg in "${names[@]}"; do
        [ ${#expr[@]} -gt 0 ] && expr+=(-o)
        expr+=(-name "$arg")
    done
    for arg in "${excludes[@]}"; do
        filters+=(! -path "$arg")
    done
    find "$TARGET_DIR" -type f \( "${expr[@]}" \) "${filters[@]}" 2>/dev/null
}
