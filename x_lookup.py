"""In-desk X profile lookup via the keyless fxtwitter public mirror.

read_x(handle) -> dict | None. Fail open: every exception is routed through
safe_err and the caller sees None, which is today's NO_SOCIAL_CUT path.

Cache lives under outbox/ (default outbox/x_cache.db). Positive hits last
30 minutes; negative hits last 10. Per-cycle HTTP lookups are capped by
X_LOOKUPS_PER_CYCLE (default 15) and a wall-clock budget X_LOOKUP_BUDGET_SEC
(default 60). Only tokens that reached the judge step should call this.
Statuses 404s are retried (the fxtwitter timeline flaps); a statuses 404
never negative-caches a good profile.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import re
import sqlite3
import time
from datetime import datetime, timezone
from urllib.parse import quote, urlparse

import requests

from secret_utils import safe_err

log = logging.getLogger("x_lookup")

FX_PROFILE_URL = "https://api.fxtwitter.com/{handle}"
FX_STATUSES_URL = "https://api.fxtwitter.com/2/profile/{handle}/statuses"
TIMEOUT_SEC = 10
POSITIVE_TTL_SEC = 30 * 60
NEGATIVE_TTL_SEC = 10 * 60
DEFAULT_LOOKUPS_PER_CYCLE = 15
DEFAULT_BUDGET_SEC = 60.0
LOCK_TIMEOUT_SEC = 0.2
STATUSES_COUNT = 10
STATUSES_404_RETRIES = 2          # extra tries after the first 404
STATUSES_404_GAP_SEC = 1.5        # pause between statuses 404 retries

HANDLE_RE = re.compile(r"^[A-Za-z0-9_]{1,15}$")
RESERVED_PATHS = {
    "i", "intent", "share", "home", "explore", "search", "settings",
    "compose", "hashtag", "web", "login", "signup", "tos", "privacy",
}
TWITTER_HOSTS = {"x.com", "twitter.com", "mobile.twitter.com"}

_conn: sqlite3.Connection | None = None
_conn_path: str | None = None
_time_fn = time.time
_sleep_fn = time.sleep
_lookups_this_cycle = 0
_window_start: float | None = None


def set_time_fn(fn):
    """Inject a clock for deterministic tests. Pass None to restore time.time."""
    global _time_fn
    _time_fn = time.time if fn is None else fn


def set_sleep_fn(fn):
    """Inject sleep for tests. Pass None to restore time.sleep."""
    global _sleep_fn
    _sleep_fn = time.sleep if fn is None else fn


def current_time() -> float:
    return float(_time_fn())


def outbox_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("DESK_OUTBOX", "outbox"))


def db_path() -> pathlib.Path:
    override = os.environ.get("X_CACHE_DB")
    if override:
        return pathlib.Path(override)
    return outbox_dir() / "x_cache.db"


def lookups_per_cycle() -> int:
    raw = os.environ.get("X_LOOKUPS_PER_CYCLE")
    if raw is None or raw == "":
        return DEFAULT_LOOKUPS_PER_CYCLE
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEFAULT_LOOKUPS_PER_CYCLE


def lookup_budget_sec() -> float:
    raw = os.environ.get("X_LOOKUP_BUDGET_SEC")
    if raw is None or raw == "":
        return DEFAULT_BUDGET_SEC
    try:
        return max(0.0, float(raw))
    except (TypeError, ValueError):
        return DEFAULT_BUDGET_SEC


def reset_cycle() -> None:
    """Clear the per-cycle HTTP counter and wall-clock window. main.run_once calls this."""
    global _lookups_this_cycle, _window_start
    _lookups_this_cycle = 0
    _window_start = None


def cycle_lookups() -> int:
    return _lookups_this_cycle


def reset() -> None:
    """Close the cached connection. Tests call this between cases."""
    global _conn, _conn_path
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    _conn = None
    _conn_path = None
    reset_cycle()


def normalize_handle(raw) -> str | None:
    """Strip @, x.com/twitter.com URLs and query strings. Reject communities and statuses.

    Community links log `x_kind=community` and return None (no profile to fetch).
    Status links are rejected: they are posts, not accounts.
    """
    if raw is None:
        return None
    if not isinstance(raw, str):
        raw = str(raw)
    s = raw.strip()
    if not s:
        return None

    s = s.split("#")[0].split("?")[0].strip()
    if not s:
        return None

    lower = s.lower()
    if "/i/communities" in lower or "/i/community/" in lower:
        log.info("x_kind=community raw=%s", s[:160])
        return None
    if "/status/" in lower or lower.rstrip("/").endswith("/status"):
        return None

    path = s
    looks_url = (
        "://" in s
        or lower.startswith("x.com/")
        or lower.startswith("www.x.com/")
        or lower.startswith("twitter.com/")
        or lower.startswith("www.twitter.com/")
        or lower.startswith("mobile.twitter.com/")
    )
    if looks_url:
        url = s if "://" in s else "https://" + s
        try:
            parsed = urlparse(url)
        except Exception:
            return None
        host = (parsed.netloc or "").lower().split(":")[0]
        if host.startswith("www."):
            host = host[4:]
        if host not in TWITTER_HOSTS:
            return None
        path = parsed.path or ""

    path = path.strip().lstrip("/").lstrip("@")
    if not path:
        return None

    first = path.split("/")[0].lstrip("@")
    rest = path[len(first):].lower() if "/" in path else ""
    if first.lower() == "i" or "/i/communities" in ("/" + path).lower():
        log.info("x_kind=community raw=%s", s[:160])
        return None
    if "/status/" in ("/" + path).lower() or rest.rstrip("/").endswith("/status"):
        return None
    if first.lower() in RESERVED_PATHS:
        return None
    if HANDLE_RE.fullmatch(first):
        return first
    return None


def read_x(handle: str, tid: str | None = None) -> dict | None:
    """Fetch the x_account block for handle, or None. Never raises to the caller."""
    global _lookups_this_cycle
    err = "-"
    source = "none"
    block = None
    key = (handle or "").strip().lstrip("@")
    if not key:
        _log_lookup(tid, handle, False, None, None, source, err)
        return None
    key = key.lower()

    try:
        cached, cached_ok = _cache_get(key)
        if cached_ok is True:
            _log_lookup(tid, handle, True, cached, cached, "cache", "-")
            return cached
        if cached_ok is False:
            _log_lookup(tid, handle, False, None, None, "cache", "-")
            return None

        if _over_budget():
            elapsed = 0.0 if _window_start is None else current_time() - _window_start
            log.info(
                "x budget exceeded elapsed=%.1fs budget=%s tid=%s handle=%s",
                elapsed, lookup_budget_sec(), tid or "-", handle,
            )
            _log_lookup(tid, handle, False, None, None, "none", "budget")
            return None

        cap = lookups_per_cycle()
        if _lookups_this_cycle >= cap:
            _log_lookup(tid, handle, False, None, None, "none", "cap")
            return None
        _lookups_this_cycle += 1

        block = _fetch_account(handle)
        if block is None:
            _cache_put(key, None, ok=False)
            _log_lookup(tid, handle, False, None, None, "none", "-")
            return None
        _cache_put(key, block, ok=True)
        _log_lookup(tid, handle, True, block, block, "fxtwitter", "-")
        return block
    except Exception as e:
        err = safe_err(e)
        try:
            _cache_put(key, None, ok=False)
        except Exception as cache_e:
            log.warning("x cache put failed: %s", safe_err(cache_e))
        _log_lookup(tid, handle, False, None, None, "none", err)
        return None


def _log_lookup(tid, handle, ok, followers_src, posts_src, source, err) -> None:
    followers = "-"
    own_posts = "-"
    if isinstance(followers_src, dict) and followers_src.get("followers") is not None:
        followers = followers_src.get("followers")
    if isinstance(posts_src, dict) and posts_src.get("own_posts_count") is not None:
        own_posts = posts_src.get("own_posts_count")
    log.info(
        "x tid=%s handle=%s ok=%s followers=%s own_posts=%s source=%s err=%s",
        tid or "-", handle or "-", ok, followers, own_posts, source, err or "-",
    )


def _fetch_account(handle: str) -> dict | None:
    """HTTP fetch. Raises on timeout/JSON errors so read_x can safe_err them."""
    profile_url = FX_PROFILE_URL.format(handle=quote(handle, safe=""))
    resp = _get_with_retry(profile_url)
    if resp.status_code != 200:
        raise RuntimeError(f"HTTP {resp.status_code}")
    try:
        payload = resp.json()
    except Exception as e:
        raise RuntimeError("malformed JSON") from e
    if not isinstance(payload, dict):
        raise RuntimeError("malformed JSON")
    user = payload.get("user")
    if not isinstance(user, dict):
        raise RuntimeError("malformed JSON")

    joined = user.get("joined")
    age = _account_age_days(joined)
    tweets = user.get("tweets")
    if tweets is None:
        tweets = user.get("statuses")
    verification = user.get("verification")
    if verification is None:
        verification = user.get("verified")

    recent_posts = _fetch_statuses(handle)
    own = 0
    handle_l = handle.lower()
    for p in recent_posts:
        author = (p.get("author") or {}).get("screen_name") or ""
        if str(author).lower() == handle_l:
            own += 1

    return {
        "handle": handle,
        "screen_name": user.get("screen_name"),
        "name": user.get("name"),
        "description": user.get("description"),
        "followers": user.get("followers"),
        "following": user.get("following"),
        "tweets": tweets,
        "joined": joined,
        "verification": verification,
        "protected": user.get("protected"),
        "account_age_days": age,
        "recent_posts": recent_posts,
        "own_posts_count": own,
        "fetched_via": "fxtwitter",
    }


def _over_budget() -> bool:
    """True when this cycle's X-lookup wall-clock window is exhausted.

    The window opens on the first uncached HTTP lookup. Cache hits do not
    start it. budget<=0 skips every HTTP lookup (fail open).
    """
    global _window_start
    budget = lookup_budget_sec()
    now = current_time()
    if _window_start is None:
        if budget <= 0:
            return True
        _window_start = now
        return False
    return (now - _window_start) >= budget


def _parse_statuses_payload(payload) -> list:
    results = []
    if isinstance(payload, dict):
        results = payload.get("results") or payload.get("tweets") or []
    elif isinstance(payload, list):
        results = payload
    if not isinstance(results, list):
        return []
    posts = []
    for item in results[:STATUSES_COUNT]:
        post = _shape_post(item)
        if post is not None:
            posts.append(post)
    return posts


def _fetch_statuses(handle: str) -> list:
    """Last ~10 posts. Best-effort: failure yields [] so a profile still ships.

    fxtwitter's statuses endpoint flaps 200/404 within seconds. Retry a 404
    up to STATUSES_404_RETRIES extra times with a short gap. A statuses 404
    must not fail (or negative-cache) the profile.
    """
    url = FX_STATUSES_URL.format(handle=quote(handle, safe="")) + f"?count={STATUSES_COUNT}"
    last_status = None
    for attempt in range(1 + STATUSES_404_RETRIES):
        try:
            resp = _get_with_retry(url)
        except Exception as e:
            log.warning("x statuses failed for @%s: %s", handle, safe_err(e))
            return []
        last_status = getattr(resp, "status_code", 0) or 0
        if last_status == 200:
            try:
                payload = resp.json()
            except Exception as e:
                log.warning("x statuses JSON for @%s: %s", handle, safe_err(e))
                return []
            return _parse_statuses_payload(payload)
        if last_status == 404 and attempt < STATUSES_404_RETRIES:
            log.info(
                "x statuses 404 retry %s/%s handle=%s",
                attempt + 1, STATUSES_404_RETRIES, handle,
            )
            try:
                _sleep_fn(STATUSES_404_GAP_SEC)
            except Exception as e:
                log.warning("x statuses sleep: %s", safe_err(e))
            continue
        log.warning("x statuses HTTP %s for @%s: %s", last_status, handle,
                    safe_err(RuntimeError(f"HTTP {last_status}")))
        return []
    log.warning("x statuses HTTP %s for @%s: %s", last_status, handle,
                safe_err(RuntimeError(f"HTTP {last_status}")))
    return []


def _shape_post(item) -> dict | None:
    if not isinstance(item, dict):
        return None
    if item.get("type") == "tombstone":
        return None
    author = item.get("author") if isinstance(item.get("author"), dict) else {}
    text = item.get("text") or item.get("content") or ""
    if not isinstance(text, str):
        text = str(text)
    return {
        "created_at": item.get("created_at"),
        "text": text[:200],
        "likes": item.get("likes"),
        "replies": item.get("replies"),
        "views": item.get("views"),
        "author": {"screen_name": author.get("screen_name")},
    }


def _get_with_retry(url: str):
    """One attempt plus one retry on 5xx or timeout. Other errors raise immediately."""
    last_exc = None
    resp = None
    for attempt in range(2):
        try:
            resp = requests.get(url, timeout=TIMEOUT_SEC)
        except (requests.Timeout, requests.ConnectTimeout, requests.ReadTimeout) as e:
            last_exc = e
            if attempt == 0:
                continue
            raise
        status = getattr(resp, "status_code", 0) or 0
        if status >= 500 and attempt == 0:
            last_exc = RuntimeError(f"HTTP {status}")
            continue
        return resp
    if resp is not None:
        return resp
    if last_exc:
        raise last_exc
    raise RuntimeError("x lookup failed")


def _account_age_days(joined) -> int | None:
    ts = _parse_joined(joined)
    if ts is None:
        return None
    age = int((current_time() - ts) / 86400)
    return max(0, age)


def _parse_joined(joined) -> float | None:
    if joined is None or joined == "":
        return None
    if isinstance(joined, (int, float)):
        ts = float(joined)
        if ts > 1e12:
            ts /= 1000.0
        return ts
    s = str(joined).strip()
    if s.endswith("Z"):
        s = s[:-1] + "+00:00"
    for fmt in (
        "%Y-%m-%dT%H:%M:%S%z",
        "%Y-%m-%dT%H:%M:%S.%f%z",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%d",
        "%a %b %d %H:%M:%S %z %Y",
    ):
        try:
            dt = datetime.strptime(s, fmt)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except ValueError:
            continue
    try:
        dt = datetime.fromisoformat(s)
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except Exception:
        return None


def _connect() -> sqlite3.Connection:
    global _conn, _conn_path
    path = str(db_path())
    if _conn is not None and _conn_path == path:
        return _conn
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(path, check_same_thread=False, timeout=LOCK_TIMEOUT_SEC)
    _conn.row_factory = sqlite3.Row
    _init_schema(_conn)
    _conn_path = path
    return _conn


def _init_schema(db: sqlite3.Connection) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS x_cache (
      handle TEXT PRIMARY KEY,
      payload_json TEXT,
      ok INTEGER NOT NULL,
      fetched_at REAL NOT NULL
    );
    """)
    db.commit()


