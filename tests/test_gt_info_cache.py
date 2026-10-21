"""GT /info SQLite cache: hits skip the call and spend; expiry refetches.

Deterministic: fake clocks, no network, no real sleeps.
"""
import logging
import sqlite3
import time
from unittest.mock import Mock, patch

import pytest

import book
import collect
import gt_info_cache
import main as shift
from collect import GTRateLimiter
from filter import chain_kill


class FakeClock:
    def __init__(self, start=1_000.0):
        self.now = start

    def time(self):
        return self.now

    def sleep(self, duration):
        self.now += duration

    def advance(self, seconds):
        self.now += seconds


def _sol_token(**over):
    row = {
        "tid": "solmint:1399811149",
        "ticker": "SOLT",
        "addr": "solmint",
        "net": 1399811149,
        "age_minutes": 42,
        "holder_count": 200,
        "mcap_usd": 300_000,
        "liquidity_usd": 48_000,
        "volume_h24": 100_000,
    }
    row.update(over)
    return row


def _pass_sol_wallet(mint):
    return {"top_wallet": 0.02, "top_10": 0.30, "pools_excluded": True}, True, None


def _whale_sol_wallet(mint):
    return {"top_wallet": 0.20, "top_10": 0.40, "pools_excluded": True}, True, None


def _gt_info_ok(holder_count=200, **attrs):
    payload = {
        "holders": {"count": holder_count},
        "mint_authority": "no",
        "freeze_authority": "no",
        "is_honeypot": False,
        "twitter_handle": "example",
        "description": "a token",
    }
    payload.update(attrs)
    return Mock(status_code=200, json=lambda: {"data": {"attributes": payload}})


def _is_gt_info(url) -> bool:
    u = url if isinstance(url, str) else ""
    return "geckoterminal.com" in u and "/tokens/" in u and "/info" in u


@pytest.fixture
def fresh_stats(monkeypatch):
    collect.reset_gt_info_cycle_stats()
    monkeypatch.setattr(collect, "sol_top_wallet", _pass_sol_wallet)
    yield
    collect.reset_gt_info_cycle_stats()


def test_ttl_min_default_and_clamp(monkeypatch):
    monkeypatch.delenv("GT_INFO_CACHE_TTL_MIN", raising=False)
    assert gt_info_cache.ttl_min() == 90
    monkeypatch.setenv("GT_INFO_CACHE_TTL_MIN", "")
    assert gt_info_cache.ttl_min() == 90
    monkeypatch.setenv("GT_INFO_CACHE_TTL_MIN", "not-a-number")
    assert gt_info_cache.ttl_min() == 90
    monkeypatch.setenv("GT_INFO_CACHE_TTL_MIN", "45")
    assert gt_info_cache.ttl_min() == 60
    monkeypatch.setenv("GT_INFO_CACHE_TTL_MIN", "200")
    assert gt_info_cache.ttl_min() == 120
    monkeypatch.setenv("GT_INFO_CACHE_TTL_MIN", "75")
    assert gt_info_cache.ttl_min() == 75
    assert gt_info_cache.ttl_seconds() == 75 * 60


def test_cache_hit_skips_gt_call_and_limiter_spend(fresh_stats):
    """A live hit must not call GT /info or spend a rate-limit slot."""
    clock = FakeClock(5_000.0)
    gt_info_cache.set_time_fn(clock.time)
    limiter = GTRateLimiter(calls_per_min=8, time_fn=clock.time, sleep_fn=clock.sleep)
    gt_info = []

    def fake_get(url, **kwargs):
        assert _is_gt_info(url)
        gt_info.append(url)
        return _gt_info_ok(holder_count=180)

    with patch("collect.requests.get", side_effect=fake_get):
        first = collect.dossier(_sol_token(), limiter=limiter)
        second = collect.dossier(_sol_token(), limiter=limiter)

    assert first["holder_count"] == 180
    # Cache hit keeps static /info (handle) but holder_count comes from the snapshot.
    assert second["holder_count"] == 200
    assert second["x_handle"] == "example"
    assert len(gt_info) == 1
    assert len(limiter.calls) == 1
    stats = collect.gt_info_cycle_stats()
    assert stats["attempts"] == 1
    assert stats["ok"] == 1
    assert stats["hits"] == 1
    assert stats["misses"] == 1


