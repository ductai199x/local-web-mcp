@echo off
rem FastMCP web server on 127.0.0.1:9000 (SearXNG + Crawl4AI + Scrapling + arXiv/alphaxiv)
cd /d %~dp0
rem Always define %uv%: default to the full path, prefer the bare command if
rem uv is on the PATH (an unset var expands to nothing and breaks the call).
set "uv=C:\Users\Tai\.local\bin\uv.exe"
where uv >nul 2>nul && set "uv=uv"
rem Windows console is cp1252 by default; force UTF-8 so tool logs don't crash on non-ASCII
set PYTHONIOENCODING=utf-8
rem This launcher runs the SHARED HTTP server (registered as "local-web-shared").
rem The default stdio mode (registered as "local-web") is spawned by Claude
rem Code itself per session and needs no launcher.
set MCP_TRANSPORT=http
if not exist "%~dp0logs" mkdir "%~dp0logs"
rem Rotate: if the log exceeds 5 MB, keep one prior generation as .1
for %%F in ("%~dp0logs\mcp.log") do if %%~zF GTR 5242880 move /y "%%F" "%~dp0logs\mcp.log.1" >nul 2>&1
%uv% run python server.py >> "%~dp0logs\mcp.log" 2>&1
