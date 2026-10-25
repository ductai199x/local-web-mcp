# local-web

A local web-search and fetch MCP server for Claude Code. It wraps four backends
into eleven tools: **SearXNG** for metasearch, **Crawl4AI** for
JavaScript-rendered pages, **PyMuPDF** for PDFs, and the **arXiv / alphaxiv /
GitHub / HuggingFace** APIs for citation and code lookups.

## Architecture

| Component | Role | Where it runs |
|---|---|---|
| **SearXNG** | Metasearch (Google, Bing, DuckDuckGo, Scholar, …) over a JSON API | `127.0.0.1:8888`, own venv in `searxng/` |
| **Crawl4AI** | JavaScript-rendered page → LLM-ready markdown (Playwright Chromium) | inside the MCP server process |
| **PyMuPDF 2.x** | PDF → markdown | inside the MCP server process |
| **arXiv / alphaxiv / GitHub / HuggingFace** | Dedicated citation and code lookups | remote APIs, no local component |

The MCP project (this folder) is a **uv** project (`pyproject.toml` +
`uv.lock`). SearXNG is a separate git checkout with its own venv, built from
`searxng/requirements.txt`.

## Tools

- **`web_search`** — `web_search(query, num_results=8, engine="auto", searx_engines="", time_range="")`. General web search via SearXNG (Google, Bing, …) with an automatic DuckDuckGo (`ddgs`) fallback. Pin engines with `searx_engines="google"`; `time_range` is `day|week|month|year`.
- **`scholar_search`** — `scholar_search(query, num_results=8)`. Google Scholar via SearXNG (`engines=google scholar`); no offline fallback, so SearXNG must be running.
- **`github_search`** — `github_search(query, kind="repo"|"issue"|"code", num_results=8)`. GitHub REST API. Code search needs the `GITHUB_TOKEN` env var; unauthenticated the limit is 10 search requests/minute.
- **`huggingface_search`** — `huggingface_search(query, kind="model"|"dataset", num_results=8, pipeline="")`. HuggingFace API. `query` matches name/ID substrings (e.g. `"wan"`), not full text; `pipeline` filters by task tag (e.g. `"video-classification"`).
- **`libgen_search`** — `libgen_search(query, num_results=8)`. Scrapes the `index.php?req=` results table on libgen.li (best-effort; the site changes and may block). Returns title, authors, publisher, year, language, pages, size, extension, and md5, plus `item_url` (edition page) and `file_url` (file page with download mirrors).
- **`arxiv_search`** — `arxiv_search(query, max_results=5)`. arXiv API entries (id, title, authors, date, abstract, abs URL; the PDF is the same URL with `.pdf`). Retries up to 3× with backoff on transient timeouts. arXiv allows ≤1 request/3s on one connection; the server spaces arxiv.org requests 5 s apart and, on a 429, pauses the whole domain ~5 min for all agents instead of retrying into the burst counter.
- **`alphaxiv_lookup`** — `alphaxiv_lookup(arxiv_id, lang="en")`. AI-generated overview of an arXiv paper (blurb, problem, solution, key insights, results, the machine-readable report, abstract, bibtex, cited papers). Accepts a bare ID (`2402.02085`) or URL. `fetch_page`/`fetch_pdf` of arXiv URLs already route through alphaxiv first, so call this directly when you want the structured overview rather than raw text.
- **`alphaxiv_fulltext`** — `alphaxiv_fulltext(arxiv_id, offset=0, max_chars=20000)`. Full extracted paper text as markdown, paginated like `fetch_page`; use it for equations, tables, and section detail the lookup card lacks.
- **`fetch_page`** — `fetch_page(url, offset=0, max_chars=20000)`. Rendered markdown, **paginated**: call again with `offset += max_chars` while `offset < total_chars`. Browser concurrency is capped by a semaphore (`BROWSER_SLOTS`, default 4). arXiv paper URLs (abs/pdf) route through alphaxiv first; the result's `source` field says which answered (`alphaxiv` | `browser` | `arxiv-direct`).
- **`fetch_stealth`** — `fetch_stealth(url, offset=0, max_chars=20000, solve_cloudflare=False)`. Scrapling `StealthyFetcher` (stealth-patched headless Chromium: real browser UA, canvas noise, WebRTC blocking, spoofed locale/timezone). Use when `fetch_page` is blocked or returns a challenge page. `solve_cloudflare=True` solves Cloudflare Turnstile/interstitials (slow, 1–3 min when it has to). Paginated the same way.
- **`fetch_pdf`** — `fetch_pdf(url, offset=0, max_chars=20000)`. PDF → markdown via PyMuPDF 2.x, paginated the same way. For arXiv paper PDFs the default route is alphaxiv's extracted text; the direct download happens only when alphaxiv lacks the paper.