def test_holder_count_refreshes_across_cycles_static_info_cached(fresh_stats):
    """FOMO/trade snapshot holders update on cache hits; handle stays cached."""
    clock = FakeClock(5_000.0)
    gt_info_cache.set_time_fn(clock.time)
    limiter = GTRateLimiter(calls_per_min=8, time_fn=clock.time, sleep_fn=clock.sleep)
    gt_info = []

    def fake_get(url, **kwargs):
        gt_info.append(url)
        return _gt_info_ok(holder_count=647)

    with patch("collect.requests.get", side_effect=fake_get):
        first = collect.dossier(_sol_token(holder_count=647), limiter=limiter)
        second = collect.dossier(_sol_token(holder_count=744), limiter=limiter)
        third = collect.dossier(_sol_token(holder_count=1470), limiter=limiter)

    assert first["holder_count"] == 647
    assert second["holder_count"] == 744
    assert third["holder_count"] == 1470
    assert second["x_handle"] == "example"
    assert third["x_handle"] == "example"
    assert third["mint_authority"] is False
    assert len(gt_info) == 1
    assert len(limiter.calls) == 1
    stats = collect.gt_info_cycle_stats()
    assert stats["attempts"] == 1
    assert stats["hits"] == 2
    assert stats["misses"] == 1


def test_expired_cache_refetches(fresh_stats, monkeypatch):
    """Past the TTL the next dossier must call GT again and spend again."""
    monkeypatch.setenv("GT_INFO_CACHE_TTL_MIN", "60")
    clock = FakeClock(10_000.0)
    gt_info_cache.set_time_fn(clock.time)
    limiter = GTRateLimiter(calls_per_min=8, time_fn=clock.time, sleep_fn=lambda d: None)
    gt_info = []
    spends = []
    real_spend = limiter.spend

    def spy_spend(*args, **kwargs):
        spends.append(limiter.time_fn())
        return real_spend(*args, **kwargs)

    limiter.spend = spy_spend

    def fake_get(url, **kwargs):
        gt_info.append(url)
        return _gt_info_ok(holder_count=50 + len(gt_info))

    with patch("collect.requests.get", side_effect=fake_get):
        first = collect.dossier(_sol_token(), limiter=limiter)
        clock.advance(60 * 60 + 1)  # just past the 60-minute TTL
        second = collect.dossier(_sol_token(), limiter=limiter)

    assert first["holder_count"] == 51
    assert second["holder_count"] == 52
    assert len(gt_info) == 2
    assert len(spends) == 2
    stats = collect.gt_info_cycle_stats()
    assert stats["attempts"] == 2
    assert stats["ok"] == 2
    assert stats["hits"] == 0
    assert stats["misses"] == 2


def test_fresh_cache_inside_ttl_does_not_refetch(fresh_stats, monkeypatch):
    monkeypatch.setenv("GT_INFO_CACHE_TTL_MIN", "60")
    clock = FakeClock(10_000.0)
    gt_info_cache.set_time_fn(clock.time)
    limiter = GTRateLimiter(calls_per_min=8, time_fn=clock.time, sleep_fn=lambda d: None)
    gt_info = []

    def fake_get(url, **kwargs):
        gt_info.append(url)
        return _gt_info_ok(holder_count=77)

    with patch("collect.requests.get", side_effect=fake_get):
        collect.dossier(_sol_token(), limiter=limiter)
        clock.advance(59 * 60)
        collect.dossier(_sol_token(), limiter=limiter)

    assert len(gt_info) == 1
    assert len(limiter.calls) == 1
    assert collect.gt_info_cycle_stats()["hits"] == 1


