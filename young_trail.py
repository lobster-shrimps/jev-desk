"""
Young-trail paper ledger — a week-long SIMULATED experiment.

Every token that free-passes with age_minutes < 60 is paper-bought once (first
tid seen) at $50, then marked each desk cycle. Exits match stop-study
semantics in outcomes.simulate(trail=True) minus take-profit: hard stop -30%
from entry, 30% trail from peak (price <= peak * 0.7), time exit at 6h;
whichever first.

Does NOT affect judging, picks, thresholds, cutoff, ticket size, or live
orders. Own DexScreener lookups (priceUsd, tokens/v1), never GT. Fail open.
Exceptions go through safe_err. Never logs secrets.
"""
from __future__ import annotations

import logging
import os
import pathlib
import sqlite3
import time

import requests

from secret_utils import safe_err

log = logging.getLogger("young_trail")

DEX_TOKENS = "https://api.dexscreener.com/tokens/v1"
UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) desk/young_trail",
    "Accept": "application/json",
}
DEX_CHAIN = {1399811149: "solana", 56: "bsc", 8453: "base", 4663: "robinhood", 143: "monad"}

TICKET_USD = 50.0
SL_PCT = -0.30
TRAIL_PCT = 0.30
WINDOW_SEC = 6 * 3600
EXPERIMENT_SEC = 7 * 86400
HTTP_TIMEOUT = 20
LOCK_TIMEOUT_SEC = 0.2
DEX_CAP_DEFAULT = 20
DEX_SPACING_SEC = 0.25
YOUNG_MAX_MINUTES = 60

_conn: sqlite3.Connection | None = None
_conn_path: str | None = None
_get_fn = None
_sleep_fn = None
_time_fn = time.time
_last_dex_call = 0.0


def set_get_fn(fn):
    """Inject HTTP get(url, timeout=, headers=) for tests. None restores."""
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


def db_path() -> pathlib.Path:
    override = os.environ.get("OUTCOMES_DB")
    if override:
        return pathlib.Path(override)
    outbox = pathlib.Path(os.environ.get("DESK_OUTBOX", "outbox"))
    return outbox / "outcomes.db"


def reset() -> None:
    """Close the cached connection. Tests call this."""
    global _conn, _conn_path, _last_dex_call
    if _conn is not None:
        try:
            _conn.close()
        except Exception:
            pass
    _conn = None
    _conn_path = None
    _last_dex_call = 0.0


def _sleep(seconds: float) -> None:
    fn = _sleep_fn
    if fn is not None:
        fn(seconds)
        return
    time.sleep(max(0.0, float(seconds)))


def _http_get(url: str, timeout=HTTP_TIMEOUT):
    fn = _get_fn or requests.get
    return fn(url, timeout=timeout, headers=UA)


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
    CREATE TABLE IF NOT EXISTS young_trail (
      tid TEXT PRIMARY KEY,
      ticker TEXT,
      chain TEXT,
      entry_ts REAL NOT NULL,
      entry_price REAL NOT NULL,
      peak REAL NOT NULL,
      last_price REAL,
      last_mark_ts REAL,
      exit_ts REAL,
      exit_price REAL,
      exit_reason TEXT,
      pnl_usd REAL
    );
    CREATE INDEX IF NOT EXISTS idx_young_trail_exit ON young_trail(exit_ts);
    CREATE TABLE IF NOT EXISTS young_trail_meta (
      key TEXT PRIMARY KEY,
      value TEXT
    );
    """)
    db.commit()


def enabled() -> bool:
    raw = os.environ.get("YOUNG_TRAIL_ENABLED")
    if raw is None or raw == "":
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def _parse_until(raw: str) -> float | None:
    s = (raw or "").strip()
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        pass
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S", "%Y-%m-%d"):
        try:
            t = time.strptime(s, fmt)
            ts = time.mktime(t)
            if fmt == "%Y-%m-%d":
                ts += 86400 - 1
            return float(ts)
        except ValueError:
            continue
    return None


def _meta_get(key: str) -> str | None:
    try:
        row = _connect().execute(
            "SELECT value FROM young_trail_meta WHERE key = ?", (key,),
        ).fetchone()
        return None if row is None else row["value"]
    except Exception as e:
        log.warning("young_trail meta read: %s", safe_err(e))
        return None


def _meta_set(key: str, value: str) -> None:
    try:
        db = _connect()
        db.execute(
            """INSERT INTO young_trail_meta (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )
        db.commit()
    except Exception as e:
        log.warning("young_trail meta write: %s", safe_err(e))


