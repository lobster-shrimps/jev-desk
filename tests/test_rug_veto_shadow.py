"""Log-only rug veto. Does not gate picks. No protected files."""
from __future__ import annotations

import logging
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DESK_SECRET", "test-secret-for-rug-veto")
os.environ.setdefault("JUDGE_MOCK", "1")

T0 = 1_700_000_000.0


@pytest.fixture
def rvs(tmp_path, monkeypatch):
    monkeypatch.setenv("OUTCOMES_DB", str(tmp_path / "outcomes.db"))
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    import desk_rugrisk
    import rug_veto_shadow
    desk_rugrisk.reset()
    rug_veto_shadow.reset()
    rug_veto_shadow.set_time_fn(lambda: T0)
    desk_rugrisk.set_time_fn(lambda: T0)
    yield rug_veto_shadow
    rug_veto_shadow.reset()
    rug_veto_shadow.set_time_fn(None)
    desk_rugrisk.reset()
    desk_rugrisk.set_time_fn(None)


def test_decide_followers_unknown_does_not_skip(rvs):
    out = rvs.decide(sniper_share=0.2, followers=None)
    assert out["would_skip"] is False
    assert "followers_unknown" in out["reasons"]


def test_decide_followers_lt_100_skips(rvs):
    out = rvs.decide(sniper_share=0.1, followers=40)
    assert out["would_skip"] is True
    assert "followers_lt_100" in out["reasons"]


def test_decide_sniper_ge_50_skips(rvs):
    out = rvs.decide(sniper_share=0.5, followers=500)
    assert out["would_skip"] is True
    assert "sniper_share_ge_50" in out["reasons"]


def test_decide_unavailable_when_timeout(rvs):
    out = rvs.decide(sniper_share=None, followers=500, status="unavailable")
    assert out["would_skip"] is False
    assert "unavailable" in out["reasons"]


def test_record_and_ops_do_not_gate(rvs):
    d = {
        "tid": "A:1399811149", "ticker": "PEPA", "chain": "solana",
        "x_followers": 40, "addr": "A", "net": 1399811149,
        "_rug_veto": {"sniper_share": 0.12, "bundled": 0, "sniper_wallets": 2,
                      "followers": 40, "status": "ok"},
    }
    stats = {"tokens": [{
        "tid": "A:1399811149", "ticker": "PEPA", "chain": "solana",
        "stage": "judged", "judge_score": 0.72, "judge_verdict": "pick",
    }]}
    order = {"token": {"ticker": "PEPA", "address": "A", "network_id": 1399811149}}
    n = rvs.record(stats, [(d, {})], order, now=T0)
    assert n == 1
    row = rvs.all_rows()[0]
    assert row["would_skip"] is True
    assert "followers_lt_100" in row["reasons"]
    assert row["became_pick"] is True
    payload = rvs.ops_payload()
    assert payload["gates_picks"] is False
    assert payload["paper"] is True
    from fastapi.testclient import TestClient
    import server
    resp = TestClient(server.app).get("/ops/rug_veto")
    assert resp.status_code == 200
    assert resp.json()["gates_picks"] is False


def test_prepare_timeout_records_unavailable(rvs, monkeypatch):
    import desk_rugrisk

    def boom(*_a, **_k):
        raise AssertionError("must not RPC when cycle budget is gone")

    monkeypatch.setattr(desk_rugrisk, "compute_one", boom)
    monkeypatch.setattr(desk_rugrisk, "enqueue", boom)
    d = {"tid": "B:1399811149", "ticker": "X", "chain": "solana",
         "addr": "B", "net": 1399811149, "creator": "Dev"}
    rvs.prepare([(d, {})], deadline=T0 - 1, now=T0)
    assert d["_rug_veto"]["status"] == "unavailable"


def test_historical_replay_pepa_prowler_skip(rvs):
    payload = rvs.replay()
    by = {r["ticker"]: r for r in payload["historical"]}
    assert by["PEPA"]["would_skip"] is True
    assert "followers_lt_100" in by["PEPA"]["reasons"]
    assert by["PROWLER"]["would_skip"] is True
    assert "sniper_share_ge_50" in by["PROWLER"]["reasons"]
    assert by["HORN"]["would_skip"] is False
    assert "followers_unknown" in by["HORN"]["reasons"]
    assert by["CHILL"]["would_skip"] is False
    assert "followers_unknown" in by["CHILL"]["reasons"]
    assert by["hellop"]["would_skip"] is False
    assert "followers_unknown" in by["hellop"]["reasons"]


def test_pick_unchanged_when_module_raises(rvs, monkeypatch):
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
        raise RuntimeError("rug_veto down")

    monkeypatch.setattr(rvs, "prepare", boom)
    monkeypatch.setattr(rvs, "record", boom)
    got = pick(judge, survivors)
    assert got == expected


def test_no_protected_file_mentions():
    for name in (
        "thresholds.py", "filter.py",
        "collect.py", "judge.py",
    ):
        text = (ROOT / name).read_text()
        assert "rug_veto_shadow" not in text, name
        r = subprocess.run(
            ["git", "diff", "--", name],
            cwd=str(ROOT), capture_output=True, text=True, check=True,
        )
        assert r.stdout == "", name


def test_replay_script_runs(rvs, capsys):
    rvs.main()
    out = capsys.readouterr().out
    assert "PEPA" in out
    assert "PROWLER" in out
    assert "would_skip=True" in out
