"""Two-cycle pick confirm. Required for shadow fills. No live orders."""
from __future__ import annotations

import logging
import os
import pathlib
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DESK_SECRET", "test-secret-for-pick-confirm")
os.environ.setdefault("JUDGE_MOCK", "1")

T0 = 1_700_000_000.0
HORN = "HornMint111111111111111111111111111111:1399811149"
PROWLER = "ProwlerMint11111111111111111111111111:1399811149"


@pytest.fixture
def pcs(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTCOMES_DB", str(tmp_path / "outcomes.db"))
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    import pick_confirm_shadow
    pick_confirm_shadow.reset()
    pick_confirm_shadow.set_time_fn(lambda: T0)
    yield pick_confirm_shadow
    pick_confirm_shadow.reset()
    pick_confirm_shadow.set_time_fn(None)


def _judged(tid, ticker, score, price=0.001, verdict="pass"):
    return {
        "tid": tid, "ticker": ticker, "stage": "judged",
        "judge_score": score, "judge_verdict": verdict,
        "price_usd": price,
    }


def test_confirm_path(pcs):
    tid = "Tok:1399811149"
    d = {"tid": tid, "ticker": "TOK", "price_usd": 0.002}
    pcs.on_cycle({"tokens": [_judged(tid, "TOK", 0.72, 0.001)]}, [(d, {})], now=T0)
    row = pcs.all_rows()[0]
    assert row["status"] == "pending_confirm"
    d2 = {"tid": tid, "ticker": "TOK", "price_usd": 0.0024}
    out = pcs.on_cycle(
        {"tokens": [_judged(tid, "TOK", 0.68, 0.0024)]}, [(d2, {})], now=T0 + 600,
    )
    assert out["confirmed"] == 1
    row = pcs.all_rows()[0]
    assert row["status"] == "confirmed"
    assert row["outcome"] == "confirmed_pick"
    assert row["confirm_price"] == pytest.approx(0.0024)
    assert row["entry_price"] == pytest.approx(0.0024)
    assert row["exit_ts"] is None


def test_fail_judge_path(pcs):
    tid = "Tok:1399811149"
    pcs.on_cycle({"tokens": [_judged(tid, "TOK", 0.71)]}, now=T0)
    pcs.on_cycle({"tokens": [_judged(tid, "TOK", 0.51)]}, now=T0 + 600)
    row = pcs.all_rows()[0]
    assert row["status"] == "unconfirmed"
    assert row["outcome"] == "dropped_unconfirmed"
    assert "0.51" in (row["unconfirm_reason"] or "")


def test_fail_soft_kill_path(pcs):
    tid = "Tok:1399811149"
    pcs.on_cycle({"tokens": [_judged(tid, "TOK", 0.8)]}, now=T0)
    pcs.on_cycle({"tokens": [{
        "tid": tid, "ticker": "TOK", "stage": "soft",
        "reason": "shape_is_wrong",
    }]}, now=T0 + 600)
    row = pcs.all_rows()[0]
    assert row["status"] == "unconfirmed"
    assert row["outcome"] == "dropped_unconfirmed"
    assert "soft" in (row["unconfirm_reason"] or "")
    assert "shape" in (row["unconfirm_reason"] or "")


def test_not_seen_expires_after_two_cycles(pcs):
    tid = "Tok:1399811149"
    pcs.on_cycle({"tokens": [_judged(tid, "TOK", 0.77)]}, now=T0)
    pcs.on_cycle({"tokens": [_judged("Other:1399811149", "OTH", 0.9)]}, now=T0 + 600)
    row = [r for r in pcs.all_rows() if r["tid"] == tid][0]
    assert row["status"] == "pending_confirm"
    assert int(row.get("missed_cycles") or 0) == 1
    pcs.on_cycle({"tokens": [_judged("Other:1399811149", "OTH", 0.9)]}, now=T0 + 1200)
    row = [r for r in pcs.all_rows() if r["tid"] == tid][0]
    assert row["status"] == "unconfirmed"
    assert row["outcome"] == "expired"
    assert row["unconfirm_reason"] == "expired"


def test_horn_and_prowler_unconfirmed_from_history(pcs):
    """HORN 0.51 next look and PROWLER shape-kill next would not confirm."""
    pcs.on_cycle({"tokens": [
        _judged(HORN, "HORN", 0.72, 0.0004),
        _judged(PROWLER, "PROWLER", 0.81, 0.0009),
    ]}, now=T0)
    assert {r["ticker"]: r["status"] for r in pcs.all_rows()} == {
        "HORN": "pending_confirm", "PROWLER": "pending_confirm",
    }
    pcs.on_cycle({"tokens": [
        _judged(HORN, "HORN", 0.51, 0.00035),
        {"tid": PROWLER, "ticker": "PROWLER", "stage": "soft",
         "reason": "shape_is_wrong"},
    ]}, now=T0 + 700)
    by = {r["ticker"]: r for r in pcs.all_rows()}
    assert by["HORN"]["status"] == "unconfirmed"
    assert by["HORN"]["outcome"] == "dropped_unconfirmed"
    assert "0.51" in (by["HORN"]["unconfirm_reason"] or "")
    assert by["PROWLER"]["status"] == "unconfirmed"
    assert by["PROWLER"]["outcome"] == "dropped_unconfirmed"
    assert "shape" in (by["PROWLER"]["unconfirm_reason"] or "")


def test_real_pick_unchanged_when_module_raises(pcs, monkeypatch):
    from pick import pick
    from thresholds import PICK_MIN_CONF, PICK_MIN_WORTH

    def judge(question_set, state):
        return {"model": "stub-pick", "answers": {
            "best": {"type": "choice", "choice": "T1", "confidence": PICK_MIN_CONF,
                     "probabilities": {"T0": 0.1, "T1": PICK_MIN_CONF, "T2": 0.1}},
            "worth_trading_at_all": {"type": "noul", "noul": PICK_MIN_WORTH},
        }, "usage": {}}

    survivors = []
    for i in range(3):
        d = {
            "tid": f"T{i}:1399811149", "ticker": f"T{i}", "addr": f"Addr{i}",
            "net": 1399811149, "chain": "solana", "age_minutes": 20,
            "mcap_usd": 300_000, "liquidity_usd": 80_000, "holder_count": 200,
            "volume_h24": 100_000, "price_usd": 0.001,
        }
        ans = {"shape": {"choice": "crowd", "probabilities": {"crowd": 0.7}},
               "concentration_is_exit_risk": {"noul": 0.2},
               "authority_risk": {"choice": "renounced"}}
        survivors.append((d, ans))
    expected = pick(judge, survivors)

    def boom(*_a, **_k):
        raise RuntimeError("pick_confirm down")

    monkeypatch.setattr(pcs, "on_cycle", boom)
    monkeypatch.setattr(pcs, "mark_open", boom)
    got = pick(judge, survivors)
    assert got == expected
    assert got["token"]["ticker"] == "T1"


def test_apply_judge_verdicts_still_runs_when_confirm_raises(pcs, monkeypatch, caplog):
    import main as shift

    def boom(*_a, **_k):
        raise RuntimeError("pick_confirm exploded")

    monkeypatch.setattr(pcs, "on_cycle", boom)
    stats = {"tokens": [{
        "tid": "X:1399811149", "ticker": "X", "stage": "judged",
    }]}
    d = {"tid": "X:1399811149", "ticker": "X",
         "chain": "solana", "age_minutes": 20, "mcap_usd": 100000,
         "liquidity_usd": 20000, "holder_count": 100}
    ans = {"shape": {"confidence": 0.6, "probabilities": {"crowd": 0.6}},
           "liquidity_fits_ticket": {"noul": 0.7}}
    caplog.set_level(logging.WARNING)
    shift._apply_judge_verdicts(stats, [(d, ans)], None, {}, None)
    assert stats["tokens"][0]["judge_verdict"] is not None


def test_confirm_uses_tp50_and_logs_old_exit(pcs, caplog):
    tid = "Tok:1399811149"
    d = {"tid": tid, "ticker": "TOK", "price_usd": 1.0}
    pcs.on_cycle({"tokens": [_judged(tid, "TOK", 0.7, 1.0)]}, [(d, {})], now=T0)
    pcs.set_time_fn(lambda: T0 + 600)
    pcs.on_cycle({"tokens": [_judged(tid, "TOK", 0.7, 1.0)]}, [(d, {})], now=T0 + 600)
    row = pcs.all_rows()[0]
    assert row["exit_policy"] == "tp_50"
    class Fomo:
        def tokens(self, tids):
            return {tid: {"price": 1.5, "vol24": 1}}
    caplog.set_level("INFO")
    pcs.mark_open(Fomo(), now=T0 + 600)
    row = pcs.all_rows()[0]
    assert row["exit_reason"].startswith("tp_50")
    assert row["pnl_usd"] == pytest.approx(25.0)
    assert "legacy_would=False" in caplog.text or "legacy_would=" in caplog.text


def test_mark_uses_shadow_exits_not_ledger(pcs, tmp_path, monkeypatch):
    tid = "Tok:1399811149"
    d = {"tid": tid, "ticker": "TOK", "price_usd": 1.0}
    pcs.on_cycle({"tokens": [_judged(tid, "TOK", 0.7, 1.0)]}, [(d, {})], now=T0)
    pcs.set_time_fn(lambda: T0 + 600)
    pcs.on_cycle({"tokens": [_judged(tid, "TOK", 0.7, 1.0)]}, [(d, {})], now=T0 + 600)
    class Fomo:
        def tokens(self, tids):
            return {tid: {"price": 0.5, "vol24": 1}}
    pcs.mark_open(Fomo(), now=T0 + 600)
    row = pcs.all_rows()[0]
    assert row["status"] == "confirmed"
    assert row["exit_reason"].startswith("stop_loss")
    assert row["pnl_usd"] == pytest.approx(50.0 * (0.5 / 1.0 - 1.0))
    ledger = tmp_path / "shadow_ledger.jsonl"
    assert not ledger.exists()


def test_ops_payload_and_route(pcs):
    pcs.on_cycle({"tokens": [_judged("A:1399811149", "A", 0.9)]}, now=T0)
    payload = pcs.ops_payload()
    assert payload["gates_picks"] is True
    assert payload["live_orders"] is False
    assert payload["counts"]["pending_confirm"] == 1
    from fastapi.testclient import TestClient
    import server
    resp = TestClient(server.app).get("/ops/pick_confirm")
    assert resp.status_code == 200
    assert resp.json()["paper"] is True


def test_vetoed_outcome_skips_confirm_and_fill(pcs):
    tid = "Tok:1399811149"
    order = {"token": {"ticker": "TOK", "address": "Tok", "network_id": 1399811149}}
    d = {"tid": tid, "ticker": "TOK", "price_usd": 0.002}
    pcs.on_cycle(
        {"tokens": [_judged(tid, "TOK", 0.72, 0.002)]}, [(d, {})],
        vetoed={"skipped": True, "tid": tid, "ticker": "TOK",
                "reasons": ["sniper_share_ge_50"]},
        now=T0,
    )
    row = pcs.all_rows()[0]
    assert row["outcome"] == "vetoed"
    assert row["status"] == "unconfirmed"
    assert "sniper_share_ge_50" in (row["unconfirm_reason"] or "")
    assert pcs.fill_allowed(order) is False
    assert pcs.counts()["vetoed"] == 1


def test_two_cycle_confirm_still_works_without_veto(pcs):
    tid = "Tok:1399811149"
    order = {"token": {"ticker": "TOK", "address": "Tok", "network_id": 1399811149}}
    d = {"tid": tid, "ticker": "TOK", "price_usd": 0.002}
    pcs.on_cycle({"tokens": [_judged(tid, "TOK", 0.72, 0.002)]}, [(d, {})], now=T0)
    assert pcs.all_rows()[0]["status"] == "pending_confirm"
    assert pcs.fill_allowed(order) is False
    pcs.on_cycle({"tokens": [_judged(tid, "TOK", 0.68, 0.0024)]}, [(d, {})], now=T0 + 600)
    assert pcs.all_rows()[0]["outcome"] == "confirmed_pick"
    assert pcs.fill_allowed(order) is True


def test_pending_then_vetoed_on_second_cycle(pcs):
    tid = "Tok:1399811149"
    order = {"token": {"ticker": "TOK", "address": "Tok", "network_id": 1399811149}}
    d = {"tid": tid, "ticker": "TOK", "price_usd": 0.002}
    pcs.on_cycle({"tokens": [_judged(tid, "TOK", 0.72, 0.002)]}, [(d, {})], now=T0)
    pcs.on_cycle(
        {"tokens": [_judged(tid, "TOK", 0.68, 0.0024)]}, [(d, {})],
        vetoed={"skipped": True, "tid": tid, "ticker": "TOK",
                "reasons": ["followers_lt_100"]},
        now=T0 + 600,
    )
    row = pcs.all_rows()[0]
    assert row["outcome"] == "vetoed"
    assert pcs.fill_allowed(order) is False


def test_first_pass_no_fill_second_pass_fills(pcs):
    tid = "Tok:1399811149"
    order = {"token": {"ticker": "TOK", "address": "Tok", "network_id": 1399811149}}
    d = {"tid": tid, "ticker": "TOK", "price_usd": 0.002}
    pcs.on_cycle({"tokens": [_judged(tid, "TOK", 0.72, 0.002)]}, [(d, {})], now=T0)
    assert pcs.all_rows()[0]["status"] == "pending_confirm"
    assert pcs.fill_allowed(order) is False
    pcs.on_cycle({"tokens": [_judged(tid, "TOK", 0.68, 0.0024)]}, [(d, {})], now=T0 + 600)
    assert pcs.all_rows()[0]["outcome"] == "confirmed_pick"
    assert pcs.fill_allowed(order) is True


def test_soft_kill_between_cycles_drops_fill(pcs):
    tid = "Tok:1399811149"
    order = {"token": {"ticker": "TOK", "address": "Tok"}}
    pcs.on_cycle({"tokens": [_judged(tid, "TOK", 0.8)]}, now=T0)
    pcs.on_cycle({"tokens": [{
        "tid": tid, "ticker": "TOK", "stage": "soft", "reason": "shape_is_wrong",
    }]}, now=T0 + 600)
    assert pcs.all_rows()[0]["outcome"] == "dropped_unconfirmed"
    assert pcs.fill_allowed(order) is False


def test_historical_replay_drops_horn_prowler_pepa(pcs):
    out = pcs.replay_historical()
    assert out["HORN"]["outcome"] == "dropped_unconfirmed"
    assert out["PROWLER"]["outcome"] == "dropped_unconfirmed"
    assert out["PEPA"]["outcome"] == "dropped_unconfirmed"
    assert out["CHILL"]["status"] == "pending_confirm"
    assert out["CHILL"]["outcome"] in (None, "")


def test_replay_historical_does_not_write_live_db(pcs, tmp_path, monkeypatch):
    live_n = len(pcs.all_rows())
    out = pcs.replay_historical(live_db=False)
    assert out["HORN"]["outcome"] == "dropped_unconfirmed"
    assert len(pcs.all_rows()) == live_n
    assert all(r.get("ticker") != "HORN" or r.get("first_ts") != 1_700_000_000.0
               for r in pcs.all_rows())


def test_no_protected_file_mentions():
    for name in (
        "thresholds.py", "filter.py",
        "collect.py", "judge.py",
    ):
        text = (ROOT / name).read_text()
        assert "pick_confirm_shadow" not in text, name
        r = subprocess.run(
            ["git", "diff", "--", name],
            cwd=str(ROOT), capture_output=True, text=True, check=True,
        )
        assert r.stdout == "", name