def until_ts(now: float | None = None) -> float:
    """Experiment end. YOUNG_TRAIL_UNTIL env wins; else persist now+7d on first use."""
    raw = os.environ.get("YOUNG_TRAIL_UNTIL")
    parsed = _parse_until(raw) if raw else None
    if parsed is not None:
        return parsed
    stored = _meta_get("until_ts")
    if stored:
        try:
            return float(stored)
        except (TypeError, ValueError):
            pass
    ts = (current_time() if now is None else float(now)) + EXPERIMENT_SEC
    _meta_set("until_ts", str(ts))
    _meta_set("deploy_ts", str(current_time() if now is None else float(now)))
    return ts


def accepting_entries(now: float | None = None) -> bool:
    ts = current_time() if now is None else float(now)
    return enabled() and ts < until_ts(ts)


def pnl_usd(exit_price: float, entry_price: float, ticket: float = TICKET_USD) -> float:
    if not entry_price:
        return 0.0
    return float(ticket) * (float(exit_price) / float(entry_price) - 1.0)


def decide_exit(entry_price: float, peak: float, price: float | None,
                entry_ts: float, now: float) -> tuple[str, float] | None:
    """Return (reason, exit_price) or None. Peak should include this tick.

    SL -30% from entry, trail 30% from peak (price <= peak * 0.7), time at 6h.
    Price stops beat the time stop in the same tick (stopsim order).
    """
    sl_price = float(entry_price) * (1.0 + SL_PCT)
    trail_price = float(peak) * (1.0 - TRAIL_PCT)
    if price is not None and price > 0:
        px = float(price)
        hit_sl = px <= sl_price
        hit_trail = px <= trail_price
        if hit_sl or hit_trail:
            if hit_trail and trail_price > sl_price + 1e-12:
                return "trail", px
            return "sl", px
        if now >= float(entry_ts) + WINDOW_SEC:
            return "time", px
        return None
    if now >= float(entry_ts) + WINDOW_SEC:
        fallback = float(peak or entry_price)
        return "time", fallback
    return None


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


def _price_from_token(t: dict | None) -> float | None:
    if not isinstance(t, dict):
        return None
    for key in ("price_usd", "price", "priceUsd"):
        v = t.get(key)
        if v is None or v == "":
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            return f
    return None


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


def _pair_price_usd(pair: dict | None) -> float | None:
    if not isinstance(pair, dict):
        return None
    for key in ("priceUsd", "price_usd", "price"):
        v = pair.get(key)
        if v is None or v == "":
            continue
        try:
            f = float(v)
        except (TypeError, ValueError):
            continue
        if f > 0:
            return f
    return None


def fetch_dex_price(tid: str) -> float | None:
    """DexScreener /tokens/v1/{chain}/{addr} highest-liq pair priceUsd. No GT."""
    addr, net = parse_tid(tid)
    if not addr:
        return None
    chain = DEX_CHAIN.get(net) if net is not None else None
    if not chain:
        return None
    url = f"{DEX_TOKENS}/{chain}/{addr}"
    try:
        global _last_dex_call
        spacing = DEX_SPACING_SEC
        wait = (_last_dex_call + spacing) - current_time()
        if wait > 0:
            _sleep(wait)
        resp = _http_get(url, timeout=HTTP_TIMEOUT)
        _last_dex_call = current_time()
        status = getattr(resp, "status_code", None)
        if status != 200:
            log.info("young_trail dex HTTP %s", status)
            return None
        pairs = resp.json()
        return _pair_price_usd(_highest_liq_pair(pairs))
    except Exception as e:
        log.warning("young_trail dex failed: %s", safe_err(e))
        return None


