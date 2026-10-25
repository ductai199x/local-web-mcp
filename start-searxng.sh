#!/usr/bin/env bash
# SearXNG dev server on 127.0.0.1:8888 (JSON format enabled).
# Linux/macOS equivalent of start-searxng.bat.
# --no-project keeps this venv separate from the local-web-mcp uv project.
# SEARXNG_SETTINGS_PATH must point at the user settings FILE (folder mode
# looks for a file named settings.yml, not user-settings.yml).
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
cd "$ROOT/searxng"
export SEARXNG_SETTINGS_PATH="$ROOT/searxng/searx/settings/user-settings.yml"
export PYTHONIOENCODING=utf-8
exec uv run --no-project --python .venv python -m searx.webapp
