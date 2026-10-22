"""Research store: snapshots, Dex sampler, labels, SQL, ops, briefing.

No network. No real sleeps. thresholds/questions/pick/filter and 0.6 untouched.
"""
import gzip
import json
import logging
import os
import pathlib
import sqlite3
import subprocess
import sys
import threading
import time
from unittest.mock import Mock, patch

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DESK_SECRET", "test-secret-for-research")
os.environ.setdefault("JUDGE_MOCK", "1")
os.environ.setdefault("DESK_OUTBOX", str(ROOT / "tests" / "_outbox"))
os.environ["TZ"] = "UTC"
if hasattr(time, "tzset"):
    time.tzset()

import research  # noqa: E402


def ts(y, m, d, hh=12, mm=0, ss=0) -> float:
    return time.mktime((y, m, d, hh, mm, ss, 0, 0, -1))


def _resp(status, body):
    m = Mock(status_code=status)
    m.json.return_value = body
    return m


def _stats(**over):
    base = {
        "seen": 8, "benched": 1, "judged": 1, "requeued": 0,
        "free": {}, "trade": {}, "chain": {}, "soft": {},
        "tokens": [], "young_free": [],
    }
    base.update(over)
    return base


@pytest.fixture
def rs(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DB", str(tmp_path / "research.db"))
    monkeypatch.setenv("RESEARCH_SAMPLER_DISABLE", "1")
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    research.reset()
    research.set_time_fn(None)
    research.set_sleep_fn(lambda _d: None)
    research.set_get_fn(lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("research tests must not use the network")
    ))
    yield research
    research.reset()
    research.set_get_fn(None)
    research.set_sleep_fn(None)
    research.set_time_fn(None)


def test_wal_and_schema(rs):
    db = rs._connect()
    mode = db.execute("PRAGMA journal_mode").fetchone()[0]
    assert str(mode).lower() == "wal"
    names = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"token_snapshots", "fast_marks", "token_labels"} <= names


