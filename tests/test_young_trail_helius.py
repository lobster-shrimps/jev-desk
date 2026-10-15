"""Paper Helius rug-signal ledger. No live orders. No young_trail edits."""
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

os.environ.setdefault("DESK_SECRET", "test-secret-for-yt-helius")
os.environ.setdefault("JUDGE_MOCK", "1")

TICKET = 50.0
ENTRY = 1.0
T0 = 1_700_000_000.0
MINT = "MintHelius11111111111111111111111111111"
TID = f"{MINT}:1399811149"
DEV = "DevWallet111111111111111111111111111111"
TOP1 = "TopHolder11111111111111111111111111111"
TOP2 = "TopHolder22222222222222222222222222222"
TOP3 = "TopHolder33333333333333333333333333333"
POOL = "PoolPda1111111111111111111111111111111"
ACC_DEV = "AccDev1111111111111111111111111111111"
ACC_T1 = "AccTop1111111111111111111111111111111"
ACC_T2 = "AccTop2222222222222222222222222222222"
ACC_T3 = "AccTop3333333333333333333333333333333"
ACC_POOL = "AccPool111111111111111111111111111111"
PUMP = "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA"


def _tok(tid=TID, ticker="HLS", age=22.0, price=ENTRY, chain="solana",
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


def _largest(*pairs):
    """pairs: (account, amount)."""
    return {"value": [{"address": a, "amount": str(amt)} for a, amt in pairs]}


def _parsed_token_acc(owner, program=None):
    return {
        "owner": program or "TokenkegQfeZyiNwAJbNbGKPFXCWuBvf9Ss623VQ5DA",
        "data": {"parsed": {"info": {"owner": owner}}},
        "lamports": 2_039_280,
    }


def _rpc_ok(largest=None, owners=None, pool_lamports=1_000_000_000,
            dev_amount=None, wsol=None):
    """Build a scripted Helius RPC. Counts calls."""
    largest = largest or _largest(
        (ACC_DEV, 1000), (ACC_T1, 800), (ACC_T2, 700),
        (ACC_T3, 600), (ACC_POOL, 5000),
    )
    owners = owners or {
        ACC_DEV: _parsed_token_acc(DEV),
        ACC_T1: _parsed_token_acc(TOP1),
        ACC_T2: _parsed_token_acc(TOP2),
        ACC_T3: _parsed_token_acc(TOP3),
        ACC_POOL: _parsed_token_acc(POOL, program=PUMP),
    }
    calls = []

    def rpc(method, params, timeout):
        calls.append((method, params))
        if method == "getTokenLargestAccounts":
            return largest
        if method == "getMultipleAccounts":
            addrs = params[0]
            return {"value": [owners.get(a) for a in addrs]}
        if method == "getAccountInfo":
            return {"value": {"lamports": pool_lamports, "owner": PUMP}}
        if method == "getTokenAccountsByOwner":
            owner, filt = params[0], params[1]
            mint = filt.get("mint") if isinstance(filt, dict) else None
            if mint == "So11111111111111111111111111111111111111112":
                amt = 0 if wsol is None else wsol
            else:
                amt = 0 if dev_amount is None else dev_amount
            return {"value": [{
                "account": {"data": {"parsed": {"info": {
                    "tokenAmount": {"amount": str(amt)},
                }}}}
            }]}
        raise AssertionError(f"unexpected rpc {method}")

    rpc.calls = calls
    return rpc


@pytest.fixture
def yth(monkeypatch):
    import young_trail
    import young_trail_helius
    monkeypatch.delenv("YOUNG_TRAIL_UNTIL", raising=False)
    monkeypatch.delenv("YOUNG_TRAIL_HELIUS_UNTIL", raising=False)
    monkeypatch.delenv("YOUNG_TRAIL_HELIUS_ENABLED", raising=False)
    monkeypatch.delenv("YOUNG_TRAIL_HELIUS_CYCLE_CAP", raising=False)
    monkeypatch.delenv("YOUNG_TRAIL_HELIUS_DAY_CAP", raising=False)
    young_trail.reset()
    young_trail.set_time_fn(lambda: T0)
    young_trail.set_sleep_fn(lambda _d: None)
    young_trail.set_get_fn(lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("young_trail tests must not use the network")
    ))
    young_trail_helius.reset()
    young_trail_helius.set_time_fn(lambda: T0)
    young_trail_helius.set_rpc_fn(_rpc_ok())
    yield young_trail, young_trail_helius
    young_trail.reset()
    young_trail_helius.reset()
    young_trail_helius.set_rpc_fn(None)


