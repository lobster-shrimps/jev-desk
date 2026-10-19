"""Optional richer judge inputs: flow, holder growth, keyless dev-sells.

Attached onto the token dossier before the market seat and pick.summary().
Each field is omitted when the data is missing (fail open). RPC exceptions go
through safe_err. SOLANA_RPC_URL is never logged.
"""
from __future__ import annotations

import logging
import os
import re
import time

import requests

from secret_utils import safe_err

log = logging.getLogger("judge_inputs")

HOLDER_GROWTH_MIN_AGE_MIN = 5
HOLDER_GROWTH_MAX_AGE_MIN = 120
DEV_SELLS_CAP = 5
DEV_SELLS_TIMEOUT_SEC = 8.0
DEV_SELLS_SIG_LIMIT = 20
MAX_TX_VERSION = 1
MAX_TX_VERSION_CAP = 2
_TX_VERSION_RE = re.compile(
    r"Transaction version \((\d+)\) is not supported", re.IGNORECASE
)

_lookups_this_cycle = 0
_time_fn = time.time
_rpc_fn = None  # tests inject: fn(method, params, timeout) -> result


def set_time_fn(fn):
    global _time_fn
    _time_fn = time.time if fn is None else fn


def set_rpc_fn(fn):
    """Inject Solana RPC. Pass None to restore requests.post."""
    global _rpc_fn
    _rpc_fn = fn


def current_time() -> float:
    return float(_time_fn())


def reset_cycle() -> None:
    global _lookups_this_cycle
    _lookups_this_cycle = 0


def cycle_lookups() -> int:
    return _lookups_this_cycle


def _num(value):
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _int(value):
    n = _num(value)
    if n is None:
        return None
    return int(n)


def format_flow(d: dict) -> str | None:
    """'flow 1h b/s {buys_h1}/{sells_h1}, 5m b/s ..' or None if 1h is missing."""
    buys_h1 = _int(d.get("buys_h1"))
    sells_h1 = _int(d.get("sells_h1"))
    if buys_h1 is None or sells_h1 is None:
        return None
    bit = f"flow 1h b/s {buys_h1}/{sells_h1}"
    buys_m5 = _int(d.get("buys_m5"))
    sells_m5 = _int(d.get("sells_m5"))
    if buys_m5 is not None and sells_m5 is not None:
        bit += f", 5m b/s {buys_m5}/{sells_m5}"
    return bit


def format_holder_growth(current, previous, minutes) -> str | None:
    """'holders +N in Mm (+P%/h)' or None if the arithmetic cannot be done."""
    cur = _int(current)
    prev = _int(previous)
    mins = _num(minutes)
    if cur is None or prev is None or mins is None:
        return None
    if mins < HOLDER_GROWTH_MIN_AGE_MIN or mins > HOLDER_GROWTH_MAX_AGE_MIN:
        return None
    if prev <= 0:
        return None
    delta = cur - prev
    m_disp = max(1, int(round(mins)))
    pct_per_hour = (delta / prev) * (60.0 / mins) * 100.0
    return f"holders {delta:+d} in {m_disp}m ({_fmt_pct_per_hour(pct_per_hour)})"


def _fmt_pct_per_hour(p: float) -> str:
    if abs(p - round(p)) < 0.05:
        return f"{p:+.0f}%/h"
    return f"{p:+.1f}%/h"


def _fmt_sold_pct(p: float) -> str:
    if p >= 1 or abs(p - round(p)) < 0.05:
        return f"{p:.0f}"
    return f"{p:.1f}"


def previous_holder_sample(d: dict, now: float | None = None) -> tuple[int, float] | None:
    """Previous holder_count for this tid, 5–120 min old. Fail open."""
    now = current_time() if now is None else float(now)
    tid = d.get("tid")
    sample = None
    if tid:
        try:
            import cycle_history
            sample = cycle_history.previous_holder_sample(tid, now=now)
        except Exception as e:
            log.warning("holder growth history: %s", safe_err(e))
            sample = None
    if sample is None:
        try:
            import gt_info_cache
            chain = d.get("chain")
            addr = d.get("addr")
            prev = gt_info_cache.get_previous(chain, addr, now=now) if chain and addr else None
            if prev:
                payload, fetched_at = prev
                count = gt_info_cache.holder_count_of(payload)
                age_min = (now - float(fetched_at)) / 60.0
                if count is not None and HOLDER_GROWTH_MIN_AGE_MIN <= age_min <= HOLDER_GROWTH_MAX_AGE_MIN:
                    sample = {"holder_count": count, "ts": fetched_at}
        except Exception as e:
            log.warning("holder growth cache: %s", safe_err(e))
    if not sample:
        return None
    count = _int(sample.get("holder_count"))
    ts = _num(sample.get("ts"))
    if count is None or ts is None:
        return None
    age_min = (now - ts) / 60.0
    if age_min < HOLDER_GROWTH_MIN_AGE_MIN or age_min > HOLDER_GROWTH_MAX_AGE_MIN:
        return None
    return count, age_min


