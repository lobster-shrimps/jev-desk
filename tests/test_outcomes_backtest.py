"""Mocked tests for outcomes, shadow kills, weekly backtest, and /ops/backtest.

No network. No real sleeps. thresholds.py and cycle/soft/judge/x log formats unchanged.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import subprocess
import sys
import time
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DESK_SECRET", "test-secret-for-outcomes")
os.environ.setdefault("JUDGE_MOCK", "1")
os.environ["TZ"] = "UTC"
if hasattr(time, "tzset"):
    time.tzset()


def ts(y, m, d, hh=12, mm=0, ss=0) -> float:
    return time.mktime((y, m, d, hh, mm, ss, 0, 0, -1))


class Resp:
    def __init__(self, status, payload):
        self.status_code = status
        self._payload = payload

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
def hist(tmp_path, monkeypatch):
    os.environ["TZ"] = "UTC"
    if hasattr(time, "tzset"):
        time.tzset()
    monkeypatch.setenv("CYCLE_HISTORY_DB", str(tmp_path / "cycle_history.db"))
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    monkeypatch.setenv("OUTCOMES_DB", str(tmp_path / "outcomes.db"))
    import cycle_history
    import outcomes
    import backtest_signals
    cycle_history.reset()
    outcomes.reset()
    backtest_signals.reset()
    yield cycle_history
    cycle_history.reset()
    outcomes.reset()
    backtest_signals.reset()


def test_sim_math_sl_before_tp_and_trail():
    import outcomes
    entry = 1_000_000.0
    # First bar: low hits SL (-30) and high hits TP (+100). SL wins.
    candles = [_candle(entry, 1.0, 2.5, 0.50, 1.2)]
    cur = outcomes.simulate(candles, entry, trail=False)
    assert cur["reason"] == "sl"
    assert cur["exit_price"] == pytest.approx(0.70)
    assert cur["pnl"] == pytest.approx(50.0 * ((0.70 / 1.0 - 1.0) - 0.03))
    # Trail: run-up then give back 30% from the high.
    trail_candles = [
        _candle(entry, 1.0, 1.50, 0.99, 1.45),
        _candle(entry + 300, 1.45, 1.52, 1.04, 1.10),
    ]
    tr = outcomes.simulate(trail_candles, entry, trail=True)
    assert tr["reason"] == "trail"
    assert tr["exit_price"] == pytest.approx(1.50 * 0.70)
    # TP path: clean double without dipping to SL.
    tp_candles = [_candle(entry, 1.0, 2.10, 0.95, 2.05)]
    tp = outcomes.simulate(tp_candles, entry, trail=False)
    assert tp["reason"] == "tp"
    assert tp["exit_price"] == pytest.approx(2.0)
    assert tp["pnl"] == pytest.approx(50.0 * (1.0 - 0.03))
    # 6h time stop uses last close.
    timed = []
    t = entry
    px = 1.0
    while t < entry + 6 * 3600:
        timed.append(_candle(t, px, px + 0.01, px - 0.01, px + 0.005))
        t += 300
        px += 0.005
    ts_sim = outcomes.simulate(timed, entry, trail=False)
    assert ts_sim["reason"] == "time"
    assert ts_sim["complete"] is True


def test_cache_hit_makes_no_http_call(hist, monkeypatch):
    import outcomes
    calls = []

    def boom(*a, **k):
        calls.append((a, k))
        raise AssertionError("http")

    outcomes.set_get_fn(boom)
    entry = time.time() - 7 * 3600
    candles = [_candle(entry + i * 300, 1.0, 1.01, 0.99, 1.0) for i in range(80)]
    outcomes.put_cached("tok:1399811149", entry, {
        "pool": "pool1", "chain": "solana", "candles": candles,
        "complete": True, "peak_x": 1.01, "trough_x": 0.99, "ret_6h": 0.0,
        "sim_current_pnl": -1.5, "sim_current_reason": "time",
        "sim_trail_pnl": -1.5, "sim_trail_reason": "time",
        "fetched_at": time.time(),
    })
    got = outcomes.fetch_outcome("tok:1399811149", entry, now=time.time())
    assert got is not None
    assert got["complete"] is True
    assert got["peak_x"] == pytest.approx(1.01)
    assert calls == []


def test_429_backoff(hist):
    import outcomes
    slept = []
    outcomes.set_sleep_fn(lambda d: slept.append(d))
    clock = [0.0]
    outcomes.set_time_fn(lambda: clock[0])

    def get(url, params=None, timeout=None, headers=None):
        if "dexscreener.com" in url:
            return Resp(200, [{
                "pairAddress": "PoolX",
                "liquidity": {"usd": 80_000},
            }])
        return Resp(429, {"error": "rate limited"})

    outcomes.set_get_fn(get)
    entry = -7 * 3600  # well past 6h10m relative to clock 0
    got = outcomes.fetch_outcome("Addr1:1399811149", entry, now=0.0, deadline=1000.0)
    assert got is None or not (got or {}).get("complete")
    assert any(abs(s - 30.0) < 1e-6 for s in slept)


def test_shadow_kills_from_history_hook(hist):
    stats = {
        "seen": 8, "benched": 0, "judged": 1, "requeued": 0,
        "free": {}, "trade": {},
        "chain": {"top_wallet": 1, "top_10": 1},
        "soft": {"momentum_already_spent": 1, "shape": 1},
        "tokens": [
            {"tid": "w:1399811149", "ticker": "WHL", "net": 1399811149,
             "stage": "chain", "reason": "top_wallet", "age_minutes": 22,
             "liquidity_usd": 40_000, "mcap_usd": 200_000, "holder_count": 90,
             "top_wallet_percent": 0.12},
            {"tid": "t10:56", "ticker": "TEN", "net": 56,
             "stage": "chain", "reason": "top_10", "age_minutes": 18,
             "liquidity_usd": 22_000, "mcap_usd": 90_000, "holder_count": 120},
            {"tid": "h:56", "ticker": "HLD", "net": 56,
             "stage": "chain", "reason": "holders", "age_minutes": 30,
             "holder_count": 40},
            {"tid": "old:56", "ticker": "OLD", "net": 56,
             "stage": "chain", "reason": "top_wallet", "age_minutes": 90},
            {"tid": "s:1399811149", "ticker": "SFT", "net": 1399811149,
             "stage": "soft", "reason": "momentum_already_spent", "age_minutes": 25,
             "soft_noul": 0.88, "soft_scores": {"momentum_already_spent": 0.88}},
            {"tid": "sh:56", "ticker": "SHP", "net": 56,
             "stage": "soft", "reason": "shape", "age_minutes": 40,
             "soft_noul": 0.41, "crowd_prob": 0.41},
            {"tid": "p:1399811149", "ticker": "PSS", "net": 1399811149,
             "stage": "judged", "reason": None, "age_minutes": 33,
             "judge_verdict": "pass", "judge_score": 0.42,
             "soft_scores": {"momentum_already_spent": 0.2}},
            {"tid": "pk:1399811149", "ticker": "PKD", "net": 1399811149,
             "stage": "judged", "age_minutes": 28, "judge_verdict": "pick",
             "judge_score": 0.8},
            {"tid": "e:8453", "ticker": "ERR", "net": 8453,
             "stage": "judge_error", "age_minutes": 19, "judge_verdict": "error"},
        ],
        "young_free": [],
    }
    cid = hist.on_cycle(stats, now=ts(2026, 10, 12, 8), source="backfill")
    assert cid is not None
    rows = hist.shadow_kills_since(0)
    reasons = {(r["ticker"], r["stage"], r["reason"]) for r in rows}
    assert ("WHL", "chain", "top_wallet") in reasons
    assert ("TEN", "chain", "top_10") in reasons
    assert ("HLD", "chain", "holders") in reasons
    assert ("SFT", "soft", "momentum_already_spent") in reasons
    assert ("SHP", "soft", "shape") in reasons
    assert ("PSS", "judged", "pass") in reasons
    assert ("ERR", "judge_error", "error") in reasons
    assert not any(r["ticker"] == "OLD" for r in rows)
    assert not any(r["ticker"] == "PKD" for r in rows)
    whale = next(r for r in rows if r["ticker"] == "WHL")
    assert whale["liq"] == 40_000
    assert whale["mcap"] == 200_000
    assert whale["holders"] == 90
    assert whale["entry_ts"] == ts(2026, 10, 12, 8)


def test_shadow_kills_backfill_from_tokens_and_young(hist):
    hist.on_cycle({
        "seen": 4, "benched": 0, "judged": 1, "requeued": 0,
        "free": {}, "trade": {}, "chain": {}, "soft": {},
        "tokens": [{
            "tid": "j:1399811149", "ticker": "JK", "net": 1399811149,
            "stage": "judged", "age_minutes": 21, "judge_verdict": "pass",
            "judge_score": 0.31, "soft_scores": {"momentum_already_spent": 0.2},
        }],
        "young_free": [
            {"tid": "y:1399811149", "ticker": "YNG", "net": 1399811149,
             "age_minutes": 16},
        ],
    }, now=ts(2026, 10, 12, 9), source="backfill")
    # Extra young/chain row only lives on young_free_pass in this fixture.
    db = hist._connect()
    db.execute(
        """INSERT INTO young_free_pass (cycle_id, ts, tid, chain_id, chain, ticker,
             age_minutes, outcome, reason) VALUES (?,?,?,?,?,?,?,?,?)""",
        (1, ts(2026, 10, 12, 9), "y:1399811149", 1399811149, "solana", "YNG",
         16, "chain", "top_wallet"),
    )
    db.commit()
    result = hist.backfill_shadow_kills()
    assert result["total"] >= 2
    rows = hist.shadow_kills_since(0)
    tids = {r["tid"] for r in rows}
    assert "j:1399811149" in tids
    assert "y:1399811149" in tids


def test_hook_exception_does_not_break_run_once(hist, monkeypatch):
    import book
    import collect
    import main as shift

    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.execute("DELETE FROM carry")
    book.DB.commit()

    def boom(*_a, **_k):
        raise RuntimeError("token=LEAKEDTOKEN123")

    monkeypatch.setattr(hist, "on_cycle", boom)
    monkeypatch.setattr(hist, "on_soft_token", boom)

    tid = f"HookAddr:{1399811149}"

    def fake_shortlist(fomo, id_list):
        return [{
            "addr": "HookAddr", "net": 1399811149, "tid": tid, "ticker": "HOK",
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
                "description": "a token", "x_handle": None, "net": 1399811149}

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

    order, stats = shift.run_once(
        type("F", (), {"token": lambda self: None})(),
        fake_judge, Desk(), 1000.0, shadow=True, gt_dossier_reserve=3,
    )
    assert order is None
    assert stats["seen"] == 1
    assert "soft" in stats


def test_format_strings_and_thresholds_untouched():
    src = (ROOT / "main.py").read_text()
    assert (
        'log.info("cycle: %(seen)s seen, %(benched)s benched, free %(free)s, "\n'
        '             "trade %(trade)s, chain %(chain)s, soft %(soft)s, '
        'judged %(judged)s, requeued %(requeued)s", stats)'
    ) in src
    assert (
        'log.info("soft tid=%s ticker=%s reason=%s noul=%s age_minutes=%s '
        'top_10_percent=%s top_wallet_percent=%s developer_holding_percentage=%s '
        'holder_count=%s rpc_ok=%s soft_scores=%s"'
    ) in src
    assert 'log.info("unevaluated %d ids (dex_slots=%d, gt_available=%d)",' in src
    assert (
        '"judge tid=%s ticker=%s verdict=%s score=%s confidence=%s reason=%s mode=%s"'
    ) in src
    xsrc = (ROOT / "x_lookup.py").read_text()
    assert (
        '"x tid=%s handle=%s ok=%s followers=%s own_posts=%s source=%s err=%s"'
    ) in xsrc
    r = subprocess.run(
        ["git", "diff", "--", "thresholds.py"],
        cwd=str(ROOT), capture_output=True, text=True, check=True,
    )
    assert r.stdout == ""
    assert "PICK_MIN_WORTH  = 0.60" in (ROOT / "thresholds.py").read_text()


def test_backtest_markdown_on_synthetic_data(hist, tmp_path, monkeypatch):
    import backtest_signals
    import outcomes
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    now = ts(2026, 10, 12, 12)
    rows = []
    for i, reason in enumerate(
        ["top_wallet", "top_10", "holders", "momentum_already_spent", "shape", "pass"]
    ):
        rows.append({
            "tid": f"t{i}:1399811149",
            "ticker": f"T{i}",
            "reason": reason,
            "kill_reason": reason,
            "kill_stage": "judged" if reason == "pass" else ("soft" if reason in ("momentum_already_spent", "shape") else "chain"),
            "age": 15 + i * 5,
            "liq": 12_000 + i * 8_000,
            "mcap": 70_000 + i * 20_000,
            "holders": 80 + i * 40,
            "top_10": 20 + i,
            "top_wallet": 0.02 + i * 0.01,
            "dev_pct": 1 + i,
            "crowd_prob": 0.4 + i * 0.05,
            "momentum_noul": 0.2 + i * 0.08,
            "x_present": 1 if i % 2 == 0 else 0,
            "x_followers": 100 * (i + 1) if i % 2 == 0 else None,
            "x_own_posts": 3 + i if i % 2 == 0 else None,
            "judge_score": 0.2 + i * 0.1,
            "regime": "neutral" if i < 3 else "risk_on",
            "peak_x": 2.4 if i % 2 == 0 else 1.1,
            "trough_x": 0.8,
            "ret_6h": 0.4 if i % 2 == 0 else -0.2,
            "sim_current_pnl": 12.0 if i % 2 == 0 else -16.5,
            "sim_trail_pnl": 8.0 if i % 2 == 0 else -10.0,
            "outcome_complete": True,
        })
    report = backtest_signals.build_report(rows, week_id="2026-42", days=7, now=now)
    md = report["markdown"]
    assert md.startswith("# Signal backtest — 2026-42")
    assert "Winners vs losers" in md
    assert "Per-feature quartiles" in md
    assert "### age" in md
    assert "Per kill reason" in md
    assert "top_wallet" in md
    assert "Judge score buckets" in md
    assert backtest_signals.N_CAVEAT in md
    assert "does not change" in md.lower() or "Does not change" in md
    path = backtest_signals.persist_report(report)
    assert path.exists()
    sibling = path.with_suffix(".json")
    assert sibling.exists()
    payload = json.loads(sibling.read_text())
    assert payload["week_id"] == "2026-42"
    assert payload["n"] == 6
    assert "SUPERSECRET" not in sibling.read_text()


def test_ops_backtest_returns_200(hist, tmp_path, monkeypatch):
    import backtest_signals
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    from fastapi.testclient import TestClient
    import server
    client = TestClient(server.app)
    empty = client.get("/ops/backtest")
    assert empty.status_code == 200
    body = empty.json()
    assert body.get("n") == 0 or "error" in body
    report = backtest_signals.build_report(
        [{
            "tid": "a:1399811149", "reason": "top_wallet", "kill_reason": "top_wallet",
            "age": 20, "peak_x": 2.2, "sim_current_pnl": 5.0, "sim_trail_pnl": 4.0,
            "outcome_complete": True, "judge_score": 0.4,
        }],
        week_id="2026-42", days=7, now=ts(2026, 10, 12, 12),
    )
    backtest_signals.persist_report(report)
    resp = client.get("/ops/backtest")
    assert resp.status_code == 200
    data = resp.json()
    assert data["week_id"] == "2026-42"
    assert data["n"] == 1
    assert "kill_reasons" in data
    dumped = json.dumps(data)
    assert "SUPERSECRET" not in dumped
    assert "TYPESAFE" not in dumped


def test_briefing_includes_killed_young_section(hist):
    hist.on_cycle({
        "seen": 3, "benched": 0, "judged": 0, "requeued": 0,
        "free": {}, "trade": {}, "chain": {"top_wallet": 1}, "soft": {},
        "tokens": [{
            "tid": "w:1399811149", "ticker": "WHL", "net": 1399811149,
            "stage": "chain", "reason": "top_wallet", "age_minutes": 20,
            "liquidity_usd": 30_000, "mcap_usd": 100_000, "holder_count": 85,
        }],
    }, now=ts(2026, 10, 12, 8), source="backfill")
    payload = hist.build_briefing(ts(2026, 10, 12, 9))
    assert "## Killed young tokens (6h outcome)" in payload["markdown"]
    assert "top_wallet" in payload["markdown"]
    assert "Latest backtest:" in payload["markdown"]
    w = payload["last_24h"]
    assert w["shadow_kills"]["n"] >= 1
    assert "top_wallet" in w["shadow_kills"]["by_reason"]


def test_weekly_thread_sunday_after_0700(hist, tmp_path, monkeypatch):
    import backtest_signals
    monkeypatch.delenv("BACKTEST_DISABLE", raising=False)
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    monkeypatch.setenv("BACKTEST_TIME_CAP_SEC", "5")
    # Sunday 2026-10-11 08:00 UTC.
    now = ts(2026, 10, 11, 8, 0)
    backtest_signals.set_time_fn(lambda: now)
    called = {}

    def fake_run(**kwargs):
        called.update(kwargs)
        report = backtest_signals.build_report(
            [], week_id=kwargs.get("week_id") or "2026-41", days=7, now=now,
        )
        backtest_signals.persist_report(report)
        return report

    monkeypatch.setattr(backtest_signals, "run_backtest", fake_run)
    t = backtest_signals.maybe_start_weekly(now=now)
    assert t is not None
    t.join(timeout=2)
    assert called.get("week_id") == "2026-41"
    assert called.get("days") == 7
    assert called.get("fetch") is False
    assert (tmp_path / "backtests" / "2026-41.json").exists()
    # Second kick is a no-op.
    t2 = backtest_signals.maybe_start_weekly(now=now)
    assert t2 is None


def test_weekly_not_before_sunday_0700(hist, monkeypatch, tmp_path):
    import backtest_signals
    monkeypatch.delenv("BACKTEST_DISABLE", raising=False)
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    # Previous week's file already written: Sunday 06:59 must not start this week.
    dest = tmp_path / "backtests"
    dest.mkdir(parents=True, exist_ok=True)
    (dest / "2026-40.json").write_text("{\"week_id\":\"2026-40\"}")
    now = ts(2026, 10, 11, 6, 59)  # Sunday before 07:00
    t = backtest_signals.maybe_start_weekly(now=now)
    assert t is None


def test_cli_days_flag(hist, tmp_path, monkeypatch):
    import backtest_signals
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    seen = {}

    def fake(**k):
        seen.update(k)
        return {"path": str(tmp_path / "backtests" / "x.md"), "week_id": "x", **k}

    monkeypatch.setattr(backtest_signals, "run_backtest", fake)
    rc = backtest_signals.main(["--days", "7"])
    assert rc == 0
    assert seen.get("fetch") is False
    seen.clear()
    rc = backtest_signals.main(["--days", "7", "--fetch"])
    assert rc == 0
    assert seen.get("fetch") is False  # report stays cache-only; --fetch drains first


def test_no_secrets_in_outcome_logs(hist, caplog):
    import outcomes
    outcomes.set_get_fn(lambda *a, **k: (_ for _ in ()).throw(
        RuntimeError("https://api.example/?api-key=SUPERSECRET")
    ))
    with caplog.at_level(logging.WARNING):
        outcomes.fetch_outcome("x:1399811149", time.time() - 8 * 3600, now=time.time())
    assert "SUPERSECRET" not in caplog.text
    assert "REDACTED" in caplog.text or "outcomes" in caplog.text


def test_filter_judge_pick_untouched():
    """Nothing in this PR may change a kill, judge, pick, or order."""
    r = subprocess.run(
        ["git", "diff", "--", "filter.py", "judge.py", "pick.py", "thresholds.py"],
        cwd=str(ROOT), capture_output=True, text=True, check=True,
    )
    assert r.stdout == ""


def test_first_num_ignores_truthy_non_numeric_liq():
    import cycle_history as ch
    assert ch._liq_of({"liq": {"usd": 12_000}, "liquidity_usd": 99}) == 12_000
    assert ch._liq_of({"liq": "n/a", "liquidity_usd": 40_000}) == 40_000
    assert ch._mcap_of({"mcap": {"usd": 80_000}}) == 80_000
    assert ch._first_num(None, "", "None", {"usd": 5}) == 5


def test_tokens_persist_liq_mcap_and_quartile_join(hist):
    """tokens.liquidity_usd / mcap_usd reach shadow_kills and quartile tables."""
    import backtest_signals
    now = ts(2026, 10, 8, 10)
    hist.on_cycle({
        "seen": 5, "benched": 0, "judged": 1, "requeued": 0,
        "free": {}, "trade": {}, "chain": {"top_wallet": 3, "top_10": 1}, "soft": {},
        "tokens": [
            {"tid": "w:1399811149", "ticker": "WHL", "net": 1399811149,
             "stage": "chain", "reason": "top_wallet", "age_minutes": 22,
             "liquidity_usd": 40_000, "mcap_usd": 200_000, "holder_count": 90},
            {"tid": "w2:1399811149", "ticker": "WH2", "net": 1399811149,
             "stage": "chain", "reason": "top_wallet", "age_minutes": 18,
             "liquidity_usd": 12_000, "mcap_usd": 70_000, "holder_count": 70},
            {"tid": "t:56", "ticker": "TEN", "net": 56,
             "stage": "chain", "reason": "top_10", "age_minutes": 30,
             "liquidity_usd": 55_000, "mcap_usd": 310_000, "holder_count": 140},
            {"tid": "j:1399811149", "ticker": "JK", "net": 1399811149,
             "stage": "judged", "reason": None, "age_minutes": 21,
             "judge_verdict": "pass", "judge_score": 0.31,
             "liq": {"usd": 18_000}, "mcap": 80_000},
        ],
    }, now=now, source="backfill")
    tokens = hist._tokens_since(0)
    assert any(t["kind"] == "chain" and t["liquidity_usd"] == 40_000 for t in tokens)
    judged = next(t for t in tokens if t["kind"] == "judged")
    assert judged["liquidity_usd"] == 18_000
    assert judged["mcap_usd"] == 80_000
    whale = next(s for s in hist.shadow_kills_since(0) if s["ticker"] == "WHL")
    assert whale["liq"] == 40_000
    assert whale["mcap"] == 200_000
    rows = backtest_signals.load_rows(0, now + 1)
    assert any(r.get("liq") == 40_000 and r.get("mcap") == 200_000 for r in rows)
    report = backtest_signals.build_report(rows, week_id="2026-41", days=7, now=now)
    assert report["features"]["liq"]["n_with_value"] >= 1
    assert sum(q["n"] for q in report["features"]["liq"]["quartiles"]) >= 1
    assert report["features"]["mcap"]["n_with_value"] >= 1


def test_backfill_join_fills_null_shadow_liq_from_tokens(hist):
    """Existing shadow_kills with NULL liq/mcap pick up tokens columns."""
    import backtest_signals
    now = ts(2026, 10, 8, 10)
    hist.on_cycle({
        "seen": 1, "benched": 0, "judged": 1, "requeued": 0,
        "free": {}, "trade": {}, "chain": {}, "soft": {},
        "tokens": [{
            "tid": "j:1399811149", "ticker": "JK", "net": 1399811149,
            "stage": "judged", "age_minutes": 21, "judge_verdict": "pass",
            "judge_score": 0.31,
        }],
    }, now=now, source="backfill")
    db = hist._connect()
    db.execute(
        "UPDATE tokens SET liquidity_usd = 25000, mcap_usd = 90000 WHERE tid = ?",
        ("j:1399811149",),
    )
    db.execute(
        "UPDATE shadow_kills SET liq = NULL, mcap = NULL WHERE tid = ?",
        ("j:1399811149",),
    )
    db.commit()
    hist.backfill_shadow_kills()
    row = hist.shadow_kills_since(0)[0]
    assert row["liq"] == 25_000
    assert row["mcap"] == 90_000
    enriched = backtest_signals.load_rows(0, now + 1)
    assert enriched[0]["liq"] == 25_000
    assert enriched[0]["mcap"] == 90_000


def test_skip_cycle_patches_liq_mcap_from_log(hist):
    """Re-parsing run.log fills market fields on already-stored cycles."""
    log_text = """
