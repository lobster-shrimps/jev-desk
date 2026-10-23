"""Log-only legacy exit track after a tp_50 close.

Once a main-desk shadow fill (or pick_confirm row) closes at +50%, keep
marking on the existing FOMO cadence until the legacy rule resolves:
+100% take-profit, -30% stop-loss, or 6h from entry. Fetch failures retry
on the next mark; past 6h plus a grace period the row is 'unresolved'.

Does not change fills, tickets, cutoffs, or any trading decision.
Fail-open. Exception text via safe_err only.
"""
from __future__ import annotations

import logging
import os
import pathlib
import sqlite3
import threading
import time

from secret_utils import safe_err

log = logging.getLogger("legacy_shadow_track")

LEGACY_TP_PCT = 100.0
LEGACY_SL_PCT = -30.0
LEGACY_MAX_HOLD_HOURS = 6.0
LEGACY_GRACE_HOURS = 1.0
TICKET_USD = 50.0
SOURCE_LEDGER = "ledger"
REASON_TP = "take_profit"
REASON_SL = "stop_loss"
REASON_TIME = "time_stop"
REASON_UNRESOLVED = "unresolved"
LOCK_TIMEOUT_SEC = 0.2

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
    return pathlib.Path(os.environ.get("DESK_OUTBOX", "outbox")) / "outcomes.db"


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
    _close_local()


def _connect() -> sqlite3.Connection:
    path = str(db_path())
    conn = getattr(_tls, "conn", None)
    if conn is not None and getattr(_tls, "path", None) == path:
        return conn
    _close_local()
    pathlib.Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=LOCK_TIMEOUT_SEC)
    conn.row_factory = sqlite3.Row
    with _schema_lock:
        _init_schema(conn)
    _tls.conn = conn
    _tls.path = path
    return conn


def _schema_step(db, name, fn) -> None:
    try:
        fn()
        db.commit()
    except Exception as e:
        log.warning("legacy_shadow schema %s: %s", name, safe_err(e))
        try:
            db.rollback()
        except Exception:
            pass


def _init_schema(db: sqlite3.Connection) -> None:
    _schema_step(db, "legacy_shadow_track", lambda: db.execute("""
    CREATE TABLE IF NOT EXISTS legacy_shadow_track (
      source TEXT NOT NULL,
      key TEXT NOT NULL,
      ticker TEXT,
      address TEXT,
      network_id INTEGER,
      tid TEXT,
      entry_price REAL NOT NULL,
      entry_ts REAL NOT NULL,
      size_usd REAL,
      tp50_exit_price REAL,
      tp50_exit_ts REAL,
      tp50_pnl REAL,
      tp50_reason TEXT,
      legacy_exit_reason TEXT,
      legacy_exit_price REAL,
      legacy_exit_ts REAL,
      legacy_pnl REAL,
      last_mark REAL,
      last_mark_ts REAL,
      PRIMARY KEY (source, key)
    )"""))
    _schema_step(db, "idx_lst_pending", lambda: db.execute(
        """CREATE INDEX IF NOT EXISTS idx_lst_pending
           ON legacy_shadow_track(source, legacy_exit_ts)"""
    ))


def _num(value, default=None):
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _price_of(bag) -> float | None:
    if bag is None or bag == "":
        return None
    if isinstance(bag, (int, float)):
        px = float(bag)
        return px if px > 0 else None
    if not isinstance(bag, dict):
        return None
    for key in ("price_usd", "price", "priceUsd", "current_price_usd"):
        px = _num(bag.get(key))
        if px is not None and px > 0:
            return px
    return None


def _row(r) -> dict:
    return dict(r) if r is not None else {}


def _pnl_usd(entry_price, exit_price, size_usd=TICKET_USD):
    entry = _num(entry_price)
    px = _num(exit_price)
    size = _num(size_usd, TICKET_USD) or TICKET_USD
    if entry is None or entry <= 0 or px is None or px <= 0:
        return None
    return size * (px / entry - 1.0)