def _enter_sol(yt, helius, now=T0, **extra):
    tok = _tok(**extra)
    yt.consider_entries([tok], now=now)
    if extra.get("creator"):
        helius.note_creator(tok["tid"], extra["creator"], now=now)
    return tok


def test_copies_solana_only_same_tid_and_price(yth):
    yt, helius = yth
    yt.consider_entries([
        _tok(),
        _tok(tid="BscTok:56", ticker="BSC", chain="bsc", net=56),
    ], now=T0)
    n = helius.sync_entries(now=T0)
    assert n == 1
    opens = helius.open_rows()
    assert len(opens) == 1
    assert opens[0]["tid"] == TID
    assert opens[0]["entry_price"] == pytest.approx(ENTRY)
    assert opens[0]["exit_ts"] is None
    watches = helius.watch_rows()
    assert watches and watches[0]["mint"] == MINT


def test_does_not_backfill_pre_deploy_entries(yth):
    yt, helius = yth
    yt.consider_entries([_tok()], now=T0)
    helius.deploy_ts(now=T0 + 10)
    assert helius.sync_entries(now=T0 + 20) == 0
    assert helius.open_rows() == []


def test_snapshot_records_dev_top10_and_pool(yth):
    yt, helius = yth
    _enter_sol(yt, helius, creator=DEV)
    helius.begin_cycle(T0)
    helius.sync_entries(now=T0)
    assert helius.snapshot_pending(now=T0) == 1
    watch = helius.watch_rows()[0]
    assert watch["snapshot_ok"] == 1
    assert watch["dev_wallet"] == DEV
    assert watch["dev_amount"] == pytest.approx(1000)
    assert watch["pool_account"] == POOL
    assert watch["pool_sol_lamports"] == pytest.approx(1_000_000_000)
    holders = json.loads(watch["holders_json"])
    assert [h["owner"] for h in holders][:3] == [DEV, TOP1, TOP2]
    assert all(not h["is_pool"] for h in holders)
    top3 = json.loads(watch["top3_json"])
    assert len(top3) == 3


def test_dev_sell_20_paper_exit(yth):
    yt, helius = yth
    _enter_sol(yt, helius, creator=DEV)
    helius.on_sampler_tick({TID: ENTRY}, now=T0)
    assert helius.open_rows() and helius.open_rows()[0]["exit_ts"] is None

    later = _rpc_ok(largest=_largest(
        (ACC_DEV, 790), (ACC_T1, 800), (ACC_T2, 700),
        (ACC_T3, 600), (ACC_POOL, 5000),
    ))
    helius.set_rpc_fn(later)
    helius.set_time_fn(lambda: T0 + 60)
    helius.on_sampler_tick({TID: 0.80}, now=T0 + 60)
    closed = helius.closed_rows(limit=None)
    assert len(closed) == 1
    row = closed[0]
    assert row["signal_type"] == "dev_sell_20"
    assert row["signal_ts"] == pytest.approx(T0 + 60)
    assert row["signal_price"] == pytest.approx(0.80)
    assert row["exit_price"] == pytest.approx(0.80)
    assert row["pnl_usd"] == pytest.approx(-10.0)
    assert yt.open_rows() and yt.open_rows()[0]["exit_ts"] is None