2026-10-08 10:00:01,001 desk INFO free tid=Young1:1399811149 reason=pass age_minutes=22.0 liquidity_usd=48000 volume_usd=610000 mcap_usd=300000
2026-10-08 10:00:03,003 desk INFO chain tid=Young1:1399811149 ticker=YNG reason=top_wallet
2026-10-08 10:00:07,007 desk INFO cycle: 2 seen, 0 benched, free {'pass': 1}, trade {}, chain {'top_wallet': 1}, soft {}, judged 0, requeued 0
"""
    ts_cycle = time.mktime(time.strptime("2026-10-08 10:00:07", "%Y-%m-%d %H:%M:%S"))
    hist.on_cycle({
        "seen": 2, "benched": 0, "judged": 0, "requeued": 0,
        "free": {"pass": 1}, "trade": {}, "chain": {"top_wallet": 1}, "soft": {},
        "tokens": [{
            "tid": "Young1:1399811149", "ticker": "YNG", "net": 1399811149,
            "stage": "chain", "reason": "top_wallet", "age_minutes": 22,
        }],
        "young_free": [{
            "tid": "Young1:1399811149", "ticker": "YNG", "net": 1399811149,
            "age_minutes": 22,
        }],
    }, now=ts_cycle, source="live")
    shadows = hist.shadow_kills_since(0)
    assert shadows
    assert shadows[0]["liq"] is None
    result = hist.backfill_text(log_text)
    assert result["skipped"] == 1
    whale = next(s for s in hist.shadow_kills_since(0) if s["tid"] == "Young1:1399811149")
    assert whale["liq"] == 48_000
    assert whale["mcap"] == 300_000
    tok = next(t for t in hist._tokens_since(0) if t["tid"] == "Young1:1399811149")
    assert tok["liquidity_usd"] == 48_000
    assert tok["mcap_usd"] == 300_000


def test_free_pass_parse_carries_liq_mcap(hist):
    log_text = """
