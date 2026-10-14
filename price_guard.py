"""Reject illiquid, wrong-mint, and unconfirmed-spike Dex marks.

Paper/analysis only. Used by young_trail, variants, fast, helius, and
research fast_marks. Never called from filter, judge, pick, or live orders.
Fail open: a rejected mark is skipped, never used as peak or exit.
"""
from __future__ import annotations

import logging

from secret_utils import safe_err

log = logging.getLogger("price_guard")

MIN_LIQ_USD = 1000.0
MAX_X_ENTRY = 20.0
MAX_X_PREV = 10.0
CONFIRM_TOL = 0.30


def _num(value, default=None):
    if value is None or value == "":
        return default
    try:
        n = float(value)
    except (TypeError, ValueError):
        return default
    return n


def mint_of(tid) -> str | None:
    if not tid:
        return None
    s = str(tid)
    if ":" not in s:
        return s
    addr, tail = s.rsplit(":", 1)
    try:
        int(tail)
        return addr
    except ValueError:
        return s


def addrs_equal(a, b) -> bool:
    if not a or not b:
        return False
    return str(a).lower() == str(b).lower()


def pair_liq_usd(pair) -> float:
    if not isinstance(pair, dict):
        return 0.0
    block = pair.get("liquidity")
    if isinstance(block, dict):
        return _num(block.get("usd"), 0.0) or 0.0
    return _num(block, 0.0) or 0.0


def pair_price_usd(pair) -> float | None:
    if not isinstance(pair, dict):
        return None
    for key in ("priceUsd", "price_usd", "price"):
        n = _num(pair.get(key))
        if n is not None and n > 0:
            return n
    return None


def pair_base_addr(pair) -> str | None:
    if not isinstance(pair, dict):
        return None
    base = pair.get("baseToken")
    if not isinstance(base, dict):
        return None
    addr = base.get("address")
    return str(addr) if addr else None


def pair_reject_reason(pair, mint: str | None) -> str | None:
    """Why this pair must not be used as a mark. None means it qualifies."""
    if not isinstance(pair, dict):
        return "not_a_pair"
    if mint and not addrs_equal(pair_base_addr(pair), mint):
        return "base_mint_mismatch"
    if pair_liq_usd(pair) < MIN_LIQ_USD:
        return "low_liq"
    if pair_price_usd(pair) is None:
        return "no_price"
    return None


def qualifying_pairs(pairs, mint: str | None) -> list[dict]:
    out = []
    if isinstance(pairs, dict):
        pairs = pairs.get("pairs") or pairs.get("data") or [pairs]
    if not isinstance(pairs, list):
        return out
    for p in pairs:
        if pair_reject_reason(p, mint) is None:
            out.append(p)
    return out


def is_spike(px: float, *, entry=None, prev=None) -> bool:
    try:
        px = float(px)
    except (TypeError, ValueError):
        return False
    if px <= 0:
        return False
    e = _num(entry)
    if e is not None and e > 0 and px > MAX_X_ENTRY * e:
        return True
    p = _num(prev)
    if p is not None and p > 0 and px > MAX_X_PREV * p:
        return True
    return False


def confirmed_by_second(px: float, others) -> bool:
    try:
        px = float(px)
    except (TypeError, ValueError):
        return False
    if px <= 0:
        return False
    for raw in others or []:
        o = _num(raw)
        if o is None or o <= 0:
            continue
        if abs(o - px) / px <= CONFIRM_TOL:
            return True
    return False


def accept_price(px, *, entry=None, prev=None, seconds=None, tid=None,
                 reason_prefix: str = "unconfirmed_spike") -> float | None:
    """Return px if usable, else log and return None. Never raises."""
    n = _num(px)
    if n is None or n <= 0:
        return None
    if is_spike(n, entry=entry, prev=prev):
        if not confirmed_by_second(n, seconds):
            try:
                log.info(
                    "price_guard skip tid=%s reason=%s px=%s entry=%s prev=%s",
                    tid, reason_prefix, n, entry, prev,
                )
            except Exception as e:
                log.warning("price_guard log: %s", safe_err(e))
            return None
    return n


def mark_from_pairs(pairs, mint: str | None, *, entry=None, prev=None,
                    tid=None) -> dict | None:
    """Highest-liq qualifying pair, with spike confirm from a second pair.

    Rejects liq < $1,000 and baseToken.address != mint. A >20x entry or
    >10x previous mark needs another qualifying pair within ±30%.
    """
    if isinstance(pairs, dict):
        raw = pairs.get("pairs") or pairs.get("data") or [pairs]
    else:
        raw = pairs
    if not isinstance(raw, list):
        raw = []
    quals = qualifying_pairs(raw, mint)
    if not quals:
        if raw:
            reasons = []
            for p in raw:
                r = pair_reject_reason(p, mint)
                if r:
                    reasons.append(r)
            reason = reasons[0] if reasons else "no_qualifying_pair"
            try:
                log.info(
                    "price_guard skip tid=%s reason=%s n_pairs=%d",
                    tid, reason, len(raw),
                )
            except Exception as e:
                log.warning("price_guard log: %s", safe_err(e))
        return None
    best = max(quals, key=pair_liq_usd)
    px = pair_price_usd(best)
    others = [pair_price_usd(p) for p in quals if p is not best]
    accepted = accept_price(px, entry=entry, prev=prev, seconds=others, tid=tid)
    if accepted is None:
        return None
    base = best.get("baseToken") if isinstance(best.get("baseToken"), dict) else {}
    vol = best.get("volume") if isinstance(best.get("volume"), dict) else {}
    return {
        "price_usd": accepted,
        "liq_usd": pair_liq_usd(best),
        "fdv": _num(best.get("fdv") or best.get("marketCap")),
        "vol_5m": _num(vol.get("m5")) if vol else None,
        "vol_1h": _num(vol.get("h1")) if vol else None,
        "vol_h24": _num(vol.get("h24")) if vol else None,
        "chain": best.get("chainId"),
        "ticker": base.get("symbol") if isinstance(base, dict) else None,
    }


def price_from_pairs(pairs, mint: str | None, *, entry=None, prev=None,
                     tid=None) -> float | None:
    mark = mark_from_pairs(pairs, mint, entry=entry, prev=prev, tid=tid)
    if not mark:
        return None
    return mark.get("price_usd")
