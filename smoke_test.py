"""Smoke test for the local-web MCP server (run: uv run python smoke_test.py).
Exercises each tool once through the HTTP endpoint. Both servers must be running
(start-all.bat)."""
import asyncio
import os
import sys

# Windows consoles default to cp1252; search results often contain UTF-8 that
# would crash a bare print().
try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

from mcp import ClientSession
try:
    from mcp.client.streamable_http import streamable_http_client as streamablehttp_client
except ImportError:  # older mcp versions
    from mcp.client.streamable_http import streamablehttp_client

URL = "http://127.0.0.1:9000/mcp"

CASES = [
    ("web_search", {"query": "CVPR 2027 author guidelines"}),
    ("scholar_search", {"query": "generated video detection"}),
    ("github_search", {"query": "video forensics", "kind": "repo"}),
    ("huggingface_search", {"query": "detection", "kind": "model"}),
    ("libgen_search", {"query": "deep learning"}),
    ("arxiv_search", {"query": "AI generated video detection"}),
    ("fetch_pdf", {"url": "https://arxiv.org/pdf/2402.02085", "max_chars": 200}),
    ("fetch_page", {"url": "https://example.com", "max_chars": 200}),
    # solve_cloudflare=True is verified manually (nopecha.com demo, ~2.5 min) but
    # too slow for the routine smoke test.
    ("fetch_stealth", {"url": "https://example.com", "max_chars": 200}),
    ("alphaxiv_lookup", {"arxiv_id": "2402.02085"}),
    ("alphaxiv_fulltext", {"arxiv_id": "2402.02085", "max_chars": 200}),
]

# SKIP_ARXIV=1: don't touch the arXiv API (it rate-limits this IP aggressively;
# each request can refresh the throttle window).
if os.getenv("SKIP_ARXIV"):
    CASES = [c for c in CASES if c[0] != "arxiv_search"]


async def main():
    async with streamablehttp_client(URL) as (r, w):
        async with ClientSession(r, w) as s:
            await s.initialize()
            tools = await s.list_tools()
            print("tools:", ", ".join(t.name for t in tools.tools))
            for name, args in CASES:
                try:
                    res = await s.call_tool(name, args)
                    txt = res.content[0].text
                    head = " ".join(txt.split())[:200]
                    print(f"--- {name}: OK ({len(txt)} chars)\n    {head}")
                except Exception as e:
                    print(f"--- {name}: FAILED ({type(e).__name__}: {e})")


asyncio.run(main())
