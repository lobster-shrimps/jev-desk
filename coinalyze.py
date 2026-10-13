"""
LOG-ONLY market-regime capture from the free Coinalyze API.

This module never feeds filter, soft_kill, pick, or thresholds. A missing key,
timeout, 429, or any other error skips the fetch with one INFO line and cannot
fail a cycle. The API key is read from COINALYZE_API_KEY and is never logged.

Each capture uses about 5–10 HTTP calls (future-markets only when the symbol
cache is cold, then one batched call each for funding, predicted funding,
open-interest history, liquidations, long/short ratio). At most once per
15 minutes. Per-request timeout is short; an overall deadline aborts leftover
calls. Exceptions go through safe_err.

Regime tags (first match wins) — documented so a later reader can reproduce them:

  long_flush  long-liquidation USD share across the basket >= 0.70
              and total long+short liquidations > 0
  risk_on     median majors funding > 0
              AND median meme funding > 0
              AND median open-interest change > 0
  risk_off    median majors funding < 0
              OR (median OI change < 0 AND median meme funding < 0)
  neutral     anything else, including incomplete data that misses 1–3

Majors: BTC, SOL, BNB. Meme basket: WIF, BONK, POPCAT. Symbols are resolved
from GET /future-markets (USDT stable-margined perps, Binance preferred) and
cached.
"""
from __future__ import annotations

import logging
import os
import time
from typing import Callable

import requests

from secret_utils import safe_err

log = logging.getLogger("coinalyze")

BASE_URL = "https://api.coinalyze.net/v1"
WATCH_MAJORS = ("BTC", "SOL", "BNB")
WATCH_MEMES = ("WIF", "BONK", "POPCAT")
WATCH = WATCH_MAJORS + WATCH_MEMES
MIN_INTERVAL_SEC = 900
HTTP_TIMEOUT_SEC = 2.5
OVERALL_TIMEOUT_SEC = 10.0
MAX_HTTP_CALLS = 10
HISTORY_LOOKBACK_SEC = 4 * 3600
HISTORY_INTERVAL = "1hour"
SYMBOL_CACHE_TTL_SEC = 86400
LONG_FLUSH_SHARE = 0.70
REGIME_RULES = (
    "first match: long_flush if long-liq USD share >= 0.70 and total liq > 0; "
    "risk_on if median majors funding > 0 and median meme funding > 0 and "
    "median OI change > 0; risk_off if median majors funding < 0 or "
    "(median OI change < 0 and median meme funding < 0); else neutral"
)

_time_fn: Callable[[], float] = time.time
_get_fn = None
_last_attempt_ts: float | None = None
_memory_cache: dict | None = None
_last_snap: dict | None = None


def set_time_fn(fn: Callable[[], float] | None) -> None:
    """Inject a clock for deterministic tests. Pass None to restore time.time."""
    global _time_fn
    _time_fn = time.time if fn is None else fn


def set_get_fn(fn) -> None:
    """Inject a requests.get stand-in. Pass None to restore requests.get."""
    global _get_fn
    _get_fn = fn


def current_time() -> float:
    return float(_time_fn())


def reset(*, hooks: bool = True) -> None:
    """Clear cooldown and in-memory symbol cache. Tests call this between cases.

    hooks=False keeps injected get_fn/time_fn so cycle_history.reset() does not
    undo a test double.
    """
    global _last_attempt_ts, _memory_cache, _time_fn, _get_fn, _last_snap
    _last_attempt_ts = None
    _memory_cache = None
    _last_snap = None
    if hooks:
        _time_fn = time.time
        _get_fn = None


def last_snapshot() -> dict | None:
    """Most recent successful maybe_capture payload, or None. No HTTP."""
    return dict(_last_snap) if isinstance(_last_snap, dict) else None


def last_regime() -> str | None:
    """Regime tag from the last successful capture. No HTTP."""
    snap = last_snapshot()
    if not snap:
        return None
    tag = snap.get("regime")
    return str(tag) if tag else None


