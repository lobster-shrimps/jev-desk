"""Paper 60s young_trail_fast ledger. No live orders. No young_trail edits."""
import json
import os
import pathlib
import sqlite3
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DESK_SECRET", "test-secret-for-yt-fast")
os.environ.setdefault("JUDGE_MOCK", "1")

TICKET = 50.0
ENTRY = 1.0
T0 = 1_700_000_000.0
TID = "Fast1:1399811149"


def _tok(tid=TID, ticker="FST", age=22.0, price=ENTRY, **extra):
    row = {
        "tid": tid,
        "ticker": ticker,
        "chain": "solana",
        "net": 1399811149,
        "age_minutes": age,
        "price_usd": price,
    }
    row.update(extra)
    return row


@pytest.fixture
def ytf(monkeypatch):
    import young_trail
    import young_trail_fast
    monkeypatch.delenv("YOUNG_TRAIL_UNTIL", raising=False)
    monkeypatch.delenv("YOUNG_TRAIL_FAST_UNTIL", raising=False)
    monkeypatch.delenv("YOUNG_TRAIL_FAST_ENABLED", raising=False)
    young_trail.reset()
    young_trail.set_time_fn(lambda: T0)
    young_trail.set_sleep_fn(lambda _d: None)
    young_trail.set_get_fn(lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("young_trail tests must not use the network")
    ))
    young_trail_fast.reset()
    young_trail_fast.set_time_fn(lambda: T0)
    yield young_trail, young_trail_fast
    young_trail.reset()
    young_trail_fast.reset()


def test_copies_same_tid_and_entry_price_from_now_on(ytf):
    yt, fast = ytf
    yt.consider_entries([_tok()], now=T0)
    n = fast.sync_entries(now=T0)
    assert n == 2  # baseline + tp_50
    opens = fast.open_rows()
    assert {r["variant"] for r in opens} == {"baseline", "tp_50"}
    for r in opens:
        assert r["tid"] == TID
        assert r["entry_price"] == pytest.approx(ENTRY)
        assert r["entry_ts"] == pytest.approx(T0)
        assert r["peak"] == pytest.approx(ENTRY)
        assert r["time_to_peak_sec"] == pytest.approx(0.0)
        assert r["exit_ts"] is None


def test_does_not_backfill_pre_deploy_entries(ytf):
    yt, fast = ytf
    yt.consider_entries([_tok()], now=T0)
    fast.deploy_ts(now=T0 + 10)
    assert fast.sync_entries(now=T0 + 20) == 0
    assert fast.open_rows() == []


def test_60s_mark_hits_sl_while_cycle_still_open(ytf):
    yt, fast = ytf
    yt.consider_entries([_tok()], now=T0)
    fast.on_sampler_tick({TID: 0.70}, now=T0 + 60)
    base = [r for r in fast.closed_rows(limit=None) if r["variant"] == "baseline"]
    assert len(base) == 1
    assert base[0]["exit_reason"] == "sl"
    assert base[0]["exit_price"] == pytest.approx(0.70)
    assert base[0]["exit_ts"] == pytest.approx(T0 + 60)
    assert base[0]["pnl_usd"] == pytest.approx(-15.0)
    assert base[0]["peak"] == pytest.approx(1.0)
    assert yt.open_rows() and yt.open_rows()[0]["exit_ts"] is None
    assert yt.closed_rows() == []


def test_unmarked_time_exit_excluded_from_pnl(ytf):
    """No post-entry mark → close as unmarked, leave out of P&L stats."""
    yt, fast = ytf
    yt.consider_entries([_tok()], now=T0)
    assert fast.sync_entries(now=T0) == 2
    stats = fast.on_marks({}, now=T0 + 6 * 3600)
    assert stats["closed"] == 2
    closed = fast.closed_rows(limit=None)
    assert closed
    assert all(r["exit_reason"] == "unmarked" for r in closed)
    assert all(r["pnl_usd"] is None for r in closed)
    summary = fast.ops_payload()["summary"]["baseline"]
    assert summary["closed_count"] == 0
    assert summary["winning_trades"] == 0
    assert summary["losing_trades"] == 0
    assert summary["realized_pnl_usd"] == pytest.approx(0.0)


