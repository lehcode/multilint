#!/usr/bin/env bash
set -euo pipefail

# Reject an unusable workspace before starting anything.
#
# docker-compose.yml mounts ${MULTILINT_WORKSPACE:-$PWD} here using compose's SHORT volume syntax,
# and that syntax CREATES a missing bind source instead of refusing it. Measured: a non-existent
# MULTILINT_WORKSPACE leaves a root-owned empty directory on the host and an empty mount in here,
# the container starts, and every lint then passes having read nothing. `--mount type=bind` refuses
# the same path outright; the short syntax does not.
#
# A linter that reports success on an empty directory is worse than one that fails, so fail here
# with the instruction rather than let a typo look like a clean tree.
#
# Only continuous mode is guarded. One-shot mode is handed an explicit target as "$@", and the
# plugin hooks bypass this script entirely via --entrypoint bash.
workspace="${MULTILINT_CONTAINER_TARGET:-/workspace}"
if [ "${CONTINUOUS_LINT:-0}" = "1" ] && { [ ! -d "$workspace" ] || [ -z "$(ls -A "$workspace" 2>/dev/null)" ]; }; then
    if [ -d "$workspace" ]; then
        # Two causes reach here and the container cannot tell them apart: nothing was mounted (the
        # image ships an empty /workspace), or MULTILINT_WORKSPACE named a host path that did not
        # exist and docker created it empty. Name both rather than guess.
        echo "multilint: workspace '$workspace' is empty — nothing to lint." >&2
        echo "multilint: either no directory was mounted there, or MULTILINT_WORKSPACE named a host" >&2
        echo "multilint: path that does not exist and docker created it empty instead of refusing." >&2
    else
        echo "multilint: workspace '$workspace' does not exist in the container." >&2
        echo "multilint: MULTILINT_CONTAINER_TARGET names a path that was never mounted." >&2
    fi
    echo "multilint:" >&2
    echo "multilint: set MULTILINT_WORKSPACE to an existing project directory and recreate:" >&2
    echo "multilint:   MULTILINT_WORKSPACE=/path/to/project docker compose up -d --force-recreate" >&2
    echo "multilint:" >&2
    echo "multilint: it defaults to the directory you run compose from, so 'cd' there and omit it." >&2
    exit 1
fi

# Continuous mode: run HTTP server and MCP server
if [ "${CONTINUOUS_LINT:-0}" = "1" ]; then
    echo "Starting MultiLint HTTP server on port ${LINT_SERVER_PORT:-8591}..."
    python3 /usr/local/bin/server.py &
    HTTP_PID=$!

    echo "Starting MultiLint MCP server on port ${MCP_SERVER_PORT:-8592}..."
    MCP_SERVER_PORT="${MCP_SERVER_PORT:-8592}" python3 /usr/local/bin/mcp_server.py &
    MCP_PID=$!

    # Handle graceful shutdown
    cleanup() {
        echo "Shutting down MultiLint servers..."
        kill "$HTTP_PID" 2>/dev/null || true
        kill "$MCP_PID" 2>/dev/null || true
        wait "$HTTP_PID" 2>/dev/null || true
        wait "$MCP_PID" 2>/dev/null || true
        echo "All servers stopped."
    }
    trap cleanup EXIT INT TERM

    echo "MultiLint servers running"
    echo "  HTTP API:  http://localhost:${LINT_SERVER_PORT:-8591}/lint"
    echo "  MCP:       http://localhost:${MCP_SERVER_PORT:-8592}/mcp"
    echo "  Health:    http://localhost:${LINT_SERVER_PORT:-8591}/health"

    wait "$HTTP_PID"
else
    # One-shot mode: run lint.sh directly (unchanged behavior)
    exec bash /usr/local/bin/lint.sh "$@"
fi
