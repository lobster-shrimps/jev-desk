"""
Parallel paper exit-variant tracker for young_trail.

Replays several exit rules on the same entry and the same per-cycle marks.
Does NOT change young_trail live rules, judging, picks, or ticket size.
No GT calls. Fail open. Exceptions through safe_err. Never logs secrets.

Caveat: marks arrive only every ~15 min desk cycle; intra-cycle moves are missed.
"""
from __future__ import annotations

import logging

from secret_utils import safe_err

log = logging.getLogger("young_trail_variants")

TICKET_USD = 50.0
CAVEAT = "Marks are only every ~15 min cycle (intra-cycle moves missed)."

# One lever at a time off the live baseline (-30% SL, 30% trail, 6h).
VARIANTS: list[dict] = [
    {"id": "baseline", "sl_pct": -0.30, "trail_pct": 0.30, "window_sec": 6 * 3600,
     "label": "baseline SL-30 trail30 6h"},
    {"id": "tp_50", "sl_pct": -0.30, "trail_pct": 0.30, "window_sec": 6 * 3600,
     "tp_pct": 0.50, "label": "take-profit +50%"},
    {"id": "tp_100", "sl_pct": -0.30, "trail_pct": 0.30, "window_sec": 6 * 3600,
     "tp_pct": 1.00, "label": "take-profit +100%"},
    {"id": "half_50_trail", "sl_pct": -0.30, "trail_pct": 0.30, "window_sec": 6 * 3600,
     "half_tp_pct": 0.50, "label": "sell half +50% then trail 30%"},
    {"id": "trail_20", "sl_pct": -0.30, "trail_pct": 0.20, "window_sec": 6 * 3600,
     "label": "tighter trail 20%"},
    {"id": "trail_40", "sl_pct": -0.30, "trail_pct": 0.40, "window_sec": 6 * 3600,
     "label": "wider trail 40%"},
    {"id": "sl_20", "sl_pct": -0.20, "trail_pct": 0.30, "window_sec": 6 * 3600,
     "label": "tighter SL -20%"},
    {"id": "time_1h", "sl_pct": -0.30, "trail_pct": 0.30, "window_sec": 1 * 3600,
     "label": "time exit 1h"},
    {"id": "time_2h", "sl_pct": -0.30, "trail_pct": 0.30, "window_sec": 2 * 3600,
     "label": "time exit 2h"},
    {"id": "time_3h", "sl_pct": -0.30, "trail_pct": 0.30, "window_sec": 3 * 3600,
     "label": "time exit 3h"},
    {"id": "be_after_30", "sl_pct": -0.30, "trail_pct": 0.30, "window_sec": 6 * 3600,
     "be_trigger_pct": 0.30, "label": "breakeven stop after +30%"},
]
VARIANT_BY_ID = {v["id"]: v for v in VARIANTS}
# Shadow of the pre-tp50 live ledger. Seeded only on new tp50 entries; same
# SL/trail/time as baseline. Not in VARIANTS so ranking stays one-lever-at-a-time.
LEGACY_BASE_RULE = {
    "id": "legacy_base", "sl_pct": -0.30, "trail_pct": 0.30,
    "window_sec": 6 * 3600, "label": "legacy base (no TP)",
}
VARIANT_BY_ID[LEGACY_BASE_RULE["id"]] = LEGACY_BASE_RULE


