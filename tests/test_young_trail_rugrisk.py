"""Paper rug-risk features and skip-rule counterfactuals. No gating. No live orders."""
import json
import logging
import os
import pathlib
import sqlite3
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DESK_SECRET", "test-secret-for-yt-rugrisk")
os.environ.setdefault("JUDGE_MOCK", "1")

ENTRY = 1.0
T0 = 1_700_000_000.0
MINT = "MintRugRisk11111111111111111111111111"
TID = f"{MINT}:1399811149"
DEV = "DevRugRisk111111111111111111111111111"
S1 = "SniperOne11111111111111111111111111111"
S2 = "SniperTwo22222222222222222222222222222"
S3 = "SniperThree333333333333333333333333333"
S4 = "SniperFour4444444444444444444444444444"
WHALE = "WhaleHolder11111111111111111111111111"
CURVE_OWNER = "CurveOwner111111111111111111111111111"
PRIOR = "PriorMintRug1111111111111111111111111"
ACC_S1 = "AccS111111111111111111111111111111111"
ACC_S2 = "AccS222222222222222222222222222222222"
ACC_S3 = "AccS333333333333333333333333333333333"
ACC_WH = "AccWhale11111111111111111111111111111"
PUMP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"
CURVE = "CurveRugRisk1111111111111111111111111"
CREATE_SLOT = 100


def _tok(tid=TID, ticker="RRK", age=22.0, price=ENTRY, chain="solana",
         net=1399811149, **extra):
    row = {
        "tid": tid,
        "ticker": ticker,
        "chain": chain,
        "net": net,
        "age_minutes": age,
        "price_usd": price,
    }
    row.update(extra)
    return row


def _ix(kind, mint):
    return {"parsed": {"type": kind, "info": {"mint": mint}}}


def _tx(slot, mint, deltas, ixs=None, signer=None):
    """deltas: (owner, pre, post)."""
    pre, post = [], []
    for owner, before, after in deltas:
        pre.append({
            "mint": mint, "owner": owner,
            "uiTokenAmount": {"uiAmount": before, "amount": str(int(before))},
        })
        post.append({
            "mint": mint, "owner": owner,
            "uiTokenAmount": {"uiAmount": after, "amount": str(int(after))},
        })
    keys = []
    if signer:
        keys.append({"pubkey": signer, "signer": True, "writable": True})
    return {
        "slot": slot,
        "transaction": {"message": {"accountKeys": keys, "instructions": ixs or []}},
        "meta": {
            "preTokenBalances": pre,
            "postTokenBalances": post,
            "innerInstructions": [],
        },
    }


def _parsed_token_acc(owner, program=None):
    return {
        "owner": program or "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "data": {"parsed": {"info": {"owner": owner}}},
        "lamports": 2_039_280,
    }


def _rpc_scripted(buys=None, supply=1000.0, holders=None, wallet_n=None,
                  creator_txs=None, mint_history=None, extra_txs=None,
                  signer=None, tx_list=None, sig_by_addr=None, gtfa_by_addr=None):
    """Scripted Helius RPC. Counts calls. Never hits the network.

    `tx_list` is oldest-first mint txs: {signature, slot, deltas, ixs, signer}.
    `buys is None` uses the default bundled create-slot buys; pass [] for none.
    Unknown addresses return no signatures (do not invent dummy histories).
    `gtfa_by_addr is None` means getTransactionsForAddress is unsupported.
    """
    holders = holders or [
        (ACC_S1, 200, S1),
        (ACC_S2, 200, S2),
        (ACC_S3, 100, S3),
        (ACC_WH, 400, WHALE),
    ]
    wallet_n = wallet_n or {S1: 1, S2: 1, S3: 2, WHALE: 20, DEV: 8}
    mint_sigs = []
    txs = {}
    if tx_list is not None:
        for spec in tx_list:
            sig = spec["signature"]
            slot = spec["slot"]
            mint_sigs.append({
                "signature": sig, "slot": slot, "err": spec.get("err"),
            })
            txs[sig] = _tx(
                slot, spec.get("mint") or MINT, spec.get("deltas") or [],
                ixs=spec.get("ixs"), signer=spec.get("signer"),
            )
        mint_sigs = list(reversed(mint_sigs))
    else:
        if buys is None:
            buys = [
                (CREATE_SLOT, [(S1, 0, 200), (S2, 0, 200), (S3, 0, 100), (S4, 0, 50)]),
                (CREATE_SLOT + 80, [(WHALE, 0, 50)]),
            ]
        for i, (slot, deltas) in enumerate(buys):
            sig = f"mintsig{i:02d}"
            mint_sigs.append({"signature": sig, "slot": slot, "err": None})
            txs[sig] = _tx(slot, MINT, deltas, signer=signer)
        mint_sigs = list(reversed(mint_sigs))
    if mint_history is not None:
        mint_sigs = list(mint_history)
    if extra_txs:
        txs.update(extra_txs)
    sig_by_addr = dict(sig_by_addr or {})

    creator_txs = creator_txs or [
        ("devcreate", CREATE_SLOT - 50, PRIOR, "initializeMint",
         [(DEV, 0, 1)]),
        ("devrug", CREATE_SLOT - 10, PRIOR, "removeLiquidity",
         [(DEV, 1, 0)]),
    ]
    creator_sigs = []
    for sig, slot, mint, kind, deltas in creator_txs:
        creator_sigs.append({"signature": sig, "slot": slot, "err": None})
        txs[sig] = _tx(slot, mint, deltas, ixs=[_ix(kind, mint)])

    calls = []

    def rpc(method, params, timeout):
        calls.append((method, params))
        if method == "getTokenSupply":
            return {"value": {"uiAmount": supply, "amount": str(int(supply)),
                              "decimals": 0}}
        if method == "getTokenLargestAccounts":
            return {"value": [
                {"address": acc, "amount": str(amt), "uiAmount": amt}
                for acc, amt, _owner in holders
            ]}
        if method == "getMultipleAccounts":
            addrs = params[0]
            by = {acc: _parsed_token_acc(owner) for acc, _amt, owner in holders}
            return {"value": [by.get(a) for a in addrs]}
        if method == "getSignaturesForAddress":
            addr = params[0]
            opts = params[1] if len(params) > 1 and isinstance(params[1], dict) else {}
            limit = int(opts.get("limit") or 20)
            before = opts.get("before")
            if addr == MINT:
                start = 0
                if before:
                    ids = [s["signature"] for s in mint_sigs]
                    try:
                        start = ids.index(before) + 1
                    except ValueError:
                        return []
                return mint_sigs[start:start + limit]
            if addr == DEV:
                return creator_sigs[:limit]
            if addr in sig_by_addr:
                rows = sig_by_addr[addr]
                start = 0
                if before:
                    ids = [s["signature"] for s in rows]
                    try:
                        start = ids.index(before) + 1
                    except ValueError:
                        return []
                return rows[start:start + limit]
            n = wallet_n.get(addr)
            if n is None:
                return []
            n = int(n)
            return [
                {"signature": f"{addr[:6]}s{i}", "slot": CREATE_SLOT + i, "err": None}
                for i in range(n)
            ][:limit]
        if method == "getTransactionsForAddress":
            if gtfa_by_addr is None:
                raise RuntimeError("Method not found")
            addr = params[0]
            return {"data": list(gtfa_by_addr.get(addr) or [])}
        if method == "getTransaction":
            sig = params[0]
            if sig not in txs:
                raise AssertionError(f"unexpected tx {sig}")
            return txs[sig]
        raise AssertionError(f"unexpected rpc {method}")

    rpc.calls = calls
    return rpc


