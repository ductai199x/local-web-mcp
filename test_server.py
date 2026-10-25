"""Unit tests for server.py's pure helpers and the domain gate.

Run: uv run pytest
(No network access needed; the live smoke_test.py covers end-to-end behavior.)
"""
import asyncio
import time

import server


# ---------------------------------------------------------------- pure helpers
def test_arxiv_id_of_bare():
    assert server._arxiv_id_of("2402.02085") == "2402.02085"
    assert server._arxiv_id_of("2402.02085v2") == "2402.02085v2"
    assert server._arxiv_id_of("https://arxiv.org/pdf/hep-th/9901001") == "hep-th/9901001"


def test_arxiv_id_of_urls():
    assert server._arxiv_id_of("https://arxiv.org/abs/2402.02085") == "2402.02085"
    assert server._arxiv_id_of("https://arxiv.org/pdf/2402.02085v3") == "2402.02085v3"
    assert server._arxiv_id_of("https://alphaxiv.org/overview/2402.02085") == "2402.02085"
    assert server._arxiv_id_of("https://arxiv.org/abs/2402.02085?context=12#top") == "2402.02085"


def test_arxiv_paper_id():
    assert server._arxiv_paper_id("https://arxiv.org/abs/2402.02085") == "2402.02085"
    assert server._arxiv_paper_id("https://arxiv.org/pdf/2402.02085") == "2402.02085"
    assert server._arxiv_paper_id("https://www.alphaxiv.org/overview/2402.02085") == "2402.02085"
    # non-paper arxiv.org pages and other domains are out of scope
    assert server._arxiv_paper_id("https://arxiv.org/list/cs.CV/recent") == ""
    assert server._arxiv_paper_id("https://example.com/abs/2402.02085") == ""


def test_domain_of():
    assert server._domain_of("https://api.github.com/repos/x") == "github.com"
    assert server._domain_of("https://export.arxiv.org/api") == "arxiv.org"
    assert server._domain_of("http://127.0.0.1:8888/search") == "127.0.0.1"
    assert server._domain_of("https://user:pw@www.example.co.uk/x") == "example.co.uk"


# ---------------------------------------------------------------- domain gate
def test_gate_and_pause():
    # One event loop for everything: module-level asyncio primitives bind to
    # the loop that first uses them, so separate asyncio.run() calls would clash.
    async def scenario():
        server.DOMAIN_GAPS["example.com"] = 2.0
        server.DOMAIN_GAPS["other.org"] = 0.0
        in_flight = {"example.com": 0, "other.org": 0}
        peak = {"example.com": 0, "other.org": 0}

        async def work(domain):
            async with server._gate(domain):
                in_flight[domain] += 1
                peak[domain] = max(peak[domain], in_flight[domain])
                await asyncio.sleep(0.2)  # simulated request
                in_flight[domain] -= 1

        # 1) same-domain serialization + gap; cross-domain parallelism
        t0 = time.monotonic()
        await asyncio.gather(work("example.com"), work("example.com"), work("other.org"))
        elapsed = time.monotonic() - t0

        # 2) short pause is waited out at the gate
        async with server._domains_guard:
            server._domain_paused_until["pausetest.io"] = time.monotonic() + 1.5
        t0 = time.monotonic()
        async with server._gate("pausetest.io"):
            pass
        waited = time.monotonic() - t0

        # 3) long pause (> GATE_WAIT_CAP) bails immediately
        async with server._domains_guard:
            server._domain_paused_until["longban.io"] = time.monotonic() + 120
        bailed = False
        try:
            async with server._gate("longban.io"):
                pass
        except server._DomainPaused:
            bailed = True

        # 4) _pause honors Retry-After
        class FakeResp:
            headers = {"Retry-After": "45"}
        secs = await server._pause("retry.io", r=FakeResp())
        return elapsed, peak, waited, bailed, secs

    elapsed, peak, waited, bailed, secs = asyncio.run(scenario())
    assert peak["example.com"] == 1, "same-domain requests must serialize"
    assert peak["other.org"] == 1
    assert elapsed >= 2.0, "2s per-domain gap not honored"
    assert waited >= 1.4, "short pause should be waited out"
    assert bailed, "long pause should raise _DomainPaused"
    assert secs == 45.0, "Retry-After not honored"
