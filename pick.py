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