@pytest.fixture
def ytr(monkeypatch):
    import young_trail
    import young_trail_helius
    import young_trail_rugrisk
    monkeypatch.delenv("YOUNG_TRAIL_UNTIL", raising=False)
    monkeypatch.delenv("YOUNG_TRAIL_ENABLED", raising=False)
    monkeypatch.delenv("YOUNG_TRAIL_RUGRISK_UNTIL", raising=False)
    monkeypatch.delenv("YOUNG_TRAIL_RUGRISK_ENABLED", raising=False)
    monkeypatch.setenv("YOUNG_TRAIL_RUGRISK_CYCLE_CAP", "40")
    monkeypatch.setenv("YOUNG_TRAIL_RUGRISK_DAY_CAP", "500")
    monkeypatch.setenv("YOUNG_TRAIL_RUGRISK_OVERNIGHT_CYCLE_CAP", "40")
    monkeypatch.setenv("YOUNG_TRAIL_RUGRISK_HELIUS_DAY_RESERVE", "2")
    young_trail.reset()
    young_trail.set_time_fn(lambda: T0)
    young_trail.set_sleep_fn(lambda _d: None)
    young_trail.set_get_fn(lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("young_trail tests must not use the network")
    ))
    young_trail_helius.reset()
    young_trail_helius.set_time_fn(lambda: T0)
    young_trail_helius.set_rpc_fn(lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("rugrisk tests must not use helius rpc")
    ))
    young_trail_rugrisk.reset()
    young_trail_rugrisk.set_time_fn(lambda: T0)
    young_trail_rugrisk.set_rpc_fn(_rpc_scripted())
    young_trail_rugrisk.set_overnight_fn(lambda _now: False)
    yield young_trail, young_trail_helius, young_trail_rugrisk
    young_trail.reset()
    young_trail_helius.reset()
    young_trail_rugrisk.reset()
    young_trail_rugrisk.set_rpc_fn(None)
    young_trail_rugrisk.set_overnight_fn(None)


def _enter(yt, helius, now=T0, **extra):
    tok = _tok(**extra)
    yt.consider_entries([tok], now=now)
    if extra.get("creator"):
        helius.note_creator(tok["tid"], extra["creator"], now=now)
    return tok


def test_pure_sniper_share_and_bundled():
    import young_trail_rugrisk as r
    buys = [
        {"owner": S1, "delta": 300, "slot": 10},
        {"owner": S2, "delta": 200, "slot": 10},
        {"owner": S3, "delta": 100, "slot": 10},
        {"owner": WHALE, "delta": 400, "slot": 80},
    ]
    sn = r.sniper_share_from_buys(buys, 1000.0, 10, 32)
    assert sn["sniper_share"] == pytest.approx(0.60)
    assert sn["sniper_wallets"] == 3
    assert sn["sniper_denominator"] == pytest.approx(1000.0)
    assert sn["sniper_slots"] == 32
    bun = r.bundled_from_buys(buys, 10, {S1, S2})
    assert bun["bundled"] is True
    assert bun["bundled_buys"] == 3
    two = r.bundled_from_buys(
        [{"owner": S1, "delta": 10, "slot": 10},
         {"owner": S2, "delta": 10, "slot": 10}],
        10, {S1, S2},
    )
    assert two["bundled"] is False
    one = r.bundled_from_buys(
        [{"owner": S1, "delta": 10, "slot": 10}], 10, {S1},
    )
    assert one["bundled"] is False
    excluded = r.sniper_share_from_buys(
        buys, 1000.0, 10, 32, exclude={S1},
    )
    assert excluded["sniper_wallets"] == 2
    assert excluded["sniper_share"] == pytest.approx(0.30)


def test_pure_fresh_holder_share():
    import young_trail_rugrisk as r
    holders = [
        {"owner": S1, "amount": 30, "is_pool": False},
        {"owner": WHALE, "amount": 70, "is_pool": False},
        {"owner": PUMP, "amount": 500, "is_pool": True},
    ]
    out = r.fresh_holder_share(holders, {S1})
    assert out["fresh_holder_share"] == pytest.approx(0.30)
    assert out["top_holder_n"] == 2


def test_skip_flags_unknown_on_na_and_error():
    import young_trail_rugrisk as r
    na = r.skip_flags({"status": "n/a", "sniper_share": 0.9, "bundled": True,
                       "dev_prior_rugs": 3})
    assert na == {"skip_snipers": None, "skip_bundled": None, "skip_dev_rugs": None}
    err = r.skip_flags({"status": "error"})
    assert err["skip_snipers"] is None
    ok = r.skip_flags({
        "status": "ok", "sniper_share": 0.31, "sniper_wallets": 4,
        "bundled": False, "dev_prior_rugs": 0,
    })
    assert ok["skip_snipers"] is True
    assert ok["skip_bundled"] is False
    assert ok["skip_dev_rugs"] is False
    few = r.skip_flags({
        "status": "ok", "sniper_share": 0.50, "sniper_wallets": 2,
        "bundled": False, "dev_prior_rugs": 0,
    })
    assert few["skip_snipers"] is False


def test_counterfactual_math_skipped_vs_kept():
    import young_trail_rugrisk as r
    rows = [
        {"tid": "A:1", "status": "ok", "sniper_share": 0.5, "sniper_wallets": 4,
         "bundled": False, "dev_prior_rugs": 0},
        {"tid": "B:1", "status": "ok", "sniper_share": 0.1, "sniper_wallets": 4,
         "bundled": False, "dev_prior_rugs": 0},
        {"tid": "C:1", "status": "n/a"},
        {"tid": "D:1", "status": "ok", "sniper_share": 0.4, "sniper_wallets": 5,
         "bundled": True, "dev_prior_rugs": 2},
    ]
    pnls = {
        "young_trail": {"A:1": -20.0, "B:1": 15.0, "D:1": -10.0},
        "fast_baseline": {"A:1": -8.0, "B:1": 12.0, "D:1": -4.0},
        "fast_tp50": {"A:1": -5.0, "B:1": 30.0, "D:1": 2.0},
    }
    cf = r.counterfactual_for_rule("snipers_gt_30", rows, pnls)
    assert cf["skipped"]["n"] == 2
    assert cf["kept"]["n"] == 1
    assert cf["unknown_n"] == 1
    assert cf["skipped"]["young_trail_pnl"] == pytest.approx(-30.0)
    assert cf["kept"]["young_trail_pnl"] == pytest.approx(15.0)
    assert cf["skipped"]["fast_baseline_pnl"] == pytest.approx(-12.0)
    assert cf["kept"]["fast_tp50_pnl"] == pytest.approx(30.0)
    # unmarked / missing pnl excluded
    pnls2 = {"young_trail": {"A:1": -20.0}, "fast_baseline": {}, "fast_tp50": {}}
    cf2 = r.counterfactual_for_rule("snipers_gt_30", rows, pnls2)
    assert cf2["skipped"]["n_with_pnl"]["young_trail"] == 1
    assert cf2["skipped"]["young_trail_pnl"] == pytest.approx(-20.0)