2026-10-08 10:00:01,001 desk INFO free tid=Young1:1399811149 reason=pass age_minutes=22.0 liquidity_usd=48000 volume_usd=610000 mcap_usd=300000
2026-10-08 10:00:03,003 desk INFO chain tid=Young1:1399811149 ticker=YNG reason=top_wallet
2026-10-08 10:00:04,004 desk INFO soft tid=Soft1:56 ticker=SFT reason=momentum_already_spent noul=0.77 age_minutes=44 top_10_percent=31 top_wallet_percent=0.02 developer_holding_percentage=3 holder_count=190 rpc_ok=True soft_scores={'momentum_already_spent': 0.77}
2026-10-08 10:00:07,007 desk INFO cycle: 3 seen, 0 benched, free {'pass': 1}, trade {}, chain {'top_wallet': 1}, soft {'momentum_already_spent': 1}, judged 0, requeued 0
"""
    hist.backfill_text(log_text)
    chain = next(t for t in hist._tokens_since(0) if t["kind"] == "chain")
    assert chain["liquidity_usd"] == 48_000
    assert chain["mcap_usd"] == 300_000
    yng = next(s for s in hist.shadow_kills_since(0) if s["tid"] == "Young1:1399811149")
    assert yng["liq"] == 48_000
    assert yng["mcap"] == 300_000
    import backtest_signals
    rows = backtest_signals.load_rows(0, ts(2026, 10, 9))
    yng_row = next(r for r in rows if r["tid"] == "Young1:1399811149")
    assert yng_row["liq"] == 48_000
    assert yng_row["mcap"] == 300_000


def test_measured_equals_complete_not_partial_peak(hist, tmp_path, monkeypatch, caplog):
    """Log complete= and report measured must use the same 6h-complete definition."""
    import backtest_signals
    import outcomes
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    now = ts(2026, 10, 12, 12)
    entry = now - 8 * 3600
    hist.on_cycle({
        "seen": 2, "benched": 0, "judged": 0, "requeued": 0,
        "free": {}, "trade": {}, "chain": {"top_wallet": 1, "top_10": 1}, "soft": {},
        "tokens": [
            {"tid": "a:1399811149", "ticker": "A", "net": 1399811149,
             "stage": "chain", "reason": "top_wallet", "age_minutes": 20,
             "liquidity_usd": 10_000, "mcap_usd": 50_000},
            {"tid": "b:1399811149", "ticker": "B", "net": 1399811149,
             "stage": "chain", "reason": "top_10", "age_minutes": 25,
             "liquidity_usd": 20_000, "mcap_usd": 80_000},
        ],
    }, now=entry, source="backfill")
    outcomes.put_cached("a:1399811149", entry, {
        "pool": "p", "chain": "solana", "candles": [],
        "complete": True, "peak_x": 2.2, "trough_x": 0.8, "ret_6h": 0.1,
        "sim_current_pnl": 5.0, "sim_current_reason": "time",
        "sim_trail_pnl": 4.0, "sim_trail_reason": "time",
        "fetched_at": now,
    })
    outcomes.put_cached("b:1399811149", entry, {
        "pool": "p", "chain": "solana", "candles": [],
        "complete": False, "peak_x": 3.1, "trough_x": 0.9, "ret_6h": None,
        "sim_current_pnl": None, "sim_current_reason": None,
        "sim_trail_pnl": None, "sim_trail_reason": None,
        "fetched_at": now,
    })
    with caplog.at_level(logging.INFO):
        report = backtest_signals.run_backtest(
            days=7, now=now, week_id="2026-41", fetch=False, persist=True,
        )
    assert report["n"] == 2
    assert report["n_complete"] == 1
    assert report["n_measured"] == 1
    assert report["n_complete"] == report["n_measured"]
    assert "complete=1" in caplog.text
    assert "measured=1" in caplog.text
    rows = backtest_signals.attach_outcomes(
        backtest_signals.load_rows(entry - 10, now), now=now, fetch=False,
    )
    incomplete = next(r for r in rows if r["tid"] == "b:1399811149")
    assert incomplete["outcome_complete"] is False
    assert incomplete["peak_x"] is None
    assert backtest_signals._is_measured(incomplete) is False


def test_outcomes_window_default_and_wrap():
    import gt_api
    import time as time_mod
    start, end = gt_api.parse_outcomes_window("01:00-06:00")
    assert start == 60
    assert end == 360
    start, end = gt_api.parse_outcomes_window("22:00-04:00")
    assert start == 22 * 60
    assert end == 4 * 60

    def lt_at(hour, minute=0):
        def _lt(_ts):
            return time_mod.struct_time((2026, 10, 12, hour, minute, 0, 0, 0, -1))
        return _lt

    assert gt_api.in_outcomes_window(0, localtime=lt_at(1, 0)) is True
    assert gt_api.in_outcomes_window(0, localtime=lt_at(5, 59)) is True
    assert gt_api.in_outcomes_window(0, localtime=lt_at(6, 0)) is False
    assert gt_api.in_outcomes_window(0, localtime=lt_at(12, 0)) is False


def test_fetch_jobs_resume_after_deadline(hist, monkeypatch):
    """Each completed fetch is in outcomes.db; a timeout leaves the rest pending."""
    import outcomes
    now = ts(2026, 10, 12, 12)
    outcomes.set_time_fn(lambda: now)
    clock = [0.0]
    outcomes.set_sleep_fn(lambda d: clock.__setitem__(0, clock[0] + d))
    calls = []

    def get(url, params=None, timeout=None, headers=None):
        calls.append(url)
        if "dexscreener.com" in url:
            return Resp(200, [{"pairAddress": "PoolX", "liquidity": {"usd": 80_000}}])
        candles = [_candle(now - 8 * 3600 + i * 300, 1.0, 1.01, 0.99, 1.0) for i in range(80)]
        return Resp(200, _ohlcv_list(candles))

    outcomes.set_get_fn(get)
    a, b = "aa:1399811149", "bb:1399811149"
    entry = now - 8 * 3600
    outcomes.enqueue_job(a, entry, now=now)
    outcomes.enqueue_job(b, entry, now=now)
    assert len(outcomes.list_jobs("pending")) == 2
    result = outcomes.drain_jobs(now=now, trickle=False, max_calls=1)
    assert result["fetched"] == 1
    assert result["complete"] == 1
    pending = outcomes.list_jobs("pending")
    assert len(pending) == 1
    cached_a = outcomes.get_cached(a, entry)
    cached_b = outcomes.get_cached(b, entry)
    assert (cached_a and cached_a.get("complete")) or (cached_b and cached_b.get("complete"))
    assert not ((cached_a and cached_a.get("complete")) and (cached_b and cached_b.get("complete")))
    result2 = outcomes.drain_jobs(now=now, trickle=False)
    assert result2["complete"] >= 1
    assert outcomes.get_cached(a, entry)["complete"] is True
    assert outcomes.get_cached(b, entry)["complete"] is True
    assert outcomes.list_jobs("pending") == []


def test_drain_pauses_on_cycle_gt_429(hist, monkeypatch):
    import outcomes
    import main as shift
    now = ts(2026, 10, 12, 15)
    outcomes.set_time_fn(lambda: now)
    shift._gt_limiter.reset_for_test()
    shift._gt_limiter.stats_429_count = 1
    calls = []
    outcomes.set_get_fn(lambda *a, **k: calls.append(a) or Resp(500, {}))
    outcomes.enqueue_job("z:1399811149", now - 8 * 3600, now=now)
    result = outcomes.drain_jobs(now=now, trickle=True, max_calls=1)
    assert result["paused"] is True
    assert result["fetched"] == 0
    assert calls == []
    shift._gt_limiter.reset_for_test()


def test_trickle_spacing_is_20s(hist):
    import gt_api
    import outcomes
    assert gt_api.outcomes_spacing_sec(trickle=True) == 20.0
    outcomes._trickle_mode = True
    try:
        slept = []
        outcomes.set_sleep_fn(lambda d: slept.append(d))
        outcomes.set_time_fn(lambda: 1000.0)
        with outcomes._gt_lock:
            outcomes._last_gt_call = 1000.0
            outcomes._gt_backoff_until = 0.0
        assert outcomes._pace_gt(deadline=None) is True
        assert slept and slept[0] == pytest.approx(20.0)
    finally:
        outcomes._trickle_mode = False


def test_coingecko_pro_url_and_fallback(hist, monkeypatch, caplog):
    import gt_api
    import outcomes
    monkeypatch.setenv("COINGECKO_API_KEY", "CG-SUPERSECRETKEY")
    monkeypatch.setenv("COINGECKO_PLAN", "pro")
    gt_api.reset()
    assert gt_api.plan() == "pro"
    assert gt_api.using_pro() is True
    pro = gt_api.prefer_onchain_url(
        "https://api.geckoterminal.com/api/v2/networks/solana/pools/x/ohlcv/minute",
        purpose="outcomes",
    )
    assert pro.startswith("https://pro-api.coingecko.com/api/v3/onchain/")
    assert "SUPERSECRET" not in pro
    headers = gt_api.request_headers(pro, purpose="outcomes")
    assert headers.get("x-cg-pro-api-key") == "CG-SUPERSECRETKEY"
    now = ts(2026, 10, 12, 12)
    outcomes.set_time_fn(lambda: now)
    outcomes.set_sleep_fn(lambda d: None)
    seen = []

    def get(url, params=None, timeout=None, headers=None):
        seen.append(url)
        if "pro-api.coingecko.com" in url:
            return Resp(401, {"error": "unauthorized"})
        if "dexscreener.com" in url:
            return Resp(200, [{"pairAddress": "PoolX", "liquidity": {"usd": 80_000}}])
        candles = [_candle(now - 8 * 3600 + i * 300, 1.0, 1.01, 0.99, 1.0) for i in range(80)]
        return Resp(200, _ohlcv_list(candles))

    outcomes.set_get_fn(get)
    with caplog.at_level(logging.INFO):
        got = outcomes.fetch_outcome("tok:1399811149", now - 8 * 3600, now=now)
    assert got is not None
    assert any("pro-api.coingecko.com" in u for u in seen)
    assert any("api.geckoterminal.com" in u for u in seen)
    assert "CG-SUPERSECRETKEY" not in caplog.text
    assert "SUPERSECRET" not in caplog.text
    assert gt_api.using_pro() is False
    monkeypatch.delenv("COINGECKO_API_KEY", raising=False)
    gt_api.reset()


def test_desk_limiter_pro_pacing(monkeypatch):
    import gt_api
    from collect import GTRateLimiter
    monkeypatch.setenv("COINGECKO_API_KEY", "CG-TESTKEY99")
    monkeypatch.setenv("COINGECKO_PLAN", "pro")
    monkeypatch.delenv("GT_CALLS_PER_MIN", raising=False)
    gt_api.reset()
    lim = GTRateLimiter(calls_per_min=5, min_spacing_sec=12.0)
    gt_api.apply_desk_pacing(lim)
    assert lim.calls_per_min == 30
    assert lim.min_spacing_sec == 2.0
    monkeypatch.delenv("COINGECKO_API_KEY", raising=False)
    monkeypatch.delenv("COINGECKO_PLAN", raising=False)
    gt_api.reset()


def test_coingecko_demo_default_plan(hist, monkeypatch, caplog):
    import gt_api
    import outcomes
    monkeypatch.setenv("COINGECKO_API_KEY", "CG-DEMOSECRET99")
    monkeypatch.delenv("COINGECKO_PLAN", raising=False)
    gt_api.reset()
    assert gt_api.plan() == "demo"
    assert gt_api.using_pro() is False
    assert gt_api.using_demo() is True
    demo = gt_api.prefer_onchain_url(
        "https://api.geckoterminal.com/api/v2/networks/solana/pools/x/ohlcv/minute",
        purpose="outcomes",
    )
    assert demo.startswith("https://api.coingecko.com/api/v3/onchain/")
    headers = gt_api.request_headers(demo, purpose="outcomes")
    assert headers.get("x-cg-demo-api-key") == "CG-DEMOSECRET99"
    assert "x-cg-pro-api-key" not in headers
    desk = gt_api.prefer_onchain_url(
        "https://api.geckoterminal.com/api/v2/networks/solana/new_pools",
        purpose="desk",
    )
    assert desk.startswith("https://api.geckoterminal.com/api/v2/")
    desk_h = gt_api.request_headers(desk, purpose="desk")
    assert "x-cg-demo-api-key" not in desk_h
    now = ts(2026, 10, 12, 12)
    outcomes.set_time_fn(lambda: now)
    outcomes.set_sleep_fn(lambda d: None)
    seen = []
    seen_headers = []

    def get(url, params=None, timeout=None, headers=None):
        seen.append(url)
        seen_headers.append(headers or {})
        if "api.coingecko.com" in url:
            return Resp(401, {"error": "unauthorized"})
        if "dexscreener.com" in url:
            return Resp(200, [{"pairAddress": "PoolX", "liquidity": {"usd": 80_000}}])
        candles = [_candle(now - 8 * 3600 + i * 300, 1.0, 1.01, 0.99, 1.0) for i in range(80)]
        return Resp(200, _ohlcv_list(candles))

    outcomes.set_get_fn(get)
    with caplog.at_level(logging.INFO):
        got = outcomes.fetch_outcome("tok:1399811149", now - 8 * 3600, now=now)
    assert got is not None
    assert any("api.coingecko.com/api/v3/onchain" in u for u in seen)
    assert any("api.geckoterminal.com" in u for u in seen)
    assert "CG-DEMOSECRET99" not in caplog.text
    assert outcomes.demo_calls_this_month(now) >= 1
    lim = __import__("collect").GTRateLimiter(calls_per_min=5, min_spacing_sec=12.0)
    gt_api.apply_desk_pacing(lim)
    assert lim.calls_per_min == 5
    assert lim.min_spacing_sec == 12.0


def test_demo_monthly_cap_stops_key(hist, monkeypatch):
    import gt_api
    import outcomes
    monkeypatch.setenv("COINGECKO_API_KEY", "CG-DEMOSECRET99")
    monkeypatch.delenv("COINGECKO_PLAN", raising=False)
    gt_api.reset()
    now = ts(2026, 10, 12, 12)
    outcomes.set_time_fn(lambda: now)
    db = outcomes._connect()
    db.execute(
        "INSERT OR REPLACE INTO cg_demo_calls (yyyymm, calls) VALUES (?, ?)",
        (outcomes._yyyymm(now), gt_api.DEMO_MONTHLY_CAP),
    )
    db.commit()
    assert outcomes.demo_month_capped(now) is True
    assert gt_api.using_demo() is False
    url = gt_api.prefer_onchain_url(
        "https://api.geckoterminal.com/api/v2/networks/solana/pools/x/ohlcv/minute",
        purpose="outcomes",
    )
    assert url.startswith("https://api.geckoterminal.com/api/v2/")
    assert gt_api.outcomes_spacing_sec() == 7.0
    seen = []

    def get(url, params=None, timeout=None, headers=None):
        seen.append(url)
        if "dexscreener.com" in url:
            return Resp(200, [{"pairAddress": "PoolX", "liquidity": {"usd": 80_000}}])
        candles = [_candle(now - 8 * 3600 + i * 300, 1.0, 1.01, 0.99, 1.0) for i in range(80)]
        return Resp(200, _ohlcv_list(candles))

    outcomes.set_get_fn(get)
    outcomes.set_sleep_fn(lambda d: None)
    got = outcomes.fetch_outcome("tok:1399811149", now - 8 * 3600, now=now)
    assert got is not None
    assert not any("api.coingecko.com" in u for u in seen)
    assert any("api.geckoterminal.com" in u for u in seen)
    assert outcomes.demo_calls_this_month(now) == gt_api.DEMO_MONTHLY_CAP


def test_demo_outcomes_spacing_is_2s(hist, monkeypatch):
    import gt_api
    import outcomes
    monkeypatch.setenv("COINGECKO_API_KEY", "CG-DEMOSECRET99")
    monkeypatch.delenv("COINGECKO_PLAN", raising=False)
    gt_api.reset()
    assert gt_api.outcomes_spacing_sec(trickle=False) == 2.0
    outcomes._trickle_mode = False
    try:
        slept = []
        outcomes.set_sleep_fn(lambda d: slept.append(d))
        outcomes.set_time_fn(lambda: 1000.0)
        with outcomes._gt_lock:
            outcomes._last_gt_call = 1000.0
            outcomes._gt_backoff_until = 0.0
        assert outcomes._pace_gt(deadline=None) is True
        assert slept and slept[0] == pytest.approx(2.0)
    finally:
        outcomes._trickle_mode = False


def test_cli_fetch_drains_then_cache_report(hist, tmp_path, monkeypatch):
    import backtest_signals
    import outcomes
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    drained = []
    monkeypatch.setattr(outcomes, "enqueue_from_history", lambda **k: 2)
    monkeypatch.setattr(outcomes, "drain_jobs", lambda **k: drained.append(k) or {"fetched": 2})
    monkeypatch.setattr(
        backtest_signals, "run_backtest",
        lambda **k: {"path": str(tmp_path / "backtests" / "x.md"), "week_id": "x",
                     "fetch": k.get("fetch"), **k},
    )
    rc = backtest_signals.main(["--days", "7", "--fetch"])
    assert rc == 0
    assert drained
    assert drained[0].get("trickle") is False


def test_weekly_backtest_does_not_hit_network(hist, tmp_path, monkeypatch):
    import backtest_signals
    import outcomes
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    monkeypatch.delenv("BACKTEST_DISABLE", raising=False)
    now = ts(2026, 10, 11, 8, 0)
    backtest_signals.set_time_fn(lambda: now)
    outcomes.set_get_fn(lambda *a, **k: (_ for _ in ()).throw(AssertionError("network")))
    hist.on_cycle({
        "seen": 1, "benched": 0, "judged": 0, "requeued": 0,
        "free": {}, "trade": {}, "chain": {"top_wallet": 1}, "soft": {},
        "tokens": [{
            "tid": "w:1399811149", "ticker": "WHL", "net": 1399811149,
            "stage": "chain", "reason": "top_wallet", "age_minutes": 20,
            "liquidity_usd": 10_000, "mcap_usd": 50_000,
        }],
    }, now=now - 8 * 3600, source="backfill")
    report = backtest_signals.run_backtest(
        days=7, now=now, week_id="2026-41", fetch=False, persist=True,
    )
    assert report.get("error") is None
    assert report["n"] >= 1
    assert report["n_complete"] == 0


def _seed_job(outcomes, tid, entry_ts, *, attempts=0, last_attempt=None,
              last_error=None, status="pending", now=None):
    db = outcomes._connect()
    db.execute(
        """INSERT OR REPLACE INTO fetch_jobs
           (tid, entry_ts, status, attempts, last_attempt, last_error, enqueued_at)
           VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (tid, float(entry_ts), status, int(attempts), last_attempt, last_error,
         float(now or entry_ts)),
    )
    db.commit()


