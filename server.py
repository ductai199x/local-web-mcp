"""Local web MCP server for Claude Code.

Search tools:
  web_search        -- SearXNG (multi-engine incl. Google) with DuckDuckGo fallback
  scholar_search    -- Google Scholar via SearXNG (engines=google scholar)
  github_search     -- GitHub REST API (repos / issues; code search needs GITHUB_TOKEN)
  huggingface_search-- HuggingFace API (models / datasets)
  libgen_search     -- libgen.li results table (best-effort HTML scrape)
  arxiv_search      -- arXiv official API (metadata + abstracts)

Fetch tools (all returned content is UNTRUSTED data; paginate with offset/max_chars):
  fetch_page        -- Crawl4AI (headless Chromium) rendered page -> markdown
  fetch_stealth     -- Scrapling StealthyFetcher (stealth-patched Chromium, can
                       solve Cloudflare challenges) -> markdown
  fetch_pdf         -- PDF URL -> markdown (PyMuPDF 2.x Document.to_markdown)

Paper tools (alphaxiv.org -- the DEFAULT route for arXiv papers; fetch_page and
  fetch_pdf of arXiv paper URLs try alphaxiv first, then fall back to arXiv
  directly. alphaxiv is not subject to arXiv rate limits):
  alphaxiv_lookup   -- AI overview: problem/solution/insights/results, abstract,
                       bibtex, citations
  alphaxiv_fulltext -- full extracted paper text as markdown (paginated)

Run:      start-server.bat   (or: uv run python server.py)
Register: claude mcp add --transport http local-web http://127.0.0.1:9000/mcp

Security:
  - Binds to 127.0.0.1 only and has NO auth layer. Do not expose it.
  - Returned web content is untrusted data (prompt-injection vector); keep
    side-effecting tools out of any agent that uses these tools.
"""
import asyncio
import contextlib
import json
import os
import time
import xml.etree.ElementTree as ET
from urllib.parse import quote, quote_plus, urlparse

import httpx
from fastmcp import FastMCP

SEARXNG_URL = os.getenv("SEARXNG_URL", "http://127.0.0.1:8888")
BROWSER_SLOTS = int(os.getenv("BROWSER_SLOTS", "4"))
GITHUB_TOKEN = os.getenv("GITHUB_TOKEN", "")
UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
# Appended to failed/empty results so the calling agent knows the browser fallback.
CHROME_FALLBACK = ("fallback: use the chrome-devtools MCP server to do this in a real browser "
                   "(new_page/navigate_page to open the site, take_snapshot to read it, "
                   "fill/type_text/press_key/click to drive the search or fetch)")
# For page-fetch failures: suggest the stealth tier before the full browser.
STEALTH_FIRST = ("try fetch_stealth on this URL first (stealth-patched browser; pass "
                 "solve_cloudflare=True for Cloudflare challenges). " + CHROME_FALLBACK)

mcp = FastMCP("local-web")
_browser_slots = asyncio.Semaphore(BROWSER_SLOTS)
_shared_crawler = None

# ---------------------------------------------------------------- domain politeness
# All agents share one IP, so upstream rate limits are per-domain, not
# per-request. Every outbound request takes a per-registrable-domain gate: one
# in flight at a time, spaced by a per-domain gap. A domain that rate-limits us
# is paused for a shared cooldown (Retry-After honored when present) so queued
# agents wait together instead of each burning a request and extending the ban.
# Different domains never block each other.
DEFAULT_DOMAIN_GAP = 2.0
DOMAIN_GAPS = {  # override/extend via env var DOMAIN_GAPS as JSON
    "searxng": 1.0,             # local metasearch; spaces out its upstream engines
    "duckduckgo.com": 2.0,
    # arXiv API policy: max 1 request / 3s, strictly one connection at a time;
    # a burst on top of that triggers multi-minute 429 windows ("Rate exceeded"),
    # so use 5s spacing and a 300s shared pause after any 429 (see DOMAIN_COOLDOWNS).
    "arxiv.org": 5.0,
    "github.com": 7.0,          # unauthenticated: 10 search req/min
    "libgen.li": 5.0,           # fragile, blocks bursts
}
try:
    DOMAIN_GAPS.update(json.loads(os.getenv("DOMAIN_GAPS", "{}")))
