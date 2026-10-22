"""Log-only rug veto before the final pick. Paper/shadow only.

For each token that reaches the final judge, reuse desk_rugrisk (sniper share,
bundled, sniper wallets) under its existing Helius / ET day cap, plus the
fxtwitter follower count already on the dossier. would_skip is sniper_share
>= 0.5 OR followers < 100. Unknown followers record reason followers_unknown
and do not skip.

Never gates picks, the judge, or the shadow ledger. Fail open. safe_err only.
On cycle-budget timeout the row is status=unavailable.
"""
from __future__ import annotations

import json
import logging
import os
import pathlib
import sqlite3
import threading
import time

from secret_utils import safe_err

log = logging.getLogger("rug_veto_shadow")

SNIPER_SKIP = 0.5
FOLLOWERS_SKIP = 100.0
MIN_REMAINING_SEC = 8.0
LOCK_TIMEOUT_SEC = 0.2
STATUS_OK = "ok"
STATUS_UNAVAILABLE = "unavailable"
STATUS_BUDGET = "budget"
STATUS_NA = "n/a"

# Live-shaped fixtures for the five historical picks. Used when outcomes.db
# has no row. Numbers come from ops/tests (PEPA followers 40, PROWLER sniper
# 0.75 bundled). Missing fields stay None so would_skip cannot invent a skip.
HISTORICAL_PICKS = (
    {"ticker": "hellop", "tid": "hellop:1399811149", "chain": "solana",
     "judge_score": 0.70, "sniper_share": None, "bundled": None,
     "sniper_wallets": None, "followers": None, "became_pick": True},
    {"ticker": "CHILL", "tid": "CHILL:1399811149", "chain": "solana",
     "judge_score": 0.64, "sniper_share": None, "bundled": None,
     "sniper_wallets": None, "followers": None, "became_pick": True},
    {"ticker": "HORN", "tid": "HornMint111111111111111111111111111111:1399811149",
     "chain": "solana", "judge_score": 0.72, "sniper_share": None, "bundled": None,
     "sniper_wallets": None, "followers": None, "became_pick": True},
    {"ticker": "PROWLER", "tid": "ProwlerMint11111111111111111111111111:1399811149",
     "chain": "solana", "judge_score": 0.81, "sniper_share": 0.75, "bundled": 1,
     "sniper_wallets": 8, "followers": None, "became_pick": True},
    {"ticker": "PEPA", "tid": "PepaMint111111111111111111111111111111:1399811149",
     "chain": "solana", "judge_score": 0.72, "sniper_share": None, "bundled": None,
     "sniper_wallets": None, "followers": 40.0,
     "x_account": {"handle": "PEPA", "followers": 40},
     "became_pick": True},
)

_tls = threading.local()
_schema_lock = threading.Lock()
_time_fn = time.time
_prepared: dict[str, dict] = {}


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
    _prepared.clear()


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
        log.warning("rug_veto schema %s: %s", name, safe_err(e))
        try:
            db.rollback()
        except Exception:
            pass


def _init_schema(db: sqlite3.Connection) -> None:
    _schema_step(db, "rug_veto_shadow", lambda: db.execute("""
    CREATE TABLE IF NOT EXISTS rug_veto_shadow (
      tid TEXT NOT NULL,
      ticker TEXT,
      chain TEXT,
      ts REAL NOT NULL,
      judge_score REAL,
      sniper_share REAL,
      sniper_wallets INTEGER,
      bundled INTEGER,
      followers REAL,
      would_skip INTEGER,
      reasons TEXT,
      became_pick INTEGER,
      status TEXT,
      PRIMARY KEY (tid, ts)
    )"""))
    _schema_step(db, "idx_rvs_ts", lambda: db.execute(
        "CREATE INDEX IF NOT EXISTS idx_rvs_ts ON rug_veto_shadow(ts DESC)"
    ))


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