def _cache_get(handle: str):
    """Return (payload, True) on positive hit, (None, False) on negative hit,
    (None, None) on miss / error (fail open to HTTP)."""
    try:
        db = _connect()
        row = db.execute(
            "SELECT payload_json, ok, fetched_at FROM x_cache WHERE handle = ?",
            (handle,),
        ).fetchone()
        if row is None:
            return None, None
        age = current_time() - float(row["fetched_at"])
        ok = int(row["ok"]) == 1
        ttl = POSITIVE_TTL_SEC if ok else NEGATIVE_TTL_SEC
        if age > ttl:
            return None, None
        if not ok:
            return None, False
        payload = json.loads(row["payload_json"]) if row["payload_json"] else None
        if not isinstance(payload, dict):
            return None, None
        return payload, True
    except Exception as e:
        log.warning("x cache get failed: %s", safe_err(e))
        return None, None


def _cache_put(handle: str, payload, ok: bool) -> bool:
    if not handle:
        return False
    try:
        db = _connect()
        body = json.dumps(payload, default=str) if ok and payload is not None else None
        db.execute(
            """INSERT OR REPLACE INTO x_cache (handle, payload_json, ok, fetched_at)
               VALUES (?, ?, ?, ?)""",
            (handle, body, 1 if ok else 0, float(current_time())),
        )
        db.commit()
        return True
    except Exception as e:
        log.warning("x cache put failed: %s", safe_err(e))
        return False
