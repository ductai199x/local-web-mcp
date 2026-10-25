@echo off
rem Start the local-web MCP stack (SearXNG + FastMCP server) in two console windows.
start "local-web: searxng" cmd /c "%~dp0start-searxng.bat"
start "local-web: mcp"     cmd /c "%~dp0start-server.bat"
echo Both servers starting. MCP endpoint: http://127.0.0.1:9000/mcp
