"""Richer judge inputs: optional flow, holder growth, keyless dev-sells.

Mocked only. thresholds.py / questions.py / 0.6 cutoff / sizing stay untouched.
"""
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

os.environ.setdefault("DESK_SECRET", "test-secret-for-judge-inputs")
os.environ.setdefault("JUDGE_MOCK", "1")

import book
import collect
import cycle_history
import gt_info_cache
import judge_inputs
import main as shift
from fomo_api import Fomo
from pick import summary, size_factor_for
from thresholds import PICK_MIN_WORTH, NO_SOCIAL_CUT

# Sample token used in the PR before/after line.
SAMPLE_D = {
    "chain": "solana", "age_minutes": 42, "mcap_usd": 300_000,
    "liquidity_usd": 48_000, "holder_count": 310, "ticker": "T0",
    "tid": "Addr0:1399811149", "addr": "Addr0", "net": 1399811149,
}
SAMPLE_ANS = {
    "shape": {"choice": "crowd", "probabilities": {"crowd": 0.7}},
    "concentration_is_exit_risk": {"noul": 0.2},
    "authority_risk": {"choice": "renounced"},
}
SAMPLE_BEFORE = (
    "solana, 42m old, $300,000 mcap, $48,000 liq, 310 holders; "
    "crowd 0.70, concentration risk 0.20; authority renounced; no usable X account"
)
SAMPLE_AFTER = (
    SAMPLE_BEFORE
    + "; flow 1h b/s 540/120, 5m b/s 40/15"
    + "; holders +50 in 30m (+38.5%/h)"
    + "; dev sold 12% of initial since launch"
)


def _prefix_bits(text: str) -> list[str]:
    return text.split("; ")[:2]


def test_summary_prefix_unchanged_without_optional_fields():
    s = summary(SAMPLE_D, SAMPLE_ANS)
    assert s == SAMPLE_BEFORE
    assert _prefix_bits(s) == [
        "solana, 42m old, $300,000 mcap, $48,000 liq, 310 holders",
        "crowd 0.70, concentration risk 0.20",
    ]


def test_summary_renders_flow_with_and_without_m5():
    with_m5 = {**SAMPLE_D, "buys_h1": 540, "sells_h1": 120, "buys_m5": 40, "sells_m5": 15}
    s = summary(with_m5, SAMPLE_ANS)
    assert s.startswith(SAMPLE_BEFORE)
    assert "flow 1h b/s 540/120, 5m b/s 40/15" in s
    without_m5 = {**SAMPLE_D, "buys_h1": 540, "sells_h1": 120}
    s2 = summary(without_m5, SAMPLE_ANS)
    assert "flow 1h b/s 540/120" in s2
    assert "5m b/s" not in s2


def test_summary_omits_flow_when_h1_missing():
    s = summary({**SAMPLE_D, "buys_h1": 10}, SAMPLE_ANS)
    assert "flow" not in s
    s2 = summary({**SAMPLE_D, "buys_m5": 1, "sells_m5": 1}, SAMPLE_ANS)
    assert "flow" not in s2


def test_summary_renders_holder_growth_and_omits_when_missing():
    with_g = {**SAMPLE_D, "holder_growth": "holders +50 in 30m (+38.5%/h)"}
    s = summary(with_g, SAMPLE_ANS)
    assert "holders +50 in 30m (+38.5%/h)" in s
    assert summary(SAMPLE_D, SAMPLE_ANS) == SAMPLE_BEFORE


def test_summary_renders_dev_sells_and_omits_when_missing():
    sold = {**SAMPLE_D, "dev_sells": "dev sold 12% of initial since launch"}
    none = {**SAMPLE_D, "dev_sells": "dev sells: none seen"}
    assert "dev sold 12% of initial since launch" in summary(sold, SAMPLE_ANS)
    assert "dev sells: none seen" in summary(none, SAMPLE_ANS)
    assert "dev sold" not in summary(SAMPLE_D, SAMPLE_ANS)
    assert "dev sells" not in summary(SAMPLE_D, SAMPLE_ANS)


def test_summary_sample_token_before_after_all_fields():
    d = {
        **SAMPLE_D,
        "buys_h1": 540, "sells_h1": 120, "buys_m5": 40, "sells_m5": 15,
        "holder_growth": "holders +50 in 30m (+38.5%/h)",
        "dev_sells": "dev sold 12% of initial since launch",
    }
    s = summary(d, SAMPLE_ANS)
    assert s == SAMPLE_AFTER
    assert _prefix_bits(s) == _prefix_bits(SAMPLE_BEFORE)