def _dex_cap() -> int:
    raw = os.environ.get("YOUNG_TRAIL_DEX_CAP")
    if raw is None or raw == "":
        return DEX_CAP_DEFAULT
    try:
        return max(0, int(raw))
    except (TypeError, ValueError):
        return DEX_CAP_DEFAULT


def fetch_dex_prices(tids: list[str], *, cap: int | None = None) -> dict[str, float]:
    out: dict[str, float] = {}
    limit = _dex_cap() if cap is None else max(0, int(cap))
    seen = set()
    n = 0
    for tid in tids:
        if not tid or tid in seen:
            continue
        seen.add(tid)
        if n >= limit:
            break
        n += 1
        px = fetch_dex_price(tid)
        if px:
            out[tid] = px
    return out


def _row_dict(row: sqlite3.Row) -> dict:
    return {
        "tid": row["tid"],
        "ticker": row["ticker"],
        "chain": row["chain"],
        "entry_ts": row["entry_ts"],
        "entry_price": row["entry_price"],
        "peak": row["peak"],
        "last_price": row["last_price"],
        "last_mark_ts": row["last_mark_ts"],
        "exit_ts": row["exit_ts"],
        "exit_price": row["exit_price"],
        "exit_reason": row["exit_reason"],
        "pnl_usd": row["pnl_usd"],
    }


def has_tid(tid: str) -> bool:
    if not tid:
        return False
    try:
        row = _connect().execute(
            "SELECT 1 FROM young_trail WHERE tid = ?", (tid,),
        ).fetchone()
        return bool(row)
    except Exception as e:
        log.warning("young_trail has_tid: %s", safe_err(e))
        return True  # fail closed on lookup so we do not double-enter


