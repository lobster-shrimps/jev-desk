"""
Forward-price outcomes for shadow/backtest analysis.

Resolves a DexScreener pool, pulls GT 5m OHLCV, and simulates two exits.
LOG/ANALYSIS ONLY: never called from filter, judge, pick, or order paths.
Keyless GT uses the desk shared limiter (non-priority). Fail open.
Bulk fetch in OUTCOMES_WINDOW (default 01:00-06:00 local); daytime trickle.
Progress lives in outcomes.db (fetch_jobs) and resumes after timeout.
Exceptions go through safe_err. Never logs secrets.
"""
from __future__ import annotations

import calendar
import json
import logging
import os
import pathlib
import sqlite3
import threading
import time

import requests

import gt_api
from secret_utils import safe_err

log = logging.getLogger("outcomes")

DEX_TOKENS = "https://api.dexscreener.com/tokens/v1"
GT_API = gt_api.GT_FREE_BASE
UA = gt_api.UA

DEX_CHAIN = {1399811149: "solana", 56: "bsc", 8453: "base", 4663: "robinhood", 143: "monad"}
GT_NET = {1399811149: "solana", 56: "bsc", 8453: "base", 4663: "robinhood", 143: "monad"}

WINDOW_SEC = 6 * 3600
READY_AFTER_SEC = WINDOW_SEC + 10 * 60  # 6h10m
CANDLE_SEC = 5 * 60
GT_SPACING_SEC = 7.0
GT_429_BACKOFF_SEC = 30.0
PAUSE_SLEEP_SEC = 45.0  # in [30, 60] when the worker is paused
PAUSE_LOG_INTERVAL_SEC = 60.0
OHLCV_LIMIT = 80
DEAD_NO_POOL_ATTEMPTS = 5
TICKET_USD = 50.0
COST = 0.03
SL_PCT = -0.30
TP_PCT = 1.00
TRAIL_PCT = 0.30
HTTP_TIMEOUT = 20
LOCK_TIMEOUT_SEC = 0.2
PRIORITY_LIVE = 0
PRIORITY_BACKFILL = 1

_conn: sqlite3.Connection | None = None
_conn_path: str | None = None
_get_fn = None
_sleep_fn = None
_time_fn = time.time
_gt_lock = threading.Lock()
_last_gt_call = 0.0
_gt_backoff_until = 0.0
_pacing_loaded = False
_trickle_mode = False
_worker_lock = threading.Lock()
_worker_thread: threading.Thread | None = None
_shared_limiter = None
_last_pause_log = 0.0
_outcomes_429_count = 0


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


def set_shared_limiter(limiter) -> None:
    """Inject the desk GTRateLimiter. None restores lazy main._gt_limiter."""
    global _shared_limiter
    _shared_limiter = limiter


def current_time() -> float:
    return float(_time_fn())


def pause_sleep_sec() -> float:
    raw = os.environ.get("OUTCOMES_PAUSE_SLEEP_SEC")
    if raw:
        try:
            return min(60.0, max(30.0, float(raw)))
        except (TypeError, ValueError):
            pass
    return PAUSE_SLEEP_SEC


def outcomes_429_count() -> int:
    return int(_outcomes_429_count)


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
    global _trickle_mode, _worker_thread, _pacing_loaded
    global _shared_limiter, _last_pause_log, _outcomes_429_count
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
    _pacing_loaded = False
    _trickle_mode = False
    _shared_limiter = None
    _last_pause_log = 0.0
    _outcomes_429_count = 0
    with _worker_lock:
        _worker_thread = None
    try:
        gt_api.set_demo_capped_fn(demo_budget_capped)
        gt_api.reset()
    except Exception:
        pass
    try:
        import main as shift
        lim = getattr(shift, "_gt_limiter", None)
        if lim is not None and hasattr(lim, "reset_for_test"):
            lim.reset_for_test()
    except Exception:
        pass


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
    _enable_wal(_conn)
    _init_schema(_conn)
    _conn_path = path
    return _conn