def test_trade_counts_stashes_m5_from_dex():
    pairs = [{
        "liquidity": {"usd": 50_000},
        "pairAddress": "pair1",
        "volume": {"m5": 111.0, "h1": 2222.0, "h24": 9000.0},
        "txns": {
            "m5": {"buys": 4, "sells": 2},
            "h1": {"buys": 10, "sells": 8},
            "h6": {"buys": 50, "sells": 45},
            "h24": {"buys": 200, "sells": 180},
        },
    }]
    token = {"addr": "test", "ticker": "TEST", "net": 1399811149}
    with patch("collect.requests.get", return_value=Mock(status_code=200, json=lambda: pairs)):
        data, status = collect.trade_counts(token)
    assert status == "ok"
    assert data["buys_m5"] == 4 and data["sells_m5"] == 2
    assert token["buys_m5"] == 4 and token["sells_m5"] == 2
    assert data["buys_h1"] == 10
    assert token["vol_1h"] == pytest.approx(2222.0)
    assert token["vol_5m"] == pytest.approx(111.0)


def test_trade_counts_omits_m5_when_absent():
    pairs = [{
        "liquidity": {"usd": 50_000},
        "txns": {"h1": {"buys": 10, "sells": 8},
                 "h6": {"buys": 50, "sells": 45},
                 "h24": {"buys": 200, "sells": 180}},
    }]
    token = {"addr": "test", "ticker": "TEST", "net": 1399811149}
    with patch("collect.requests.get", return_value=Mock(status_code=200, json=lambda: pairs)):
        data, status = collect.trade_counts(token)
    assert status == "ok"
    assert data["buys_m5"] is None and data["sells_m5"] is None


def test_holder_growth_from_cycle_history_in_window():
    now = 1_700_000_000.0
    tid = SAMPLE_D["tid"]
    cycle_history.on_cycle(
        {"seen": 1, "benched": 0, "judged": 1, "requeued": 0,
         "tokens": [{"tid": tid, "ticker": "T0", "net": 1399811149,
                     "chain": "solana", "stage": "judged", "holder_count": 260}]},
        now=now - 30 * 60, source="live",
    )
    judge_inputs.set_time_fn(lambda: now)
    d = {**SAMPLE_D}
    judge_inputs.attach(d)
    assert d["holder_growth"] == "holders +50 in 30m (+38.5%/h)"


def test_holder_growth_omitted_outside_5_120_window():
    now = 1_700_000_000.0
    tid = SAMPLE_D["tid"]
    cycle_history.on_cycle(
        {"seen": 1, "benched": 0, "judged": 1, "requeued": 0,
         "tokens": [{"tid": tid, "ticker": "T0", "net": 1399811149,
                     "chain": "solana", "stage": "judged", "holder_count": 260}]},
        now=now - 3 * 60, source="live",
    )
    judge_inputs.set_time_fn(lambda: now)
    d = {**SAMPLE_D}
    judge_inputs.attach(d)
    assert "holder_growth" not in d

    cycle_history.reset()
    cycle_history.on_cycle(
        {"seen": 1, "benched": 0, "judged": 1, "requeued": 0,
         "tokens": [{"tid": tid, "ticker": "T0", "net": 1399811149,
                     "chain": "solana", "stage": "judged", "holder_count": 260}]},
        now=now - 130 * 60, source="live",
    )
    d2 = {**SAMPLE_D}
    judge_inputs.attach(d2)
    assert "holder_growth" not in d2


def test_holder_growth_from_gt_info_cache_previous():
    now = 1_700_000_000.0
    gt_info_cache.set_time_fn(lambda: now - 30 * 60)
    assert gt_info_cache.put("solana", "Addr0", {"holders": {"count": 260}})
    gt_info_cache.set_time_fn(lambda: now)
    assert gt_info_cache.put("solana", "Addr0", {"holders": {"count": 310}})
    judge_inputs.set_time_fn(lambda: now)
    d = {**SAMPLE_D}
    judge_inputs.attach(d)
    assert d["holder_growth"] == "holders +50 in 30m (+38.5%/h)"
    gt_info_cache.set_time_fn(None)


def _parsed_tx(mint, owner, pre, post):
    def bal(amount):
        return {"mint": mint, "owner": owner,
                "uiTokenAmount": {"amount": str(amount)}}
    return {"meta": {"preTokenBalances": [bal(pre)],
                     "postTokenBalances": [bal(post)]}}