def api_key() -> str | None:
    raw = os.environ.get("COINALYZE_API_KEY")
    if raw is None:
        return None
    key = raw.strip()
    return key or None


def _scrub(text: str) -> str:
    """Replace the live key value if it ever appears in an error string."""
    key = api_key()
    if key and key in text:
        text = text.replace(key, "REDACTED")
    return text


def _skip(reason: str) -> None:
    log.info("coinalyze: skipped (%s)", _scrub(reason))


def _median(values: list[float | None]) -> float | None:
    nums = [float(v) for v in values if v is not None]
    if not nums:
        return None
    nums.sort()
    n = len(nums)
    mid = n // 2
    if n % 2:
        return nums[mid]
    return (nums[mid - 1] + nums[mid]) / 2.0


def _num(value):
    if value is None or value == "":
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _score_market(market: dict, ticker: str) -> int:
    base = str(market.get("base_asset") or "").upper()
    if base == ticker:
        score = 100
    elif ticker and ticker in base:
        score = 40
    else:
        return -1
    if market.get("is_perpetual"):
        score += 50
    quote = str(market.get("quote_asset") or "").upper()
    if quote == "USDT":
        score += 20
    elif quote == "USD":
        score += 10
    if str(market.get("margined") or "").upper() == "STABLE":
        score += 10
    if str(market.get("exchange") or "") == "A":
        score += 15
    if market.get("has_long_short_ratio_data"):
        score += 8
    return score


def resolve_symbols(markets) -> dict:
    """Pick one Coinalyze perp symbol per watched ticker from future-markets rows."""
    if not isinstance(markets, list):
        return {}
    out = {}
    for ticker in WATCH:
        best = None
        best_score = -1
        for market in markets:
            if not isinstance(market, dict):
                continue
            score = _score_market(market, ticker)
            if score > best_score:
                best_score = score
                best = market
        if best and best.get("symbol"):
            out[ticker] = {
                "symbol": str(best["symbol"]),
                "exchange": best.get("exchange"),
                "resolved_at": current_time(),
            }
    return out


def _cache_fresh(cache: dict | None, now: float) -> bool:
    if not cache:
        return False
    for ticker in WATCH:
        row = cache.get(ticker)
        if not isinstance(row, dict) or not row.get("symbol"):
            return False
        resolved = _num(row.get("resolved_at"))
        if resolved is None or now - resolved > SYMBOL_CACHE_TTL_SEC:
            return False
    return True


def _symbol_map(cache: dict | None) -> dict[str, str]:
    out = {}
    for ticker, row in (cache or {}).items():
        if isinstance(row, dict) and row.get("symbol"):
            out[str(ticker)] = str(row["symbol"])
    return out


def _by_symbol(rows) -> dict:
    out = {}
    if not isinstance(rows, list):
        return out
    for row in rows:
        if isinstance(row, dict) and row.get("symbol"):
            out[str(row["symbol"])] = row
    return out


def _oi_change_pct(history) -> float | None:
    if not isinstance(history, list) or not history:
        return None
    first = _num(history[0].get("c") if isinstance(history[0], dict) else None)
    last = _num(history[-1].get("c") if isinstance(history[-1], dict) else None)
    if first in (None, 0) or last is None:
        return None
    return (last - first) / first


def _sum_liq(history) -> tuple[float, float]:
    long_usd = 0.0
    short_usd = 0.0
    if not isinstance(history, list):
        return long_usd, short_usd
    for candle in history:
        if not isinstance(candle, dict):
            continue
        long_usd += _num(candle.get("l")) or 0.0
        short_usd += _num(candle.get("s")) or 0.0
    return long_usd, short_usd


def _latest_lsr(history) -> dict:
    empty = {"ratio": None, "long_pct": None, "short_pct": None}
    if not isinstance(history, list) or not history:
        return empty
    last = history[-1]
    if not isinstance(last, dict):
        return empty
    return {
        "ratio": _num(last.get("r")),
        "long_pct": _num(last.get("l")),
        "short_pct": _num(last.get("s")),
    }


