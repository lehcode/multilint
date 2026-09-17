#!/usr/bin/env bash
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PID_DIR="$SCRIPT_DIR/pids"

# Kill HTTP server
if [ -f "$PID_DIR/http.pid" ]; then
    HTTP_PID=$(cat "$PID_DIR/http.pid")
    if kill -0 "$HTTP_PID" 2>/dev/null; then
        echo "Stopping HTTP server (PID: $HTTP_PID)..."
        kill "$HTTP_PID" 2>/dev/null || true
    fi
    rm -f "$PID_DIR/http.pid"
fi

# Kill MCP server
if [ -f "$PID_DIR/mcp.pid" ]; then
    MCP_PID=$(cat "$PID_DIR/mcp.pid")
    if kill -0 "$MCP_PID" 2>/dev/null; then
        echo "Stopping MCP server (PID: $MCP_PID)..."
        kill "$MCP_PID" 2>/dev/null || true
    fi
    rm -f "$PID_DIR/mcp.pid"
fi

# Kill any remaining python processes on ports 8591/8592
echo "Checking for remaining processes..."
for port in 8591 8592; do
    pid=$(sudo lsof -ti:8591 -ti:8592 2>/dev/null || true)
    if [ -n "$pid" ]; then
        echo "Killing process on port $port: $pid"
        sudo kill -9 $pid 2>/dev/null || true
    fi
done

echo "All MultiLint servers stopped."
