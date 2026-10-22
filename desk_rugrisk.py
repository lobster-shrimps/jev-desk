"""Paper rug-risk log on main-desk judged tokens and picks.

Enqueue is DB-only (no RPC). Features are computed later on the research
sampler via young_trail_rugrisk.compute_features. Shadow/paper only: this
module never gates trades, picks, or the judge.

Own America/New_York day cap (DESK_RUGRISK_DAY_CAP, default 600). Skip the
young_trail Helius reserve (helius_allows day_left>=400) when young_trail_helius
is not accepting entries and has 0 open positions.

Exceptions go through safe_err. Never logs keys or URLs with keys.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import sqlite3
import threading
import time
from datetime import datetime, timezone

from secret_utils import safe_err

log = logging.getLogger("desk_rugrisk")

SOL_NET = 1399811149
DEFAULT_DAY_CAP = 600
DEFAULT_TICK_CAP = 8
TARGET_ENTRY_CALLS = 8
STATUS_PENDING = "pending"
STATUS_OK = "ok"
STATUS_NA = "n/a"
STATUS_ERROR = "error"
STATUS_BUDGET = "budget"
STATUS_UNAVAILABLE = "unavailable"
LOCK_TIMEOUT_SEC = 0.2

FLAG_FOLLOWERS = "followers_lt_100"
FLAG_SNIPER = "sniper_share_ge_50"
FLAG_DEV = "dev_holding_ge_1pct"
FLAG_RECYCLED = "recycled_account_ge_02"
SHADOW_FLAGS = (
    {"id": FLAG_FOLLOWERS, "label": "followers < 100"},
    {"id": FLAG_SNIPER, "label": "sniper_share >= 0.5"},
    {"id": FLAG_DEV, "label": "dev holding >= 1%"},
    {"id": FLAG_RECYCLED, "label": "recycled_account >= 0.2"},
)

try:
    from zoneinfo import ZoneInfo
    _ET = ZoneInfo("America/New_York")
except Exception:
    _ET = None

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
    """Close this thread's cached connection. Tests call this."""
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


def _schema_step(db: sqlite3.Connection, name: str, fn) -> None:
    try:
        fn()
        db.commit()
    except Exception as e:
        log.warning("desk_rugrisk schema %s: %s", name, safe_err(e))
        try:
            db.rollback()
        except Exception:
            pass


def _init_schema(db: sqlite3.Connection) -> None:
    _schema_step(db, "desk_rugrisk", lambda: db.execute("""
    CREATE TABLE IF NOT EXISTS desk_rugrisk (
      tid TEXT NOT NULL,
      ticker TEXT,
      ts REAL NOT NULL,
      stage TEXT,
      judge_score REAL,
      verdict TEXT,
      status TEXT,
      sniper_share REAL,
      sniper_wallets INTEGER,
      bundled INTEGER,
      bundled_buys INTEGER,
      dev_prior_launches INTEGER,
      dev_prior_rugs INTEGER,
      dev_initial_share REAL,
      fresh_holder_share REAL,
      features_json TEXT,
      error TEXT,
      calls_used INTEGER,
      computed_ts REAL,
      PRIMARY KEY (tid, ts)
    )"""))
    _schema_step(db, "idx_desk_rr_status", lambda: db.execute(
        "CREATE INDEX IF NOT EXISTS idx_desk_rr_status ON desk_rugrisk(status)"
    ))
    _schema_step(db, "idx_desk_rr_stage", lambda: db.execute(
        "CREATE INDEX IF NOT EXISTS idx_desk_rr_stage ON desk_rugrisk(stage, judge_score)"
    ))
    _schema_step(db, "desk_rugrisk_meta", lambda: db.execute("""
    CREATE TABLE IF NOT EXISTS desk_rugrisk_meta (
      key TEXT PRIMARY KEY,
      value TEXT
    )"""))


def _num(value, default=None):
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _int(value, default=None):
    n = _num(value, None)
    if n is None:
        return default
    try:
        return int(n)
    except (TypeError, ValueError):
        return default


