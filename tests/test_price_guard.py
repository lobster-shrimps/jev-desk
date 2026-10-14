"""Price sanity: mint match, min liq, unconfirmed spikes. Paper marks only.

Does not change thresholds, questions, pick, filter, collect, or judge.
"""
import logging
import os
import pathlib
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DESK_SECRET", "test-secret-for-price-guard")
os.environ.setdefault("JUDGE_MOCK", "1")

import price_guard  # noqa: E402

T0 = 1_700_000_000.0
TICKET = 50.0
# Catnip 61q3T4… closed at +$499,249.90 off a $0.02-liquidity pumpswap quote.
CATNIP_MINT = "61q3T4xxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx"
CATNIP_TID = f"{CATNIP_MINT}:1399811149"
CATNIP_ENTRY = 9.24e-7
CATNIP_SPIKE = 0.009226


@pytest.fixture(autouse=True)
def _reset_guard():
    price_guard.reset()
    yield
    price_guard.reset()


def _pair(mint, price, liq, **extra):
    row = {
        "priceUsd": str(price),
        "liquidity": {"usd": liq},
        "baseToken": {"address": mint, "symbol": extra.pop("symbol", "X")},
        "chainId": extra.pop("chainId", "solana"),
        "volume": extra.pop("volume", {"m5": 1, "h1": 2, "h24": 3}),
    }
    row.update(extra)
    return row


def _pumpfun_pair(mint, price, *, symbol="PINECONE", market_cap=21_500, **extra):
    """Real-shaped DexScreener pump.fun bonding-curve pair: no liquidity field."""
    row = {
        "chainId": "solana",
        "dexId": "pumpfun",
        "url": f"https://dexscreener.com/solana/{mint[:8]}pump",
        "pairAddress": f"{mint[:8]}bondingcurve",
        "labels": ["pump"],
        "baseToken": {"address": mint, "name": symbol, "symbol": symbol},
        "quoteToken": {
            "address": "So11111111111111111111111111111111111111112",
            "name": "Wrapped SOL",
            "symbol": "SOL",
        },
        "priceNative": "0.00001234",
        "priceUsd": str(price),
        "txns": {
            "m5": {"buys": 12, "sells": 3},
            "h1": {"buys": 80, "sells": 20},
            "h24": {"buys": 200, "sells": 50},
        },
        "volume": {"h24": 12000.0, "h6": 4000.0, "h1": 800.0, "m5": 100.0},
        "priceChange": {"m5": 5.2, "h1": 12.0, "h6": 40.0, "h24": 80.0},
        "fdv": market_cap,
        "marketCap": market_cap,
    }
    row.update(extra)
    return row


def test_reject_low_liq_and_wrong_mint():
    mint = "Mint111"
    assert price_guard.pair_reject_reason(_pair(mint, 1.0, 999), mint) == "low_liq"
    assert price_guard.pair_reject_reason(
        _pair("OtherMint", 1.0, 50_000), mint,
    ) == "base_mint_mismatch"
    quote_as_base = {
        "priceUsd": "1.0",
        "liquidity": {"usd": 50_000},
        "quoteToken": {"address": mint},
        "baseToken": {"address": "So11111111111111111111111111111111111111112"},
    }
    assert price_guard.pair_reject_reason(quote_as_base, mint) == "base_mint_mismatch"
    ok = price_guard.mark_from_pairs([_pair(mint, 1.25, 5_000)], mint, tid="t")
    assert ok["price_usd"] == pytest.approx(1.25)


def test_pumpfun_pair_with_no_liquidity_field_is_accepted():
    mint = "PINECONEmint1111111111111111111111111111111"
    pair = _pumpfun_pair(mint, 0.00215, symbol="PINECONE", market_cap=21_500)
    assert "liquidity" not in pair
    assert price_guard.pair_reject_reason(pair, mint) is None
    mark = price_guard.mark_from_pairs([pair], mint, entry=0.002, prev=0.002, tid=f"{mint}:1399811149")
    assert mark is not None
    assert mark["price_usd"] == pytest.approx(0.00215)
    assert mark["liq_usd"] is None

    jump = _pumpfun_pair(mint, 0.00215, symbol="JUMP", market_cap=48_000)
    assert price_guard.mark_from_pairs([jump], mint, tid="jump:1399811149")["price_usd"] == pytest.approx(0.00215)

    cheap = _pumpfun_pair(mint, 0.00215, market_cap=500)
    assert price_guard.pair_reject_reason(cheap, mint) == "low_mcap"