def test_top3_sell_30_and_pool_sol_drop(yth):
    yt, helius = yth
    _enter_sol(yt, helius, creator=DEV)
    helius.on_sampler_tick({TID: ENTRY}, now=T0)

    later = _rpc_ok(
        largest=_largest(
            (ACC_DEV, 1000), (ACC_T1, 800), (ACC_T2, 480),
            (ACC_T3, 600), (ACC_POOL, 5000),
        ),
        pool_lamports=1_000_000_000,
    )
    helius.set_rpc_fn(later)
    helius.set_time_fn(lambda: T0 + 60)
    helius.on_sampler_tick({TID: 0.90}, now=T0 + 60)
    row = helius.closed_rows(limit=None)[0]
    assert row["signal_type"] == "top3_sell_30"
    assert row["pnl_usd"] == pytest.approx(-5.0)

    # Fresh tid for the pool-drop path.
    mint2 = "MintPool22222222222222222222222222222"
    tid2 = f"{mint2}:1399811149"
    yt.consider_entries([_tok(tid=tid2, ticker="PL")], now=T0)
    helius.note_creator(tid2, DEV, now=T0)
    helius.set_rpc_fn(_rpc_ok())
    helius.set_time_fn(lambda: T0)
    helius.on_sampler_tick({tid2: ENTRY}, now=T0)
    drop = _rpc_ok(pool_lamports=740_000_000)
    helius.set_rpc_fn(drop)
    helius.set_time_fn(lambda: T0 + 120)
    helius.on_sampler_tick({tid2: 0.50}, now=T0 + 120)
    pool_row = [r for r in helius.closed_rows(limit=None) if r["tid"] == tid2]
    assert pool_row and pool_row[0]["signal_type"] == "pool_sol_drop_25"
    assert pool_row[0]["pnl_usd"] == pytest.approx(-25.0)


def test_under_threshold_stays_open(yth):
    yt, helius = yth
    _enter_sol(yt, helius, creator=DEV)
    helius.on_sampler_tick({TID: ENTRY}, now=T0)
    later = _rpc_ok(largest=_largest(
        (ACC_DEV, 850), (ACC_T1, 800), (ACC_T2, 700),
        (ACC_T3, 600), (ACC_POOL, 5000),
    ), pool_lamports=800_000_000)
    helius.set_rpc_fn(later)
    helius.on_sampler_tick({TID: 0.95}, now=T0 + 60)
    assert helius.open_rows() and not helius.closed_rows(limit=None)


def test_detect_signals_pure():
    import young_trail_helius as h
    watch = {
        "dev_wallet": DEV,
        "dev_amount": 1000,
        "holders_json": json.dumps([
            {"owner": DEV, "account": ACC_DEV, "amount": 1000},
        ]),
        "top3_json": json.dumps([
            {"owner": TOP1, "account": ACC_T1, "amount": 800},
        ]),
        "pool_sol_lamports": 1_000_000_000,
    }
    largest = [
        {"address": ACC_DEV, "amount": 1000},
        {"address": ACC_T1, "amount": 800},
    ]
    assert h.detect_signals(watch, largest, 1_000_000_000) == []
    assert h.detect_signals(watch, [
        {"address": ACC_DEV, "amount": 800},
        {"address": ACC_T1, "amount": 800},
    ], 1_000_000_000) == ["dev_sell_20"]
    assert h.detect_signals(watch, [
        {"address": ACC_DEV, "amount": 1000},
        {"address": ACC_T1, "amount": 560},
    ], 1_000_000_000) == ["top3_sell_30"]
    assert h.detect_signals(watch, largest, 750_000_000) == ["pool_sol_drop_25"]


def test_cycle_and_day_budget_caps(yth, monkeypatch):
    yt, helius = yth
    monkeypatch.setenv("YOUNG_TRAIL_HELIUS_CYCLE_CAP", "2")
    monkeypatch.setenv("YOUNG_TRAIL_HELIUS_DAY_CAP", "5")
    rpc = _rpc_ok()
    helius.set_rpc_fn(rpc)
    mint_b = "MintBudget222222222222222222222222222"
    yt.consider_entries([
        _tok(creator=DEV),
        _tok(tid=f"{mint_b}:1399811149", ticker="HB", creator=DEV),
    ], now=T0)
    helius.note_creator(TID, DEV, now=T0)
    helius.note_creator(f"{mint_b}:1399811149", DEV, now=T0)
    helius.begin_cycle(T0)
    helius.sync_entries(now=T0)
    n = helius.snapshot_pending(now=T0)
    # First watch uses 2 calls (largest+owners); second needs 2 more and waits.
    assert n == 1
    assert helius.budget_remaining(T0)["cycle_used"] == 2
    assert helius.budget_remaining(T0)["day_used"] == 2
    helius.begin_cycle(T0)
    n2 = helius.snapshot_pending(now=T0)
    assert n2 == 1
    assert helius.budget_remaining(T0)["day_used"] == 4
    helius.begin_cycle(T0)
    assert helius.snapshot_pending(now=T0) == 0
    assert helius._charge(1, T0) is True
    assert helius._charge(1, T0) is False
    bud = helius.budget_remaining(T0)
    assert bud["day_used"] == 5
    assert bud["day_left"] == 0
    assert bud["cycle_left"] == 1