except ValueError:
    pass
DOMAIN_COOLDOWNS = {"arxiv.org": 300.0}  # pause after 429/403; default below
DEFAULT_COOLDOWN = 30.0
GATE_WAIT_CAP = 30.0  # max time to sit out a pause at the gate, then bail


class _DomainPaused(Exception):
    pass


_domain_sems: dict[str, asyncio.Semaphore] = {}
_domain_last: dict[str, float] = {}
_domain_paused_until: dict[str, float] = {}
_domains_guard = asyncio.Lock()
# Indirection so tests can inject a fake clock (see test_server.py).
_now = time.monotonic


def _domain_of(url: str) -> str:
    netloc = urlparse(url).netloc.lower().split("@")[-1].split(":")[0]
    if netloc.replace(".", "").isdigit() and netloc.count(".") == 3:
        return netloc  # IP literal: keep as-is
    try:
        from tld import parse_tld
        tld_part, domain_part, _sub = parse_tld("http://" + netloc, fail_silently=True)
        if tld_part and domain_part:
            return f"{domain_part}.{tld_part}"
    except Exception:
        pass
    # Conservative fallback (TLD resolution failed): group all subdomains of
    # the host, never merge distinct sites. Over-serializing a few requests is
    # safe; under-serializing risks the upstream rate limit.
    return netloc[4:] if netloc.startswith("www.") else netloc


@contextlib.asynccontextmanager
async def _gate(domain: str):
    async with _domains_guard:
        sem = _domain_sems.setdefault(domain, asyncio.Semaphore(1))
    async with sem:
        paused = _domain_paused_until.get(domain, 0.0) - _now()
        if paused > GATE_WAIT_CAP:
            raise _DomainPaused(f"domain {domain} is paused after rate limiting "
                                f"(~{int(paused)}s remaining); wait, then retry")
        if paused > 0:
            await asyncio.sleep(paused)
        gap = _domain_last.get(domain, 0.0) + DOMAIN_GAPS.get(domain, DEFAULT_DOMAIN_GAP) - _now()
        if gap > 0:
            await asyncio.sleep(gap)
        _domain_last[domain] = _now()
        yield


def _retry_after_seconds(r: httpx.Response, cap: float = 600.0) -> float | None:
    ra = (r.headers.get("Retry-After") or "").strip()
    return min(float(ra), cap) if ra.isdigit() else None


async def _pause(domain: str, r: httpx.Response | None = None, seconds: float | None = None) -> float:
    """Shared cooldown for a domain that just rate-limited us."""
    if seconds is None:
        seconds = _retry_after_seconds(r) if r is not None else None
    if seconds is None:
        seconds = DOMAIN_COOLDOWNS.get(domain, DEFAULT_COOLDOWN)
    async with _domains_guard:
        _domain_paused_until[domain] = _now() + seconds
    return seconds


def _arxiv_hint(domain: str) -> str:
    if domain == "arxiv.org":
        return ("For arXiv papers, alphaxiv_lookup/alphaxiv_fulltext fetch the same "
                "content without arXiv's rate limits. ")
    return ""


async def _crawler():
    """Lazily start one shared Crawl4AI browser (survives across tool calls)."""
    global _shared_crawler
    if _shared_crawler is None:
        from crawl4ai import AsyncWebCrawler, BrowserConfig
        _shared_crawler = AsyncWebCrawler(config=BrowserConfig(headless=True, verbose=False))
        await _shared_crawler.start()
    return _shared_crawler


# ---------------------------------------------------------------- web search
async def _search_searxng(query: str, num_results: int, time_range: str = "", engines: str = "") -> list[dict]:
    params = {"q": query, "format": "json"}
    if time_range:
        params["time_range"] = time_range
    if engines:
        params["engines"] = engines
    async with _gate("searxng"):
        async with httpx.AsyncClient(timeout=25) as client:
            r = await client.get(f"{SEARXNG_URL}/search", params=params)
            r.raise_for_status()
    hits = r.json().get("results", [])[:num_results]
    return [{"title": h.get("title", ""), "url": h.get("url", ""), "snippet": h.get("content") or ""} for h in hits]