def test_catnip_pumpswap_quote_drains_at_zero(caplog):
    caplog.set_level(logging.INFO)
    # pumpswap reports $0.02 liquidity and ~10,000× entry. Still low_liq.
    # Upward junk with a valid anchor must drain at 0 so SL/trail fire.
    pairs = [_pair(
        CATNIP_MINT, CATNIP_SPIKE, 0.02, symbol="Catnip", dexId="pumpswap",
        marketCap=9_000_000,
    )]
    assert CATNIP_SPIKE / CATNIP_ENTRY == pytest.approx(10_000, rel=0.01)
    mark = price_guard.mark_from_pairs(
        pairs, CATNIP_MINT, entry=CATNIP_ENTRY, prev=CATNIP_ENTRY, tid=CATNIP_TID,
    )
    assert mark is not None
    assert mark["source"] == "drained"
    assert mark["price_usd"] == 0.0
    assert price_guard.pair_reject_reason(pairs[0], CATNIP_MINT) == "low_liq"
    blob = caplog.text
    assert "price_guard drain" in blob
    assert CATNIP_TID in blob
    assert "TYPESAFE" not in blob

    import young_trail
    import young_trail_fast
    young_trail.reset()
    young_trail_fast.reset()
    young_trail.set_time_fn(lambda: T0)
    young_trail_fast.set_time_fn(lambda: T0)
    assert young_trail.enter(
        CATNIP_TID, {"ticker": "CAT", "chain": "solana"}, CATNIP_ENTRY, now=T0,
    )
    n = young_trail_fast.sync_entries(now=T0)
    assert n >= 1
    stats = young_trail.mark_open({CATNIP_TID: 0.0}, now=T0 + 60)
    assert stats["closed"] == 1
    row = young_trail.closed_rows()[0]
    assert row["exit_reason"] == "sl"
    assert row["exit_price"] == pytest.approx(0.0)
    assert row["pnl_usd"] < 0
    young_trail_fast.on_marks({CATNIP_TID: 0.0}, now=T0 + 60)
    fast_closed = [
        r for r in young_trail_fast.closed_rows(limit=None) if r["tid"] == CATNIP_TID
    ]
    assert fast_closed
    assert {r["exit_reason"] for r in fast_closed} <= {"sl", "trail"}
    assert all(r["exit_price"] == pytest.approx(0.0) for r in fast_closed)
    young_trail.reset()
    young_trail_fast.reset()


def test_pumpfun_low_mcap_upward_quote_is_not_drained():
    """pump.fun, no liq field, $900 mcap, 9e-7 after a 5e-7 crash anchor.

    Failed low_mcap must skip, never drain-at-0, so the last price is kept.
    """
    mint = "PumpLowMcap11111111111111111111111111111"
    tid = f"{mint}:1399811149"
    entry = 4e-6
    prev = 5e-7
    quote = 9e-7
    pair = _pumpfun_pair(mint, quote, market_cap=900)
    assert "liquidity" not in pair
    assert price_guard.is_pumpfun(pair)
    assert price_guard.pair_liq_reported(pair) is None
    assert price_guard.pair_reject_reason(pair, mint) == "low_mcap"
    assert quote > price_guard.CRASH_MAX_X_PREV * prev
    mark = price_guard.mark_from_pairs(
        [pair], mint, entry=entry, prev=prev, tid=tid, now=T0,
    )
    assert mark is None  # skipped; never recorded as drained


def test_upward_low_liq_without_anchor_still_skipped():
    mint = "NoAnchorMint111"
    mark = price_guard.mark_from_pairs(
        [_pair(mint, 0.009, 0.02, dexId="pumpswap")], mint, tid="x:1399811149",
    )
    assert mark is None