def holder_growth_bit(d: dict, now: float | None = None) -> str | None:
    sample = previous_holder_sample(d, now=now)
    if sample is None:
        return None
    prev, minutes = sample
    return format_holder_growth(d.get("holder_count"), prev, minutes)


def _token_amount(entry: dict) -> int | None:
    if not isinstance(entry, dict):
        return None
    ui = entry.get("uiTokenAmount") or {}
    raw = ui.get("amount") if isinstance(ui, dict) else None
    n = _int(raw)
    if n is not None:
        return n
    if isinstance(ui, dict) and ui.get("uiAmount") is not None:
        try:
            return int(float(ui["uiAmount"]))
        except (TypeError, ValueError):
            return None
    return None


def _balance_for(entries, mint: str, owner: str) -> int | None:
    mint_l = (mint or "").strip()
    owner_l = (owner or "").strip()
    if not mint_l or not owner_l:
        return None
    for entry in entries or []:
        if not isinstance(entry, dict):
            continue
        if str(entry.get("mint") or "") != mint_l:
            continue
        if str(entry.get("owner") or "") != owner_l:
            continue
        amt = _token_amount(entry)
        if amt is not None:
            return amt
    return None


def _clamp_tx_version(version) -> int:
    try:
        ver = int(version)
    except (TypeError, ValueError):
        ver = MAX_TX_VERSION
    return min(max(0, ver), MAX_TX_VERSION_CAP)


def _with_max_tx_version(method: str, params, version: int | None = None):
    if method not in ("getTransaction", "getBlock", "getTransactionsForAddress"):
        return params
    params = list(params or [])
    if not params:
        return params
    ver = MAX_TX_VERSION if version is None else _clamp_tx_version(version)
    if len(params) < 2 or not isinstance(params[1], dict):
        if len(params) == 1:
            params.append({"maxSupportedTransactionVersion": ver})
        return params
    cfg = dict(params[1])
    cfg["maxSupportedTransactionVersion"] = ver
    params[1] = cfg
    return params


def _tx_version_from_error(err) -> int | None:
    if err is None:
        return None
    if isinstance(err, dict):
        msg = str(err.get("message") or err)
    else:
        msg = str(err)
    m = _TX_VERSION_RE.search(msg)
    if not m:
        return None
    return _clamp_tx_version(m.group(1))


def _rpc_raw(method: str, params, timeout: float):
    if _rpc_fn is not None:
        return _rpc_fn(method, params, timeout)
    rpc_url = os.environ.get("SOLANA_RPC_URL", "https://api.mainnet-beta.solana.com")
    resp = requests.post(
        rpc_url,
        json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
        timeout=max(0.5, float(timeout)),
    )
    if getattr(resp, "status_code", 200) >= 500:
        raise RuntimeError(f"HTTP {resp.status_code}")
    data = resp.json()
    if not isinstance(data, dict):
        raise RuntimeError("solana rpc: non-object response")
    if "error" in data:
        err = data["error"]
        if isinstance(err, dict):
            raise RuntimeError(err.get("message") or "solana rpc error")
        raise RuntimeError(str(err))
    if "result" not in data:
        raise RuntimeError("solana rpc: missing result")
    return data["result"]


def _rpc(method: str, params, timeout: float):
    params = _with_max_tx_version(method, params)
    try:
        return _rpc_raw(method, params, timeout)
    except Exception as e:
        ver = _tx_version_from_error(e)
        if ver is None:
            raise
        retry_params = _with_max_tx_version(method, params, version=ver)
        return _rpc_raw(method, retry_params, timeout)