## Running

Two registration modes expose the same eleven tools:

| Name | Transport | Lifecycle |
|---|---|---|
| `local-web` | **stdio** (default) | Claude Code spawns `uv run python server.py` per session. Nothing to start, nothing to keep alive. |
| `local-web-shared` | **HTTP** `:9000` | One long-lived server shared by all sessions (shared rate-limit gate + browser pool). |

Only the shared server and SearXNG need to stay running, and both come up
automatically:

- **At logon** — the scheduled task `local-web-mcp-stack` runs `autostart.vbs`
  → `check-and-start.bat`.
- **At every session start** — a SessionStart hook re-runs `check-and-start.bat`.
- **Idempotent and headless** — `check-and-start.bat` starts only whatever port
  is not already listening, and launches each server as a hidden background
  process (no console window) via `run-hidden.vbs`.

**Logs.** Each server writes to a file with 5 MB size-based rotation (one prior
generation kept as `.1`):

- `logs/searxng.log` — SearXNG
- `logs/mcp.log` — the shared MCP server

For debugging you can also launch with visible windows: `start-all.bat` /
`./start-all.sh`, or the individual `start-searxng.*` / `start-server.*`
(the server launchers set `MCP_TRANSPORT=http`).

## Registration

Register both at user scope:

```
claude mcp add -s user local-web -- <path to uv> run --directory <this folder> python server.py
claude mcp add -s user local-web-shared --transport http http://127.0.0.1:9000/mcp
claude mcp list        # both should show Connected
```

With both registered, a session sees the eleven tools twice (once per server
prefix). Use `local-web` (stdio) for everyday work; the shared server is for
running several sessions or agents concurrently against one browser pool.

## Setup from scratch

```powershell
# MCP project (this folder)
uv add fastmcp crawl4ai httpx pymupdf beautifulsoup4 ddgs "scrapling[fetchers]" markdownify
uv run python -m playwright install chromium    # Crawl4AI + Scrapling browsers
uv run scrapling install                        # marks Scrapling's browser deps as installed

# SearXNG (searxng/ — a depth-1 clone of github.com/searxng/searxng)
uv venv searxng/.venv --python 3.11
uv pip install --python searxng/.venv -r searxng/requirements.txt
```

**SearXNG settings** live in `searxng/searx/settings/user-settings.yml`:
`search.formats` must include `json` (the default is html-only, which returns
403 for JSON), and `server.limiter` is `false` (the dev runner has no
valkey/redis, so a limiter would fail).

**Cloning SearXNG on Windows** — the repo contains a file whose name includes a
colon (`…/sites-available/searxng.conf:socket`), which Windows cannot check
out. Clone with `--no-checkout` and restore only what is needed:
`git checkout HEAD -- searx requirements.txt`.

## Tests

```
uv run pytest                            # unit tests: pure helpers + domain gate (fake clock, no network, ~1 s)
uv run python smoke_test.py              # live end-to-end, all 11 tools
SKIP_ARXIV=1 uv run python smoke_test.py # skip arXiv while it is rate-limiting
```

Unit tests run in CI (`.github/workflows/ci.yml`). The smoke test hits the live
HTTP server, so it is local-only — start the stack with `check-and-start.bat`
first.

## Security

- The MCP server binds to **127.0.0.1 only, with no authentication** — do not
  expose it (no `0.0.0.0` / Tailscale binding).
- Fetched web content is **untrusted data** (a prompt-injection vector). Keep
  side-effecting tools (shell, file writes) out of any agent that browses.
- Search and fetch hit the public internet normally; the browser semaphore and
  Crawl4AI's cache mitigate re-hammering.

## Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `web_search` errors with `searxng: …` | SearXNG is not running — start it. The result's `engine` field shows which backend answered (`searxng` or `ddg`). |
| SearXNG JSON returns 403 | `json` is missing from `search.formats` in `user-settings.yml`. |
| `fetch_page` browser errors | `uv run python -m playwright install chromium`. |
| Crawl4AI setup check | `uv run python -c "import crawl4ai_doctor"` (or `uv run crawl4ai-doctor` if the CLI is exposed). |
| Ports in use | SearXNG `8888`, MCP `9000`. `SEARXNG_URL` overrides the SearXNG address in `server.py`. |
| `arxiv_search` returns 429 | The arXiv burst counter is active (can last ~15 min). Do not retry in a loop — that refreshes the window. Fetch specific papers via `alphaxiv_lookup` / `alphaxiv_fulltext` meanwhile; `SKIP_ARXIV=1` skips the arXiv smoke-test case. |