def test_rpc_url_never_logged(yth, monkeypatch, caplog):
    yt, helius = yth
    secret = "https://mainnet.helius-rpc.com/?api-key=SUPERSECRET"
    monkeypatch.setenv("SOLANA_RPC_URL", secret)
    caplog.set_level(logging.WARNING)

    def boom(method, params, timeout):
        raise RuntimeError(f"Max retries exceeded with url: {secret}")

    helius.set_rpc_fn(boom)
    _enter_sol(yt, helius, creator=DEV)
    helius.on_sampler_tick({TID: ENTRY}, now=T0)
    blob = caplog.text + json.dumps(helius.ops_payload())
    assert "SUPERSECRET" not in blob
    assert secret not in blob
    src = (ROOT / "young_trail_helius.py").read_text()
    for line in src.splitlines():
        if "rpc_url" in line and line.strip().startswith("log."):
            raise AssertionError(f"logs rpc_url: {line}")
    assert "print(" not in src


def test_rpc_failure_via_requests_safe_err(yth, monkeypatch, caplog):
    yt, helius = yth
    secret = "https://mainnet.helius-rpc.com/?api-key=LEAKME99"
    monkeypatch.setenv("SOLANA_RPC_URL", secret)
    caplog.set_level(logging.WARNING)
    helius.set_rpc_fn(None)

    def fake_post(url, json=None, timeout=None):
        raise ConnectionError(f"Failed to resolve {url}")

    from unittest.mock import patch
    _enter_sol(yt, helius, creator=DEV)
    with patch("requests.post", side_effect=fake_post):
        helius.on_sampler_tick({TID: ENTRY}, now=T0)
    assert "LEAKME99" not in caplog.text
    assert secret not in caplog.text
    assert "young_trail_helius" in caplog.text


def test_precision_recall_lead_from_labels(yth, tmp_path, monkeypatch):
    yt, helius = yth
    _enter_sol(yt, helius, creator=DEV)
    helius.on_sampler_tick({TID: ENTRY}, now=T0)
    later = _rpc_ok(largest=_largest(
        (ACC_DEV, 700), (ACC_T1, 800), (ACC_T2, 700),
        (ACC_T3, 600), (ACC_POOL, 5000),
    ))
    helius.set_rpc_fn(later)
    helius.set_time_fn(lambda: T0 + 120)
    helius.on_sampler_tick({TID: 0.60}, now=T0 + 120)

    import research
    research.reset()
    rdb = research._connect()
    rdb.execute(
        """INSERT INTO token_labels (
             tid, ticker, chain, first_seen_ts, first_free_ts, rugged, rug_ts,
             time_to_rug_min, runner, updated_ts, origin
           ) VALUES (?, 'HLS', 'solana', ?, ?, 1, ?, 8.0, 0, ?, 'live')""",
        (TID, T0, T0, T0 + 600, T0 + 700),
    )
    rdb.commit()
    q = helius.quality()
    assert q["signals"] == 1
    assert q["rugs_watched"] == 1
    assert q["rugs_caught"] == 1
    assert q["precision"] == pytest.approx(1.0)
    assert q["recall"] == pytest.approx(1.0)
    assert q["lead_min_median"] == pytest.approx(8.0)  # (600-120)/60
    closed = helius.closed_rows(limit=None)[0]
    assert closed["rugged"] == 1
    assert closed["lead_min"] == pytest.approx(8.0)