def test_429_is_never_cached(fresh_stats):
    limiter = GTRateLimiter(calls_per_min=8, time_fn=lambda: 1_000.0, sleep_fn=lambda d: None)
    gt_info = []

    def always_429(url, **kwargs):
        gt_info.append(url)
        return Mock(status_code=429, headers={"Retry-After": "30"})

    with patch("collect.requests.get", side_effect=always_429):
        with pytest.raises(collect.DossierRetryNeeded):
            collect.dossier(_sol_token(addr="a1", tid="a1:1399811149"), limiter=limiter)
        with pytest.raises(collect.DossierRetryNeeded):
            collect.dossier(_sol_token(addr="a1", tid="a1:1399811149"), limiter=limiter)

    assert len(gt_info) == 2
    assert gt_info_cache.get("solana", "a1") is None
    stats = collect.gt_info_cycle_stats()
    assert stats["attempts"] == 2
    assert stats["ok"] == 0
    assert stats["hits"] == 0
    assert stats["misses"] == 2


def test_malformed_info_is_not_cached(fresh_stats):
    def bad_body(url, **kwargs):
        return Mock(status_code=200, json=lambda: {"data": {"attributes": None}})

    with patch("collect.requests.get", side_effect=bad_body):
        with pytest.raises(ValueError):
            collect.dossier(_sol_token(addr="bad1", tid="bad1:1399811149"))
    assert gt_info_cache.get("solana", "bad1") is None
    stats = collect.gt_info_cycle_stats()
    assert stats["attempts"] == 1
    assert stats["ok"] == 0
    assert stats["misses"] == 1


def test_concentration_kill_is_not_a_gt_attempt(fresh_stats, monkeypatch):
    monkeypatch.setattr(collect, "sol_top_wallet", _whale_sol_wallet)
    limiter = GTRateLimiter(calls_per_min=8, time_fn=lambda: 1_000.0, sleep_fn=lambda d: None)

    def boom(url, **kwargs):
        raise AssertionError("GT /info must not run after a concentration kill")

    with patch("collect.requests.get", side_effect=boom):
        d = collect.dossier(_sol_token(), limiter=limiter)
    assert chain_kill(d) == "top_wallet"
    assert len(limiter.calls) == 0
    stats = collect.gt_info_cycle_stats()
    assert stats == {"attempts": 0, "ok": 0, "hits": 0, "misses": 0}


def test_run_once_counts_only_real_gt_calls(fresh_stats, monkeypatch, caplog, tmp_path):
    """attempts/ok exclude concentration kills and cache hits; history stores both."""
    monkeypatch.setenv("CYCLE_HISTORY_DB", str(tmp_path / "cycle_history.db"))
    import cycle_history
    cycle_history.reset()

    book.release()
    book.DB.execute("DELETE FROM carry")
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.commit()

    wall = [1_700_100_000.0]
    monkeypatch.setattr(time, "time", lambda: wall[0])
    monkeypatch.setattr(shift, "_now", lambda: wall[0])

    clock = FakeClock(20_000.0)
    gt_info_cache.set_time_fn(clock.time)
    limiter = GTRateLimiter(calls_per_min=10, time_fn=clock.time, sleep_fn=lambda d: None)
    gt_info = []

    pass_tid = "passmint:1399811149"
    whale_tid = "whalemint:1399811149"

    def fake_universe(limiter=None, fomo=None):
        return ([pass_tid, whale_tid], {})

    def fake_shortlist(fomo, ids):
        return [
            _sol_token(tid=pass_tid, addr="passmint", ticker="PASS"),
            _sol_token(tid=whale_tid, addr="whalemint", ticker="WHAL"),
        ]

    def fake_trade(t, gt_txns_cache=None):
        return ({"buys_h1": 100, "sells_h1": 50, "buys_h6": 200, "sells_h6": 100,
                 "trades_h24": 1000}, "ok")

    def wallet(mint):
        if mint == "whalemint":
            return _whale_sol_wallet(mint)
        return _pass_sol_wallet(mint)

    def fake_get(url, **kwargs):
        assert _is_gt_info(url)
        gt_info.append(url)
        return _gt_info_ok(holder_count=220)

    def fake_judge(question_set, state):
        return {"model": "test", "answers": {
            "concentration_is_exit_risk": {"type": "noul", "noul": 0.3},
            "shape": {"type": "choice", "choice": "crowd",
                      "probabilities": {"crowd": 0.8, "one_buyer": 0.1,
                                        "fading": 0.05, "too_early": 0.05}},
        }, "usage": {}}

    monkeypatch.setattr(collect, "sol_top_wallet", wallet)
    monkeypatch.setattr(shift, "universe", fake_universe)
    monkeypatch.setattr(shift, "shortlist", fake_shortlist)
    monkeypatch.setattr(shift, "trade_counts", fake_trade)
    desk = Mock()
    desk.read_x = Mock(return_value=None)
    desk.write_state = Mock()
    desk.log_shadow = Mock()
    caplog.set_level(logging.INFO)

    with patch("collect.requests.get", side_effect=fake_get):
        _order, stats1 = shift.run_once(
            Mock(), fake_judge, desk, 10000, shadow=True, gt_limiter=limiter,
        )

    assert stats1["gt_dossier_attempts"] == 1
    assert stats1["gt_dossier_ok"] == 1
    assert stats1["gt_cache_hits"] == 0
    assert stats1["gt_cache_misses"] == 1
    assert stats1["chain"].get("top_wallet") == 1
    assert len(gt_info) == 1
    assert any("cache_hits=0 cache_misses=1" in r.message for r in caplog.records)

    book.DB.execute("DELETE FROM bench")
    book.DB.execute("DELETE FROM defer")
    book.DB.commit()
    wall[0] += 5
    caplog.clear()

    with patch("collect.requests.get", side_effect=fake_get):
        _order, stats2 = shift.run_once(
            Mock(), fake_judge, desk, 10000, shadow=True, gt_limiter=limiter,
        )

    assert stats2["gt_dossier_attempts"] == 0
    assert stats2["gt_dossier_ok"] == 0
    assert stats2["gt_cache_hits"] == 1
    assert stats2["gt_cache_misses"] == 0
    assert len(gt_info) == 1
    assert any("cache_hits=1 cache_misses=0" in r.message for r in caplog.records)

    rows = cycle_history.cycles_since(0)
    assert len(rows) >= 2
    assert rows[0]["gt_cache_hits"] == 0
    assert rows[0]["gt_cache_misses"] == 1
    assert rows[0]["gt_dossier_attempts"] == 1
    assert rows[0]["gt_dossier_ok"] == 1
    assert rows[-1]["gt_cache_hits"] == 1
    assert rows[-1]["gt_cache_misses"] == 0
    assert rows[-1]["gt_dossier_attempts"] == 0
    cycle_history.reset()


