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


def test_catnip_pumpswap_quote_is_rejected(caplog):
    caplog.set_level(logging.INFO)
    pairs = [_pair(CATNIP_MINT, CATNIP_SPIKE, 0.02, symbol="Catnip")]
    mark = price_guard.mark_from_pairs(
        pairs, CATNIP_MINT, entry=CATNIP_ENTRY, prev=CATNIP_ENTRY, tid=CATNIP_TID,
    )
    assert mark is None
    blob = caplog.text
    assert "price_guard skip" in blob
    assert CATNIP_TID in blob
    assert "TYPESAFE" not in blob


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
    gone = young_trail.mark_open({tid: CATNIP_SPIKE}, now=T0 + 6 * 3600)
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


def test_fast_marks_skip_catnip_and_low_liq(rs_env, caplog):
    import research
    caplog.set_level(logging.INFO)
    t0 = T0
    research.set_time_fn(lambda: t0)
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
    assert out["marks"] == 0
    rows = research.run_sql("SELECT * FROM fast_marks")
    assert rows == []
    assert "price_guard skip" in caplog.text


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