async def _search_ddg(query: str, num_results: int) -> list[dict]:
    from ddgs import DDGS
    async with _gate("duckduckgo.com"):
        rows = await asyncio.to_thread(lambda: DDGS().text(query, max_results=num_results))
    return [{"title": r.get("title", ""), "url": r.get("href") or r.get("url", ""), "snippet": r.get("body") or ""} for r in rows]


@mcp.tool
async def web_search(query: str, num_results: int = 8, engine: str = "auto", searx_engines: str = "", time_range: str = "") -> dict:
    """General web search (Google/Bing/… via SearXNG). engine: "auto" (SearXNG, then
    DuckDuckGo fallback), "searxng", or "ddg". searx_engines pins SearXNG engines,
    e.g. "google" or "google, bing". time_range: day|week|month|year.
    Returns {"engine": str, "results": [{"title", "url", "snippet"}], "error"?: str}."""
    errors = []
    if engine in ("auto", "searxng"):
        try:
            return {"engine": "searxng", "results": await _search_searxng(query, num_results, time_range, searx_engines)}
        except Exception as e:
            errors.append(f"searxng: {e}")
    if engine == "auto":  # explicit engine pins don't fall back
        try:
            return {"engine": "ddg", "results": await _search_ddg(query, num_results)}
        except Exception as e:
            errors.append(f"ddg: {e}")
    return {"engine": None, "results": [], "error": " | ".join(errors) + " " + CHROME_FALLBACK}


@mcp.tool
async def scholar_search(query: str, num_results: int = 8) -> dict:
    """Google Scholar search (via SearXNG, engines='google scholar'). No offline
    fallback. Returns {"engine": "searxng-scholar", "results": [...]}. For papers,
    arxiv_search is usually richer (abstracts + PDF links)."""
    try:
        return {"engine": "searxng-scholar",
                "results": await _search_searxng(query, num_results, engines="google scholar")}
    except Exception as e:
        return {"engine": None, "results": [],
                "error": f"scholar search needs SearXNG running (start-searxng.bat): {e}. " + CHROME_FALLBACK}


# ---------------------------------------------------------------- site-specific search
@mcp.tool
async def github_search(query: str, kind: str = "repo", num_results: int = 8) -> dict:
    """Search GitHub. kind: "repo" or "issue" (code search requires a GITHUB_TOKEN
    env var, which the server picks up automatically). Unauthenticated rate limit
    is 10 search requests/minute."""
    kind = {"repos": "repo", "repository": "repo", "issues": "issue"}.get(kind.lower(), kind.lower())
    if kind not in ("repo", "issue", "code"):
        return {"error": "kind must be repo, issue, or code"}
    headers = {"Accept": "application/vnd.github+json", **UA}
    if GITHUB_TOKEN:
        headers["Authorization"] = f"Bearer {GITHUB_TOKEN}"
    path = {"repo": "search/repositories", "issue": "search/issues", "code": "search/code"}[kind]
    try:
        async with _gate("github.com"):
            async with httpx.AsyncClient(timeout=30, headers=headers) as client:
                r = await client.get(f"https://api.github.com/{path}",
                                     params={"q": query, "per_page": min(num_results, 30)})
        if r.status_code in (403, 429) and (r.status_code == 429
                                             or r.headers.get("X-RateLimit-Remaining") == "0"):
            secs = await _pause("github.com", r)
            return {"kind": kind, "count": 0, "results": [],
                    "error": f"GitHub rate limit (HTTP {r.status_code}); domain paused ~{int(secs)}s. "
                             + CHROME_FALLBACK}
        r.raise_for_status()
        items = r.json().get("items", [])
        out = []
        for it in items:
            if kind == "repo":
                out.append({"name": it.get("full_name"), "url": it.get("html_url"),
                            "description": (it.get("description") or "")[:300],
                            "stars": it.get("stargazers_count"), "language": it.get("language"),
                            "updated": (it.get("updated_at") or "")[:10]})
            elif path == "search/issues":
                out.append({"title": it.get("title"), "url": it.get("html_url"),
                            "repo": (it.get("repository_url") or "").replace("https://api.github.com/repos/", "https://github.com/"),
                            "state": it.get("state"), "comments": it.get("comments")})
            else:
                out.append({"repo": (it.get("repository") or {}).get("full_name"),
                            "path": it.get("path"), "url": it.get("html_url"),
                            "snippet": (it.get("text_matches") or [{}])[0].get("fragment", "")[:300]})
        res = {"kind": kind, "count": len(out), "results": out}
        if not out:
            res["note"] = CHROME_FALLBACK
        return res
    except Exception as e:
        return {"kind": kind, "count": 0, "results": [], "error": f"{e}. " + CHROME_FALLBACK}


