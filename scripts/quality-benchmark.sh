#!/usr/bin/env bash
# Code Quality Benchmark: dead code (vulture), complexity (xenon), import contracts (import-linter).
# Fails on findings (exit 1) and on tools that could not run (missing tool, crash); reports them apart.
set -uo pipefail

cd "$(git rev-parse --show-toplevel)" || {
    echo "quality-benchmark: not inside a git repository" >&2
    exit 1
}

if [[ -t 1 ]]; then
    RED=$'\033[0;31m'
    GREEN=$'\033[0;32m'
    BOLD=$'\033[1m'
    RESET=$'\033[0m'
else
    RED='' GREEN='' BOLD='' RESET=''
fi

# Tracked Python files only; tests/fixtures and tests/test_files are intentionally broken lint inputs.
FILES=()
while IFS= read -r -d '' f; do
    case "$f" in
    tests/fixtures/* | tests/test_files/*) ;;
    *) FILES+=("$f") ;;
    esac
done < <(git ls-files -z '*.py')
if [[ ${#FILES[@]} -eq 0 ]]; then
    echo "quality-benchmark: no tracked Python files found" >&2
    exit 1
fi

# Per-stage results, indexed by stage: a count, or "ERR" when the tool did not run properly.
NAMES=("Dead code (vulture)" "Complexity (xenon)" "Import contracts (import-linter)")
THRESHOLDS=("0 findings" "0 above B/A/A" "0 broken")
RESULTS=()
FAILED=0

indent() {
    local line
    while IFS= read -r line; do
        echo "    $line"
    done <<<"$1"
}

# stage <n> <tool> <count-mode> <args...>: run the tool, record its finding count or ERR.
stage() {
    local idx=$1 tool=$2 mode=$3 out rc count
    shift 3
    printf '%s[%d/3]%s %s\n' "$BOLD" "$((idx + 1))" "$RESET" "${NAMES[idx]}"
    if ! command -v "$tool" >/dev/null 2>&1; then
        echo "  ${RED}✘${RESET} required tool '$tool' not found on PATH"
        RESULTS[idx]=ERR
        return
    fi
    out=$("$tool" "$@" 2>&1)
    rc=$?
    case "$mode" in
    lines) count=$(grep -c . <<<"$out") ;;
    xenon) count=$(grep -c '^ERROR:xenon:' <<<"$out") ;;
    contracts)
        count=$(sed -n 's/^Contracts: [0-9]* kept, \([0-9]*\) broken\.$/\1/p' <<<"$out")
        [[ -z "$count" ]] && count=ERR
        ;;
    esac
    # Output with a non-zero status but nothing countable means the tool itself failed.
    if [[ "$count" == ERR ]] || { [[ $rc -ne 0 && "$count" -eq 0 ]]; }; then
        echo "  ${RED}✘${RESET} $tool failed to run (exit $rc)"
        indent "$out"
        RESULTS[idx]=ERR
    elif [[ "$count" -gt 0 ]]; then
        echo "  ${RED}✘${RESET} $count finding(s)"
        indent "$out"
        RESULTS[idx]=$count
    else
        echo "  ${GREEN}✔${RESET} clean"
        RESULTS[idx]=0
    fi
}

echo "${BOLD}=== Code Quality Benchmark ===${RESET}"
echo "Files: ${#FILES[@]} tracked Python files"
echo

stage 0 vulture lines --min-confidence 80 "${FILES[@]}"
stage 1 xenon xenon --max-absolute B --max-modules A --max-average A "${FILES[@]}"
stage 2 lint-imports contracts --no-cache --no-logo

echo
echo "${BOLD}SUMMARY${RESET}"
printf '%-34s | %-8s | %s\n' Metric Found Threshold
for i in 0 1 2; do
    printf '%-34s | %-8s | %s\n' "${NAMES[i]}" "${RESULTS[i]}" "${THRESHOLDS[i]}"
    [[ "${RESULTS[i]}" == 0 ]] || FAILED=1
done
echo
if [[ $FAILED -eq 0 ]]; then
    echo "${GREEN}${BOLD}RESULT: PASS${RESET}"
else
    note=""
    [[ " ${RESULTS[*]} " == *" ERR "* ]] && note=" (ERR = tool failed to run)"
    echo "${RED}${BOLD}RESULT: FAIL${RESET}${note}"
    exit 1
fi