def test_remember_creators_from_fomo_tokens(yth):
    _yt, helius = yth
    n = helius.remember_creators([
        _tok(creator=DEV),
        _tok(tid="Bsc:56", chain="bsc", net=56, creator="0xdev"),
        {"tid": TID, "foo": 1},
    ], now=T0)
    assert n == 1
    assert helius.lookup_creator(TID) == DEV


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
    import young_trail_helius
    young_trail_helius.reset()
    conn = young_trail_helius._connect()
    names = {r[0] for r in conn.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "young_trail_helius" in names
    assert "young_trail_helius_watch" in names
    assert "young_trail_helius_signals" in names
    assert "young_trail_helius_meta" in names
    assert "young_trail" in names
    keep = conn.execute(
        "SELECT ticker, entry_price FROM young_trail WHERE tid='keep:1'"
    ).fetchone()
    assert keep["ticker"] == "KEEP"
    assert keep["entry_price"] == pytest.approx(2.5)
    young_trail_helius.init_schema(conn)
    young_trail_helius.init_schema(conn)
    young_trail_helius.reset()


def test_schema_steps_commit_independently(tmp_path, monkeypatch):
    path = tmp_path / "partial.db"
    monkeypatch.setenv("OUTCOMES_DB", str(path))
    import young_trail_helius
    young_trail_helius.reset()
    db = sqlite3.connect(path)
    db.row_factory = sqlite3.Row
    steps = []

    def boom():
        steps.append("boom")
        raise sqlite3.OperationalError("simulated fail")

    young_trail_helius._schema_step(db, "boom", boom)
    young_trail_helius._schema_step(db, "meta", lambda: db.execute(
        "CREATE TABLE IF NOT EXISTS young_trail_helius_meta "
        "(key TEXT PRIMARY KEY, value TEXT)"
    ))
    names = {r[0] for r in db.execute(
        "SELECT name FROM sqlite_master WHERE type='table'"
    )}
    assert "young_trail_helius_meta" in names
    assert steps == ["boom"]
    db.close()
    young_trail_helius.reset()


def test_until_matches_young_trail_oct20(yth, monkeypatch):
    yt, helius = yth
    monkeypatch.setenv("YOUNG_TRAIL_UNTIL", "2026-10-20T11:38:07Z")
    assert helius.until_ts(T0) == pytest.approx(yt.until_ts(T0))
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(helius.until_ts(T0)))
    assert iso.startswith("2026-10-20T")


def test_ops_payload_and_endpoint(yth):
    yt, helius = yth
    _enter_sol(yt, helius, creator=DEV)
    helius.on_sampler_tick({TID: ENTRY}, now=T0)
    later = _rpc_ok(largest=_largest(
        (ACC_DEV, 700), (ACC_T1, 800), (ACC_T2, 700),
        (ACC_T3, 600), (ACC_POOL, 5000),
    ))
    helius.set_rpc_fn(later)
    helius.set_time_fn(lambda: T0 + 60)
    helius.on_sampler_tick({TID: 0.70}, now=T0 + 60)
    payload = helius.ops_payload()
    assert payload["paper"] is True
    assert payload["simulated"] is True
    assert payload["live_orders"] is False
    assert payload["summary"]["closed_count"] == 1
    assert payload["comparison"]
    row = payload["comparison"][0]
    assert row["tid"] == TID
    assert row["helius_signal"] == "dev_sell_20"
    assert row["helius_pnl_usd"] == pytest.approx(-15.0)
    blob = json.dumps(payload)
    assert "SUPERSECRET" not in blob
    from fastapi.testclient import TestClient
    import server
    resp = TestClient(server.app).get("/ops/young_trail_helius")
    assert resp.status_code == 200
    data = resp.json()
    assert data["paper"] is True
    assert data["live_orders"] is False
    assert data["summary"]["closed_count"] == 1
    assert "budget" in data


def test_ops_html_has_helius_section():
    html = (ROOT / "ops.html").read_text()
    assert 'id="yt-helius"' in html
    assert "/ops/young_trail_helius" in html
    assert "Helius vs fast vs per-cycle" in html
    assert "https://" not in html


def test_sampler_drives_helius_without_young_trail_exits(yth):
    yt, helius = yth
    import research
    _enter_sol(yt, helius, creator=DEV)
    helius.on_sampler_tick({TID: ENTRY}, now=T0)

    later = _rpc_ok(largest=_largest(
        (ACC_DEV, 700), (ACC_T1, 800), (ACC_T2, 700),
        (ACC_T3, 600), (ACC_POOL, 5000),
    ))
    helius.set_rpc_fn(later)

    def get(url, timeout=None, headers=None, **kw):
        return type("R", (), {
            "status_code": 200,
            "json": lambda self=None: {"pairs": [{
                "chainId": "solana",
                "priceUsd": "0.70",
                "liquidity": {"usd": 9000},
                "baseToken": {"address": MINT, "symbol": "HLS"},
            }]},
        })()

    research.set_get_fn(get)
    research.set_time_fn(lambda: T0 + 60)
    from unittest.mock import patch
    called = []

    def boom(*a, **k):
        called.append(1)
        raise AssertionError("young_trail exits must not run")

    with patch("young_trail.mark_open", side_effect=boom):
        with patch("young_trail.enter", side_effect=boom):
            out = research.tick_sampler(now=T0 + 60)
    assert called == []
    assert out["marks"] >= 1
    closed = helius.closed_rows(limit=None)
    assert closed and closed[0]["signal_type"] == "dev_sell_20"
    assert yt.open_rows()[0]["exit_ts"] is None