def test_dev_sells_sold_percent_and_none_seen():
    mint, creator = "Addr0", "Dev111"
    txs = {
        "old": _parsed_tx(mint, creator, 1000, 1000),
        "new": _parsed_tx(mint, creator, 1000, 880),
    }

    def rpc(method, params, timeout):
        if method == "getSignaturesForAddress":
            return [{"signature": "new"}, {"signature": "old"}]
        return txs[params[0]]

    judge_inputs.set_rpc_fn(rpc)
    d = {**SAMPLE_D, "creator": creator}
    judge_inputs.attach(d)
    assert d["dev_sells"] == "dev sold 12% of initial since launch"

    judge_inputs.reset_cycle()

    def rpc_none(method, params, timeout):
        if method == "getSignaturesForAddress":
            return [{"signature": "a"}]
        return _parsed_tx(mint, creator, 1000, 1000)

    judge_inputs.set_rpc_fn(rpc_none)
    d2 = {**SAMPLE_D, "creator": creator}
    judge_inputs.attach(d2)
    assert d2["dev_sells"] == "dev sells: none seen"


def test_dev_sells_over_100_is_partial_window():
    judge_inputs.reset_cycle()
    mint, creator = "Addr0", "Dev111"
    txs = {
        "old": _parsed_tx(mint, creator, 100, 100),
        "new": _parsed_tx(mint, creator, 400, 150),
    }

    def rpc(method, params, timeout):
        if method == "getSignaturesForAddress":
            return [{"signature": "new"}, {"signature": "old"}]
        return txs[params[0]]

    judge_inputs.set_rpc_fn(rpc)
    d = {**SAMPLE_D, "creator": creator}
    judge_inputs.attach(d)
    assert d.get("dev_sold_pct") is None
    assert d["dev_sells_note"].startswith("partial window")
    assert "250" in d["dev_sells_note"]
    assert "since launch" not in (d.get("dev_sells") or "")
    assert judge_inputs.cycle_lookups() == 1


def test_dev_sells_window_not_reaching_launch():
    judge_inputs.reset_cycle()
    mint, creator = "Addr0", "Dev111"
    now = 1_700_100_000.0
    judge_inputs.set_time_fn(lambda: now)
    sigs = [{"signature": f"s{i}", "blockTime": now - 30} for i in range(20)]

    def rpc(method, params, timeout):
        if method == "getSignaturesForAddress":
            return sigs
        return _parsed_tx(mint, creator, 1000, 880)

    judge_inputs.set_rpc_fn(rpc)
    d = {**SAMPLE_D, "creator": creator, "age_minutes": 240}
    judge_inputs.attach(d)
    assert d.get("dev_sold_pct") is None
    assert d["dev_sells_note"].startswith("partial window")
    assert "of observed" in d["dev_sells_note"]
    assert "since launch" not in (d.get("dev_sells") or "")
    assert judge_inputs.cycle_lookups() == 1
    judge_inputs.set_time_fn(None)


def test_dev_sells_omitted_without_creator():
    def boom(*a, **k):
        raise AssertionError("RPC must not run without a creator")
    judge_inputs.set_rpc_fn(boom)
    d = {**SAMPLE_D}
    judge_inputs.attach(d)
    assert "dev_sells" not in d
    assert d.get("dev_sold_pct") is None
    assert judge_inputs.cycle_lookups() == 0


def test_creator_from_helius_computes_dev_sold_pct():
    import young_trail_helius
    young_trail_helius.reset()
    young_trail_helius.note_creator(SAMPLE_D["tid"], "DevFromHelius")
    mint, creator = SAMPLE_D["addr"], "DevFromHelius"
    txs = {
        "old": _parsed_tx(mint, creator, 1000, 1000),
        "new": _parsed_tx(mint, creator, 1000, 880),
    }

    def rpc(method, params, timeout):
        if method == "getSignaturesForAddress":
            assert params[0] == creator
            return [{"signature": "old"}, {"signature": "new"}]
        return txs[params[0]]

    judge_inputs.set_rpc_fn(rpc)
    d = {**SAMPLE_D}
    assert "creator" not in d or d.get("creator") != creator
    d.pop("creator", None)
    judge_inputs.attach(d)
    assert d["creator"] == creator
    assert d["dev_sells"] == "dev sold 12% of initial since launch"
    assert d["dev_sold_pct"] == pytest.approx(12.0)
    young_trail_helius.reset()