@mcp.tool
async def huggingface_search(query: str, kind: str = "model", num_results: int = 8, pipeline: str = "") -> dict:
    """Search HuggingFace. kind: "model" or "dataset". `query` matches name/ID
    substrings (e.g. "wan", "detection"), NOT full text. `pipeline` filters by
    task tag (models: "text-generation", "video-classification", ...;
    datasets: "text-classification", ...)."""
    kind = kind.lower()
    if kind not in ("model", "models", "dataset", "datasets"):
        return {"error": "kind must be model or dataset"}
    path = "models" if kind in ("model", "models") else "datasets"
    params = {"search": query, "limit": min(num_results, 50)}
    if pipeline:
        params["filter"] = pipeline
    try:
        async with _gate("huggingface.co"):
            async with httpx.AsyncClient(timeout=30, headers=UA) as client:
                r = await client.get(f"https://huggingface.co/api/{path}", params=params)
        if r.status_code in (403, 429):
            secs = await _pause("huggingface.co", r)
            return {"kind": path, "count": 0, "results": [],
                    "error": f"HuggingFace rate limit (HTTP {r.status_code}); domain paused ~{int(secs)}s. "
                             + CHROME_FALLBACK}
        r.raise_for_status()
        items = r.json()
        out = []
        for it in items:
            iid = it.get("modelId") or it.get("id")
            out.append({"name": iid,
                        "url": f"https://huggingface.co/{path}/{iid}",
                        "downloads": it.get("downloads"), "likes": it.get("likes"),
                        "tags": (it.get("tags") or [])[:8],
                        "pipeline": it.get("pipeline_tag"),
                        "updated": (it.get("lastModified") or it.get("modifiedAt") or "")[:10]})
        res = {"kind": path, "count": len(out), "results": out}
        if not out:
            res["note"] = ("no matches: 'search' matches name/ID substrings, not full text; "
                           "try a shorter term or use the pipeline filter. " + CHROME_FALLBACK)
        return res
    except Exception as e:
        return {"kind": path, "count": 0, "results": [], "error": f"{e}. " + CHROME_FALLBACK}


