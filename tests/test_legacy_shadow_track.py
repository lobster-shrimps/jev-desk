"""Log-only legacy track after a tp_50 close. No trading decisions."""
from __future__ import annotations

import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DESK_SECRET", "test-secret-for-legacy-track")
os.environ.setdefault("JUDGE_MOCK", "1")

T0 = 1_700_000_000.0
NET = 1399811149


@pytest.fixture
def lst(tmp_path, monkeypatch):
    import legacy_shadow_track
    import shadow_ledger
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    monkeypatch.setenv("OUTCOMES_DB", str(tmp_path / "outcomes.db"))
    monkeypatch.setattr(shadow_ledger, "LEDGER_PATH", tmp_path / "shadow_ledger.jsonl")
    monkeypatch.setattr(shadow_ledger, "OUTBOX", tmp_path)
    legacy_shadow_track.reset()
    legacy_shadow_track.set_time_fn(lambda: T0)
    yield legacy_shadow_track, shadow_ledger
    legacy_shadow_track.reset()
    legacy_shadow_track.set_time_fn(None)


@pytest.fixture
def pcs(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTCOMES_DB", str(tmp_path / "outcomes.db"))
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    import pick_confirm_shadow
    import legacy_shadow_track
    pick_confirm_shadow.reset()
    pick_confirm_shadow.set_time_fn(lambda: T0)
    legacy_shadow_track.reset()
    legacy_shadow_track.set_time_fn(lambda: T0)
    yield pick_confirm_shadow
    pick_confirm_shadow.reset()
    pick_confirm_shadow.set_time_fn(None)
    legacy_shadow_track.reset()
    legacy_shadow_track.set_time_fn(None)


def _order(ticker="TOK", address="tok1"):
    return {
        "token": {
            "ticker": ticker, "address": address,
            "network_id": NET, "chain": "solana",
        },
        "size_factor": 1.0, "model": "jev-1.13.0",
    }


def _judged(tid, ticker, score, price=1.0):
    return {
        "tid": tid, "ticker": ticker, "stage": "judged",
        "judge_score": score, "judge_verdict": "pass",
        "price_usd": price,
    }


def _confirm(pcs, tid, ticker="TOK", price=1.0, t0=T0):
    d = {"tid": tid, "ticker": ticker, "price_usd": price}
    pcs.on_cycle({"tokens": [_judged(tid, ticker, 0.7, price)]}, [(d, {})], now=t0)
    pcs.set_time_fn(lambda: t0 + 600)
    pcs.on_cycle({"tokens": [_judged(tid, ticker, 0.7, price)]}, [(d, {})], now=t0 + 600)
    return pcs.all_rows()[0]


class Fomo:
    def __init__(self, prices, calls=None):
        self.prices = prices
        self.calls = calls if calls is not None else []

    def tokens(self, tids):
        self.calls.append(list(tids))
        return {t: self.prices.get(t, {}) for t in tids}


def test_ledger_tp50_then_legacy_take_profit(lst):
    track, ledger = lst
    ledger.entry(_order(), {"price": 1.0})
    closed = ledger.mark("tok1", NET, current_price=1.6)
    assert closed["action"] == "close"
    assert "tp_50" in closed["reason"]
    assert ledger.open_positions() == []
    pending = track.pending_tracks()
    assert len(pending) == 1
    assert pending[0]["legacy_exit_reason"] is None
    tid = f"tok1:{NET}"
    out = track.mark_pending({tid: {"price": 2.1}}, now=T0 + 1800)
    assert out["resolved"] == 1
    row = track._ledger_rows()[0]
    assert row["legacy_exit_reason"] == "take_profit"
    assert row["legacy_exit_price"] == pytest.approx(2.1)
    assert row["legacy_pnl"] == pytest.approx(55.0)
    assert track.pending_tracks() == []


def test_ledger_tp50_then_legacy_stop_loss(lst):
    track, ledger = lst
    ledger.entry(_order("SL", "sl1"), {"price": 1.0})
    ledger.mark("sl1", NET, current_price=1.55)
    assert track.pending_tracks()
    track.mark_pending({f"sl1:{NET}": {"price": 0.65}}, now=T0 + 1200)
    row = track._ledger_rows()[0]
    assert row["legacy_exit_reason"] == "stop_loss"
    assert row["legacy_exit_price"] == pytest.approx(0.65)
    assert row["legacy_pnl"] == pytest.approx(-17.5)


def test_ledger_tp50_then_legacy_timeout(lst):
    track, ledger = lst
    ledger.entry(_order("TM", "tm1"), {"price": 1.0})
    ledger.mark("tm1", NET, current_price=1.5)
    pending = track.pending_tracks()[0]
    entry_ts = pending["entry_ts"]
    track.mark_pending({f"tm1:{NET}": {"price": 1.2}}, now=entry_ts + 6 * 3600)
    row = track._ledger_rows()[0]
    assert row["legacy_exit_reason"] == "time_stop"
    assert row["legacy_exit_price"] == pytest.approx(1.2)
    assert row["legacy_pnl"] == pytest.approx(10.0)


def test_ledger_fetch_failure_retries_then_unresolved(lst):
    track, ledger = lst
    ledger.entry(_order("FL", "fl1"), {"price": 1.0})
    ledger.mark("fl1", NET, current_price=1.5)
    pending = track.pending_tracks()[0]
    entry_ts = pending["entry_ts"]

    class Boom:
        def tokens(self, tids):
            raise RuntimeError("fomo down api-key=SECRET123")

    result = ledger.mark_all_open_positions(Boom())
    assert result["closed"] == 0
    assert len(track.pending_tracks()) == 1

    track.mark_pending({}, now=entry_ts + 6 * 3600 + 100)
    assert len(track.pending_tracks()) == 1

    track.mark_pending({}, now=entry_ts + 7 * 3600 + 1)
    row = track._ledger_rows()[0]
    assert row["legacy_exit_reason"] == "unresolved"
    assert row["legacy_pnl"] is None
    assert track.pending_tracks() == []


def test_pick_confirm_tp50_then_legacy_resolves(pcs):
    tid = f"Tok:{NET}"
    _confirm(pcs, tid, price=1.0)
    pcs.mark_open(Fomo({tid: {"price": 1.5}}), now=T0 + 700)
    row = pcs.all_rows()[0]
    assert row["exit_reason"].startswith("tp_50")
    assert row["pnl_usd"] == pytest.approx(25.0)
    assert row["legacy_exit_reason"] in (None, "")
    pcs.mark_open(Fomo({tid: {"price": 2.0}}), now=T0 + 1400)
    row = pcs.all_rows()[0]
    assert row["legacy_exit_reason"] == "take_profit"
    assert row["legacy_exit_price"] == pytest.approx(2.0)
    assert row["legacy_pnl"] == pytest.approx(50.0)


def test_ops_summary_counts_only_resolved_pairs(lst):
    track, ledger = lst
    ledger.entry(_order("A", "a1"), {"price": 1.0})
    ledger.mark("a1", NET, current_price=1.5)
    track.mark_pending({f"a1:{NET}": {"price": 2.0}}, now=T0 + 900)

    ledger.entry(_order("B", "b1"), {"price": 1.0})
    ledger.mark("b1", NET, current_price=1.6)
    assert len(track.pending_tracks()) == 1

    summary = track.comparison_summary()
    assert summary["pending_count"] == 1
    assert summary["tp_50"]["closed_count"] == 1
    assert summary["legacy"]["closed_count"] == 1
    assert summary["tp_50"]["wins"] == 1
    assert summary["legacy"]["wins"] == 1
    assert summary["tp_50"]["realized_pnl_usd"] == pytest.approx(25.0)
    assert summary["legacy"]["realized_pnl_usd"] == pytest.approx(50.0)
    assert summary["live_orders"] is False

    from fastapi.testclient import TestClient
    import server
    resp = TestClient(server.app).get("/api/shadow_ledger")
    assert resp.status_code == 200
    body = resp.json()["legacy_compare"]
    assert body["pending_count"] == 1
    assert body["tp_50"]["closed_count"] == 1
    assert body["legacy"]["closed_count"] == 1
    assert body["tp_50"]["realized_pnl_usd"] == pytest.approx(25.0)
    assert body["legacy"]["realized_pnl_usd"] == pytest.approx(50.0)


def test_ops_html_has_legacy_compare():
    html = (ROOT / "ops.html").read_text()
    assert 'id="leg-kv"' in html
    assert "tp_50 vs legacy" in html
    assert "legacy pending" in html


def test_no_protected_file_mentions():
    for name in ("thresholds.py", "filter.py", "collect.py", "judge.py"):
        r = subprocess.run(
            ["git", "diff", "--", name],
            cwd=str(ROOT), capture_output=True, text=True, check=True,
        )
        assert r.stdout == "", name
