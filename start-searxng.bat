@echo off
rem SearXNG dev server on 127.0.0.1:8888 (JSON format enabled).
rem --no-project keeps this venv separate from the local-web-mcp uv project.
rem SEARXNG_SETTINGS_PATH must point at the user settings FILE (folder mode
rem looks for a file named settings.yml, not user-settings.yml).
cd /d %~dp0\searxng
set "SEARXNG_SETTINGS_PATH=%~dp0searx\settings\user-settings.yml"
where uv >nul 2>nul || set "uv=C:\Users\Tai\.local\bin\uv.exe"
set PYTHONIOENCODING=utf-8
%uv% run --no-project --python .venv python -m searx.webapp
