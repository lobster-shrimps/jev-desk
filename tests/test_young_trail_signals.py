"""Paper RugCheck / boosts / Birdeye features. No gating. No live orders."""
import json
import logging
import os
import pathlib
import sqlite3
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DESK_SECRET", "test-secret-for-yt-signals")
os.environ.setdefault("JUDGE_MOCK", "1")

ENTRY = 1.0
T0 = 1_700_000_000.0
MINT = "MintSignals11111111111111111111111111"
TID = f"{MINT}:1399811149"
TID_BSC = "0xabcde00000000000000000000000000000000001:56"

RUGCHECK_DANGER = {
    "score": 8500,
    "score_normalised": 85,
    "risks": [
        {"name": "Rug pull", "level": "danger", "score": 5000},
        {"name": "Low LP", "level": "warn"},
    ],
    "tokenMeta": {
        "mintAuthority": "SomeMintAuth111111111111111111111",
        "freezeAuthority": None,
        "name": "SCAM",
        "symbol": "SCAM",
    },
    "mintAuthority": "SomeMintAuth111111111111111111111",
    "freezeAuthority": None,
    "topHolders": [{"pct": 40.0, "insider": True}],
    "markets": [{"lp": {"lpLockedPct": 10}, "lpLockedPct": 10}],
    "graphInsidersDetected": 2,
    "insiderNetworks": [{"id": 1}],
    "creatorTokens": [{"mint": "x"}],
}

RUGCHECK_CLEAN = {
    "score": 100,
    "score_normalised": 1,
    "risks": [{"name": "Mutable metadata", "level": "info"}],
    "mintAuthority": None,
    "freezeAuthority": None,
    "tokenMeta": {"mintAuthority": None, "freezeAuthority": None},
    "topHolders": [{"pct": 5.0}],
    "markets": [{"lpLockedPct": 100, "lp": {"lpLockedPct": 100}}],
    "graphInsidersDetected": 0,
    "insiderNetworks": [],
}

RUGCHECK_SUMMARY_ONLY = {
    "score": 12,
    "score_normalised": 12,
    "risks": [{"name": "Mutable metadata", "level": "info"}],
    "tokenMeta": {"name": "X", "symbol": "X"},
}

# Live free summary: no mintAuthority / freezeAuthority / topHolders.
RUGCHECK_LIVE = {
    "tokenProgram": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "tokenType": "standard",
    "risks": [
        {"name": "Mint Authority still enabled", "value": "",
         "description": "The mint authority is not revoked", "score": 2000,
         "level": "warn"},
        {"name": "Freeze Authority still enabled", "value": "",
         "description": "The freeze authority is not revoked", "score": 1000,
         "level": "warn"},
        {"name": "Top 10 holders high ownership", "value": "80%",
         "description": "Top 10 hold a large share", "score": 2000,
         "level": "warn"},
        {"name": "Rug pull", "value": "", "description": "danger",
         "score": 5000, "level": "danger"},
    ],
    "score": 8500,
    "score_normalised": 85,
    "lpLockedPct": 10,
}

RUGCHECK_LIVE_CLEAN = {
    "tokenProgram": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "tokenType": "standard",
    "risks": [
        {"name": "Mutable metadata", "value": "Yes",
         "description": "Metadata can change", "score": 100, "level": "info"},
    ],
    "score": 100,
    "score_normalised": 1,
    "lpLockedPct": 100,
}

RUGCHECK_LIVE_SINGLE = {
    "tokenProgram": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "tokenType": "standard",
    "risks": [
        {"name": "Single holder ownership", "value": "45%",
         "description": "One wallet holds a large share", "score": 2000,
         "level": "warn"},
    ],
    "score": 2000,
    "score_normalised": 20,
    "lpLockedPct": 80,
}

RUGCHECK_NO_RISKS_KEY = {
    "tokenProgram": "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
    "tokenType": "standard",
    "score": 1,
    "score_normalised": 1,
    "lpLockedPct": 100,
}

BIRDEYE_SPENT = {
    "success": True,
    "data": {
        "holder": 500,
        "priceChange30mPercent": 10,
        "priceChange1hPercent": 80,
        "buy30m": 20,
        "sell30m": 40,
        "vBuy30mUSD": 1000,
        "vSell30mUSD": 3000,
        "uniqueWallet30m": 15,
        "buy1h": 100,
        "sell1h": 80,
        "vBuy1hUSD": 5000,
        "vSell1hUSD": 4000,
        "uniqueWallet1h": 40,
    },
}

