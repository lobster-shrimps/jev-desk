"""Replay pick-judge prompts: current main vs known-facts branch.

CI has no live LLM key. Run with --live on the Mac when JUDGE_URL /
TYPESAFE_API_KEY are set. Without a key this prints prompts and, if
requested, mock_judge score hashes (not real Jev scores).

--db PATH copies the live outcomes.db (or an outbox dir) to a temp file
and never writes the live DB. --tokens TICKER,TICKER rebuilds those
tokens both ways (main vs this branch) and can call the judge on each.
"""
from __future__ import annotations

import argparse
import json
import os
import pathlib
import shutil
import sys
import tempfile

from pick import (
    _clamp_share,
    _fmt_unknown,
    _followers_and_age,
    _noul_of,
    _rug_bits,
    _x_handle,
    account_is_the_project,
    render_pick_prompt,
    social_tail,
    summary,
)
from questions import PICK, PICK_INSTRUCTIONS  # PICK used for branch prompts
from secret_utils import safe_err

# Eleven tokens from the post-#96 judge replay. PEPA has no hardcoded
# follower count — real x_cache / dossier data is used, or the field is
# omitted. Missing facts stay None.
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
           "x_account": {"handle": "PEPA", "account_age_days": 4}},
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

MAIN_PICK_INSTRUCTIONS = (
    PICK_INSTRUCTIONS
    + " Small, new or recycled accounts with high sniper share are classic rug setups."
)


def _copy_sqlite(src: pathlib.Path, dest: pathlib.Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(src, dest)
    for suffix in ("-wal", "-shm"):
        extra = pathlib.Path(str(src) + suffix)
        if extra.is_file():
            shutil.copy2(extra, pathlib.Path(str(dest) + suffix))


def copy_live_db(src: str) -> tuple[pathlib.Path, pathlib.Path]:
    """Copy live outcomes.db (or an outbox dir) to a temp dir. Never write src."""
    src_path = pathlib.Path(src).expanduser()
    if not src_path.exists():
        raise FileNotFoundError(str(src_path))
    tmpdir = pathlib.Path(tempfile.mkdtemp(prefix="judge-known-facts-"))
    if src_path.is_dir():
        outcomes = src_path / "outcomes.db"
        dest = tmpdir / "outcomes.db"
        if outcomes.is_file():
            _copy_sqlite(outcomes, dest)
        xcache = src_path / "x_cache.db"
        if xcache.is_file():
            _copy_sqlite(xcache, tmpdir / "x_cache.db")
        return dest, tmpdir
    dest = tmpdir / src_path.name
    _copy_sqlite(src_path, dest)
    sibling = src_path.parent / "x_cache.db"
    if sibling.is_file():
        _copy_sqlite(sibling, tmpdir / "x_cache.db")
    return dest, tmpdir


def point_at_temp_db(dest: pathlib.Path, tmpdir: pathlib.Path) -> None:
    os.environ["OUTCOMES_DB"] = str(dest)
    os.environ["DESK_OUTBOX"] = str(tmpdir)
    xcopy = tmpdir / "x_cache.db"
    if xcopy.is_file():
        os.environ["X_CACHE_DB"] = str(xcopy)
    for name in (
        "desk_rugrisk", "young_trail_rugrisk", "young_trail_signals",
        "x_lookup", "rug_veto_shadow",
    ):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "reset"):
            try:
                mod.reset()
            except Exception as e:
                print(f"reset {name}: {safe_err(e)}")