def _dev_sells_from_parsed(txs: list, mint: str, creator: str) -> str | None:
    """Compute sold-of-initial from parsed txs (oldest first)."""
    initial = None
    last = None
    sold = 0
    saw_mint = False
    for tx in txs:
        if not isinstance(tx, dict):
            continue
        meta = tx.get("meta") or {}
        pre = _balance_for(meta.get("preTokenBalances"), mint, creator)
        post = _balance_for(meta.get("postTokenBalances"), mint, creator)
        if pre is None and post is None:
            continue
        saw_mint = True
        if initial is None:
            initial = pre if pre is not None else post
        if pre is not None and post is not None and post < pre:
            sold += pre - post
        if post is not None:
            last = post
        elif pre is not None:
            last = pre
    if not saw_mint:
        return "dev sells: none seen"
    if initial is None or initial <= 0:
        return None
    if sold <= 0 and (last is None or last >= initial):
        return "dev sells: none seen"
    if sold <= 0:
        sold = max(0, initial - (last if last is not None else initial))
    if sold <= 0:
        return "dev sells: none seen"
    pct = sold / initial * 100.0
    return f"dev sold {_fmt_sold_pct(pct)}% of initial since launch"


def solana_dev_sells(d: dict) -> str | None:
    """Keyless Solana creator sell trail. Cap 5/cycle, 8s each. Fail open."""
    global _lookups_this_cycle
    creator = d.get("creator")
    mint = d.get("addr")
    if not creator or not mint:
        return None
    if _lookups_this_cycle >= DEV_SELLS_CAP:
        return None
    _lookups_this_cycle += 1
    deadline = current_time() + DEV_SELLS_TIMEOUT_SEC
    ticker = d.get("ticker") or mint
    try:
        remaining = deadline - current_time()
        if remaining <= 0:
            return None
        sigs = _rpc(
            "getSignaturesForAddress",
            [creator, {"limit": DEV_SELLS_SIG_LIMIT}],
            remaining,
        )
        if not sigs:
            return "dev sells: none seen"
        if not isinstance(sigs, list):
            return None
        signatures = [s.get("signature") for s in sigs if isinstance(s, dict) and s.get("signature")]
        if not signatures:
            return "dev sells: none seen"
        # Oldest first so the first pre-balance we see is the closest to launch.
        signatures.reverse()
        parsed = []
        for sig in signatures:
            remaining = deadline - current_time()
            if remaining <= 0.4:
                break
            try:
                tx = _rpc(
                    "getTransaction",
                    [sig, {"encoding": "jsonParsed", "maxSupportedTransactionVersion": MAX_TX_VERSION}],
                    remaining,
                )
            except Exception as e:
                log.warning("dev sells tx parse failed for %s: %s", ticker, safe_err(e))
                continue
            if tx:
                parsed.append(tx)
        if not parsed:
            return None
        return _dev_sells_from_parsed(parsed, mint, creator)
    except Exception as e:
        log.warning("dev sells lookup failed for %s: %s", ticker, safe_err(e))
        return None


def evm_dev_sells(d: dict) -> str | None:
    """EVM: no extra RPC. Only if honeypot holder data already named the creator.

    Without a sell trail we cannot invent a percentage, so the field is omitted
    unless the already-fetched holder row itself shows a sell vs initial.
    """
    if not d.get("creator"):
        return None
    initial = _num(d.get("creator_initial_balance") or d.get("dev_initial_balance"))
    current = _num(d.get("creator_balance") or d.get("dev_balance"))
    if initial is None or initial <= 0 or current is None:
        return None
    if current >= initial:
        return "dev sells: none seen"
    pct = (initial - current) / initial * 100.0
    return f"dev sold {_fmt_sold_pct(pct)}% of initial since launch"


def dev_sells_bit(d: dict) -> str | None:
    net = d.get("net")
    chain = (d.get("chain") or "").lower()
    is_solana = chain == "solana" or net == 1399811149
    if is_solana:
        return solana_dev_sells(d)
    if chain in ("bsc", "base") or net in (56, 8453):
        return evm_dev_sells(d)
    return None


def attach(d: dict, now: float | None = None) -> dict:
    """Stash optional flow / holder_growth / dev_sells on d. Mutates and returns d."""
    if not isinstance(d, dict):
        return d
    try:
        flow = format_flow(d)
        if flow:
            d["flow"] = flow
    except Exception as e:
        log.warning("flow bit failed: %s", safe_err(e))
    try:
        growth = holder_growth_bit(d, now=now)
        if growth:
            d["holder_growth"] = growth
    except Exception as e:
        log.warning("holder growth bit failed: %s", safe_err(e))
    try:
        sells = dev_sells_bit(d)
        if sells:
            d["dev_sells"] = sells
    except Exception as e:
        log.warning("dev sells bit failed: %s", safe_err(e))
    return d
