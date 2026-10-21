"""Outcomes GT pause + shared limiter. Fake clocks only. No network.

Does not edit collect.GTRateLimiter. Shadow/paper only.
"""
from __future__ import annotations

import logging
import os
import pathlib
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DESK_SECRET", "test-secret-for-gt-limiter")
os.environ.setdefault("JUDGE_MOCK", "1")
os.environ["TZ"] = "UTC"
if hasattr(time, "tzset"):
    time.tzset()


def ts(y, m, d, hh=12, mm=0, ss=0) -> float:
    return time.mktime((y, m, d, hh, mm, ss, 0, 0, -1))


class Resp:
    def __init__(self, status, payload, headers=None):
        self.status_code = status
        self._payload = payload
        self.headers = headers or {}

    def json(self):
        return self._payload


def _candle(t, o, h, l, c):
    return {"ts": t, "open": o, "high": h, "low": l, "close": c, "volume": 1.0}


def _ohlcv_list(candles):
    return {
        "data": {
            "type": "ohlcv_request_response",
            "attributes": {
                "ohlcv_list": [
                    [c["ts"], c["open"], c["high"], c["low"], c["close"], c.get("volume", 1)]
                    for c in candles
                ]
            },
        }
    }


@pytest.fixture
def env(tmp_path, monkeypatch):
    os.environ["TZ"] = "UTC"
    if hasattr(time, "tzset"):
        time.tzset()
    monkeypatch.setenv("CYCLE_HISTORY_DB", str(tmp_path / "cycle_history.db"))
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    monkeypatch.setenv("OUTCOMES_DB", str(tmp_path / "outcomes.db"))
    monkeypatch.delenv("COINGECKO_API_KEY", raising=False)
    monkeypatch.delenv("COINGECKO_PLAN", raising=False)
    import gt_api
    import outcomes
    import ops_summary
    import cycle_history
    from collect import GTRateLimiter
    gt_api.reset()
    outcomes.reset()
    ops_summary.reset()
    cycle_history.reset()
    clock = [1_000.0]
    limiter = GTRateLimiter(
        calls_per_min=5, min_spacing_sec=12.0,
        time_fn=lambda: clock[0],
        sleep_fn=lambda d: clock.__setitem__(0, clock[0] + d),
    )
    outcomes.set_shared_limiter(limiter)
    outcomes.set_time_fn(lambda: clock[0])
    outcomes.set_sleep_fn(lambda d: clock.__setitem__(0, clock[0] + d))
    yield {
        "clock": clock,
        "limiter": limiter,
        "outcomes": outcomes,
        "gt_api": gt_api,
        "ops_summary": ops_summary,
        "cycle_history": cycle_history,
    }
    outcomes.set_shared_limiter(None)
    outcomes.reset()
    gt_api.reset()
    ops_summary.reset()
    cycle_history.reset()


def test_paused_worker_sleeps_and_log_is_rate_limited(env, monkeypatch, caplog):
    outcomes = env["outcomes"]
    gt_api = env["gt_api"]
    limiter = env["limiter"]
    clock = env["clock"]
    monkeypatch.setattr(gt_api, "in_outcomes_window", lambda now=None, **k: True)
    limiter.backoff_until = clock[0] + 600.0
    caplog.set_level(logging.INFO, logger="outcomes")
    first = outcomes.worker_step()
    assert first.get("paused") is True
    assert any("outcomes fetch paused" in r.message for r in caplog.records)
    pause_lines = [r for r in caplog.records if "outcomes fetch paused" in r.message]
    assert len(pause_lines) == 1
    slept_at = clock[0]
    assert slept_at == pytest.approx(1_000.0 + outcomes.pause_sleep_sec())
    second = outcomes.worker_step()
    assert second.get("paused") is True
    pause_lines = [r for r in caplog.records if "outcomes fetch paused" in r.message]
    assert len(pause_lines) == 1
    clock[0] = slept_at + 61.0
    limiter.backoff_until = clock[0] + 600.0
    third = outcomes.worker_step()
    assert third.get("paused") is True
    pause_lines = [r for r in caplog.records if "outcomes fetch paused" in r.message]
    assert len(pause_lines) == 2