def test_no_secrets_in_payload(yth, monkeypatch, caplog):
    yt, helius = yth
    monkeypatch.setenv("COINGECKO_API_KEY", "cg-FAKESECRET-should-never-appear")
    monkeypatch.setenv("DESK_SECRET", "desk-secret-xyz-never")
    monkeypatch.setenv("SOLANA_RPC_URL", "https://mainnet.helius-rpc.com/?api-key=NEVERPRINT")
    caplog.set_level("DEBUG")
    _enter_sol(yt, helius, creator=DEV)
    helius.on_sampler_tick({TID: 0.7}, now=T0)
    blob = json.dumps(helius.ops_payload()) + caplog.text
    assert "cg-FAKESECRET-should-never-appear" not in blob
    assert "desk-secret-xyz-never" not in blob
    assert "NEVERPRINT" not in blob


def _insert_fast_mark(tid, ts, price, ticker="HLS"):
    import research
    rdb = research._connect()
    rdb.execute(
        """INSERT OR REPLACE INTO fast_marks (
             ts, ts_sec, tid, chain, ticker, price_usd, source, origin
           ) VALUES (?, ?, ?, 'solana', ?, ?, 'dex_batch', 'live')""",
        (float(ts), int(ts), tid, ticker, float(price)),
    )
    rdb.commit()


def test_absent_token_marked_from_stored_fast_marks_or_young_trail(yth):
    yt, helius = yth
    _enter_sol(yt, helius, creator=DEV)
    helius.sync_entries(now=T0)
    _insert_fast_mark(TID, T0 + 60, 0.42)
    helius.on_marks({}, now=T0 + 120)
    row = [r for r in helius.open_rows() if r["tid"] == TID][0]
    assert row["last_price"] == pytest.approx(0.42)
    assert row["last_mark_ts"] == pytest.approx(T0 + 60)

    mint2 = "MintStoredYt2222222222222222222222222"
    tid2 = f"{mint2}:1399811149"
    yt.consider_entries([_tok(tid=tid2, ticker="YST")], now=T0)
    helius.sync_entries(now=T0)
    yt.mark_open({tid2: 0.33}, now=T0 + 60)
    helius.on_marks({}, now=T0 + 120)
    row2 = [r for r in helius.open_rows() if r["tid"] == tid2][0]
    assert row2["last_price"] == pytest.approx(0.33)
    assert row2["last_price"] != pytest.approx(ENTRY)


def test_signal_with_no_fresh_mark_is_unmarked_null_pnl(yth):
    yt, helius = yth
    _enter_sol(yt, helius, creator=DEV)
    helius.on_sampler_tick({TID: ENTRY}, now=T0)
    assert helius.open_rows()
    later = _rpc_ok(largest=_largest(
        (ACC_DEV, 700), (ACC_T1, 800), (ACC_T2, 700),
        (ACC_T3, 600), (ACC_POOL, 5000),
    ))
    helius.set_rpc_fn(later)
    helius.set_time_fn(lambda: T0 + 16 * 60)
    helius.on_sampler_tick({}, now=T0 + 16 * 60)
    closed = helius.closed_rows(limit=None)
    assert len(closed) == 1
    assert closed[0]["exit_reason"] == "unmarked"
    assert closed[0]["pnl_usd"] is None
    assert closed[0]["pnl_usd"] != 0
    assert closed[0]["exit_price"] is None
    summary = helius.ops_payload()["summary"]
    assert summary["closed_count"] == 0
    assert summary["realized_pnl_usd"] == pytest.approx(0.0)
    q = helius.quality()
    assert q["signals"] == 0