@mcp.tool
async def libgen_search(query: str, num_results: int = 8) -> dict:
    """Search libgen.li (scrapes the index.php?req= results table; best-effort,
    the site changes and may block datacenter-style requests). Per result:
    title, authors, publisher, year, language, pages, size, extension, md5,
    item_url (edition page) and file_url (the file page with download mirrors)."""
    try:
        async with _gate("libgen.li"):
            async with httpx.AsyncClient(timeout=45, follow_redirects=True, headers=UA) as client:
                r = await client.get("https://libgen.li/index.php", params={"req": query})
        if r.status_code in (403, 429):
            secs = await _pause("libgen.li", r)
            return {"count": 0, "results": [],
                    "error": f"libgen.li rate limit (HTTP {r.status_code}); domain paused ~{int(secs)}s. "
                             + CHROME_FALLBACK}
        r.raise_for_status()
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(r.text, "html.parser")
        table = (soup.find("table", id="tablelibgen") or soup.find("table", id="datalist")
                 or soup.find("table", class_="dataTable"))
        if table is None:
            return {"count": 0, "results": [],
                    "error": f"results table not found (site layout change or blocked). {CHROME_FALLBACK}"}
        # Current libgen.li layout, 9 cols: title | authors | publisher | year |
        # language | pages | size (link -> file.php) | ext | mirrors (link -> ads.php?md5=)
        out = []
        for row in table.find_all("tr"):
            tds = row.find_all("td")
            if len(tds) < 8:
                continue
            link = tds[0].find("a")
            title = (link.get_text(" ", strip=True) if link else tds[0].get_text(" ", strip=True))
            if not title:
                continue
            size_a = tds[6].find("a")
            mir_a = tds[8].find("a") if len(tds) > 8 else None
            md5 = ""
            if mir_a is not None and mir_a.get("href") and "md5=" in mir_a["href"]:
                md5 = mir_a["href"].split("md5=", 1)[1].split("&", 1)[0]
            out.append({"title": title,
                        "authors": tds[1].get_text(" ", strip=True),
                        "publisher": tds[2].get_text(" ", strip=True),
                        "year": tds[3].get_text(" ", strip=True),
                        "language": tds[4].get_text(" ", strip=True),
                        "pages": tds[5].get_text(" ", strip=True),
                        "size": tds[6].get_text(" ", strip=True),
                        "extension": tds[7].get_text(" ", strip=True),
                        "md5": md5,
                        "item_url": "https://libgen.li/" + link["href"].lstrip("/") if link and link.get("href") else "",
                        "file_url": "https://libgen.li/" + size_a["href"].lstrip("/") if size_a and size_a.get("href") else ""})
            if len(out) >= num_results:
                break
        if not out:
            return {"count": 0, "results": [],
                    "error": f"results table found but no rows parsed (site layout change or blocked). {CHROME_FALLBACK}"}
        return {"count": len(out), "results": out}
    except Exception as e:
        return {"count": 0, "results": [], "error": f"{e}. " + CHROME_FALLBACK}


# ---------------------------------------------------------------- fetch
@mcp.tool
async def fetch_page(url: str, offset: int = 0, max_chars: int = 20000) -> dict:
    """Fetch a web page (JS-rendered via headless Chromium) as markdown.
    Paginated: returns the slice [offset, offset+max_chars) plus total_chars;
    call again with a larger offset to continue. For arXiv paper URLs the
    default route is alphaxiv.org's extracted text (not subject to arXiv rate
    limits); the browser is used only when alphaxiv doesn't have the paper.
    `source` says which one answered ("alphaxiv" | "browser" | "arxiv-direct").
    On failure returns {"success": false, "error": ...}."""
    domain = _domain_of(url)
    arxiv_id = _arxiv_paper_id(url)
    if arxiv_id:
        ax = await _alphaxiv_fulltext_core(arxiv_id, offset, max_chars)
        if ax.get("success"):
            ax["source"] = "alphaxiv"
            return ax
    try:
        async with _browser_slots:
            async with _gate(domain):
                try:
                    from crawl4ai import CacheMode, CrawlerRunConfig
                    crawler = await _crawler()
                    result = await crawler.arun(url=url, config=CrawlerRunConfig(cache_mode=CacheMode.ENABLED))
                except Exception as e:
                    return {"url": url, "success": False, "error": f"browser: {e}. "
                            + _arxiv_hint(domain) + STEALTH_FIRST}
    except _DomainPaused as e:
        return {"url": url, "success": False, "error": f"{e}. " + _arxiv_hint(domain) + STEALTH_FIRST}
    if not result.success:
        code = getattr(result, "status_code", None)
        if code in (403, 429):
            await _pause(domain)
        return {"url": url, "success": False,
                "error": (f"HTTP {code}: " if code else "") + str(getattr(result, "error_message", "crawl failed"))
                + " " + _arxiv_hint(domain) + STEALTH_FIRST}
    md = str(result.markdown)
    out = {"url": url, "success": True, "source": "arxiv-direct" if arxiv_id else "browser",
           "total_chars": len(md), "offset": offset, "content": md[offset:offset + max_chars]}
    if arxiv_id:
        out["note"] = "alphaxiv did not serve this paper; fetched directly from arXiv."
    return out