def _followers(d: dict | None):
    d = d if isinstance(d, dict) else {}
    n = _num(d.get("x_followers"))
    if n is not None:
        return n
    xa = d.get("x_account")
    if isinstance(xa, dict):
        return _num(xa.get("followers") if xa.get("followers") is not None
                    else xa.get("followers_count"))
    return _num(d.get("followers"))


def _soft_scores_of(src: dict | None) -> dict:
    src = src if isinstance(src, dict) else {}
    scores = src.get("soft_scores")
    if isinstance(scores, dict):
        return scores
    blob = _loads(src.get("soft_scores_json"))
    if blob:
        return blob
    feat = _loads(src.get("features_json"))
    extra = feat.get("soft_scores")
    return extra if isinstance(extra, dict) else {}


def _project_followers(d: dict | None, ans=None, soft_scores=None):
    """Followers for the <100 check only when the account is the project's.

    Same rule as pick.summary: noul >= 0.70 or handle matches ticker.
    Borrowed mega-accounts and unverified handles are unknown (no skip).
    """
    d = d if isinstance(d, dict) else {}
    try:
        from pick import account_is_the_project, project_followers
        if not account_is_the_project(d, ans=ans, soft_scores=soft_scores):
            return None
        n = project_followers(d, ans=ans, soft_scores=soft_scores)
        if n is not None:
            return _num(n)
    except Exception as e:
        log.warning("rug_veto project followers: %s", safe_err(e))
    return None