def test_connect_skips_wal_pragma_when_already_wal(rs):
    first = rs._connect()
    assert str(first.execute("PRAGMA journal_mode").fetchone()[0]).lower() == "wal"
    rs.reset()
    issued = []
    real_connect = sqlite3.connect

    def wrapped(*a, **k):
        conn = real_connect(*a, **k)

        def tracer(sql):
            text = "".join(str(sql).split()).lower()
            if "journal_mode=" in text:
                issued.append(sql)

        conn.set_trace_callback(tracer)
        return conn

    with patch.object(rs.sqlite3, "connect", side_effect=wrapped):
        second = rs._connect()
        mode = str(second.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    assert mode == "wal"
    assert issued == []


def test_live_snapshot_origin_is_live(rs):
    t0 = ts(2026, 10, 13, 12)
    rs.on_cycle(_stats(tokens=[{
        "tid": "L:1399811149", "ticker": "LIVE", "stage": "soft", "price_usd": 1,
    }]), now=t0)
    row = rs.run_sql("SELECT origin FROM token_snapshots")[0]
    assert row["origin"] == "live"


def test_on_cycle_merges_shortlist_price_usd(rs):
    t0 = ts(2026, 10, 13, 12)
    rs.set_time_fn(lambda: t0)
    stats = _stats(tokens=[{
        "tid": "HelLo:1399811149", "ticker": "hellop", "chain": "solana",
        "net": 1399811149, "stage": "soft", "reason": "momentum_already_spent",
        "age_minutes": 22, "soft_noul": 0.41, "liquidity_usd": 40_000,
        "mcap_usd": 90_000, "holder_count": 400,
        "top_wallet_percent": 0.08, "top_10_percent": 22.0,
        "x_followers": 1200, "judge_score": 0.7, "judge_confidence": 0.62,
        "judge_verdict": "no",
    }])
    shortlist = [{
        "tid": "HelLo:1399811149", "ticker": "hellop", "net": 1399811149,
        "price_usd": 0.0042, "buys_m5": 12, "sells_m5": 9,
        "buys_h1": 80, "sells_h1": 40, "holder_growth": "holders +50 in 30m",
        "x_handle": "hellopcoin", "x_account": {"followers": 1200, "x_kind": "official"},
        "trades_source": "ok",
    }]
    n = rs.on_cycle(stats, tokens=shortlist, now=t0)
    assert n == 1
    rows = rs.run_sql("SELECT * FROM token_snapshots")
    assert len(rows) == 1
    r = rows[0]
    assert r["price_usd"] == pytest.approx(0.0042)
    assert r["stage"] == "soft"
    assert r["kill_reason"] == "momentum_already_spent"
    assert r["noul"] == pytest.approx(0.41)
    assert r["buys_5m"] == 12
    assert r["x_handle"] == "hellopcoin"
    assert r["x_kind"] == "official"
    assert r["trades_source"] == "ok"


def test_labels_peak_runner_and_rug(rs):
    t0 = ts(2026, 10, 13, 10)
    tid = "Run:1399811149"
    rs.on_cycle(_stats(tokens=[{
        "tid": tid, "ticker": "RUN", "chain": "solana", "stage": "trade",
        "reason": "pass", "age_minutes": 18, "price_usd": 1.0, "liquidity_usd": 10_000,
    }]), now=t0)
    db = rs._connect()
    db.execute(
        """INSERT OR REPLACE INTO fast_marks
           (ts, ts_sec, tid, price_usd, liq_usd, source) VALUES (?,?,?,?,?,?)""",
        (t0 + 600, int(t0 + 600), tid, 3.2, 12_000, "dex_batch"),
    )
    db.execute(
        """INSERT OR REPLACE INTO fast_marks
           (ts, ts_sec, tid, price_usd, liq_usd, source) VALUES (?,?,?,?,?,?)""",
        (t0 + 1800, int(t0 + 1800), tid, 0.005, 1_500, "dex_batch"),
    )
    db.commit()
    rs.set_time_fn(lambda: t0 + 6 * 3600)
    n = rs.compute_labels(tids=[tid], now=t0 + 6 * 3600)
    assert n == 1
    lab = rs.run_sql("SELECT * FROM token_labels")[0]
    assert lab["peak_x"] == pytest.approx(3.2)
    assert lab["runner"] == 1
    assert lab["rugged"] == 1
    assert lab["time_to_peak_min"] == pytest.approx(10.0)
    assert lab["ret_15m"] == pytest.approx(2.2)


def test_sampler_batch_url_cap_timeout_no_gt(rs, caplog):
    caplog.set_level(logging.INFO)
    t0 = ts(2026, 10, 13, 11)
    rs.set_time_fn(lambda: t0)
    tids = []
    for i in range(40):
        tid = f"Tok{i}:1399811149"
        tids.append(tid)
        rs.on_cycle(_stats(tokens=[{
            "tid": tid, "ticker": f"T{i}", "stage": "trade", "reason": "pass",
            "age_minutes": 10, "price_usd": 1.0, "liquidity_usd": 5000,
        }]), now=t0)
    calls = []

    def get(url, timeout=None, headers=None, **kw):
        calls.append((url, timeout))
        assert timeout == pytest.approx(5.0)
        assert "dexscreener.com/latest/dex/tokens/" in url
        assert "geckoterminal" not in url
        addrs = url.rsplit("/", 1)[-1].split(",")
        assert len(addrs) <= 30
        pairs = []
        for a in addrs:
            pairs.append({
                "chainId": "solana",
                "priceUsd": "1.25",
                "fdv": 80_000,
                "liquidity": {"usd": 9_000},
                "volume": {"m5": 400, "h1": 2000, "h24": 9000},
                "baseToken": {"address": a, "symbol": "T"},
            })
        return _resp(200, {"pairs": pairs})

    rs.set_get_fn(get)
    with patch("young_trail.open_rows", return_value=[{"tid": "Tok0:1399811149"}]):
        out = rs.tick_sampler(now=t0 + 30)
    assert out["n"] <= 30
    assert out["marks"] >= 1
    assert calls and all(c[1] == pytest.approx(5.0) for c in calls)
    marks = rs.run_sql("SELECT * FROM fast_marks")
    assert marks and marks[0]["price_usd"] == pytest.approx(1.25)
    assert marks[0]["fdv"] == pytest.approx(80_000)
    assert not any("geckoterminal" in (c[0] or "") for c in calls)


def test_sampler_does_not_call_young_trail_exits(rs):
    called = []

    def boom(*a, **k):
        called.append((a, k))
        raise AssertionError("young_trail exits must not run")

    rs.set_get_fn(lambda *a, **k: _resp(200, {"pairs": []}))
    with patch("young_trail.open_rows", return_value=[]):
        with patch("young_trail.mark_open", side_effect=boom):
            with patch("young_trail.enter", side_effect=boom):
                rs.tick_sampler()
    assert called == []


def test_sql_select_only_and_limit(rs):
    t0 = ts(2026, 10, 13, 9)
    for i in range(3):
        rs.on_cycle(_stats(tokens=[{
            "tid": f"A{i}:1399811149", "ticker": f"A{i}", "stage": "chain",
            "reason": "top_wallet", "age_minutes": 20, "price_usd": 1,
        }]), now=t0 + i)
    rows = rs.run_sql("SELECT ticker FROM token_snapshots ORDER BY ticker")
    assert [r["ticker"] for r in rows] == ["A0", "A1", "A2"]
    with pytest.raises(ValueError):
        rs.run_sql("INSERT INTO token_labels (tid) VALUES ('x')")
    with pytest.raises(ValueError):
        rs.run_sql("DELETE FROM token_snapshots")
    with pytest.raises(ValueError):
        rs.run_sql("SELECT 1; SELECT 2")
    payload = rs.ops_query("UPDATE token_snapshots SET ticker='x'")
    assert payload["error"]
    assert payload["rows"] == []


def test_ops_research_query_and_report(rs):
    t0 = ts(2026, 10, 13, 8)
    rs.on_cycle(_stats(tokens=[{
        "tid": "Q:1399811149", "ticker": "QQ", "stage": "judged",
        "price_usd": 1.0, "liquidity_usd": 20_000, "age_minutes": 30,
    }]), now=t0)
    from fastapi.testclient import TestClient
    import server
    client = TestClient(server.app)
    bad = client.get("/ops/research/query", params={"sql": "DROP TABLE token_snapshots"})
    assert bad.status_code == 400
    ok = client.get("/ops/research/query", params={"sql": "SELECT ticker FROM token_snapshots"})
    assert ok.status_code == 200
    data = ok.json()
    assert data["n"] >= 1
    assert data["rows"][0]["ticker"] == "QQ"
    assert "SUPERSECRET" not in json.dumps(data)
    feat = client.get("/ops/research/report", params={"kind": "features"})
    assert feat.status_code == 200
    assert feat.json()["kind"] == "features"


def test_cli_sql(rs, capsys):
    t0 = ts(2026, 10, 13, 7)
    rs.on_cycle(_stats(tokens=[{
        "tid": "C:1399811149", "ticker": "CLI", "stage": "soft", "price_usd": 2,
    }]), now=t0)
    assert rs.main(["sql", "SELECT ticker FROM token_snapshots"]) == 0
    out = json.loads(capsys.readouterr().out)
    assert out[0]["ticker"] == "CLI"


def test_backfill_from_cycle_history_and_young_trail(rs, tmp_path, monkeypatch):
    import cycle_history
    monkeypatch.setenv("CYCLE_HISTORY_DB", str(tmp_path / "cycle_history.db"))
    cycle_history.reset()
    t0 = ts(2026, 10, 12, 12)
    cycle_history.on_cycle({
        "seen": 4, "benched": 0, "judged": 1, "requeued": 0,
        "free": {}, "trade": {}, "chain": {"top_wallet": 1}, "soft": {},
        "tokens": [{
            "tid": "WHL:1399811149", "ticker": "WHL", "net": 1399811149,
            "stage": "chain", "reason": "top_wallet", "age_minutes": 22,
            "liquidity_usd": 33_000, "mcap_usd": 80_000,
        }],
        "young_free": [{
            "tid": "YNG:1399811149", "ticker": "YNG", "net": 1399811149,
            "age_minutes": 16, "liquidity_usd": 12_000, "mcap_usd": 40_000,
        }],
    }, now=t0, source="backfill")
    import young_trail
    young_trail.reset()
    young_trail.set_time_fn(lambda: t0)
    young_trail.enter("YNG:1399811149", {"ticker": "YNG", "chain": "solana"}, 0.5, now=t0)
    result = rs.backfill()
    assert result["token_snapshots"] >= 2
    tickers = {r["ticker"] for r in rs.run_sql("SELECT ticker FROM token_snapshots")}
    assert "WHL" in tickers
    assert result["token_labels"] >= 1
    origins = {r["origin"] for r in rs.run_sql("SELECT origin FROM token_snapshots")}
    assert any(o.startswith("backfill:") for o in origins)
    assert "per_source" in result
    assert result["span"]["min_ts"] is not None
    cycle_history.reset()


def test_briefing_early_signals_section(rs, tmp_path, monkeypatch):
    import cycle_history
    monkeypatch.setenv("CYCLE_HISTORY_DB", str(tmp_path / "ch.db"))
    cycle_history.reset()
    payload = cycle_history.build_briefing(now=ts(2026, 10, 13, 8))
    assert "## Early signals (research)" in payload["markdown"]
    assert "does not change kills, judge, pick" in payload["markdown"].lower()
    assert "early_signals" in payload
    assert "TYPESAFE" not in payload["markdown"]
    cycle_history.reset()


def test_ops_html_has_research_hooks():
    html = (ROOT / "ops.html").read_text()
    assert 'id="research"' in html
    assert 'id="q-sql"' in html
    assert "/ops/research/query" in html
    assert "/ops/research/report" in html
    assert "kind=features" in html


def test_protected_files_untouched():
    for name in ("thresholds.py", "filter.py"):
        r = subprocess.run(
            ["git", "diff", "--", name],
            cwd=str(ROOT), capture_output=True, text=True, check=True,
        )
        assert r.stdout == ""
    src = (ROOT / "thresholds.py").read_text()
    assert "PICK_MIN_WORTH  = 0.60" in src


def test_no_keys_in_research_logs(rs, caplog, monkeypatch):
    caplog.set_level(logging.DEBUG)
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-should-never-appear")
    monkeypatch.setenv("DESK_SECRET", "desk-secret-should-never-appear")
    rs.on_cycle(_stats(tokens=[{
        "tid": "K:1399811149", "ticker": "KK", "stage": "soft", "price_usd": 1,
    }]), now=ts(2026, 10, 13, 6))
    rs.ops_query("SELECT 1")
    blob = caplog.text
    assert "ts-should-never-appear" not in blob
    assert "desk-secret-should-never-appear" not in blob


def test_backfill_rotated_logs_and_judge_lines(rs, tmp_path, monkeypatch):
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    log1 = tmp_path / "run.log.1"
    log1.write_text(
        "2026-10-01 08:00:00 desk INFO judge tid=Old:1399811149 ticker=OLD "
        "verdict=yes score=0.72 confidence=0.61 reason=momentum mode=live\n"
        "2026-10-01 08:00:01 desk INFO cycle: 3 seen, 0 benched, free {}, trade {}, "
        "chain {}, soft {}, judged 1, requeued 0\n"
    )
    n = rs.backfill_from_run_log()
    assert sum(n.values()) >= 1
    rows = rs.run_sql("SELECT ticker, origin, judge_verdict, stage FROM token_snapshots")
    assert any(r["ticker"] == "OLD" for r in rows)
    assert any(str(r["origin"]).startswith("backfill:run.log") for r in rows)


def test_backfill_shadow_ledger_and_state_json(rs, tmp_path, monkeypatch):
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    (tmp_path / "shadow_ledger.jsonl").write_text(json.dumps({
        "action": "entry", "ts": "2026-10-10T12:00:00Z",
        "ticker": "SHD", "address": "ShdAddr", "network_id": 1399811149,
        "chain": "solana", "entry_price_usd": 0.02,
    }) + "\n")
    (tmp_path / "state.json").write_text(json.dumps({
        "updated_at": "2026-10-11T12:00:00Z",
        "tokens": [{
            "tid": "St:1399811149", "ticker": "STAT", "stage": "chain",
            "reason": "top_wallet", "age_minutes": 19, "liquidity_usd": 8000,
        }],
    }))
    js = rs.backfill_from_cycle_json()
    sh = rs.backfill_from_shadow_jsonl()
    assert sh >= 1
    assert sum(js.values()) >= 1
    tickers = {r["ticker"] for r in rs.run_sql("SELECT ticker FROM token_snapshots")}
    assert "SHD" in tickers
    assert "STAT" in tickers


def test_backfill_outcomes_candles_and_queue_missing(rs, tmp_path, monkeypatch):
    import outcomes
    t0 = ts(2026, 10, 1, 1)
    candles = [{"ts": t0 + 300 * i, "close": 1.0 + i * 0.1} for i in range(4)]
    db = outcomes._connect()
    db.execute(
        """INSERT INTO outcomes (tid, entry_ts, candles_json, complete, peak_x)
           VALUES (?,?,?,?,?)""",
        ("Cndl:1399811149", t0, json.dumps(candles), 1, 1.3),
    )
    db.commit()
    n = rs.backfill_from_outcomes()
    assert n >= 1
    marks = rs.run_sql("SELECT origin, source FROM fast_marks")
    assert any(r["origin"] == "backfill:outcomes" for r in marks)

    t1 = ts(2026, 10, 12, 3)
    rs.on_cycle(_stats(tokens=[{
        "tid": "NeedPx:1399811149", "ticker": "NOPX", "stage": "chain",
        "reason": "top_wallet", "age_minutes": 20,
    }]), now=t1, origin="backfill:shadow_kills", skip_labels=True)
    queued = []

    def capture(tid, entry_ts, now=None, priority=0):
        queued.append((tid, entry_ts, priority))
        return True

    monkeypatch.setattr(outcomes, "enqueue_job", capture)
    nq = rs.queue_missing_prices(now=t1 + 3600)
    assert nq["queued"] >= 1
    assert "skipped_by_cap" in nq
    assert any(q[0] == "NeedPx:1399811149" for q in queued)
    assert any(q[2] == outcomes.PRIORITY_BACKFILL for q in queued)


def test_backfill_x_cache_and_coinalyze(rs, tmp_path, monkeypatch):
    import x_lookup
    import cycle_history
    t0 = ts(2026, 10, 8, 9)
    rs.on_cycle(_stats(tokens=[{
        "tid": "Xtok:1399811149", "ticker": "XT", "stage": "soft",
        "x_handle": "tokacct", "price_usd": 1, "age_minutes": 20,
    }]), now=t0)
    xdb = x_lookup._connect()
    xdb.execute(
        "INSERT OR REPLACE INTO x_cache (handle, payload_json, ok, fetched_at) VALUES (?,?,?,?)",
        ("tokacct", json.dumps({"followers": 4242, "x_kind": "official"}), 1, t0),
    )
    xdb.commit()
    n = rs.backfill_from_x_cache()
    assert n >= 1
    row = rs.run_sql("SELECT x_followers, x_kind FROM token_snapshots WHERE ticker='XT'")[0]
    assert row["x_followers"] == 4242

    monkeypatch.setenv("CYCLE_HISTORY_DB", str(tmp_path / "ch_reg.db"))
    cycle_history.reset()
    cid = cycle_history.on_cycle(_stats(tokens=[{
        "tid": "Rg:1399811149", "ticker": "RG", "net": 1399811149,
        "stage": "chain", "reason": "top_wallet", "age_minutes": 15,
    }]), now=t0, source="backfill")
    ch = cycle_history._connect()
    ch.execute(
        "INSERT INTO market_regimes (cycle_id, ts, ts_iso, regime, metrics_json, symbols_json) "
        "VALUES (?,?,?,?,?,?)",
        (cid or 1, t0, "2026-10-08T09:00:00Z", "risk_off", "{}", "{}"),
    )
    ch.commit()
    rs.backfill_from_cycle_history()
    # regime stamped onto matching snapshots
    # the RG token is in cycle_history, ingested then stamped
    regs = rs.run_sql("SELECT ticker, regime FROM token_snapshots WHERE ticker='RG'")
    assert regs
    assert regs[0]["regime"] == "risk_off"
    cycle_history.reset()


def test_coverage_report_span_and_per_source(rs):
    t0 = ts(2026, 9, 1, 0)
    t1 = ts(2026, 10, 13, 12)
    rs.on_cycle(_stats(tokens=[{
        "tid": "A:1399811149", "ticker": "A", "stage": "soft", "price_usd": 1,
    }]), now=t0)
    rs.on_cycle(_stats(tokens=[{
        "tid": "B:1399811149", "ticker": "B", "stage": "chain", "price_usd": 2,
    }]), now=t1, origin="backfill:run.log", skip_labels=True)
    cov = rs.coverage_report()
    assert cov["span"]["min_ts"] == pytest.approx(t0)
    assert cov["span"]["max_ts"] == pytest.approx(t1)
    assert cov["span"]["days"] == pytest.approx((t1 - t0) / 86400.0)
    assert cov["snapshots_by_origin"]["live"] >= 1
    assert cov["snapshots_by_origin"]["backfill:run.log"] >= 1
    assert cov["token_snapshots"] >= 2


def _need_px_token(i, t0):
    return {
        "tid": f"Cap{i}:1399811149", "ticker": f"C{i}", "stage": "chain",
        "reason": "top_wallet", "age_minutes": 20,
    }


def test_queue_missing_caps_newest_skips_existing_reports_cap(rs, tmp_path, monkeypatch):
    import outcomes
    monkeypatch.setenv("RESEARCH_QUEUE_MAX", "2")
    monkeypatch.setenv("OUTCOMES_DB", str(tmp_path / "outcomes.db"))
    outcomes.reset()
    t_old = ts(2026, 10, 1, 1)
    t_mid = ts(2026, 10, 10, 1)
    t_new = ts(2026, 10, 12, 3)
    for i, t in enumerate((t_old, t_mid, t_new, t_new + 60, t_new + 120)):
        rs.on_cycle(_stats(tokens=[{
            "tid": f"Cap{i}:1399811149", "ticker": f"C{i}", "stage": "chain",
            "reason": "top_wallet", "age_minutes": 20,
        }]), now=t, origin="backfill:shadow_kills", skip_labels=True)

    exist_tid = "Cap0:1399811149"
    odb = outcomes._connect()
    odb.execute(
        """INSERT INTO outcomes (tid, entry_ts, candles_json, complete)
           VALUES (?,?,?,?)""",
        (exist_tid, t_old, "[]", 0),
    )
    odb.commit()

    queued = []

    def capture(tid, entry_ts, now=None, priority=0):
        queued.append((tid, entry_ts, priority))
        return True

    monkeypatch.setattr(outcomes, "enqueue_job", capture)
    out = rs.queue_missing_prices(now=t_new + 3600)
    assert out["queued"] == 2
    assert out["skipped_by_cap"] >= 1
    assert out["skipped_existing"] >= 1
    assert exist_tid not in [q[0] for q in queued]
    assert all(q[2] == outcomes.PRIORITY_BACKFILL for q in queued)
    # newest first: Cap4 then Cap3 (t_new+120, t_new+60)
    assert queued[0][0] == "Cap4:1399811149"
    assert queued[1][0] == "Cap3:1399811149"
    result = rs.backfill()
    assert "skipped_by_cap" in result
    outcomes.reset()


def test_queue_skips_existing_fetch_jobs(rs, tmp_path, monkeypatch):
    import outcomes
    monkeypatch.setenv("OUTCOMES_DB", str(tmp_path / "outcomes.db"))
    monkeypatch.setenv("RESEARCH_QUEUE_MAX", "50")
    outcomes.reset()
    t0 = ts(2026, 10, 11, 2)
    tid = "Jobbed:1399811149"
    rs.on_cycle(_stats(tokens=[{
        "tid": tid, "ticker": "JB", "stage": "chain", "reason": "top_wallet",
        "age_minutes": 15,
    }]), now=t0, origin="backfill:run.log", skip_labels=True)
    assert outcomes.enqueue_job(tid, t0, now=t0, priority=outcomes.PRIORITY_LIVE)
    queued = []

    def capture(tid, entry_ts, now=None, priority=0):
        queued.append(tid)
        return True

    monkeypatch.setattr(outcomes, "enqueue_job", capture)
    out = rs.queue_missing_prices(now=t0 + 3600)
    assert tid not in queued
    assert out["queued"] == 0
    assert out["skipped_existing"] >= 1
    outcomes.reset()


def test_on_cycle_does_not_compute_labels(rs, monkeypatch):
    called = []
    monkeypatch.setattr(rs, "compute_labels", lambda *a, **k: called.append(1) or 0)
    monkeypatch.setattr(rs, "maybe_compute_labels", lambda *a, **k: called.append(2) or 0)
    rs.on_cycle(_stats(tokens=[{
        "tid": "L:1399811149", "ticker": "LL", "stage": "soft", "price_usd": 1,
    }]), now=ts(2026, 10, 13, 12))
    assert called == []


def test_sampler_throttles_and_times_labels(rs, caplog):
    caplog.set_level(logging.INFO)
    t0 = ts(2026, 10, 13, 11)
    rs.set_time_fn(lambda: t0)
    rs.on_cycle(_stats(tokens=[{
        "tid": "Tok0:1399811149", "ticker": "T0", "stage": "trade", "reason": "pass",
        "age_minutes": 10, "price_usd": 1.0, "liquidity_usd": 5000,
    }]), now=t0)
    times = []
    real = rs.compute_labels

    def wrapped(*a, **k):
        times.append(k.get("now") or (a[0] if a else None))
        return real(*a, **k)

    rs.compute_labels = wrapped

    def get(url, timeout=None, headers=None, **kw):
        return _resp(200, {"pairs": [{
            "chainId": "solana", "priceUsd": "1.1",
            "liquidity": {"usd": 9_000},
            "baseToken": {"address": "Tok0", "symbol": "T0"},
        }]})

    rs.set_get_fn(get)
    rs.tick_sampler(now=t0)
    rs.tick_sampler(now=t0 + 60)
    assert len(times) == 1
    rs.tick_sampler(now=t0 + rs.LABELS_INTERVAL_SEC + 1)
    assert len(times) == 2
    assert "elapsed=" in caplog.text
    assert "research labels n=" in caplog.text


def test_sqlite_lock_serializes_cycle_and_sampler_writes(rs):
    t0 = ts(2026, 10, 13, 12)
    errors = []

    def cycle_writer(i):
        try:
            rs.on_cycle(_stats(tokens=[{
                "tid": f"Cyc{i}:1399811149", "ticker": f"C{i}", "stage": "soft",
                "price_usd": 1,
            }]), now=t0 + i)
        except Exception as e:
            errors.append(e)

    def sampler_writer(i):
        try:
            with rs._locked() as db:
                rs._insert_mark(db, {
                    "ts": t0 + i, "ts_sec": int(t0 + i),
                    "tid": f"Sam{i}:1399811149", "price_usd": 1.2,
                    "source": "dex_batch", "origin": "live",
                })
                db.commit()
        except Exception as e:
            errors.append(e)

    threads = []
    for i in range(12):
        threads.append(threading.Thread(target=cycle_writer, args=(i,)))
        threads.append(threading.Thread(target=sampler_writer, args=(i,)))
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    snaps = rs.run_sql("SELECT tid FROM token_snapshots")
    marks = rs.run_sql("SELECT tid FROM fast_marks")
    assert len(snaps) == 12
    assert len(marks) == 12
    assert rs._db_lock is not None


def test_sqlite_connections_are_per_thread(rs):
    """Cycle, sampler, and ops threads must not share a Connection object."""
    import cycle_history
    import young_trail_fast
    import young_trail_helius
    import young_trail_rugrisk
    import young_trail_signals
    import desk_rugrisk

    for name in (
        "cycle_history.py", "research.py",
        "young_trail_fast.py", "young_trail_helius.py",
        "young_trail_rugrisk.py",
        "young_trail_signals.py",
        "desk_rugrisk.py",
    ):
        src = (ROOT / name).read_text()
        assert "check_same_thread=False" not in src, name

    seen: dict[str, object] = {}
    errors: list[BaseException] = []

    def worker(mod, key):
        try:
            seen[key] = mod._connect()  # keep alive: id() reuse after GC
        except BaseException as e:
            errors.append(e)

    for key, mod in (
        ("research", rs),
        ("cycle_history", cycle_history),
        ("young_trail_fast", young_trail_fast),
        ("young_trail_helius", young_trail_helius),
        ("young_trail_rugrisk", young_trail_rugrisk),
        ("young_trail_signals", young_trail_signals),
        ("desk_rugrisk", desk_rugrisk),
    ):
        t = threading.Thread(target=worker, args=(mod, key))
        t.start()
        t.join()
        assert errors == []
        assert seen[key] is not mod._connect(), key


def test_backfill_streams_gzip_logs_not_whole_read(rs, tmp_path, monkeypatch):
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    p = tmp_path / "run.log.gz"
    text = (
        "2026-10-02 08:00:00 desk INFO judge tid=Gz:1399811149 ticker=GZZ "
        "verdict=yes score=0.7 confidence=0.6 reason=x mode=live\n"
        "2026-10-02 08:00:01 desk INFO cycle: 1 seen, 0 benched, free {}, trade {}, "
        "chain {}, soft {}, judged 1, requeued 0\n"
    )
    with gzip.open(p, "wt") as f:
        f.write(text)

    def boom(path):
        raise AssertionError("must stream log files, not read() whole files")

    monkeypatch.setattr(rs, "_read_text_file", boom)
    n = rs.backfill_from_run_log()
    assert sum(n.values()) >= 1
    rows = rs.run_sql("SELECT ticker, origin FROM token_snapshots")
    assert any(r["ticker"] == "GZZ" for r in rows)


def test_regime_now_uses_last_regime_not_latest(rs, caplog):
    import coinalyze
    caplog.set_level(logging.INFO)
    assert not hasattr(coinalyze, "latest")
    coinalyze._last_snap = {"regime": "risk_on"}
    try:
        assert rs._regime_now() == "risk_on"
    finally:
        coinalyze._last_snap = None
    assert "has no attribute latest" not in caplog.text


def test_regime_now_falls_back_to_market_regimes(rs, tmp_path, monkeypatch):
    import coinalyze
    import cycle_history
    monkeypatch.setenv("CYCLE_HISTORY_DB", str(tmp_path / "ch_reg2.db"))
    cycle_history.reset()
    coinalyze._last_snap = None
    t0 = ts(2026, 10, 12, 8)
    db = cycle_history._connect()
    db.execute(
        "INSERT INTO market_regimes (cycle_id, ts, ts_iso, regime, metrics_json, symbols_json) "
        "VALUES (?,?,?,?,?,?)",
        (1, t0, "2026-10-12T08:00:00Z", "long_flush", "{}", "{}"),
    )
    db.commit()
    assert rs._regime_now() == "long_flush"
    cycle_history.reset()


def test_snapshot_wires_dex_txns_volume_and_x_cache(rs, tmp_path, monkeypatch):
    import x_lookup
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    x_lookup.reset()
    t0 = ts(2026, 10, 13, 12)
    xdb = x_lookup._connect()
    xdb.execute(
        "INSERT OR REPLACE INTO x_cache (handle, payload_json, ok, fetched_at) VALUES (?,?,?,?)",
        ("tokacct", json.dumps({"followers": 4242, "x_kind": "official"}), 1, t0),
    )
    xdb.commit()
    stats = _stats(tokens=[{
        "tid": "Dex:1399811149", "ticker": "DEX", "stage": "trade",
        "reason": "trades", "age_minutes": 18,
    }])
    shortlist = [{
        "tid": "Dex:1399811149", "ticker": "DEX", "net": 1399811149,
        "fomo_twitter": "tokacct",
        "txns": {"h1": {"buys": 80, "sells": 40}, "m5": {"buys": 12, "sells": 9}},
        "volume": {"h1": 12345.0, "m5": 100.0},
    }]
    n = rs.on_cycle(stats, tokens=shortlist, now=t0)
    assert n == 1
    row = rs.run_sql("SELECT * FROM token_snapshots")[0]
    assert row["buys_1h"] == pytest.approx(80)
    assert row["sells_1h"] == pytest.approx(40)
    assert row["vol_1h"] == pytest.approx(12345.0)
    assert row["vol_5m"] == pytest.approx(100.0)
    assert row["x_handle"] == "tokacct"
    assert row["x_followers"] == 4242
    assert row["x_kind"] == "official"
    x_lookup.reset()


def test_backfill_x_followers_and_buys_from_logs(rs, tmp_path, monkeypatch):
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    t0 = ts(2026, 10, 2, 8)
    rs.on_cycle(_stats(tokens=[{
        "tid": "Lg:1399811149", "ticker": "LG", "stage": "trade",
        "reason": "trades", "age_minutes": 20,
    }]), now=t0, origin="backfill:run.log", skip_labels=True)
    p = tmp_path / "run.log"
    p.write_text(
        "2026-10-02 08:00:00 desk INFO x tid=Lg:1399811149 handle=tokacct "
        "ok=True followers=777 own_posts=4 source=cache err=- x_kind=official\n"
        "2026-10-02 08:00:01 desk INFO trade tid=Lg:1399811149 ticker=LG "
        "reason=trades age_minutes=20 trades_h24=400 buys_h1=54 sells_h1=12 "
        "dex_status=ok trades_source=dex\n"
    )
    n = rs.backfill_from_run_log()
    assert sum(n.values()) >= 1
    row = rs.run_sql("SELECT x_followers, x_handle, x_kind, buys_1h FROM token_snapshots")[0]
    assert row["x_followers"] == 777
    assert row["x_handle"] == "tokacct"
    assert row["x_kind"] == "official"
    assert row["buys_1h"] == pytest.approx(54)


def test_on_cycle_merges_shortlist_buys_vol_followers(rs):
    t0 = ts(2026, 10, 13, 12)
    stats = _stats(tokens=[{
        "tid": "HelLo:1399811149", "ticker": "hellop", "stage": "soft",
        "reason": "momentum_already_spent", "age_minutes": 22,
        "x_followers": 1200,
    }])
    shortlist = [{
        "tid": "HelLo:1399811149",
        "buys_h1": 80, "vol_1h": 5000.0,
        "x_handle": "hellopcoin",
        "x_account": {"followers": 1200, "x_kind": "official"},
    }]
    rs.on_cycle(stats, tokens=shortlist, now=t0)
    r = rs.run_sql("SELECT buys_1h, vol_1h, x_followers FROM token_snapshots")[0]
    assert r["buys_1h"] == pytest.approx(80)
    assert r["vol_1h"] == pytest.approx(5000.0)
    assert r["x_followers"] == 1200
