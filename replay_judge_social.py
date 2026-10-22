"""Replay pick-judge inputs: old summary vs social-tail summary.

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

# Five historical picks plus a couple of >=0.5 rejects. Missing facts stay
# None so the new summary says unknown instead of inventing zeros.
CASES = (
    {"ticker": "hellop", "kind": "pick", "old_score": 0.70,
     "d": {"chain": "solana", "age_minutes": 38, "mcap_usd": 210_000,
           "liquidity_usd": 42_000, "holder_count": 280, "ticker": "hellop",
           "tid": "hellop:1399811149"},
     "ans": {"shape": {"probabilities": {"crowd": 0.68}},
             "concentration_is_exit_risk": {"noul": 0.22},
             "authority_risk": {"choice": "renounced"}}},
    {"ticker": "CHILL", "kind": "pick", "old_score": 0.64,
     "d": {"chain": "solana", "age_minutes": 51, "mcap_usd": 180_000,
           "liquidity_usd": 36_000, "holder_count": 190, "ticker": "CHILL",
           "tid": "CHILL:1399811149"},
     "ans": {"shape": {"probabilities": {"crowd": 0.62}},
             "concentration_is_exit_risk": {"noul": 0.28},
             "authority_risk": {"choice": "renounced"}}},
    {"ticker": "HORN", "kind": "pick", "old_score": 0.72,
     "d": {"chain": "solana", "age_minutes": 44, "mcap_usd": 260_000,
           "liquidity_usd": 51_000, "holder_count": 744, "ticker": "HORN",
           "tid": "HornMint111111111111111111111111111111:1399811149",
           "developer_holding_percentage": 1.2},
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
           "x_account": {"followers": 40, "account_age_days": 4}},
     "ans": {"shape": {"probabilities": {"crowd": 0.66}},
             "concentration_is_exit_risk": {"noul": 0.24},
             "authority_risk": {"choice": "renounced"}}},
    {"ticker": "REJ1", "kind": "reject", "old_score": 0.54,
     "d": {"chain": "solana", "age_minutes": 70, "mcap_usd": 90_000,
           "liquidity_usd": 18_000, "holder_count": 95, "ticker": "REJ1",
           "tid": "Rej1:1399811149"},
     "ans": {"shape": {"probabilities": {"crowd": 0.51}},
             "concentration_is_exit_risk": {"noul": 0.40},
             "authority_risk": {"choice": "renounced"}}},
    {"ticker": "REJ2", "kind": "reject", "old_score": 0.58,
     "d": {"chain": "solana", "age_minutes": 55, "mcap_usd": 120_000,
           "liquidity_usd": 24_000, "holder_count": 130, "ticker": "REJ2",
           "tid": "Rej2:1399811149",
           "x_account": {"followers": 80, "account_age_days": 2}},
     "ans": {"shape": {"probabilities": {"crowd": 0.57}},
             "concentration_is_exit_risk": {"noul": 0.33},
             "authority_risk": {"choice": "renounced"},
             "recycled_account": {"noul": 0.45}}},
)


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
        old = _old_summary(d, ans)
        new = summary(d, ans)
        rec = {
            "ticker": case["ticker"],
            "kind": case["kind"],
            "old_score_recorded": case.get("old_score"),
            "old_summary": old,
            "new_summary": new,
            "old_noul": None,
            "new_noul": None,
            "delta": None,
            "mode": mode,
        }
        if judge is not None:
            rec["old_noul"] = _score_with(judge, d, ans, old)
            rec["new_noul"] = _score_with(judge, d, ans, new)
            if rec["old_noul"] is not None and rec["new_noul"] is not None:
                rec["delta"] = round(rec["new_noul"] - rec["old_noul"], 4)
        rows.append(rec)
        print(f"\n{case['ticker']} ({case['kind']}, recorded {case.get('old_score')})")
        print(f"  old: {old}")
        print(f"  new: {new}")
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