def test_single_pool_spike_accepted_after_two_consecutive_confirms():
    mint = "SoloMint111"
    tid = f"{mint}:1399811149"
    entry = 1.0
    spike = 25.0
    first = [_pair(mint, spike, 8_000, dexId="raydium")]
    assert price_guard.mark_from_pairs(
        first, mint, entry=entry, prev=entry, tid=tid, now=T0,
    ) is None
    # Same timestamp must not confirm (variants/fast share a tick).
    assert price_guard.mark_from_pairs(
        first, mint, entry=entry, prev=entry, tid=tid, now=T0,
    ) is None
    second = [_pair(mint, spike * 1.1, 8_000, dexId="raydium")]
    mark = price_guard.mark_from_pairs(
        second, mint, entry=entry, prev=entry, tid=tid, now=T0 + 60,
    )
    assert mark is not None
    assert mark["price_usd"] == pytest.approx(spike * 1.1)

    # Same via accept_price (paper ledgers). Fresh tid so pending is empty.
    tid2 = "solo2:1399811149"
    assert price_guard.accept_price(
        25.0, entry=1.0, prev=1.0, tid=tid2, now=T0,
    ) is None
    assert price_guard.accept_price(
        24.0, entry=1.0, prev=1.0, tid=tid2, now=T0 + 60,
    ) == pytest.approx(24.0)


def test_spike_needs_second_source_within_30pct():
    mint = "Mint222"
    entry = 1.0
    spike = 25.0  # 25× entry
    only = [_pair(mint, spike, 8_000)]
    assert price_guard.mark_from_pairs(only, mint, entry=entry, tid="t") is None
    disagree = [
        _pair(mint, spike, 8_000),
        _pair(mint, 1.1, 4_000),  # not within ±30% of 25
    ]
    assert price_guard.mark_from_pairs(disagree, mint, entry=entry, tid="t") is None
    confirm = [
        _pair(mint, spike, 8_000),
        _pair(mint, 25.0 * 1.2, 3_000),  # +20% of spike
    ]
    mark = price_guard.mark_from_pairs(confirm, mint, entry=entry, tid="t")
    assert mark["price_usd"] == pytest.approx(25.0)

    # 10× previous mark, still under 20× entry, needs confirm.
    prev_spike = [_pair(mint, 11.0, 8_000)]
    assert price_guard.mark_from_pairs(
        prev_spike, mint, entry=1.0, prev=1.0, tid="t",
    ) is None


def test_young_trail_skips_spike_never_uses_for_peak_or_exit():
    import young_trail
    young_trail.reset()
    young_trail.set_time_fn(lambda: T0)
    tid = CATNIP_TID
    assert young_trail.enter(tid, {"ticker": "CAT", "chain": "solana"}, CATNIP_ENTRY, now=T0)
    stats = young_trail.mark_open({tid: CATNIP_SPIKE}, now=T0 + 60)
    pos = young_trail.open_rows()[0]
    assert stats["closed"] == 0
    assert pos["peak"] == pytest.approx(CATNIP_ENTRY)
    assert pos["last_price"] == pytest.approx(CATNIP_ENTRY)
    # Time exit with no valid mark uses last_price, not the spike.
    gone = young_trail.mark_open({}, now=T0 + 6 * 3600)
    assert gone["closed"] == 1
    closed = young_trail.closed_rows()[0]
    assert closed["exit_reason"] == "time"
    assert closed["exit_price"] == pytest.approx(CATNIP_ENTRY)
    pnl = TICKET * (CATNIP_ENTRY / CATNIP_ENTRY - 1.0)
    assert closed["pnl_usd"] == pytest.approx(pnl)
    assert closed["pnl_usd"] < 1.0  # never the +$499k Catnip close
    young_trail.reset()


