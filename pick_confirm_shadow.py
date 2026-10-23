"""Two-cycle pick confirm. Required for shadow fills.

When a judged token first clears score >= 0.6, record pending_confirm.
It is only shadow-filled if it clears again on a later cycle with no new
soft kill. Pending expires if not re-seen within 2 cycles.

Outcomes: confirmed_pick / dropped_unconfirmed / expired / vetoed.
Shadow fills only. Never live orders. Fail open. safe_err only.
"""
from __future__ import annotations

import argparse
import logging
import os
import pathlib
import sqlite3
import tempfile
import threading
import time

from secret_utils import safe_err

log = logging.getLogger("pick_confirm_shadow")

CONFIRM_MIN = 0.6
TICKET_USD = 50.0
MAX_HOLD_HOURS = 6.0
STOP_LOSS_PCT = -30.0
TAKE_PROFIT_PCT = 100.0
TP50_PCT = 50.0
EXIT_POLICY_TP50 = "tp_50"
EXIT_POLICY_LEGACY = "legacy"
LOCK_TIMEOUT_SEC = 0.2

STATUS_PENDING = "pending_confirm"
STATUS_CONFIRMED = "confirmed"
STATUS_UNCONFIRMED = "unconfirmed"
OUTCOME_PICK = "confirmed_pick"
OUTCOME_DROPPED = "dropped_unconfirmed"
OUTCOME_EXPIRED = "expired"
OUTCOME_VETOED = "vetoed"
EXPIRE_MISSED_CYCLES = 2
PEPA = "PepaMint111111111111111111111111111111:1399811149"
HORN = "HornMint111111111111111111111111111111:1399811149"
PROWLER = "ProwlerMint11111111111111111111111111:1399811149"
CHILL = "CHILL:1399811149"

