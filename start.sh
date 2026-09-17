#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="$SCRIPT_DIR/.venv"
PID_DIR="$SCRIPT_DIR/pids"

# Activate venv
source "$VENV_DIR/bin/activate"

# Create pids directory
mkdir -p "$PID_DIR"

# Start HTTP API server on port 8591
echo "Starting HTTP API server on port 8591..."
LINT_SERVER_PORT=8591 "$VENV_DIR/bin/python3" "$SCRIPT_DIR/server.py" &
HTTP_PID=$!
echo "$HTTP_PID" > "$PID_DIR/http.pid"
echo "  PID: $HTTP_PID"

# Start MCP server on port 8592
echo "Starting MCP server on port 8592..."
MCP_SERVER_PORT=8592 "$VENV_DIR/bin/python3" "$SCRIPT_DIR/mcp_server.py" &
MCP_PID=$!
echo "$MCP_PID" > "$PID_DIR/mcp.pid"
echo "  PID: $MCP_PID"

echo ""
echo "MultiLint servers running"
echo "  HTTP API:  http://localhost:8591/lint"
echo "  MCP:       http://localhost:8592/mcp"
echo "  Health:    http://localhost:8591/health"
echo ""
echo "PIDs: HTTP=$HTTP_PID, MCP=$MCP_PID"

# Wait for both processes
wait $HTTP_PID $MCP_PID
