@echo off
rem Idempotent launcher for the local-web stack's persistent parts:
rem   - SearXNG on :8888 (web/scholar search backend for both MCP modes)
rem   - the HTTP shared server on :9000 (MCP_TRANSPORT=http; registered in
rem     Claude Code as "local-web-shared")
rem The stdio registration ("local-web") needs no launching: Claude Code
rem spawns its own server per session.
rem Safe to run at logon (scheduled task), from the SessionStart hook, or
rem manually. The hidden background processes survive this process exiting,
rem and each server writes its log to logs\ (searxng.log, mcp.log).
setlocal
netstat -ano | findstr ":8888" | findstr "LISTENING" >nul 2>&1 || wscript //nologo "%~dp0run-hidden.vbs" "%~dp0start-searxng.bat"
netstat -ano | findstr ":9000" | findstr "LISTENING" >nul 2>&1 || wscript //nologo "%~dp0run-hidden.vbs" "%~dp0start-server.bat"
exit /b 0
