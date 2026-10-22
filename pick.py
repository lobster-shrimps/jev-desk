"""
PICK — CHIEF runs it. The only call that ever sees more than one token at a time.

NEVER re-rank the winner. The choice settled it. If you disagree with the pick, you
  disagree with a threshold, and thresholds live in thresholds.py.
NEVER run pick on one survivor. A choice over a single option returns that option with
  high confidence, because it is the only thing there. (main.py handles the single case.)
NEVER skip worth_trading_at_all. A choice settles which of these. The noul settles
  whether today is a day at all, so the two always ship together.

No trade is a result. Log it with the reason, send it to Telegram, and stand down.
"""
import logging
from secret_utils import safe_err
from thresholds import PICK_MIN_WORTH, PICK_MIN_CONF, DARK_TICKET_CUT, NO_SOCIAL_CUT
from judge_inputs import format_flow

log = logging.getLogger(__name__)


def _fmt_unknown(value, *, kind="num") -> str:
    if value is None or value == "":
        return "unknown"
    if kind == "bool":
        if value in (True, 1, "1", "yes", "true"):
            return "yes"
        if value in (False, 0, "0", "no", "false"):
            return "no"
        return "unknown"
    if kind == "pct":
        try:
            return f"{float(value):.0f}%"
        except (TypeError, ValueError):
            return "unknown"
    if kind == "share":
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return "unknown"
    if kind == "noul":
        try:
            return f"{float(value):.2f}"
        except (TypeError, ValueError):
            return "unknown"
    try:
        if kind == "int":
            return str(int(float(value)))
        return str(value)
    except (TypeError, ValueError):
        return "unknown"


def _noul_of(ans, name):
    block = (ans or {}).get(name)
    if isinstance(block, dict):
        return block.get("noul")
    return None


def _followers_and_age(d: dict):
    xa = d.get("x_account") if isinstance(d.get("x_account"), dict) else {}
    followers = d.get("x_followers")
    if followers is None:
        followers = xa.get("followers")
        if followers is None:
            followers = xa.get("followers_count")
    age = xa.get("account_age_days")
    if age is None:
        age = d.get("account_age_days")
    return followers, age


def _rug_bits(d: dict) -> dict:
    feat = d.get("_rug_veto") if isinstance(d.get("_rug_veto"), dict) else {}
    if feat.get("sniper_share") is None and feat.get("bundled") is None:
        try:
            import desk_rugrisk
            tid = d.get("tid")
            for row in desk_rugrisk.all_rows():
                if row.get("tid") == tid:
                    feat = {
                        "sniper_share": row.get("sniper_share"),
                        "bundled": row.get("bundled"),
                        "sniper_wallets": row.get("sniper_wallets"),
                    }
                    break
        except Exception as e:
            log.warning("summary rugrisk: %s", safe_err(e))
    return feat


def _signal_bits(d: dict) -> dict:
    try:
        import young_trail_signals
        tid = d.get("tid")
        if not tid:
            return {}
        for row in young_trail_signals.all_rows():
            if row.get("tid") == tid:
                return row
    except Exception as e:
        log.warning("summary signals: %s", safe_err(e))
    return {}


def social_tail(d, ans) -> str:
    """Always-present extras. Missing values are 'unknown', never 0."""
    d = d if isinstance(d, dict) else {}
    ans = ans if isinstance(ans, dict) else {}
    followers, age = _followers_and_age(d)
    official = _noul_of(ans, "account_is_the_project")
    recycled = _noul_of(ans, "recycled_account")
    loaded = _noul_of(ans, "dev_still_loaded")
    rug = _rug_bits(d)
    sig = _signal_bits(d)
    sold = d.get("dev_sold_pct")
    holding = d.get("developer_holding_percentage")
    if holding is None:
        holding = d.get("dev_pct")
    promo = sig.get("paid_promo")
    if promo is None:
        promo = d.get("paid_promo")
    boost = sig.get("boost_amount")
    if boost is None:
        boost = d.get("boost_amount")
    lp = sig.get("lp_locked_pct")
    if lp is None:
        lp = d.get("lp_locked_pct")
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


def summary(d, ans) -> str:
    """Two lines per candidate, built from answers Jev already gave.
       Never the raw dossier. A fat state costs accuracy."""
    bits = [f"{d['chain']}, {d['age_minutes']:.0f}m old, ${d['mcap_usd']:,.0f} mcap, "
            f"${d['liquidity_usd']:,.0f} liq, {d['holder_count'] or '?'} holders",
            f"crowd {ans['shape']['probabilities']['crowd']:.2f}, "
            f"concentration risk {ans['concentration_is_exit_risk']['noul']:.2f}"]

    if "authority_risk" in ans:
        bits.append(f"authority {ans['authority_risk']['choice']}")
    if "sell_side_risk" in ans:
        bits.append(f"sell side {ans['sell_side_risk']['choice']}")
    if "data_coverage" in ans:
        bits.append(f"data {ans['data_coverage']['choice']}")
    if "account_is_the_project" in ans:
        bits.append(f"official account {ans['account_is_the_project']['noul']:.2f}, "
                    f"effort {ans['effort']['score']:.1f}")
    elif isinstance(d.get("x_account"), dict) and d["x_account"].get("x_kind") == "borrowed":
        bits.append("linked X is unrelated big account")
    else:
        bits.append("no usable X account")
    # Optional richer inputs. Omitted when missing so the prefix stays parseable.
    flow = d.get("flow") or format_flow(d)
    if flow:
        bits.append(flow)
    if d.get("holder_growth"):
        bits.append(d["holder_growth"])
    if d.get("dev_sells"):
        bits.append(d["dev_sells"])
    bits.append(social_tail(d, ans))
    return "; ".join(bits)


def size_factor_for(ans) -> float:
    size_factor = 1.0
    if ans.get("data_coverage", {}).get("choice") == "dark":
        size_factor *= DARK_TICKET_CUT   # less visibility, smaller ticket
    if "account_is_the_project" not in ans:
        size_factor *= NO_SOCIAL_CUT
    return round(size_factor, 2)


def pick(judge, survivors) -> dict | None:
    """survivors: [(dossier, answers), ...]. Returns the order, or None."""
    if not survivors:
        return None

    try:
        state = {"candidates": [{"ticker": d["ticker"], "summary": summary(d, a)}
                                for d, a in survivors]}
        r = judge("pick", state)
        best, worth = r["answers"]["best"], r["answers"]["worth_trading_at_all"]

        if worth["noul"] < PICK_MIN_WORTH:
            return None                      # every candidate is mediocre. Normal outcome.
        if best["confidence"] < PICK_MIN_CONF:
            return None                      # flat over ten options means no favourite.

        d, ans = next((x for x in survivors if x[0]["ticker"] == best["choice"]), (None, None))
        if d is None:
            return None                      # the schema guarantees the option is in the
                                             # list, so log this one and stand down.

        return {"model": r["model"],
                "token": {"ticker": d["ticker"], "address": d["addr"],
                          "network_id": d["net"], "chain": d["chain"]},
                "size_factor": size_factor_for(ans),
                "confidence": best["confidence"],
                "runner_up": sorted(best["probabilities"].items(),
                                    key=lambda kv: -kv[1])[1:2],
                "why": {k: v for k, v in ans.items()}}
    except Exception as e:
        log.warning("pick failed (NO PICK): %s", safe_err(e))
        return None                          # judge error means no pick this cycle