def test_compute_features_mocked_rpc(ytr):
    _yt, helius, rug = ytr
    helius.note_creator(TID, DEV, now=T0)
    rug.begin_cycle(T0)
    feat = rug.compute_features(MINT, creator=DEV, now=T0)
    assert feat["status"] == "ok"
    # 200+200+100+50 = 550 of 1000 in the create slot (denominator = supply)
    assert feat["sniper_share"] == pytest.approx(0.55)
    assert feat["sniper_wallets"] == 4
    assert feat["sniper_denominator"] == pytest.approx(1000.0)
    assert feat["sniper_min_wallets"] == rug.SNIPER_MIN_WALLETS
    assert feat["bundled"] is True
    assert feat["skip_snipers"] is True
    assert feat["skip_bundled"] is True
    assert feat["dev_prior_launches"] == 1
    assert feat["dev_prior_rugs"] == 1
    assert feat["skip_dev_rugs"] is True
    used = (feat.get("features_json") or {}).get("calls_used", 0)
    assert used >= 4
    assert used <= rug.TARGET_ENTRY_CALLS


def test_non_solana_is_na_no_rpc(ytr):
    yt, _helius, rug = ytr
    calls_before = list(rug._rpc_fn.calls) if hasattr(rug._rpc_fn, "calls") else []
    yt.consider_entries([
        _tok(),
        _tok(tid="0xBsc:56", ticker="BSC", chain="bsc", net=56),
    ], now=T0)
    n = rug.sync_entries(now=T0)
    assert n == 2
    rows = {r["tid"]: r for r in rug.all_rows()}
    assert rows["0xBsc:56"]["status"] == "n/a"
    assert rows[TID]["status"] == "pending"
    # n/a must not spend RPC
    rug.begin_cycle(T0)
    rug.compute_pending(now=T0, limit=8, source="live")
    bsc = {r["tid"]: r for r in rug.all_rows()}["0xBsc:56"]
    assert bsc["status"] == "n/a"
    assert bsc["sniper_share"] is None
    assert bsc["skip_snipers"] is None
    # mint RPC only for the solana pending row
    methods = [c[0] for c in rug._rpc_fn.calls[len(calls_before):]]
    assert "getTokenSupply" in methods


def test_sync_includes_pre_deploy_for_backfill(ytr):
    yt, _helius, rug = ytr
    yt.consider_entries([_tok()], now=T0)
    rug.deploy_ts(now=T0 + 10)
    assert rug.sync_entries(now=T0 + 20) == 1
    assert rug.all_rows()[0]["tid"] == TID


def test_budget_caps_and_does_not_starve_helius(ytr, monkeypatch):
    yt, helius, rug = ytr
    monkeypatch.setenv("YOUNG_TRAIL_RUGRISK_CYCLE_CAP", "3")
    monkeypatch.setenv("YOUNG_TRAIL_RUGRISK_DAY_CAP", "5")
    monkeypatch.setenv("YOUNG_TRAIL_HELIUS_CYCLE_CAP", "4")
    monkeypatch.setenv("YOUNG_TRAIL_HELIUS_DAY_CAP", "6")
    monkeypatch.setenv("YOUNG_TRAIL_RUGRISK_HELIUS_DAY_RESERVE", "3")
    _enter(yt, helius, creator=DEV)
    rug.sync_entries(now=T0)
    helius.reset()
    helius.set_time_fn(lambda: T0)
    helius.begin_cycle(T0)
    # Burn helius day budget down to the reserve line.
    while helius.budget_remaining(T0)["day_left"] >= 3:
        assert helius._charge(1, T0)
    assert helius.budget_remaining(T0)["day_left"] == 2
    rug.reset()
    rug.set_time_fn(lambda: T0)
    rug.set_rpc_fn(_rpc_scripted())
    rug.set_overnight_fn(lambda _n: False)
    rug.begin_cycle(T0)
    assert rug.helius_allows(T0) is False
    calls_before = len(rug._rpc_fn.calls)
    n = rug.compute_pending(now=T0, limit=8)
    assert n == 0
    assert len(rug._rpc_fn.calls) == calls_before

    # Own cycle cap: restore helius day, but rugrisk cycle=3 cannot start (needs 4).
    helius.reset()
    helius.set_time_fn(lambda: T0)
    helius.begin_cycle(T0)
    rug.reset()
    rug.set_time_fn(lambda: T0)
    rug.set_rpc_fn(_rpc_scripted())
    rug.set_overnight_fn(lambda _n: False)
    rug.begin_cycle(T0)
    yt.consider_entries([_tok()], now=T0)
    rug.sync_entries(now=T0)
    n = rug.compute_pending(now=T0, limit=8)
    assert n == 0
    assert rug.budget_remaining(T0)["cycle_left"] == 3

    # Own day cap
    monkeypatch.setenv("YOUNG_TRAIL_RUGRISK_CYCLE_CAP", "40")
    monkeypatch.setenv("YOUNG_TRAIL_RUGRISK_DAY_CAP", "3")
    rug.reset()
    rug.set_time_fn(lambda: T0)
    rug.set_rpc_fn(_rpc_scripted())
    rug.set_overnight_fn(lambda _n: False)
    rug.begin_cycle(T0)
    yt.consider_entries([_tok()], now=T0)
    rug.sync_entries(now=T0)
    n = rug.compute_pending(now=T0, limit=8)
    assert n == 0


def test_does_not_call_helius_begin_cycle(ytr, monkeypatch):
    yt, helius, rug = ytr
    _enter(yt, helius, creator=DEV)
    called = []
    orig = helius.begin_cycle

    def spy(now=None):
        called.append(now)
        return orig(now)

    monkeypatch.setattr(helius, "begin_cycle", spy)
    rug.begin_cycle(T0)
    rug.sync_entries(now=T0)
    rug.compute_pending(now=T0, limit=1)
    assert called == []


def test_sampler_order_helius_then_rugrisk():
    src = (ROOT / "research.py").read_text()
    h = src.find("young_trail_helius.on_sampler_tick")
    r = src.find("young_trail_rugrisk.on_sampler_tick")
    assert h != -1 and r != -1
    assert h < r


def test_overnight_backfill_uses_higher_cap(ytr, monkeypatch):
    yt, helius, rug = ytr
    monkeypatch.setenv("YOUNG_TRAIL_RUGRISK_CYCLE_CAP", "2")
    monkeypatch.setenv("YOUNG_TRAIL_RUGRISK_OVERNIGHT_CYCLE_CAP", "40")
    _enter(yt, helius, creator=DEV)
    rug.set_overnight_fn(lambda _n: False)
    rug.begin_cycle(T0)
    rug.sync_entries(now=T0)
    assert rug.cycle_cap(T0) == 2
    n_day = rug.compute_pending(now=T0, limit=8)
    assert n_day == 0
    rug.set_overnight_fn(lambda _n: True)
    rug.begin_cycle(T0)
    assert rug.cycle_cap(T0) == 40
    n_night = rug.backfill_pending(now=T0, limit=4, force=True)
    assert n_night >= 1
    row = rug.all_rows()[0]
    assert row["status"] == "ok"
    assert row["source"] == "backfill"