BIRDEYE_FRESH = {
    "success": True,
    "data": {
        "holder": 80,
        "priceChange30mPercent": 5,
        "priceChange1hPercent": 8,
        "buy30m": 40,
        "sell30m": 10,
        "vBuy30mUSD": 2000,
        "vSell30mUSD": 400,
        "uniqueWallet30m": 30,
        "buy1h": 90,
        "sell1h": 20,
        "vBuy1hUSD": 4000,
        "vSell1hUSD": 800,
        "uniqueWallet1h": 50,
    },
}


class FakeResp:
    def __init__(self, status_code, data, headers=None):
        self.status_code = status_code
        self._data = data
        self.headers = headers or {}

    def json(self):
        return self._data


def _scripted(rug=None, boosts=None, orders=None, birdeye=None, status=None):
    """Mock HTTP. Counts calls. Never hits the network."""
    rug = RUGCHECK_CLEAN if rug is None else rug
    boosts = [{"chainId": "solana", "tokenAddress": MINT, "amount": 0,
               "totalAmount": 0}] if boosts is None else boosts
    orders = [] if orders is None else orders
    birdeye = BIRDEYE_FRESH if birdeye is None else birdeye
    status = status or {}
    calls = []

    def http(url, headers=None, timeout=None):
        calls.append({"url": url, "headers": dict(headers or {})})
        u = url or ""
        if "rugcheck" in u:
            return FakeResp(status.get("rugcheck", 200), rug)
        if "token-boosts" in u:
            return FakeResp(status.get("boosts", 200), boosts)
        if "/orders/v1/" in u:
            return FakeResp(status.get("orders", 200), orders)
        if "birdeye" in u:
            return FakeResp(status.get("birdeye", 200), birdeye,
                            headers=status.get("birdeye_headers") or {})
        return FakeResp(404, {})

    http.calls = calls
    return http