def test_cache_key_includes_chain():
    """Same address on two chains must not collide. Fails if chain is dropped."""
    addr = "0xcccccccccccccccccccccccccccccccccccccccc"
    bsc = {"holders": {"count": 111}, "is_honeypot": False}
    base = {"holders": {"count": 222}, "is_honeypot": True}
    assert gt_info_cache.normalize_key("bsc", addr)[0] == "bsc"
    assert gt_info_cache.normalize_key("base", addr)[0] == "base"
    assert gt_info_cache.normalize_key("bsc", addr) != gt_info_cache.normalize_key("base", addr)
    assert gt_info_cache.put("bsc", addr, bsc)
    assert gt_info_cache.put("base", addr, base)
    got_bsc = gt_info_cache.get("bsc", addr)
    got_base = gt_info_cache.get("base", addr)
    assert got_bsc is not None and got_bsc["holders"]["count"] == 111
    assert got_base is not None and got_base["holders"]["count"] == 222
    assert gt_info_cache.get("robinhood", addr) is None


def test_non_200_with_body_is_not_cached(fresh_stats):
    """A 500 that still has data.attributes must not be written to the cache."""
    gt_info = []

    def http_500(url, **kwargs):
        gt_info.append(url)
        return Mock(status_code=500, json=lambda: {"data": {"attributes": {
            "holders": {"count": 150},
            "mint_authority": "no",
            "freeze_authority": "no",
        }}})

    with patch("collect.requests.get", side_effect=http_500):
        first = collect.dossier(_sol_token(addr="err1", tid="err1:1399811149"))
        collect.dossier(_sol_token(addr="err1", tid="err1:1399811149"))

    assert first["holder_count"] == 150
    assert gt_info_cache.get("solana", "err1") is None
    assert gt_info_cache.put("solana", "err1", {"holders": {"count": 150}}, status_code=500) is False
    assert len(gt_info) == 2
    stats = collect.gt_info_cycle_stats()
    assert stats["hits"] == 0
    assert stats["misses"] == 2


