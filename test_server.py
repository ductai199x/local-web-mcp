"""Unit tests for server.py's pure helpers and the domain gate.

Run: uv run pytest -q
No network access and no real-time waiting: the gate tests run against an
injected fake clock (asyncio.sleep only advances the fake clock plus a
zero-cost event-loop turn), so the whole suite completes in a few seconds of
CPU time. The live smoke_test.py covers end-to-end behavior separately.
"""
import asyncio

import pytest

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
@pytest.fixture
def fake_clock(monkeypatch):
    """Deterministic clock: sleep() advances it instantly, plus one zero-cost
    event-loop turn so concurrent tasks interleave like they would for real."""
    real_sleep = asyncio.sleep

    class Clock:
        # large epoch, like a real monotonic clock: first gate acquisition
        # must not look like a 2s gap from t=0
        now = 1_000_000.0

    def fake_monotonic() -> float:
        return Clock.now

    async def fake_sleep(delay: float):
        Clock.now += delay
        await real_sleep(0)

    monkeypatch.setattr(server, "_now", fake_monotonic)
    monkeypatch.setattr(server.asyncio, "sleep", fake_sleep)
    return Clock


def test_gate_and_pause(fake_clock):
    # One event loop for everything: module-level asyncio primitives (the
    # _domains_guard lock, per-domain semaphores) bind to the first loop.
    async def scenario():
        server.DOMAIN_GAPS["example.com"] = 2.0
        server.DOMAIN_GAPS["other.org"] = 0.0
        in_flight = {"example.com": 0, "other.org": 0}
        peak = {"example.com": 0, "other.org": 0, "total": 0}

        async def work(domain):
            async with server._gate(domain):
                in_flight[domain] += 1
                peak[domain] = max(peak[domain], in_flight[domain])
                peak["total"] = max(peak["total"], sum(in_flight.values()))
                await asyncio.sleep(0.2)  # fake: simulated request
                in_flight[domain] -= 1

        t0 = fake_clock.now
        # 1) same-domain serialize + gap; different domains overlap
        await asyncio.gather(work("example.com"), work("example.com"), work("other.org"))
        elapsed = fake_clock.now - t0

        # 2) short pause (< GATE_WAIT_CAP) is waited out at the gate
        async with server._domains_guard:
            server._domain_paused_until["pausetest.io"] = server._now() + 1.5
        t0 = fake_clock.now
        async with server._gate("pausetest.io"):
            pass
        waited = fake_clock.now - t0

        # 3) long pause (> GATE_WAIT_CAP) bails immediately
        async with server._domains_guard:
            server._domain_paused_until["longban.io"] = server._now() + 120
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
    assert peak["total"] == 2, "different domains must overlap"
    assert elapsed >= 2.0, "2s per-domain gap not honored"
    assert waited == pytest.approx(1.5), "short pause should be waited out"
    assert bailed, "long pause should raise _DomainPaused"
    assert secs == 45.0, "Retry-After not honored"
