"""GeckoTerminal / CoinGecko onchain helper.

Free GT:  https://api.geckoterminal.com/api/v2
Demo:     https://api.coingecko.com/api/v3/onchain     (x-cg-demo-api-key)
Pro:      https://pro-api.coingecko.com/api/v3/onchain (x-cg-pro-api-key)

COINGECKO_PLAN=demo|pro. Default demo when COINGECKO_API_KEY is set.
Demo is outcomes/backtest only (10k calls/month). Pro is desk + outcomes.
Never logs the key. Fail open to free GT on 401/403 or demo monthly cap.
"""
from __future__ import annotations

import logging
import os

from secret_utils import safe_err

log = logging.getLogger("gt_api")

GT_FREE_BASE = "https://api.geckoterminal.com/api/v2"
CG_DEMO_ONCHAIN = "https://api.coingecko.com/api/v3/onchain"
CG_PRO_ONCHAIN = "https://pro-api.coingecko.com/api/v3/onchain"
DEMO_HEADER = "x-cg-demo-api-key"
PRO_HEADER = "x-cg-pro-api-key"

UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) desk/1.0",
    "Accept": "application/json",
}

FREE_OUTCOMES_SPACING_SEC = 7.0
DEMO_OUTCOMES_SPACING_SEC = 2.0
PRO_OUTCOMES_SPACING_SEC = 1.5
FREE_GT_CALLS_PER_MIN = 5
PRO_GT_CALLS_PER_MIN = 30
FREE_GT_MIN_SPACING_SEC = 12.0
PRO_GT_MIN_SPACING_SEC = 2.0
DEFAULT_TRICKLE_SEC = 20.0
DEFAULT_WINDOW = "01:00-06:00"
DEMO_MONTHLY_CAP = 9000

_key_failed = False
_demo_capped_fn = None


def reset() -> None:
    """Tests: clear the key-fallback latch."""
    global _key_failed
    _key_failed = False


def set_demo_capped_fn(fn) -> None:
    """outcomes.db wires the Demo budget cap (monthly or daily share). None restores."""
    global _demo_capped_fn
    _demo_capped_fn = fn


def coingecko_api_key() -> str | None:
    raw = os.environ.get("COINGECKO_API_KEY")
    if raw is None:
        return None
    key = str(raw).strip()
    return key or None


def plan() -> str | None:
    """'demo' | 'pro' | None. Default demo when a key is set."""
    if not coingecko_api_key():
        return None
    raw = (os.environ.get("COINGECKO_PLAN") or "demo").strip().lower()
    if raw in ("pro", "paid"):
        return "pro"
    return "demo"


def demo_capped() -> bool:
    fn = _demo_capped_fn
    if fn is None:
        return False
    try:
        return bool(fn())
    except Exception:
        # Can't read the counter: do not burn the Demo monthly quota.
        return True


def using_pro() -> bool:
    return plan() == "pro" and bool(coingecko_api_key()) and not _key_failed


def using_demo() -> bool:
    return (
        plan() == "demo"
        and bool(coingecko_api_key())
        and not _key_failed
        and not demo_capped()
    )


def key_for(purpose: str) -> bool:
    """Desk uses Pro only. Outcomes/backtest may use Demo or Pro."""
    if purpose == "desk":
        return using_pro()
    return using_pro() or using_demo()


def note_key_failed() -> None:
    global _key_failed
    if not _key_failed:
        _key_failed = True
        label = plan() or "key"
        log.info("coingecko %s unavailable, falling back to free GT", label)


def note_pro_failed() -> None:
    """Back-compat alias."""
    note_key_failed()


def onchain_base(*, purpose: str = "outcomes") -> str:
    if not key_for(purpose):
        return GT_FREE_BASE
    if using_pro():
        return CG_PRO_ONCHAIN
    return CG_DEMO_ONCHAIN


def request_headers(url: str | None = None, *, purpose: str = "outcomes") -> dict:
    """UA plus plan header when this URL should use the key. Never log this dict."""
    headers = dict(UA)
    if not key_for(purpose):
        return headers
    if url and url.startswith(GT_FREE_BASE):
        return headers
    if url and "dexscreener.com" in url:
        return headers
    key = coingecko_api_key()
    if not key:
        return headers
    if using_pro() and (url is None or url.startswith(CG_PRO_ONCHAIN)):
        headers[PRO_HEADER] = key
    elif using_demo() and (url is None or url.startswith(CG_DEMO_ONCHAIN)):
        headers[DEMO_HEADER] = key
    return headers


def prefer_onchain_url(url: str, *, purpose: str = "outcomes") -> str:
    if not url or not key_for(purpose):
        return url
    if url.startswith(GT_FREE_BASE):
        return onchain_base(purpose=purpose) + url[len(GT_FREE_BASE):]
    return url


def prefer_pro_url(url: str) -> str:
    """Back-compat: rewrite for outcomes/backtest (Demo or Pro)."""
    return prefer_onchain_url(url, purpose="outcomes")


def fallback_free_url(url: str) -> str | None:
    if not url:
        return None
    for base in (CG_PRO_ONCHAIN, CG_DEMO_ONCHAIN):
        if url.startswith(base):
            return GT_FREE_BASE + url[len(base):]
    return None


def is_demo_url(url: str | None) -> bool:
    return bool(url) and str(url).startswith(CG_DEMO_ONCHAIN)


def onchain_url(path: str, *, purpose: str = "outcomes") -> str:
    """path like /networks/solana/pools/x/ohlcv/minute"""
    if not path.startswith("/"):
        path = "/" + path
    return onchain_base(purpose=purpose) + path


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
    if using_demo():
        return DEMO_OUTCOMES_SPACING_SEC
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