def init_schema(db) -> None:
    db.executescript("""
    CREATE TABLE IF NOT EXISTS young_trail_variants (
      tid TEXT NOT NULL,
      variant TEXT NOT NULL,
      exit_ts REAL,
      exit_price REAL,
      exit_reason TEXT,
      pnl_usd REAL,
      peak REAL,
      last_price REAL,
      remaining_frac REAL NOT NULL DEFAULT 1.0,
      partial_pnl REAL NOT NULL DEFAULT 0.0,
      be_armed INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (tid, variant)
    );
    CREATE INDEX IF NOT EXISTS idx_ytv_variant_exit
      ON young_trail_variants(variant, exit_ts);
    CREATE TABLE IF NOT EXISTS young_trail_marks (
      tid TEXT NOT NULL,
      ts REAL NOT NULL,
      price REAL NOT NULL,
      PRIMARY KEY (tid, ts)
    );
    CREATE TABLE IF NOT EXISTS young_trail_entry_stats (
      tid TEXT PRIMARY KEY,
      peak REAL NOT NULL,
      peak_ts REAL NOT NULL,
      peak_multiple REAL NOT NULL,
      time_to_peak_sec REAL NOT NULL
    );
    """)
    db.commit()


def _db():
    import young_trail
    return young_trail._connect()


def _f(v, default=None):
    if v is None or v == "":
        return default
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def pnl_usd(exit_price: float, entry_price: float, ticket: float = TICKET_USD) -> float:
    if not entry_price:
        return 0.0
    return float(ticket) * (float(exit_price) / float(entry_price) - 1.0)


def decide_variant_tick(
    rule: dict,
    *,
    entry_price: float,
    peak: float,
    price: float | None,
    entry_ts: float,
    now: float,
    last_price: float | None,
    remaining_frac: float = 1.0,
    partial_pnl: float = 0.0,
    be_armed: bool = False,
) -> tuple[tuple[str, float] | None, dict]:
    """One mark. SL/trail/be, then full TP, then half-TP, then time. Fail closed none."""
    state = {
        "peak": float(peak),
        "remaining_frac": float(remaining_frac),
        "partial_pnl": float(partial_pnl),
        "be_armed": bool(be_armed),
        "last_price": _f(last_price, None),
    }
    sl_pct = float(rule.get("sl_pct", -0.30))
    trail_pct = float(rule.get("trail_pct", 0.30))
    window_sec = float(rule.get("window_sec", 6 * 3600))
    sl_price = float(entry_price) * (1.0 + sl_pct)
    px = _f(price, None)
    if px is not None and px < 0:
        px = None
    if px is not None:
        state["peak"] = max(state["peak"], px)
        state["last_price"] = px
        be_trig = rule.get("be_trigger_pct")
        if be_trig is not None and px >= float(entry_price) * (1.0 + float(be_trig)):
            state["be_armed"] = True
    peak_now = state["peak"]
    if state["be_armed"]:
        sl_price = max(sl_price, float(entry_price))
    trail_price = peak_now * (1.0 - trail_pct)

    def _stop_reason(obs: float) -> str:
        hit_floor = obs <= sl_price
        hit_trail = obs <= trail_price
        if hit_trail and trail_price > sl_price + 1e-12:
            return "trail"
        if state["be_armed"] and sl_price >= float(entry_price) - 1e-12:
            return "be"
        if hit_floor:
            return "sl"
        return "trail"

    if px is not None and (px <= sl_price or px <= trail_price):
        return (_stop_reason(px), px), state

    if px is not None:
        tp = rule.get("tp_pct")
        if tp is not None and px >= float(entry_price) * (1.0 + float(tp)):
            return ("tp", px), state
        half = rule.get("half_tp_pct")
        if (
            half is not None
            and state["remaining_frac"] > 0.5 + 1e-12
            and px >= float(entry_price) * (1.0 + float(half))
        ):
            sold = 0.5
            state["partial_pnl"] += pnl_usd(px, entry_price, TICKET_USD * sold)
            state["remaining_frac"] = 0.5

    timed_out = now >= float(entry_ts) + window_sec
    if timed_out:
        if px is not None:
            return ("time", px), state
        marked = _f(state.get("last_price"), 0.0) or 0.0
        fallback = marked if marked > 0 else float(entry_price)
        return ("time", fallback), state
    return None, state