def enter(tid: str, token: dict | None, price: float, now: float | None = None) -> bool:
    """Insert a paper entry. First tid wins (INSERT OR IGNORE). No live orders."""
    if not tid or not price or price <= 0:
        return False
    if not accepting_entries(now):
        return False
    ts = current_time() if now is None else float(now)
    token = token or {}
    ticker = token.get("ticker") or "?"
    chain = token.get("chain")
    if not chain:
        _, net = parse_tid(tid)
        chain = DEX_CHAIN.get(net) if net is not None else token.get("net")
    try:
        db = _connect()
        cur = db.execute(
            """INSERT OR IGNORE INTO young_trail
               (tid, ticker, chain, entry_ts, entry_price, peak, last_price, last_mark_ts)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (tid, ticker, chain, ts, float(price), float(price), float(price), ts),
        )
        db.commit()
        if cur.rowcount:
            log.info(
                "young_trail entry tid=%s ticker=%s price=%s ticket=%s paper=1",
                tid, ticker, price, TICKET_USD,
            )
            return True
        return False
    except Exception as e:
        log.warning("young_trail enter: %s", safe_err(e))
        return False


def open_rows() -> list[dict]:
    try:
        rows = _connect().execute(
            "SELECT * FROM young_trail WHERE exit_ts IS NULL ORDER BY entry_ts",
        ).fetchall()
        return [_row_dict(r) for r in rows]
    except Exception as e:
        log.warning("young_trail open: %s", safe_err(e))
        return []


def closed_rows(limit: int | None = 50) -> list[dict]:
    try:
        sql = "SELECT * FROM young_trail WHERE exit_ts IS NOT NULL ORDER BY exit_ts DESC"
        args: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            args = (int(limit),)
        rows = _connect().execute(sql, args).fetchall()
        return [_row_dict(r) for r in rows]
    except Exception as e:
        log.warning("young_trail closed: %s", safe_err(e))
        return []


def _close_row(tid: str, exit_ts: float, exit_price: float, reason: str,
               peak: float) -> dict | None:
    try:
        db = _connect()
        row = db.execute("SELECT * FROM young_trail WHERE tid = ?", (tid,)).fetchone()
        if row is None or row["exit_ts"] is not None:
            return None
        entry_price = float(row["entry_price"])
        pnl = pnl_usd(exit_price, entry_price)
        db.execute(
            """UPDATE young_trail SET peak = ?, last_price = ?, last_mark_ts = ?,
               exit_ts = ?, exit_price = ?, exit_reason = ?, pnl_usd = ?
               WHERE tid = ? AND exit_ts IS NULL""",
            (float(peak), float(exit_price), float(exit_ts),
             float(exit_ts), float(exit_price), reason, pnl, tid),
        )
        db.commit()
        log.info(
            "young_trail exit tid=%s ticker=%s reason=%s pnl_usd=%.2f paper=1",
            tid, row["ticker"], reason, pnl,
        )
        return {
            **_row_dict(row),
            "peak": float(peak),
            "last_price": float(exit_price),
            "last_mark_ts": float(exit_ts),
            "exit_ts": float(exit_ts),
            "exit_price": float(exit_price),
            "exit_reason": reason,
            "pnl_usd": pnl,
        }
    except Exception as e:
        log.warning("young_trail close: %s", safe_err(e))
        return None


def mark_open(prices: dict[str, float] | None = None, now: float | None = None) -> dict:
    """Mark open paper positions. Realized P&L only on close. Fail open."""
    prices = prices or {}
    ts = current_time() if now is None else float(now)
    marked = 0
    closed = 0
    stale = 0
    try:
        opens = open_rows()
    except Exception as e:
        log.warning("young_trail mark: %s", safe_err(e))
        return {"marked": 0, "closed": 0, "stale": 0}
    for pos in opens:
        tid = pos["tid"]
        px = prices.get(tid)
        try:
            peak = float(pos["peak"] or pos["entry_price"])
            if px is not None and px > 0:
                peak = max(peak, float(px))
            decision = decide_exit(
                float(pos["entry_price"]), peak, px, float(pos["entry_ts"]), ts,
            )
            if decision:
                reason, exit_px = decision
                if _close_row(tid, ts, exit_px, reason, peak):
                    closed += 1
                continue
            if px is None or px <= 0:
                stale += 1
                continue
            db = _connect()
            db.execute(
                """UPDATE young_trail SET peak = ?, last_price = ?, last_mark_ts = ?
                   WHERE tid = ? AND exit_ts IS NULL""",
                (peak, float(px), ts, tid),
            )
            db.commit()
            marked += 1
        except Exception as e:
            log.warning("young_trail mark tid=%s: %s", tid, safe_err(e))
    if opens:
        log.info("young_trail mark: %d marked, %d closed, %d stale paper=1",
                 marked, closed, stale)
    return {"marked": marked, "closed": closed, "stale": stale}


def consider_entries(candidates: list[dict], prices: dict[str, float] | None = None,
                     now: float | None = None) -> int:
    """Paper-enter young free-passers (age < 60), first tid only."""
    if not accepting_entries(now):
        return 0
    prices = prices or {}
    n = 0
    for t in candidates or []:
        if not isinstance(t, dict):
            continue
        tid = t.get("tid")
        if not tid:
            continue
        try:
            age = float(t.get("age_minutes") or 0)
        except (TypeError, ValueError):
            age = 0.0
        if age >= YOUNG_MAX_MINUTES:
            continue
        px = prices.get(tid) or _price_from_token(t)
        if not px:
            continue
        if enter(tid, t, px, now):
            n += 1
    return n


def on_cycle(stats=None, tokens=None, now: float | None = None) -> dict:
    """Cycle hook: optional new entries, then MTM. Never raises to the desk."""
    empty = {"entries": 0, "marked": 0, "closed": 0, "stale": 0}
    try:
        if not enabled():
            return empty
        ts = current_time() if now is None else float(now)
        until_ts(ts)  # persist default end date on first live cycle
        tokens = list(tokens or [])
        stats = stats or {}
        prices: dict[str, float] = {}
        by_tid: dict[str, dict] = {}
        for t in tokens:
            if not isinstance(t, dict) or not t.get("tid"):
                continue
            by_tid[t["tid"]] = t
            px = _price_from_token(t)
            if px:
                prices[t["tid"]] = px
        missing = [r["tid"] for r in open_rows() if r["tid"] not in prices]
        candidates: list[dict] = []
        if accepting_entries(ts):
            for y in stats.get("young_free") or []:
                if not isinstance(y, dict) or not y.get("tid"):
                    continue
                try:
                    age = float(y.get("age_minutes") or 0)
                except (TypeError, ValueError):
                    age = 0.0
                if age >= YOUNG_MAX_MINUTES:
                    continue
                tid = y["tid"]
                t = by_tid.get(tid) or y
                px = prices.get(tid) or _price_from_token(t)
                if px:
                    prices[tid] = px
                elif tid not in missing:
                    missing.append(tid)
                candidates.append(t if t.get("tid") else y)
        if missing:
            prices.update(fetch_dex_prices(missing))
        n_entries = consider_entries(candidates, prices, ts) if candidates else 0
        marks = mark_open(prices, ts)
        return {"entries": n_entries, **marks}
    except Exception as e:
        log.warning("young_trail on_cycle: %s", safe_err(e))
        return empty


def summary() -> dict:
    """Realized P&L counts closed rows only."""
    try:
        opens = open_rows()
        closes = closed_rows(limit=None)
        wins = sum(1 for c in closes if (c.get("pnl_usd") or 0) > 0)
        losses = sum(1 for c in closes if (c.get("pnl_usd") or 0) < 0)
        n = len(closes)
        realized = sum(c.get("pnl_usd") or 0 for c in closes)
        return {
            "open_count": len(opens),
            "closed_count": n,
            "winning_trades": wins,
            "losing_trades": losses,
            "win_rate": (wins / n) if n else 0.0,
            "realized_pnl_usd": realized,
        }
    except Exception as e:
        log.warning("young_trail summary: %s", safe_err(e))
        return {
            "open_count": 0, "closed_count": 0, "winning_trades": 0,
            "losing_trades": 0, "win_rate": 0.0, "realized_pnl_usd": 0.0,
        }


def ops_payload() -> dict:
    """JSON for GET /ops/young_trail. Paper/simulated; no live orders."""
    try:
        now = current_time()
        until = until_ts(now) if enabled() else None
        sm = summary()
        return {
            "paper": True,
            "simulated": True,
            "live_orders": False,
            "enabled": enabled(),
            "accepting_entries": accepting_entries(now),
            "until_ts": until,
            "until_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until)) if until else None,
            "ticket_usd": TICKET_USD,
            "rules": {
                "sl_pct": SL_PCT,
                "trail_pct": TRAIL_PCT,
                "window_hours": WINDOW_SEC / 3600.0,
                "young_max_minutes": YOUNG_MAX_MINUTES,
            },
            "open_positions": open_rows(),
            "closed_trades": closed_rows(limit=50),
            "summary": sm,
        }
    except Exception as e:
        log.warning("young_trail ops: %s", safe_err(e))
        return {
            "paper": True,
            "simulated": True,
            "live_orders": False,
            "error": "unavailable",
            "open_positions": [],
            "closed_trades": [],
            "summary": {
                "open_count": 0, "closed_count": 0, "win_rate": 0.0,
                "realized_pnl_usd": 0.0, "winning_trades": 0, "losing_trades": 0,
            },
        }
