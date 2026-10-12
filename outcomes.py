"""
Forward-price outcomes for shadow/backtest analysis.

Resolves a DexScreener pool, pulls GT 5m OHLCV, and simulates two exits.
LOG/ANALYSIS ONLY: never called from filter, judge, pick, or order paths.
Own GT pacing (1 call / 7s); never the cycle GT limiter. Fail open.
Exceptions go through safe_err. Never logs secrets.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import sqlite3
import threading
import time

import requests

from secret_utils import safe_err

log = logging.getLogger("outcomes")

DEX_TOKENS = "https://api.dexscreener.com/tokens/v1"
GT_API = "https://api.geckoterminal.com/api/v2"
UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) desk/1.0",
    "Accept": "application/json",
}

DEX_CHAIN = {1399811149: "solana", 56: "bsc", 8453: "base", 4663: "robinhood", 143: "monad"}
GT_NET = {1399811149: "solana", 56: "bsc", 8453: "base", 4663: "robinhood", 143: "monad"}

WINDOW_SEC = 6 * 3600
READY_AFTER_SEC = WINDOW_SEC + 10 * 60  # 6h10m
CANDLE_SEC = 5 * 60
GT_SPACING_SEC = 7.0
GT_429_BACKOFF_SEC = 30.0
OHLCV_LIMIT = 80
TICKET_USD = 50.0
COST = 0.03
SL_PCT = -0.30
TP_PCT = 1.00
TRAIL_PCT = 0.30
HTTP_TIMEOUT = 20
LOCK_TIMEOUT_SEC = 0.2

_conn: sqlite3.Connection | None = None
_conn_path: str | None = None
_get_fn = None
_sleep_fn = None
_time_fn = time.time
_gt_lock = threading.Lock()
_last_gt_call = 0.0
_gt_backoff_until = 0.0


def set_get_fn(fn):
    """Inject HTTP get(url, params=, timeout=, headers=) for tests. None restores."""
    global _get_fn
    _get_fn = fn


def set_sleep_fn(fn):
    global _sleep_fn
    _sleep_fn = fn


def set_time_fn(fn):
    global _time_fn
    _time_fn = time.time if fn is None else fn


def current_time() -> float:
    return float(_time_fn())


def outbox_dir() -> pathlib.Path:
    return pathlib.Path(os.environ.get("DESK_OUTBOX", "outbox"))


def db_path() -> pathlib.Path:
    override = os.environ.get("OUTCOMES_DB")
    if override:
        return pathlib.Path(override)
    return outbox_dir() / "outcomes.db"


def reset() -> None:
    """Close the cached connection and clear pacing. Tests call this."""
    global _conn, _conn_path, _last_gt_call, _gt_backoff_until
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    _conn = None
    _conn_path = None
    with _gt_lock:
        _last_gt_call = 0.0
        _gt_backoff_until = 0.0


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
    CREATE TABLE IF NOT EXISTS outcomes (
      tid TEXT NOT NULL,
      entry_ts REAL NOT NULL,
      pool TEXT,
      chain TEXT,
      candles_json TEXT,
      fetched_at REAL,
      complete INTEGER NOT NULL DEFAULT 0,
      peak_x REAL,
      trough_x REAL,
      ret_6h REAL,
      sim_current_pnl REAL,
      sim_current_reason TEXT,
      sim_trail_pnl REAL,
      sim_trail_reason TEXT,
      PRIMARY KEY (tid, entry_ts)
    );
    """)
    db.commit()


def _sleep(seconds: float) -> None:
    fn = _sleep_fn
    if fn is not None:
        fn(seconds)
        return
    time.sleep(max(0.0, float(seconds)))


def _http_get(url: str, params=None, timeout=HTTP_TIMEOUT):
    fn = _get_fn or requests.get
    return fn(url, params=params, timeout=timeout, headers=UA)


def parse_tid(tid) -> tuple[str | None, int | None]:
    if not tid:
        return None, None
    s = str(tid)
    if ":" not in s:
        return s, None
    addr, tail = s.rsplit(":", 1)
    try:
        return addr, int(tail)
    except ValueError:
        return s, None


