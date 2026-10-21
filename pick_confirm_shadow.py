"""Two-cycle pick confirm. Paper/shadow only.

When a judged token first clears score >= 0.6, record pending_confirm.
Next cycle: judged again at >= 0.6 → confirmed + paper 'would pick';
judge fail, filter/soft kill, or not seen → unconfirmed.

Never writes the real pick or shadow_ledger. Fail open. safe_err only.
"""
from __future__ import annotations

import logging
import os
import pathlib
import sqlite3
import threading
import time

from secret_utils import safe_err

log = logging.getLogger("pick_confirm_shadow")

CONFIRM_MIN = 0.6
TICKET_USD = 50.0
MAX_HOLD_HOURS = 6.0
STOP_LOSS_PCT = -30.0
TAKE_PROFIT_PCT = 100.0
LOCK_TIMEOUT_SEC = 0.2

STATUS_PENDING = "pending_confirm"
STATUS_CONFIRMED = "confirmed"
STATUS_UNCONFIRMED = "unconfirmed"

_tls = threading.local()
_schema_lock = threading.Lock()
_time_fn = time.time


def set_time_fn(fn) -> None:
    global _time_fn
    _time_fn = time.time if fn is None else fn


def current_time() -> float:
    return float(_time_fn())


def confirm_min() -> float:
    raw = os.environ.get("PICK_CONFIRM_MIN")
    if raw:
        try:
            return float(raw)
        except (TypeError, ValueError):
            pass
    return CONFIRM_MIN


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
        log.warning("pick_confirm schema %s: %s", name, safe_err(e))
        try:
            db.rollback()
        except Exception:
            pass


def _init_schema(db: sqlite3.Connection) -> None:
    _schema_step(db, "pick_confirm", lambda: db.execute("""
    CREATE TABLE IF NOT EXISTS pick_confirm (
      tid TEXT NOT NULL,
      first_ts REAL NOT NULL,
      ticker TEXT,
      first_score REAL,
      first_price REAL,
      status TEXT NOT NULL,
      confirm_ts REAL,
      confirm_score REAL,
      confirm_price REAL,
      unconfirm_reason TEXT,
      entry_price REAL,
      exit_price REAL,
      exit_ts REAL,
      exit_reason TEXT,
      pnl_usd REAL,
      last_mark REAL,
      last_mark_ts REAL,
      PRIMARY KEY (tid, first_ts)
    )"""))
    _schema_step(db, "idx_pc_status", lambda: db.execute(
        "CREATE INDEX IF NOT EXISTS idx_pc_status ON pick_confirm(status)"
    ))


def _num(value, default=None):
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _price_of(bag) -> float | None:
    if not isinstance(bag, dict):
        return None
    for key in ("price_usd", "price", "priceUsd"):
        px = _num(bag.get(key))
        if px is not None and px > 0:
            return px
    return None


def _row(r) -> dict:
    return dict(r) if r is not None else {}


def pending_rows() -> list[dict]:
    try:
        return [_row(r) for r in _connect().execute(
            "SELECT * FROM pick_confirm WHERE status = ? ORDER BY first_ts",
            (STATUS_PENDING,),
        ).fetchall()]
    except Exception as e:
        log.warning("pick_confirm pending: %s", safe_err(e))
        return []


def all_rows() -> list[dict]:
    try:
        return [_row(r) for r in _connect().execute(
            "SELECT * FROM pick_confirm ORDER BY first_ts DESC"
        ).fetchall()]
    except Exception as e:
        log.warning("pick_confirm rows: %s", safe_err(e))
        return []


def _upsert_pending(tid, ticker, ts, score, price) -> bool:
    db = _connect()
    existing = db.execute(
        "SELECT tid FROM pick_confirm WHERE tid = ? AND status = ?",
        (tid, STATUS_PENDING),
    ).fetchone()
    if existing:
        return False
    db.execute(
        """INSERT INTO pick_confirm (
             tid, first_ts, ticker, first_score, first_price, status
           ) VALUES (?, ?, ?, ?, ?, ?)""",
        (tid, float(ts), ticker, _num(score), _num(price), STATUS_PENDING),
    )
    db.commit()
    return True