OPS_BUSY_TIMEOUT_SEC = 0.05


def _enable_wal(db: sqlite3.Connection) -> None:
    """WAL + synchronous=NORMAL. Idempotent. Own commit (SQLite auto-commits PRAGMA)."""
    try:
        db.commit()
    except Exception:
        pass
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")


def readonly_connect(timeout: float | None = None) -> sqlite3.Connection:
    """Ops readers: mode=ro, short busy timeout. Creates the file if missing."""
    path = db_path()
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    if not pathlib.Path(path).exists():
        _connect()
    uri = f"file:{path}?mode=ro"
    busy = OPS_BUSY_TIMEOUT_SEC if timeout is None else float(timeout)
    conn = sqlite3.connect(uri, uri=True, timeout=busy)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA query_only = ON")
    return conn


def _schema_step(db: sqlite3.Connection, name: str, fn) -> None:
    """One schema change. Failure is logged and does not skip later steps."""
    try:
        fn()
        db.commit()
    except Exception as e:
        log.warning("outcomes schema %s: %s", name, safe_err(e))
        try:
            db.rollback()
        except Exception:
            pass


def _init_schema(db: sqlite3.Connection) -> None:
    """Idempotent. ALTER priority before any index that names it.

    Each step commits on its own so a failure (e.g. index on a missing
    column on a live pre-#60 DB) cannot abort cg_demo_calls_daily / gt_pacing.
    """
    _schema_step(db, "outcomes", lambda: db.execute("""
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
    )"""))
    _schema_step(db, "fetch_jobs", lambda: db.execute("""
    CREATE TABLE IF NOT EXISTS fetch_jobs (
      tid TEXT NOT NULL,
      entry_ts REAL NOT NULL,
      status TEXT NOT NULL DEFAULT 'pending',
      attempts INTEGER NOT NULL DEFAULT 0,
      last_attempt REAL,
      last_error TEXT,
      enqueued_at REAL NOT NULL,
      priority INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (tid, entry_ts)
    )"""))
    _schema_step(db, "priority", lambda: _ensure_fetch_job_priority(db))
    _schema_step(db, "idx_status", lambda: db.execute(
        "CREATE INDEX IF NOT EXISTS idx_fetch_jobs_status ON fetch_jobs(status)"
    ))
    _schema_step(db, "idx_drain", lambda: db.execute(
        """CREATE INDEX IF NOT EXISTS idx_fetch_jobs_drain
           ON fetch_jobs(status, priority, attempts, last_attempt)"""
    ))
    _schema_step(db, "cg_demo_calls", lambda: db.execute("""
    CREATE TABLE IF NOT EXISTS cg_demo_calls (
      yyyymm TEXT PRIMARY KEY,
      calls INTEGER NOT NULL DEFAULT 0
    )"""))
    _schema_step(db, "cg_demo_calls_daily", lambda: db.execute("""
    CREATE TABLE IF NOT EXISTS cg_demo_calls_daily (
      yyyymmdd TEXT PRIMARY KEY,
      calls INTEGER NOT NULL DEFAULT 0
    )"""))
    _schema_step(db, "gt_pacing", lambda: db.execute("""
    CREATE TABLE IF NOT EXISTS gt_pacing (
      k TEXT PRIMARY KEY,
      v REAL NOT NULL
    )"""))
    _schema_step(db, "retire_overlimit", lambda: _retire_overlimit_jobs(db))
    try:
        gt_api.set_demo_capped_fn(demo_budget_capped)
    except Exception:
        pass
    _schema_step(db, "load_pacing", lambda: _load_pacing(db))
    _schema_step(db, "wal", lambda: _enable_wal(db))


def _ensure_fetch_job_priority(db: sqlite3.Connection) -> None:
    """Existing DBs created before priority: default 0 (live). Raises on failure."""
    cols = {row[1] for row in db.execute("PRAGMA table_info(fetch_jobs)")}
    if "priority" not in cols:
        db.execute(
            "ALTER TABLE fetch_jobs ADD COLUMN priority INTEGER NOT NULL DEFAULT 0"
        )