def test_skip_rules_do_not_gate_entries(ytr):
    yt, helius, rug = ytr
    _enter(yt, helius, creator=DEV)
    rug.on_sampler_tick({}, now=T0)
    row = rug.all_rows()[0]
    assert row["skip_snipers"] == 1
    assert row["skip_bundled"] == 1
    assert row["skip_dev_rugs"] == 1
    # young_trail still holds the entry; rugrisk never closes or deletes it
    opens = yt.open_rows()
    assert len(opens) == 1
    assert opens[0]["tid"] == TID
    assert opens[0]["exit_ts"] is None
    payload = rug.ops_payload()
    assert payload["gates_entries"] is False
    assert payload["live_orders"] is False
    assert payload["paper"] is True


def test_no_gating_imports_or_protected_files():
    for name in (
        "thresholds.py", "questions.py", "pick.py", "filter.py",
        "collect.py", "judge.py", "young_trail.py", "young_trail_fast.py",
        "young_trail_helius.py", "young_trail_variants.py",
    ):
        text = (ROOT / name).read_text()
        assert "young_trail_rugrisk" not in text, name
    for name in (
        "thresholds.py", "questions.py", "pick.py", "filter.py",
        "collect.py", "judge.py", "young_trail_variants.py",
    ):
        r = subprocess.run(
            ["git", "diff", "--", name],
            cwd=str(ROOT), capture_output=True, text=True, check=True,
        )
        assert r.stdout == "", name


def test_rpc_url_never_logged(ytr, monkeypatch, caplog):
    yt, helius, rug = ytr
    secret = "https://mainnet.helius-rpc.com/?api-key=SUPERSECRET"
    monkeypatch.setenv("SOLANA_RPC_URL", secret)
    caplog.set_level(logging.WARNING)

    def boom(method, params, timeout):
        raise RuntimeError(f"Max retries exceeded with url: {secret}")

    rug.set_rpc_fn(boom)
    _enter(yt, helius, creator=DEV)
    rug.on_sampler_tick({}, now=T0)
    blob = caplog.text + json.dumps(rug.ops_payload())
    assert "SUPERSECRET" not in blob
    assert secret not in blob
    src = (ROOT / "young_trail_rugrisk.py").read_text()
    for line in src.splitlines():
        if "rpc_url" in line and line.strip().startswith("log."):
            raise AssertionError(f"logs rpc_url: {line}")
    assert "print(" not in src.split("def main")[0]


def test_rpc_failure_via_requests_safe_err(ytr, monkeypatch, caplog):
    yt, helius, rug = ytr
    secret = "https://mainnet.helius-rpc.com/?api-key=LEAKME99"
    monkeypatch.setenv("SOLANA_RPC_URL", secret)
    caplog.set_level(logging.WARNING)
    rug.set_rpc_fn(None)
    _enter(yt, helius, creator=DEV)
    rug.sync_entries(now=T0)

    def fake_post(url, json=None, timeout=None):
        raise ConnectionError(f"Failed to resolve {url}")

    from unittest.mock import patch
    rug.begin_cycle(T0)
    with patch("requests.post", side_effect=fake_post):
        rug.compute_pending(now=T0, limit=1)
    assert "LEAKME99" not in caplog.text
    assert secret not in caplog.text
    assert "young_trail_rugrisk" in caplog.text


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
    import young_trail_rugrisk
    young_trail_rugrisk.reset()
    conn = young_trail_rugrisk._connect()
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "young_trail_rugrisk" in names
    assert "young_trail_rugrisk_meta" in names
    assert "young_trail" in names
    keep = conn.execute(
        "SELECT ticker, entry_price FROM young_trail WHERE tid='keep:1'"
    ).fetchone()
    assert keep["ticker"] == "KEEP"
    assert keep["entry_price"] == pytest.approx(2.5)
    young_trail_rugrisk.init_schema(conn)
    young_trail_rugrisk.init_schema(conn)
    young_trail_rugrisk.reset()


def test_schema_alter_skips_existing_column(ytr, caplog):
    caplog.set_level(logging.WARNING)
    _yt, _helius, rug = ytr
    db = rug._connect()
    rug._add_column(db, "young_trail_rugrisk", "dev_initial_share", "REAL")
    rug._add_column(db, "young_trail_rugrisk", "dev_initial_share", "REAL")
    rug._add_column(db, "young_trail_rugrisk", "sniper_denominator", "REAL")
    rug._add_column(db, "young_trail_rugrisk", "sniper_denominator", "REAL")
    rug._add_column(db, "young_trail_rugrisk", "sniper_min_wallets", "INTEGER")
    rug._add_column(db, "young_trail_rugrisk", "sniper_min_wallets", "INTEGER")
    rug.init_schema()
    cols = rug._table_columns(db, "young_trail_rugrisk")
    assert "sniper_denominator" in cols
    assert "sniper_min_wallets" in cols
    assert "duplicate column" not in caplog.text.lower()


def test_until_matches_young_trail_oct20(ytr):
    yt, _helius, rug = ytr
    assert rug.until_ts(T0) == pytest.approx(yt.until_ts(T0))
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(rug.until_ts(T0)))
    assert iso == "2026-10-21T03:59:59Z"


def test_ops_payload_and_endpoint(ytr):
    yt, helius, rug = ytr
    _enter(yt, helius, creator=DEV)
    rug.on_sampler_tick({}, now=T0)
    # Close the young_trail row so counterfactual has P&L
    db = yt._connect()
    db.execute(
        """UPDATE young_trail SET exit_ts = ?, exit_price = ?, exit_reason = 'stop',
           pnl_usd = ? WHERE tid = ?""",
        (T0 + 60, 0.5, -25.0, TID),
    )
    db.commit()
    payload = rug.ops_payload()
    assert payload["paper"] is True
    assert payload["gates_entries"] is False
    assert payload["live_orders"] is False
    assert payload["summary"]["ok"] == 1
    assert payload["counterfactuals"]
    snipers = next(c for c in payload["counterfactuals"] if c["id"] == "snipers_gt_30")
    assert snipers["skipped"]["n"] == 1
    assert snipers["skipped"]["young_trail_pnl"] == pytest.approx(-25.0)
    blob = json.dumps(payload)
    assert "SUPERSECRET" not in blob
    from fastapi.testclient import TestClient
    import server
    resp = TestClient(server.app).get("/ops/young_trail_rugrisk")
    assert resp.status_code == 200
    data = resp.json()
    assert data["paper"] is True
    assert data["gates_entries"] is False
    assert "budget" in data


def test_ops_html_has_rugrisk_section():
    html = (ROOT / "ops.html").read_text()
    assert 'id="yt-rugrisk"' in html
    assert "/ops/young_trail_rugrisk" in html
    assert "paper skip rules" in html
    assert "https://" not in html


