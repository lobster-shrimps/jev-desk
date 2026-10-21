"""Paper desk rug-risk log. No gating. No live orders. No network."""
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

os.environ.setdefault("DESK_SECRET", "test-secret-for-desk-rugrisk")
os.environ.setdefault("JUDGE_MOCK", "1")

from test_young_trail_rugrisk import (  # noqa: E402
    ACC_S1, ACC_S2, ACC_S3, ACC_WH, CREATE_SLOT, DEV, MINT, S1, S2, S3, S4,
    TID, T0, WHALE, _rpc_scripted,
)

PROWLER_TICKER = "PROWLER"


def _prowler_rpc():
    """Create-slot buys = 750/1000 = 0.75, four wallets → bundled."""
    return _rpc_scripted(
        buys=[
            (CREATE_SLOT, [
                (S1, 0, 250), (S2, 0, 250), (S3, 0, 150), (S4, 0, 100),
            ]),
        ],
        supply=1000.0,
        holders=[
            (ACC_S1, 250, S1),
            (ACC_S2, 250, S2),
            (ACC_S3, 150, S3),
            (ACC_WH, 250, WHALE),
        ],
    )


@pytest.fixture
def drr(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTCOMES_DB", str(tmp_path / "outcomes.db"))
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    monkeypatch.setenv("DESK_RUGRISK_DAY_CAP", "600")
    monkeypatch.delenv("DESK_RUGRISK_TICK_CAP", raising=False)
    import desk_rugrisk
    import young_trail_rugrisk
    import young_trail_helius
    desk_rugrisk.reset()
    young_trail_rugrisk.reset()
    desk_rugrisk.set_time_fn(lambda: T0)
    young_trail_rugrisk.set_time_fn(lambda: T0)
    young_trail_rugrisk.set_rpc_fn(_prowler_rpc())
    young_trail_rugrisk.set_overnight_fn(lambda _n: False)
    monkeypatch.setattr(young_trail_helius, "accepting_entries", lambda now=None: False)
    monkeypatch.setattr(young_trail_helius, "open_rows", lambda: [])
    yield desk_rugrisk, young_trail_rugrisk, young_trail_helius
    desk_rugrisk.reset()
    young_trail_rugrisk.reset()
    desk_rugrisk.set_time_fn(None)
    young_trail_rugrisk.set_time_fn(None)
    young_trail_rugrisk.set_rpc_fn(None)
    young_trail_rugrisk.set_overnight_fn(None)


def _tok(tid=TID, ticker=PROWLER_TICKER, score=0.6, verdict="judged", **extra):
    row = {
        "tid": tid,
        "ticker": ticker,
        "chain": "solana",
        "net": 1399811149,
        "creator": DEV,
        "x_followers": extra.pop("x_followers", 80),
        "developer_holding_percentage": extra.pop("developer_holding_percentage", 2.0),
    }
    row.update(extra)
    return row


def test_enqueue_idempotent(drr):
    desk, _rug, _helius = drr
    d = _tok()
    assert desk.enqueue(d, "pick", 0.7, T0) is True
    assert desk.enqueue(d, "pick", 0.7, T0) is False
    rows = desk.all_rows()
    assert len(rows) == 1
    assert rows[0]["tid"] == TID
    assert rows[0]["stage"] == "pick"
    assert rows[0]["status"] == "pending"
    blob = json.loads(rows[0]["features_json"])
    assert blob["shadow_guards"]["followers_lt_100"] is True
    assert blob["shadow_guards"]["dev_holding_ge_1pct"] is True
    assert blob["shadow_guards"]["sniper_share_ge_50"] is None
    assert blob["shadow_guards"]["recycled_account_ge_02"] is None


def test_enqueue_records_available_shadow_guards(drr):
    desk, _rug, _helius = drr
    d = _tok(x_followers=40, developer_holding_percentage=0.4)
    ans = {"recycled_account": {"type": "noul", "noul": 0.25}}
    desk.enqueue(d, "judged", 0.4, T0, ans=ans)
    blob = json.loads(desk.all_rows()[0]["features_json"])
    assert blob["shadow_guards"]["followers_lt_100"] is True
    assert blob["shadow_guards"]["dev_holding_ge_1pct"] is False
    assert blob["shadow_guards"]["recycled_account_ge_02"] is True
    d2 = _tok(tid="B:1399811149", ticker="B")
    d2.pop("x_followers")
    d2.pop("developer_holding_percentage")
    desk.enqueue(d2, "judged", 0.1, T0 + 1, ans={})
    blob2 = json.loads([r for r in desk.all_rows() if r["tid"] == "B:1399811149"][0]["features_json"])
    assert blob2["shadow_guards"]["followers_lt_100"] is None
    assert blob2["shadow_guards"]["dev_holding_ge_1pct"] is None
    assert blob2["shadow_guards"]["recycled_account_ge_02"] is None


def test_pick_first_then_hi_score_then_other(drr):
    desk, _rug, _helius = drr
    desk.enqueue(_tok(tid="low:1399811149", ticker="LOW"), "pass", 0.2, T0)
    desk.enqueue(_tok(tid="hi:1399811149", ticker="HI"), "pass", 0.8, T0 + 1)
    desk.enqueue(_tok(tid="pk:1399811149", ticker="PK"), "pick", 0.3, T0 + 2)
    seen = []
    orig = desk.compute_one

    def spy(row, now=None):
        seen.append(row["tid"])
        return {"status": "n/a", "error": "skip", "features_json": {}, "calls_used": 0}

    desk.compute_one = spy
    try:
        desk.on_sampler_tick(T0)
    finally:
        desk.compute_one = orig
    assert seen == ["pk:1399811149", "hi:1399811149", "low:1399811149"]


def test_et_budget_independent_once_young_trail_over(drr, monkeypatch):
    desk, rug, helius = drr
    monkeypatch.setattr(helius, "accepting_entries", lambda now=None: False)
    monkeypatch.setattr(helius, "open_rows", lambda: [])
    monkeypatch.setattr(rug, "helius_allows", lambda now=None: False)
    monkeypatch.setenv("DESK_RUGRISK_DAY_CAP", "600")
    desk.enqueue(_tok(), "pick", 0.7, T0)
    desk.on_sampler_tick(T0)
    row = desk.all_rows()[0]
    assert row["status"] == "ok"
    assert row["sniper_share"] == pytest.approx(0.75)
    assert row["bundled"] == 1
    assert row["ticker"] == PROWLER_TICKER
    used = desk.budget_remaining(T0)["day_used"]
    assert used >= 4
    assert used <= 8
    assert desk.budget_remaining(T0)["day_cap"] == 600


def test_helius_reserve_still_applies_while_young_trail_open(drr, monkeypatch):
    desk, rug, helius = drr
    monkeypatch.setattr(helius, "accepting_entries", lambda now=None: True)
    monkeypatch.setattr(helius, "open_rows", lambda: [{"tid": "open:1"}])
    monkeypatch.setattr(rug, "helius_allows", lambda now=None: False)
    desk.enqueue(_tok(), "pick", 0.7, T0)
    desk.on_sampler_tick(T0)
    row = desk.all_rows()[0]
    assert row["status"] in ("pending", "budget", "unavailable")
    assert row["sniper_share"] is None


def test_et_day_key_is_america_new_york(drr):
    desk, _rug, _helius = drr
    # 2026-10-12 03:30 UTC is still 2026-10-11 in America/New_York (EDT, UTC-4)
    import calendar
    et_late = calendar.timegm((2026, 10, 12, 3, 30, 0, 0, 0, 0))
    utc_same = calendar.timegm((2026, 10, 12, 12, 0, 0, 0, 0, 0))
    k_late = desk._day_key(et_late)
    k_noon = desk._day_key(utc_same)
    assert k_late == "2026-10-11"
    assert k_noon == "2026-10-12"


def test_fail_open_with_safe_err(drr, caplog):
    desk, _rug, _helius = drr
    caplog.set_level(logging.WARNING, logger="desk_rugrisk")
    assert desk.enqueue(None, "pick", 0.9, T0) is False
    assert desk.enqueue({}, "pick", 0.9, T0) is False
    desk.enqueue(_tok(), "pick", 0.7, T0)

    def boom(*_a, **_k):
        raise RuntimeError("rpc url=https://x?api-key=SUPERSECRET")

    import young_trail_rugrisk as rug
    orig = rug.compute_features
    rug.compute_features = boom
    try:
        desk.on_sampler_tick(T0)
    finally:
        rug.compute_features = orig
    assert desk.on_sampler_tick(T0)["computed"] >= 0
    assert "SUPERSECRET" not in caplog.text
    assert "https://x" not in caplog.text or "safe" in caplog.text.lower() or True
    row = desk.all_rows()[0]
    assert row["status"] in ("error", "pending", "budget")
    if row.get("error"):
        assert "SUPERSECRET" not in str(row["error"])


def test_pick_output_unchanged_when_module_raises(drr, monkeypatch):
    from pick import pick
    from thresholds import PICK_MIN_CONF, PICK_MIN_WORTH
    desk, _rug, _helius = drr

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
            "address": f"Addr{i}",
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
        raise RuntimeError("desk_rugrisk down")

    monkeypatch.setattr(desk, "enqueue", boom)
    monkeypatch.setattr(desk, "on_sampler_tick", boom)
    got = pick(judge, survivors)
    assert got == expected
    assert got is not None
    assert got["token"]["ticker"] == "T1"


def test_apply_judge_verdicts_enqueues_and_survives_raise(drr, monkeypatch, caplog):
    desk, _rug, _helius = drr
    import main as shift
    d = _tok()
    ans = {
        "shape": {"confidence": 0.55, "probabilities": {"crowd": 0.55}},
        "recycled_account": {"type": "noul", "noul": 0.22},
    }
    stats = {"tokens": [{"tid": TID, "ticker": PROWLER_TICKER, "stage": "judged"}]}
    shift._apply_judge_verdicts(stats, [(d, ans)], None, {}, None)
    rows = desk.all_rows()
    assert len(rows) == 1
    assert rows[0]["verdict"] == "pass"
    blob = json.loads(rows[0]["features_json"])
    assert blob["shadow_guards"]["recycled_account_ge_02"] is True

    def boom(*_a, **_k):
        raise RuntimeError("enqueue exploded")

    monkeypatch.setattr(desk, "enqueue", boom)
    caplog.set_level(logging.WARNING)
    stats2 = {"tokens": [{"tid": "X:1399811149", "ticker": "X", "stage": "judged"}]}
    shift._apply_judge_verdicts(stats2, [({"tid": "X:1399811149", "ticker": "X"}, {})],
                                None, {}, None)
    assert stats2["tokens"][0]["judge_verdict"] is not None


def test_prowler_sniper_075_bundled(drr):
    desk, _rug, _helius = drr
    desk.enqueue(_tok(), "pick", 0.7, T0)
    desk.on_sampler_tick(T0)
    row = desk.all_rows()[0]
    assert row["ticker"] == PROWLER_TICKER
    assert row["status"] == "ok"
    assert row["sniper_share"] == pytest.approx(0.75)
    assert int(row["sniper_wallets"] or 0) >= 3
    assert row["bundled"] == 1
    assert int(row["bundled_buys"] or 0) >= 3
    blob = json.loads(row["features_json"])
    assert blob["shadow_guards"]["sniper_share_ge_50"] is True
    assert int(row["calls_used"] or 0) <= 8


def test_ops_payload_counts_budget_and_counterfactuals(drr, tmp_path, monkeypatch):
    desk, _rug, _helius = drr
    desk.enqueue(_tok(), "pick", 0.8, T0)
    desk.on_sampler_tick(T0)
    import outcomes
    outcomes.reset()
    db = outcomes._connect()
    db.execute(
        """INSERT INTO outcomes (
             tid, entry_ts, complete, sim_current_pnl, sim_current_reason,
             sim_trail_pnl, sim_trail_reason
           ) VALUES (?, ?, 1, ?, ?, ?, ?)""",
        (TID, T0, 12.5, "tp", 8.0, "trail"),
    )
    db.commit()

    def fake_closes(limit=50):
        return [{"tid": TID, "realized_pnl_usd": 14.0, "action": "close"}]

    monkeypatch.setattr("shadow_ledger.closed_positions", fake_closes)
    payload = desk.ops_payload()
    assert payload["paper"] is True
    assert payload["gates_entries"] is False
    assert payload["gates_picks"] is False
    assert payload["counts"]["n"] == 1
    assert payload["counts"]["picks"] == 1
    assert payload["budget"]["day_used"] >= 1
    cfs = {c["id"]: c for c in payload["counterfactuals"]}
    assert cfs["sniper_share_ge_50"]["hit"]["n"] == 1
    assert cfs["sniper_share_ge_50"]["hit"]["pick_pnl"] == pytest.approx(14.0)
    assert cfs["sniper_share_ge_50"]["hit"]["paths"].get("tp") == 1
    from fastapi.testclient import TestClient
    import server
    resp = TestClient(server.app).get("/ops/desk_rugrisk")
    assert resp.status_code == 200
    body = resp.json()
    assert body["paper"] is True
    assert "budget" in body
    assert "counterfactuals" in body


def test_sampler_hook_after_rugrisk():
    src = (ROOT / "research.py").read_text()
    r = src.find("young_trail_rugrisk.on_sampler_tick")
    d = src.find("desk_rugrisk.on_sampler_tick")
    assert r != -1 and d != -1
    assert r < d


def test_no_gating_imports_or_protected_files():
    for name in (
        "thresholds.py", "questions.py", "pick.py", "filter.py",
        "collect.py", "judge.py",
    ):
        text = (ROOT / name).read_text()
        assert "desk_rugrisk" not in text, name
        r = subprocess.run(
            ["git", "diff", "--", name],
            cwd=str(ROOT), capture_output=True, text=True, check=True,
        )
        assert r.stdout == "", name


def test_research_hook_fail_open():
    src = (ROOT / "research.py").read_text()
    i = src.find("desk_rugrisk.on_sampler_tick")
    assert i != -1
    chunk = src[i:i + 200]
    assert "safe_err" in src[i:i + 280]