def test_null_holders_not_cached(fresh_stats):
    """Null / missing holder_count is applied live but not frozen for the TTL."""
    assert gt_info_cache.is_complete_attributes({"holders": None}) is False
    assert gt_info_cache.is_complete_attributes({"holders": {}}) is False
    assert gt_info_cache.is_complete_attributes({"holders": {"count": None}}) is False
    assert gt_info_cache.is_complete_attributes({"holders": {"count": 80}}) is True
    assert gt_info_cache.put("solana", "young1", {"holders": None}) is False
    assert gt_info_cache.put("solana", "young1", {"holders": {"count": None}}) is False

    gt_info = []

    def null_holders(url, **kwargs):
        gt_info.append(url)
        return Mock(status_code=200, json=lambda: {"data": {"attributes": {
            "holders": None,
            "mint_authority": "no",
            "freeze_authority": "no",
        }}})

    young = _sol_token(addr="young1", tid="young1:1399811149", age_minutes=12, holder_count=40)
    with patch("collect.requests.get", side_effect=null_holders):
        first = collect.dossier(young)
        collect.dossier(_sol_token(addr="young1", tid="young1:1399811149", age_minutes=12, holder_count=40))

    assert first["holder_count"] == 40  # FOMO fallback, not a cached null
    assert gt_info_cache.get("solana", "young1") is None
    assert len(gt_info) == 2


def test_locked_db_fails_open_to_live_call(fresh_stats):
    """A locked cache must not stall ~5s; get fails open and dossier calls GT."""
    complete = {"holders": {"count": 180}, "mint_authority": "no", "freeze_authority": "no"}
    assert gt_info_cache.put("solana", "solmint", complete)
    assert gt_info_cache.get("solana", "solmint") is not None

    locker = sqlite3.connect(str(gt_info_cache.db_path()), timeout=0)
    locker.isolation_level = None
    locker.execute("BEGIN EXCLUSIVE")
    gt_info = []
    try:
        t0 = time.monotonic()
        assert gt_info_cache.get("solana", "solmint") is None
        assert time.monotonic() - t0 < 1.0

        def fake_get(url, **kwargs):
            gt_info.append(url)
            return _gt_info_ok(holder_count=99)

        limiter = GTRateLimiter(calls_per_min=8, time_fn=lambda: 1_000.0, sleep_fn=lambda d: None)
        with patch("collect.requests.get", side_effect=fake_get):
            d = collect.dossier(_sol_token(), limiter=limiter)
        elapsed = time.monotonic() - t0
        assert elapsed < 1.0
        assert len(gt_info) == 1
        assert d["holder_count"] == 99
        assert len(limiter.calls) == 1
    finally:
        locker.execute("ROLLBACK")
        locker.close()


def test_briefing_picks_line_not_young_reached_judge(tmp_path, monkeypatch):
    """Young tokens that reached the judge are not reported as picks."""
    monkeypatch.setenv("CYCLE_HISTORY_DB", str(tmp_path / "cycle_history.db"))
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    import cycle_history
    cycle_history.reset()
    stats = {
        "seen": 8, "benched": 0, "judged": 1, "requeued": 0,
        "free": {}, "trade": {}, "chain": {}, "soft": {},
        "carry": 0, "unevaluated": 0,
        "tokens": [{
            "tid": "y4:56", "ticker": "YNG", "net": 56,
            "stage": "judged", "reason": None, "age_minutes": 22,
            "soft_scores": {"momentum_already_spent": 0.3},
        }],
        "young_free": [
            {"tid": "y4:56", "ticker": "YNG", "net": 56, "age_minutes": 22},
        ],
    }
    cycle_history.on_cycle(stats, now=1_700_000_000.0, source="backfill")
    payload = cycle_history.build_briefing(1_700_000_100.0)
    md = payload["markdown"]
    assert "- passed judge/picks: 0" in md
    assert "- young reached judge: 1" in md
    assert "- passed judge/picks: 1" not in md
    assert "young reached judge" in md
    yo = payload["last_24h"]["young_outcomes"]
    assert yo["judged"] == 1
    assert payload["last_24h"]["shadow"]["picks"] == 0
    cycle_history.reset()