def parse_ohlcv(payload) -> list[dict]:
    """GT ohlcv_list -> [{ts, open, high, low, close, volume}, ...] oldest first."""
    if not isinstance(payload, dict):
        return []
    data = payload.get("data")
    attrs = data.get("attributes") if isinstance(data, dict) else None
    raw = (attrs or {}).get("ohlcv_list") if isinstance(attrs, dict) else None
    if not isinstance(raw, list):
        return []
    candles = []
    for row in raw:
        if not isinstance(row, (list, tuple)) or len(row) < 5:
            continue
        try:
            ts = float(row[0])
            if ts > 1e12:
                ts /= 1000.0
            candles.append({
                "ts": ts,
                "open": float(row[1]),
                "high": float(row[2]),
                "low": float(row[3]),
                "close": float(row[4]),
                "volume": float(row[5]) if len(row) > 5 and row[5] is not None else 0.0,
            })
        except (TypeError, ValueError):
            continue
    candles.sort(key=lambda c: c["ts"])
    return candles


def entry_slice(candles: list[dict], entry_ts: float) -> list[dict]:
    """Candles whose open is at or after entry_ts, through the 6h window."""
    end = float(entry_ts) + WINDOW_SEC
    out = []
    for c in candles:
        ts = c.get("ts")
        if ts is None:
            continue
        if ts + 1e-9 < float(entry_ts):
            continue
        if ts > end + CANDLE_SEC:
            break
        out.append(c)
    return out


def window_complete(candles: list[dict], entry_ts: float) -> bool:
    if not candles:
        return False
    last = candles[-1]["ts"]
    return last + CANDLE_SEC >= float(entry_ts) + WINDOW_SEC - 1.0


def path_metrics(candles: list[dict], entry_price: float) -> dict:
    highs = [c["high"] for c in candles]
    lows = [c["low"] for c in candles]
    last_close = candles[-1]["close"]
    peak_x = (max(highs) / entry_price) if entry_price else None
    trough_x = (min(lows) / entry_price) if entry_price else None
    ret_6h = (last_close / entry_price - 1.0) if entry_price else None
    return {"peak_x": peak_x, "trough_x": trough_x, "ret_6h": ret_6h}


def pnl_usd(exit_price: float, entry_price: float, ticket: float = TICKET_USD,
            cost: float = COST) -> float:
    if not entry_price:
        return 0.0
    return float(ticket) * ((exit_price / entry_price - 1.0) - cost)


def simulate(candles: list[dict], entry_ts: float, *, trail: bool = False,
             ticket: float = TICKET_USD) -> dict:
    """Walk 5m candles. Within a candle, SL (and trail) before TP (pessimistic).

    sim_current: SL -30, TP +100, 6h time stop, 3% cost.
    sim_trail:   those plus a 30% trail from the running high.
    """
    empty = {
        "pnl": None, "reason": None, "exit_price": None, "exit_ts": None, "complete": False,
    }
    sliced = entry_slice(candles, entry_ts)
    if not sliced:
        return dict(empty)
    entry_price = sliced[0]["open"]
    if not entry_price or entry_price <= 0:
        return dict(empty)
    sl_price = entry_price * (1.0 + SL_PCT)
    tp_price = entry_price * (1.0 + TP_PCT)
    end_ts = float(entry_ts) + WINDOW_SEC
    running_high = entry_price
    last = sliced[0]
    for c in sliced:
        ts = c["ts"]
        stop = sl_price
        if trail:
            stop = max(sl_price, running_high * (1.0 - TRAIL_PCT))
        # Pessimistic: stop before take-profit in the same bar.
        if c["low"] <= stop:
            reason = "trail" if trail and stop > sl_price + 1e-12 else "sl"
            return {
                "pnl": pnl_usd(stop, entry_price, ticket),
                "reason": reason,
                "exit_price": stop,
                "exit_ts": ts,
                "complete": True,
                "entry_price": entry_price,
            }
        if c["high"] >= tp_price:
            return {
                "pnl": pnl_usd(tp_price, entry_price, ticket),
                "reason": "tp",
                "exit_price": tp_price,
                "exit_ts": ts,
                "complete": True,
                "entry_price": entry_price,
            }
        running_high = max(running_high, c["high"])
        last = c
        if ts + CANDLE_SEC >= end_ts:
            px = c["close"]
            return {
                "pnl": pnl_usd(px, entry_price, ticket),
                "reason": "time",
                "exit_price": px,
                "exit_ts": ts,
                "complete": True,
                "entry_price": entry_price,
            }
    complete = window_complete(sliced, entry_ts)
    if complete:
        px = last["close"]
        return {
            "pnl": pnl_usd(px, entry_price, ticket),
            "reason": "time",
            "exit_price": px,
            "exit_ts": last["ts"],
            "complete": True,
            "entry_price": entry_price,
        }
    return {
        "pnl": None, "reason": None, "exit_price": None, "exit_ts": None,
        "complete": False, "entry_price": entry_price,
    }