def _main_social_tail(d, ans) -> str:
    """#98 / current-main tail: always-present, unknowns printed, sniper clamped."""
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
    try:
        from pick import _lp_boost, _signal_bits
        sig = _signal_bits(d)
        boost, lp = _lp_boost(d, sig)
        promo = sig.get("paid_promo")
        if promo is None:
            promo = d.get("paid_promo")
    except Exception as e:
        print(f"main tail extras: {safe_err(e)}")
        boost, lp, promo = None, None, d.get("paid_promo")
    if account_is_the_project(d, ans):
        follow_bit = (
            f"x followers {_fmt_unknown(followers, kind='int')}, "
            f"account age {_fmt_unknown(age, kind='int')}"
            + ("d" if age is not None else "")
        )
    elif followers is not None and followers != "":
        handle = _x_handle(d)
        handle_s = f"@{handle}" if handle else "linked x post"
        try:
            n_fol = int(float(followers))
        except (TypeError, ValueError):
            n_fol = followers
        follow_bit = (
            f"x linked account is not the project ({handle_s}, {n_fol} followers), "
            f"account age {_fmt_unknown(age, kind='int')}"
            + ("d" if age is not None else "")
        )
    else:
        follow_bit = (
            f"x followers unknown, "
            f"account age {_fmt_unknown(age, kind='int')}"
            + ("d" if age is not None else "")
        )
    return "; ".join([
        follow_bit,
        f"official {_fmt_unknown(official, kind='noul')}, "
        f"recycled {_fmt_unknown(recycled, kind='noul')}, "
        f"dev_loaded {_fmt_unknown(loaded, kind='noul')}",
        f"sniper {_fmt_unknown(_clamp_share(rug.get('sniper_share')), kind='share')}, "
        f"bundled {_fmt_unknown(rug.get('bundled'), kind='bool')}, "
        f"snipers {_fmt_unknown(rug.get('sniper_wallets'), kind='int')}",
        f"dev holding {_fmt_unknown(holding, kind='pct')}, "
        f"dev sold {_fmt_unknown(sold, kind='pct')}",
        f"paid promo {_fmt_unknown(promo, kind='bool')}, "
        f"dex boost {_fmt_unknown(boost, kind='share')}, "
        f"lp {_fmt_unknown(lp, kind='pct')}",
    ])


def _prefix(d, ans) -> str:
    full = summary(d, ans)
    tail = social_tail(d, ans)
    if tail and full.endswith("; " + tail):
        return full[: -(len(tail) + 2)]
    return full


def main_summary(d, ans) -> str:
    """summary() as current main (#98) renders it."""
    return _prefix(d, ans) + "; " + _main_social_tail(d, ans)


def main_prompt(d, ans) -> str:
    return f"{MAIN_PICK_INSTRUCTIONS}\n{d['ticker']}: {main_summary(d, ans)}"


def branch_prompt(d, ans) -> str:
    return render_pick_prompt([(d, ans)])


def branch_instructions(has_facts: bool) -> str:
    qs = PICK({"candidates": [{"ticker": "T", "summary": "s"}], "has_facts": has_facts})
    return getattr(qs["best"], "instructions", "") or ""


def _loads(raw):
    if isinstance(raw, dict):
        return raw
    if not raw:
        return {}
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


def _overlay_row(d: dict, row: dict | None) -> dict:
    if not row:
        return d
    out = dict(d)
    for key in ("tid", "chain", "ticker"):
        if row.get(key):
            out[key] = row[key]
    rug = dict(out.get("_rug_veto") or {})
    for key in ("sniper_share", "bundled", "sniper_wallets"):
        if row.get(key) is not None:
            rug[key] = row[key]
    feat = _loads(row.get("features_json"))
    for key in ("sniper_share", "bundled", "sniper_wallets"):
        if feat.get(key) is not None and rug.get(key) is None:
            rug[key] = feat[key]
    if rug:
        out["_rug_veto"] = rug
    if feat.get("x_account"):
        out["x_account"] = feat["x_account"]
    if feat.get("followers") is not None:
        out.setdefault("x_followers", feat["followers"])
    for key in (
        "boost_amount", "lp_locked_pct", "paid_promo",
        "developer_holding_percentage", "dev_sold_pct", "dev_pct",
        "x_handle", "x_followers",
    ):
        if feat.get(key) is not None:
            out[key] = feat[key]
        elif row.get(key) is not None:
            out[key] = row[key]
    return out