def test_6h_time_exit_fires(yth):
    yt, helius = yth
    _enter_sol(yt, helius, creator=DEV)
    helius.on_sampler_tick({TID: ENTRY}, now=T0)
    helius.set_time_fn(lambda: T0 + 60)
    helius.on_marks({TID: 0.80}, now=T0 + 60)
    assert helius.open_rows()
    helius.set_time_fn(lambda: T0 + 6 * 3600)
    stats = helius.on_marks({}, now=T0 + 6 * 3600)
    assert stats["closed"] == 1
    row = helius.closed_rows(limit=None)[0]
    assert row["exit_reason"] == "time"
    assert row["exit_price"] == pytest.approx(0.80)
    assert row["pnl_usd"] == pytest.approx(-10.0)
    assert row["signal_type"] is None


def test_rug_before_signal_negative_lead_not_true_positive(yth):
    yt, helius = yth
    _enter_sol(yt, helius, creator=DEV)
    helius.on_sampler_tick({TID: ENTRY}, now=T0)
    later = _rpc_ok(largest=_largest(
        (ACC_DEV, 700), (ACC_T1, 800), (ACC_T2, 700),
        (ACC_T3, 600), (ACC_POOL, 5000),
    ))
    helius.set_rpc_fn(later)
    helius.set_time_fn(lambda: T0 + 120)
    helius.on_sampler_tick({TID: 0.60}, now=T0 + 120)

    import research
    research.reset()
    rdb = research._connect()
    rdb.execute(
        """INSERT INTO token_labels (
             tid, ticker, chain, first_seen_ts, first_free_ts, rugged, rug_ts,
             time_to_rug_min, runner, updated_ts, origin
           ) VALUES (?, 'HLS', 'solana', ?, ?, 1, ?, 1.0, 0, ?, 'live')""",
        (TID, T0, T0, T0 + 30, T0 + 700),
    )
    rdb.commit()
    q = helius.quality()
    closed = helius.closed_rows(limit=None)[0]
    assert closed["lead_min"] == pytest.approx((30 - 120) / 60.0)
    assert closed["lead_min"] < 0
    assert q["true_positives"] == 0
    assert q["rugs_watched"] == 1
    assert q["rugs_caught"] == 0
    assert q["lead_n"] == 0
    assert q["lead_min_median"] is None
    assert q["precision"] == pytest.approx(0.0)


def test_marking_when_helius_budget_exhausted(yth, monkeypatch):
    yt, helius = yth
    _enter_sol(yt, helius, creator=DEV)
    helius.on_sampler_tick({TID: ENTRY}, now=T0)
    monkeypatch.setenv("YOUNG_TRAIL_HELIUS_CYCLE_CAP", "0")
    helius.begin_cycle(T0 + 60)
    helius.set_time_fn(lambda: T0 + 60)
    stats = helius.on_marks({TID: 0.55}, now=T0 + 60)
    assert stats["checked"] == 0
    assert stats["skipped"] >= 1
    assert stats["marked"] == 1
    row = helius.open_rows()[0]
    assert row["last_price"] == pytest.approx(0.55)
    assert row["last_mark_ts"] == pytest.approx(T0 + 60)
    assert row["exit_ts"] is None


def test_no_stale_entry_price_when_marks_exist_elsewhere(yth):
    yt, helius = yth
    _enter_sol(yt, helius, creator=DEV)
    helius.sync_entries(now=T0)
    _insert_fast_mark(TID, T0 + 60, 0.31)
    helius.on_marks({}, now=T0 + 31 * 60)
    row = helius.open_rows()[0]
    assert row["last_price"] != pytest.approx(ENTRY)
    assert row["last_price"] == pytest.approx(0.31)
    assert (T0 + 31 * 60) - float(row["entry_ts"]) > 30 * 60