def metrics_for_candles(candles: list[dict], entry_ts: float) -> dict | None:
    sliced = entry_slice(candles, entry_ts)
    if not sliced or not sliced[0]["open"]:
        return None
    entry_price = sliced[0]["open"]
    path = path_metrics(sliced, entry_price)
    cur = simulate(candles, entry_ts, trail=False)
    tr = simulate(candles, entry_ts, trail=True)
    complete = window_complete(sliced, entry_ts)
    return {
        "entry_price": entry_price,
        "peak_x": path["peak_x"],
        "trough_x": path["trough_x"],
        "ret_6h": path["ret_6h"] if complete else None,
        "sim_current_pnl": cur.get("pnl") if complete or cur.get("reason") in ("sl", "tp", "trail") else None,
        "sim_current_reason": cur.get("reason"),
        "sim_trail_pnl": tr.get("pnl") if complete or tr.get("reason") in ("sl", "tp", "trail") else None,
        "sim_trail_reason": tr.get("reason"),
        "complete": complete,
        "candles": sliced,
    }


def get_cached(tid: str, entry_ts: float) -> dict | None:
    """Return a stored row dict, or None. Never hits the network."""
    try:
        db = _connect()
        row = db.execute(
            "SELECT * FROM outcomes WHERE tid = ? AND ABS(entry_ts - ?) < 1e-6",
            (str(tid), float(entry_ts)),
        ).fetchone()
        if row is None:
            return None
        return _row_dict(row)
    except Exception as e:
        log.warning("outcomes cache get failed: %s", safe_err(e))
        return None


def _row_dict(row: sqlite3.Row) -> dict:
    candles = []
    raw = row["candles_json"]
    if raw:
        try:
            candles = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            candles = []
    return {
        "tid": row["tid"],
        "entry_ts": row["entry_ts"],
        "pool": row["pool"],
        "chain": row["chain"],
        "candles": candles,
        "fetched_at": row["fetched_at"],
        "complete": bool(row["complete"]),
        "peak_x": row["peak_x"],
        "trough_x": row["trough_x"],
        "ret_6h": row["ret_6h"],
        "sim_current_pnl": row["sim_current_pnl"],
        "sim_current_reason": row["sim_current_reason"],
        "sim_trail_pnl": row["sim_trail_pnl"],
        "sim_trail_reason": row["sim_trail_reason"],
    }


