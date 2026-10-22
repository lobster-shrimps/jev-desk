"""Ops UI for today's paper panels. Shadow/read-only. No protected files."""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DESK_SECRET", "test-secret-for-ops-today")
os.environ.setdefault("JUDGE_MOCK", "1")

OPS_ENDPOINTS = (
    "/ops",
    "/ops/summary",
    "/ops/desk_rugrisk",
    "/ops/rug_veto",
    "/ops/pick_confirm",
    "/ops/young_trail",
    "/ops/young_trail_variants",
    "/ops/young_trail_fast",
    "/ops/young_trail_helius",
    "/ops/young_trail_rugrisk",
    "/ops/young_trail_signals",
    "/api/shadow_ledger",
)


def _client():
    from fastapi.testclient import TestClient
    import server
    return TestClient(server.app)


def test_ops_html_renders_and_new_endpoints_exist():
    client = _client()
    html = client.get("/ops").text
    assert "JEV Ops" in html
    assert 'id="desk-rug"' in html
    assert 'id="rug-veto"' in html
    assert 'id="pick-confirm"' in html
    assert 'id="drr-err"' in html
    assert 'id="rv-err"' in html
    assert 'id="pc-err"' in html
    assert "shadow, does not gate picks" in html
    assert "1 row adjusted (bad price mark)" in html or "row adjusted (bad price mark)" in html
    assert "holders_source||'unknown'" in html or 'holders_source||"unknown"' in html
    assert "Oct 20 11:59 PM ET" not in html
    assert "/ops/desk_rugrisk" in html
    assert "/ops/rug_veto" in html
    assert "/ops/pick_confirm" in html
    assert "/ops/young_trail_variants" in html
    assert "/ops/summary" in html
    assert "softErr" in html
    for path in OPS_ENDPOINTS:
        resp = client.get(path)
        assert resp.status_code == 200, path


def test_ops_payloads_are_paper_only():
    client = _client()
    for path in (
        "/ops/desk_rugrisk", "/ops/rug_veto", "/ops/pick_confirm",
        "/ops/young_trail", "/ops/young_trail_variants",
    ):
        data = client.get(path).json()
        assert data.get("paper") is True or data.get("simulated") is True
        assert data.get("live_orders") is False


def test_young_trail_ops_marks_ended_after_until(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTCOMES_DB", str(tmp_path / "outcomes.db"))
    import young_trail
    young_trail.reset()
    young_trail.set_time_fn(lambda: 1_800_000_000.0)  # after 2026-10-20 23:59 ET
    payload = young_trail.ops_payload()
    assert payload["ended"] is True
    assert payload["ended_label"].startswith("Stopped taking entries")
    assert payload["accepting_entries"] is False
    young_trail.reset()
    monkeypatch.setenv("YOUNG_TRAIL_UNTIL", "2026-12-01T00:00:00Z")
    young_trail.set_time_fn(lambda: 1_760_000_000.0)
    later = young_trail.ops_payload()
    assert later["ended"] is False
    assert later["ended_label"].startswith("Accepting entries until")
    assert later["ended_label"] != payload["ended_label"]
    assert "Nov 30" in later["ended_label"]
    young_trail.reset()
    young_trail.set_time_fn(None)


def test_desk_rugrisk_ops_flattens_guards(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTCOMES_DB", str(tmp_path / "outcomes.db"))
    import desk_rugrisk
    desk_rugrisk.reset()
    desk_rugrisk.set_time_fn(lambda: 1_700_000_000.0)
    desk_rugrisk.enqueue(
        {"tid": "PepaMint:1399811149", "ticker": "PEPA", "chain": "solana",
         "net": 1399811149, "creator": "Dev1", "x_followers": 40,
         "developer_holding_percentage": 2.0},
        "pick", 0.72, 1_700_000_000.0,
    )
    payload = desk_rugrisk.ops_payload()
    assert payload["entries"]
    row = payload["entries"][0]
    assert row["ticker"] == "PEPA"
    assert row["is_pick"] is True
    assert "followers_lt_100" in row
    assert "shadow_guards" in row
    desk_rugrisk.reset()
    desk_rugrisk.set_time_fn(None)


def test_judge_hi_includes_holders_and_dev_sold(tmp_path, monkeypatch):
    monkeypatch.setenv("CYCLE_HISTORY_DB", str(tmp_path / "cycle_history.db"))
    import cycle_history
    import ops_summary
    cycle_history.reset()
    ops_summary.reset()
    now = 1_700_100_000.0
    cycle_history.on_cycle(
        {"seen": 1, "benched": 0, "judged": 1, "requeued": 0,
         "tokens": [{
             "tid": "Horn:1399811149", "ticker": "HORN", "net": 1399811149,
             "chain": "solana", "stage": "judged", "holder_count": 744,
             "holder_source": "trade_snapshot", "dev_sold_pct": 12.0,
             "judge_score": 0.72, "judge_confidence": 0.61, "judge_verdict": "pass",
         }]},
        now=now, source="live",
    )
    payload = ops_summary.summary_payload(now=now + 30, force=True)
    hi = payload["judge_hi"]
    horn = next(j for j in hi if j["ticker"] == "HORN")
    assert horn["holder_count"] == 744
    assert horn["holders_source"] == "trade_snapshot"
    cycle_history.on_cycle(
        {"seen": 1, "benched": 0, "judged": 1, "requeued": 0,
         "tokens": [{
             "tid": "Unk:1399811149", "ticker": "UNK", "net": 1399811149,
             "chain": "solana", "stage": "judged", "holder_count": 10,
             "judge_score": 0.70, "judge_verdict": "pass",
         }]},
        now=now + 10, source="live",
    )
    payload2 = ops_summary.summary_payload(now=now + 40, force=True)
    unk = next(j for j in payload2["judge_hi"] if j["ticker"] == "UNK")
    assert unk["holder_count"] == 10
    assert not unk.get("holders_source")
    assert horn["dev_sold_pct"] == pytest.approx(12.0)
    cycle_history.reset()
    ops_summary.reset()


def test_protected_files_untouched():
    for name in (
        "thresholds.py", "filter.py",
        "collect.py", "judge.py",
    ):
        r = subprocess.run(
            ["git", "diff", "--", name],
            cwd=str(ROOT), capture_output=True, text=True, check=True,
        )
        assert r.stdout == "", name