def _rows_for_ticker(ticker: str) -> list[dict]:
    hits = []
    key = (ticker or "").strip().lower()
    if not key:
        return hits
    for name in ("desk_rugrisk", "young_trail_rugrisk"):
        try:
            mod = __import__(name)
            for row in mod.all_rows():
                if str(row.get("ticker") or "").strip().lower() == key:
                    hits.append(row)
        except Exception as e:
            print(f"{name} lookup: {safe_err(e)}")
    return hits


def _case_by_ticker(ticker: str) -> dict | None:
    key = (ticker or "").strip().lower()
    for case in CASES:
        if case["ticker"].lower() == key:
            return {
                "ticker": case["ticker"],
                "kind": case.get("kind"),
                "old_score": case.get("old_score"),
                "d": dict(case["d"]),
                "ans": dict(case["ans"]),
            }
    return None


def _default_case(ticker: str) -> dict:
    return {
        "ticker": ticker,
        "kind": "db",
        "old_score": None,
        "d": {
            "chain": "solana", "age_minutes": 40, "mcap_usd": 200_000,
            "liquidity_usd": 40_000, "holder_count": 200, "ticker": ticker,
            "tid": ticker,
        },
        "ans": {
            "shape": {"probabilities": {"crowd": 0.60}},
            "concentration_is_exit_risk": {"noul": 0.25},
            "authority_risk": {"choice": "renounced"},
        },
    }


def resolve_case(ticker: str, *, use_db: bool) -> dict | None:
    case = _case_by_ticker(ticker)
    rows = _rows_for_ticker(ticker) if use_db else []
    if case is None and not rows:
        return None
    if case is None:
        case = _default_case(ticker)
    if use_db:
        for row in reversed(rows):
            case["d"] = _overlay_row(case["d"], row)
    # Drop PEPA's hardcoded 40 if a leftover fixture still carries it.
    xa = case["d"].get("x_account")
    if (
        str(case["d"].get("ticker") or "").upper() == "PEPA"
        and isinstance(xa, dict)
        and xa.get("followers") == 40
        and not use_db
    ):
        xa = dict(xa)
        xa.pop("followers", None)
        case["d"]["x_account"] = xa
    return case


def _live_available() -> bool:
    return bool(os.environ.get("TYPESAFE_API_KEY") or os.environ.get("JUDGE_URL"))


def _score_with(judge, d, ans, text) -> float | None:
    try:
        has_facts = bool(social_tail(d, ans)) if text == summary(d, ans) else True
        state = {
            "candidates": [{"ticker": d["ticker"], "summary": text}],
            "has_facts": has_facts,
        }
        r = judge("pick", state)
        return float(((r.get("answers") or {}).get("worth_trading_at_all") or {}).get("noul"))
    except Exception as e:
        print(f"  judge failed: {safe_err(e)}")
        return None


def _parse_tokens(raw) -> list[str]:
    if not raw:
        return []
    if isinstance(raw, (list, tuple)):
        parts = []
        for item in raw:
            parts.extend(str(item).split(","))
        return [p.strip() for p in parts if p.strip()]
    return [p.strip() for p in str(raw).split(",") if p.strip()]


def _saved_db_env() -> dict[str, str | None]:
    return {key: os.environ.get(key) for key in (
        "OUTCOMES_DB", "DESK_OUTBOX", "X_CACHE_DB",
    )}


def _restore_db_env(saved: dict[str, str | None]) -> None:
    for key, val in saved.items():
        if val is None:
            os.environ.pop(key, None)
        else:
            os.environ[key] = val
    for name in (
        "desk_rugrisk", "young_trail_rugrisk", "young_trail_signals",
        "x_lookup", "rug_veto_shadow",
    ):
        mod = sys.modules.get(name)
        if mod is not None and hasattr(mod, "reset"):
            try:
                mod.reset()
            except Exception as e:
                print(f"restore {name}: {safe_err(e)}")