def test_sampler_drives_rugrisk_without_young_trail_exits(ytr):
    yt, helius, rug = ytr
    import research
    _enter(yt, helius, creator=DEV)

    def get(url, timeout=None, headers=None, **kw):
        return type("R", (), {
            "status_code": 200,
            "json": lambda self=None: {"pairs": [{
                "chainId": "solana",
                "priceUsd": "0.70",
                "liquidity": {"usd": 9000},
                "baseToken": {"address": MINT, "symbol": "RRK"},
            }]},
        })()

    research.set_get_fn(get)
    research.set_time_fn(lambda: T0)
    from unittest.mock import patch
    called = []

    def boom(*a, **k):
        called.append(1)
        raise AssertionError("young_trail exits must not run")

    with patch("young_trail.mark_open", side_effect=boom):
        with patch("young_trail.enter", side_effect=boom):
            out = research.tick_sampler(now=T0)
    assert called == []
    assert out["marks"] >= 1
    rows = rug.all_rows()
    assert rows and rows[0]["tid"] == TID
    assert yt.open_rows()[0]["exit_ts"] is None


def test_unmarked_pnl_excluded_from_counterfactual(ytr):
    yt, helius, rug = ytr
    _enter(yt, helius, creator=DEV)
    rug.on_sampler_tick({}, now=T0)
    db = yt._connect()
    db.execute(
        """UPDATE young_trail SET exit_ts = ?, exit_price = NULL, exit_reason = 'unmarked',
           pnl_usd = NULL WHERE tid = ?""",
        (T0 + 60, TID),
    )
    db.commit()
    cfs = rug.counterfactuals()
    snipers = next(c for c in cfs if c["id"] == "snipers_gt_30")
    assert snipers["skipped"]["n"] == 1
    assert snipers["skipped"]["n_with_pnl"]["young_trail"] == 0
    assert snipers["skipped"]["young_trail_pnl"] == pytest.approx(0.0)


def test_truncated_history_unavailable_no_skip_flags(ytr, monkeypatch):
    _yt, helius, rug = ytr
    monkeypatch.setattr(rug, "SIG_PAGE_SIZE", 10)
    history = [
        {"signature": f"rec{i:03d}", "slot": 900 - i, "err": None}
        for i in range(30)
    ]
    rpc = _rpc_scripted(mint_history=history, creator_txs=[])
    rug.set_rpc_fn(rpc)
    helius.note_creator(TID, DEV, now=T0)
    rug.begin_cycle(T0)
    feat = rug.compute_features(MINT, creator=None, now=T0)
    assert feat["status"] == "unavailable"
    assert feat.get("reason") == "history_truncated" or feat.get("error") == "history_truncated"
    assert feat["history_truncated"] is True
    assert feat["create_slot"] is None
    assert feat["sniper_share"] is None
    assert feat["bundled"] is None
    assert feat["skip_snipers"] is None
    assert feat["skip_bundled"] is None
    assert feat["skip_dev_rugs"] is None
    assert rug.would_skip(feat, "snipers_gt_30") is None
    assert rug.would_skip(feat, "bundled") is None
    mint_sig_calls = [
        c for c in rpc.calls
        if c[0] == "getSignaturesForAddress" and c[1][0] == MINT
    ]
    assert len(mint_sig_calls) == 1
    assert [c[1][0] for c in rpc.calls if c[0] == "getTransaction"] == []


def test_creator_create_slot_buy_excluded(ytr):
    _yt, _helius, rug = ytr
    rpc = _rpc_scripted(
        buys=[(CREATE_SLOT, [
            (DEV, 0, 400), (S1, 0, 200), (S2, 0, 200), (S3, 0, 100),
        ])],
        supply=1000.0,
        signer=DEV,
    )
    rug.set_rpc_fn(rpc)
    rug.begin_cycle(T0)
    feat = rug.compute_features(MINT, creator=DEV, now=T0)
    assert feat["status"] == "ok"
    assert feat["sniper_share"] == pytest.approx(0.50)
    assert feat["sniper_wallets"] == 3
    assert feat["dev_initial_share"] == pytest.approx(0.40)
    assert feat["bundled"] is True
    creators = set((feat.get("features_json") or {}).get("creators") or [])
    assert DEV in creators
    # Creator is not a fresh-wallet candidate (only queried for prior-rug history).
    fresh_addrs = [
        c[1][0] for c in rpc.calls
        if c[0] == "getSignaturesForAddress" and c[1][0] not in (MINT, DEV)
    ]
    assert DEV not in fresh_addrs


def test_two_non_dev_create_slot_buyers_not_bundled(ytr):
    _yt, _helius, rug = ytr
    rpc = _rpc_scripted(
        buys=[(CREATE_SLOT, [(DEV, 0, 200), (S1, 0, 100), (S2, 0, 100)])],
        signer=DEV,
        wallet_n={S1: 1, S2: 1, DEV: 8, WHALE: 20, S3: 2},
    )
    rug.set_rpc_fn(rpc)
    rug.begin_cycle(T0)
    feat = rug.compute_features(MINT, creator=DEV, now=T0)
    assert feat["status"] == "ok"
    assert feat["bundled"] is False
    assert feat["skip_bundled"] is False
    assert feat["bundled_buys"] == 2
    assert feat["sniper_wallets"] == 2
    assert feat["dev_initial_share"] == pytest.approx(0.20)


def _feat_json(row):
    raw = row.get("features_json") if isinstance(row, dict) else None
    if raw is None:
        return {}
    return json.loads(raw) if isinstance(raw, str) else dict(raw)


def test_create_ix_only_creators_same_slot_buys_are_bundled(ytr):
    """1 create tx + 3 other wallets' buy txs in the same slot.

    Bundled buyers must not be treated as creators just because they signed
    a create-slot transaction.
    """
    _yt, helius, rug = ytr
    rpc = _rpc_scripted(
        tx_list=[
            {
                "signature": "asigcreate", "slot": CREATE_SLOT,
                "deltas": [(DEV, 0, 50)],
                "ixs": [_ix("create", MINT)], "signer": DEV,
            },
            {
                "signature": "bsigbuy1", "slot": CREATE_SLOT,
                "deltas": [(S1, 0, 100)], "signer": S1,
            },
            {
                "signature": "bsigbuy2", "slot": CREATE_SLOT,
                "deltas": [(S2, 0, 100)], "signer": S2,
            },
            {
                "signature": "bsigbuy3", "slot": CREATE_SLOT,
                "deltas": [(S3, 0, 100)], "signer": S3,
            },
        ],
        supply=1000.0,
        wallet_n={S1: 1, S2: 1, S3: 1, DEV: 8, WHALE: 20},
    )
    rug.set_rpc_fn(rpc)
    helius.note_creator(TID, DEV, now=T0)
    rug.begin_cycle(T0)
    feat = rug.compute_features(MINT, creator=DEV, now=T0)
    assert feat["status"] == "ok"
    assert feat["bundled"] is True
    assert feat["bundled_buys"] == 3
    assert feat["skip_bundled"] is True
    creators = set((feat.get("features_json") or {}).get("creators") or [])
    assert creators == {DEV}
    assert S1 not in creators and S2 not in creators and S3 not in creators
    assert feat["dev_initial_share"] == pytest.approx(0.05)
    assert feat["sniper_wallets"] == 3
    extra = feat.get("features_json") or {}
    assert extra.get("calls_used", 0) >= 4
    assert extra.get("create_sig") == "asigcreate"