def seed_legacy_base(tid: str, entry_price: float) -> None:
    """Record the old base exit as a shadow on new tp50 entries. Fail open."""
    try:
        if not tid or not entry_price or entry_price <= 0:
            return
        db = _db()
        init_schema(db)
        px = float(entry_price)
        db.execute(
            """INSERT OR IGNORE INTO young_trail_variants
               (tid, variant, peak, last_price, remaining_frac, partial_pnl, be_armed)
               VALUES (?, ?, ?, ?, 1.0, 0.0, 0)""",
            (tid, LEGACY_BASE_RULE["id"], px, px),
        )
        db.commit()
    except Exception as e:
        log.warning("young_trail variants seed legacy_base: %s", safe_err(e))


def on_entry(tid: str, entry_price: float, entry_ts: float) -> None:
    """Seed every variant row and the first mark. Fail open."""
    try:
        if not tid or not entry_price or entry_price <= 0:
            return
        db = _db()
        init_schema(db)
        px = float(entry_price)
        ts = float(entry_ts)
        db.execute(
            """INSERT OR IGNORE INTO young_trail_marks (tid, ts, price) VALUES (?, ?, ?)""",
            (tid, ts, px),
        )
        db.execute(
            """INSERT OR IGNORE INTO young_trail_entry_stats
               (tid, peak, peak_ts, peak_multiple, time_to_peak_sec)
               VALUES (?, ?, ?, 1.0, 0.0)""",
            (tid, px, ts),
        )
        for rule in VARIANTS:
            db.execute(
                """INSERT OR IGNORE INTO young_trail_variants
                   (tid, variant, peak, last_price, remaining_frac, partial_pnl, be_armed)
                   VALUES (?, ?, ?, ?, 1.0, 0.0, 0)""",
                (tid, rule["id"], px, px),
            )
        db.commit()
    except Exception as e:
        log.warning("young_trail variants on_entry: %s", safe_err(e))


def _record_mark(db, tid: str, ts: float, price: float, entry_ts: float,
                 entry_price: float) -> None:
    db.execute(
        """INSERT OR IGNORE INTO young_trail_marks (tid, ts, price) VALUES (?, ?, ?)""",
        (tid, float(ts), float(price)),
    )
    row = db.execute(
        "SELECT peak, peak_ts FROM young_trail_entry_stats WHERE tid = ?", (tid,),
    ).fetchone()
    if row is None:
        db.execute(
            """INSERT INTO young_trail_entry_stats
               (tid, peak, peak_ts, peak_multiple, time_to_peak_sec)
               VALUES (?, ?, ?, ?, ?)""",
            (tid, float(price), float(ts),
             float(price) / float(entry_price) if entry_price else 1.0,
             max(0.0, float(ts) - float(entry_ts))),
        )
        return
    peak = float(row["peak"])
    peak_ts = float(row["peak_ts"])
    if float(price) > peak + 1e-15:
        peak = float(price)
        peak_ts = float(ts)
        db.execute(
            """UPDATE young_trail_entry_stats
               SET peak = ?, peak_ts = ?, peak_multiple = ?, time_to_peak_sec = ?
               WHERE tid = ?""",
            (peak, peak_ts,
             peak / float(entry_price) if entry_price else 1.0,
             max(0.0, peak_ts - float(entry_ts)), tid),
        )


def _parent(db, tid: str):
    return db.execute(
        "SELECT entry_ts, entry_price, last_price, peak FROM young_trail WHERE tid = ?",
        (tid,),
    ).fetchone()


def _close_variant(db, tid: str, variant: str, exit_ts: float, exit_price: float,
                   reason: str, state: dict, entry_price: float) -> None:
    rest = float(state.get("remaining_frac") or 1.0)
    partial = float(state.get("partial_pnl") or 0.0)
    pnl = partial + pnl_usd(exit_price, entry_price, TICKET_USD * rest)
    db.execute(
        """UPDATE young_trail_variants
           SET exit_ts = ?, exit_price = ?, exit_reason = ?, pnl_usd = ?,
               peak = ?, last_price = ?, remaining_frac = ?, partial_pnl = ?,
               be_armed = ?
           WHERE tid = ? AND variant = ? AND exit_ts IS NULL""",
        (float(exit_ts), float(exit_price), reason, pnl,
         float(state["peak"]), float(exit_price), rest, partial,
         1 if state.get("be_armed") else 0, tid, variant),
    )