def decide_legacy(entry_price, entry_ts, price, now) -> dict | None:
    """Legacy rule from entry: +100%, -30%, or 6h. None = keep waiting.

    Missing price retries until 6h + grace, then reason='unresolved'.
    """
    entry = _num(entry_price)
    ts = _num(entry_ts)
    tnow = _num(now)
    if entry is None or entry <= 0 or ts is None or tnow is None:
        return None
    px = _price_of(price) if not isinstance(price, (int, float, type(None))) else _num(price)
    if px is not None and px <= 0:
        px = None
    held_h = (tnow - ts) / 3600.0
    grace_h = LEGACY_MAX_HOLD_HOURS + LEGACY_GRACE_HOURS
    if px is not None:
        pct = (px / entry - 1.0) * 100.0
        if pct >= LEGACY_TP_PCT:
            reason = REASON_TP
        elif pct <= LEGACY_SL_PCT:
            reason = REASON_SL
        elif held_h >= LEGACY_MAX_HOLD_HOURS:
            reason = REASON_TIME
        else:
            return None
        return {
            "legacy_exit_reason": reason,
            "legacy_exit_price": px,
            "legacy_exit_ts": tnow,
            "legacy_pnl": _pnl_usd(entry, px),
        }
    if held_h >= grace_h:
        return {
            "legacy_exit_reason": REASON_UNRESOLVED,
            "legacy_exit_price": None,
            "legacy_exit_ts": tnow,
            "legacy_pnl": None,
        }
    return None


