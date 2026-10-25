@echo off
rem FastMCP web server on 127.0.0.1:9000 (SearXNG + Crawl4AI + pymupdf4llm + arXiv)
cd /d %~dp0
where uv >nul 2>nul || set "uv=C:\Users\Tai\.local\bin\uv.exe"
rem Windows console is cp1252 by default; force UTF-8 so tool logs don't crash on non-ASCII
set PYTHONIOENCODING=utf-8
%uv% run python server.py