def _sold_rpc(mint, creator):
    txs = {
        "old": _parsed_tx(mint, creator, 1000, 1000),
        "new": _parsed_tx(mint, creator, 1000, 880),
    }

    def rpc(method, params, timeout):
        if method == "getSignaturesForAddress":
            assert params[0] == creator
            return [{"signature": "old"}, {"signature": "new"}]
        return txs[params[0]]

    return rpc


def test_creator_from_young_trail_rugrisk_dev_wallet():
    import young_trail_helius
    import young_trail_rugrisk
    young_trail_helius.reset()
    young_trail_rugrisk.reset()
    creator = "DevFromYTR"
    db = young_trail_rugrisk._connect()
    db.execute(
        """INSERT INTO young_trail_rugrisk
           (tid, ticker, chain, entry_ts, status, dev_wallet)
           VALUES (?, 'T0', 'solana', 1.0, 'ok', ?)""",
        (SAMPLE_D["tid"], creator),
    )
    db.commit()
    judge_inputs.set_rpc_fn(_sold_rpc(SAMPLE_D["addr"], creator))
    d = {k: v for k, v in SAMPLE_D.items() if k != "creator"}
    judge_inputs.attach(d)
    assert d["creator"] == creator
    assert d["dev_sold_pct"] == pytest.approx(12.0)
    assert young_trail_helius.lookup_creator(SAMPLE_D["tid"]) == creator
    row = young_trail_helius._connect().execute(
        "SELECT source FROM young_trail_helius_creators WHERE tid = ?",
        (SAMPLE_D["tid"],),
    ).fetchone()
    assert row["source"] == "rugrisk"
    young_trail_helius.reset()
    young_trail_rugrisk.reset()


def test_creator_from_desk_rugrisk_creators_array():
    import json
    import desk_rugrisk
    import young_trail_helius
    import young_trail_rugrisk
    young_trail_helius.reset()
    young_trail_rugrisk.reset()
    desk_rugrisk.reset()
    creator = "DevFromDesk"
    db = desk_rugrisk._connect()
    db.execute(
        """INSERT INTO desk_rugrisk
           (tid, ticker, ts, stage, status, features_json)
           VALUES (?, 'T0', 1.0, 'judged', 'ok', ?)""",
        (SAMPLE_D["tid"], json.dumps({"creators": [creator, "OtherWallet"]})),
    )
    db.commit()
    judge_inputs.set_rpc_fn(_sold_rpc(SAMPLE_D["addr"], creator))
    d = {k: v for k, v in SAMPLE_D.items() if k != "creator"}
    judge_inputs.attach(d)
    assert d["creator"] == creator
    assert d["dev_sold_pct"] == pytest.approx(12.0)
    assert young_trail_helius.lookup_creator(SAMPLE_D["tid"]) == creator
    young_trail_helius.reset()
    desk_rugrisk.reset()
    young_trail_rugrisk.reset()


def test_rpc_failure_omitted_and_logged_via_safe_err(monkeypatch, caplog):
    secret = "https://secret-rpc.example/?api-key=LEAKME"
    monkeypatch.setenv("SOLANA_RPC_URL", secret)
    caplog.set_level(logging.WARNING)

    def boom(method, params, timeout):
        raise RuntimeError(f"Max retries exceeded with url: {secret}")

    judge_inputs.set_rpc_fn(boom)
    d = {**SAMPLE_D, "creator": "Dev111"}
    judge_inputs.attach(d)
    assert "dev_sells" not in d
    assert "LEAKME" not in caplog.text
    assert secret not in caplog.text
    assert "dev sells lookup failed" in caplog.text
    assert "RuntimeError" in caplog.text


def test_solana_rpc_url_never_logged_from_requests(monkeypatch, caplog):
    secret = "https://mainnet.helius-rpc.com/?api-key=SUPERSECRET"
    monkeypatch.setenv("SOLANA_RPC_URL", secret)
    caplog.set_level(logging.WARNING)

    def fake_post(url, json=None, timeout=None):
        raise ConnectionError(f"Failed to resolve {url}")

    with patch("judge_inputs.requests.post", side_effect=fake_post):
        d = {**SAMPLE_D, "creator": "Dev111"}
        judge_inputs.attach(d)
    assert "dev_sells" not in d
    assert "SUPERSECRET" not in caplog.text
    assert secret not in caplog.text
    src = (ROOT / "judge_inputs.py").read_text()
    for line in src.splitlines():
        if "rpc_url" in line and line.strip().startswith("log."):
            raise AssertionError(f"judge_inputs logs rpc_url: {line}")