def on_marks(prices: dict | None, now: float) -> None:
    """Apply this cycle's prices to every still-open variant. Same marks as live."""
    try:
        prices = prices or {}
        db = _db()
        init_schema(db)
        rows = db.execute(
            "SELECT * FROM young_trail_variants WHERE exit_ts IS NULL",
        ).fetchall()
        if not rows:
            db.commit()
            return
        by_tid: dict[str, list] = {}
        for row in rows:
            by_tid.setdefault(row["tid"], []).append(row)
        for tid, group in by_tid.items():
            parent = _parent(db, tid)
            if parent is None:
                continue
            entry_price = float(parent["entry_price"])
            entry_ts = float(parent["entry_ts"])
            import price_guard
            raw_px = _f(prices.get(tid), None)
            prev = _f(parent["last_price"], None) or entry_price
            px = price_guard.accept_price(
                raw_px, entry=entry_price, prev=prev, tid=tid, now=now,
            )
            if px is not None and px > 0:
                _record_mark(db, tid, now, px, entry_ts, entry_price)
            for row in group:
                rule = VARIANT_BY_ID.get(row["variant"])
                if not rule:
                    continue
                decision, state = decide_variant_tick(
                    rule,
                    entry_price=entry_price,
                    peak=_f(row["peak"], entry_price) or entry_price,
                    price=px,
                    entry_ts=entry_ts,
                    now=float(now),
                    last_price=_f(row["last_price"], None),
                    remaining_frac=_f(row["remaining_frac"], 1.0) or 1.0,
                    partial_pnl=_f(row["partial_pnl"], 0.0) or 0.0,
                    be_armed=bool(row["be_armed"]),
                )
                if decision:
                    reason, exit_px = decision
                    _close_variant(
                        db, tid, row["variant"], now, exit_px, reason, state, entry_price,
                    )
                    continue
                db.execute(
                    """UPDATE young_trail_variants
                       SET peak = ?, last_price = ?, remaining_frac = ?,
                           partial_pnl = ?, be_armed = ?
                       WHERE tid = ? AND variant = ? AND exit_ts IS NULL""",
                    (state["peak"],
                     state["last_price"] if state["last_price"] is not None else row["last_price"],
                     state["remaining_frac"], state["partial_pnl"],
                     1 if state["be_armed"] else 0, tid, row["variant"]),
                )
        db.commit()
    except Exception as e:
        log.warning("young_trail variants on_marks: %s", safe_err(e))


def get_variant(tid: str, variant: str) -> dict | None:
    try:
        db = _db()
        row = db.execute(
            "SELECT * FROM young_trail_variants WHERE tid = ? AND variant = ?",
            (tid, variant),
        ).fetchone()
        if row is None:
            return None
        return {k: row[k] for k in row.keys()}
    except Exception as e:
        log.warning("young_trail variants get: %s", safe_err(e))
        return None


def ranking() -> list[dict]:
    """Closed-only. Rank by realized P&L, then win rate, then n."""
    try:
        db = _db()
        init_schema(db)
        out = []
        for rule in VARIANTS:
            vid = rule["id"]
            rows = db.execute(
                """SELECT pnl_usd FROM young_trail_variants
                   WHERE variant = ? AND exit_ts IS NOT NULL""",
                (vid,),
            ).fetchall()
            pnls = [_f(r["pnl_usd"], 0.0) or 0.0 for r in rows]
            n = len(pnls)
            wins = sum(1 for p in pnls if p > 0)
            open_n = db.execute(
                """SELECT COUNT(*) AS c FROM young_trail_variants
                   WHERE variant = ? AND exit_ts IS NULL""",
                (vid,),
            ).fetchone()["c"]
            realized = sum(pnls)
            out.append({
                "variant": vid,
                "label": rule.get("label") or vid,
                "n": n,
                "open": int(open_n),
                "winning_trades": wins,
                "win_rate": (wins / n) if n else 0.0,
                "realized_pnl_usd": realized,
            })
        out.sort(key=lambda r: (-r["realized_pnl_usd"], -r["win_rate"], -r["n"], r["variant"]))
        return out
    except Exception as e:
        log.warning("young_trail variants ranking: %s", safe_err(e))
        return []