def _load_pacing(db: sqlite3.Connection | None = None) -> None:
    """Restore 2s Demo spacing across process restarts."""
    global _last_gt_call, _pacing_loaded
    if _pacing_loaded:
        return
    try:
        conn = db or _connect()
        row = conn.execute(
            "SELECT v FROM gt_pacing WHERE k = ?", ("last_gt_call",),
        ).fetchone()
        if row is not None:
            with _gt_lock:
                if _last_gt_call == 0.0:
                    _last_gt_call = float(row["v"])
    except Exception as e:
        log.warning("outcomes pacing load: %s", safe_err(e))
    _pacing_loaded = True


def _persist_last_gt_call(ts: float) -> None:
    try:
        db = _connect()
        db.execute(
            "INSERT OR REPLACE INTO gt_pacing (k, v) VALUES (?, ?)",
            ("last_gt_call", float(ts)),
        )
        db.commit()
    except Exception as e:
        log.warning("outcomes pacing save: %s", safe_err(e))


def _sleep(seconds: float) -> None:
    fn = _sleep_fn
    if fn is not None:
        fn(seconds)
        return
    time.sleep(max(0.0, float(seconds)))


def _http_get(url: str, params=None, timeout=HTTP_TIMEOUT):
    """One HTTP request. Demo URLs increment the real-request counter."""
    fn = _get_fn or requests.get
    resp = fn(
        url, params=params, timeout=timeout,
        headers=gt_api.request_headers(url, purpose="outcomes"),
    )
    if gt_api.is_demo_url(url):
        try:
            n = record_demo_call()
            if n >= gt_api.DEMO_MONTHLY_CAP:
                log.info(
                    "coingecko demo monthly cap reached (%d), falling back to free GT", n,
                )
            elif demo_day_capped():
                log.info(
                    "coingecko demo daily cap reached (%d/%d), falling back to free GT",
                    demo_calls_today(), demo_daily_cap(),
                )
        except Exception as e:
            log.warning("demo call counter after HTTP: %s", safe_err(e))
    return resp


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
    """Wait for own spacing and 429 backoff. False if the deadline would be missed.

    last_gt_call is persisted so 2s Demo spacing survives process restarts.
    """
    global _last_gt_call
    _load_pacing()
    spacing = gt_api.outcomes_spacing_sec(trickle=_trickle_mode)
    with _gt_lock:
        now = current_time()
        wait_until = max(_last_gt_call + spacing, _gt_backoff_until)
        wait = wait_until - now
    if wait > 0:
        if deadline is not None and current_time() + wait > deadline:
            return False
        _sleep(wait)
    with _gt_lock:
        _last_gt_call = current_time()
        stamped = _last_gt_call
    _persist_last_gt_call(stamped)
    return True


def _note_gt_429() -> None:
    global _gt_backoff_until, _outcomes_429_count
    _outcomes_429_count += 1
    try:
        gt_api.note_429("outcomes")
    except Exception:
        pass
    with _gt_lock:
        _gt_backoff_until = current_time() + GT_429_BACKOFF_SEC
    log.info("outcomes GT 429, backing off %.0fs", GT_429_BACKOFF_SEC)
    _sleep(GT_429_BACKOFF_SEC)


def _desk_limiter():
    if _shared_limiter is not None:
        return _shared_limiter
    try:
        import main as shift
        return getattr(shift, "_gt_limiter", None)
    except Exception:
        return None


def _in_cycle_gt_phase(now: float | None = None) -> bool:
    """True while the desk limiter still has a future cycle_deadline."""
    now = current_time() if now is None else float(now)
    lim = _desk_limiter()
    if lim is None:
        return False
    deadline = getattr(lim, "cycle_deadline", None)
    if deadline is None:
        return False
    try:
        return float(deadline) > now
    except (TypeError, ValueError):
        return False