def derive_regime(metrics: dict) -> tuple[str, str]:
    """Return (tag, reason) from already-aggregated metrics. No I/O."""
    agg = (metrics or {}).get("aggregates") or {}
    long_share = _num(agg.get("long_liq_share"))
    long_usd = _num(agg.get("long_liq_usd")) or 0.0
    short_usd = _num(agg.get("short_liq_usd")) or 0.0
    majors_fr = _num(agg.get("majors_funding_median"))
    memes_fr = _num(agg.get("memes_funding_median"))
    oi = _num(agg.get("oi_change_median"))

    if long_share is not None and (long_usd + short_usd) > 0 and long_share >= LONG_FLUSH_SHARE:
        return "long_flush", f"long-liq share {long_share:.2f} >= {LONG_FLUSH_SHARE:.2f}"
    if (
        majors_fr is not None and majors_fr > 0
        and memes_fr is not None and memes_fr > 0
        and oi is not None and oi > 0
    ):
        return "risk_on", "majors and meme funding > 0 and OI expanding"
    if majors_fr is not None and majors_fr < 0:
        return "risk_off", "median majors funding < 0"
    if oi is not None and oi < 0 and memes_fr is not None and memes_fr < 0:
        return "risk_off", "OI contracting and meme funding < 0"
    return "neutral", "no long_flush / risk_on / risk_off rule matched"


def _http_get(path: str, *, key: str, params: dict | None, deadline: float) -> object:
    remaining = deadline - current_time()
    if remaining <= 0.05:
        raise TimeoutError("coinalyze overall deadline")
    timeout = min(HTTP_TIMEOUT_SEC, max(0.1, remaining))
    url = f"{BASE_URL}{path}"
    headers = {"api_key": key, "Accept": "application/json"}
    get = _get_fn or requests.get
    resp = get(url, headers=headers, params=params or {}, timeout=timeout)
    status = getattr(resp, "status_code", None)
    if status == 429:
        raise RuntimeError("coinalyze HTTP 429")
    if status is not None and status >= 400:
        raise RuntimeError(f"coinalyze HTTP {status}")
    raise_for_status = getattr(resp, "raise_for_status", None)
    if callable(raise_for_status):
        raise_for_status()
    return resp.json()


def _build_assets(symbols: dict[str, str], raw: dict) -> dict:
    funding = _by_symbol(raw.get("funding"))
    predicted = _by_symbol(raw.get("predicted_funding"))
    oi_hist = _by_symbol(raw.get("open_interest_history"))
    liq_hist = _by_symbol(raw.get("liquidation_history"))
    lsr_hist = _by_symbol(raw.get("long_short_ratio_history"))
    assets = {}
    for ticker, symbol in symbols.items():
        oi_row = oi_hist.get(symbol) or {}
        liq_row = liq_hist.get(symbol) or {}
        lsr = _latest_lsr((lsr_hist.get(symbol) or {}).get("history"))
        long_usd, short_usd = _sum_liq(liq_row.get("history"))
        fr = funding.get(symbol) or {}
        pfr = predicted.get(symbol) or {}
        assets[ticker] = {
            "symbol": symbol,
            "funding": _num(fr.get("value")),
            "predicted_funding": _num(pfr.get("value")),
            "oi_change_pct": _oi_change_pct(oi_row.get("history")),
            "long_liq_usd": long_usd,
            "short_liq_usd": short_usd,
            "ls_ratio": lsr["ratio"],
            "long_pct": lsr["long_pct"],
            "short_pct": lsr["short_pct"],
        }
    return assets