def replay(*, live: bool = False, use_mock: bool = False,
           db_path: str | None = None, tokens=None) -> list[dict]:
    wanted = _parse_tokens(tokens)
    use_db = bool(db_path)
    saved_env = _saved_db_env() if use_db else None
    tmpdir = None
    if use_db:
        try:
            dest, tmpdir = copy_live_db(db_path)
            point_at_temp_db(dest, tmpdir)
        except Exception as e:
            print(f"db copy failed: {safe_err(e)}")
            if saved_env is not None:
                _restore_db_env(saved_env)
            return []

    try:
        if wanted:
            cases = []
            for ticker in wanted:
                rec = resolve_case(ticker, use_db=use_db)
                if rec is None:
                    print(f"token not found: {ticker}")
                    continue
                cases.append(rec)
        else:
            cases = [
                {"ticker": c["ticker"], "kind": c.get("kind"),
                 "old_score": c.get("old_score"), "d": dict(c["d"]),
                 "ans": dict(c["ans"])}
                for c in CASES
            ]
            if use_db:
                cases = [resolve_case(c["ticker"], use_db=True) or c for c in cases]

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
        print(f"judge-social replay mode={mode} tokens={len(cases)}")
        for case in cases:
            d, ans = case["d"], case["ans"]
            main_text = main_summary(d, ans)
            branch_text = summary(d, ans)
            main_p = main_prompt(d, ans)
            branch_p = branch_prompt(d, ans)
            rec = {
                "ticker": case["ticker"],
                "kind": case.get("kind"),
                "old_score_recorded": case.get("old_score"),
                "old_summary": main_text,
                "new_summary": branch_text,
                "main_summary": main_text,
                "branch_summary": branch_text,
                "main_prompt": main_p,
                "branch_prompt": branch_p,
                "main_facts": _main_social_tail(d, ans),
                "branch_facts": social_tail(d, ans),
                "old_noul": None,
                "new_noul": None,
                "main_noul": None,
                "branch_noul": None,
                "delta": None,
                "mode": mode,
            }
            if judge is not None:
                rec["main_noul"] = _score_with(judge, d, ans, main_text)
                rec["branch_noul"] = _score_with(judge, d, ans, branch_text)
                rec["old_noul"] = rec["main_noul"]
                rec["new_noul"] = rec["branch_noul"]
                if rec["main_noul"] is not None and rec["branch_noul"] is not None:
                    rec["delta"] = round(rec["branch_noul"] - rec["main_noul"], 4)
            rows.append(rec)
            print(f"\n{case['ticker']} ({case.get('kind')}, recorded {case.get('old_score')})")
            print(f"  main facts:   {rec['main_facts'] or '(none)'}")
            print(f"  branch facts: {rec['branch_facts'] or '(none)'}")
            print(f"  main summary:   {main_text}")
            print(f"  branch summary: {branch_text}")
            print(f"  main prompt:    {main_p}")
            print(f"  branch prompt:  {branch_p}")
            if rec["delta"] is not None:
                print(f"  noul {rec['main_noul']} -> {rec['branch_noul']}  delta={rec['delta']}")
        if mode == "summaries-only":
            print("\nNo LLM key in this environment. Re-run on the Mac:")
            print("  python3 replay_judge_social.py --db /path/to/outcomes.db "
                  "--tokens FOD,PROWLER,T0 --live")
        return rows
    except Exception as e:
        print(f"replay failed: {safe_err(e)}")
        return []
    finally:
        if saved_env is not None:
            _restore_db_env(saved_env)


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--live", action="store_true", help="Call the live judge if a key is set")
    p.add_argument("--mock", action="store_true", help="Use mock_judge (CI / no key)")
    p.add_argument("--db", help="Live outcomes.db or outbox dir. Copied to temp; never written.")
    p.add_argument("--tokens", help="Comma-separated tickers to rebuild both ways")
    args = p.parse_args(argv)
    replay(live=args.live, use_mock=args.mock, db_path=args.db, tokens=args.tokens)
    return 0


if __name__ == "__main__":
    sys.exit(main())
