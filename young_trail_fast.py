"""Paper 60s-mark young_trail_fast ledger.

Copies young_trail entries from deploy onward (same tid + entry price) and
evaluates baseline (-30% SL, 30% trail, 6h) plus tp_50 on every research
sampler mark. Does NOT change young_trail, variants, judging, or live orders.
No Dex/GT calls of its own — uses prices the sampler already fetched.
Fail open. Exceptions through safe_err. Never logs secrets.
"""
from __future__ import annotations

import logging
import os
import pathlib
import sqlite3
import threading
import time

from secret_utils import safe_err

log = logging.getLogger("young_trail_fast")

TICKET_USD = 50.0
SL_PCT = -0.30
TRAIL_PCT = 0.30
WINDOW_SEC = 6 * 3600
LOCK_TIMEOUT_SEC = 0.2
# Same wall-clock end as the live young_trail week (2026-10-20 7:38 AM ET).
DEFAULT_UNTIL_ISO = "2026-10-20T11:38:07Z"

FAST_VARIANTS: list[dict] = [
    {"id": "baseline", "sl_pct": SL_PCT, "trail_pct": TRAIL_PCT,
     "window_sec": WINDOW_SEC, "label": "baseline SL-30 trail30 6h (60s)"},
    {"id": "tp_50", "sl_pct": SL_PCT, "trail_pct": TRAIL_PCT,
     "window_sec": WINDOW_SEC, "tp_pct": 0.50,
     "label": "take-profit +50% (60s)"},
]
VARIANT_BY_ID = {v["id"]: v for v in FAST_VARIANTS}

_tls = threading.local()
_schema_lock = threading.Lock()
_time_fn = time.time


def set_time_fn(fn) -> None:
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


def _close_local() -> None:
    conn = getattr(_tls, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except Exception:
            pass
    _tls.conn = None
    _tls.path = None


def reset() -> None:
    """Close this thread's cached connection. Tests call this."""
    _close_local()


def _schema_step(db: sqlite3.Connection, name: str, fn) -> None:
    """One schema change. Failure is logged and does not skip later steps."""
    try:
        fn()
        db.commit()
    except Exception as e:
        log.warning("young_trail_fast schema %s: %s", name, safe_err(e))
        try:
            db.rollback()
        except Exception:
            pass


def init_schema(db: sqlite3.Connection | None = None) -> None:
    """Idempotent. Each step commits on its own (live-DB safe, lesson from #60)."""
    conn = db if db is not None else _connect()
    _schema_step(conn, "young_trail_fast", lambda: conn.execute("""
    CREATE TABLE IF NOT EXISTS young_trail_fast (
      tid TEXT NOT NULL,
      variant TEXT NOT NULL,
      ticker TEXT,
      chain TEXT,
      entry_ts REAL NOT NULL,
      entry_price REAL NOT NULL,
      peak REAL NOT NULL,
      peak_ts REAL,
      time_to_peak_sec REAL,
      last_price REAL,
      last_mark_ts REAL,
      exit_ts REAL,
      exit_price REAL,
      exit_reason TEXT,
      pnl_usd REAL,
      PRIMARY KEY (tid, variant)
    )"""))
    _schema_step(conn, "idx_ytf_exit", lambda: conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ytf_exit ON young_trail_fast(exit_ts)"
    ))
    _schema_step(conn, "idx_ytf_variant", lambda: conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ytf_variant ON young_trail_fast(variant, exit_ts)"
    ))
    _schema_step(conn, "young_trail_fast_meta", lambda: conn.execute("""
    CREATE TABLE IF NOT EXISTS young_trail_fast_meta (
      key TEXT PRIMARY KEY,
      value TEXT
    )"""))


def _connect() -> sqlite3.Connection:
    """Per-thread connection. Default check_same_thread (this thread only)."""
    path = str(db_path())
    conn = getattr(_tls, "conn", None)
    if conn is not None and getattr(_tls, "path", None) == path:
        return conn
    _close_local()
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=LOCK_TIMEOUT_SEC)
    conn.row_factory = sqlite3.Row
    with _schema_lock:
        init_schema(conn)
    _tls.conn = conn
    _tls.path = path
    return conn