def _aggregates(assets: dict) -> dict:
    def group(tickers, field):
        return _median([
            _num((assets.get(t) or {}).get(field)) for t in tickers
        ])

    long_usd = sum(_num((assets.get(t) or {}).get("long_liq_usd")) or 0.0 for t in WATCH)
    short_usd = sum(_num((assets.get(t) or {}).get("short_liq_usd")) or 0.0 for t in WATCH)
    total = long_usd + short_usd
    return {
        "majors_funding_median": group(WATCH_MAJORS, "funding"),
        "memes_funding_median": group(WATCH_MEMES, "funding"),
        "majors_predicted_median": group(WATCH_MAJORS, "predicted_funding"),
        "memes_predicted_median": group(WATCH_MEMES, "predicted_funding"),
        "oi_change_median": group(WATCH, "oi_change_pct"),
        "ls_ratio_median": group(WATCH, "ls_ratio"),
        "long_liq_usd": long_usd,
        "short_liq_usd": short_usd,
        "long_liq_share": (long_usd / total) if total > 0 else None,
    }


def maybe_capture(now: float | None = None, cache: dict | None = None) -> dict | None:
    """Fetch one snapshot or skip. Never raises. Never logs the API key."""
    global _last_attempt_ts, _memory_cache, _last_snap
    wall = current_time()
    stamp = wall if now is None else float(now)
    key = api_key()
    if not key:
        _skip("no key")
        return None
    if _last_attempt_ts is not None and wall - _last_attempt_ts < MIN_INTERVAL_SEC:
        _skip("cooldown")
        return None
    _last_attempt_ts = wall
    working_cache = dict(cache or _memory_cache or {})
    deadline = wall + OVERALL_TIMEOUT_SEC
    calls = 0
    raw = {
        "funding": None,
        "predicted_funding": None,
        "open_interest_history": None,
        "liquidation_history": None,
        "long_short_ratio_history": None,
    }
    try:
        if not _cache_fresh(working_cache, wall):
            if calls >= MAX_HTTP_CALLS:
                _skip("call budget")
                return None
            markets = _http_get("/future-markets", key=key, params=None, deadline=deadline)
            calls += 1
            resolved = resolve_symbols(markets)
            if not resolved:
                _skip("no symbols")
                return None
            working_cache = resolved
            _memory_cache = dict(resolved)
        symbols = _symbol_map(working_cache)
        if not symbols:
            _skip("no symbols")
            return None
        joined = ",".join(symbols[t] for t in WATCH if t in symbols)
        hist_params = {
            "symbols": joined,
            "interval": HISTORY_INTERVAL,
            "from": int(stamp - HISTORY_LOOKBACK_SEC),
            "to": int(stamp),
            "convert_to_usd": "true",
        }
        endpoints = (
            ("/funding-rate", {"symbols": joined}, "funding"),
            ("/predicted-funding-rate", {"symbols": joined}, "predicted_funding"),
            ("/open-interest-history", hist_params, "open_interest_history"),
            ("/liquidation-history", hist_params, "liquidation_history"),
            ("/long-short-ratio-history", {
                "symbols": joined,
                "interval": HISTORY_INTERVAL,
                "from": int(stamp - HISTORY_LOOKBACK_SEC),
                "to": int(stamp),
            }, "long_short_ratio_history"),
        )
        for path, params, dest in endpoints:
            if calls >= MAX_HTTP_CALLS or current_time() >= deadline:
                break
            try:
                raw[dest] = _http_get(path, key=key, params=params, deadline=deadline)
                calls += 1
            except TimeoutError:
                break

        if all(raw[k] is None for k in raw):
            _skip("no metrics")
            return None

        assets = _build_assets(symbols, raw)
        metrics = {
            "assets": assets,
            "aggregates": _aggregates(assets),
            "raw": raw,
            "http_calls": calls,
            "rules": REGIME_RULES,
        }
        tag, reason = derive_regime(metrics)
        metrics["reason"] = reason
        _memory_cache = dict(working_cache)
        log.info("coinalyze: regime=%s calls=%s", tag, calls)
        _last_snap = {
            "ts": stamp,
            "regime": tag,
            "reason": reason,
            "symbols": working_cache,
            "metrics": metrics,
            "http_calls": calls,
        }
        return dict(_last_snap)
    except Exception as e:
        _skip(safe_err(e))
        return None