def test_drain_picks_least_attempted_pending(hist):
    """Daytime trickle must not stick on a high-attempt Dex-miss head job."""
    import outcomes
    now = ts(2026, 10, 12, 15)
    outcomes.set_time_fn(lambda: now)
    outcomes.set_sleep_fn(lambda d: None)
    sticky = "J4KKkYxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxxx:1399811149"
    fresh = "fresh:1399811149"
    entry = now - 8 * 3600
    _seed_job(outcomes, sticky, entry, attempts=4, last_attempt=now - 60,
              last_error="no_pool", now=now)
    outcomes.enqueue_job(fresh, entry, now=now)
    seen_tids = []

    def get(url, params=None, timeout=None, headers=None):
        if "dexscreener.com" in url:
            addr = url.rstrip("/").rsplit("/", 1)[-1]
            if addr.startswith("J4KKkY"):
                return Resp(200, [])
            seen_tids.append(addr)
            return Resp(200, [{"pairAddress": "PoolFresh", "liquidity": {"usd": 80_000}}])
        candles = [_candle(entry + i * 300, 1.0, 1.01, 0.99, 1.0) for i in range(80)]
        return Resp(200, _ohlcv_list(candles))

    outcomes.set_get_fn(get)
    result = outcomes.drain_jobs(now=now, trickle=True, max_calls=1)
    assert result["fetched"] == 1
    assert result["complete"] == 1
    assert any(s.startswith("fresh") for s in seen_tids)
    pending = outcomes.list_jobs("pending")
    assert any(j["tid"] == sticky for j in pending)
    assert not any(j["tid"] == fresh for j in pending)
    assert outcomes.get_cached(fresh, entry)["complete"] is True


