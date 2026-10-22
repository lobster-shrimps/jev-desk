"""Replay pick-judge inputs: old summary vs quality-fixed summary.

CI has no live LLM key. Run with --live on the Mac when JUDGE_URL /
TYPESAFE_API_KEY are set. Without a key this prints summaries and, if
requested, mock_judge score hashes (not real Jev scores).
"""
from __future__ import annotations

import argparse
import os
import sys

from pick import social_tail, summary
from secret_utils import safe_err

# Eleven tokens from the post-#96 judge replay. Facts are the ones that
# poisoned the score (borrowed mega-accounts, sniper > 1, default lp/boost
# zeros, create-slot dust marked bundled). Missing facts stay None.
CASES = (
    {"ticker": "hellop", "kind": "pick", "old_score": 0.70,
     "d": {"chain": "solana", "age_minutes": 38, "mcap_usd": 210_000,
           "liquidity_usd": 42_000, "holder_count": 280, "ticker": "hellop",
           "tid": "hellop:1399811149",
           "_rug_veto": {"sniper_share": 1.02, "bundled": 1, "sniper_wallets": 6}},
     "ans": {"shape": {"probabilities": {"crowd": 0.68}},
             "concentration_is_exit_risk": {"noul": 0.22},
             "authority_risk": {"choice": "renounced"}}},
    {"ticker": "CHILL", "kind": "pick", "old_score": 0.64,
     "d": {"chain": "solana", "age_minutes": 51, "mcap_usd": 180_000,
           "liquidity_usd": 36_000, "holder_count": 190, "ticker": "CHILL",
           "tid": "CHILL:1399811149",
           "_rug_veto": {"sniper_share": 0.18, "bundled": 1, "sniper_wallets": 3}},
     "ans": {"shape": {"probabilities": {"crowd": 0.62}},
             "concentration_is_exit_risk": {"noul": 0.28},
             "authority_risk": {"choice": "renounced"}}},
    {"ticker": "HORN", "kind": "pick", "old_score": 0.72,
     "d": {"chain": "solana", "age_minutes": 44, "mcap_usd": 260_000,
           "liquidity_usd": 51_000, "holder_count": 744, "ticker": "HORN",
           "tid": "HornMint111111111111111111111111111111:1399811149",
           "developer_holding_percentage": 1.2,
           "_rug_veto": {"sniper_share": 0.21, "bundled": 1, "sniper_wallets": 4}},
     "ans": {"shape": {"probabilities": {"crowd": 0.71}},
             "concentration_is_exit_risk": {"noul": 0.18},
             "authority_risk": {"choice": "renounced"}}},
    {"ticker": "PROWLER", "kind": "pick", "old_score": 0.81,
     "d": {"chain": "solana", "age_minutes": 29, "mcap_usd": 310_000,
           "liquidity_usd": 48_000, "holder_count": 410, "ticker": "PROWLER",
           "tid": "ProwlerMint11111111111111111111111111:1399811149",
           "_rug_veto": {"sniper_share": 0.75, "bundled": 1, "sniper_wallets": 8}},
     "ans": {"shape": {"probabilities": {"crowd": 0.74}},
             "concentration_is_exit_risk": {"noul": 0.31},
             "authority_risk": {"choice": "renounced"}}},
    {"ticker": "PEPA", "kind": "pick", "old_score": 0.72,
     "d": {"chain": "solana", "age_minutes": 103, "mcap_usd": 140_000,
           "liquidity_usd": 22_000, "holder_count": 160, "ticker": "PEPA",
           "tid": "PepaMint111111111111111111111111111111:1399811149",
           "x_account": {"handle": "PEPA", "followers": 40, "account_age_days": 4}},
     "ans": {"shape": {"probabilities": {"crowd": 0.66}},
             "concentration_is_exit_risk": {"noul": 0.24},
             "authority_risk": {"choice": "renounced"}}},
    {"ticker": "FOD", "kind": "reject", "old_score": 0.54,
     "d": {"chain": "solana", "age_minutes": 40, "mcap_usd": 240_000,
           "liquidity_usd": 38_000, "holder_count": 320, "ticker": "FOD",
           "tid": "FodMint:1399811149",
           "x_account": {"handle": "elonmusk", "followers": 4_400_000,
                         "account_age_days": 5000, "x_kind": "borrowed"},
           "_rug_veto": {"sniper_share": 0.14, "bundled": 1, "sniper_wallets": 3}},
     "ans": {"shape": {"probabilities": {"crowd": 0.58}},
             "concentration_is_exit_risk": {"noul": 0.26},
             "authority_risk": {"choice": "renounced"},
             "account_is_the_project": {"noul": 0.11},
             "effort": {"score": 1.0}}},
    {"ticker": "SHADOW", "kind": "reject", "old_score": 0.50,
     "d": {"chain": "solana", "age_minutes": 33, "mcap_usd": 190_000,
           "liquidity_usd": 29_000, "holder_count": 210, "ticker": "SHADOW",
           "tid": "ShadowMint:1399811149",
           "_rug_veto": {"sniper_share": 1.02, "bundled": 1, "sniper_wallets": 7}},
     "ans": {"shape": {"probabilities": {"crowd": 0.55}},
             "concentration_is_exit_risk": {"noul": 0.34},
             "authority_risk": {"choice": "renounced"}}},
    {"ticker": "Jeetjak", "kind": "reject", "old_score": 0.49,
     "d": {"chain": "solana", "age_minutes": 61, "mcap_usd": 110_000,
           "liquidity_usd": 19_000, "holder_count": 140, "ticker": "Jeetjak",
           "tid": "JeetjakMint:1399811149",
           "_rug_veto": {"sniper_share": 0.19, "bundled": 1, "sniper_wallets": 3}},
     "ans": {"shape": {"probabilities": {"crowd": 0.52}},
             "concentration_is_exit_risk": {"noul": 0.30},
             "authority_risk": {"choice": "renounced"}}},
    {"ticker": "COOKED", "kind": "reject", "old_score": 0.48,
     "d": {"chain": "solana", "age_minutes": 47, "mcap_usd": 165_000,
           "liquidity_usd": 27_000, "holder_count": 250, "ticker": "COOKED",
           "tid": "CookedMint:1399811149",
           "x_account": {"handle": "MrBeast", "followers": 1_400_000,
                         "account_age_days": 3200, "x_kind": "borrowed"},
           "_rug_veto": {"sniper_share": 0.16, "bundled": 1, "sniper_wallets": 4}},
     "ans": {"shape": {"probabilities": {"crowd": 0.54}},
             "concentration_is_exit_risk": {"noul": 0.29},
             "authority_risk": {"choice": "renounced"},
             "account_is_the_project": {"noul": 0.18},
             "effort": {"score": 1.2}}},
    {"ticker": "KUM", "kind": "reject", "old_score": 0.46,
     "d": {"chain": "solana", "age_minutes": 55, "mcap_usd": 95_000,
           "liquidity_usd": 16_000, "holder_count": 120, "ticker": "KUM",
           "tid": "KumMint:1399811149",
           "_rug_veto": {"sniper_share": 0.11, "bundled": 1, "sniper_wallets": 3}},
     "ans": {"shape": {"probabilities": {"crowd": 0.50}},
             "concentration_is_exit_risk": {"noul": 0.36},
             "authority_risk": {"choice": "renounced"}}},
    {"ticker": "Bien", "kind": "reject", "old_score": 0.45,
     "d": {"chain": "solana", "age_minutes": 36, "mcap_usd": 220_000,
           "liquidity_usd": 41_000, "holder_count": 280, "ticker": "Bien",
           "tid": "BienMint:1399811149",
           "x_account": {"handle": "Cristiano", "followers": 3_900_000,
                         "account_age_days": 4800, "x_kind": "borrowed"},
           "_rug_veto": {"sniper_share": 0.13, "bundled": 1, "sniper_wallets": 3}},
     "ans": {"shape": {"probabilities": {"crowd": 0.53}},
             "concentration_is_exit_risk": {"noul": 0.27},
             "authority_risk": {"choice": "renounced"},
             "account_is_the_project": {"noul": 0.09},
             "effort": {"score": 0.8}}},
)