def test_partial_window_resumes_never_ok_while_partial(ytr, monkeypatch):
    """Capped window that needs more than one tick stays pending until complete."""
    yt, helius, rug = ytr
    monkeypatch.setenv("YOUNG_TRAIL_RUGRISK_CYCLE_CAP", "4")
    tx_list = [{
        "signature": "asigcreate", "slot": CREATE_SLOT,
        "deltas": [(DEV, 0, 50)],
        "ixs": [_ix("create", MINT)], "signer": DEV,
    }]
    buyers = (S1, S2, S3)
    for i in range(20):
        owner = buyers[i % 3]
        tx_list.append({
            "signature": f"bsig{i:04d}", "slot": CREATE_SLOT + 1,
            "deltas": [(owner, 0, 10)], "signer": owner,
        })
    rpc = _rpc_scripted(
        tx_list=tx_list, supply=1000.0,
        wallet_n={S1: 1, S2: 1, S3: 1, DEV: 8, WHALE: 20},
    )
    rug.set_rpc_fn(rpc)
    _enter(yt, helius, creator=DEV)
    rug.sync_entries(now=T0)
    statuses = []
    for _ in range(16):
        rug.begin_cycle(T0)
        rug.compute_pending(now=T0, limit=1, source="live")
        row = rug.all_rows()[0]
        statuses.append(row["status"])
        extra = _feat_json(row)
        if row["status"] == "ok":
            assert extra.get("progress") is None
            break
        assert row["status"] == "pending"
        assert extra.get("progress")
        assert extra.get("calls_used", 0) >= 1
        assert row["skip_snipers"] is None
        assert row["bundled"] is None
    assert statuses[-1] == "ok"
    assert "ok" not in statuses[:-1]
    assert len(statuses) > 1
    extra = _feat_json(rug.all_rows()[0])
    assert extra.get("window_capped") is True
    assert extra.get("calls_used", 0) >= 4
    assert extra.get("calls_used", 0) <= rug.TARGET_ENTRY_CALLS + 4
    window_tx = [
        c[1][0] for c in rpc.calls
        if c[0] == "getTransaction" and (
            c[1][0] == "asigcreate" or str(c[1][0]).startswith("bsig")
        )
    ]
    assert window_tx.count("asigcreate") == 1
    buy_fetched = {s for s in window_tx if str(s).startswith("bsig")}
    assert len(buy_fetched) == rug.WINDOW_NON_CREATE_CAP
    assert "bsig0008" not in buy_fetched