def test_per_cycle_dev_sells_cap():
    calls = []

    def rpc(method, params, timeout):
        calls.append(method)
        return []

    judge_inputs.set_rpc_fn(rpc)
    for i in range(7):
        d = {**SAMPLE_D, "addr": f"Mint{i}", "creator": "Dev111", "ticker": f"T{i}"}
        judge_inputs.attach(d)
        if i < 5:
            assert d.get("dev_sells") == "dev sells: none seen"
        else:
            assert "dev_sells" not in d
    assert calls.count("getSignaturesForAddress") == 5
    assert judge_inputs.cycle_lookups() == 5


def test_evm_skips_unless_honeypot_already_has_creator():
    def boom(*a, **k):
        raise AssertionError("EVM must not hit Solana RPC")
    judge_inputs.set_rpc_fn(boom)
    d = {"chain": "bsc", "net": 56, "addr": "0xabc", "ticker": "B", "holder_count": 100}
    judge_inputs.attach(d)
    assert "dev_sells" not in d
    d2 = {**d, "creator": "0xdev"}
    judge_inputs.attach(d2)
    assert "dev_sells" not in d2
    d3 = {**d2, "creator_initial_balance": 1000, "creator_balance": 1000}
    judge_inputs.attach(d3)
    assert d3["dev_sells"] == "dev sells: none seen"
    d4 = {**d2, "creator_initial_balance": 1000, "creator_balance": 750}
    judge_inputs.attach(d4)
    assert d4["dev_sells"] == "dev sold 25% of initial since launch"


def test_attach_sets_market_state_keys():
    d = {**SAMPLE_D, "buys_h1": 10, "sells_h1": 8}
    judge_inputs.attach(d)
    assert d["flow"] == "flow 1h b/s 10/8"
    assert "holder_growth" not in d
    assert "dev_sells" not in d


def test_market_seat_receives_new_keys(monkeypatch):
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.execute("DELETE FROM carry")
    book.DB.commit()
    tid = "JTok:1399811149"
    now = 1_700_100_000.0
    cycle_history.on_cycle(
        {"seen": 1, "benched": 0, "judged": 1, "requeued": 0,
         "tokens": [{"tid": tid, "ticker": "JTK", "net": 1399811149,
                     "chain": "solana", "stage": "judged", "holder_count": 260}]},
        now=now - 30 * 60, source="live",
    )
    monkeypatch.setattr(shift, "_now", lambda: now)
    monkeypatch.setattr(time, "time", lambda: now)
    judge_inputs.set_time_fn(lambda: now)

    def fake_rpc(method, params, timeout):
        if method == "getSignaturesForAddress":
            return [{"signature": "s"}]
        return _parsed_tx("JTok", "DevJ", 1000, 880)

    judge_inputs.set_rpc_fn(fake_rpc)
    market_states = []

    def fake_shortlist(fomo, ids):
        return [{
            "addr": "JTok", "net": 1399811149, "tid": tid, "ticker": "JTK",
            "mcap_usd": 300_000, "liquidity_usd": 48_000, "volume_h24": 610_000,
            "price_usd": 0.001, "holder_count": 310,
            "change": {"5m": 0.04, "1h": 0.22, "4h": 0.4, "24h": 0.61},
            "age_minutes": 42, "chain": "solana", "creator": "DevJ",
        }]

    def fake_dossier(t, limiter=None):
        return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                "developer_holding_percentage": 2, "gt_score_details": None,
                "is_honeypot": None, "mint_authority": False, "freeze_authority": False,
                "description": "a token", "x_handle": None, "net": 1399811149,
                "creator": "DevJ"}

    def judge(qs, state):
        if qs == "market":
            market_states.append(state)
        if qs == "pick":
            ticker = state["candidates"][0]["ticker"]
            return {"model": "test", "answers": {
                "best": {"type": "choice", "choice": ticker, "confidence": 0.9,
                         "probabilities": {ticker: 0.9}},
                "worth_trading_at_all": {"type": "noul", "noul": 0.8},
            }, "usage": {}}
        return {"model": "test", "answers": {
            "concentration_is_exit_risk": {"type": "noul", "noul": 0.30},
            "momentum_already_spent": {"type": "noul", "noul": 0.25},
            "liquidity_fits_ticket": {"type": "noul", "noul": 0.72},
            "shape": {"type": "choice", "choice": "crowd", "confidence": 0.80,
                      "probabilities": {"crowd": 0.80, "one_buyer": 0.10,
                                        "fading": 0.05, "too_early": 0.05}},
            "authority_risk": {"type": "choice", "choice": "renounced", "confidence": 0.9,
                               "probabilities": {"renounced": 0.9, "mint_open": 0.05,
                                                 "freeze_open": 0.03, "both_open": 0.02}},
        }, "usage": {}}

    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: ([tid], {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist)
    monkeypatch.setattr(collect, "shortlist", fake_shortlist)
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: (
        {"buys_h1": 540, "sells_h1": 120, "buys_m5": 40, "sells_m5": 15,
         "buys_h6": 900, "sells_h6": 400, "trades_h24": 4000}, "ok"))
    monkeypatch.setattr(shift, "dossier", fake_dossier)

    class Desk:
        def bank(self):
            return 1000.0
        def read_x(self, h, tid=None, token=None):
            return None
        def log_shadow(self, o, s, fomo_data=None):
            pass
        def report(self, o, s):
            pass
        def send_to_seats(self, o):
            pass

    shift.run_once(type("F", (), {"token": lambda self: None})(),
                   judge, Desk(), 1000.0, shadow=True, gt_dossier_reserve=3)
    assert market_states
    st = market_states[0]
    assert st["flow"] == "flow 1h b/s 540/120, 5m b/s 40/15"
    assert st["holder_growth"] == "holders +50 in 30m (+38.5%/h)"
    assert st["dev_sells"] == "dev sold 12% of initial since launch"


