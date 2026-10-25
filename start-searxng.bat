@echo off
rem SearXNG dev server on 127.0.0.1:8888 (JSON format enabled).
rem --no-project keeps this venv separate from the local-web-mcp uv project.
rem SEARXNG_SETTINGS_PATH must point at the user settings FILE (folder mode
rem looks for a file named settings.yml, not user-settings.yml).
cd /d %~dp0\searxng
set "SEARXNG_SETTINGS_PATH=%~dp0searxng\searx\settings\user-settings.yml"
rem Always define %uv% (see start-server.bat); unset vars expand to nothing.
set "uv=C:\Users\Tai\.local\bin\uv.exe"
where uv >nul 2>nul && set "uv=uv"
set PYTHONIOENCODING=utf-8
if not exist "%~dp0logs" mkdir "%~dp0logs"
rem Rotate: if the log exceeds 5 MB, keep one prior generation as .1
for %%F in ("%~dp0logs\searxng.log") do if %%~zF GTR 5242880 move /y "%%F" "%~dp0logs\searxng.log.1" >nul 2>&1
%uv% run --no-project --python .venv python -m searx.webapp >> "%~dp0logs\searxng.log" 2>&1
