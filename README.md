# local-web — local web search / fetch MCP for Claude Code

A thin local MCP server (FastMCP, HTTP transport) over three components:

| Component | Role | Runs |
|---|---|---|
| **SearXNG** | metasearch (Google/Bing/DuckDuckGo/Scholar/…), JSON API | `127.0.0.1:8888` (own venv, `searxng/`) |
| **Crawl4AI** | JS-rendered page → LLM markdown (Playwright Chromium) | inside the MCP server process |
| **PyMuPDF 2.x** | PDF → markdown | inside the MCP server process |
| **arXiv / alphaxiv / GitHub / HuggingFace APIs** | dedicated citation & code lookups | no local component |

Everything is managed with **uv**. The MCP project (this folder) is a uv project
(`pyproject.toml` + `uv.lock`); SearXNG is a git checkout with a uv-managed venv.

## Tools

- `web_search(query, num_results=8, engine="auto", searx_engines="", time_range="")`
  — general web search (SearXNG: Google/Bing/…), automatic DuckDuckGo (`ddgs`)
  fallback. Pin engines with `searx_engines="google"`. `time_range`:
  day|week|month|year.
- `scholar_search(query, num_results=8)` — Google Scholar via SearXNG
  (`engines=google scholar`); no offline fallback (SearXNG must be running).
- `github_search(query, kind="repo"|"issue"|"code", num_results=8)` — GitHub REST
  API. Code search needs `GITHUB_TOKEN` env var; unauthenticated limit is
  10 search req/min.
- `huggingface_search(query, kind="model"|"dataset", num_results=8, pipeline="")`
  — HF API. `query` matches **name/ID substrings** (e.g. `"wan"`), not full
  text; `pipeline` filters by task tag (e.g. `"video-classification"`).
- `libgen_search(query, num_results=8)` — scrapes libgen.li's `index.php?req=`
  results table (best-effort; the site changes and may block). Returns title,
  authors, publisher, year, language, pages, size, extension, md5, plus
  `item_url` (edition page) and `file_url` (file page with download mirrors).
- `arxiv_search(query, max_results=5)` — arXiv API entries (id, title, authors,
  date, abstract, abs URL; PDF = same URL with `.pdf`). Retries up to 3× with
  backoff on transient timeouts. arXiv policy: ≤1 req/3s, one connection at a
  time; the server spaces arxiv.org requests 5s apart and, on a 429 ("Rate
  exceeded"), pauses the whole domain ~5 min for all agents instead of retrying
  into the burst counter.
- `alphaxiv_lookup(arxiv_id, lang="en")` — AI overview of an arXiv paper
  (blurb/problem/solution/key-insights/results, the machine-readable report,
  abstract, bibtex, cited papers). Bare ID (`2402.02085`) or URL. For a short
  blurb this is the fastest path. `fetch_page`/`fetch_pdf` of arXiv paper URLs
  already route through alphaxiv first, so you rarely need to call these
  directly — use them when you want the structured overview rather than raw text.
- `alphaxiv_fulltext(arxiv_id, offset=0, max_chars=20000)` — full extracted
  paper text as markdown, paginated like `fetch_page`. For equations/tables/
  section detail the lookup card doesn't have.
- `fetch_page(url, offset=0, max_chars=20000)` — rendered markdown, **paginated**:
  call again with `offset = offset + max_chars` while `offset < total_chars`.
  Browser concurrency is capped (semaphore, `BROWSER_SLOTS`, default 4).
  **arXiv paper URLs (abs/pdf) are routed through alphaxiv first** — the result's
  `source` field says which answered (`alphaxiv` | `browser` | `arxiv-direct`).
- `fetch_stealth(url, offset=0, max_chars=20000, solve_cloudflare=False)` —
  Scrapling `StealthyFetcher` (stealth-patched headless Chromium: real browser
  UA, canvas noise, WebRTC blocking, spoofed locale/timezone). Use when
  `fetch_page` gets blocked or returns a challenge page.
  `solve_cloudflare=True` solves Cloudflare Turnstile/interstitials (slow,
  1–3 min when it has to). Paginated the same way.
- `fetch_pdf(url, offset=0, max_chars=20000)` — PDF → markdown (PyMuPDF 2.x),
  paginated the same way. For arXiv paper PDFs the default route is alphaxiv's
  extracted text; the direct download happens only when alphaxiv lacks the paper.

## Run

```
start-all.bat          # SearXNG + MCP server, each in its own console window
```

or individually: `start-searxng.bat`, `start-server.bat`.

## Register with Claude Code

```
claude mcp add --transport http local-web http://127.0.0.1:9000/mcp
claude mcp list        # verify (both servers must be running)
```

Then `/mcp` in a session shows the four tools.

## (Re)setup from scratch

```powershell
# MCP project (this folder)
uv add fastmcp crawl4ai httpx pymupdf beautifulsoup4 ddgs "scrapling[fetchers]" markdownify
uv run python -m playwright install chromium    # Crawl4AI + Scrapling browsers
uv run scrapling install                        # marks Scrapling's browser deps installed

# SearXNG (searxng/ — clone of github.com/searxng/searxng at depth 1)
uv venv searxng/.venv --python 3.11
uv pip install --python searxng/.venv -r searxng/requirements.txt
```

SearXNG user settings live in `searxng/searx/settings/user-settings.yml`:
`search.formats` includes `json` (default is html-only → JSON returns 403) and
`server.limiter: false`. The dev runner generates no storage dependency; if you
see botdetection/limiter errors, keep `limiter: false` (there is no valkey/redis).

Note on cloning SearXNG on Windows: the repo contains a file whose name includes
a colon (`utils/templates/etc/httpd/sites-available/searxng.conf:socket`), which
Windows cannot check out. Clone with `--no-checkout` and restore only what is
needed: `git checkout HEAD -- searx requirements.txt`.

## Security

- The MCP server binds to **127.0.0.1 only and has no auth layer** — do not
  expose it (no tailscale/0.0.0.0 binding).
- Fetched web content is **untrusted data** (prompt-injection vector). Keep
  side-effecting tools (shell, file writes) out of any agent that browses.
- Search/fetch hit the public internet normally; be considerate (the semaphore
  and Crawl4AI's cache mitigate re-hammering).

## Troubleshooting

- `web_search` errors mention `searxng: …` → SearXNG not running (start it).
  `engine` in the result shows which backend answered (`searxng` or `ddg`).
- SearXNG JSON 403 → `json` missing from `search.formats` in user-settings.yml.
- `fetch_page` browser errors → `uv run python -m playwright install chromium`.
- Crawl4AI setup check: `uv run python -c "import crawl4ai_doctor"` or
  `uv run crawl4ai-doctor` if the CLI is exposed by the installed version.
- Ports in use: SearXNG 8888, MCP 9000 (`SEARXNG_URL` env var overrides the
  SearXNG address in `server.py`).
- `arxiv_search` returns a 429 error: the arXiv burst counter is active (can
  last ~15 min). Don't retry in a loop — that refreshes the window. Fetch the
  specific papers via `alphaxiv_lookup`/`alphaxiv_fulltext` meanwhile.
  `SKIP_ARXIV=1 uv run python smoke_test.py` skips the arXiv case during a
  throttle window.