def test_pause_clears_when_backoff_expires(env):
    outcomes = env["outcomes"]
    limiter = env["limiter"]
    clock = env["clock"]
    now = clock[0]
    limiter.backoff_until = now + 30.0
    limiter.stats_429_count = 9
    assert outcomes.cycle_gt_paused(now) is True
    clock[0] = now + 30.0
    assert outcomes.cycle_gt_paused(clock[0]) is False
    clock[0] = now + 31.0
    assert outcomes.cycle_gt_paused(clock[0]) is False


def test_sticky_429_count_does_not_pause(env):
    outcomes = env["outcomes"]
    limiter = env["limiter"]
    clock = env["clock"]
    limiter.stats_429_count = 7
    limiter.backoff_until = 0.0
    assert outcomes.cycle_gt_paused(clock[0]) is False
    calls = []
    outcomes.set_get_fn(lambda *a, **k: calls.append(a) or Resp(500, {}))
    outcomes.enqueue_job("z:1399811149", clock[0] - 8 * 3600, now=clock[0])
    result = outcomes.drain_jobs(now=clock[0], trickle=True, max_calls=1)
    assert result["paused"] is False
    assert result["fetched"] >= 1


def test_outcomes_consume_shared_limiter_slots(env):
    outcomes = env["outcomes"]
    limiter = env["limiter"]
    clock = env["clock"]
    now = clock[0]
    entry = now - 8 * 3600
    candles = [_candle(entry + i * 300, 1.0, 1.01, 0.99, 1.0) for i in range(80)]
    seen = []

    def get(url, params=None, timeout=None, headers=None):
        seen.append(url)
        if "dexscreener.com" in url:
            return Resp(200, [{"pairAddress": "PoolX", "liquidity": {"usd": 80_000}}])
        return Resp(200, _ohlcv_list(candles))

    outcomes.set_get_fn(get)
    before = len(limiter.calls)
    got = outcomes.fetch_outcome("tok:1399811149", entry, now=now)
    assert got is not None
    assert any("api.geckoterminal.com" in u for u in seen)
    assert len(limiter.calls) == before + 1
    assert not any("api.coingecko.com" in u for u in seen)


def test_desk_priority_wins_reserved_slots(env):
    outcomes = env["outcomes"]
    limiter = env["limiter"]
    clock = env["clock"]
    limiter.calls_per_min = 2
    limiter.reserve(1)
    limiter.min_spacing_sec = 0.0
    now = clock[0]
    entry = now - 8 * 3600
    candles = [_candle(entry + i * 300, 1.0, 1.01, 0.99, 1.0) for i in range(80)]

    def get(url, params=None, timeout=None, headers=None):
        if "dexscreener.com" in url:
            return Resp(200, [{"pairAddress": "PoolX", "liquidity": {"usd": 80_000}}])
        return Resp(200, _ohlcv_list(candles))

    outcomes.set_get_fn(get)
    got = outcomes.fetch_outcome("tok:1399811149", entry, now=now)
    assert got is not None
    assert len(limiter.calls) == 1
    assert limiter.available() == 1
    # non-priority outcomes cannot take the reserved last slot
    assert limiter.spend(1, priority=False) is False
    assert limiter.wait_if_needed(priority=True, deadline=now + 1) is True
    assert limiter.spend(1, priority=True) is True
    assert len(limiter.calls) == 2


def test_no_outcomes_gt_during_cycle_phase(env):
    outcomes = env["outcomes"]
    limiter = env["limiter"]
    clock = env["clock"]
    limiter.cycle_deadline = clock[0] + 30.0
    calls = []
    outcomes.set_get_fn(lambda *a, **k: calls.append(a) or Resp(500, {}))
    outcomes.enqueue_job("z:1399811149", clock[0] - 8 * 3600, now=clock[0])
    result = outcomes.drain_jobs(now=clock[0], trickle=True, max_calls=1)
    assert result["paused"] is True
    assert result["fetched"] == 0
    assert calls == []
    limiter.cycle_deadline = clock[0] - 1.0
    assert outcomes._in_cycle_gt_phase(clock[0]) is False