def enabled() -> bool:
    raw = os.environ.get("YOUNG_TRAIL_FAST_ENABLED")
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
            if fmt == "%Y-%m-%dT%H:%M:%SZ":
                import calendar
                return float(calendar.timegm(t))
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
            "SELECT value FROM young_trail_fast_meta WHERE key = ?", (key,),
        ).fetchone()
        return None if row is None else row["value"]
    except Exception as e:
        log.warning("young_trail_fast meta read: %s", safe_err(e))
        return None


def _meta_set(key: str, value: str) -> None:
    try:
        db = _connect()
        db.execute(
            """INSERT INTO young_trail_fast_meta (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )
        db.commit()
    except Exception as e:
        log.warning("young_trail_fast meta write: %s", safe_err(e))


def deploy_ts(now: float | None = None) -> float:
    """First-use stamp. Only young_trail entries at/after this are copied."""
    stored = _meta_get("deploy_ts")
    if stored:
        try:
            return float(stored)
        except (TypeError, ValueError):
            pass
    ts = current_time() if now is None else float(now)
    _meta_set("deploy_ts", str(ts))
    return ts


def until_ts(now: float | None = None) -> float:
    """Same experiment end as young_trail (2026-10-20 7:38 AM ET)."""
    raw = os.environ.get("YOUNG_TRAIL_FAST_UNTIL")
    parsed = _parse_until(raw) if raw else None
    if parsed is not None:
        return parsed
    stored = _meta_get("until_ts")
    if stored:
        try:
            return float(stored)
        except (TypeError, ValueError):
            pass
    try:
        import young_trail
        ts = float(young_trail.until_ts(now))
        _meta_set("until_ts", str(ts))
        return ts
    except Exception as e:
        log.warning("young_trail_fast until from young_trail: %s", safe_err(e))
    fallback = _parse_until(DEFAULT_UNTIL_ISO) or 0.0
    _meta_set("until_ts", str(fallback))
    return fallback


def accepting_entries(now: float | None = None) -> bool:
    ts = current_time() if now is None else float(now)
    return enabled() and ts < until_ts(ts)


def pnl_usd(exit_price: float, entry_price: float, ticket: float = TICKET_USD) -> float:
    if not entry_price:
        return 0.0
    return float(ticket) * (float(exit_price) / float(entry_price) - 1.0)


def _num(value, default=None):
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _price_of(mark) -> float | None:
    if mark is None:
        return None
    if isinstance(mark, dict):
        for key in ("price_usd", "price", "priceUsd"):
            px = _num(mark.get(key))
            if px is not None and px >= 0:
                return px
        return None
    px = _num(mark)
    return px if px is not None and px >= 0 else None


def _row_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


def _source_entries() -> list[dict]:
    try:
        import young_trail
        rows = list(young_trail.open_rows())
        rows.extend(young_trail.closed_rows(limit=None))
        return [r for r in rows if r.get("tid") and _num(r.get("entry_price"), 0) > 0]
    except Exception as e:
        log.warning("young_trail_fast source entries: %s", safe_err(e))
        return []


def sync_entries(now: float | None = None) -> int:
    """Copy young_trail tids/entry prices from deploy onward. Fail open."""
    if not enabled():
        return 0
    ts = current_time() if now is None else float(now)
    if not accepting_entries(ts):
        return 0
    since = deploy_ts()
    n = 0
    try:
        db = _connect()
        for src in _source_entries():
            entry_ts = _num(src.get("entry_ts"))
            if entry_ts is None or entry_ts < since - 1e-9:
                continue
            px = float(src["entry_price"])
            for rule in FAST_VARIANTS:
                cur = db.execute(
                    """INSERT OR IGNORE INTO young_trail_fast (
                         tid, variant, ticker, chain, entry_ts, entry_price,
                         peak, peak_ts, time_to_peak_sec, last_price, last_mark_ts
                       ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0.0, ?, ?)""",
                    (
                        src["tid"], rule["id"], src.get("ticker"), src.get("chain"),
                        float(entry_ts), px, px, float(entry_ts), px, float(entry_ts),
                    ),
                )
                if cur.rowcount:
                    n += 1
        db.commit()
        if n:
            log.info("young_trail_fast sync entries=%d paper=1", n)
    except Exception as e:
        log.warning("young_trail_fast sync: %s", safe_err(e))
    return n


def _never_marked_after_entry(row) -> bool:
    """True when last_mark_ts is still the entry stamp (no post-entry mark)."""
    entry_ts = _num(row["entry_ts"])
    last_mark_ts = _num(row["last_mark_ts"])
    if entry_ts is None or last_mark_ts is None:
        return True
    return last_mark_ts <= entry_ts + 1e-9


def _close(db: sqlite3.Connection, tid: str, variant: str, exit_ts: float,
           exit_price: float, reason: str, peak: float, peak_ts: float,
           time_to_peak_sec: float, *, unmarked: bool = False) -> None:
    entry = db.execute(
        "SELECT entry_price FROM young_trail_fast WHERE tid = ? AND variant = ?",
        (tid, variant),
    ).fetchone()
    if entry is None:
        return
    if unmarked:
        reason = "unmarked"
        pnl = None
    else:
        pnl = pnl_usd(exit_price, float(entry["entry_price"]))
    db.execute(
        """UPDATE young_trail_fast
           SET peak = ?, peak_ts = ?, time_to_peak_sec = ?,
               last_price = ?, last_mark_ts = ?,
               exit_ts = ?, exit_price = ?, exit_reason = ?, pnl_usd = ?
           WHERE tid = ? AND variant = ? AND exit_ts IS NULL""",
        (float(peak), float(peak_ts), float(time_to_peak_sec),
         float(exit_price), float(exit_ts),
         float(exit_ts), float(exit_price), reason, pnl, tid, variant),
    )
    if pnl is None:
        log.info(
            "young_trail_fast exit tid=%s variant=%s reason=%s pnl_usd=null paper=1",
            tid, variant, reason,
        )
    else:
        log.info(
            "young_trail_fast exit tid=%s variant=%s reason=%s pnl_usd=%.2f paper=1",
            tid, variant, reason, pnl,
        )


def on_marks(prices: dict | None, now: float | None = None) -> dict:
    """Apply sampler prices to still-open fast rows. No HTTP. Fail open."""
    prices = prices or {}
    ts = current_time() if now is None else float(now)
    marked = 0
    closed = 0
    try:
        from young_trail_variants import decide_variant_tick
        db = _connect()
        rows = db.execute(
            "SELECT * FROM young_trail_fast WHERE exit_ts IS NULL",
        ).fetchall()
        for row in rows:
            rule = VARIANT_BY_ID.get(row["variant"])
            if not rule:
                continue
            tid = row["tid"]
            import price_guard
            entry_price = float(row["entry_price"])
            last_px = _num(row["last_price"], None) or entry_price
            px = price_guard.accept_price(
                _price_of(prices.get(tid)),
                entry=entry_price, prev=last_px, tid=tid, now=ts,
            )
            peak = _num(row["peak"], entry_price) or entry_price
            peak_ts = _num(row["peak_ts"], row["entry_ts"]) or float(row["entry_ts"])
            time_to_peak = _num(row["time_to_peak_sec"], 0.0) or 0.0
            if px is not None and px > peak + 1e-15:
                peak = px
                peak_ts = ts
                time_to_peak = max(0.0, ts - float(row["entry_ts"]))
            decision, state = decide_variant_tick(
                rule,
                entry_price=entry_price,
                peak=peak,
                price=px,
                entry_ts=float(row["entry_ts"]),
                now=ts,
                last_price=_num(row["last_price"], None),
            )
            peak = float(state.get("peak") or peak)
            if decision:
                reason, exit_px = decision
                unmarked = (
                    reason == "time"
                    and px is None
                    and _never_marked_after_entry(row)
                )
                _close(db, tid, row["variant"], ts, exit_px, reason,
                       peak, peak_ts, time_to_peak, unmarked=unmarked)
                closed += 1
                continue
            if px is None:
                continue
            db.execute(
                """UPDATE young_trail_fast
                   SET peak = ?, peak_ts = ?, time_to_peak_sec = ?,
                       last_price = ?, last_mark_ts = ?
                   WHERE tid = ? AND variant = ? AND exit_ts IS NULL""",
                (peak, peak_ts, time_to_peak, float(px), ts, tid, row["variant"]),
            )
            marked += 1
        db.commit()
        if rows:
            log.info("young_trail_fast mark: %d marked, %d closed paper=1", marked, closed)
    except Exception as e:
        log.warning("young_trail_fast marks: %s", safe_err(e))
    return {"marked": marked, "closed": closed}


def on_sampler_tick(prices: dict | None = None, now: float | None = None) -> dict:
    """Sampler hook: copy new young_trail entries, then mark. No Dex."""
    empty = {"entries": 0, "marked": 0, "closed": 0}
    try:
        if not enabled():
            return empty
        ts = current_time() if now is None else float(now)
        until_ts(ts)
        deploy_ts()
        n = sync_entries(ts)
        marks = on_marks(prices or {}, ts)
        return {"entries": n, **marks}
    except Exception as e:
        log.warning("young_trail_fast sampler: %s", safe_err(e))
        return empty


def open_rows(variant: str | None = None) -> list[dict]:
    try:
        sql = "SELECT * FROM young_trail_fast WHERE exit_ts IS NULL"
        args: tuple = ()
        if variant:
            sql += " AND variant = ?"
            args = (variant,)
        sql += " ORDER BY entry_ts"
        return [_row_dict(r) for r in _connect().execute(sql, args).fetchall()]
    except Exception as e:
        log.warning("young_trail_fast open: %s", safe_err(e))
        return []


def closed_rows(variant: str | None = None, limit: int | None = 50) -> list[dict]:
    try:
        sql = "SELECT * FROM young_trail_fast WHERE exit_ts IS NOT NULL"
        args: list = []
        if variant:
            sql += " AND variant = ?"
            args.append(variant)
        sql += " ORDER BY exit_ts DESC"
        if limit is not None:
            sql += " LIMIT ?"
            args.append(int(limit))
        return [_row_dict(r) for r in _connect().execute(sql, tuple(args)).fetchall()]
    except Exception as e:
        log.warning("young_trail_fast closed: %s", safe_err(e))
        return []


def _variant_summary(variant: str) -> dict:
    try:
        opens = open_rows(variant)
        closes = closed_rows(variant, limit=None)
        counted = [
            c for c in closes
            if c.get("exit_reason") != "unmarked" and c.get("pnl_usd") is not None
        ]
        wins = sum(1 for c in counted if (c.get("pnl_usd") or 0) > 0)
        n = len(counted)
        realized = sum(c.get("pnl_usd") or 0 for c in counted)
        return {
            "variant": variant,
            "open_count": len(opens),
            "closed_count": n,
            "winning_trades": wins,
            "losing_trades": n - wins,
            "win_rate": (wins / n) if n else 0.0,
            "realized_pnl_usd": realized,
        }
    except Exception as e:
        log.warning("young_trail_fast summary %s: %s", variant, safe_err(e))
        return {
            "variant": variant, "open_count": 0, "closed_count": 0,
            "winning_trades": 0, "losing_trades": 0, "win_rate": 0.0,
            "realized_pnl_usd": 0.0,
        }


def comparison(limit: int = 80) -> list[dict]:
    """Same-entry P&L: 60s fast vs per-cycle young_trail (+ cycle tp_50)."""
    try:
        db = _connect()
        rows = db.execute(
            """SELECT DISTINCT tid FROM young_trail_fast ORDER BY entry_ts DESC"""
        ).fetchall()
        tids = [r["tid"] for r in rows][: max(0, int(limit))]
        if not tids:
            return []
        cycle_by: dict[str, dict] = {}
        try:
            import young_trail
            for r in young_trail.open_rows() + young_trail.closed_rows(limit=None):
                cycle_by[r["tid"]] = r
        except Exception as e:
            log.warning("young_trail_fast cycle join: %s", safe_err(e))
        cycle_tp: dict[str, dict] = {}
        try:
            import young_trail_variants
            for r in young_trail_variants.closed_rows(limit=None) + []:
                if r.get("variant") == "tp_50":
                    cycle_tp[r["tid"]] = r
            # open cycle tp_50
            ydb = young_trail_variants._db()
            for r in ydb.execute(
                "SELECT * FROM young_trail_variants WHERE variant = 'tp_50'"
            ).fetchall():
                cycle_tp.setdefault(r["tid"], {k: r[k] for k in r.keys()})
        except Exception as e:
            log.warning("young_trail_fast cycle tp_50: %s", safe_err(e))
        out = []
        for tid in tids:
            by_var = {}
            for r in db.execute(
                "SELECT * FROM young_trail_fast WHERE tid = ?", (tid,),
            ).fetchall():
                by_var[r["variant"]] = _row_dict(r)
            base = by_var.get("baseline") or {}
            tp = by_var.get("tp_50") or {}
            cycle = cycle_by.get(tid) or {}
            ctp = cycle_tp.get(tid) or {}
            fast_pnl = _num(base.get("pnl_usd"))
            cycle_pnl = _num(cycle.get("pnl_usd"))
            delta = None
            if fast_pnl is not None and cycle_pnl is not None:
                delta = fast_pnl - cycle_pnl
            out.append({
                "tid": tid,
                "ticker": base.get("ticker") or cycle.get("ticker"),
                "chain": base.get("chain") or cycle.get("chain"),
                "entry_ts": base.get("entry_ts") or cycle.get("entry_ts"),
                "entry_price": base.get("entry_price") or cycle.get("entry_price"),
                "cycle_exit_ts": cycle.get("exit_ts"),
                "cycle_exit_reason": cycle.get("exit_reason"),
                "cycle_pnl_usd": cycle_pnl,
                "cycle_peak": cycle.get("peak"),
                "fast_baseline_exit_ts": base.get("exit_ts"),
                "fast_baseline_exit_reason": base.get("exit_reason"),
                "fast_baseline_pnl_usd": fast_pnl,
                "fast_baseline_peak": base.get("peak"),
                "fast_baseline_time_to_peak_sec": base.get("time_to_peak_sec"),
                "fast_tp50_exit_ts": tp.get("exit_ts"),
                "fast_tp50_exit_reason": tp.get("exit_reason"),
                "fast_tp50_pnl_usd": _num(tp.get("pnl_usd")),
                "cycle_tp50_pnl_usd": _num(ctp.get("pnl_usd")),
                "delta_vs_cycle_usd": delta,
            })
        return out
    except Exception as e:
        log.warning("young_trail_fast comparison: %s", safe_err(e))
        return []


def ops_payload() -> dict:
    """JSON for GET /ops/young_trail_fast. Paper/simulated; no live orders."""
    try:
        now = current_time()
        until = until_ts(now) if enabled() else None
        return {
            "paper": True,
            "simulated": True,
            "live_orders": False,
            "enabled": enabled(),
            "accepting_entries": accepting_entries(now),
            "until_ts": until,
            "until_iso": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until)) if until else None,
            "ticket_usd": TICKET_USD,
            "mark_interval_sec": 60,
            "caveat": "60s Dex sampler marks; same entries as young_trail from deploy onward.",
            "rules": {
                "sl_pct": SL_PCT,
                "trail_pct": TRAIL_PCT,
                "window_hours": WINDOW_SEC / 3600.0,
                "variants": [v["id"] for v in FAST_VARIANTS],
            },
            "summary": {
                "baseline": _variant_summary("baseline"),
                "tp_50": _variant_summary("tp_50"),
            },
            "comparison": comparison(limit=80),
            "open_positions": open_rows(),
            "closed_trades": closed_rows(limit=80),
        }
    except Exception as e:
        log.warning("young_trail_fast ops: %s", safe_err(e))
        return {
            "paper": True,
            "simulated": True,
            "live_orders": False,
            "error": "unavailable",
            "summary": {
                "baseline": _variant_summary("baseline"),
                "tp_50": _variant_summary("tp_50"),
            },
            "comparison": [],
            "open_positions": [],
            "closed_trades": [],
        }