def test_variants_fast_helius_skip_rejected_marks():
    import young_trail
    import young_trail_variants
    import young_trail_fast
    import young_trail_helius
    young_trail.reset()
    young_trail_fast.reset()
    young_trail_helius.reset()
    young_trail.set_time_fn(lambda: T0)
    young_trail_fast.set_time_fn(lambda: T0)
    young_trail_helius.set_time_fn(lambda: T0)
    tid = "Tok:1399811149"
    young_trail.enter(tid, {"ticker": "TOK", "chain": "solana"}, 1.0, now=T0)

    young_trail_variants.on_marks({tid: 50.0}, T0 + 60)
    vrow = young_trail_variants.get_variant(tid, "baseline")
    assert vrow["peak"] == pytest.approx(1.0)
    assert vrow["exit_ts"] is None

    n = young_trail_fast.sync_entries(now=T0)
    assert n >= 1
    young_trail_fast.on_marks({tid: 50.0}, now=T0 + 60)
    fopen = young_trail_fast.open_rows("baseline")[0]
    assert fopen["peak"] == pytest.approx(1.0)
    assert fopen["last_price"] == pytest.approx(1.0)

    helius_src = (ROOT / "young_trail_helius.py").read_text()
    assert "price_guard.accept_price" in helius_src
    assert price_guard.accept_price(50.0, entry=1.0, prev=1.0, tid=tid) is None
    young_trail.reset()
    young_trail_fast.reset()
    young_trail_helius.reset()


def test_drained_pool_crash_fires_young_trail_and_fast_stops():
    """Token at 1e-6, pool drains to $1 liq quoting 1e-8 → SL / trail."""
    import young_trail
    import young_trail_fast
    young_trail.reset()
    young_trail_fast.reset()
    young_trail.set_time_fn(lambda: T0)
    young_trail_fast.set_time_fn(lambda: T0)
    mint = "RugMint111111111111111111111111111111111"
    tid = f"{mint}:1399811149"
    entry = 1e-6
    crash = 1e-8
    assert young_trail.enter(tid, {"ticker": "RUG", "chain": "solana"}, entry, now=T0)
    n = young_trail_fast.sync_entries(now=T0)
    assert n >= 1
    pair = _pair(mint, crash, 1.0, dexId="pumpswap")
    assert price_guard.pair_reject_reason(pair, mint) == "low_liq"
    mark = price_guard.mark_from_pairs(
        [pair], mint, entry=entry, prev=entry, tid=tid, now=T0 + 60,
    )
    assert mark is not None
    assert mark["source"] == "crash"
    assert mark["price_usd"] == pytest.approx(crash)
    stats = young_trail.mark_open({tid: crash}, now=T0 + 60)
    assert stats["closed"] == 1
    row = young_trail.closed_rows()[0]
    assert row["exit_reason"] == "sl"
    assert row["exit_price"] == pytest.approx(crash)
    assert row["pnl_usd"] < 0
    young_trail_fast.on_marks({tid: crash}, now=T0 + 60)
    fast_closed = [
        r for r in young_trail_fast.closed_rows(limit=None) if r["tid"] == tid
    ]
    assert fast_closed
    assert {r["exit_reason"] for r in fast_closed} <= {"sl", "trail"}
    assert all(r["exit_price"] == pytest.approx(crash) for r in fast_closed)
    young_trail.reset()
    young_trail_fast.reset()


def test_replay_downward_crash_marks_are_accepted():
    """Replay-style: once-valid tokens, drained pools, downward prints."""
    replay = [
        ("tokA:1399811149", 1.2e-6, 4.1e-7, 1.0, "pumpswap"),
        ("tokB:1399811149", 8e-6, 1e-8, 12.0, "raydium"),
        ("tokC:1399811149", 3e-5, 2.5e-5, 0.4, "pumpswap"),
        ("tokD:1399811149", 1e-4, 2e-5, None, "pumpfun"),
    ]
    accepted = 0
    for tid, prev, px, liq, dex in replay:
        mint = tid.split(":")[0]
        assert px <= 1.3 * prev
        if dex == "pumpfun":
            pair = _pumpfun_pair(mint, px, market_cap=200)
        else:
            pair = _pair(mint, px, liq, dexId=dex)
        mark = price_guard.mark_from_pairs(
            [pair], mint, entry=prev, prev=prev, tid=tid, now=T0,
        )
        assert mark is not None, tid
        assert mark["price_usd"] == pytest.approx(px)
        assert mark["source"] == "crash"
        accepted += 1
    assert accepted == len(replay)
    # Time exit on a crash print uses it, not the stale last_price.
    import young_trail
    assert young_trail.decide_exit(
        1e-6, 1e-6, 9e-7, T0, T0 + 6 * 3600, last_price=1e-6,
    ) == ("time", 9e-7)