def test_new_entry_prioritized_ahead_of_older_pending(ytr, monkeypatch):
    yt, helius, rug = ytr
    monkeypatch.setenv("YOUNG_TRAIL_RUGRISK_CYCLE_CAP", "6")
    rpc = _rpc_scripted()
    rug.set_rpc_fn(rpc)
    old_tid = "OldMintRugRisk11111111111111111111:1399811149"
    db = rug._connect()
    db.execute(
        """INSERT INTO young_trail_rugrisk (
             tid, ticker, chain, entry_ts, entry_price, status, source
           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
        (old_tid, "OLD", "solana", T0 - 3600, 1.0, "pending", "sync"),
    )
    db.commit()
    _enter(yt, helius, creator=DEV)
    rug.on_sampler_tick({}, now=T0)
    rows = {r["tid"]: r for r in rug.all_rows()}
    assert old_tid in rows and TID in rows
    assert rows[old_tid]["status"] == "pending"
    assert rows[old_tid]["features_json"] in (None, "")
    assert rows[TID]["status"] in ("pending", "ok")
    assert rows[TID]["features_json"]
    extra = _feat_json(rows[TID])
    assert extra.get("calls_used", 0) >= 1


def test_pump_global_pda_vector():
    import young_trail_rugrisk as r
    pda = r.find_program_address([b"global"], r.PUMP_PROGRAM_ID)
    assert pda == "4wTV1YmiEkRvAtNtsSGPtUrqRYQMe5SKy2uB4Jjaxnjf"
    assert r.bonding_curve_pda("not-a-pubkey") is None


def _hot_mint_history(n=5001):
    return [
        {"signature": f"hot{i:05d}", "slot": 9000 - i, "err": None}
        for i in range(n)
    ]


def test_hot_mint_finds_create_via_bonding_curve(ytr, monkeypatch):
    """>5,000 mint sigs: bonding-curve short history still finds the create slot."""
    _yt, helius, rug = ytr
    monkeypatch.setattr(rug, "bonding_curve_pda", lambda *a, **k: CURVE)
    monkeypatch.setattr(rug, "bonding_curve_v2_pda", lambda *a, **k: None)
    curve_oldest = [
        {
            "signature": "csigcreate", "slot": CREATE_SLOT,
            "deltas": [(DEV, 0, 50)],
            "ixs": [_ix("create", MINT)], "signer": DEV,
        },
        {
            "signature": "csigbuy1", "slot": CREATE_SLOT,
            "deltas": [(S1, 0, 100)], "signer": S1,
        },
        {
            "signature": "csigbuy2", "slot": CREATE_SLOT,
            "deltas": [(S2, 0, 100)], "signer": S2,
        },
        {
            "signature": "csigbuy3", "slot": CREATE_SLOT,
            "deltas": [(S3, 0, 100)], "signer": S3,
        },
    ]
    extra = {}
    curve_sigs = []
    for spec in curve_oldest:
        extra[spec["signature"]] = _tx(
            spec["slot"], MINT, spec.get("deltas") or [],
            ixs=spec.get("ixs"), signer=spec.get("signer"),
        )
        curve_sigs.append({
            "signature": spec["signature"], "slot": spec["slot"], "err": None,
        })
    rpc = _rpc_scripted(
        buys=[],
        mint_history=_hot_mint_history(),
        extra_txs=extra,
        sig_by_addr={CURVE: list(reversed(curve_sigs))},
        wallet_n={S1: 1, S2: 1, S3: 1, DEV: 8, WHALE: 20},
    )
    rug.set_rpc_fn(rpc)
    helius.note_creator(TID, DEV, now=T0)
    rug.begin_cycle(T0)
    feat = rug.compute_features(MINT, creator=DEV, now=T0)
    assert feat["status"] == "ok"
    assert feat["create_slot"] == CREATE_SLOT
    assert feat["history_truncated"] is False
    assert feat["bundled"] is True
    assert feat["bundled_buys"] == 3
    extra_j = feat.get("features_json") or {}
    assert extra_j.get("window_src") == "bonding_curve"
    assert extra_j.get("calls_used", 99) <= rug.TARGET_ENTRY_CALLS
    mint_sig_calls = [
        c for c in rpc.calls
        if c[0] == "getSignaturesForAddress" and c[1][0] == MINT
    ]
    assert mint_sig_calls == []


def test_hot_mint_finds_create_via_creator_wallet(ytr, monkeypatch):
    _yt, helius, rug = ytr
    monkeypatch.setattr(rug, "bonding_curve_pda", lambda *a, **k: None)
    monkeypatch.setattr(rug, "bonding_curve_v2_pda", lambda *a, **k: None)
    rpc = _rpc_scripted(
        buys=[],
        mint_history=_hot_mint_history(),
        creator_txs=[
            ("asigcreate", CREATE_SLOT, MINT, "create", [(DEV, 0, 50)]),
            ("devother", CREATE_SLOT - 20, PRIOR, "initializeMint", [(DEV, 0, 1)]),
        ],
        extra_txs={
            "asigcreate": _tx(
                CREATE_SLOT, MINT, [(DEV, 0, 50)],
                ixs=[_ix("create", MINT)], signer=DEV,
            ),
        },
    )
    rug.set_rpc_fn(rpc)
    helius.note_creator(TID, DEV, now=T0)
    rug.begin_cycle(T0)
    feat = rug.compute_features(MINT, creator=DEV, now=T0)
    assert feat["status"] == "ok"
    assert feat["create_slot"] == CREATE_SLOT
    assert feat["history_truncated"] is False
    extra_j = feat.get("features_json") or {}
    assert extra_j.get("window_src") == "creator"
    assert extra_j.get("calls_used", 99) <= rug.TARGET_ENTRY_CALLS
    mint_sig_calls = [
        c for c in rpc.calls
        if c[0] == "getSignaturesForAddress" and c[1][0] == MINT
    ]
    assert mint_sig_calls == []


def test_no_create_path_unavailable_when_mint_history_is_hot(ytr, monkeypatch):
    _yt, _helius, rug = ytr
    monkeypatch.setattr(rug, "bonding_curve_pda", lambda *a, **k: None)
    monkeypatch.setattr(rug, "bonding_curve_v2_pda", lambda *a, **k: None)
    rpc = _rpc_scripted(
        buys=[],
        mint_history=_hot_mint_history(),
        creator_txs=[],
    )
    rug.set_rpc_fn(rpc)
    rug.begin_cycle(T0)
    feat = rug.compute_features(MINT, creator=None, now=T0)
    assert feat["status"] == "unavailable"
    assert feat["history_truncated"] is True
    assert feat["create_slot"] is None
    assert feat["skip_snipers"] is None
    assert feat["skip_bundled"] is None
    assert feat["skip_dev_rugs"] is None
    mint_sig_calls = [
        c for c in rpc.calls
        if c[0] == "getSignaturesForAddress" and c[1][0] == MINT
    ]
    assert len(mint_sig_calls) == 1


def test_reset_truncated_rows_requeues_for_new_path(ytr):
    _yt, _helius, rug = ytr
    db = rug._connect()
    db.execute(
        "DELETE FROM young_trail_rugrisk_meta WHERE key = 'reset_truncated_create_path'"
    )
    db.execute(
        """INSERT INTO young_trail_rugrisk (
             tid, ticker, chain, entry_ts, entry_price, status, error, features_json
           ) VALUES (?, 'RRK', 'solana', ?, 1.0, 'unavailable', ?, ?)""",
        (TID, T0, "history_truncated", json.dumps({"history_truncated": True})),
    )
    other = "OtherMint111111111111111111111111111:1399811149"
    db.execute(
        """INSERT INTO young_trail_rugrisk (
             tid, ticker, chain, entry_ts, entry_price, status, error
           ) VALUES (?, 'OTH', 'solana', ?, 1.0, 'unavailable', ?)""",
        (other, T0, "rpc timeout"),
    )
    db.commit()
    rug._reset_truncated_rows(db)
    db.commit()
    rows = {r["tid"]: r for r in rug.all_rows()}
    assert rows[TID]["status"] == "pending"
    assert rows[TID]["error"] is None
    assert rows[TID]["features_json"] in (None, "")
    assert rows[TID]["source"] == "requeue"
    assert rows[other]["status"] == "unavailable"
    assert rows[other]["error"] == "rpc timeout"


def test_get_transaction_sets_max_supported_version(ytr):
    _yt, helius, rug = ytr
    rpc = _rpc_scripted()
    rug.set_rpc_fn(rpc)
    helius.note_creator(TID, DEV, now=T0)
    rug.begin_cycle(T0)
    feat = rug.compute_features(MINT, creator=DEV, now=T0)
    assert feat["status"] == "ok"
    tx_calls = [c for c in rpc.calls if c[0] == "getTransaction"]
    assert tx_calls
    for _method, params in tx_calls:
        assert isinstance(params[1], dict)
        assert params[1].get("maxSupportedTransactionVersion") == 0
    for _method, params in rpc.calls:
        if _method in ("getTransaction", "getBlock", "getTransactionsForAddress"):
            assert isinstance(params[1], dict)
            assert params[1].get("maxSupportedTransactionVersion") == 0


def test_gtfa_network_blip_retries_after_cooldown(ytr, monkeypatch):
    _yt, _helius, rug = ytr
    monkeypatch.setattr(rug, "GTFA_RETRY_SEC", 1800)
    n = {"gtfa": 0}
    inner = _rpc_scripted()

    def rpc(method, params, timeout):
        if method == "getTransactionsForAddress":
            n["gtfa"] += 1
            raise ConnectionError("timed out contacting rpc")
        return inner(method, params, timeout)

    rpc.calls = inner.calls
    rug.set_rpc_fn(rpc)
    rug.begin_cycle(T0)
    addr = "SomeCurve11111111111111111111111111"
    assert rug._gtfa_oldest(addr, now=T0) is None
    assert rug._gtfa_supported is not False
    assert n["gtfa"] == 1
    rug.begin_cycle(T0)
    assert rug._gtfa_oldest(addr, now=T0 + 60) is None
    assert n["gtfa"] == 1
    rug.begin_cycle(T0)
    assert rug._gtfa_oldest(addr, now=T0 + 1800) is None
    assert n["gtfa"] == 2


def test_gtfa_method_not_found_stays_unsupported(ytr):
    _yt, _helius, rug = ytr
    n = {"gtfa": 0}
    inner = _rpc_scripted()

    def rpc(method, params, timeout):
        if method == "getTransactionsForAddress":
            n["gtfa"] += 1
            raise RuntimeError("Method not found")
        return inner(method, params, timeout)

    rpc.calls = inner.calls
    rug.set_rpc_fn(rpc)
    rug.begin_cycle(T0)
    addr = "SomeCurve11111111111111111111111111"
    assert rug._gtfa_oldest(addr, now=T0) is None
    assert rug._gtfa_supported is False
    assert n["gtfa"] == 1
    rug.begin_cycle(T0)
    assert rug._gtfa_oldest(addr, now=T0 + 3600) is None
    assert n["gtfa"] == 1


def test_sniper_share_not_dominated_by_one_or_two_early_buys(ytr, monkeypatch):
    """Curve/pool buy in the first txs must not dominate sniper share."""
    _yt, _helius, rug = ytr
    monkeypatch.setattr(rug, "bonding_curve_pda", lambda *a, **k: CURVE_OWNER)
    monkeypatch.setattr(rug, "bonding_curve_v2_pda", lambda *a, **k: None)
    rpc = _rpc_scripted(
        buys=[(CREATE_SLOT, [
            (CURVE_OWNER, 0, 400), (S1, 0, 50), (S2, 0, 50),
        ])],
        supply=1000.0,
        signer=DEV,
    )
    rug.set_rpc_fn(rpc)
    rug.begin_cycle(T0)
    feat = rug.compute_features(MINT, creator=DEV, now=T0)
    assert feat["status"] == "ok"
    # Denominator is total supply, not 500 window buys. Curve excluded.
    assert feat["sniper_share"] == pytest.approx(0.10)
    assert feat["sniper_wallets"] == 2
    assert feat["sniper_denominator"] == pytest.approx(1000.0)
    assert feat["skip_snipers"] is False
    extra = feat.get("features_json") or {}
    assert extra.get("buys")
    assert extra.get("sniper_denominator") == pytest.approx(1000.0)
    assert extra.get("calls_used", 99) <= rug.TARGET_ENTRY_CALLS


def test_sniper_min_wallets_guard():
    import young_trail_rugrisk as r
    few = r.skip_flags({
        "status": "ok", "sniper_share": 0.45, "sniper_wallets": 2,
        "bundled": False, "dev_prior_rugs": 0,
    })
    assert few["skip_snipers"] is False
    enough = r.skip_flags({
        "status": "ok", "sniper_share": 0.45, "sniper_wallets": 4,
        "bundled": False, "dev_prior_rugs": 0,
    })
    assert enough["skip_snipers"] is True
    none = r.skip_flags({
        "status": "ok", "sniper_share": 0.45,
        "bundled": False, "dev_prior_rugs": 0,
    })
    assert none["skip_snipers"] is False


def test_dev_history_fetched_under_normal_budget(ytr, caplog):
    caplog.set_level(logging.INFO)
    _yt, _helius, rug = ytr
    rpc = _rpc_scripted()
    rug.set_rpc_fn(rpc)
    rug.begin_cycle(T0)
    feat = rug.compute_features(MINT, creator=DEV, now=T0)
    assert feat["status"] == "ok"
    assert feat["dev_prior_launches"] == 1
    assert feat["dev_prior_rugs"] == 1
    assert feat["skip_dev_rugs"] is True
    assert "skipped budget" not in caplog.text


def test_per_entry_calls_under_target(ytr):
    _yt, helius, rug = ytr
    rpc = _rpc_scripted()
    rug.set_rpc_fn(rpc)
    helius.note_creator(TID, DEV, now=T0)
    rug.begin_cycle(T0)
    feat = rug.compute_features(MINT, creator=DEV, now=T0)
    used = (feat.get("features_json") or {}).get("calls_used", 99)
    assert feat["status"] == "ok"
    assert used <= rug.TARGET_ENTRY_CALLS
    assert rug.TARGET_ENTRY_CALLS <= 8


def test_dev_and_creator_cache_reuse(ytr, monkeypatch):
    _yt, helius, rug = ytr
    rpc = _rpc_scripted()
    rug.set_rpc_fn(rpc)
    rug.begin_cycle(T0)
    n = {"n": 0}
    orig = helius.lookup_creator

    def spy(tid):
        n["n"] += 1
        return orig(tid)

    monkeypatch.setattr(helius, "lookup_creator", spy)
    helius.note_creator(TID, DEV, now=T0)
    assert rug._lookup_creator(TID) == DEV
    assert rug._lookup_creator(TID) == DEV
    assert n["n"] == 1
    a = rug._dev_history(DEV, MINT, now=T0)
    after = len(rpc.calls)
    b = rug._dev_history(DEV, MINT, now=T0)
    assert a["dev_prior_rugs"] == 1
    assert b["dev_prior_rugs"] == 1
    assert len(rpc.calls) == after


def test_recompute_sniper_from_cached_no_rpc():
    import young_trail_rugrisk as r
    row = {
        "tid": TID, "status": "ok",
        "sniper_share": 0.416, "sniper_wallets": 2, "sniper_slots": 32,
        "features_json": {
            "buys": [
                {"owner": S1, "delta": 400, "slot": CREATE_SLOT},
                {"owner": S2, "delta": 16, "slot": CREATE_SLOT},
            ],
            "supply": 1000.0, "create_slot": CREATE_SLOT, "creators": [DEV],
        },
    }
    out = r.recompute_sniper_from_cached(row)
    assert out is not None
    assert out["sniper_share"] == pytest.approx(0.416)
    assert out["sniper_wallets"] == 2
    assert out["sniper_denominator"] == pytest.approx(1000.0)
    assert out["skip_snipers"] is False
    cheap = r.recompute_sniper_from_cached({
        "tid": TID, "status": "ok",
        "sniper_share": 0.50, "sniper_wallets": 2,
    })
    assert cheap["skip_snipers"] is False
    assert cheap["sniper_min_wallets"] == r.SNIPER_MIN_WALLETS


def test_add_column_pragma_before_alter():
    src = (ROOT / "young_trail_rugrisk.py").read_text()
    cols = src.split("def _table_columns")[1].split("\ndef ")[0]
    add = src.split("def _add_column")[1].split("\ndef ")[0]
    assert "PRAGMA table_info" in cols
    assert "_table_columns" in add
    assert "ALTER TABLE" in add
    assert add.find("_table_columns") < add.find("ALTER TABLE")


def test_summary_unavailable_not_labeled_error(ytr):
    _yt, _helius, rug = ytr
    db = rug._connect()
    db.execute(
        """INSERT INTO young_trail_rugrisk (
             tid, ticker, chain, entry_ts, entry_price, status, source
           ) VALUES (?, 'OK', 'solana', ?, 1.0, 'ok', 't')""",
        (TID, T0),
    )
    db.execute(
        """INSERT INTO young_trail_rugrisk (
             tid, ticker, chain, entry_ts, entry_price, status, error, source
           ) VALUES (?, 'U', 'solana', ?, 1.0, 'unavailable', 'history_truncated', 't')""",
        ("UnavailMint1111111111111111111111111:1399811149", T0),
    )
    db.execute(
        """INSERT INTO young_trail_rugrisk (
             tid, ticker, chain, entry_ts, entry_price, status, error, source
           ) VALUES (?, 'E', 'solana', ?, 1.0, 'error', 'rpc timeout', 't')""",
        ("ErrorMint111111111111111111111111111:1399811149", T0),
    )
    db.commit()
    s = rug._summary()
    assert s["ok"] == 1
    assert s["unavailable"] == 1
    assert s["error"] == 1
    html = (ROOT / "ops.html").read_text()
    assert "Unavailable" in html
    assert "s.unavailable" in html


def test_fair_share_newest_new_and_backlog():
    import young_trail_rugrisk as r
    rows = [{"tid": "new:1"}, {"tid": "old:1"}, {"tid": "old:2"}]
    ordered = r._fair_share_pending(rows, ["new:1"], 2)
    assert [x["tid"] for x in ordered] == ["new:1", "old:1"]
    only_old = r._fair_share_pending(rows[1:], [], 2)
    assert [x["tid"] for x in only_old] == ["old:1", "old:2"]


def test_dev_history_logs_budget_skip(ytr, caplog):
    caplog.set_level(logging.WARNING)
    _yt, _helius, rug = ytr
    rpc = _rpc_scripted()
    rug.set_rpc_fn(rpc)
    rug.begin_cycle(T0)
    while rug._can_charge(1, T0):
        assert rug._charge(1, T0)
    out = rug._dev_history(DEV, MINT, now=T0)
    assert out["dev_prior_rugs"] is None
    assert out["dev_prior_launches"] is None
    assert "skipped budget" in caplog.text
    assert "SUPERSECRET" not in caplog.text