def test_time_exit_uses_last_valid_mark(ytf):
    yt, fast = ytf
    yt.consider_entries([_tok()], now=T0)
    fast.on_sampler_tick({TID: 1.10}, now=T0 + 60)
    opens = [r for r in fast.open_rows() if r["variant"] == "baseline"]
    assert opens
    assert opens[0]["last_price"] == pytest.approx(1.10)
    stats = fast.on_marks({}, now=T0 + 6 * 3600)
    assert stats["closed"] >= 1
    base = [r for r in fast.closed_rows(limit=None) if r["variant"] == "baseline"]
    assert len(base) == 1
    assert base[0]["exit_reason"] == "time"
    assert base[0]["exit_price"] == pytest.approx(1.10)
    assert base[0]["pnl_usd"] == pytest.approx(5.0)
    summary = fast.ops_payload()["summary"]["baseline"]
    assert summary["closed_count"] == 1
    assert summary["realized_pnl_usd"] == pytest.approx(5.0)


def test_tp_50_on_60s_mark(ytf):
    yt, fast = ytf
    yt.consider_entries([_tok()], now=T0)
    fast.on_sampler_tick({TID: 1.50}, now=T0 + 60)
    tp = [r for r in fast.closed_rows(limit=None) if r["variant"] == "tp_50"]
    base = [r for r in fast.open_rows() if r["variant"] == "baseline"]
    assert tp and tp[0]["exit_reason"] == "tp"
    assert tp[0]["pnl_usd"] == pytest.approx(25.0)
    assert tp[0]["peak"] == pytest.approx(1.50)
    assert tp[0]["time_to_peak_sec"] == pytest.approx(60.0)
    assert base and base[0]["exit_ts"] is None
    assert yt.open_rows()[0]["exit_ts"] is None


def test_sampler_reuses_dex_batch_no_extra_http(ytf, monkeypatch):
    yt, fast = ytf
    import research
    yt.consider_entries([_tok()], now=T0)
    calls = []

    def get(url, timeout=None, headers=None, **kw):
        calls.append(url)
        assert timeout == pytest.approx(5.0)
        assert "dexscreener.com/latest/dex/tokens/" in url
        assert "geckoterminal" not in url
        return type("R", (), {
            "status_code": 200,
            "json": lambda self=None: {"pairs": [{
                "chainId": "solana",
                "priceUsd": "0.70",
                "liquidity": {"usd": 9000},
                "baseToken": {"address": TID.split(":")[0], "symbol": "FST"},
            }]},
        })()

    research.set_get_fn(get)
    research.set_time_fn(lambda: T0 + 60)
    out = research.tick_sampler(now=T0 + 60)
    assert out["marks"] >= 1
    assert len(calls) == 1
    closed = [r for r in fast.closed_rows(limit=None) if r["variant"] == "baseline"]
    assert closed and closed[0]["exit_reason"] == "sl"
    assert yt.open_rows()[0]["exit_ts"] is None


def test_sampler_still_does_not_call_young_trail_exits(ytf):
    yt, fast = ytf
    import research
    called = []

    def boom(*a, **k):
        called.append(1)
        raise AssertionError("young_trail exits must not run")

    research.set_get_fn(lambda *a, **k: type("R", (), {
        "status_code": 200, "json": lambda self=None: {"pairs": []},
    })())
    from unittest.mock import patch
    with patch("young_trail.mark_open", side_effect=boom):
        with patch("young_trail.enter", side_effect=boom):
            research.tick_sampler(now=T0 + 30)
    assert called == []