def test_fomo_and_gt_creator_pass_through():
    row = Fomo._row({
        "symbol": "T0", "marketCap": 1, "liquidity": 1, "volume24": 1,
        "priceUSD": 1, "holders": 10, "createdAt": int(time.time() * 1000),
        "creatorAddress": "DevFromFomo",
    })
    assert row["creator"] == "DevFromFomo"
    t = collect.normalise("Mint1:1399811149", {**row, "change": {}})
    assert t["creator"] == "DevFromFomo"
    d = collect._apply_gt_info_attributes(dict(t), t, "solana", {
        "holders": {"count": 12},
        "creator": "DevFromGT",
    })
    assert d["creator"] == "DevFromGT"
    d2 = collect._apply_gt_info_attributes(dict(t), t, "solana", {
        "holders": {"count": 12},
    })
    assert d2["creator"] == "DevFromFomo"


def test_cutoff_and_sizing_unchanged():
    assert PICK_MIN_WORTH == 0.60
    assert NO_SOCIAL_CUT == 0.60
    ans = {"shape": {"choice": "crowd"}}
    assert size_factor_for(ans) == NO_SOCIAL_CUT
    ans2 = {**ans, "account_is_the_project": {"noul": 0.9}}
    assert size_factor_for(ans2) == 1.0


def test_max_tx_version_is_one_on_all_three_methods():
    for method in ("getTransaction", "getBlock", "getTransactionsForAddress"):
        out = judge_inputs._with_max_tx_version(
            method, ["addr", {"encoding": "jsonParsed"}],
        )
        assert out[1]["maxSupportedTransactionVersion"] == 1
        bare = judge_inputs._with_max_tx_version(method, ["addr"])
        assert bare[1]["maxSupportedTransactionVersion"] == 1
    other = judge_inputs._with_max_tx_version("getTokenSupply", ["mint"])
    assert other == ["mint"]


def test_tx_version_error_retries_once_and_succeeds():
    n = {"n": 0}
    versions = []

    def rpc(method, params, timeout):
        n["n"] += 1
        versions.append((params[1] or {}).get("maxSupportedTransactionVersion"))
        if n["n"] == 1:
            raise RuntimeError("Transaction version (1) is not supported")
        return [{"signature": "s"}]

    judge_inputs.set_rpc_fn(rpc)
    got = judge_inputs._rpc("getTransaction", ["sig", {"encoding": "jsonParsed"}], 8)
    assert got == [{"signature": "s"}]
    assert n["n"] == 2
    assert versions == [1, 1]


def test_thresholds_and_questions_diff_empty():
    for name in ("thresholds.py", "questions.py"):
        r = subprocess.run(
            ["git", "diff", "--", name],
            cwd=str(ROOT), capture_output=True, text=True, check=True,
        )
        assert r.stdout == "", r.stdout
    src = (ROOT / "thresholds.py").read_text()
    assert "PICK_MIN_WORTH  = 0.60" in src
    q = (ROOT / "questions.py").read_text()
    assert "Classify the shape of this launch from the fields in `state`." in q
    assert "`developer_holding_percentage` is large enough that the creator" in q
