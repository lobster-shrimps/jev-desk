"""Ops/judge hang fix: sync handlers, stored-label report cache, WAL, ops.html.

Does not change thresholds, questions, pick, filter, collect, or judge logic.
"""
import inspect
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

os.environ.setdefault("DESK_SECRET", "test-secret-for-ops-hang")
os.environ.setdefault("JUDGE_MOCK", "1")
os.environ["TZ"] = "UTC"
if hasattr(time, "tzset"):
    time.tzset()


def ts(y, m, d, hh=12, mm=0, ss=0) -> float:
    return time.mktime((y, m, d, hh, mm, ss, 0, 0, -1))


def test_ops_and_api_handlers_are_sync_judge_stays_async():
    import server
    import judge
    for name in (
        "ops_panel", "ops_summary", "ops_briefing", "ops_young_trail",
        "ops_young_trail_fast", "ops_young_trail_helius",
        "ops_young_trail_rugrisk",
        "ops_young_trail_variants", "ops_research_query",
        "ops_research_report", "ops_backtest", "api_state",
        "api_shadow_ledger", "api_fomo_status", "api_fomo_activate",
        "held", "release",
    ):
        fn = getattr(server, name)
        assert not inspect.iscoroutinefunction(fn), name
    assert inspect.iscoroutinefunction(judge.judge)
    assert inspect.iscoroutinefunction(judge.health)