def on_tp50_policy_close(
    *,
    source: str,
    key: str,
    ticker=None,
    address=None,
    network_id=None,
    tid=None,
    entry_price,
    entry_ts,
    size_usd=TICKET_USD,
    tp50_exit_price=None,
    tp50_exit_ts=None,
    tp50_pnl=None,
    tp50_reason=None,
    now: float | None = None,
) -> dict | None:
    """Start (or immediately resolve) a ledger track after a tp_50-policy close."""
    try:
        if source != SOURCE_LEDGER:
            return None
        entry = _num(entry_price)
        ets = _num(entry_ts)
        if entry is None or entry <= 0 or ets is None or not key:
            return None
        tnow = current_time() if now is None else float(now)
        exit_ts = _num(tp50_exit_ts, tnow)
        decision = decide_legacy(entry, ets, tp50_exit_price, exit_ts)
        db = _connect()
        db.execute(
            """INSERT OR REPLACE INTO legacy_shadow_track (
                 source, key, ticker, address, network_id, tid,
                 entry_price, entry_ts, size_usd,
                 tp50_exit_price, tp50_exit_ts, tp50_pnl, tp50_reason,
                 legacy_exit_reason, legacy_exit_price, legacy_exit_ts, legacy_pnl,
                 last_mark, last_mark_ts
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                SOURCE_LEDGER, str(key), ticker, address,
                int(network_id) if network_id is not None else None,
                tid or (f"{address}:{network_id}" if address is not None and network_id is not None else None),
                entry, ets, _num(size_usd, TICKET_USD),
                _num(tp50_exit_price), exit_ts, _num(tp50_pnl),
                str(tp50_reason)[:200] if tp50_reason else None,
                (decision or {}).get("legacy_exit_reason"),
                (decision or {}).get("legacy_exit_price"),
                (decision or {}).get("legacy_exit_ts"),
                (decision or {}).get("legacy_pnl"),
                _num(tp50_exit_price), exit_ts,
            ),
        )
        db.commit()
        rec = _row(db.execute(
            "SELECT * FROM legacy_shadow_track WHERE source = ? AND key = ?",
            (SOURCE_LEDGER, str(key)),
        ).fetchone())
        if decision:
            _append_ledger_resolve(rec)
        return rec
    except Exception as e:
        log.warning("legacy track start: %s", safe_err(e))
        return None


def pending_tracks() -> list[dict]:
    try:
        rows = _connect().execute(
            """SELECT * FROM legacy_shadow_track
               WHERE source = ? AND legacy_exit_ts IS NULL""",
            (SOURCE_LEDGER,),
        ).fetchall()
        return [_row(r) for r in rows]
    except Exception as e:
        log.warning("legacy pending: %s", safe_err(e))
        return []


def pending_tids() -> list[str]:
    out = []
    for row in pending_tracks():
        tid = row.get("tid")
        if not tid and row.get("address") is not None and row.get("network_id") is not None:
            tid = f"{row['address']}:{row['network_id']}"
        if tid:
            out.append(str(tid))
    return out


def _append_ledger_resolve(row: dict) -> None:
    try:
        import shadow_ledger
        rec = {
            "action": "legacy_resolve",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(
                _num(row.get("legacy_exit_ts"), current_time())
            )),
            "ticker": row.get("ticker"),
            "address": row.get("address"),
            "network_id": row.get("network_id"),
            "entry_price_usd": row.get("entry_price"),
            "legacy_exit_reason": row.get("legacy_exit_reason"),
            "legacy_exit_price": row.get("legacy_exit_price"),
            "legacy_exit_ts": row.get("legacy_exit_ts"),
            "legacy_pnl": row.get("legacy_pnl"),
        }
        shadow_ledger._append(rec)
    except Exception as e:
        log.warning("legacy ledger append: %s", safe_err(e))


def _resolve_row(row: dict, decision: dict) -> None:
    db = _connect()
    db.execute(
        """UPDATE legacy_shadow_track SET
             legacy_exit_reason = ?, legacy_exit_price = ?,
             legacy_exit_ts = ?, legacy_pnl = ?,
             last_mark = ?, last_mark_ts = ?
           WHERE source = ? AND key = ?""",
        (
            decision.get("legacy_exit_reason"),
            decision.get("legacy_exit_price"),
            decision.get("legacy_exit_ts"),
            decision.get("legacy_pnl"),
            decision.get("legacy_exit_price"),
            decision.get("legacy_exit_ts"),
            row["source"], row["key"],
        ),
    )
    db.commit()
    merged = dict(row)
    merged.update(decision)
    _append_ledger_resolve(merged)


def mark_pending(prices=None, now: float | None = None) -> dict:
    """Mark pending ledger tracks with already-fetched FOMO prices. No new fetch."""
    out = {"marked": 0, "resolved": 0, "stale": 0}
    try:
        ts = current_time() if now is None else float(now)
        prices = prices if isinstance(prices, dict) else {}
        for row in pending_tracks():
            tid = row.get("tid")
            if not tid and row.get("address") is not None and row.get("network_id") is not None:
                tid = f"{row['address']}:{row['network_id']}"
            bag = prices.get(tid) if tid else None
            if bag is None and tid:
                bag = prices.get(str(tid))
            px = _price_of(bag)
            decision = decide_legacy(row.get("entry_price"), row.get("entry_ts"), px, ts)
            if decision:
                _resolve_row(row, decision)
                out["resolved"] += 1
                continue
            db = _connect()
            if px is not None:
                db.execute(
                    """UPDATE legacy_shadow_track SET last_mark = ?, last_mark_ts = ?
                       WHERE source = ? AND key = ?""",
                    (px, ts, row["source"], row["key"]),
                )
                db.commit()
                out["marked"] += 1
            else:
                out["stale"] += 1
        return out
    except Exception as e:
        log.warning("legacy mark: %s", safe_err(e))
        return out


def _ledger_rows() -> list[dict]:
    try:
        return [_row(r) for r in _connect().execute(
            "SELECT * FROM legacy_shadow_track WHERE source = ?",
            (SOURCE_LEDGER,),
        ).fetchall()]
    except Exception as e:
        log.warning("legacy ledger rows: %s", safe_err(e))
        return []


def _pick_confirm_tp50_closed() -> list[dict]:
    try:
        import pick_confirm_shadow as pcs
        rows = []
        for r in pcs.all_rows():
            if r.get("exit_policy") != "tp_50":
                continue
            if r.get("exit_ts") is None:
                continue
            rows.append(r)
        return rows
    except Exception as e:
        log.warning("legacy pick_confirm rows: %s", safe_err(e))
        return []


def _side(pnls) -> dict:
    measured = [p for p in pnls if p is not None]
    return {
        "closed_count": len(pnls),
        "wins": sum(1 for p in measured if p > 0),
        "realized_pnl_usd": round(sum(measured), 4),
    }


def comparison_summary() -> dict:
    """tp_50 vs legacy on the same entries. Resolved pairs only + pending count."""
    empty = {
        "tp_50": _side([]),
        "legacy": _side([]),
        "pending_count": 0,
        "paper": True,
        "simulated": True,
        "live_orders": False,
    }
    try:
        tp_pnls = []
        lg_pnls = []
        pending = 0
        for row in _ledger_rows():
            if row.get("tp50_exit_ts") is None:
                continue
            if row.get("legacy_exit_reason"):
                tp_pnls.append(_num(row.get("tp50_pnl")))
                lg_pnls.append(_num(row.get("legacy_pnl")))
            else:
                pending += 1
        for row in _pick_confirm_tp50_closed():
            if row.get("legacy_exit_reason"):
                tp_pnls.append(_num(row.get("pnl_usd")))
                lg_pnls.append(_num(row.get("legacy_pnl")))
            else:
                pending += 1
        return {
            "tp_50": _side(tp_pnls),
            "legacy": _side(lg_pnls),
            "pending_count": pending,
            "paper": True,
            "simulated": True,
            "live_orders": False,
        }
    except Exception as e:
        log.warning("legacy compare: %s", safe_err(e))
        return empty