def test_signal_after_time_exit_is_logged_only(yth):
    yt, helius = yth
    _enter_sol(yt, helius, creator=DEV)
    helius.on_sampler_tick({TID: ENTRY}, now=T0)
    helius.on_marks({TID: 0.80}, now=T0 + 60)
    helius.on_marks({}, now=T0 + 6 * 3600)
    assert helius.closed_rows(limit=None)[0]["exit_reason"] == "time"
    later = _rpc_ok(largest=_largest(
        (ACC_DEV, 700), (ACC_T1, 800), (ACC_T2, 700),
        (ACC_T3, 600), (ACC_POOL, 5000),
    ))
    helius.set_rpc_fn(later)
    helius.set_time_fn(lambda: T0 + 6 * 3600 + 60)
    stats = helius.on_marks({TID: 0.20}, now=T0 + 6 * 3600 + 60)
    closed = helius.closed_rows(limit=None)
    assert len(closed) == 1
    assert closed[0]["exit_reason"] == "time"
    assert closed[0]["pnl_usd"] == pytest.approx(-10.0)
    assert stats["signals"] >= 1
    sigs = helius.signal_rows(limit=None)
    assert sigs and sigs[0]["signal_type"] == "dev_sell_20"


OVERNIGHT = [
    # ticker, stored exit multiple of $1 entry → pnl on a $50 ticket
    ("RFC", 0.04, -48.0),
    ("ELON2028", 0.06, -47.0),
    ("WAP", 0.10, -45.0),
    ("VEILTROLL", 0.20, -40.0),
    ("Riot Games", 0.28, -36.0),
    ("smol", 0.50, -25.0),
]


def test_replay_overnight_zero_closes_repairs_to_about_minus_241(yth):
    yt, helius = yth
    seeded = []
    for i, (ticker, stored, _pnl) in enumerate(OVERNIGHT):
        mint = f"Mint{ticker.replace(' ', '')}{'x' * 20}"[:40]
        tid = f"{mint}:1399811149"
        entry_ts = T0 + i
        exit_ts = T0 + 3600
        yt.consider_entries([_tok(tid=tid, ticker=ticker)], now=entry_ts)
        helius.sync_entries(now=entry_ts)
        yt.mark_open({tid: stored}, now=entry_ts + 60)
        seeded.append((tid, exit_ts))
    db = helius._connect()
    for tid, exit_ts in seeded:
        db.execute(
            """UPDATE young_trail_helius
               SET last_price = entry_price, last_mark_ts = entry_ts,
                   signal_type = 'dev_sell_20', signal_ts = ?, signal_price = entry_price,
                   pnl_usd = 0.0, exit_ts = ?, exit_price = entry_price,
                   exit_reason = 'dev_sell_20'
               WHERE tid = ?""",
            (exit_ts, exit_ts, tid),
        )
    db.commit()
    closed = helius.closed_rows(limit=None)
    assert len(closed) == 6
    assert all(r["pnl_usd"] == pytest.approx(0.0) for r in closed)
    plan = helius.plan_zero_exit_repairs()
    assert len(plan) == 6
    diff = helius.format_repair_diff(plan)
    assert "RFC" in diff
    assert "ELON2028" in diff
    dry_pnl = sum(p["new_pnl_usd"] or 0 for p in plan)
    assert dry_pnl == pytest.approx(-241.0)
    # dry-run must not write
    assert all(r["pnl_usd"] == pytest.approx(0.0) for r in helius.closed_rows(limit=None))
    n = helius.apply_zero_exit_repairs(plan)
    assert n == 6
    repaired = helius.closed_rows(limit=None)
    assert sum(r["pnl_usd"] or 0 for r in repaired) == pytest.approx(-241.0)
    assert all(r["exit_price"] != pytest.approx(r["entry_price"]) for r in repaired)


def test_repair_script_dry_run_cli(yth, tmp_path, monkeypatch):
    yt, helius = yth
    _enter_sol(yt, helius, creator=DEV)
    helius.sync_entries(now=T0)
    yt.mark_open({TID: 0.50}, now=T0 + 60)
    db = helius._connect()
    db.execute(
        """UPDATE young_trail_helius
           SET last_price = entry_price, pnl_usd = 0.0,
               exit_ts = ?, exit_price = entry_price, signal_type = 'dev_sell_20'
           WHERE tid = ?""",
        (T0 + 120, TID),
    )
    db.commit()
    db_path = os.environ["OUTCOMES_DB"]
    import repair_helius_zero_exits as repair
    rc = repair.main(["--db", db_path])
    assert rc == 0
    row = helius.closed_rows(limit=None)[0]
    assert row["pnl_usd"] == pytest.approx(0.0)
    rc = repair.main(["--db", db_path, "--apply"])
    assert rc == 0
    helius.reset()
    row = helius.closed_rows(limit=None)[0]
    assert row["pnl_usd"] == pytest.approx(-25.0)


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