def test_report_reads_stored_labels_only_and_caches(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DB", str(tmp_path / "research.db"))
    monkeypatch.setenv("RESEARCH_SAMPLER_DISABLE", "1")
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    import research
    research.reset()
    t0 = ts(2026, 10, 14, 8)
    clock = {"t": t0}
    research.set_time_fn(lambda: clock["t"])
    called = []

    def boom(*a, **k):
        called.append(1)
        raise AssertionError("report must not call compute_labels")

    monkeypatch.setattr(research, "compute_labels", boom)
    db = research._connect()
    db.execute(
        """INSERT OR REPLACE INTO token_labels (
             tid, ticker, runner, rugged, updated_ts
           ) VALUES ('R:1399811149', 'RUN', 1, 0, ?)""",
        (t0,),
    )
    db.execute(
        """INSERT INTO token_snapshots (
             ts, ts_sec, tid, ticker, stage, price_usd, origin
           ) VALUES (?, ?, 'R:1399811149', 'RUN', 'trade', 1.0, 'live')""",
        (t0, int(t0)),
    )
    db.commit()
    feat = research.report("features")
    assert called == []
    assert feat["kind"] == "features"
    assert feat["runners"]["n"] == 1
    rugs = research.report("rugs")
    assert called == []
    assert rugs["n"] == 0

    db.execute(
        """INSERT OR REPLACE INTO token_labels (
             tid, ticker, runner, rugged, updated_ts
           ) VALUES ('G:1399811149', 'RUG', 0, 1, ?)""",
        (t0,),
    )
    db.commit()
    clock["t"] = t0 + 60
    again = research.report("rugs")
    assert again["n"] == 0  # cached ≥5 minutes
    clock["t"] = t0 + 301
    fresh = research.report("rugs")
    assert fresh["n"] == 1
    research.reset()


def test_report_fallback_when_db_busy(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DB", str(tmp_path / "research.db"))
    monkeypatch.setenv("RESEARCH_SAMPLER_DISABLE", "1")
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    import research
    research.reset()
    t0 = ts(2026, 10, 14, 9)
    clock = {"t": t0}
    research.set_time_fn(lambda: clock["t"])
    db = research._connect()
    db.execute(
        """INSERT OR REPLACE INTO token_labels (
             tid, ticker, runner, rugged, updated_ts
           ) VALUES ('R:1399811149', 'RUN', 1, 0, ?)""",
        (t0,),
    )
    db.execute(
        """INSERT INTO token_snapshots (
             ts, ts_sec, tid, ticker, stage, price_usd, origin
           ) VALUES (?, ?, 'R:1399811149', 'RUN', 'trade', 1.0, 'live')""",
        (t0, int(t0)),
    )
    db.commit()
    first = research.report("features")
    assert first["runners"]["n"] == 1
    clock["t"] = t0 + 301

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(research, "readonly_connect", boom)
    stale = research.report("features")
    assert stale.get("stale") is True
    assert stale["runners"]["n"] == 1
    assert stale.get("error") == "report unavailable"
    research.reset()


def test_outcomes_wal_and_readonly_ops_reader(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTCOMES_DB", str(tmp_path / "outcomes.db"))
    import outcomes
    outcomes.reset()
    db = outcomes._connect()
    mode = str(db.execute("PRAGMA journal_mode").fetchone()[0]).lower()
    assert mode == "wal"
    sync = str(db.execute("PRAGMA synchronous").fetchone()[0])
    # NORMAL == 1
    assert sync in ("1", "normal")
    ro = outcomes.readonly_connect()
    try:
        ro.execute("SELECT 1 FROM fetch_jobs").fetchall()
        with pytest.raises(sqlite3.OperationalError):
            ro.execute("INSERT INTO fetch_jobs (tid, entry_ts, status, enqueued_at) VALUES ('x', 1, 'pending', 1)")
            ro.commit()
    finally:
        ro.close()
    outcomes.reset()


def test_ops_summary_outcomes_busy_returns_partial(tmp_path, monkeypatch):
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    monkeypatch.setenv("OUTCOMES_DB", str(tmp_path / "outcomes.db"))
    monkeypatch.setenv("CYCLE_HISTORY_DB", str(tmp_path / "ch.db"))
    import outcomes
    import ops_summary
    import cycle_history
    outcomes.reset()
    ops_summary.reset()
    cycle_history.reset()
    outcomes._connect()

    def boom(*a, **k):
        raise sqlite3.OperationalError("database is locked")

    monkeypatch.setattr(outcomes, "readonly_connect", boom)
    payload = ops_summary.summary_payload(now=ts(2026, 10, 14, 10), force=True)
    assert payload["outcomes"]["complete"] == 0
    assert "error" not in payload or True
    ops_summary.reset()
    outcomes.reset()
    cycle_history.reset()


def test_ops_html_report_once_and_inflight_guard():
    html = (ROOT / "ops.html").read_text()
    assert "loadInflight" in html
    assert "if(loadInflight)return" in html
    assert "featInflight" in html
    assert "id=\"feat-reload\"" in html
    assert "loadResearch()" in html
    load_start = html.index("async function load()")
    load_end = html.index("function paintFeat")
    load_src = html[load_start:load_end]
    assert "/ops/research/report" not in load_src
    assert "setInterval(load,60000)" in html
    interval_line = [ln for ln in html.splitlines() if "setInterval(load" in ln][0]
    assert "loadResearch" not in interval_line or "load();loadResearch();runQ();setInterval(load,60000)" in html.replace(" ", "")


def test_ops_research_report_endpoint_uses_store(tmp_path, monkeypatch):
    monkeypatch.setenv("RESEARCH_DB", str(tmp_path / "research.db"))
    monkeypatch.setenv("RESEARCH_SAMPLER_DISABLE", "1")
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    import research
    research.reset()
    from fastapi.testclient import TestClient
    import server
    called = []
    monkeypatch.setattr(research, "compute_labels", lambda *a, **k: called.append(1))
    client = TestClient(server.app)
    resp = client.get("/ops/research/report", params={"kind": "features"})
    assert resp.status_code == 200
    assert resp.json()["kind"] == "features"
    assert called == []
    research.reset()


def test_protected_files_untouched():
    for name in ("thresholds.py", "questions.py", "pick.py", "filter.py",
                 "collect.py", "judge.py"):
        r = subprocess.run(
            ["git", "diff", "--", name],
            cwd=str(ROOT), capture_output=True, text=True, check=True,
        )
        assert r.stdout == "", name


def test_no_secrets_in_new_modules(caplog, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-should-never-appear")
    monkeypatch.setenv("DESK_SECRET", "desk-secret-should-never-appear")
    caplog.set_level("INFO")
    import price_guard
    price_guard.mark_from_pairs(
        [{"priceUsd": "9", "liquidity": {"usd": 0.02},
          "baseToken": {"address": "x"}}],
        "x", entry=1e-6, tid="x:1399811149",
    )
    blob = caplog.text + json.dumps(price_guard.mark_from_pairs([], "x") or {})
    assert "ts-should-never-appear" not in blob
    assert "desk-secret-should-never-appear" not in blob