def _meta_get(key: str) -> str | None:
    try:
        row = _connect().execute(
            "SELECT value FROM desk_rugrisk_meta WHERE key = ?", (key,),
        ).fetchone()
        return None if row is None else row["value"]
    except Exception as e:
        log.warning("desk_rugrisk meta read: %s", safe_err(e))
        return None


def _meta_set(key: str, value: str) -> None:
    try:
        db = _connect()
        db.execute(
            """INSERT INTO desk_rugrisk_meta (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )
        db.commit()
    except Exception as e:
        log.warning("desk_rugrisk meta write: %s", safe_err(e))


def day_cap() -> int:
    raw = os.environ.get("DESK_RUGRISK_DAY_CAP")
    try:
        n = int(raw) if raw not in (None, "") else DEFAULT_DAY_CAP
    except (TypeError, ValueError):
        n = DEFAULT_DAY_CAP
    return max(0, n)


def tick_cap() -> int:
    raw = os.environ.get("DESK_RUGRISK_TICK_CAP")
    try:
        n = int(raw) if raw not in (None, "") else DEFAULT_TICK_CAP
    except (TypeError, ValueError):
        n = DEFAULT_TICK_CAP
    return max(0, n)


def _day_key(now: float | None = None) -> str:
    ts = current_time() if now is None else float(now)
    if _ET is not None:
        return datetime.fromtimestamp(ts, tz=_ET).strftime("%Y-%m-%d")
    return datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%Y-%m-%d")


def _day_used(now: float | None = None) -> int:
    key = _day_key(now)
    if _meta_get("budget_day") != key:
        return 0
    try:
        return int(_meta_get("budget_day_calls") or 0)
    except (TypeError, ValueError):
        return 0


def day_left(now: float | None = None) -> int:
    return max(0, day_cap() - _day_used(now))


def budget_remaining(now: float | None = None) -> dict:
    used = _day_used(now)
    cap = day_cap()
    return {
        "day": _day_key(now),
        "day_used": used,
        "day_cap": cap,
        "day_left": max(0, cap - used),
        "tick_cap": tick_cap(),
    }


def _charge_day(n: int, now: float | None = None) -> None:
    key = _day_key(now)
    used = _day_used(now) + int(n)
    _meta_set("budget_day", key)
    _meta_set("budget_day_calls", str(used))


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


def is_solana(tid: str | None = None, chain=None, net=None) -> bool:
    if net == SOL_NET:
        return True
    if str(chain or "").strip().lower() == "solana":
        return True
    if tid:
        _, parsed = parse_tid(tid)
        return parsed == SOL_NET
    return False


def _yt_helius_over(now: float | None = None) -> bool:
    """True when young_trail_helius is done: not accepting and no open rows."""
    try:
        import young_trail_helius as helius
        if helius.accepting_entries(now):
            return False
        return len(helius.open_rows() or []) == 0
    except Exception as e:
        log.warning("desk_rugrisk helius over: %s", safe_err(e))
        return False


def _skip_helius_reserve(now: float | None = None) -> bool:
    return _yt_helius_over(now)


def _followers(d: dict) -> float | None:
    """Freshest fxtwitter count. Same helper as the rug veto and judge text."""
    try:
        import x_lookup
        rec = x_lookup.fresh_followers(d)
        if rec.get("followers") is not None:
            return _num(rec.get("followers"))
    except Exception as e:
        log.warning("desk_rugrisk followers: %s", safe_err(e))
    n = _num(d.get("x_followers"))
    if n is not None:
        return n
    xa = d.get("x_account")
    if isinstance(xa, dict):
        return _num(xa.get("followers") if xa.get("followers") is not None
                    else xa.get("followers_count"))
    return None


def _dev_holding(d: dict) -> float | None:
    if d.get("developer_holding_percentage") is not None:
        return _num(d.get("developer_holding_percentage"))
    return _num(d.get("dev_pct"))


def _recycled(ans) -> float | None:
    if not isinstance(ans, dict):
        return None
    block = ans.get("recycled_account")
    if isinstance(block, dict):
        return _num(block.get("noul"))
    return _num(block)


def shadow_guards(d: dict | None, ans=None, sniper_share=None) -> dict:
    """Flag values from whatever is present. Missing fields are null."""
    d = d if isinstance(d, dict) else {}
    followers = _followers(d)
    dev = _dev_holding(d)
    recycled = _recycled(ans if ans is not None else d.get("answers") or {})
    sn = _num(sniper_share if sniper_share is not None else d.get("sniper_share"))
    return {
        FLAG_FOLLOWERS: (followers < 100) if followers is not None else None,
        FLAG_SNIPER: (sn >= 0.5) if sn is not None else None,
        FLAG_DEV: (dev >= 1.0) if dev is not None else None,
        FLAG_RECYCLED: (recycled >= 0.2) if recycled is not None else None,
        "followers": followers,
        "dev_holding_pct": dev,
        "recycled_account": recycled,
        "sniper_share": sn,
    }


def _loads(raw) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        out = json.loads(raw)
        return out if isinstance(out, dict) else {}
    except Exception:
        return {}


def lookup_creator(tid: str) -> str | None:
    """First creators[] entry on the newest desk_rugrisk row. No RPC."""
    if not tid:
        return None
    try:
        rows = _connect().execute(
            """SELECT features_json FROM desk_rugrisk
               WHERE tid = ? ORDER BY ts DESC""",
            (tid,),
        ).fetchall()
        for row in rows:
            blob = _loads(row["features_json"])
            creators = blob.get("creators")
            if isinstance(creators, list):
                for item in creators:
                    wallet = str(item or "").strip()
                    if wallet:
                        return wallet
    except Exception as e:
        log.warning("desk_rugrisk lookup_creator: %s", safe_err(e))
    return None


def enqueue(d, verdict, score, ts, ans=None) -> bool:
    """Insert one judged token. DB only, no RPC. Idempotent on (tid, ts)."""
    try:
        d = d if isinstance(d, dict) else {}
        tid = d.get("tid")
        if not tid:
            return False
        now = current_time() if ts is None else float(ts)
        stage = "pick" if str(verdict or "") == "pick" else "judged"
        mint, net = parse_tid(tid)
        guards = shadow_guards(d, ans=ans)
        fol_ts = None
        fol_src = None
        try:
            import x_lookup
            rec = x_lookup.fresh_followers(d)
            fol_ts = rec.get("fetched_at")
            fol_src = rec.get("source")
        except Exception:
            pass
        blob = {
            "shadow_guards": {k: guards.get(k) for k in (
                FLAG_FOLLOWERS, FLAG_SNIPER, FLAG_DEV, FLAG_RECYCLED,
            )},
            "followers": guards.get("followers"),
            "followers_ts": fol_ts,
            "followers_source": fol_src,
            "dev_holding_pct": guards.get("dev_holding_pct"),
            "recycled_account": guards.get("recycled_account"),
            "mint": mint,
            "creator": d.get("creator") or d.get("dev_wallet"),
            "chain": d.get("chain"),
            "net": d.get("net") if d.get("net") is not None else net,
        }
        db = _connect()
        cur = db.execute(
            """INSERT OR IGNORE INTO desk_rugrisk (
                 tid, ticker, ts, stage, judge_score, verdict, status,
                 features_json, calls_used
               ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (
                str(tid), d.get("ticker"), now, stage, _num(score),
                None if verdict is None else str(verdict), STATUS_PENDING,
                json.dumps(blob, default=str, sort_keys=True), 0,
            ),
        )
        db.commit()
        return cur.rowcount > 0
    except Exception as e:
        log.warning("desk_rugrisk enqueue: %s", safe_err(e))
        return False


def _pending_rows(limit: int) -> list[dict]:
    db = _connect()
    rows = db.execute(
        """SELECT * FROM desk_rugrisk
           WHERE status IN (?, ?, ?)
           ORDER BY CASE
             WHEN stage = 'pick' THEN 0
             WHEN COALESCE(judge_score, 0) >= 0.5 THEN 1
             ELSE 2
           END, ts ASC
           LIMIT ?""",
        (STATUS_PENDING, STATUS_BUDGET, STATUS_UNAVAILABLE, int(limit)),
    ).fetchall()
    return [dict(r) for r in rows]


def _compute_features(mint: str, creator: str | None, now: float) -> dict:
    """Wrap young_trail_rugrisk charge so the desk ET cap is independent."""
    import young_trail_rugrisk as rug
    orig_can = rug._can_charge
    orig_charge = rug._charge

    def can(n: int = 1, when=None) -> bool:
        when = now if when is None else when
        if n <= 0:
            return True
        if not _skip_helius_reserve(when):
            try:
                if not rug.helius_allows(when):
                    return False
            except Exception as e:
                log.warning("desk_rugrisk helius_allows: %s", safe_err(e))
        return day_left(when) >= n

    def charge(n: int = 1, when=None) -> bool:
        when = now if when is None else when
        if n <= 0:
            return True
        if not can(n, when):
            return False
        try:
            rug._cycle_calls += n
        except Exception:
            pass
        _charge_day(n, when)
        return True

    if not rug.acquire_charge_lock():
        return {"status": STATUS_UNAVAILABLE, "error": "charge_lock_timeout"}
    try:
        rug._can_charge = can
        rug._charge = charge
        return rug.compute_features(mint, creator, now=now)
    finally:
        rug._can_charge = orig_can
        rug._charge = orig_charge
        rug.release_charge_lock()


def _store_computed(row: dict, feat: dict, now: float) -> None:
    blob = _loads(row.get("features_json"))
    guards = dict(blob.get("shadow_guards") or {})
    sn = feat.get("sniper_share")
    if sn is not None:
        guards[FLAG_SNIPER] = float(sn) >= 0.5
    extra = feat.get("features_json")
    if isinstance(extra, dict):
        for k, v in extra.items():
            if k == "shadow_guards":
                continue
            blob[k] = v
    blob["shadow_guards"] = guards
    status = feat.get("status") or STATUS_ERROR
    db = _connect()
    db.execute(
        """UPDATE desk_rugrisk SET
             status = ?, sniper_share = ?, sniper_wallets = ?,
             bundled = ?, bundled_buys = ?,
             dev_prior_launches = ?, dev_prior_rugs = ?,
             dev_initial_share = ?, fresh_holder_share = ?,
             features_json = ?, error = ?, calls_used = ?, computed_ts = ?
           WHERE tid = ? AND ts = ?""",
        (
            status,
            feat.get("sniper_share"), feat.get("sniper_wallets"),
            None if feat.get("bundled") is None else (1 if feat.get("bundled") else 0),
            feat.get("bundled_buys"),
            feat.get("dev_prior_launches"), feat.get("dev_prior_rugs"),
            feat.get("dev_initial_share"), feat.get("fresh_holder_share"),
            json.dumps(blob, default=str, sort_keys=True),
            feat.get("error"),
            _int(feat.get("calls_used") or (extra or {}).get("calls_used"), 0)
            if not isinstance(extra, dict)
            else _int(feat.get("calls_used") or extra.get("calls_used"), 0),
            float(now),
            row["tid"], row["ts"],
        ),
    )
    db.commit()


def compute_one(row: dict, now: float | None = None) -> dict | None:
    ts = current_time() if now is None else float(now)
    blob = _loads(row.get("features_json"))
    mint = blob.get("mint") or parse_tid(row.get("tid"))[0]
    creator = blob.get("creator")
    chain = blob.get("chain")
    net = blob.get("net")
    if not is_solana(row.get("tid"), chain=chain, net=net):
        feat = {
            "status": STATUS_NA, "error": "non-solana",
            "features_json": {"calls_used": 0}, "calls_used": 0,
        }
        _store_computed(row, feat, ts)
        return feat
    if not mint:
        feat = {
            "status": STATUS_UNAVAILABLE, "error": "no mint",
            "features_json": {"calls_used": 0}, "calls_used": 0,
        }
        _store_computed(row, feat, ts)
        return feat
    if day_left(ts) < 1:
        feat = {
            "status": STATUS_BUDGET, "error": "budget",
            "features_json": blob, "calls_used": row.get("calls_used") or 0,
        }
        _store_computed(row, feat, ts)
        return feat
    try:
        feat = _compute_features(str(mint), creator, ts)
    except Exception as e:
        msg = safe_err(e)
        log.warning("desk_rugrisk compute %s: %s", str(mint)[:12], msg)
        feat = {"status": STATUS_ERROR, "error": msg, "features_json": {}, "calls_used": 0}
    used = _int((feat.get("features_json") or {}).get("calls_used") if isinstance(
        feat.get("features_json"), dict) else None, 0)
    if feat.get("calls_used") is None:
        feat["calls_used"] = used
    if feat.get("status") == STATUS_OK and used > TARGET_ENTRY_CALLS:
        log.info("desk_rugrisk calls_used=%d tid=%s paper=1", used, row.get("tid"))
    _store_computed(row, feat, ts)
    return feat


def on_sampler_tick(ts=None, prices=None) -> dict:
    """Compute pending desk rows. Priority: picks, score>=0.5, other judged."""
    empty = {"pending": 0, "computed": 0}
    try:
        now = current_time() if ts is None else float(ts)
        cap = tick_cap()
        if cap <= 0 or day_left(now) <= 0:
            return empty
        rows = _pending_rows(cap)
        computed = 0
        for row in rows:
            if day_left(now) <= 0:
                break
            try:
                compute_one(row, now=now)
                computed += 1
            except Exception as e:
                log.warning("desk_rugrisk tick row: %s", safe_err(e))
        return {"pending": len(rows), "computed": computed}
    except Exception as e:
        log.warning("desk_rugrisk sampler: %s", safe_err(e))
        return empty


def all_rows() -> list[dict]:
    try:
        return [dict(r) for r in _connect().execute(
            "SELECT * FROM desk_rugrisk ORDER BY ts DESC"
        ).fetchall()]
    except Exception as e:
        log.warning("desk_rugrisk rows: %s", safe_err(e))
        return []


def counts() -> dict:
    rows = all_rows()
    out = {
        "n": len(rows),
        "pending": 0, "ok": 0, "n_a": 0, "error": 0,
        "budget": 0, "unavailable": 0,
        "picks": 0, "judged": 0,
    }
    for r in rows:
        st = r.get("status") or ""
        if st == STATUS_PENDING:
            out["pending"] += 1
        elif st == STATUS_OK:
            out["ok"] += 1
        elif st == STATUS_NA:
            out["n_a"] += 1
        elif st == STATUS_ERROR:
            out["error"] += 1
        elif st == STATUS_BUDGET:
            out["budget"] += 1
        elif st == STATUS_UNAVAILABLE:
            out["unavailable"] += 1
        if r.get("stage") == "pick" or r.get("verdict") == "pick":
            out["picks"] += 1
        else:
            out["judged"] += 1
    return out


def _shadow_pick_pnl() -> dict[str, float]:
    out: dict[str, float] = {}
    try:
        import shadow_ledger
        for rec in shadow_ledger.closed_positions(limit=None) or []:
            tid = rec.get("tid")
            pnl = _num(rec.get("realized_pnl_usd"))
            if tid and pnl is not None and not rec.get("unmeasured"):
                out[str(tid)] = out.get(str(tid), 0.0) + pnl
    except Exception as e:
        log.warning("desk_rugrisk shadow pnl: %s", safe_err(e))
    return out


def _outcome_paths() -> dict[str, dict]:
    out: dict[str, dict] = {}
    try:
        import outcomes
        db = outcomes.readonly_connect()
        try:
            rows = db.execute(
                """SELECT tid, sim_current_pnl, sim_current_reason,
                          sim_trail_pnl, sim_trail_reason, complete
                   FROM outcomes"""
            ).fetchall()
        finally:
            db.close()
        for r in rows:
            tid = r["tid"]
            if not tid:
                continue
            out[str(tid)] = {
                "sim_current_pnl": r["sim_current_pnl"],
                "sim_current_reason": r["sim_current_reason"],
                "sim_trail_pnl": r["sim_trail_pnl"],
                "sim_trail_reason": r["sim_trail_reason"],
                "complete": r["complete"],
            }
    except Exception as e:
        log.warning("desk_rugrisk outcomes: %s", safe_err(e))
    return out


def _flag_value(row: dict, flag_id: str):
    blob = _loads(row.get("features_json"))
    guards = blob.get("shadow_guards") or {}
    return guards.get(flag_id)


def _is_pick(row: dict) -> bool:
    return row.get("stage") == "pick" or row.get("verdict") == "pick"


def counterfactual_for_flag(flag_id: str, rows: list[dict] | None = None,
                            pick_pnl: dict | None = None,
                            paths: dict | None = None) -> dict:
    data = rows if rows is not None else all_rows()
    pick_pnl = pick_pnl if pick_pnl is not None else _shadow_pick_pnl()
    paths = paths if paths is not None else _outcome_paths()

    def bucket(group: list[dict]) -> dict:
        pick_rows = [r for r in group if _is_pick(r)]
        pnls = []
        for r in pick_rows:
            p = pick_pnl.get(r.get("tid"))
            if p is not None:
                pnls.append(float(p))
        reasons: dict[str, int] = {}
        for r in group:
            oc = paths.get(r.get("tid")) or {}
            reason = oc.get("sim_current_reason") or oc.get("sim_trail_reason")
            if reason:
                reasons[str(reason)] = reasons.get(str(reason), 0) + 1
        return {
            "n": len(group),
            "picks": len(pick_rows),
            "pick_pnl": round(sum(pnls), 4) if pnls else 0.0,
            "pick_pnl_n": len(pnls),
            "paths": reasons,
        }

    hit = [r for r in data if _flag_value(r, flag_id) is True]
    miss = [r for r in data if _flag_value(r, flag_id) is False]
    unknown = [r for r in data if _flag_value(r, flag_id) is None]
    label = next((f["label"] for f in SHADOW_FLAGS if f["id"] == flag_id), flag_id)
    return {
        "id": flag_id,
        "label": label,
        "hit": bucket(hit),
        "miss": bucket(miss),
        "unknown": bucket(unknown),
    }


def counterfactuals(rows: list[dict] | None = None) -> list[dict]:
    try:
        data = rows if rows is not None else all_rows()
        pick_pnl = _shadow_pick_pnl()
        paths = _outcome_paths()
        return [
            counterfactual_for_flag(f["id"], data, pick_pnl, paths)
            for f in SHADOW_FLAGS
        ]
    except Exception as e:
        log.warning("desk_rugrisk counterfactual: %s", safe_err(e))
        return []


def _ops_entry(row: dict) -> dict:
    d = dict(row)
    blob = _loads(d.get("features_json"))
    guards = blob.get("shadow_guards") or {}
    d["shadow_guards"] = guards
    d["followers_lt_100"] = guards.get(FLAG_FOLLOWERS)
    d["sniper_share_ge_50"] = guards.get(FLAG_SNIPER)
    d["dev_holding_ge_1pct"] = guards.get(FLAG_DEV)
    d["recycled_account_ge_02"] = guards.get(FLAG_RECYCLED)
    d["is_pick"] = _is_pick(d)
    return d


def ops_payload() -> dict:
    """JSON for GET /ops/desk_rugrisk. Paper only; no gating."""
    try:
        now = current_time()
        rows = all_rows()
        return {
            "paper": True,
            "simulated": True,
            "live_orders": False,
            "gates_entries": False,
            "gates_picks": False,
            "caveat": (
                "Paper rug-risk log on judged desk tokens. Shadow-guard flags "
                "and skip-style counters are counterfactual — they do not gate "
                "picks, trades, or the judge."
            ),
            "budget": budget_remaining(now),
            "counts": counts(),
            "flags": list(SHADOW_FLAGS),
            "counterfactuals": counterfactuals(rows),
            "entries": [_ops_entry(r) for r in rows[:80]],
        }
    except Exception as e:
        log.warning("desk_rugrisk ops: %s", safe_err(e))
        return {
            "paper": True,
            "simulated": True,
            "live_orders": False,
            "gates_entries": False,
            "gates_picks": False,
            "error": "unavailable",
            "budget": budget_remaining(),
            "counts": counts(),
            "counterfactuals": [],
            "entries": [],
        }