def test_schema_idempotent_and_safe_on_live_db(tmp_path, monkeypatch):
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
    import young_trail_fast
    young_trail_fast.reset()
    conn = young_trail_fast._connect()
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "young_trail_fast" in names
    assert "young_trail_fast_meta" in names
    assert "young_trail" in names
    keep = conn.execute("SELECT ticker, entry_price FROM young_trail WHERE tid='keep:1'").fetchone()
    assert keep["ticker"] == "KEEP"
    assert keep["entry_price"] == pytest.approx(2.5)
    young_trail_fast.init_schema(conn)
    young_trail_fast.init_schema(conn)
    young_trail_fast.reset()


def test_schema_steps_commit_independently(tmp_path, monkeypatch):
    path = tmp_path / "partial.db"
    monkeypatch.setenv("OUTCOMES_DB", str(path))
    import young_trail_fast
    young_trail_fast.reset()
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    steps = []

    def boom():
        steps.append("boom")
        raise sqlite3.OperationalError("simulated fail")

    young_trail_fast._schema_step(db, "boom", boom)
    young_trail_fast._schema_step(db, "meta", lambda: db.execute(
        "CREATE TABLE IF NOT EXISTS young_trail_fast_meta (key TEXT PRIMARY KEY, value TEXT)"
    ))
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert "young_trail_fast_meta" in names
    assert steps == ["boom"]
    db.close()
    young_trail_fast.reset()


def test_until_matches_young_trail_oct20(ytf):
    yt, fast = ytf
    assert fast.until_ts(T0) == pytest.approx(yt.until_ts(T0))
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(fast.until_ts(T0)))
    assert iso == "2026-10-21T03:59:59Z"


def test_ops_payload_and_endpoint(ytf):
    yt, fast = ytf
    yt.consider_entries([_tok()], now=T0)
    fast.on_sampler_tick({TID: 0.70}, now=T0 + 60)
    payload = fast.ops_payload()
    assert payload["paper"] is True
    assert payload["simulated"] is True
    assert payload["live_orders"] is False
    assert payload["summary"]["baseline"]["closed_count"] == 1
    assert payload["comparison"]
    row = payload["comparison"][0]
    assert row["tid"] == TID
    assert row["fast_baseline_pnl_usd"] == pytest.approx(-15.0)
    assert row["cycle_pnl_usd"] is None
    blob = json.dumps(payload)
    assert "SUPERSECRET" not in blob
    from fastapi.testclient import TestClient
    import server
    resp = TestClient(server.app).get("/ops/young_trail_fast")
    assert resp.status_code == 200
    data = resp.json()
    assert data["paper"] is True
    assert data["live_orders"] is False
    assert data["summary"]["baseline"]["closed_count"] == 1


def test_ops_html_has_fast_section():
    html = (ROOT / "ops.html").read_text()
    assert 'id="yt-fast"' in html
    assert "/ops/young_trail_fast" in html
    assert "60s marks vs per-cycle" in html
    assert "https://" not in html


def test_no_secrets_in_payload(ytf, monkeypatch, caplog):
    yt, fast = ytf
    monkeypatch.setenv("COINGECKO_API_KEY", "cg-FAKESECRET-should-never-appear")
    monkeypatch.setenv("DESK_SECRET", "desk-secret-xyz-never")
    caplog.set_level("DEBUG")
    yt.consider_entries([_tok()], now=T0)
    fast.on_sampler_tick({TID: 0.7}, now=T0 + 60)
    blob = json.dumps(fast.ops_payload()) + caplog.text
    assert "cg-FAKESECRET-should-never-appear" not in blob
    assert "desk-secret-xyz-never" not in blob


def test_protected_files_untouched():
    for name in (
        "thresholds.py", "questions.py", "pick.py", "filter.py",
        "collect.py", "judge.py",
    ):
        r = subprocess.run(
            ["git", "diff", "--", name],
            cwd=str(ROOT), capture_output=True, text=True, check=True,
        )
        assert r.stdout == "", name