@mcp.tool
async def fetch_stealth(url: str, offset: int = 0, max_chars: int = 20000, solve_cloudflare: bool = False) -> dict:
    """Fetch a page with Scrapling's StealthyFetcher (stealth-patched headless
    Chromium: real browser UA, canvas noise, WebRTC blocking, spoofed
    locale/timezone) as markdown. Use when fetch_page gets blocked or returns a
    challenge/error page. solve_cloudflare=True also solves Cloudflare
    Turnstile/interstitial challenges (slow: 1-3 min when it has to). Paginated
    like fetch_page. On failure returns {"success": false, "error": ...}."""
    domain = _domain_of(url)
    try:
        async with _browser_slots:
            async with _gate(domain):
                try:
                    from scrapling.fetchers import StealthyFetcher
                    page = await asyncio.to_thread(
                        StealthyFetcher.fetch, url,
                        headless=True, timeout=180000, solve_cloudflare=solve_cloudflare)
                except Exception as e:
                    return {"url": url, "success": False, "error": f"stealth browser: {e}. " + CHROME_FALLBACK}
    except _DomainPaused as e:
        return {"url": url, "success": False, "error": f"{e}. " + CHROME_FALLBACK}
    if page.status != 200:
        if page.status in (403, 429):
            await _pause(domain)
        return {"url": url, "success": False, "error": f"HTTP {page.status}. " + CHROME_FALLBACK}
    md = str(page.markdown())
    return {"url": url, "success": True, "total_chars": len(md), "offset": offset,
            "content": md[offset:offset + max_chars]}


@mcp.tool
async def fetch_pdf(url: str, offset: int = 0, max_chars: int = 20000) -> dict:
    """Download a PDF and convert it to markdown (PyMuPDF 2.x).
    Paginated like fetch_page. For arXiv paper PDFs the default route is
    alphaxiv.org's extracted text (not subject to arXiv rate limits); the
    direct download is used only when alphaxiv doesn't have the paper.
    `source` says which one answered ("alphaxiv" | "direct" | "arxiv-direct").
    On failure returns {"success": false, "error": ...}."""
    domain = _domain_of(url)
    arxiv_id = _arxiv_paper_id(url)
    if arxiv_id:
        ax = await _alphaxiv_fulltext_core(arxiv_id, offset, max_chars)
        if ax.get("success"):
            ax["source"] = "alphaxiv"
            return ax
    try:
        async with _gate(domain):
            async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers=UA) as client:
                r = await client.get(url)
        if r.status_code in (403, 429):
            await _pause(domain, r)
        r.raise_for_status()
        import pymupdf

        def _convert() -> str:
            doc = pymupdf.open(stream=r.content, filetype="pdf")
            return doc.to_markdown()

        md = await asyncio.to_thread(_convert)
    except _DomainPaused as e:
        return {"url": url, "success": False, "error": f"{e}. " + _arxiv_hint(domain) + CHROME_FALLBACK}
    except Exception as e:
        return {"url": url, "success": False,
                "error": f"{type(e).__name__}: {e}. " + _arxiv_hint(domain) + CHROME_FALLBACK}
    out = {"url": url, "success": True, "source": "arxiv-direct" if arxiv_id else "direct",
           "total_chars": len(md), "offset": offset, "content": md[offset:offset + max_chars]}
    if arxiv_id:
        out["note"] = "alphaxiv did not serve this paper; downloaded the PDF directly."
    return out


# ---------------------------------------------------------------- arxiv
_ARXIV_NS = {"a": "http://www.w3.org/2005/Atom"}


