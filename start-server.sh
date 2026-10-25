#!/usr/bin/env bash
# FastMCP web server on 127.0.0.1:9000 (SearXNG + Crawl4AI + Scrapling + arXiv/alphaxiv).
# Linux/macOS equivalent of start-server.bat.
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
# This launcher runs the SHARED HTTP server (registered as "local-web-shared").
# The default stdio mode ("local-web") is spawned by Claude Code per session.
export MCP_TRANSPORT=http
exec uv run python server.py
