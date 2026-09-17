#!/usr/bin/env bash
set -euo pipefail

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