def _is_keyless_gt_url(url: str | None) -> bool:
    return bool(url) and str(url).startswith(gt_api.GT_FREE_BASE)


def _retry_after_sec(resp) -> float | None:
    headers = getattr(resp, "headers", None) or {}
    raw = None
    try:
        raw = headers.get("Retry-After")
    except Exception:
        raw = None
    if raw is None or raw == "":
        return None
    try:
        return float(raw)
    except (TypeError, ValueError):
        return None


def _acquire_keyless_slot(deadline: float | None) -> bool:
    """Non-priority desk slot for free-GT. False = skip. Fail open to own pace."""
    if _in_cycle_gt_phase():
        return False
    lim = _desk_limiter()
    if lim is None:
        return _pace_gt(deadline)
    old_time = getattr(lim, "time_fn", None)
    old_sleep = getattr(lim, "sleep_fn", None)
    try:
        lim.time_fn = current_time
        lim.sleep_fn = _sleep
        if not lim.wait_if_needed(priority=False, deadline=deadline):
            return False
        if _in_cycle_gt_phase():
            return False
        if not lim.spend(1, priority=False):
            return False
        return True
    except Exception as e:
        log.warning("outcomes shared limiter: %s", safe_err(e))
        return False
    finally:
        try:
            lim.time_fn = old_time
            lim.sleep_fn = old_sleep
        except Exception:
            pass


def _log_fetch_paused() -> bool:
    """Log the pause at most once per minute. True if a line was written."""
    global _last_pause_log
    now = current_time()
    if _last_pause_log and (now - _last_pause_log) < PAUSE_LOG_INTERVAL_SEC:
        return False
    _last_pause_log = now
    log.info("outcomes fetch paused (cycle GT 429)")
    return True


def _record_keyless_429(resp) -> None:
    lim = _desk_limiter()
    if lim is None:
        return
    try:
        lim.record_429(_retry_after_sec(resp))
    except Exception as e:
        log.warning("outcomes record_429: %s", safe_err(e))


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


def _yyyymm(now: float | None = None) -> str:
    ts = current_time() if now is None else float(now)
    lt = time.localtime(ts)
    return f"{lt.tm_year:04d}-{lt.tm_mon:02d}"


def _yyyymmdd(now: float | None = None) -> str:
    ts = current_time() if now is None else float(now)
    lt = time.localtime(ts)
    return f"{lt.tm_year:04d}-{lt.tm_mon:02d}-{lt.tm_mday:02d}"


def days_left_in_month(now: float | None = None) -> int:
    """Days remaining including today. Oct 12 -> 20 in a 31-day month."""
    ts = current_time() if now is None else float(now)
    lt = time.localtime(ts)
    last = calendar.monthrange(lt.tm_year, lt.tm_mon)[1]
    return max(1, last - int(lt.tm_mday) + 1)


def demo_calls_this_month(now: float | None = None) -> int:
    try:
        db = _connect()
        row = db.execute(
            "SELECT calls FROM cg_demo_calls WHERE yyyymm = ?",
            (_yyyymm(now),),
        ).fetchone()
        return int(row["calls"]) if row else 0
    except Exception as e:
        log.warning("demo call counter read failed: %s", safe_err(e))
        return gt_api.DEMO_MONTHLY_CAP


def demo_calls_today(now: float | None = None) -> int:
    try:
        db = _connect()
        row = db.execute(
            "SELECT calls FROM cg_demo_calls_daily WHERE yyyymmdd = ?",
            (_yyyymmdd(now),),
        ).fetchone()
        return int(row["calls"]) if row else 0
    except Exception as e:
        log.warning("demo daily counter read failed: %s", safe_err(e))
        return gt_api.DEMO_MONTHLY_CAP


