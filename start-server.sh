#!/usr/bin/env bash
# FastMCP web server on 127.0.0.1:9000 (SearXNG + Crawl4AI + Scrapling + arXiv/alphaxiv).
# Linux/macOS equivalent of start-server.bat.
set -euo pipefail
cd "$(dirname "$0")"
export PYTHONIOENCODING=utf-8
exec uv run python server.py