def _confirm(row: dict, ts: float, score, price) -> None:
    px = _num(price)
    db = _connect()
    db.execute(
        """UPDATE pick_confirm SET
             status = ?, confirm_ts = ?, confirm_score = ?, confirm_price = ?,
             entry_price = ?, last_mark = ?, last_mark_ts = ?
           WHERE tid = ? AND first_ts = ?""",
        (STATUS_CONFIRMED, float(ts), _num(score), px, px, px, float(ts),
         row["tid"], row["first_ts"]),
    )
    db.commit()
    log.info(
        "pick_confirm would pick tid=%s ticker=%s price=%s score=%s paper=1",
        row.get("tid"), row.get("ticker") or "?", px, score,
    )


def _unconfirm(row: dict, reason: str) -> None:
    db = _connect()
    db.execute(
        """UPDATE pick_confirm SET status = ?, unconfirm_reason = ?
           WHERE tid = ? AND first_ts = ?""",
        (STATUS_UNCONFIRMED, str(reason or "unknown")[:200],
         row["tid"], row["first_ts"]),
    )
    db.commit()


def _dossier_by_tid(survivors) -> dict:
    out = {}
    for item in survivors or []:
        d = item[0] if isinstance(item, (tuple, list)) and item else item
        if isinstance(d, dict) and d.get("tid"):
            out[d["tid"]] = d
    return out


def on_cycle(stats: dict | None, survivors=None, now: float | None = None) -> dict:
    """Resolve last-cycle pending, then record new >=0.6 judged tokens."""
    empty = {"pending": 0, "confirmed": 0, "unconfirmed": 0, "new_pending": 0}
    try:
        ts = current_time() if now is None else float(now)
        stats = stats if isinstance(stats, dict) else {}
        tokens = [t for t in (stats.get("tokens") or []) if isinstance(t, dict)]
        by_tid = {t.get("tid"): t for t in tokens if t.get("tid")}
        dossiers = _dossier_by_tid(survivors)
        threshold = confirm_min()
        confirmed = unconfirmed = 0
        resolved = set()
        for row in pending_rows():
            tid = row.get("tid")
            resolved.add(tid)
            seen = by_tid.get(tid)
            if seen is None:
                _unconfirm(row, "not_seen")
                unconfirmed += 1
                continue
            stage = seen.get("stage") or ""
            if stage == "judged" and seen.get("judge_verdict") != "error":
                score = _num(seen.get("judge_score"))
                if score is not None and score >= threshold:
                    d = dossiers.get(tid) or seen
                    _confirm(row, ts, score, _price_of(d) or _price_of(seen))
                    confirmed += 1
                    continue
                _unconfirm(row, f"judge_score={score}")
                unconfirmed += 1
                continue
            reason = seen.get("reason") or stage or "killed"
            _unconfirm(row, f"{stage}:{reason}" if stage else str(reason))
            unconfirmed += 1
        new_pending = 0
        pending_now = {r["tid"] for r in pending_rows()}
        for t in tokens:
            if t.get("stage") != "judged" or t.get("judge_verdict") == "error":
                continue
            score = _num(t.get("judge_score"))
            if score is None or score < threshold:
                continue
            tid = t.get("tid")
            if not tid or tid in pending_now or tid in resolved:
                continue
            d = dossiers.get(tid) or t
            if _upsert_pending(tid, t.get("ticker"), ts, score, _price_of(d)):
                new_pending += 1
                pending_now.add(tid)
        return {
            "pending": len(pending_rows()),
            "confirmed": confirmed,
            "unconfirmed": unconfirmed,
            "new_pending": new_pending,
        }
    except Exception as e:
        log.warning("pick_confirm cycle: %s", safe_err(e))
        return empty


def _apply_mark(row: dict, price, now: float) -> str:
    entry = _num(row.get("entry_price") or row.get("confirm_price"))
    if entry is None or entry <= 0:
        return "skip"
    last = _num(row.get("last_mark")) or entry
    px = _num(price)
    held_h = (now - float(row.get("confirm_ts") or now)) / 3600.0
    exit_px = px if px is not None and px > 0 else last
    reason = None
    if held_h >= MAX_HOLD_HOURS:
        reason = f"time_stop_{held_h:.1f}h" + ("_stale" if px is None else "")
    elif px is not None and px > 0:
        pct = (px / entry - 1.0) * 100.0
        if pct <= STOP_LOSS_PCT:
            reason = f"stop_loss_{pct:.1f}pct"
        elif pct >= TAKE_PROFIT_PCT:
            reason = f"take_profit_{pct:.1f}pct"
    db = _connect()
    if reason:
        pnl = TICKET_USD * (exit_px / entry - 1.0) if exit_px else None
        db.execute(
            """UPDATE pick_confirm SET
                 exit_price = ?, exit_ts = ?, exit_reason = ?, pnl_usd = ?,
                 last_mark = ?, last_mark_ts = ?
               WHERE tid = ? AND first_ts = ?""",
            (exit_px, now, reason, pnl, exit_px, now, row["tid"], row["first_ts"]),
        )
        db.commit()
        return "closed"
    if px is None or px <= 0:
        return "stale"
    db.execute(
        """UPDATE pick_confirm SET last_mark = ?, last_mark_ts = ?
           WHERE tid = ? AND first_ts = ?""",
        (px, now, row["tid"], row["first_ts"]),
    )
    db.commit()
    return "marked"