@mcp.tool
async def arxiv_search(query: str, max_results: int = 5) -> dict:
    """Search arXiv (all fields) via the official API.
    Returns entries with arxiv_id, title, authors, published (YYYY-MM-DD),
    abstract, and url (abs page; PDF = same URL with '.pdf'). Retries up to 3
    times (15s apart) on transient timeouts; an HTTP 429 rate limit pauses the
    whole domain for all agents (see DOMAIN_COOLDOWNS) and returns immediately."""
    api = (f"http://export.arxiv.org/api/query?search_query=all:{quote_plus(query)}"
           f"&start=0&max_results={max_results}")
    domain = "arxiv.org"
    last_err = ""
    for attempt in range(3):
        try:
            async with _gate(domain):
                async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers=UA) as client:
                    r = await client.get(api)
            if r.status_code == 429:
                secs = await _pause(domain, r)
                return {"query": query, "count": 0, "entries": [],
                        "error": f"arXiv rate limit (HTTP 429); domain paused ~{int(secs)}s "
                                 f"for all agents. " + CHROME_FALLBACK}
            r.raise_for_status()
            root = ET.fromstring(r.text)
            total = int(root.findtext("a:totalResults", "0", _ARXIV_NS) or 0)
            entries = []
            for e in root.findall("a:entry", _ARXIV_NS):
                eid = e.findtext("a:id", "", _ARXIV_NS)
                entries.append({
                    "arxiv_id": eid.rsplit("/", 1)[-1],
                    "title": " ".join(e.findtext("a:title", "", _ARXIV_NS).split()),
                    "authors": [a.findtext("a:name", "", _ARXIV_NS) for a in e.findall("a:author", _ARXIV_NS)],
                    "published": e.findtext("a:published", "", _ARXIV_NS)[:10],
                    "abstract": " ".join(e.findtext("a:summary", "", _ARXIV_NS).split()),
                    "url": eid,
                })
            if entries or total == 0:
                res = {"query": query, "count": len(entries), "entries": entries}
                if entries:
                    res["note"] = ("for the papers themselves, use alphaxiv_lookup (blurb) or "
                                   "alphaxiv_fulltext (full text) -- they bypass arXiv rate limits")
                return res
            last_err = f"arXiv reported {total} matches but returned no entries"
        except _DomainPaused as e:
            return {"query": query, "count": 0, "entries": [], "error": f"{e}. " + CHROME_FALLBACK}
        except Exception as e:
            last_err = str(e) or type(e).__name__
        if attempt < 2:
            await asyncio.sleep(15)
    return {"query": query, "count": 0, "entries": [],
            "error": f"{last_err}. For a specific paper you already know, "
                     f"alphaxiv_lookup/alphaxiv_fulltext bypass the arXiv API entirely. " + CHROME_FALLBACK}


# ---------------------------------------------------------------- alphaxiv
# alphaxiv.org: AI overviews + extracted full text of arXiv papers. Not subject
# to arXiv's rate limits, so prefer these tools over fetch_page/fetch_pdf when
# the target is an arXiv paper.
def _arxiv_id_of(arxiv_id: str) -> str:
    """Accept bare IDs (2402.02085, 2402.02085v2) or arxiv/alphaxiv URLs."""
    s = arxiv_id.strip().split("?")[0].split("#")[0]
    low = s.lower()
    for marker in ("/abs/", "/pdf/", "/overview/"):
        if marker in low:
            s = s[low.index(marker) + len(marker):]
            break
    return s.rstrip("/")


def _arxiv_paper_id(url: str) -> str:
    """Return the arXiv paper ID if url points at an arXiv/alphaxiv *paper*
    (abs/pdf/overview page), else ''. Non-paper arxiv.org pages (lists, etc.)
    return '' so the caller uses its normal path."""
    if _domain_of(url) not in ("arxiv.org", "alphaxiv.org"):
        return ""
    low = url.lower()
    if not any(m in low for m in ("/abs/", "/pdf/", "/overview/")):
        return ""
    return _arxiv_id_of(url)


@mcp.tool
async def alphaxiv_lookup(arxiv_id: str, lang: str = "en") -> dict:
    """Look up an arXiv paper on alphaxiv.org: AI-generated structured overview
    (blurb, problem, solution, key insights, results), the machine-readable
    report, abstract, bibtex, and cited papers. Accepts a bare ID (2402.02085,
    version suffix ok) or an arxiv/alphaxiv URL. Use alphaxiv_fulltext when you
    need equations, tables, or section-level detail."""
    return await _alphaxiv_lookup_core(_arxiv_id_of(arxiv_id), lang)