def demo_daily_cap(now: float | None = None) -> int:
    """Spread remaining monthly Demo budget across days left (including today).

    remaining is monthly unused *before today*, so today's own calls do not
    shrink the day's allowance. 0 when the month is already exhausted.
    """
    now = current_time() if now is None else float(now)
    used_month = demo_calls_this_month(now)
    used_today = demo_calls_today(now)
    used_before_today = max(0, used_month - used_today)
    remaining = max(0, gt_api.DEMO_MONTHLY_CAP - used_before_today)
    if remaining <= 0:
        return 0
    return max(1, remaining // days_left_in_month(now))


def record_demo_call(now: float | None = None) -> int:
    """Count one actual Demo HTTP request. Returns the new monthly total."""
    month = _yyyymm(now)
    day = _yyyymmdd(now)
    try:
        db = _connect()
        db.execute(
            """INSERT INTO cg_demo_calls (yyyymm, calls) VALUES (?, 1)
               ON CONFLICT(yyyymm) DO UPDATE SET calls = calls + 1""",
            (month,),
        )
        db.execute(
            """INSERT INTO cg_demo_calls_daily (yyyymmdd, calls) VALUES (?, 1)
               ON CONFLICT(yyyymmdd) DO UPDATE SET calls = calls + 1""",
            (day,),
        )
        db.commit()
        row = db.execute(
            "SELECT calls FROM cg_demo_calls WHERE yyyymm = ?", (month,),
        ).fetchone()
        return int(row["calls"]) if row else 1
    except Exception as e:
        log.warning("demo call counter write failed: %s", safe_err(e))
        return gt_api.DEMO_MONTHLY_CAP


def demo_month_capped(now: float | None = None) -> bool:
    return demo_calls_this_month(now) >= gt_api.DEMO_MONTHLY_CAP


def demo_day_capped(now: float | None = None) -> bool:
    cap = demo_daily_cap(now)
    if cap <= 0:
        return True
    return demo_calls_today(now) >= cap


def demo_budget_capped(now: float | None = None) -> bool:
    """True when Demo must not be used: monthly cap or today's share is gone."""
    return demo_month_capped(now) or demo_day_capped(now)


def fetch_ohlcv(chain: str, pool: str, entry_ts: float, deadline: float | None = None):
    """GT / CoinGecko 5m OHLCV ending at entry+6h. Keyless uses the desk limiter."""
    net = chain
    url = gt_api.prefer_onchain_url(
        f"{GT_API}/networks/{net}/pools/{pool}/ohlcv/minute", purpose="outcomes",
    )
    params = {
        "aggregate": 5,
        "before_timestamp": int(float(entry_ts) + WINDOW_SEC),
        "limit": OHLCV_LIMIT,
        "currency": "usd",
    }
    if deadline is not None and current_time() >= deadline:
        return None
    keyless = _is_keyless_gt_url(url)
    if keyless:
        if not _acquire_keyless_slot(deadline):
            return None
    else:
        if not _pace_gt(deadline):
            return None
    try:
        resp = _http_get(url, params=params, timeout=HTTP_TIMEOUT)
        status = getattr(resp, "status_code", None)
        if status in (401, 403):
            free = gt_api.fallback_free_url(url)
            if free:
                gt_api.note_key_failed()
                log.info("outcomes coingecko HTTP %s, falling back to free GT", status)
                if not _acquire_keyless_slot(deadline):
                    return None
                resp = _http_get(free, params=params, timeout=HTTP_TIMEOUT)
                status = getattr(resp, "status_code", None)
                url = free
        if status == 429:
            if _is_keyless_gt_url(url):
                _record_keyless_429(resp)
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
            return {
                "tid": tid, "entry_ts": float(entry_ts), "pool": None,
                "complete": False, "resolve_failed": True,
            }
        if not (cached or {}).get("pool"):
            # Keep the Dex pair even if OHLCV later 429s, so no-pool retire
            # does not kill a resolvable job.
            put_cached(tid, entry_ts, {
                "pool": pool, "chain": chain, "complete": False,
                "candles": (cached or {}).get("candles") or [],
                "fetched_at": current_time(),
            })
            cached = get_cached(tid, entry_ts) or cached
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


# ---------------------------------------------------------------------------
# Resumable fetch queue (outcomes.db fetch_jobs)
# ---------------------------------------------------------------------------

def enqueue_job(tid: str, entry_ts: float, *, now: float | None = None,
                priority: int = PRIORITY_LIVE) -> bool:
    """Insert a pending job if missing. False if already complete/cached/queued.

    priority: 0=live (drained anytime), 1=backfill (01:00-06:00 window only).
    """
    try:
        cached = get_cached(tid, entry_ts)
        if cached and cached.get("complete"):
            _upsert_job(tid, entry_ts, status="complete", now=now)
            return False
        db = _connect()
        ts = current_time() if now is None else float(now)
        pri = int(priority)
        cur = db.execute(
            """INSERT OR IGNORE INTO fetch_jobs
               (tid, entry_ts, status, attempts, enqueued_at, priority)
               VALUES (?, ?, 'pending', 0, ?, ?)""",
            (str(tid), float(entry_ts), ts, pri),
        )
        db.commit()
        return int(cur.rowcount or 0) > 0
    except Exception as e:
        log.warning("outcomes enqueue failed: %s", safe_err(e))
        return False


def queued_or_fetched_tids() -> set[str]:
    """Tids that already have an outcomes row or a fetch_jobs row. Fail closed."""
    try:
        db = _connect()
        tids = {r[0] for r in db.execute("SELECT DISTINCT tid FROM outcomes")}
        tids |= {r[0] for r in db.execute("SELECT DISTINCT tid FROM fetch_jobs")}
        return tids
    except Exception as e:
        log.warning("outcomes queued tids: %s", safe_err(e))
        return set()


def _upsert_job(tid: str, entry_ts: float, *, status: str, now: float | None = None,
                error: str | None = None, bump_attempt: bool = False) -> None:
    try:
        db = _connect()
        ts = current_time() if now is None else float(now)
        row = db.execute(
            "SELECT attempts FROM fetch_jobs WHERE tid = ? AND ABS(entry_ts - ?) < 1e-6",
            (str(tid), float(entry_ts)),
        ).fetchone()
        attempts = int(row["attempts"]) if row else 0
        if bump_attempt:
            attempts += 1
        db.execute(
            """INSERT INTO fetch_jobs
               (tid, entry_ts, status, attempts, last_attempt, last_error,
                enqueued_at, priority)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)
               ON CONFLICT(tid, entry_ts) DO UPDATE SET
                 status = excluded.status,
                 attempts = excluded.attempts,
                 last_attempt = excluded.last_attempt,
                 last_error = excluded.last_error""",
            (str(tid), float(entry_ts), status, attempts, ts, error, ts,
             PRIORITY_LIVE),
        )
        db.commit()
    except Exception as e:
        log.warning("outcomes job update failed: %s", safe_err(e))


def _retire_overlimit_jobs(db: sqlite3.Connection | None = None) -> int:
    """Mark pending jobs already over the no-pool attempt limit as dead. Fail open."""
    try:
        conn = db or _connect()
        cur = conn.execute(
            """UPDATE fetch_jobs SET status = 'dead',
                   last_error = COALESCE(NULLIF(last_error, ''), 'no_pool')
               WHERE status = 'pending' AND attempts >= ?
                 AND NOT EXISTS (
                   SELECT 1 FROM outcomes o
                   WHERE o.tid = fetch_jobs.tid
                     AND ABS(o.entry_ts - fetch_jobs.entry_ts) < 1e-6
                     AND o.pool IS NOT NULL AND o.pool != ''
                 )""",
            (DEAD_NO_POOL_ATTEMPTS,),
        )
        conn.commit()
        return int(cur.rowcount or 0)
    except Exception as e:
        log.warning("outcomes retire overlimit failed: %s", safe_err(e))
        return 0


_JOB_COLS = (
    "tid, entry_ts, status, attempts, last_attempt, last_error, "
    "COALESCE(priority, 0) AS priority"
)


def list_jobs(status: str | None = "pending", limit: int = 500,
              *, include_backfill: bool = True) -> list[dict]:
    """Pending drain order: priority, attempts, last_attempt (live=0 before backfill=1)."""
    try:
        db = _connect()
        if status == "pending":
            extra = "" if include_backfill else " AND COALESCE(priority, 0) = 0"
            rows = db.execute(
                f"""SELECT {_JOB_COLS}
                   FROM fetch_jobs WHERE status = 'pending'{extra}
                   ORDER BY COALESCE(priority, 0) ASC,
                            attempts ASC,
                            last_attempt ASC,
                            tid
                   LIMIT ?""",
                (int(limit),),
            ).fetchall()
        elif status:
            rows = db.execute(
                f"""SELECT {_JOB_COLS}
                   FROM fetch_jobs WHERE status = ?
                   ORDER BY entry_ts, tid LIMIT ?""",
                (status, int(limit)),
            ).fetchall()
        else:
            rows = db.execute(
                f"""SELECT {_JOB_COLS}
                   FROM fetch_jobs ORDER BY entry_ts, tid LIMIT ?""",
                (int(limit),),
            ).fetchall()
        return [dict(r) for r in rows]
    except Exception as e:
        log.warning("outcomes job list failed: %s", safe_err(e))
        return []


def enqueue_from_history(*, days: int = 7, now: float | None = None) -> int:
    """Queue shadow-kill rows that are old enough. Idempotent. Fail open."""
    now = current_time() if now is None else float(now)
    since = now - float(days) * 86400
    n = 0
    try:
        import cycle_history
        rows = cycle_history.shadow_kills_since(since, now)
        for row in rows:
            tid = row.get("tid")
            entry_ts = row.get("entry_ts") if row.get("entry_ts") is not None else row.get("ts")
            if not tid or entry_ts is None:
                continue
            if enqueue_job(str(tid), float(entry_ts), now=now):
                n += 1
    except Exception as e:
        log.warning("outcomes enqueue from history: %s", safe_err(e))
    return n


def cycle_gt_paused(now: float | None = None) -> bool:
    """True when the desk limiter backoff is still in the future.

    Sticky stats_429_count is ignored — a past 429 must not pause after backoff.
    """
    now = current_time() if now is None else float(now)
    try:
        lim = _desk_limiter()
        if lim is None:
            return False
        if float(getattr(lim, "backoff_until", 0) or 0) > now:
            return True
    except Exception as e:
        log.warning("outcomes cycle_gt_paused: %s", safe_err(e))
        return False
    return False


def _outcomes_should_pause(now: float | None = None) -> bool:
    """Backoff or desk cycle GT phase — do not fetch keyless GT either way."""
    now = current_time() if now is None else float(now)
    return cycle_gt_paused(now) or _in_cycle_gt_phase(now)


def drain_jobs(*, now: float | None = None, deadline: float | None = None,
               max_calls: int | None = None, trickle: bool | None = None) -> dict:
    """Fetch pending jobs one at a time. Commits after each. Resumes later.

    trickle=None means: trickle when outside OUTCOMES_WINDOW.
    Pauses on cycle GT 429. Fail open.
    """
    global _trickle_mode
    now = current_time() if now is None else float(now)
    if trickle is None:
        trickle = not gt_api.in_outcomes_window(now)
    _trickle_mode = bool(trickle)
    fetched = 0
    complete = 0
    paused = False
    try:
        _retire_overlimit_jobs()
        in_window = gt_api.in_outcomes_window(now)
        jobs = list_jobs("pending", include_backfill=in_window)
        for job in jobs:
            if deadline is not None and current_time() >= deadline:
                break
            if _outcomes_should_pause():
                paused = True
                _log_fetch_paused()
                break
            tid = job["tid"]
            entry_ts = float(job["entry_ts"])
            if now < entry_ts + READY_AFTER_SEC:
                continue
            payload = fetch_outcome(tid, entry_ts, now=now, deadline=deadline)
            fetched += 1
            if payload and payload.get("complete"):
                _upsert_job(tid, entry_ts, status="complete", now=now, bump_attempt=True)
                complete += 1
            else:
                resolve_failed = bool(payload and payload.get("resolve_failed"))
                cached = get_cached(tid, entry_ts)
                no_pool = resolve_failed or not (
                    (payload or {}).get("pool") or (cached or {}).get("pool")
                )
                err = "no_pool" if no_pool else ("incomplete" if not payload else None)
                attempts = int(job.get("attempts") or 0) + 1
                if no_pool and attempts >= DEAD_NO_POOL_ATTEMPTS:
                    _upsert_job(tid, entry_ts, status="dead", now=now,
                                error=err or "no_pool", bump_attempt=True)
                else:
                    _upsert_job(tid, entry_ts, status="pending", now=now,
                                error=err, bump_attempt=True)
            if max_calls is not None and fetched >= int(max_calls):
                break
    except Exception as e:
        log.warning("outcomes drain failed: %s", safe_err(e))
    finally:
        _trickle_mode = False
    remaining = len(list_jobs("pending"))
    log.info(
        "outcomes drain fetched=%d complete=%d remaining=%d trickle=%s paused=%s",
        fetched, complete, remaining, bool(trickle), paused,
    )
    return {
        "fetched": fetched, "complete": complete, "remaining": remaining,
        "trickle": bool(trickle), "paused": paused,
    }


def tick_fetch(*, now: float | None = None, days: int = 7,
               deadline: float | None = None) -> dict:
    """One worker slice: enqueue, then drain (bulk in-window, else 1 trickle call)."""
    now = current_time() if now is None else float(now)
    try:
        enqueue_from_history(days=days, now=now)
        if _outcomes_should_pause(now):
            _log_fetch_paused()
            return {"fetched": 0, "complete": 0, "remaining": len(list_jobs("pending")),
                    "paused": True}
        if gt_api.in_outcomes_window(now):
            return drain_jobs(now=now, deadline=deadline, trickle=False)
        return drain_jobs(now=now, deadline=deadline, trickle=True, max_calls=1)
    except Exception as e:
        log.warning("outcomes tick failed: %s", safe_err(e))
        return {"fetched": 0, "error": safe_err(e)}


def worker_step() -> dict:
    """One worker slice. Sleeps 30-60s when paused so the in-window loop cannot spin."""
    now = current_time()
    in_window = gt_api.in_outcomes_window(now)
    if in_window:
        result = tick_fetch(now=now, deadline=now + 60.0)
    else:
        result = tick_fetch(now=now, deadline=now + 30.0)
    if result.get("paused"):
        _sleep(pause_sleep_sec())
    elif not in_window:
        _sleep(gt_api.outcomes_spacing_sec(trickle=True))
    return result


def maybe_start_worker() -> threading.Thread | None:
    """Background fetch worker. Fail open. Disabled by OUTCOMES_FETCH_DISABLE=1."""
    if os.environ.get("OUTCOMES_FETCH_DISABLE") == "1":
        return None
    global _worker_thread
    with _worker_lock:
        if _worker_thread is not None and _worker_thread.is_alive():
            return None

        def _loop():
            while True:
                try:
                    worker_step()
                except Exception as e:
                    log.warning("outcomes worker: %s", safe_err(e))
                    _sleep(10.0)

        try:
            _retire_overlimit_jobs()
        except Exception as e:
            log.warning("outcomes retire overlimit failed: %s", safe_err(e))
        t = threading.Thread(target=_loop, name="outcomes-fetch", daemon=True)
        _worker_thread = t
        t.start()
        log.info("outcomes fetch worker started")
        return t