def _enter(yt, ticker="SIG", tid=TID, chain="solana", now=T0):
    db = yt._connect()
    db.execute(
        """INSERT OR REPLACE INTO young_trail (
             tid, ticker, chain, entry_ts, entry_price, peak,
             last_price, last_mark_ts
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
        (tid, ticker, chain, now, ENTRY, ENTRY, ENTRY, now),
    )
    db.commit()


@pytest.fixture
def yts(monkeypatch, tmp_path):
    monkeypatch.setenv("OUTCOMES_DB", str(tmp_path / "outcomes.db"))
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    monkeypatch.delenv("BIRDEYE_API_KEY", raising=False)
    monkeypatch.delenv("RUGCHECK_API_KEY", raising=False)
    monkeypatch.delenv("YOUNG_TRAIL_SIGNALS_UNTIL", raising=False)
    import young_trail
    import young_trail_signals
    young_trail.reset()
    young_trail.set_time_fn(lambda: T0)
    young_trail.set_sleep_fn(lambda _d: None)
    young_trail.set_get_fn(lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("signals tests must not use young_trail http")
    ))
    young_trail_signals.reset()
    young_trail_signals.set_time_fn(lambda: T0)
    young_trail_signals.set_sleep_fn(lambda _d: None)
    young_trail_signals.set_http_fn(_scripted())
    yield young_trail, young_trail_signals
    young_trail_signals.reset()
    young_trail_signals.set_http_fn(None)
    young_trail.reset()


def test_parse_rugcheck_summary_and_full():
    import young_trail_signals as s
    full = s.parse_rugcheck(RUGCHECK_DANGER)
    assert full["danger_risk"] == 1
    assert full["rugcheck_score_norm"] == pytest.approx(85)
    assert full["lp_locked_pct"] == pytest.approx(10)
    assert full["mint_authority"] == 1
    assert full["freeze_authority"] == 0
    assert full["top_holder_pct"] == pytest.approx(40)
    assert full["insider_flag"] == 1
    assert full["creator_flag"] == 1
    assert any(r["level"] == "danger" for r in full["risks"])
    clean = s.parse_rugcheck(RUGCHECK_CLEAN)
    assert clean["danger_risk"] == 0
    assert clean["mint_authority"] == 0
    assert clean["freeze_authority"] == 0
    assert clean["lp_locked_pct"] == pytest.approx(100)
    assert clean["insider_flag"] == 0
    summary = s.parse_rugcheck(RUGCHECK_SUMMARY_ONLY)
    assert summary["rugcheck_score_norm"] == pytest.approx(12)
    assert summary["danger_risk"] == 0
    assert summary["lp_locked_pct"] is None
    assert summary["mint_authority"] == 0
    assert summary["freeze_authority"] == 0
    assert summary["top_holder_flag"] == 0
    empty = s.parse_rugcheck({})
    assert empty["danger_risk"] is None
    assert empty["mint_authority"] is None
    assert empty["freeze_authority"] is None


def test_parse_rugcheck_live_summary_derives_authority_from_risks():
    import young_trail_signals as s
    live = s.parse_rugcheck(RUGCHECK_LIVE)
    assert live["mint_authority"] == 1
    assert live["freeze_authority"] == 1
    assert live["top_holder_flag"] == 1
    assert live["danger_risk"] == 1
    assert live["lp_locked_pct"] == pytest.approx(10)
    assert live["rugcheck_score_norm"] == pytest.approx(85)
    flags = s.skip_flags({**live, "status": "ok"})
    assert flags["skip_authority"] is True
    assert flags["skip_lp"] is True
    assert flags["skip_danger"] is True
    clean = s.parse_rugcheck(RUGCHECK_LIVE_CLEAN)
    assert clean["mint_authority"] == 0
    assert clean["freeze_authority"] == 0
    assert clean["top_holder_flag"] == 0
    assert clean["danger_risk"] == 0
    assert clean["lp_locked_pct"] == pytest.approx(100)
    cf = s.skip_flags({**clean, "status": "ok"})
    assert cf["skip_authority"] is False
    single = s.parse_rugcheck(RUGCHECK_LIVE_SINGLE)
    assert single["mint_authority"] == 0
    assert single["freeze_authority"] == 0
    assert single["top_holder_flag"] == 1
    mixed_case = s.parse_rugcheck({
        "tokenProgram": "Tokenkeg",
        "tokenType": "standard",
        "risks": [
            {"name": "MINT AUTHORITY still enabled", "level": "warn", "score": 1,
             "value": "", "description": ""},
            {"name": "freeze Authority", "level": "warn", "score": 1,
             "value": "", "description": ""},
        ],
        "score": 10,
        "score_normalised": 10,
        "lpLockedPct": 60,
    })
    assert mixed_case["mint_authority"] == 1
    assert mixed_case["freeze_authority"] == 1
    none = s.parse_rugcheck(RUGCHECK_NO_RISKS_KEY)
    assert none["mint_authority"] is None
    assert none["freeze_authority"] is None
    assert none["top_holder_flag"] is None
    assert none["danger_risk"] is None
    empty_risks = s.parse_rugcheck({
        "tokenProgram": "Tokenkeg", "tokenType": "standard",
        "risks": [], "score": 1, "score_normalised": 1, "lpLockedPct": 100,
    })
    assert empty_risks["mint_authority"] == 0
    assert empty_risks["freeze_authority"] == 0
    assert empty_risks["top_holder_flag"] == 0
    assert empty_risks["danger_risk"] == 0


def test_parse_boosts_and_orders():
    import young_trail_signals as s
    idx = s.parse_boosts([
        {"chainId": "solana", "tokenAddress": MINT, "amount": 10, "totalAmount": 50},
        {"chainId": "solana", "tokenAddress": MINT, "amount": 4, "totalAmount": 80},
    ])
    rec = idx[("solana", MINT.lower())]
    assert rec["amount"] == pytest.approx(10)
    assert rec["total"] == pytest.approx(80)
    paid = s.parse_orders([
        {"type": "tokenAd", "status": "approved", "paymentTimestamp": 1728000000},
        {"type": "tokenProfile", "status": "rejected", "paymentTimestamp": 1},
    ])
    assert paid["paid_promo"] is True
    assert len(paid["orders"]) == 2
    none = s.parse_orders([])
    assert none["paid_promo"] is False
    none2 = s.parse_orders([{"type": "tokenAd", "status": "cancelled"}])
    assert none2["paid_promo"] is False


def test_parse_birdeye():
    import young_trail_signals as s
    p = s.parse_birdeye(BIRDEYE_SPENT)
    assert p["holder_count"] == 500
    assert p["buy_30m"] == 20
    assert p["sell_30m"] == 40
    assert p["price_change_1h"] == pytest.approx(80)
    assert p["unique_wallets_30m"] == 15
    assert p["vol_buy_1h"] == pytest.approx(5000)


def test_skip_flags_per_rule():
    import young_trail_signals as s
    danger = s.parse_rugcheck(RUGCHECK_DANGER)
    danger["status"] = "ok"
    danger["paid_promo"] = 0
    danger["boost_amount"] = 0
    flags = s.skip_flags(danger)
    assert flags["skip_danger"] is True
    assert flags["skip_lp"] is True
    assert flags["skip_authority"] is True
    assert flags["skip_score"] is True
    clean = s.parse_rugcheck(RUGCHECK_CLEAN)
    clean["status"] = "ok"
    clean["paid_promo"] = 0
    clean["boost_amount"] = 0
    cf = s.skip_flags(clean)
    assert cf["skip_danger"] is False
    assert cf["skip_lp"] is False
    assert cf["skip_authority"] is False
    assert cf["skip_score"] is False
    unknown = s.parse_rugcheck(RUGCHECK_SUMMARY_ONLY)
    unknown["status"] = "ok"
    uf = s.skip_flags(unknown)
    assert uf["skip_lp"] is None
    assert uf["skip_authority"] is False
    promo = {"status": "ok", "paid_promo": 1, "boost_amount": 0}
    assert s.skip_flags(promo)["skip_promo"] is True
    boost = {"status": "ok", "paid_promo": 0, "boost_amount": 5}
    assert s.skip_flags(boost)["skip_promo"] is True
    spent = {
        "status": "ok", "price_change_1h": 80, "buy_30m": 10, "sell_30m": 20,
        "birdeye_skipped": 0,
    }
    assert s.skip_flags(spent)["skip_momentum"] is True
    fresh = {
        "status": "ok", "price_change_1h": 8, "buy_30m": 40, "sell_30m": 10,
        "birdeye_skipped": 0,
    }
    assert s.skip_flags(fresh)["skip_momentum"] is False
    skipped_key = {
        "status": "ok", "price_change_1h": 80, "buy_30m": 10, "sell_30m": 20,
        "birdeye_skipped": 1,
    }
    assert s.skip_flags(skipped_key)["skip_momentum"] is None
    pending = {"status": "pending", "danger_risk": 1}
    assert s.skip_flags(pending)["skip_danger"] is None


def test_counterfactual_skipped_vs_kept_pnl(yts):
    yt, sig = yts
    rows = [
        {"tid": "a:1", "status": "ok", "skip_danger": 1, "danger_risk": 1},
        {"tid": "b:1", "status": "ok", "skip_danger": 0, "danger_risk": 0},
        {"tid": "c:1", "status": "ok", "skip_danger": None, "danger_risk": None},
        {"tid": "d:1", "status": "pending"},
    ]
    maps = {
        "young_trail": {"a:1": -25.0, "b:1": 10.0, "c:1": 99.0},
        "fast_baseline": {"a:1": -20.0, "b:1": 8.0},
        "fast_tp50": {"a:1": -15.0, "b:1": 12.0},
    }
    cf = sig.counterfactual_for_rule("danger_risk", rows, maps)
    assert cf["skipped"]["n"] == 1
    assert cf["skipped"]["young_trail_pnl"] == pytest.approx(-25.0)
    assert cf["kept"]["n"] == 1
    assert cf["kept"]["young_trail_pnl"] == pytest.approx(10.0)
    assert cf["unknown_n"] == 2
    assert cf["skipped"]["fast_baseline_pnl"] == pytest.approx(-20.0)
    assert cf["kept"]["fast_tp50_pnl"] == pytest.approx(12.0)


def test_cache_hit_skips_http(yts):
    yt, sig = yts
    http = _scripted(rug=RUGCHECK_CLEAN)
    sig.set_http_fn(http)
    a = sig.fetch_rugcheck(MINT, now=T0)
    n1 = len(http.calls)
    b = sig.fetch_rugcheck(MINT, now=T0 + 60)
    n2 = len(http.calls)
    assert a["lp_locked_pct"] == pytest.approx(100)
    assert b["lp_locked_pct"] == pytest.approx(100)
    assert n2 == n1
    sig.fetch_rugcheck(MINT, now=T0 + 31 * 60)
    assert len(http.calls) == n1 + 1


def test_rugcheck_rate_limit_three_per_sec(yts):
    yt, sig = yts
    sleeps = []
    sig.set_sleep_fn(lambda d: sleeps.append(d))
    clock = {"t": T0}

    def now():
        return clock["t"]

    sig.set_time_fn(now)
    http = _scripted()
    sig.set_http_fn(http)
    sig.fetch_rugcheck("mintA", now=T0)
    clock["t"] = T0 + 0.05
    sig.fetch_rugcheck("mintB", now=T0 + 0.05)
    assert sleeps
    assert sleeps[0] == pytest.approx(sig.RUGCHECK_MIN_INTERVAL - 0.05)


def test_birdeye_429_backoff(yts, monkeypatch):
    yt, sig = yts
    monkeypatch.setenv("BIRDEYE_API_KEY", "bird-secret-key-xxxxx")
    clock = {"t": T0}
    sig.set_time_fn(lambda: clock["t"])
    http = _scripted(
        birdeye={"error": "rate"},
        status={"birdeye": 429, "birdeye_headers": {"Retry-After": "2"}},
    )
    sig.set_http_fn(http)
    first = sig.fetch_birdeye(MINT, "solana", now=T0)
    assert first is None
    n1 = len([c for c in http.calls if "birdeye" in c["url"]])
    second = sig.fetch_birdeye(MINT, "solana", now=T0 + 0.2)
    n2 = len([c for c in http.calls if "birdeye" in c["url"]])
    assert second is None
    assert n2 == n1
    clock["t"] = T0 + 5
    http2 = _scripted(birdeye=BIRDEYE_FRESH)
    sig.set_http_fn(http2)
    third = sig.fetch_birdeye(MINT, "solana", now=T0 + 5)
    assert third is not None
    assert third["holder_count"] == 80


def test_missing_birdeye_key_skips_silently(yts, caplog, monkeypatch):
    yt, sig = yts
    monkeypatch.delenv("BIRDEYE_API_KEY", raising=False)
    caplog.set_level(logging.INFO)
    http = _scripted()
    sig.set_http_fn(http)
    _enter(yt)
    sig.on_sampler_tick({}, now=T0)
    bird_calls = [c for c in http.calls if "birdeye" in c["url"]]
    assert bird_calls == []
    row = sig.all_rows()[0]
    assert row["birdeye_skipped"] == 1
    assert row["skip_momentum"] is None
    blob = caplog.text.lower()
    assert "birdeye_api_key" not in blob
    assert "missing birdeye" not in blob


def test_rugcheck_optional_key_header(yts, monkeypatch):
    yt, sig = yts
    monkeypatch.setenv("RUGCHECK_API_KEY", "rug-secret-key-xxxxx")
    http = _scripted()
    sig.set_http_fn(http)
    sig.fetch_rugcheck(MINT, now=T0)
    rug_calls = [c for c in http.calls if "rugcheck" in c["url"]]
    assert rug_calls
    assert rug_calls[0]["headers"].get("X-API-KEY") == "rug-secret-key-xxxxx"


def test_boosts_fetched_once_per_cycle(yts):
    yt, sig = yts
    http = _scripted(boosts=[
        {"chainId": "solana", "tokenAddress": MINT, "amount": 7, "totalAmount": 21},
    ])
    sig.set_http_fn(http)
    sig.begin_cycle(T0)
    n1 = len([c for c in http.calls if "token-boosts" in c["url"]])
    assert n1 == 2
    sig.begin_cycle(T0 + 10)
    n2 = len([c for c in http.calls if "token-boosts" in c["url"]])
    assert n2 == n1
    rec = sig.lookup_boost("solana", MINT)
    assert rec["amount"] == pytest.approx(7)
    assert rec["boosted"] is True


def test_sampler_computes_and_never_gates(yts):
    yt, sig = yts
    http = _scripted(
        rug=RUGCHECK_DANGER,
        boosts=[{"chainId": "solana", "tokenAddress": MINT, "amount": 9, "totalAmount": 9}],
        orders=[{"type": "tokenAd", "status": "approved", "paymentTimestamp": 1}],
    )
    sig.set_http_fn(http)
    _enter(yt)
    sig.on_sampler_tick({}, now=T0)
    row = sig.all_rows()[0]
    assert row["status"] == "ok"
    assert row["skip_danger"] == 1
    assert row["skip_lp"] == 1
    assert row["skip_authority"] == 1
    assert row["skip_score"] == 1
    assert row["skip_promo"] == 1
    opens = yt.open_rows()
    assert len(opens) == 1
    assert opens[0]["exit_ts"] is None
    payload = sig.ops_payload()
    assert payload["gates_entries"] is False
    assert payload["live_orders"] is False
    assert payload["paper"] is True
    assert payload["ticket_usd"] == pytest.approx(50.0)


def test_non_solana_skips_rugcheck_and_birdeye(yts, monkeypatch):
    yt, sig = yts
    monkeypatch.setenv("BIRDEYE_API_KEY", "bird-secret-key-xxxxx")
    http = _scripted(orders=[])
    sig.set_http_fn(http)
    _enter(yt, ticker="BSC", tid=TID_BSC, chain="bsc")
    sig.on_sampler_tick({}, now=T0)
    row = sig.all_rows()[0]
    assert row["tid"] == TID_BSC
    assert row["status"] == "ok"
    assert row["rugcheck_score"] is None
    assert row["skip_danger"] is None
    rug_calls = [c for c in http.calls if "rugcheck" in c["url"]]
    bird_calls = [c for c in http.calls if "birdeye" in c["url"]]
    assert rug_calls == []
    assert bird_calls == []
    assert any("/orders/v1/bsc/" in c["url"] for c in http.calls)


def test_note_candidate_queues_without_http(yts):
    yt, sig = yts
    http = _scripted(rug=RUGCHECK_CLEAN)
    sig.set_http_fn(http)
    queued = sig.note_candidate({
        "tid": TID, "ticker": "SIG", "chain": "solana", "net": 1399811149,
        "address": MINT,
    }, now=T0)
    assert queued is not None
    assert queued["tid"] == TID
    assert queued["mint"] == MINT
    assert queued["chain"] == "solana"
    assert http.calls == []
    assert sig.all_rows() == []
    pending = sig.pending_candidates()
    assert len(pending) == 1
    sig.on_sampler_tick({}, now=T0)
    assert sig.all_rows() == []
    assert any("rugcheck" in c["url"] for c in http.calls)
    _enter(yt)
    sig.on_sampler_tick({}, now=T0)
    rows = sig.all_rows()
    assert len(rows) == 1
    assert rows[0]["tid"] == TID


def test_note_candidate_never_raises_or_fetches(yts):
    yt, sig = yts
    called = []

    def boom(*_a, **_k):
        called.append(1)
        raise RuntimeError("network down")

    sig.set_http_fn(boom)
    sig.set_sleep_fn(lambda d: called.append(d) or (_ for _ in ()).throw(
        RuntimeError("sleep on cycle thread")
    ))
    out = sig.note_candidate({"tid": TID, "ticker": "X", "chain": "solana"}, now=T0)
    assert out is None or isinstance(out, dict)
    assert called == []


def test_keys_never_logged(yts, monkeypatch, caplog):
    yt, sig = yts
    secret = "bird-SUPERSECRET-key99"
    rug_secret = "rug-SUPERSECRET-key99"
    monkeypatch.setenv("BIRDEYE_API_KEY", secret)
    monkeypatch.setenv("RUGCHECK_API_KEY", rug_secret)
    caplog.set_level(logging.WARNING)

    def boom(url, headers=None, timeout=None):
        raise ConnectionError(f"Failed {url} key={secret} {rug_secret}")

    sig.set_http_fn(boom)
    _enter(yt)
    sig.on_sampler_tick({}, now=T0)
    blob = caplog.text + json.dumps(sig.ops_payload())
    assert secret not in blob
    assert rug_secret not in blob
    src = (ROOT / "young_trail_signals.py").read_text()
    for line in src.splitlines():
        if line.strip().startswith("log.") and ("BIRDEYE_API_KEY" in line or "RUGCHECK_API_KEY" in line):
            if "os.environ" in line:
                raise AssertionError(f"logs key env: {line}")
    assert "print(" not in src.split("def main")[0]


def test_http_failure_via_requests_safe_err(yts, monkeypatch, caplog):
    yt, sig = yts
    monkeypatch.setenv("BIRDEYE_API_KEY", "bird-LEAKME99-xxxxxx")
    caplog.set_level(logging.WARNING)
    sig.set_http_fn(None)
    _enter(yt)

    def fake_get(url, headers=None, timeout=None):
        raise ConnectionError(f"Failed to resolve {url} token=bird-LEAKME99-xxxxxx")

    from unittest.mock import patch
    with patch("requests.get", side_effect=fake_get):
        sig.compute_features(MINT, chain="solana", now=T0)
    assert "LEAKME99" not in caplog.text
    assert "young_trail_signals" in caplog.text


def test_schema_idempotent_and_pragma_before_alter(yts, tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.WARNING)
    yt, sig = yts
    db = sig._connect()
    names = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "young_trail_signals" in names
    assert "young_trail_signals_meta" in names
    assert "young_trail_signals_cache" in names
    sig._add_column(db, "young_trail_signals", "lp_burned_pct", "REAL")
    sig._add_column(db, "young_trail_signals", "lp_burned_pct", "REAL")
    sig.init_schema()
    sig.init_schema()
    cols = sig._table_columns(db, "young_trail_signals")
    assert "lp_burned_pct" in cols
    assert "duplicate column" not in caplog.text.lower()
    src = (ROOT / "young_trail_signals.py").read_text()
    add = src.split("def _add_column")[1].split("\ndef ")[0]
    assert "PRAGMA table_info" in src.split("def _table_columns")[1].split("\ndef ")[0]
    assert add.find("_table_columns") < add.find("ALTER TABLE")


def test_schema_safe_on_live_db(tmp_path, monkeypatch):
    path = tmp_path / "live_outcomes.db"
    db = sqlite3.connect(path)
    db.execute(
        """CREATE TABLE young_trail (
             tid TEXT PRIMARY KEY, ticker TEXT, chain TEXT,
             entry_ts REAL, entry_price REAL, peak REAL,
             last_price REAL, last_mark_ts REAL,
             exit_ts REAL, exit_price REAL, exit_reason TEXT, pnl_usd REAL
           )"""
    )
    db.execute(
        "INSERT INTO young_trail (tid, ticker, entry_ts, entry_price, peak) "
        "VALUES ('keep:1', 'KEEP', 1, 2.5, 2.5)"
    )
    db.commit()
    db.close()
    monkeypatch.setenv("OUTCOMES_DB", str(path))
    import young_trail_signals
    young_trail_signals.reset()
    conn = young_trail_signals._connect()
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "young_trail_signals" in names
    keep = conn.execute(
        "SELECT ticker, entry_price FROM young_trail WHERE tid='keep:1'"
    ).fetchone()
    assert keep["ticker"] == "KEEP"
    assert keep["entry_price"] == pytest.approx(2.5)
    young_trail_signals.init_schema(conn)
    young_trail_signals.init_schema(conn)
    young_trail_signals.reset()


def test_until_matches_young_trail_oct20(yts):
    yt, sig = yts
    assert sig.until_ts(T0) == pytest.approx(yt.until_ts(T0))
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(sig.until_ts(T0)))
    assert iso == "2026-10-21T03:59:59Z"
    assert sig.TICKET_USD == pytest.approx(50.0)


def test_ops_payload_and_endpoint(yts):
    yt, sig = yts
    http = _scripted(rug=RUGCHECK_DANGER, orders=[
        {"type": "tokenAd", "status": "approved", "paymentTimestamp": 1},
    ])
    sig.set_http_fn(http)
    _enter(yt)
    sig.on_sampler_tick({}, now=T0)
    db = yt._connect()
    db.execute(
        """UPDATE young_trail SET exit_ts = ?, exit_price = ?, exit_reason = 'stop',
           pnl_usd = ? WHERE tid = ?""",
        (T0 + 60, 0.5, -25.0, TID),
    )
    db.commit()
    payload = sig.ops_payload()
    assert payload["paper"] is True
    assert payload["gates_entries"] is False
    assert payload["summary"]["ok"] == 1
    danger = next(c for c in payload["counterfactuals"] if c["id"] == "danger_risk")
    assert danger["skipped"]["n"] == 1
    assert danger["skipped"]["young_trail_pnl"] == pytest.approx(-25.0)
    from fastapi.testclient import TestClient
    import server
    resp = TestClient(server.app).get("/ops/young_trail_signals")
    assert resp.status_code == 200
    data = resp.json()
    assert data["paper"] is True
    assert data["gates_entries"] is False
    lines = sig.briefing_lines()
    assert any("SKIPPED" in ln and "KEPT" in ln for ln in lines)


def test_ops_html_has_signals_section():
    html = (ROOT / "ops.html").read_text()
    assert 'id="yt-signals"' in html
    assert "/ops/young_trail_signals" in html
    assert "https://" not in html
    ytr = html.find('id="yt-rugrisk"')
    yts = html.find('id="yt-signals"')
    assert ytr != -1 and yts != -1
    assert ytr < yts


def test_sampler_order_rugrisk_then_signals():
    src = (ROOT / "research.py").read_text()
    r = src.find("young_trail_rugrisk.on_sampler_tick")
    s = src.find("young_trail_signals.on_sampler_tick")
    assert r != -1 and s != -1
    assert r < s


def test_note_candidate_source_is_queue_only():
    src = (ROOT / "young_trail_signals.py").read_text()
    body = src.split("def note_candidate")[1].split("\ndef ")[0]
    assert "compute_features" not in body
    assert "_http_get" not in body
    assert "begin_cycle" not in body
    assert "ensure_boosts" not in body
    assert "fetch_rugcheck" not in body
    assert "fetch_orders" not in body
    assert "time.sleep" not in body


def test_main_notes_candidate_after_dossier_does_not_gate():
    src = (ROOT / "main.py").read_text()
    dnone = src.find("if d is None:")
    note = src.find("young_trail_signals.note_candidate")
    kill = src.find("if (k := chain_kill(d)):")
    assert dnone != -1 and note != -1 and kill != -1
    assert dnone < note < kill
    window = src[note:kill]
    assert "continue" not in window


def test_no_gating_imports_or_protected_files():
    for name in (
        "thresholds.py", "filter.py",
        "collect.py", "judge.py", "young_trail.py", "young_trail_fast.py",
        "young_trail_helius.py", "young_trail_variants.py",
        "young_trail_rugrisk.py",
    ):
        text = (ROOT / name).read_text()
        assert "young_trail_signals" not in text, name
    for name in (
        "thresholds.py", "filter.py",
        "judge.py",
    ):
        r = subprocess.run(
            ["git", "diff", "--", name],
            cwd=str(ROOT), capture_output=True, text=True, check=True,
        )
        assert r.stdout == "", name


def test_unmarked_pnl_excluded_from_counterfactual(yts):
    yt, sig = yts
    _enter(yt)
    http = _scripted(rug=RUGCHECK_DANGER)
    sig.set_http_fn(http)
    sig.on_sampler_tick({}, now=T0)
    db = yt._connect()
    db.execute(
        """UPDATE young_trail SET exit_ts = ?, exit_price = ?, exit_reason = 'unmarked',
           pnl_usd = ? WHERE tid = ?""",
        (T0 + 60, 0.5, -25.0, TID),
    )
    db.commit()
    payload = sig.ops_payload()
    danger = next(c for c in payload["counterfactuals"] if c["id"] == "danger_risk")
    assert danger["skipped"]["n"] == 1
    assert danger["skipped"]["young_trail_pnl"] == pytest.approx(0.0)
    assert danger["skipped"]["n_with_pnl"]["young_trail"] == 0


def test_cycle_cap_skips_extra_rugcheck(yts, monkeypatch):
    yt, sig = yts
    monkeypatch.setenv("YOUNG_TRAIL_SIGNALS_RUGCHECK_CAP", "1")
    http = _scripted()
    sig.set_http_fn(http)
    sig.begin_cycle(T0)
    sig.fetch_rugcheck("mintA", now=T0)
    sig.fetch_rugcheck("mintB", now=T0 + 1)
    rug = [c for c in http.calls if "rugcheck" in c["url"]]
    assert len(rug) == 1


def test_run_once_hanging_http_adds_no_cycle_time(monkeypatch, tmp_path):
    """note_candidate must not fetch or sleep on the main cycle thread."""
    import book
    import collect
    import main as shift
    import young_trail_signals as sig

    monkeypatch.setenv("OUTCOMES_DB", str(tmp_path / "outcomes.db"))
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.execute("DELETE FROM carry")
    book.DB.commit()
    sig.reset()
    sig.set_time_fn(lambda: T0)
    hangs = []

    def hang(*_a, **_k):
        hangs.append("http")
        time.sleep(30)
        raise AssertionError("signals HTTP ran on the cycle thread")

    def hang_sleep(d):
        hangs.append("sleep")
        time.sleep(30)
        raise AssertionError("signals sleep ran on the cycle thread")

    sig.set_http_fn(hang)
    sig.set_sleep_fn(hang_sleep)

    tid = f"{MINT}:1399811149"

    def fake_shortlist(fomo, id_list):
        return [{
            "addr": MINT, "net": 1399811149, "tid": tid, "ticker": "HNG",
            "mcap_usd": 300_000, "liquidity_usd": 48_000, "volume_h24": 610_000,
            "price_usd": 0.001, "holder_count": 310,
            "change": {"5m": 0.04, "1h": 0.22, "4h": 0.4, "24h": 0.61},
            "age_minutes": 42, "chain": "solana",
        }]

    def fake_judge(question_set, state):
        if question_set == "market":
            return {"model": "test", "answers": {
                "concentration_is_exit_risk": {"type": "noul", "noul": 0.75},
                "momentum_already_spent": {"type": "noul", "noul": 0.40},
            }, "usage": {}}
        return {"model": "test", "answers": {}, "usage": {}}

    def fake_dossier(t, limiter=None):
        return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                "developer_holding_percentage": 2, "gt_score_details": None,
                "is_honeypot": None, "mint_authority": None, "freeze_authority": None,
                "description": "a token", "x_handle": None, "net": 1399811149,
                "address": MINT}

    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: ([tid], {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist)
    monkeypatch.setattr(collect, "shortlist", fake_shortlist)
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: (
        {"buys_h1": 540, "sells_h1": 120, "buys_h6": 900, "sells_h6": 400,
         "trades_h24": 4000}, "ok"))
    monkeypatch.setattr(shift, "dossier", fake_dossier)

    class Desk:
        def bank(self): return 1000.0
        def read_x(self, h, tid=None, token=None): return None
        def log_shadow(self, o, s, fomo_data=None): pass
        def report(self, o, s): pass
        def send_to_seats(self, o): pass

    t0 = time.perf_counter()
    order, stats = shift.run_once(
        type("F", (), {"token": lambda self: None})(),
        fake_judge, Desk(), 1000.0, shadow=True, gt_dossier_reserve=3,
    )
    elapsed = time.perf_counter() - t0
    assert order is None
    assert stats["seen"] == 1
    assert hangs == []
    assert elapsed < 2.0
    queued = sig.pending_candidates()
    assert any(q.get("tid") == tid for q in queued)
    sig.reset()