def test_drain_mark_zero_fires_stop():
    mint = "DryMint111"
    tid = f"{mint}:1399811149"
    pair = _pair(mint, 0.0, 1.0, dexId="pumpswap")
    mark = price_guard.mark_from_pairs(
        [pair], mint, entry=1e-6, prev=1e-6, tid=tid,
    )
    assert mark is not None
    assert mark["source"] == "drained"
    assert mark["price_usd"] == 0.0
    missing = dict(_pair(mint, 1e-6, 1.0, dexId="pumpswap"))
    missing.pop("priceUsd")
    drained = price_guard.mark_from_pairs(
        [missing], mint, entry=1e-6, prev=1e-6, tid=tid,
    )
    assert drained["source"] == "drained"
    assert drained["price_usd"] == 0.0
    import young_trail
    young_trail.reset()
    young_trail.set_time_fn(lambda: T0)
    young_trail.enter(tid, {"ticker": "DRY", "chain": "solana"}, 1e-6, now=T0)
    stats = young_trail.mark_open({tid: 0.0}, now=T0 + 60)
    assert stats["closed"] == 1
    row = young_trail.closed_rows()[0]
    assert row["exit_reason"] == "sl"
    assert row["exit_price"] == pytest.approx(0.0)
    young_trail.reset()


def test_fast_marks_drain_catnip_and_low_liq(rs_env, caplog):
    import research
    import young_trail_fast
    caplog.set_level(logging.INFO)
    t0 = T0
    research.set_time_fn(lambda: t0)
    young_trail_fast.reset()
    young_trail_fast.set_time_fn(lambda: t0)
    research.on_cycle({
        "seen": 1, "benched": 0, "judged": 0, "requeued": 0,
        "free": {}, "trade": {}, "chain": {}, "soft": {},
        "tokens": [{
            "tid": CATNIP_TID, "ticker": "CAT", "stage": "trade", "reason": "pass",
            "age_minutes": 10, "price_usd": CATNIP_ENTRY, "liquidity_usd": 5_000,
        }],
        "young_free": [],
    }, now=t0)
    import young_trail
    young_trail.enter(
        CATNIP_TID, {"ticker": "CAT", "chain": "solana"}, CATNIP_ENTRY, now=t0,
    )

    def get(url, timeout=None, headers=None, **kw):
        return _DexResp([{
            "priceUsd": str(CATNIP_SPIKE),
            "liquidity": {"usd": 0.02},
            "baseToken": {"address": CATNIP_MINT, "symbol": "CAT"},
            "chainId": "solana",
            "volume": {"m5": 1, "h1": 1, "h24": 1},
        }])

    research.set_get_fn(get)
    out = research.tick_sampler(now=t0 + 30)
    assert out["marks"] == 1
    rows = research.run_sql("SELECT * FROM fast_marks")
    assert len(rows) == 1
    assert rows[0]["price_usd"] == pytest.approx(0.0)
    assert rows[0]["source"] == "drained"
    assert "price_guard drain" in caplog.text
    fast_closed = [
        r for r in young_trail_fast.closed_rows(limit=None) if r["tid"] == CATNIP_TID
    ]
    assert fast_closed
    assert {r["exit_reason"] for r in fast_closed} <= {"sl", "trail"}
    assert all(r["exit_price"] == pytest.approx(0.0) for r in fast_closed)
    young_trail_fast.reset()


class _DexResp:
    def __init__(self, pairs):
        self.status_code = 200
        self._pairs = pairs

    def json(self):
        return {"pairs": self._pairs}


@pytest.fixture
def rs_env(tmp_path, monkeypatch):
    import research
    import young_trail
    monkeypatch.setenv("RESEARCH_DB", str(tmp_path / "research.db"))
    monkeypatch.setenv("RESEARCH_SAMPLER_DISABLE", "1")
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    research.reset()
    research.set_sleep_fn(lambda _d: None)
    research.set_get_fn(lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("no network")
    ))
    young_trail.reset()
    young_trail.set_time_fn(lambda: T0)
    yield research
    research.reset()
    young_trail.reset()