def mark_open(fomo_client=None, now: float | None = None) -> dict:
    """MTM confirmed opens. Same $50 / 6h / -30% / +100% as main shadow."""
    out = {"marked": 0, "closed": 0, "stale": 0}
    try:
        ts = current_time() if now is None else float(now)
        opens = [_row(r) for r in _connect().execute(
            """SELECT * FROM pick_confirm
               WHERE status = ? AND exit_ts IS NULL""",
            (STATUS_CONFIRMED,),
        ).fetchall()]
        if not opens:
            return out
        prices = {}
        if fomo_client is not None:
            try:
                tids = [r["tid"] for r in opens if r.get("tid")]
                raw = fomo_client.tokens(tids) if hasattr(fomo_client, "tokens") else {}
                if isinstance(raw, dict):
                    prices = raw
            except Exception as e:
                log.warning("pick_confirm fomo mark: %s", safe_err(e))
        for row in opens:
            bag = prices.get(row.get("tid")) or {}
            px = _price_of(bag) if bag else None
            kind = _apply_mark(row, px, ts)
            if kind in out:
                out[kind] += 1
        return out
    except Exception as e:
        log.warning("pick_confirm mark: %s", safe_err(e))
        return out


def counts() -> dict:
    rows = all_rows()
    out = {
        "n": len(rows),
        "pending_confirm": 0,
        "confirmed": 0,
        "unconfirmed": 0,
        "open": 0,
        "closed": 0,
        "confirmed_pnl": 0.0,
    }
    for r in rows:
        st = r.get("status")
        if st == STATUS_PENDING:
            out["pending_confirm"] += 1
        elif st == STATUS_CONFIRMED:
            out["confirmed"] += 1
            if r.get("exit_ts") is None:
                out["open"] += 1
            else:
                out["closed"] += 1
                pnl = _num(r.get("pnl_usd"))
                if pnl is not None:
                    out["confirmed_pnl"] += pnl
        elif st == STATUS_UNCONFIRMED:
            out["unconfirmed"] += 1
    out["confirmed_pnl"] = round(out["confirmed_pnl"], 4)
    return out


def _real_picks_pnl() -> dict:
    payload = {"n": 0, "closed": 0, "pnl": 0.0}
    try:
        import shadow_ledger
        closes = shadow_ledger.closed_positions(limit=None) or []
        measured = [c for c in closes if not c.get("unmeasured")]
        pnls = [_num(c.get("realized_pnl_usd")) for c in measured]
        pnls = [p for p in pnls if p is not None]
        payload["n"] = len(closes)
        payload["closed"] = len(measured)
        payload["pnl"] = round(sum(pnls), 4)
    except Exception as e:
        log.warning("pick_confirm real pnl: %s", safe_err(e))
    return payload


def ops_payload() -> dict:
    try:
        c = counts()
        return {
            "paper": True,
            "simulated": True,
            "live_orders": False,
            "gates_picks": False,
            "confirm_min": confirm_min(),
            "ticket_usd": TICKET_USD,
            "caveat": (
                "Two-cycle confirm is paper only. It does not change the real "
                "pick or the main shadow ledger."
            ),
            "counts": c,
            "confirmed_pnl": c["confirmed_pnl"],
            "real_picks": _real_picks_pnl(),
            "label": "shadow, does not gate picks",
            "entries": all_rows()[:80],
        }
    except Exception as e:
        log.warning("pick_confirm ops: %s", safe_err(e))
        return {
            "paper": True, "simulated": True, "live_orders": False,
            "gates_picks": False, "error": "unavailable",
            "counts": counts(), "entries": [],
        }