def put_cached(tid: str, entry_ts: float, payload: dict) -> None:
    """Store candles + metrics. Used by tests and fetch. Fail open."""
    try:
        db = _connect()
        candles = payload.get("candles") or []
        db.execute(
            """INSERT OR REPLACE INTO outcomes (
                 tid, entry_ts, pool, chain, candles_json, fetched_at, complete,
                 peak_x, trough_x, ret_6h, sim_current_pnl, sim_current_reason,
                 sim_trail_pnl, sim_trail_reason
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (
                str(tid), float(entry_ts), payload.get("pool"), payload.get("chain"),
                json.dumps(candles, default=str),
                float(payload.get("fetched_at") or current_time()),
                1 if payload.get("complete") else 0,
                payload.get("peak_x"), payload.get("trough_x"), payload.get("ret_6h"),
                payload.get("sim_current_pnl"), payload.get("sim_current_reason"),
                payload.get("sim_trail_pnl"), payload.get("sim_trail_reason"),
            ),
        )
        db.commit()
    except Exception as e:
        log.warning("outcomes cache put failed: %s", safe_err(e))


def _pace_gt(deadline: float | None) -> bool:
    """Wait for own 7s spacing and 429 backoff. False if the deadline would be missed."""
    global _last_gt_call
    with _gt_lock:
        now = current_time()
        wait_until = max(_last_gt_call + GT_SPACING_SEC, _gt_backoff_until)
        wait = wait_until - now
    if wait > 0:
        if deadline is not None and current_time() + wait > deadline:
            return False
        _sleep(wait)
    with _gt_lock:
        _last_gt_call = current_time()
    return True


def _note_gt_429() -> None:
    global _gt_backoff_until
    with _gt_lock:
        _gt_backoff_until = current_time() + GT_429_BACKOFF_SEC
    log.info("outcomes GT 429, backing off %.0fs", GT_429_BACKOFF_SEC)
    _sleep(GT_429_BACKOFF_SEC)


def _highest_liq_pair(pairs) -> dict | None:
    if isinstance(pairs, dict):
        pairs = pairs.get("pairs") or pairs.get("data") or [pairs]
    if not isinstance(pairs, list) or not pairs:
        return None
    def liq(p):
        if not isinstance(p, dict):
            return -1.0
        block = p.get("liquidity")
        if isinstance(block, dict):
            try:
                return float(block.get("usd") or 0)
            except (TypeError, ValueError):
                return 0.0
        return 0.0
    usable = [p for p in pairs if isinstance(p, dict)]
    if not usable:
        return None
    return max(usable, key=liq)


def resolve_pool(tid: str) -> tuple[str | None, str | None]:
    """DexScreener /tokens/v1/{chain}/{addr} -> (pool, chain_slug). Fail open."""
    addr, net = parse_tid(tid)
    if not addr:
        return None, None
    chain = DEX_CHAIN.get(net) if net is not None else None
    if not chain:
        return None, None
    url = f"{DEX_TOKENS}/{chain}/{addr}"
    try:
        resp = _http_get(url, timeout=HTTP_TIMEOUT)
        status = getattr(resp, "status_code", None)
        if status != 200:
            log.info("outcomes dex HTTP %s", status)
            return None, chain
        pairs = resp.json()
        best = _highest_liq_pair(pairs)
        if not best:
            return None, chain
        pool = best.get("pairAddress") or best.get("address")
        return (str(pool) if pool else None), chain
    except Exception as e:
        log.warning("outcomes dex failed: %s", safe_err(e))
        return None, chain


def fetch_ohlcv(chain: str, pool: str, entry_ts: float, deadline: float | None = None):
    """GT 5m OHLCV ending at entry+6h. Own limiter. None on 429/error."""
    net = chain
    url = f"{GT_API}/networks/{net}/pools/{pool}/ohlcv/minute"
    params = {
        "aggregate": 5,
        "before_timestamp": int(float(entry_ts) + WINDOW_SEC),
        "limit": OHLCV_LIMIT,
        "currency": "usd",
    }
    if deadline is not None and current_time() >= deadline:
        return None
    if not _pace_gt(deadline):
        return None
    try:
        resp = _http_get(url, params=params, timeout=HTTP_TIMEOUT)
        status = getattr(resp, "status_code", None)
        if status == 429:
            _note_gt_429()
            return None
        if status != 200:
            log.info("outcomes gt HTTP %s", status)
            return None
        return parse_ohlcv(resp.json())
    except Exception as e:
        log.warning("outcomes gt failed: %s", safe_err(e))
        return None


def fetch_outcome(tid: str, entry_ts: float, *, now: float | None = None,
                  deadline: float | None = None, force: bool = False) -> dict | None:
    """Resolve pool, fetch candles, compute metrics. Cache-final once complete.

    Only fetches when entry_ts is at least 6h10m old. Fail open.
    """
    try:
        now = current_time() if now is None else float(now)
        if now < float(entry_ts) + READY_AFTER_SEC:
            return get_cached(tid, entry_ts)
        cached = get_cached(tid, entry_ts)
        if cached and cached.get("complete") and not force:
            return cached
        if deadline is not None and current_time() >= deadline:
            return cached
        pool = (cached or {}).get("pool")
        chain = (cached or {}).get("chain")
        if not pool:
            pool, chain = resolve_pool(tid)
        if not pool or not chain:
            return cached
        candles = fetch_ohlcv(chain, pool, entry_ts, deadline=deadline)
        if candles is None:
            return cached
        computed = metrics_for_candles(candles, entry_ts)
        if not computed:
            return cached
        payload = {
            "pool": pool,
            "chain": chain,
            "candles": computed["candles"],
            "fetched_at": current_time(),
            "complete": computed["complete"],
            "peak_x": computed["peak_x"],
            "trough_x": computed["trough_x"],
            "ret_6h": computed["ret_6h"],
            "sim_current_pnl": computed["sim_current_pnl"],
            "sim_current_reason": computed["sim_current_reason"],
            "sim_trail_pnl": computed["sim_trail_pnl"],
            "sim_trail_reason": computed["sim_trail_reason"],
        }
        put_cached(tid, entry_ts, payload)
        payload["tid"] = tid
        payload["entry_ts"] = float(entry_ts)
        return payload
    except Exception as e:
        log.warning("outcomes fetch failed: %s", safe_err(e))
        try:
            return get_cached(tid, entry_ts)
        except Exception:
            return None