def test_no_pool_job_marked_dead_after_five_attempts(hist):
    import outcomes
    now = ts(2026, 10, 12, 15)
    outcomes.set_time_fn(lambda: now)
    outcomes.set_sleep_fn(lambda d: None)
    tid = "nopool:1399811149"
    entry = now - 8 * 3600
    outcomes.enqueue_job(tid, entry, now=now)
    outcomes.set_get_fn(lambda *a, **k: Resp(200, []))
    for i in range(outcomes.DEAD_NO_POOL_ATTEMPTS):
        result = outcomes.drain_jobs(now=now, trickle=True, max_calls=1)
        assert result["fetched"] == 1
    assert outcomes.list_jobs("pending") == []
    dead = outcomes.list_jobs("dead")
    assert len(dead) == 1
    assert dead[0]["tid"] == tid
    assert dead[0]["attempts"] >= outcomes.DEAD_NO_POOL_ATTEMPTS
    assert dead[0]["last_error"] == "no_pool"
    result = outcomes.drain_jobs(now=now, trickle=True, max_calls=1)
    assert result["fetched"] == 0
    assert result["remaining"] == 0


def test_startup_marks_overlimit_no_pool_jobs_dead(hist):
    import outcomes
    now = ts(2026, 10, 12, 15)
    entry = now - 8 * 3600
    sticky = "J4KKkYstuck:1399811149"
    _seed_job(outcomes, sticky, entry, attempts=106, last_attempt=now - 10,
              last_error="incomplete", now=now)
    outcomes.enqueue_job("ok:1399811149", entry, now=now)
    outcomes.reset()
    pending = outcomes.list_jobs("pending")
    assert all(j["tid"] != sticky for j in pending)
    assert any(j["tid"] == "ok:1399811149" for j in pending)
    dead = outcomes.list_jobs("dead")
    assert any(j["tid"] == sticky and j["attempts"] == 106 for j in dead)
    assert all(j["status"] != "pending" for j in dead)


def test_resolvable_pool_not_killed_after_incomplete(hist):
    """GT 429 / incomplete candles must not dead a job that has a Dex pool."""
    import outcomes
    now = ts(2026, 10, 12, 15)
    outcomes.set_time_fn(lambda: now)
    outcomes.set_sleep_fn(lambda d: None)
    tid = "haspool:1399811149"
    entry = now - 8 * 3600
    outcomes.enqueue_job(tid, entry, now=now)

    def get(url, params=None, timeout=None, headers=None):
        if "dexscreener.com" in url:
            return Resp(200, [{"pairAddress": "PoolX", "liquidity": {"usd": 80_000}}])
        return Resp(429, {"error": "rate limited"})

    outcomes.set_get_fn(get)
    for _ in range(outcomes.DEAD_NO_POOL_ATTEMPTS):
        outcomes.drain_jobs(now=now, trickle=True, max_calls=1)
    pending = outcomes.list_jobs("pending")
    assert any(j["tid"] == tid for j in pending)
    assert outcomes.list_jobs("dead") == []
    cached = outcomes.get_cached(tid, entry)
    assert cached and cached.get("pool") == "PoolX"