def entry_stats(limit: int | None = 50) -> list[dict]:
    try:
        db = _db()
        init_schema(db)
        sql = """SELECT s.tid, s.peak, s.peak_ts, s.peak_multiple, s.time_to_peak_sec,
                        t.entry_ts, t.entry_price, t.ticker, t.chain
                 FROM young_trail_entry_stats s
                 LEFT JOIN young_trail t ON t.tid = s.tid
                 ORDER BY s.peak_multiple DESC"""
        args: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            args = (int(limit),)
        rows = db.execute(sql, args).fetchall()
        return [{
            "tid": r["tid"],
            "ticker": r["ticker"],
            "chain": r["chain"],
            "entry_ts": r["entry_ts"],
            "entry_price": r["entry_price"],
            "peak": r["peak"],
            "peak_ts": r["peak_ts"],
            "peak_multiple": r["peak_multiple"],
            "time_to_peak_sec": r["time_to_peak_sec"],
        } for r in rows]
    except Exception as e:
        log.warning("young_trail variants entry_stats: %s", safe_err(e))
        return []


def closed_rows(limit: int | None = 100) -> list[dict]:
    try:
        db = _db()
        init_schema(db)
        sql = """SELECT tid, variant, exit_ts, exit_price, exit_reason, pnl_usd
                 FROM young_trail_variants
                 WHERE exit_ts IS NOT NULL
                 ORDER BY exit_ts DESC"""
        args: tuple = ()
        if limit is not None:
            sql += " LIMIT ?"
            args = (int(limit),)
        rows = db.execute(sql, args).fetchall()
        return [{
            "tid": r["tid"], "variant": r["variant"], "exit_ts": r["exit_ts"],
            "exit_price": r["exit_price"], "exit_reason": r["exit_reason"],
            "pnl_usd": r["pnl_usd"],
        } for r in rows]
    except Exception as e:
        log.warning("young_trail variants closed: %s", safe_err(e))
        return []


def briefing_lines() -> list[str]:
    rank = ranking()
    closed_any = [r for r in rank if r["n"] > 0]
    if not closed_any:
        return [f"- Exit variants: no closed trades yet. {CAVEAT}"]
    top = closed_any[:3]
    bits = [
        f"{i}) {r['variant']} ${r['realized_pnl_usd']:+.2f} {r['win_rate']:.0%} n={r['n']}"
        for i, r in enumerate(top, 1)
    ]
    return [f"- Exit variants top 3: {' · '.join(bits)}. {CAVEAT}"]


def ops_payload() -> dict:
    try:
        rank = ranking()
        top = [r for r in rank if r["n"] > 0][:3]
        return {
            "paper": True,
            "simulated": True,
            "live_orders": False,
            "caveat": CAVEAT,
            "ticket_usd": TICKET_USD,
            "variant_defs": list(VARIANTS),
            "ranking": rank,
            "top3": top,
            "closed_trades": closed_rows(limit=100),
            "entry_stats": entry_stats(limit=50),
        }
    except Exception as e:
        log.warning("young_trail variants ops: %s", safe_err(e))
        return {
            "paper": True,
            "simulated": True,
            "live_orders": False,
            "error": "unavailable",
            "caveat": CAVEAT,
            "ranking": [],
            "top3": [],
            "closed_trades": [],
            "entry_stats": [],
        }