async def _alphaxiv_lookup_core(pid: str, lang: str) -> dict:
    try:
        async with _gate("alphaxiv.org"):
            async with httpx.AsyncClient(timeout=45, follow_redirects=True, headers=UA) as client:
                r = await client.get(f"https://api.alphaxiv.org/papers/v3/{quote(pid, safe='')}")
        if r.status_code == 404:
            return {"arxiv_id": pid, "success": False,
                    "error": f"paper {pid} is not indexed on alphaxiv yet. " + CHROME_FALLBACK}
        r.raise_for_status()
        meta = r.json()
        vid = meta.get("versionId", "")
        overview: dict = {}
        if vid:
            async with _gate("alphaxiv.org"):
                async with httpx.AsyncClient(timeout=45, follow_redirects=True, headers=UA) as client:
                    ro = await client.get(f"https://api.alphaxiv.org/papers/v3/{vid}/overview/{lang}")
            if ro.status_code == 200:
                overview = ro.json()
        s = overview.get("summary") or {}

        def _clean(v) -> str:
            return " ".join(str(x) for x in v) if isinstance(v, list) else (str(v) if v else "")

        return {"arxiv_id": pid, "success": True,
                "title": overview.get("title") or "",
                "abstract": (overview.get("abstract") or "")[:2000],
                "summary": {"blurb": _clean(s.get("summary"))[:3000],
                             "problem": _clean(s.get("originalProblem"))[:3000],
                             "solution": _clean(s.get("solution"))[:3000],
                             "key_insights": _clean(s.get("keyInsights"))[:3000],
                             "results": _clean(s.get("results"))[:3000]},
                "report": overview.get("intermediateReport") or "",
                "citations": [{"title": c.get("title"), "url": c.get("url") or c.get("sourceUrl") or ""}
                              for c in (overview.get("citations") or [])[:10]],
                "bibtex": meta.get("citationBibtex", ""),
                "alphaxiv_url": f"https://www.alphaxiv.org/overview/{pid}"}
    except _DomainPaused as e:
        return {"arxiv_id": pid, "success": False, "error": f"{e}. " + CHROME_FALLBACK}
    except Exception as e:
        return {"arxiv_id": pid, "success": False, "error": f"{type(e).__name__}: {e}. " + CHROME_FALLBACK}


@mcp.tool
async def alphaxiv_fulltext(arxiv_id: str, offset: int = 0, max_chars: int = 20000) -> dict:
    """Full extracted text of an arXiv paper as markdown from alphaxiv.org.
    Not subject to arXiv rate limits; use instead of fetch_page/fetch_pdf for
    arXiv papers when you need section-level detail. Paginated like fetch_page.
    Accepts a bare ID or an arxiv/alphaxiv URL."""
    return await _alphaxiv_fulltext_core(_arxiv_id_of(arxiv_id), offset, max_chars)


async def _alphaxiv_fulltext_core(pid: str, offset: int, max_chars: int) -> dict:
    src = f"alphaxiv.org/abs/{pid}.md"
    try:
        async with _gate("alphaxiv.org"):
            async with httpx.AsyncClient(timeout=60, follow_redirects=True, headers=UA) as client:
                r = await client.get(f"https://alphaxiv.org/abs/{quote(pid, safe='')}.md")
        if r.status_code == 404:
            return {"url": src, "success": False,
                    "error": f"full text of {pid} not processed on alphaxiv yet. "
                             "Try fetch_pdf on the arXiv PDF, or: " + CHROME_FALLBACK}
        r.raise_for_status()
        md = r.text
        return {"url": f"https://www.alphaxiv.org/abs/{pid}.md", "success": True,
                "total_chars": len(md), "offset": offset, "content": md[offset:offset + max_chars]}
    except _DomainPaused as e:
        return {"url": src, "success": False, "error": f"{e}. " + CHROME_FALLBACK}
    except Exception as e:
        return {"url": src, "success": False, "error": f"{type(e).__name__}: {e}. " + CHROME_FALLBACK}


if __name__ == "__main__":
    mcp.run(transport="http", host="127.0.0.1", port=9000)