def decide(sniper_share=None, followers=None, status: str | None = None) -> dict:
    """would_skip + reasons. Unknown followers never skip. Timeout → unavailable."""
    reasons = []
    would_skip = False
    st = status or STATUS_OK
    if st in (STATUS_UNAVAILABLE, STATUS_BUDGET) and sniper_share is None:
        reasons.append("unavailable")
    if followers is None:
        reasons.append("followers_unknown")
    elif followers < FOLLOWERS_SKIP:
        reasons.append("followers_lt_100")
        would_skip = True
    sn = _num(sniper_share)
    if sn is not None and sn >= SNIPER_SKIP:
        reasons.append("sniper_share_ge_50")
        would_skip = True
    return {
        "would_skip": bool(would_skip),
        "reasons": reasons,
        "sniper_share": sn,
        "followers": _num(followers),
        "status": st if sn is not None or st != STATUS_OK else st,
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


def _norm_ticker(value) -> str:
    return str(value or "").strip().upper()


def _tid_matches(left, right) -> bool:
    a, b = str(left or ""), str(right or "")
    if not a or not b:
        return False
    if a == b:
        return True
    return a.split(":", 1)[0] == b.split(":", 1)[0] and len(a.split(":", 1)[0]) >= 8


def _latest_rugrisk(tid: str | None, ticker: str | None = None) -> dict | None:
    """Newest desk_rugrisk row by tid or ticker. Fail-soft."""
    try:
        import desk_rugrisk
        rows = list(desk_rugrisk.all_rows() or [])
    except Exception as e:
        log.warning("rug_veto rugrisk lookup: %s", safe_err(e))
        return None
    if not rows:
        return None
    hit = None
    if tid:
        exact = [r for r in rows if r.get("tid") == tid]
        if exact:
            hit = max(exact, key=lambda r: float(r.get("ts") or 0))
        else:
            loose = [r for r in rows if _tid_matches(r.get("tid"), tid)]
            if loose:
                hit = max(loose, key=lambda r: float(r.get("ts") or 0))
    key = _norm_ticker(ticker)
    if hit is None and key:
        named = [r for r in rows if _norm_ticker(r.get("ticker")) == key]
        if named:
            hit = max(named, key=lambda r: float(r.get("ts") or 0))
    return hit


def _latest_cycle_token(tid: str | None, ticker: str | None = None) -> dict | None:
    """Newest cycle_history token / shadow_kill by tid or ticker. Fail-soft."""
    try:
        import cycle_history
        db = cycle_history._connect()
    except Exception as e:
        log.warning("rug_veto cycle_history: %s", safe_err(e))
        return None
    row = None
    try:
        if tid:
            row = db.execute(
                "SELECT * FROM tokens WHERE tid = ? ORDER BY ts DESC LIMIT 1",
                (tid,),
            ).fetchone()
        if row is None and ticker:
            row = db.execute(
                "SELECT * FROM tokens WHERE upper(ticker) = ? ORDER BY ts DESC LIMIT 1",
                (_norm_ticker(ticker),),
            ).fetchone()
        token = None
        if row is not None:
            try:
                token = cycle_history._token_dict(row)
            except Exception:
                token = dict(row)
        kill = None
        try:
            if tid:
                kill = db.execute(
                    "SELECT * FROM shadow_kills WHERE tid = ? ORDER BY ts DESC LIMIT 1",
                    (tid,),
                ).fetchone()
            if kill is None and ticker:
                kill = db.execute(
                    """SELECT * FROM shadow_kills
                       WHERE upper(ticker) = ? ORDER BY ts DESC LIMIT 1""",
                    (_norm_ticker(ticker),),
                ).fetchone()
        except Exception:
            kill = None
        if token is None and kill is None:
            return None
        out = dict(token or {})
        if kill is not None:
            try:
                kd = cycle_history._shadow_kill_dict(kill)
            except Exception:
                kd = dict(kill)
            if out.get("x_followers") is None:
                out["x_followers"] = kd.get("x_followers")
            if not out.get("soft_scores") and kd.get("soft_scores"):
                out["soft_scores"] = kd.get("soft_scores")
            if out.get("tid") is None:
                out["tid"] = kd.get("tid")
            if out.get("judge_score") is None:
                out["judge_score"] = kd.get("judge_score")
        return out
    except Exception as e:
        log.warning("rug_veto cycle_history lookup: %s", safe_err(e))
        return None


def _merge_real_fields(src: dict) -> dict:
    """Overlay desk_rugrisk + cycle_history facts onto a fixture/src row."""
    src = dict(src or {})
    rug = _latest_rugrisk(src.get("tid"), src.get("ticker"))
    hist = _latest_cycle_token(src.get("tid"), src.get("ticker"))
    if rug:
        for key in ("sniper_share", "bundled", "sniper_wallets", "status"):
            if rug.get(key) is not None:
                src[key] = rug.get(key)
        if rug.get("tid"):
            src["tid"] = rug["tid"]
        blob = _loads(rug.get("features_json"))
        if src.get("followers") is None and blob.get("followers") is not None:
            src["followers"] = blob.get("followers")
        if src.get("x_followers") is None and blob.get("followers") is not None:
            src["x_followers"] = blob.get("followers")
        src["source"] = src.get("source") or "desk_rugrisk"
    if hist:
        placeholders = {h.get("tid") for h in HISTORICAL_PICKS}
        if hist.get("tid") and (
            not src.get("tid") or src.get("tid") in placeholders
        ):
            src["tid"] = hist["tid"]
        if src.get("judge_score") is None and hist.get("judge_score") is not None:
            src["judge_score"] = hist.get("judge_score")
        if src.get("followers") is None and hist.get("x_followers") is not None:
            src["followers"] = hist.get("x_followers")
        if src.get("x_followers") is None and hist.get("x_followers") is not None:
            src["x_followers"] = hist.get("x_followers")
        if hist.get("soft_scores"):
            src["soft_scores"] = hist.get("soft_scores")
        if hist.get("ticker") and not src.get("ticker"):
            src["ticker"] = hist.get("ticker")
        src["source"] = src.get("source") or "cycle_history"
    return src


def _features_from_row(row: dict | None) -> dict:
    row = row if isinstance(row, dict) else {}
    blob = _loads(row.get("features_json"))
    followers = _num(blob.get("followers"))
    if followers is None:
        followers = _num(row.get("followers"))
    if followers is None:
        followers = _num(row.get("x_followers"))
    bundled = None if row.get("bundled") is None else int(bool(row.get("bundled")))
    try:
        import young_trail_rugrisk
        recomputed = young_trail_rugrisk.recompute_bundled_from_cached(row)
        if recomputed and recomputed.get("bundled") is not None:
            bundled = int(bool(recomputed.get("bundled")))
    except Exception as e:
        log.warning("rug_veto bundled recompute: %s", safe_err(e))
    return {
        "sniper_share": _num(row.get("sniper_share")),
        "sniper_wallets": _int(row.get("sniper_wallets")),
        "bundled": bundled,
        "followers": followers,
        "status": row.get("status") or STATUS_OK,
        "soft_scores": _soft_scores_of(row),
    }


def _remaining(deadline) -> float:
    if deadline is None:
        return 1e9
    try:
        return float(deadline) - current_time()
    except (TypeError, ValueError):
        return 0.0


def _compute_missing(d: dict, now: float, deadline) -> dict:
    """Run desk_rugrisk.compute_one if budget and cycle time remain."""
    tid = d.get("tid")
    existing = _latest_rugrisk(tid, d.get("ticker"))
    feat = _features_from_row(existing)
    if feat.get("sniper_share") is not None and feat.get("status") == STATUS_OK:
        return feat
    if _remaining(deadline) < MIN_REMAINING_SEC:
        feat["status"] = STATUS_UNAVAILABLE
        return feat
    try:
        import desk_rugrisk
        if desk_rugrisk.day_left(now) < 1:
            feat["status"] = STATUS_BUDGET
            return feat
        desk_rugrisk.enqueue(d, "judged", d.get("judge_score"), now)
        row = _latest_rugrisk(tid)
        if not row:
            feat["status"] = STATUS_UNAVAILABLE
            return feat
        desk_rugrisk.compute_one(row, now=now)
        return _features_from_row(_latest_rugrisk(tid))
    except Exception as e:
        log.warning("rug_veto compute: %s", safe_err(e))
        feat["status"] = STATUS_UNAVAILABLE
        return feat


def prepare(survivors, *, deadline=None, now: float | None = None) -> dict:
    """Before pick(). Stash rug features on each dossier. Never raise out."""
    out = {"n": 0, "computed": 0}
    try:
        ts = current_time() if now is None else float(now)
        _prepared.clear()
        for item in survivors or []:
            d = item[0] if isinstance(item, (tuple, list)) and item else item
            if not isinstance(d, dict) or not d.get("tid"):
                continue
            feat = _compute_missing(d, ts, deadline)
            ans = item[1] if isinstance(item, (tuple, list)) and len(item) > 1 else {}
            scores = feat.get("soft_scores") or _soft_scores_of(d)
            if feat.get("followers") is None:
                feat["followers"] = _followers(d)
            feat["followers"] = _project_followers(
                {**d, "followers": feat.get("followers"),
                 "x_followers": feat.get("followers")},
                ans=ans, soft_scores=scores,
            )
            d["_rug_veto"] = feat
            _prepared[str(d["tid"])] = feat
            out["n"] += 1
            if feat.get("status") == STATUS_OK and feat.get("sniper_share") is not None:
                out["computed"] += 1
        return out
    except Exception as e:
        log.warning("rug_veto prepare: %s", safe_err(e))
        return out


def _became_pick(order, ticker, tid) -> bool:
    token = order.get("token") if isinstance(order, dict) else None
    if not isinstance(token, dict):
        return False
    if ticker and token.get("ticker") == ticker:
        return True
    addr = token.get("address")
    if tid and addr and str(tid).startswith(str(addr)):
        return True
    return False


def _insert(row: dict) -> None:
    db = _connect()
    db.execute(
        """INSERT OR REPLACE INTO rug_veto_shadow (
             tid, ticker, chain, ts, judge_score, sniper_share, sniper_wallets,
             bundled, followers, would_skip, reasons, became_pick, status
           ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
        (
            row.get("tid"), row.get("ticker"), row.get("chain"),
            float(row.get("ts") or current_time()),
            _num(row.get("judge_score")), _num(row.get("sniper_share")),
            _int(row.get("sniper_wallets")),
            None if row.get("bundled") is None else int(bool(row.get("bundled"))),
            _num(row.get("followers")),
            1 if row.get("would_skip") else 0,
            json.dumps(row.get("reasons") or [], default=str),
            1 if row.get("became_pick") else 0,
            row.get("status") or STATUS_OK,
        ),
    )
    db.commit()


def record(stats=None, survivors=None, order=None, *, now: float | None = None) -> int:
    """After judge verdicts. Write one paper row per final-judge token."""
    written = 0
    try:
        ts = current_time() if now is None else float(now)
        stats = stats if isinstance(stats, dict) else {}
        dossiers = {}
        for item in survivors or []:
            d = item[0] if isinstance(item, (tuple, list)) and item else item
            if isinstance(d, dict) and d.get("tid"):
                dossiers[d["tid"]] = d
        for row in stats.get("tokens") or []:
            if not isinstance(row, dict) or row.get("stage") != "judged":
                continue
            if row.get("judge_verdict") == "error":
                continue
            tid = row.get("tid")
            if not tid:
                continue
            d = dossiers.get(tid) or {}
            feat = dict(d.get("_rug_veto") or _prepared.get(str(tid)) or {})
            scores = feat.get("soft_scores") or _soft_scores_of(d) or _soft_scores_of(row)
            raw_followers = feat.get("followers")
            if raw_followers is None:
                raw_followers = _followers(d) if d else _followers(row)
            feat["followers"] = _project_followers(
                {**(d or {}), **row, "followers": raw_followers,
                 "x_followers": raw_followers},
                soft_scores=scores,
            )
            decided = decide(
                feat.get("sniper_share"), feat.get("followers"), feat.get("status"),
            )
            ticker = row.get("ticker") or d.get("ticker")
            payload = {
                "tid": tid,
                "ticker": ticker,
                "chain": row.get("chain") or d.get("chain"),
                "ts": ts,
                "judge_score": row.get("judge_score"),
                "sniper_share": feat.get("sniper_share"),
                "sniper_wallets": feat.get("sniper_wallets"),
                "bundled": feat.get("bundled"),
                "followers": decided["followers"],
                "would_skip": decided["would_skip"],
                "reasons": decided["reasons"],
                "became_pick": _became_pick(order, ticker, tid),
                "status": decided["status"] or feat.get("status") or STATUS_OK,
            }
            try:
                _insert(payload)
                written += 1
            except Exception as e:
                log.warning("rug_veto insert: %s", safe_err(e))
        return written
    except Exception as e:
        log.warning("rug_veto record: %s", safe_err(e))
        return written


def all_rows() -> list[dict]:
    try:
        rows = []
        for r in _connect().execute(
            "SELECT * FROM rug_veto_shadow ORDER BY ts DESC"
        ).fetchall():
            d = dict(r)
            raw = d.get("reasons")
            if isinstance(raw, str):
                try:
                    d["reasons"] = json.loads(raw)
                except Exception:
                    d["reasons"] = [raw] if raw else []
            d["would_skip"] = bool(d.get("would_skip"))
            d["became_pick"] = bool(d.get("became_pick"))
            rows.append(d)
        return rows
    except Exception as e:
        log.warning("rug_veto rows: %s", safe_err(e))
        return []


def counts() -> dict:
    rows = all_rows()
    out = {
        "n": len(rows),
        "would_skip": 0,
        "would_keep": 0,
        "picks": 0,
        "unavailable": 0,
        "followers_unknown": 0,
    }
    for r in rows:
        if r.get("would_skip"):
            out["would_skip"] += 1
        else:
            out["would_keep"] += 1
        if r.get("became_pick"):
            out["picks"] += 1
        if r.get("status") == STATUS_UNAVAILABLE or "unavailable" in (r.get("reasons") or []):
            out["unavailable"] += 1
        if "followers_unknown" in (r.get("reasons") or []):
            out["followers_unknown"] += 1
    return out


def replay_row(src: dict) -> dict:
    """Apply decide() to a desk_rugrisk or fixture row."""
    src = _merge_real_fields(src if isinstance(src, dict) else {})
    blob = _loads(src.get("features_json"))
    raw = _followers(src)
    if raw is None:
        raw = _num(blob.get("followers"))
    bag = dict(src)
    if raw is not None:
        bag.setdefault("followers", raw)
        bag.setdefault("x_followers", raw)
    followers = _project_followers(
        bag, soft_scores=_soft_scores_of(src) or blob.get("soft_scores"),
    )
    decided = decide(src.get("sniper_share"), followers, src.get("status"))
    return {
        "ticker": src.get("ticker"),
        "tid": src.get("tid"),
        "chain": src.get("chain") or blob.get("chain") or "solana",
        "judge_score": _num(src.get("judge_score")),
        "sniper_share": decided["sniper_share"],
        "bundled": src.get("bundled"),
        "sniper_wallets": src.get("sniper_wallets"),
        "followers": decided["followers"],
        "would_skip": decided["would_skip"],
        "reasons": decided["reasons"],
        "became_pick": bool(src.get("became_pick") or src.get("stage") == "pick"
                            or src.get("verdict") == "pick"),
        "status": decided["status"],
        "source": src.get("source") or "desk_rugrisk",
    }


def replay(include_historical: bool = True) -> dict:
    """Replay existing desk_rugrisk rows plus the five historical picks."""
    entries = []
    try:
        import desk_rugrisk
        for row in desk_rugrisk.all_rows():
            rec = replay_row({**row, "source": "desk_rugrisk"})
            entries.append(rec)
    except Exception as e:
        log.warning("rug_veto replay db: %s", safe_err(e))
    seen = {(e.get("ticker") or "").upper() for e in entries if e.get("ticker")}
    historical = []
    if include_historical:
        for src in HISTORICAL_PICKS:
            rec = replay_row({**src, "source": "historical"})
            historical.append(rec)
            key = (src.get("ticker") or "").upper()
            if key not in seen:
                entries.append(rec)
                seen.add(key)
    return {
        "paper": True,
        "gates_picks": False,
        "n": len(entries),
        "historical": historical,
        "entries": entries,
    }


def ops_payload() -> dict:
    try:
        return {
            "paper": True,
            "simulated": True,
            "live_orders": False,
            "gates_picks": False,
            "caveat": (
                "Rug veto is log-only. would_skip is counterfactual and does "
                "not gate picks, trades, or the judge."
            ),
            "label": "shadow, does not gate picks",
            "counts": counts(),
            "replay": replay(),
            "entries": all_rows()[:80],
        }
    except Exception as e:
        log.warning("rug_veto ops: %s", safe_err(e))
        return {
            "paper": True, "simulated": True, "live_orders": False,
            "gates_picks": False, "error": "unavailable",
            "counts": counts(), "entries": [],
        }


def main() -> None:
    """Print replay of desk_rugrisk + historical picks (no RPC)."""
    payload = replay()
    print("rug_veto_shadow replay (paper, no gating)")
    for rec in payload.get("historical") or []:
        print(
            f"  {rec.get('ticker')}: would_skip={rec.get('would_skip')} "
            f"reasons={rec.get('reasons')} sniper={rec.get('sniper_share')} "
            f"followers={rec.get('followers')}"
        )
    print(f"rows (db+historical)={payload.get('n')}")


if __name__ == "__main__":
    main()
