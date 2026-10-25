#!/usr/bin/env bash
# Start the local-web MCP stack (SearXNG + FastMCP server).
# Linux/macOS equivalent of start-all.bat.
set -euo pipefail
cd "$(dirname "$0")"
./start-searxng.sh &
./start-server.sh &
echo "Both servers starting. MCP endpoint: http://127.0.0.1:9000/mcp"
wait