def test_demo_cap_falls_back_to_free_gt_and_shared_limiter(env, monkeypatch):
    outcomes = env["outcomes"]
    gt_api = env["gt_api"]
    limiter = env["limiter"]
    clock = env["clock"]
    monkeypatch.setenv("COINGECKO_API_KEY", "CG-DEMOSECRET99")
    monkeypatch.delenv("COINGECKO_PLAN", raising=False)
    gt_api.reset()
    now = clock[0]
    outcomes.set_time_fn(lambda: clock[0])
    cap = outcomes.demo_daily_cap(now)
    db = outcomes._connect()
    db.execute(
        "INSERT OR REPLACE INTO cg_demo_calls (yyyymm, calls) VALUES (?, ?)",
        (outcomes._yyyymm(now), cap),
    )
    db.execute(
        "INSERT OR REPLACE INTO cg_demo_calls_daily (yyyymmdd, calls) VALUES (?, ?)",
        (outcomes._yyyymmdd(now), cap),
    )
    db.commit()
    assert outcomes.demo_budget_capped(now) is True
    assert gt_api.using_demo() is False
    entry = now - 8 * 3600
    candles = [_candle(entry + i * 300, 1.0, 1.01, 0.99, 1.0) for i in range(80)]
    seen = []

    def get(url, params=None, timeout=None, headers=None):
        seen.append(url)
        if "dexscreener.com" in url:
            return Resp(200, [{"pairAddress": "PoolX", "liquidity": {"usd": 80_000}}])
        return Resp(200, _ohlcv_list(candles))

    outcomes.set_get_fn(get)
    before = len(limiter.calls)
    got = outcomes.fetch_outcome("tok:1399811149", entry, now=now)
    assert got is not None
    assert not any("api.coingecko.com" in u for u in seen)
    assert any("api.geckoterminal.com" in u for u in seen)
    assert len(limiter.calls) == before + 1


def test_keyless_429_records_on_shared_limiter(env):
    outcomes = env["outcomes"]
    limiter = env["limiter"]
    clock = env["clock"]
    now = clock[0]
    entry = now - 8 * 3600

    def get(url, params=None, timeout=None, headers=None):
        if "dexscreener.com" in url:
            return Resp(200, [{"pairAddress": "PoolX", "liquidity": {"usd": 80_000}}])
        return Resp(429, {}, headers={"Retry-After": "25"})

    outcomes.set_get_fn(get)
    before = limiter.stats_429_count
    got = outcomes.fetch_ohlcv("solana", "PoolX", entry)
    assert got is None
    assert limiter.stats_429_count == before + 1
    assert limiter.backoff_until > now
    assert outcomes.outcomes_429_count() >= 1


def test_ops_exposes_info_cache_hit_rate_and_429_by_source(env, monkeypatch):
    ops_summary = env["ops_summary"]
    gt_api = env["gt_api"]
    cycle_history = env["cycle_history"]
    import collect
    collect.reset_gt_info_cycle_stats()
    collect._gt_info_cycle.update(hits=3, misses=1)
    gt_api.note_429("outcomes")
    now = ts(2026, 10, 12, 12)
    cycle_history.on_cycle({
        "seen": 1, "benched": 0, "judged": 0, "requeued": 0,
        "free": {}, "trade": {}, "chain": {}, "soft": {},
        "tokens": [], "gt_429": 4,
        "gt_cache_hits": 6, "gt_cache_misses": 2,
    }, now=now - 60, source="backfill")
    payload = ops_summary.build_summary(now=now)
    budget = payload["budget"]
    assert budget["gt_429_by_source"]["outcomes"] >= 1
    assert budget["gt_429_by_source"]["desk"] >= 4
    cache = budget["gt_info_cache"]
    assert cache["hits"] == 3
    assert cache["misses"] == 1
    assert cache["hit_rate"] == pytest.approx(0.75)
    assert cache["hits_24h"] == 6
    assert cache["misses_24h"] == 2
    assert cache["hit_rate_24h"] == pytest.approx(0.75)
    collect.reset_gt_info_cycle_stats()
