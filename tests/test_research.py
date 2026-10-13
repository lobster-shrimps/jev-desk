"""Research store: snapshots, Dex sampler, labels, SQL, ops, briefing.

No network. No real sleeps. thresholds/questions/pick/filter and 0.6 untouched.
"""
import json
import logging
import os
import pathlib
import subprocess
import sys
import time
from unittest.mock import Mock, patch

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DESK_SECRET", "test-secret-for-research")
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
    assert 'id="briefing-early-signals"' in html
    assert 'id="research-panel"' in html
    assert "/ops/research/query" in html
    assert "/ops/research/report" in html
    assert "renderResearchFeatures" in html


def test_protected_files_untouched():
    for name in ("thresholds.py", "questions.py", "pick.py", "filter.py"):
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