def _legacy_social_tail(d, ans) -> str:
    """Pre-fix tail: raw followers, unclamped sniper, 0 defaults as 0.00/0%."""
    from pick import _fmt_unknown, _followers_and_age, _noul_of, _rug_bits

    d = d if isinstance(d, dict) else {}
    ans = ans if isinstance(ans, dict) else {}
    followers, age = _followers_and_age(d)
    official = _noul_of(ans, "account_is_the_project")
    recycled = _noul_of(ans, "recycled_account")
    loaded = _noul_of(ans, "dev_still_loaded")
    rug = _rug_bits(d)
    sold = d.get("dev_sold_pct")
    holding = d.get("developer_holding_percentage")
    if holding is None:
        holding = d.get("dev_pct")
    # Old path treated missing boost/lp as 0.00 / 0%.
    boost = 0.0 if d.get("boost_amount") is None else d.get("boost_amount")
    lp = 0.0 if d.get("lp_locked_pct") is None else d.get("lp_locked_pct")
    promo = d.get("paid_promo")
    return "; ".join([
        f"x followers {_fmt_unknown(followers, kind='int')}, "
        f"account age {_fmt_unknown(age, kind='int')}"
        + ("d" if age is not None else ""),
        f"official {_fmt_unknown(official, kind='noul')}, "
        f"recycled {_fmt_unknown(recycled, kind='noul')}, "
        f"dev_loaded {_fmt_unknown(loaded, kind='noul')}",
        f"sniper {_fmt_unknown(rug.get('sniper_share'), kind='share')}, "
        f"bundled {_fmt_unknown(rug.get('bundled'), kind='bool')}, "
        f"snipers {_fmt_unknown(rug.get('sniper_wallets'), kind='int')}",
        f"dev holding {_fmt_unknown(holding, kind='pct')}, "
        f"dev sold {_fmt_unknown(sold, kind='pct')}",
        f"paid promo {_fmt_unknown(promo, kind='bool')}, "
        f"dex boost {_fmt_unknown(boost, kind='share')}, "
        f"lp {_fmt_unknown(lp, kind='pct')}",
    ])