_tls = threading.local()
_schema_lock = threading.Lock()
_time_fn = time.time
_just_confirmed_tids: set[str] = set()
_just_confirmed_tickers: set[str] = set()


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
    _just_confirmed_tids.clear()
    _just_confirmed_tickers.clear()


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
    cols = {row[1] for row in db.execute("PRAGMA table_info(pick_confirm)")}
    if "outcome" not in cols:
        _schema_step(db, "outcome", lambda: db.execute(
            "ALTER TABLE pick_confirm ADD COLUMN outcome TEXT"
        ))
    if "missed_cycles" not in cols:
        _schema_step(db, "missed_cycles", lambda: db.execute(
            "ALTER TABLE pick_confirm ADD COLUMN missed_cycles INTEGER NOT NULL DEFAULT 0"
        ))
    if "exit_policy" not in cols:
        _schema_step(db, "exit_policy", lambda: db.execute(
            "ALTER TABLE pick_confirm ADD COLUMN exit_policy TEXT"
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
             status = ?, outcome = ?, confirm_ts = ?, confirm_score = ?,
             confirm_price = ?, entry_price = ?, last_mark = ?, last_mark_ts = ?,
             missed_cycles = 0, exit_policy = ?
           WHERE tid = ? AND first_ts = ?""",
        (STATUS_CONFIRMED, OUTCOME_PICK, float(ts), _num(score), px, px, px,
         float(ts), EXIT_POLICY_TP50, row["tid"], row["first_ts"]),
    )
    db.commit()
    if row.get("tid"):
        _just_confirmed_tids.add(str(row["tid"]))
    if row.get("ticker"):
        _just_confirmed_tickers.add(str(row["ticker"]))
    log.info(
        "pick_confirm confirmed_pick tid=%s ticker=%s price=%s score=%s paper=1",
        row.get("tid"), row.get("ticker") or "?", px, score,
    )


def _unconfirm(row: dict, reason: str, *, outcome: str = OUTCOME_DROPPED) -> None:
    db = _connect()
    db.execute(
        """UPDATE pick_confirm SET status = ?, outcome = ?, unconfirm_reason = ?
           WHERE tid = ? AND first_ts = ?""",
        (STATUS_UNCONFIRMED, outcome, str(reason or "unknown")[:200],
         row["tid"], row["first_ts"]),
    )
    db.commit()


def _bump_missed(row: dict, missed: int) -> None:
    db = _connect()
    db.execute(
        """UPDATE pick_confirm SET missed_cycles = ?
           WHERE tid = ? AND first_ts = ?""",
        (int(missed), row["tid"], row["first_ts"]),
    )
    db.commit()


def _tid_of_order(order) -> str | None:
    if not isinstance(order, dict):
        return None
    token = order.get("token") if isinstance(order.get("token"), dict) else {}
    addr = token.get("address")
    net = token.get("network_id")
    if addr is not None and net is not None:
        return f"{addr}:{net}"
    if addr:
        return str(addr)
    return None


def mark_vetoed(order, reasons=None, survivors=None, tid=None, *, now: float | None = None) -> dict | None:
    """Record pick_confirm outcome=vetoed. No shadow fill. Fail open."""
    try:
        ticker = None
        token = order.get("token") if isinstance(order, dict) else {}
        if isinstance(token, dict):
            ticker = token.get("ticker")
        if not tid:
            for item in survivors or []:
                bag = item[0] if isinstance(item, (tuple, list)) and item else item
                if isinstance(bag, dict) and ticker and bag.get("ticker") == ticker:
                    tid = bag.get("tid")
                    break
        if not tid:
            tid = _tid_of_order(order)
        if not tid:
            return None
        ts = current_time() if now is None else float(now)
        reason = ",".join(str(r) for r in (reasons or []) if r) or "rug_veto"
        db = _connect()
        pending = db.execute(
            "SELECT * FROM pick_confirm WHERE tid = ? AND status = ?",
            (tid, STATUS_PENDING),
        ).fetchone()
        if pending:
            _unconfirm(_row(pending), reason, outcome=OUTCOME_VETOED)
            return _row(db.execute(
                "SELECT * FROM pick_confirm WHERE tid = ? AND first_ts = ?",
                (tid, pending["first_ts"]),
            ).fetchone())
        db.execute(
            """INSERT INTO pick_confirm (
                 tid, first_ts, ticker, status, outcome, unconfirm_reason
               ) VALUES (?, ?, ?, ?, ?, ?)""",
            (tid, ts, ticker, STATUS_UNCONFIRMED, OUTCOME_VETOED, reason[:200]),
        )
        db.commit()
        return _row(db.execute(
            "SELECT * FROM pick_confirm WHERE tid = ? AND first_ts = ?",
            (tid, ts),
        ).fetchone())
    except Exception as e:
        log.warning("pick_confirm vetoed: %s", safe_err(e))
        return None


def fill_allowed(order) -> bool:
    """True only when this cycle just confirmed the picked token."""
    if not isinstance(order, dict):
        return False
    token = order.get("token") if isinstance(order.get("token"), dict) else {}
    ticker = token.get("ticker")
    if ticker and str(ticker) in _just_confirmed_tickers:
        return True
    addr = token.get("address")
    if addr:
        prefix = str(addr)
        for tid in _just_confirmed_tids:
            if str(tid).startswith(prefix):
                return True
    return False


def _dossier_by_tid(survivors) -> dict:
    out = {}
    for item in survivors or []:
        d = item[0] if isinstance(item, (tuple, list)) and item else item
        if isinstance(d, dict) and d.get("tid"):
            out[d["tid"]] = d
    return out


def on_cycle(stats: dict | None, survivors=None, vetoed=None,
             now: float | None = None) -> dict:
    """Resolve last-cycle pending, then record new >=0.6 judged tokens."""
    empty = {"pending": 0, "confirmed": 0, "unconfirmed": 0, "expired": 0,
             "new_pending": 0, "vetoed": 0}
    try:
        ts = current_time() if now is None else float(now)
        _just_confirmed_tids.clear()
        _just_confirmed_tickers.clear()
        stats = stats if isinstance(stats, dict) else {}
        tokens = [t for t in (stats.get("tokens") or []) if isinstance(t, dict)]
        by_tid = {t.get("tid"): t for t in tokens if t.get("tid")}
        dossiers = _dossier_by_tid(survivors)
        vetoed = vetoed if isinstance(vetoed, dict) else {}
        vetoed_tid = str(vetoed.get("tid") or "") if vetoed.get("skipped") else ""
        vetoed_ticker = str(vetoed.get("ticker") or "") if vetoed.get("skipped") else ""
        veto_reason = ",".join(str(r) for r in (vetoed.get("reasons") or []) if r) or "rug_veto"
        threshold = confirm_min()
        confirmed = unconfirmed = expired = vetoed_n = 0
        resolved = set()
        for row in pending_rows():
            tid = row.get("tid")
            resolved.add(tid)
            if vetoed_tid and str(tid) == vetoed_tid:
                _unconfirm(row, veto_reason, outcome=OUTCOME_VETOED)
                vetoed_n += 1
                unconfirmed += 1
                continue
            seen = by_tid.get(tid)
            if seen is None:
                missed = int(row.get("missed_cycles") or 0) + 1
                if missed >= EXPIRE_MISSED_CYCLES:
                    _unconfirm(row, "expired", outcome=OUTCOME_EXPIRED)
                    expired += 1
                    unconfirmed += 1
                else:
                    _bump_missed(row, missed)
                continue
            stage = seen.get("stage") or ""
            if stage == "judged" and seen.get("judge_verdict") != "error":
                score = _num(seen.get("judge_score"))
                if score is not None and score >= threshold:
                    d = dossiers.get(tid) or seen
                    _confirm(row, ts, score, _price_of(d) or _price_of(seen))
                    confirmed += 1
                    continue
                _unconfirm(row, f"judge_score={score}", outcome=OUTCOME_DROPPED)
                unconfirmed += 1
                continue
            reason = seen.get("reason") or stage or "killed"
            _unconfirm(
                row, f"{stage}:{reason}" if stage else str(reason),
                outcome=OUTCOME_DROPPED,
            )
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
            if (vetoed_tid and str(tid) == vetoed_tid) or (
                vetoed_ticker and str(t.get("ticker") or "") == vetoed_ticker
            ):
                mark_vetoed(
                    {"token": {"ticker": t.get("ticker")}},
                    vetoed.get("reasons"), survivors, tid=tid, now=ts,
                )
                vetoed_n += 1
                continue
            d = dossiers.get(tid) or t
            if _upsert_pending(tid, t.get("ticker"), ts, score, _price_of(d)):
                new_pending += 1
                pending_now.add(tid)
        return {
            "pending": len(pending_rows()),
            "confirmed": confirmed,
            "unconfirmed": unconfirmed,
            "expired": expired,
            "new_pending": new_pending,
            "vetoed": vetoed_n,
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
    policy = row.get("exit_policy") or EXIT_POLICY_LEGACY
    active_tp = TP50_PCT if policy == EXIT_POLICY_TP50 else TAKE_PROFIT_PCT
    if held_h >= MAX_HOLD_HOURS:
        reason = f"time_stop_{held_h:.1f}h" + ("_stale" if px is None else "")
    elif px is not None and px > 0:
        pct = (px / entry - 1.0) * 100.0
        tp50_would = pct >= TP50_PCT
        legacy_would = pct >= TAKE_PROFIT_PCT
        if pct <= STOP_LOSS_PCT:
            reason = f"stop_loss_{pct:.1f}pct"
        elif pct >= active_tp:
            reason = (f"tp_50_{pct:.1f}pct" if policy == EXIT_POLICY_TP50
                      else f"take_profit_{pct:.1f}pct")
        log.info(
            "pick_confirm mark tid=%s policy=%s pct=%s tp50_would=%s "
            "legacy_would=%s paper=1",
            row.get("tid"), policy, pct, tp50_would, legacy_would,
        )
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
        "dropped": 0,
        "expired": 0,
        "vetoed": 0,
        "open": 0,
        "closed": 0,
        "confirmed_pnl": 0.0,
    }
    for r in rows:
        st = r.get("status")
        outcome = r.get("outcome")
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
            if outcome == OUTCOME_EXPIRED:
                out["expired"] += 1
            elif outcome == OUTCOME_VETOED:
                out["vetoed"] += 1
            else:
                out["dropped"] += 1
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
            "gates_picks": True,
            "gates_live_orders": False,
            "confirm_min": confirm_min(),
            "ticket_usd": TICKET_USD,
            "caveat": (
                "Two-cycle confirm is required for shadow fills. First clear "
                "is pending; a second clear with no soft kill fills. Live "
                "orders stay off."
            ),
            "counts": c,
            "confirmed_pnl": c["confirmed_pnl"],
            "real_picks": _real_picks_pnl(),
            "label": "required for shadow fills · no live orders",
            "entries": all_rows()[:80],
        }
    except Exception as e:
        log.warning("pick_confirm ops: %s", safe_err(e))
        return {
            "paper": True, "simulated": True, "live_orders": False,
            "gates_picks": True, "error": "unavailable",
            "counts": counts(), "entries": [],
        }


def replay_historical(*, live_db: bool = False) -> dict:
    """HORN / PROWLER / PEPA drop; CHILL stays pending without a second look.

    Writes a throwaway temp DB unless live_db=True (--live-db-i-mean-it).
    The default path never resets or writes the live pick_confirm table.
    """
    prev = os.environ.get("OUTCOMES_DB")
    tmp_path = None
    if not live_db:
        fd, tmp_path = tempfile.mkstemp(prefix="pick_confirm_replay_", suffix=".db")
        os.close(fd)
        os.environ["OUTCOMES_DB"] = tmp_path
        reset()
    else:
        reset()
    set_time_fn(lambda: 1_700_000_000.0)
    t0 = 1_700_000_000.0
    try:
        on_cycle({"tokens": [
            {"tid": HORN, "ticker": "HORN", "stage": "judged",
             "judge_score": 0.72, "judge_verdict": "pass", "price_usd": 0.0004},
            {"tid": PROWLER, "ticker": "PROWLER", "stage": "judged",
             "judge_score": 0.81, "judge_verdict": "pass", "price_usd": 0.0009},
            {"tid": PEPA, "ticker": "PEPA", "stage": "judged",
             "judge_score": 0.72, "judge_verdict": "pick", "price_usd": 0.0002},
            {"tid": CHILL, "ticker": "CHILL", "stage": "judged",
             "judge_score": 0.64, "judge_verdict": "pick", "price_usd": 0.001},
        ]}, now=t0)
        on_cycle({"tokens": [
            {"tid": HORN, "ticker": "HORN", "stage": "judged",
             "judge_score": 0.51, "judge_verdict": "pass", "price_usd": 0.00035},
            {"tid": PROWLER, "ticker": "PROWLER", "stage": "soft",
             "reason": "shape_is_wrong"},
            {"tid": PEPA, "ticker": "PEPA", "stage": "judged",
             "judge_score": 0.48, "judge_verdict": "pass", "price_usd": 0.00015},
        ]}, now=t0 + 900)
        by = {r["ticker"]: r for r in all_rows()}
        return {
            "HORN": {"outcome": by.get("HORN", {}).get("outcome"),
                     "status": by.get("HORN", {}).get("status"),
                     "reason": by.get("HORN", {}).get("unconfirm_reason")},
            "PROWLER": {"outcome": by.get("PROWLER", {}).get("outcome"),
                        "status": by.get("PROWLER", {}).get("status"),
                        "reason": by.get("PROWLER", {}).get("unconfirm_reason")},
            "PEPA": {"outcome": by.get("PEPA", {}).get("outcome"),
                     "status": by.get("PEPA", {}).get("status"),
                     "reason": by.get("PEPA", {}).get("unconfirm_reason")},
            "CHILL": {"outcome": by.get("CHILL", {}).get("outcome"),
                      "status": by.get("CHILL", {}).get("status"),
                      "reason": by.get("CHILL", {}).get("unconfirm_reason")},
        }
    finally:
        set_time_fn(None)
        if not live_db:
            reset()
            if prev is None:
                os.environ.pop("OUTCOMES_DB", None)
            else:
                os.environ["OUTCOMES_DB"] = prev
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
            reset()


def main(argv=None) -> None:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument(
        "--live-db-i-mean-it",
        action="store_true",
        help="Allow replay_historical to write the live pick_confirm table",
    )
    args = p.parse_args(argv)
    payload = replay_historical(live_db=args.live_db_i_mean_it)
    print("pick_confirm_required replay")
    for name, rec in payload.items():
        print(f"  {name}: status={rec.get('status')} outcome={rec.get('outcome')} "
              f"reason={rec.get('reason')}")


if __name__ == "__main__":
    main()
