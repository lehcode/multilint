#!/usr/bin/env bash
# Runs inside the target environment (host for V2, container for V1): starts the
# mock provider, makes OpenCode write a broken script, then checks the result.
# Inputs (env): OC_BIN OC_GEN RUN_DIR MOCK_PROJECT AGENT CHECK_ARGS
# and optionally EXPECT_SYSTEM.
set -uo pipefail

here="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export MOCK_LOG="$RUN_DIR/requests.jsonl"
: >"$MOCK_LOG"

node "$here/mock-provider.mjs" serve >"$RUN_DIR/mock.log" 2>&1 &
mock_pid=$!
trap 'kill "$mock_pid" 2> /dev/null' EXIT
for _ in $(seq 1 50); do
    grep -q "mock provider on" "$RUN_DIR/mock.log" && break
    sleep 0.1
done

prompt="Create the shell script for the test"
args=(run)
[[ "$OC_GEN" == v2 ]] && args+=(--standalone --auto)
args+=(--agent "$AGENT" --model mock/mock-model "$prompt")

cd "$MOCK_PROJECT" || exit 2
timeout 300 "$OC_BIN" "${args[@]}" >"$RUN_DIR/opencode.log" 2>&1
echo "opencode exit status: $?" >>"$RUN_DIR/opencode.log"

# CHECK_ARGS is a word list by design (options and their values)
# shellcheck disable=SC2086
node "$here/mock-provider.mjs" check $CHECK_ARGS \
    ${EXPECT_SYSTEM:+--expect-system "$EXPECT_SYSTEM"}
