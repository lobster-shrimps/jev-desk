"""GeckoTerminal / CoinGecko Pro onchain helper.

Free GT: https://api.geckoterminal.com/api/v2
Pro:     https://pro-api.coingecko.com/api/v3/onchain  (same paths, header key)

Never logs COINGECKO_API_KEY. Fail open to free GT on 401/403.
LOG/ANALYSIS + HTTP only: does not change kill/judge/pick logic.
"""
from __future__ import annotations

import logging
import os

from secret_utils import safe_err

log = logging.getLogger("gt_api")

GT_FREE_BASE = "https://api.geckoterminal.com/api/v2"
CG_PRO_ONCHAIN = "https://pro-api.coingecko.com/api/v3/onchain"
PRO_HEADER = "x-cg-pro-api-key"

UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) desk/1.0",
    "Accept": "application/json",
}

FREE_OUTCOMES_SPACING_SEC = 7.0
PRO_OUTCOMES_SPACING_SEC = 1.5
FREE_GT_CALLS_PER_MIN = 5
PRO_GT_CALLS_PER_MIN = 30
FREE_GT_MIN_SPACING_SEC = 12.0
PRO_GT_MIN_SPACING_SEC = 2.0
DEFAULT_TRICKLE_SEC = 20.0
DEFAULT_WINDOW = "01:00-06:00"

_pro_failed = False


def reset() -> None:
    """Tests: clear the pro-fallback latch."""
    global _pro_failed
    _pro_failed = False


def coingecko_api_key() -> str | None:
    raw = os.environ.get("COINGECKO_API_KEY")
    if raw is None:
        return None
    key = str(raw).strip()
    return key or None


def using_pro() -> bool:
    return bool(coingecko_api_key()) and not _pro_failed


def note_pro_failed() -> None:
    global _pro_failed
    if not _pro_failed:
        _pro_failed = True
        log.info("coingecko pro unavailable, falling back to free GT")


def request_headers() -> dict:
    """UA plus Pro key when set. Caller must never log this dict."""
    headers = dict(UA)
    key = coingecko_api_key()
    if key and not _pro_failed:
        headers[PRO_HEADER] = key
    return headers


def prefer_pro_url(url: str) -> str:
    if not url or not using_pro():
        return url
    if url.startswith(GT_FREE_BASE):
        return CG_PRO_ONCHAIN + url[len(GT_FREE_BASE):]
    return url


def fallback_free_url(url: str) -> str | None:
    if url and url.startswith(CG_PRO_ONCHAIN):
        return GT_FREE_BASE + url[len(CG_PRO_ONCHAIN):]
    return None


def onchain_url(path: str) -> str:
    """path like /networks/solana/pools/x/ohlcv/minute"""
    if not path.startswith("/"):
        path = "/" + path
    base = CG_PRO_ONCHAIN if using_pro() else GT_FREE_BASE
    return base + path


def outcomes_spacing_sec(*, trickle: bool = False) -> float:
    if trickle:
        raw = os.environ.get("OUTCOMES_TRICKLE_SEC")
        if raw:
            try:
                return max(5.0, float(raw))
            except (TypeError, ValueError):
                pass
        return DEFAULT_TRICKLE_SEC
    if using_pro():
        return PRO_OUTCOMES_SPACING_SEC
    return FREE_OUTCOMES_SPACING_SEC


def parse_hhmm(text: str) -> int | None:
    """'01:00' or '1:00' -> minutes from midnight. None if invalid."""
    if not text:
        return None
    parts = str(text).strip().split(":")
    if len(parts) != 2:
        return None
    try:
        hh = int(parts[0])
        mm = int(parts[1])
    except (TypeError, ValueError):
        return None
    if not (0 <= hh <= 23 and 0 <= mm <= 59):
        return None
    return hh * 60 + mm


def parse_outcomes_window(raw: str | None = None) -> tuple[int, int]:
    """Return (start_min, end_min) local minutes. Default 01:00-06:00."""
    text = raw if raw is not None else os.environ.get("OUTCOMES_WINDOW", DEFAULT_WINDOW)
    if not text or not str(text).strip():
        text = DEFAULT_WINDOW
    bits = str(text).strip().split("-", 1)
    if len(bits) != 2:
        return parse_hhmm("01:00") or 60, parse_hhmm("06:00") or 360
    start = parse_hhmm(bits[0])
    end = parse_hhmm(bits[1])
    if start is None or end is None or start == end:
        return 60, 360
    return start, end


def in_outcomes_window(now: float | None = None, *, localtime=None) -> bool:
    """True during OUTCOMES_WINDOW local hours (default 01:00-06:00). Wraps midnight."""
    import time
    lt_fn = localtime or time.localtime
    ts = time.time() if now is None else float(now)
    lt = lt_fn(ts)
    minutes = int(lt.tm_hour) * 60 + int(lt.tm_min)
    start, end = parse_outcomes_window()
    if start < end:
        return start <= minutes < end
    return minutes >= start or minutes < end


def apply_desk_pacing(limiter) -> None:
    """Raise desk GT limiter throughput when Pro is on and env did not override."""
    if limiter is None or not using_pro():
        return
    if os.environ.get("GT_CALLS_PER_MIN"):
        return
    try:
        limiter.calls_per_min = PRO_GT_CALLS_PER_MIN
        if not os.environ.get("GT_MIN_SPACING_SEC"):
            limiter.min_spacing_sec = PRO_GT_MIN_SPACING_SEC
    except Exception as e:
        log.warning("desk pro pacing: %s", safe_err(e))