def _legacy_summary(d, ans) -> str:
    """summary() as it looked after #96, before the input-quality fixes."""
    full = summary(d, ans)
    tail = social_tail(d, ans)
    prefix = full[: -(len(tail) + 2)] if full.endswith("; " + tail) else full
    return prefix + "; " + _legacy_social_tail(d, ans)


def _old_summary(d, ans) -> str:
    """Prefix-only summary, matching pick.summary before the social tail."""
    full = summary(d, ans)
    tail = social_tail(d, ans)
    if full.endswith("; " + tail):
        return full[: -(len(tail) + 2)]
    return full


def _live_available() -> bool:
    return bool(os.environ.get("TYPESAFE_API_KEY") or os.environ.get("JUDGE_URL"))


def _score_with(judge, d, ans, text) -> float | None:
    try:
        r = judge("pick", {"candidates": [{"ticker": d["ticker"], "summary": text}]})
        return float(((r.get("answers") or {}).get("worth_trading_at_all") or {}).get("noul"))
    except Exception as e:
        print(f"  judge failed: {safe_err(e)}")
        return None


def replay(*, live: bool = False, use_mock: bool = False) -> list[dict]:
    judge = None
    mode = "summaries-only"
    if live and _live_available():
        try:
            from judge_client import judge as live_judge
            judge = live_judge
            mode = "live"
        except Exception as e:
            print(f"live judge unavailable: {safe_err(e)}")
    elif use_mock:
        try:
            from mock_judge import mock_judge_fn
            judge = mock_judge_fn()
            mode = "mock (not real Jev)"
        except Exception as e:
            print(f"mock judge unavailable: {safe_err(e)}")

    rows = []
    print(f"judge-social replay mode={mode}")
    for case in CASES:
        d, ans = case["d"], case["ans"]
        before = _legacy_summary(d, ans)
        after = summary(d, ans)
        rec = {
            "ticker": case["ticker"],
            "kind": case["kind"],
            "old_score_recorded": case.get("old_score"),
            "old_summary": before,
            "new_summary": after,
            "old_noul": None,
            "new_noul": None,
            "delta": None,
            "mode": mode,
        }
        if judge is not None:
            rec["old_noul"] = _score_with(judge, d, ans, before)
            rec["new_noul"] = _score_with(judge, d, ans, after)
            if rec["old_noul"] is not None and rec["new_noul"] is not None:
                rec["delta"] = round(rec["new_noul"] - rec["old_noul"], 4)
        rows.append(rec)
        print(f"\n{case['ticker']} ({case['kind']}, recorded {case.get('old_score')})")
        print(f"  before: {before}")
        print(f"  after:  {after}")
        if rec["delta"] is not None:
            print(f"  noul {rec['old_noul']} -> {rec['new_noul']}  delta={rec['delta']}")
    if mode == "summaries-only":
        print("\nNo LLM key in this environment. Re-run on the Mac:")
        print("  TYPESAFE_API_KEY=... python3 replay_judge_social.py --live")
    return rows


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--live", action="store_true", help="Call the live judge if a key is set")
    p.add_argument("--mock", action="store_true", help="Use mock_judge (CI / no key)")
    args = p.parse_args(argv)
    replay(live=args.live, use_mock=args.mock)
    return 0


if __name__ == "__main__":
    sys.exit(main())
