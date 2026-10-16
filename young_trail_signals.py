"""Paper RugCheck / DexScreener boosts / Birdeye features on young_trail entries.

Analysis only. Copies the same tids as the young_trail ledger. Skip rules are
counterfactual: they never gate entries, picks, or the judge. Fail open.
Exceptions through safe_err. Never logs API keys.

RugCheck.xyz (Solana, free summary endpoint, optional RUGCHECK_API_KEY, 3 req/s,
~30 min cache): score, risks, LP locked/burned, mint/freeze authority, top-holder
%, insider/creator flags.

DexScreener boosts/ads (free): latest+top lists once per cycle, local lookup;
orders per entry within ~60 req/min.

Birdeye (optional): only if BIRDEYE_API_KEY is set; skip silently otherwise.
Holder count, 30m/1h buy/sell/volume/unique wallets, price change. Cap per
cycle; back off on 429.

Does NOT change young_trail, fast, variants, helius, rugrisk, judging, or live
orders. Runs after young_trail_rugrisk on the sampler. Chain-check only queues (tid, chain, mint) in memory; the research sampler
thread does the fetches. Ledger rows are young_trail tids.
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

log = logging.getLogger("young_trail_signals")

TICKET_USD = 50.0
LOCK_TIMEOUT_SEC = 0.2
HTTP_TIMEOUT_SEC = 8.0
DEFAULT_UNTIL_ISO = "2026-10-21T03:59:59Z"
SOL_NET = 1399811149

RUGCHECK_SUMMARY = "https://api.rugcheck.xyz/v1/tokens/{mint}/report/summary"
DEX_BOOSTS_LATEST = "https://api.dexscreener.com/token-boosts/latest/v1"
DEX_BOOSTS_TOP = "https://api.dexscreener.com/token-boosts/top/v1"
DEX_ORDERS = "https://api.dexscreener.com/orders/v1/{chainId}/{tokenAddress}"
BIRDEYE_OVERVIEW = "https://public-api.birdeye.so/defi/token_overview"

CACHE_TTL_SEC = 30 * 60
BOOST_TTL_SEC = 60.0
RUGCHECK_MIN_INTERVAL = 1.0 / 3.0
ORDERS_MIN_INTERVAL = 1.0
DEFAULT_RUGCHECK_CAP = 12
DEFAULT_ORDERS_CAP = 8
DEFAULT_BIRDEYE_CAP = 6
DEFAULT_SCORE_CUT = 80.0
DEFAULT_LP_CUT = 50.0
DEFAULT_MOMENTUM_PX = 50.0
LP_LOCKED_CUT = 50.0

DEX_CHAIN = {
    1399811149: "solana", 56: "bsc", 8453: "base",
    4663: "robinhood", 143: "monad", 1: "ethereum",
}
UA = {
    "User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) desk/young_trail_signals",
    "Accept": "application/json",
}
PAID_TYPES = {
    "tokenProfile", "tokenAd", "communityTakeover", "trendingBarAd",
    "ad", "boost", "tokenBoost",
}
PAID_STATUS = {"approved", "processing", "pending", "active", "paid"}
DANGER_LEVELS = {"danger", "critical"}
NONE_AUTHORITIES = {
    "", "none", "null", "revoked", "disabled",
    "11111111111111111111111111111111",
}

STATUS_OK = "ok"
STATUS_NA = "n/a"
STATUS_PENDING = "pending"
STATUS_ERROR = "error"
STATUS_UNAVAILABLE = "unavailable"
STATUS_BUDGET = "budget"

RULE_DANGER = "danger_risk"
RULE_LP = "lp_unlocked"
RULE_AUTH = "authority_open"
RULE_SCORE = "score_high"
RULE_PROMO = "paid_promo"
RULE_MOMENTUM = "momentum_spent"

SKIP_RULES = (
    {"id": RULE_DANGER, "label": "danger-level risk", "field": "skip_danger"},
    {"id": RULE_LP, "label": "LP locked < 50%", "field": "skip_lp",
     "threshold": DEFAULT_LP_CUT},
    {"id": RULE_AUTH, "label": "mint or freeze authority open",
     "field": "skip_authority"},
    {"id": RULE_SCORE, "label": "RugCheck score above cut", "field": "skip_score",
     "threshold": DEFAULT_SCORE_CUT},
    {"id": RULE_PROMO, "label": "paid promotion present", "field": "skip_promo"},
    {"id": RULE_MOMENTUM, "label": "momentum already spent",
     "field": "skip_momentum", "threshold": DEFAULT_MOMENTUM_PX},
)

_tls = threading.local()
_schema_lock = threading.Lock()
_time_fn = time.time
_http_fn = None
_sleep_fn = None
_cycle_used = {"rugcheck": 0, "orders": 0, "birdeye": 0, "boosts": 0}
_last_call = {"rugcheck": 0.0, "orders": 0.0, "birdeye": 0.0, "boosts": 0.0}
_backoff_until = {"rugcheck": 0.0, "orders": 0.0, "birdeye": 0.0, "boosts": 0.0}
_backoff_exp = {"rugcheck": 0, "orders": 0, "birdeye": 0, "boosts": 0}
_boosts: dict[tuple[str, str], dict] = {}
_boosts_ts = 0.0
_boosts_ok = False
_cycle_started = 0.0
_sleeps: list[float] = []
_pending_lock = threading.Lock()
_pending_candidates: dict[str, dict] = {}


def set_time_fn(fn) -> None:
    global _time_fn
    _time_fn = time.time if fn is None else fn


def set_http_fn(fn) -> None:
    """Inject http(url, headers=None, timeout=None) for tests. None restores."""
    global _http_fn
    _http_fn = fn


def set_sleep_fn(fn) -> None:
    global _sleep_fn
    _sleep_fn = fn


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
    global _cycle_used, _last_call, _backoff_until, _backoff_exp
    global _boosts, _boosts_ts, _boosts_ok, _cycle_started, _sleeps
    global _pending_candidates
    _close_local()
    _cycle_used = {"rugcheck": 0, "orders": 0, "birdeye": 0, "boosts": 0}
    _last_call = {"rugcheck": 0.0, "orders": 0.0, "birdeye": 0.0, "boosts": 0.0}
    _backoff_until = {"rugcheck": 0.0, "orders": 0.0, "birdeye": 0.0, "boosts": 0.0}
    _backoff_exp = {"rugcheck": 0, "orders": 0, "birdeye": 0, "boosts": 0}
    _boosts = {}
    _boosts_ts = 0.0
    _boosts_ok = False
    _cycle_started = 0.0
    _sleeps = []
    with _pending_lock:
        _pending_candidates = {}


def _schema_step(db: sqlite3.Connection, name: str, fn) -> None:
    try:
        fn()
        db.commit()
    except Exception as e:
        log.warning("young_trail_signals schema %s: %s", name, safe_err(e))
        try:
            db.rollback()
        except Exception:
            pass


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, name: str, decl: str) -> None:
    if name in _table_columns(conn, table):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def init_schema(db: sqlite3.Connection | None = None) -> None:
    """Idempotent. Each step commits on its own (live-DB safe)."""
    conn = db if db is not None else _connect()
    _schema_step(conn, "young_trail_signals", lambda: conn.execute("""
    CREATE TABLE IF NOT EXISTS young_trail_signals (
      tid TEXT PRIMARY KEY,
      ticker TEXT,
      chain TEXT,
      entry_ts REAL NOT NULL,
      entry_price REAL,
      status TEXT NOT NULL DEFAULT 'pending',
      rugcheck_score REAL,
      rugcheck_score_norm REAL,
      lp_locked_pct REAL,
      mint_authority INTEGER,
      freeze_authority INTEGER,
      top_holder_pct REAL,
      insider_flag INTEGER,
      creator_flag INTEGER,
      danger_risk INTEGER,
      boost_amount REAL,
      boost_total REAL,
      paid_promo INTEGER,
      holder_count INTEGER,
      buy_30m INTEGER,
      sell_30m INTEGER,
      vol_buy_30m REAL,
      vol_sell_30m REAL,
      unique_wallets_30m INTEGER,
      buy_1h INTEGER,
      sell_1h INTEGER,
      vol_buy_1h REAL,
      vol_sell_1h REAL,
      unique_wallets_1h INTEGER,
      price_change_30m REAL,
      price_change_1h REAL,
      skip_danger INTEGER,
      skip_lp INTEGER,
      skip_authority INTEGER,
      skip_score INTEGER,
      skip_promo INTEGER,
      skip_momentum INTEGER,
      features_json TEXT,
      error TEXT,
      source TEXT,
      computed_ts REAL
    )"""))
    _schema_step(conn, "idx_yts_status", lambda: conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_yts_status ON young_trail_signals(status)"
    ))
    _schema_step(conn, "idx_yts_entry", lambda: conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_yts_entry ON young_trail_signals(entry_ts)"
    ))
    _schema_step(conn, "young_trail_signals_meta", lambda: conn.execute("""
    CREATE TABLE IF NOT EXISTS young_trail_signals_meta (
      key TEXT PRIMARY KEY,
      value TEXT
    )"""))
    _schema_step(conn, "young_trail_signals_cache", lambda: conn.execute("""
    CREATE TABLE IF NOT EXISTS young_trail_signals_cache (
      cache_key TEXT PRIMARY KEY,
      fetched_ts REAL NOT NULL,
      payload_json TEXT
    )"""))
    _schema_step(conn, "yts_lp_burned_pct", lambda: _add_column(
        conn, "young_trail_signals", "lp_burned_pct", "REAL"
    ))
    _schema_step(conn, "yts_birdeye_skipped", lambda: _add_column(
        conn, "young_trail_signals", "birdeye_skipped", "INTEGER"
    ))
    _schema_step(conn, "yts_top_holder_flag", lambda: _add_column(
        conn, "young_trail_signals", "top_holder_flag", "INTEGER"
    ))


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
    raw = os.environ.get("YOUNG_TRAIL_SIGNALS_ENABLED")
    if raw is None or raw == "":
        return True
    return raw.strip().lower() not in ("0", "false", "no", "off")


def birdeye_enabled() -> bool:
    raw = os.environ.get("BIRDEYE_API_KEY")
    return bool(raw and str(raw).strip())


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
            "SELECT value FROM young_trail_signals_meta WHERE key = ?", (key,),
        ).fetchone()
        return None if row is None else row["value"]
    except Exception as e:
        log.warning("young_trail_signals meta read: %s", safe_err(e))
        return None


def _meta_set(key: str, value: str) -> None:
    try:
        db = _connect()
        db.execute(
            """INSERT INTO young_trail_signals_meta (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )
        db.commit()
    except Exception as e:
        log.warning("young_trail_signals meta write: %s", safe_err(e))


def until_ts(now: float | None = None) -> float:
    raw = os.environ.get("YOUNG_TRAIL_SIGNALS_UNTIL")
    parsed = _parse_until(raw) if raw else None
    if parsed is not None:
        return parsed
    try:
        import young_trail
        ts = float(young_trail.until_ts(now))
    except Exception as e:
        log.warning("young_trail_signals until from young_trail: %s", safe_err(e))
        ts = _parse_until(DEFAULT_UNTIL_ISO) or 0.0
    stored = _meta_get("until_ts")
    if stored != str(ts):
        _meta_set("until_ts", str(ts))
    return ts


def accepting(now: float | None = None) -> bool:
    ts = current_time() if now is None else float(now)
    return enabled() and ts < until_ts(ts)


def _env_int(name: str, default: int) -> int:
    raw = os.environ.get(name)
    try:
        n = int(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        n = default
    return max(0, n)


def _env_float(name: str, default: float) -> float:
    raw = os.environ.get(name)
    try:
        n = float(raw) if raw not in (None, "") else default
    except (TypeError, ValueError):
        n = default
    return n


def score_cut() -> float:
    return _env_float("YOUNG_TRAIL_SIGNALS_SCORE_CUT", DEFAULT_SCORE_CUT)


def lp_cut() -> float:
    return _env_float("YOUNG_TRAIL_SIGNALS_LP_CUT", DEFAULT_LP_CUT)


def momentum_px_cut() -> float:
    return _env_float("YOUNG_TRAIL_SIGNALS_MOMENTUM_PX", DEFAULT_MOMENTUM_PX)


def rugcheck_cap() -> int:
    return _env_int("YOUNG_TRAIL_SIGNALS_RUGCHECK_CAP", DEFAULT_RUGCHECK_CAP)


def orders_cap() -> int:
    return _env_int("YOUNG_TRAIL_SIGNALS_ORDERS_CAP", DEFAULT_ORDERS_CAP)


def birdeye_cap() -> int:
    return _env_int("YOUNG_TRAIL_SIGNALS_BIRDEYE_CAP", DEFAULT_BIRDEYE_CAP)


def begin_cycle(now: float | None = None) -> None:
    """Reset per-tick caps if a new ~60s window has started. Fetch boosts once."""
    global _cycle_used, _cycle_started
    ts = current_time() if now is None else float(now)
    if _cycle_started and (ts - _cycle_started) < 50.0:
        return
    _cycle_started = ts
    _cycle_used = {"rugcheck": 0, "orders": 0, "birdeye": 0, "boosts": 0}
    try:
        ensure_boosts(ts)
    except Exception as e:
        log.warning("young_trail_signals boosts: %s", safe_err(e))


def cycle_used() -> dict:
    return dict(_cycle_used)


def _kind_cap(kind: str) -> int:
    if kind == "rugcheck":
        return rugcheck_cap()
    if kind == "orders":
        return orders_cap()
    if kind == "birdeye":
        return birdeye_cap()
    if kind == "boosts":
        return 2
    return 0


def _kind_interval(kind: str) -> float:
    if kind == "rugcheck":
        return RUGCHECK_MIN_INTERVAL
    if kind == "orders":
        return ORDERS_MIN_INTERVAL
    return 0.0


def _can_call(kind: str, now: float | None = None) -> bool:
    ts = current_time() if now is None else float(now)
    if ts < float(_backoff_until.get(kind) or 0.0):
        return False
    cap = _kind_cap(kind)
    if cap <= 0:
        return False
    return int(_cycle_used.get(kind) or 0) < cap


def _pace(kind: str, now: float | None = None) -> bool:
    """Respect min interval (sleep) and cycle cap. False = skip this call."""
    ts = current_time() if now is None else float(now)
    if not _can_call(kind, ts):
        return False
    interval = _kind_interval(kind)
    last = float(_last_call.get(kind) or 0.0)
    wait = (last + interval) - ts if interval > 0 and last > 0 else 0.0
    if wait > 0:
        sleeper = _sleep_fn if _sleep_fn is not None else time.sleep
        try:
            sleeper(wait)
        except Exception as e:
            log.warning("young_trail_signals pace: %s", safe_err(e))
        _sleeps.append(wait)
    return True


def _charge(kind: str, now: float | None = None) -> None:
    ts = current_time() if now is None else float(now)
    _cycle_used[kind] = int(_cycle_used.get(kind) or 0) + 1
    _last_call[kind] = ts


def _note_429(kind: str, retry_after, now: float | None = None) -> float:
    ts = current_time() if now is None else float(now)
    exp = int(_backoff_exp.get(kind) or 0)
    delay = 1.0 * (2 ** min(exp, 5))
    try:
        if retry_after not in (None, ""):
            delay = max(delay, float(retry_after))
    except (TypeError, ValueError):
        pass
    delay = min(30.0, max(0.5, delay))
    _backoff_exp[kind] = exp + 1
    _backoff_until[kind] = ts + delay
    return delay


def _clear_429(kind: str) -> None:
    _backoff_exp[kind] = 0
    _backoff_until[kind] = 0.0


def _num(value, default=None):
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


def _row_dict(row: sqlite3.Row) -> dict:
    return {k: row[k] for k in row.keys()}


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
        _, parsed_net = parse_tid(tid)
        if parsed_net == SOL_NET:
            return True
    return False


def dex_chain_id(chain=None, net=None, tid: str | None = None) -> str | None:
    c = str(chain or "").strip().lower()
    if c:
        return c
    n = net
    if n is None and tid:
        _, n = parse_tid(tid)
    if n is not None:
        return DEX_CHAIN.get(int(n))
    return None


# --- pure parsers ---------------------------------------------------------

def _authority_present(val, *, present: bool) -> bool | None:
    """True if mint/freeze authority is still set. False = revoked. None = missing."""
    if not present:
        return None
    if val is None or val is False:
        return False
    if val is True:
        return True
    s = str(val).strip()
    if not s or s.lower() in NONE_AUTHORITIES:
        return False
    return True


def _risk_items(raw) -> list[dict]:
    out = []
    for item in raw or []:
        if not isinstance(item, dict):
            continue
        name = item.get("name") or item.get("levelName") or ""
        level = str(item.get("level") or item.get("severity") or "").lower()
        out.append({"name": str(name), "level": level, "score": _num(item.get("score"))})
    return out


def _risk_name_hit(risks: list[dict], *needles: str) -> bool:
    """True if any risk name contains one of the needles (case-insensitive)."""
    wanted = tuple(n.lower() for n in needles)
    for r in risks or []:
        nm = (r.get("name") or "").lower()
        if any(n in nm for n in wanted):
            return True
    return False


def _lp_locked_pct(data: dict) -> tuple[float | None, float | None]:
    locked = _num(data.get("lpLockedPct") if "lpLockedPct" in data else data.get("lp_locked_pct"))
    burned = _num(data.get("lpBurnedPct") if "lpBurnedPct" in data else data.get("lp_burned_pct"))
    markets = data.get("markets") or data.get("tokenMarkets") or []
    for m in markets:
        if not isinstance(m, dict):
            continue
        lp = m.get("lp") if isinstance(m.get("lp"), dict) else m
        lv = _num(lp.get("lpLockedPct") if "lpLockedPct" in lp else lp.get("lockedPct"))
        bv = _num(lp.get("lpBurnedPct") if "lpBurnedPct" in lp else lp.get("burnedPct"))
        if lv is not None:
            locked = lv if locked is None else min(locked, lv)
        if bv is not None:
            burned = bv if burned is None else min(burned, bv)
    return locked, burned


def _top_holder_pct(data: dict) -> float | None:
    top = data.get("topHolders") or data.get("top_holders") or []
    if isinstance(top, list) and top:
        first = top[0] if isinstance(top[0], dict) else None
        if first:
            pct = _num(first.get("pct") if "pct" in first else first.get("percentage"))
            if pct is not None:
                return float(pct) if pct > 1 else float(pct) * 100.0
    pct = _num(data.get("topHolderPct") or data.get("top_holder_pct"))
    if pct is None:
        return None
    return float(pct) if pct > 1 else float(pct) * 100.0


def parse_rugcheck(data) -> dict:
    """Accept summary or full report. Missing fields stay None (fail open).

    The free summary endpoint has no mintAuthority/freezeAuthority/topHolders.
    Live shape: {tokenProgram, tokenType, risks[{name,value,description,score,level}],
    score, score_normalised, lpLockedPct}. Mint/freeze and top-holder flags are
    derived from risk names when the risks key is present.
    """
    empty = {
        "rugcheck_score": None, "rugcheck_score_norm": None,
        "lp_locked_pct": None, "lp_burned_pct": None,
        "mint_authority": None, "freeze_authority": None,
        "top_holder_pct": None, "top_holder_flag": None,
        "insider_flag": None, "creator_flag": None,
        "danger_risk": None, "risks": [],
    }
    if not isinstance(data, dict) or not data:
        return empty
    meta = data.get("tokenMeta") if isinstance(data.get("tokenMeta"), dict) else {}
    has_risks_key = "risks" in data or "risk" in data
    risks = _risk_items(data.get("risks") if "risks" in data else data.get("risk"))
    danger = None
    if has_risks_key:
        danger = any(r.get("level") in DANGER_LEVELS for r in risks)
    mint_present = "mintAuthority" in data or "mintAuthority" in meta
    freeze_present = "freezeAuthority" in data or "freezeAuthority" in meta
    mint_auth = data.get("mintAuthority")
    if mint_auth is None:
        mint_auth = meta.get("mintAuthority")
    freeze_auth = data.get("freezeAuthority")
    if freeze_auth is None:
        freeze_auth = meta.get("freezeAuthority")
    if mint_present:
        mint_p = _authority_present(mint_auth, present=True)
    elif has_risks_key:
        mint_p = _risk_name_hit(risks, "mint authority")
    else:
        mint_p = None
    if freeze_present:
        freeze_p = _authority_present(freeze_auth, present=True)
    elif has_risks_key:
        freeze_p = _risk_name_hit(risks, "freeze authority")
    else:
        freeze_p = None
    locked, burned = _lp_locked_pct(data)
    insiders = data.get("insiderNetworks") or data.get("graphInsidersDetected")
    insider = None
    if "insiderNetworks" in data or "graphInsidersDetected" in data:
        n = _num(data.get("graphInsidersDetected"), 0.0) or 0.0
        insider = bool(n > 0 or (isinstance(insiders, list) and len(insiders) > 0))
    if insider is None:
        for r in risks:
            if "insider" in (r.get("name") or "").lower():
                insider = True
                break
    creator = None
    if "creatorTokens" in data or data.get("creator") or data.get("detectedAt"):
        creator = bool(data.get("creator") or (data.get("creatorTokens") or []))
    for r in risks:
        nm = (r.get("name") or "").lower()
        if "creator" in nm or "dev holding" in nm:
            creator = True
            break
    if has_risks_key:
        top_flag = _risk_name_hit(
            risks, "top 10 holders high ownership", "single holder ownership",
        )
    else:
        top_flag = None
    return {
        "rugcheck_score": _num(data.get("score")),
        "rugcheck_score_norm": _num(
            data.get("score_normalised") if "score_normalised" in data
            else data.get("scoreNormalized")
        ),
        "lp_locked_pct": locked,
        "lp_burned_pct": burned,
        "mint_authority": None if mint_p is None else int(bool(mint_p)),
        "freeze_authority": None if freeze_p is None else int(bool(freeze_p)),
        "top_holder_pct": _top_holder_pct(data),
        "top_holder_flag": None if top_flag is None else int(bool(top_flag)),
        "insider_flag": None if insider is None else int(bool(insider)),
        "creator_flag": None if creator is None else int(bool(creator)),
        "danger_risk": None if danger is None else int(bool(danger)),
        "risks": risks,
    }


def parse_boosts(rows) -> dict[tuple[str, str], dict]:
    """Index DexScreener boost list by (chainId, tokenAddress)."""
    if isinstance(rows, dict):
        rows = rows.get("boosts") or rows.get("data") or []
    out: dict[tuple[str, str], dict] = {}
    for item in rows or []:
        if not isinstance(item, dict):
            continue
        chain = str(item.get("chainId") or item.get("chain") or "").lower()
        addr = str(item.get("tokenAddress") or item.get("address") or "").lower()
        if not chain or not addr:
            continue
        amt = _num(item.get("amount"), 0.0) or 0.0
        tot = _num(item.get("totalAmount") if "totalAmount" in item else item.get("total"), 0.0) or 0.0
        prev = out.get((chain, addr))
        if prev:
            amt = max(amt, prev.get("amount") or 0.0)
            tot = max(tot, prev.get("total") or 0.0)
        out[(chain, addr)] = {"amount": amt, "total": tot}
    return out


def parse_orders(rows) -> dict:
    if isinstance(rows, dict):
        rows = rows.get("orders") or rows.get("data") or []
    paid = False
    out = []
    fetched = isinstance(rows, list)
    for o in rows or []:
        if not isinstance(o, dict):
            continue
        typ = str(o.get("type") or "")
        status = str(o.get("status") or "").lower()
        ts = o.get("paymentTimestamp") or o.get("timestamp") or o.get("payment_ts")
        rec = {"type": typ, "status": status, "payment_ts": ts}
        out.append(rec)
        if status in {"rejected", "cancelled", "canceled", "expired"}:
            continue
        if status in PAID_STATUS or (typ in PAID_TYPES and status):
            paid = True
        elif typ in PAID_TYPES:
            paid = True
    return {"orders": out, "paid_promo": paid if fetched else None, "orders_fetched": fetched}


def parse_birdeye(data) -> dict:
    empty = {
        "holder_count": None,
        "buy_30m": None, "sell_30m": None,
        "vol_buy_30m": None, "vol_sell_30m": None,
        "unique_wallets_30m": None,
        "buy_1h": None, "sell_1h": None,
        "vol_buy_1h": None, "vol_sell_1h": None,
        "unique_wallets_1h": None,
        "price_change_30m": None, "price_change_1h": None,
        "birdeye_skipped": 0,
    }
    if not isinstance(data, dict) or not data:
        return empty
    body = data.get("data") if isinstance(data.get("data"), dict) else data
    if not isinstance(body, dict):
        return empty

    def i(*keys):
        for k in keys:
            if k in body and body[k] is not None:
                n = _num(body[k])
                return None if n is None else int(n)
        return None

    def f(*keys):
        for k in keys:
            if k in body and body[k] is not None:
                return _num(body[k])
        return None

    return {
        "holder_count": i("holder", "holderCount", "holders"),
        "buy_30m": i("buy30m", "buy_30m", "buys30m"),
        "sell_30m": i("sell30m", "sell_30m", "sells30m"),
        "vol_buy_30m": f("vBuy30mUSD", "vBuy30m", "volumeBuy30mUSD"),
        "vol_sell_30m": f("vSell30mUSD", "vSell30m", "volumeSell30mUSD"),
        "unique_wallets_30m": i("uniqueWallet30m", "unique_wallet_30m"),
        "buy_1h": i("buy1h", "buy_1h", "buys1h"),
        "sell_1h": i("sell1h", "sell_1h", "sells1h"),
        "vol_buy_1h": f("vBuy1hUSD", "vBuy1h", "volumeBuy1hUSD"),
        "vol_sell_1h": f("vSell1hUSD", "vSell1h", "volumeSell1hUSD"),
        "unique_wallets_1h": i("uniqueWallet1h", "unique_wallet_1h"),
        "price_change_30m": f("priceChange30mPercent", "priceChange30m", "price_change_30m"),
        "price_change_1h": f("priceChange1hPercent", "priceChange1h", "price_change_1h"),
        "birdeye_skipped": 0,
    }


def risk_score_100(feat: dict) -> float | None:
    """RugCheck risk on a ~0–100 scale. Higher = more risk. None if unknown."""
    n = _num(feat.get("rugcheck_score_norm"))
    if n is not None:
        return float(n) if n > 10 else float(n) * 10.0
    s = _num(feat.get("rugcheck_score"))
    if s is None:
        return None
    if s <= 10:
        return float(s) * 10.0
    if s <= 100:
        return float(s)
    return float(s) / 100.0


def lp_secure_pct(feat: dict) -> float | None:
    locked = _num(feat.get("lp_locked_pct"))
    burned = _num(feat.get("lp_burned_pct"))
    if locked is None and burned is None:
        return None
    return max(locked or 0.0, burned or 0.0)


def skip_flags(features: dict) -> dict:
    """Pure skip-rule evaluation. None = unknown (fail open: do not skip)."""
    status = features.get("status")
    if status in (STATUS_NA, STATUS_PENDING, STATUS_ERROR, STATUS_UNAVAILABLE,
                  STATUS_BUDGET):
        return {
            "skip_danger": None, "skip_lp": None, "skip_authority": None,
            "skip_score": None, "skip_promo": None, "skip_momentum": None,
        }
    danger = features.get("danger_risk")
    skip_d = None if danger is None else bool(int(danger))
    secure = lp_secure_pct(features)
    skip_lp = None if secure is None else bool(secure < lp_cut())
    mint_a = features.get("mint_authority")
    freeze_a = features.get("freeze_authority")
    skip_a = None
    if mint_a is not None or freeze_a is not None:
        skip_a = bool(int(mint_a or 0) or int(freeze_a or 0))
    score = risk_score_100(features)
    skip_s = None if score is None else bool(score > score_cut())
    paid = features.get("paid_promo")
    boost_amt = _num(features.get("boost_amount"))
    skip_p = None
    paid_true = paid is True or paid == 1
    paid_false = paid is False or paid == 0
    if paid_true or (boost_amt is not None and boost_amt > 0):
        skip_p = True
    elif paid_false:
        skip_p = False
    px = _num(features.get("price_change_1h"))
    buys = features.get("buy_30m")
    sells = features.get("sell_30m")
    skip_m = None
    if features.get("birdeye_skipped"):
        skip_m = None
    elif px is None or buys is None or sells is None:
        skip_m = None
    else:
        skip_m = bool(px >= momentum_px_cut() and int(buys) < int(sells))
    return {
        "skip_danger": skip_d,
        "skip_lp": skip_lp,
        "skip_authority": skip_a,
        "skip_score": skip_s,
        "skip_promo": skip_p,
        "skip_momentum": skip_m,
    }


def would_skip(row: dict, rule_id: str) -> bool | None:
    if not row or row.get("status") in (
        STATUS_NA, STATUS_UNAVAILABLE, STATUS_ERROR, STATUS_BUDGET, STATUS_PENDING,
    ):
        return None
    flags = skip_flags(row)
    by_id = {
        RULE_DANGER: flags["skip_danger"],
        RULE_LP: flags["skip_lp"],
        RULE_AUTH: flags["skip_authority"],
        RULE_SCORE: flags["skip_score"],
        RULE_PROMO: flags["skip_promo"],
        RULE_MOMENTUM: flags["skip_momentum"],
    }
    val = by_id.get(rule_id)
    if val is None:
        field = None
        for rule in SKIP_RULES:
            if rule["id"] == rule_id:
                field = rule["field"]
                break
        if field and row.get(field) is not None:
            return bool(row[field])
        return None
    return bool(val)


def counterfactual_for_rule(rule_id: str, rows: list[dict],
                            pnl_maps: dict) -> dict:
    """SKIPPED vs KEPT realized P&L. Unmarked / null pnl excluded. Pure."""
    skipped, kept, unknown = [], [], []
    for row in rows or []:
        decision = would_skip(row, rule_id)
        tid = row.get("tid")
        if not tid:
            continue
        if decision is True:
            skipped.append(tid)
        elif decision is False:
            kept.append(tid)
        else:
            unknown.append(tid)

    def side(tids: list[str]) -> dict:
        out = {"n": len(tids), "n_with_pnl": {}}
        for name, mapping in (pnl_maps or {}).items():
            pnls = []
            for tid in tids:
                v = mapping.get(tid)
                if v is None:
                    continue
                pnls.append(float(v))
            out[f"{name}_pnl"] = sum(pnls) if pnls else 0.0
            out["n_with_pnl"][name] = len(pnls)
        return out

    rule = next((r for r in SKIP_RULES if r["id"] == rule_id), {"id": rule_id})
    thresh = rule.get("threshold")
    if rule_id == RULE_SCORE:
        thresh = score_cut()
    elif rule_id == RULE_LP:
        thresh = lp_cut()
    elif rule_id == RULE_MOMENTUM:
        thresh = momentum_px_cut()
    return {
        "id": rule_id,
        "label": rule.get("label") or rule_id,
        "threshold": thresh,
        "skipped": side(skipped),
        "kept": side(kept),
        "unknown_n": len(unknown),
    }


# --- HTTP + cache ---------------------------------------------------------

def _cache_get(key: str, now: float, ttl: float = CACHE_TTL_SEC):
    try:
        row = _connect().execute(
            "SELECT fetched_ts, payload_json FROM young_trail_signals_cache "
            "WHERE cache_key = ?",
            (key,),
        ).fetchone()
        if row is None:
            return None
        fetched = float(row["fetched_ts"] or 0)
        if fetched <= 0 or (now - fetched) > ttl:
            return None
        raw = row["payload_json"]
        if not raw:
            return None
        return json.loads(raw)
    except Exception as e:
        log.warning("young_trail_signals cache read: %s", safe_err(e))
        return None


def _cache_put(key: str, payload, now: float) -> None:
    try:
        db = _connect()
        db.execute(
            """INSERT INTO young_trail_signals_cache (cache_key, fetched_ts, payload_json)
               VALUES (?, ?, ?)
               ON CONFLICT(cache_key) DO UPDATE SET
                 fetched_ts = excluded.fetched_ts,
                 payload_json = excluded.payload_json""",
            (key, float(now), json.dumps(payload)),
        )
        db.commit()
    except Exception as e:
        log.warning("young_trail_signals cache write: %s", safe_err(e))


class _Resp:
    def __init__(self, status_code, data, headers=None):
        self.status_code = int(status_code)
        self._data = data
        self.headers = headers or {}
        self.text = "" if data is None else (data if isinstance(data, str) else "")

    def json(self):
        if isinstance(self._data, (dict, list)):
            return self._data
        if isinstance(self._data, str) and self._data:
            return json.loads(self._data)
        return self._data


def _http_get(url: str, headers=None, timeout: float = HTTP_TIMEOUT_SEC, kind: str = ""):
    """GET JSON. Never logs keys or the full header set."""
    hdrs = dict(UA)
    if headers:
        hdrs.update(headers)
    if _http_fn is not None:
        rec = _http_fn(url, headers=hdrs, timeout=timeout)
        if rec is None:
            return None
        if isinstance(rec, _Resp):
            return rec
        if isinstance(rec, tuple) and len(rec) >= 2:
            return _Resp(rec[0], rec[1], rec[2] if len(rec) > 2 else None)
        if hasattr(rec, "status_code"):
            return rec
        return _Resp(200, rec)
    import requests
    try:
        return requests.get(url, headers=hdrs, timeout=max(0.5, float(timeout)))
    except Exception as e:
        log.warning("young_trail_signals http %s: %s", kind or "get", safe_err(e))
        raise


def _retry_after(resp) -> float | None:
    try:
        headers = getattr(resp, "headers", None) or {}
        raw = headers.get("Retry-After") or headers.get("retry-after")
        if raw in (None, ""):
            return None
        return float(raw)
    except (TypeError, ValueError):
        return None


def _json_body(resp):
    try:
        return resp.json()
    except Exception:
        return None


def _rugcheck_headers() -> dict:
    key = (os.environ.get("RUGCHECK_API_KEY") or "").strip()
    if not key:
        return {}
    return {"X-API-KEY": key}


def _birdeye_headers(chain: str) -> dict:
    key = (os.environ.get("BIRDEYE_API_KEY") or "").strip()
    return {"X-API-KEY": key, "x-chain": chain or "solana"}


def fetch_rugcheck(mint: str, now: float | None = None) -> dict | None:
    ts = current_time() if now is None else float(now)
    if not mint:
        return None
    key = f"rugcheck:{mint}"
    cached = _cache_get(key, ts)
    if cached is not None:
        return parse_rugcheck(cached)
    if not _pace("rugcheck", ts):
        return None
    url = RUGCHECK_SUMMARY.format(mint=mint)
    try:
        resp = _http_get(url, headers=_rugcheck_headers(), kind="rugcheck")
        _charge("rugcheck", ts)
        if resp is None:
            return None
        code = int(getattr(resp, "status_code", 0) or 0)
        if code == 429:
            delay = _note_429("rugcheck", _retry_after(resp), ts)
            log.warning("young_trail_signals rugcheck 429 backoff=%.1fs", delay)
            return None
        if code >= 400:
            log.warning("young_trail_signals rugcheck status=%s", code)
            return None
        _clear_429("rugcheck")
        body = _json_body(resp)
        if not isinstance(body, dict):
            return None
        _cache_put(key, body, ts)
        return parse_rugcheck(body)
    except Exception as e:
        log.warning("young_trail_signals rugcheck: %s", safe_err(e))
        return None


def ensure_boosts(now: float | None = None) -> dict:
    """Fetch latest+top once per cycle. Lookup is local after that."""
    global _boosts, _boosts_ts, _boosts_ok
    ts = current_time() if now is None else float(now)
    if _boosts_ok and (ts - _boosts_ts) < BOOST_TTL_SEC:
        return _boosts
    cached = _cache_get("boosts:cycle", ts, ttl=BOOST_TTL_SEC)
    if isinstance(cached, dict) and "items" in cached:
        parsed = {}
        for item in cached.get("items") or []:
            if isinstance(item, dict) and item.get("chain") and item.get("addr"):
                parsed[(item["chain"], item["addr"])] = {
                    "amount": item.get("amount") or 0.0,
                    "total": item.get("total") or 0.0,
                }
        _boosts = parsed
        _boosts_ts = ts
        _boosts_ok = True
        return _boosts
    merged: dict[tuple[str, str], dict] = {}
    ok = False
    for url in (DEX_BOOSTS_LATEST, DEX_BOOSTS_TOP):
        if not _pace("boosts", ts):
            break
        try:
            resp = _http_get(url, kind="boosts")
            _charge("boosts", ts)
            if resp is None:
                continue
            code = int(getattr(resp, "status_code", 0) or 0)
            if code == 429:
                _note_429("boosts", _retry_after(resp), ts)
                log.warning("young_trail_signals boosts 429")
                continue
            if code >= 400:
                log.warning("young_trail_signals boosts status=%s", code)
                continue
            body = _json_body(resp)
            chunk = parse_boosts(body)
            for k, v in chunk.items():
                prev = merged.get(k)
                if prev:
                    v = {
                        "amount": max(v.get("amount") or 0, prev.get("amount") or 0),
                        "total": max(v.get("total") or 0, prev.get("total") or 0),
                    }
                merged[k] = v
            ok = True
        except Exception as e:
            log.warning("young_trail_signals boosts: %s", safe_err(e))
    _boosts = merged
    _boosts_ts = ts
    _boosts_ok = ok
    if ok:
        items = [
            {"chain": c, "addr": a, "amount": v.get("amount"), "total": v.get("total")}
            for (c, a), v in merged.items()
        ]
        _cache_put("boosts:cycle", {"items": items}, ts)
    return _boosts


def lookup_boost(chain: str | None, mint: str | None) -> dict:
    if not chain or not mint:
        return {"amount": None, "total": None, "boosted": None}
    rec = _boosts.get((str(chain).lower(), str(mint).lower()))
    if rec is None:
        if not _boosts_ok:
            return {"amount": None, "total": None, "boosted": None}
        return {"amount": 0.0, "total": 0.0, "boosted": False}
    amt = _num(rec.get("amount"), 0.0) or 0.0
    tot = _num(rec.get("total"), 0.0) or 0.0
    return {"amount": amt, "total": tot, "boosted": amt > 0 or tot > 0}


def fetch_orders(chain: str, mint: str, now: float | None = None) -> dict | None:
    ts = current_time() if now is None else float(now)
    if not chain or not mint:
        return None
    key = f"orders:{chain.lower()}:{mint.lower()}"
    cached = _cache_get(key, ts)
    if cached is not None:
        return parse_orders(cached) if isinstance(cached, list) else (
            cached if isinstance(cached, dict) and "orders" in cached else parse_orders(cached)
        )
    if not _pace("orders", ts):
        return None
    url = DEX_ORDERS.format(chainId=chain, tokenAddress=mint)
    try:
        resp = _http_get(url, kind="orders")
        _charge("orders", ts)
        if resp is None:
            return None
        code = int(getattr(resp, "status_code", 0) or 0)
        if code == 429:
            delay = _note_429("orders", _retry_after(resp), ts)
            log.warning("young_trail_signals orders 429 backoff=%.1fs", delay)
            return None
        if code >= 400:
            log.warning("young_trail_signals orders status=%s", code)
            return None
        _clear_429("orders")
        body = _json_body(resp)
        _cache_put(key, body if body is not None else [], ts)
        return parse_orders(body if body is not None else [])
    except Exception as e:
        log.warning("young_trail_signals orders: %s", safe_err(e))
        return None


def fetch_birdeye(mint: str, chain: str = "solana",
                  now: float | None = None) -> dict | None:
    """None if no key (silent) or budget/backoff. Parsed dict otherwise."""
    if not birdeye_enabled():
        return None
    ts = current_time() if now is None else float(now)
    if not mint:
        return None
    key = f"birdeye:{mint}"
    cached = _cache_get(key, ts)
    if cached is not None:
        return parse_birdeye(cached)
    if not _pace("birdeye", ts):
        return None
    url = f"{BIRDEYE_OVERVIEW}?address={mint}"
    try:
        resp = _http_get(url, headers=_birdeye_headers(chain), kind="birdeye")
        _charge("birdeye", ts)
        if resp is None:
            return None
        code = int(getattr(resp, "status_code", 0) or 0)
        if code == 429:
            delay = _note_429("birdeye", _retry_after(resp), ts)
            log.warning("young_trail_signals birdeye 429 backoff=%.1fs", delay)
            return None
        if code >= 400:
            log.warning("young_trail_signals birdeye status=%s", code)
            return None
        _clear_429("birdeye")
        body = _json_body(resp)
        if not isinstance(body, dict):
            return None
        _cache_put(key, body, ts)
        return parse_birdeye(body)
    except Exception as e:
        log.warning("young_trail_signals birdeye: %s", safe_err(e))
        return None


def compute_features(mint: str, chain: str | None = None,
                     now: float | None = None) -> dict:
    """Fetch+parse all sources for one mint. Never raises. Never gates."""
    ts = current_time() if now is None else float(now)
    chain_id = (chain or "solana").lower()
    sol = chain_id == "solana"
    extra = {"risks": [], "orders": []}
    feat: dict = {
        "status": STATUS_OK,
        "rugcheck_score": None, "rugcheck_score_norm": None,
        "lp_locked_pct": None, "lp_burned_pct": None,
        "mint_authority": None, "freeze_authority": None,
        "top_holder_pct": None, "top_holder_flag": None,
        "insider_flag": None, "creator_flag": None,
        "danger_risk": None,
        "boost_amount": None, "boost_total": None, "paid_promo": None,
        "holder_count": None,
        "buy_30m": None, "sell_30m": None,
        "vol_buy_30m": None, "vol_sell_30m": None, "unique_wallets_30m": None,
        "buy_1h": None, "sell_1h": None,
        "vol_buy_1h": None, "vol_sell_1h": None, "unique_wallets_1h": None,
        "price_change_30m": None, "price_change_1h": None,
        "birdeye_skipped": 0 if birdeye_enabled() else 1,
        "error": None,
    }
    try:
        ensure_boosts(ts)
        boost = lookup_boost(chain_id, mint)
        feat["boost_amount"] = boost.get("amount")
        feat["boost_total"] = boost.get("total")
        if sol:
            rug = fetch_rugcheck(mint, now=ts)
            if rug:
                for k in (
                    "rugcheck_score", "rugcheck_score_norm", "lp_locked_pct",
                    "lp_burned_pct", "mint_authority", "freeze_authority",
                    "top_holder_pct", "top_holder_flag",
                    "insider_flag", "creator_flag", "danger_risk",
                ):
                    feat[k] = rug.get(k)
                extra["risks"] = rug.get("risks") or []
        orders = fetch_orders(chain_id, mint, now=ts)
        if orders:
            extra["orders"] = orders.get("orders") or []
            feat["paid_promo"] = (
                None if orders.get("paid_promo") is None
                else int(bool(orders.get("paid_promo")))
            )
        if boost.get("amount") and boost["amount"] > 0:
            feat["paid_promo"] = 1
        if sol and birdeye_enabled():
            bird = fetch_birdeye(mint, chain_id, now=ts)
            if bird:
                for k, v in bird.items():
                    if k == "birdeye_skipped":
                        continue
                    feat[k] = v
                feat["birdeye_skipped"] = 0
            else:
                feat["birdeye_skipped"] = 0
        elif not birdeye_enabled():
            feat["birdeye_skipped"] = 1
        flags = skip_flags(feat)
        feat.update(flags)
        extra["boosted"] = boost.get("boosted")
        feat["features_json"] = extra
        return feat
    except Exception as e:
        msg = safe_err(e)
        log.warning("young_trail_signals features: %s", msg)
        feat["status"] = STATUS_ERROR
        feat["error"] = msg[:400]
        feat.update(skip_flags(feat))
        feat["features_json"] = extra
        return feat


# --- ledger ---------------------------------------------------------------

def _source_entries() -> list[dict]:
    try:
        import young_trail
        rows = list(young_trail.open_rows())
        rows.extend(young_trail.closed_rows(limit=None))
        return [r for r in rows if r.get("tid") and _num(r.get("entry_price"), 0) > 0]
    except Exception as e:
        log.warning("young_trail_signals source entries: %s", safe_err(e))
        return []


def sync_entries(now: float | None = None) -> int:
    if not enabled():
        return 0
    ts = current_time() if now is None else float(now)
    if ts >= until_ts(ts):
        return 0
    n = 0
    try:
        db = _connect()
        for src in _source_entries():
            entry_ts = _num(src.get("entry_ts"))
            if entry_ts is None:
                continue
            cur = db.execute(
                """INSERT OR IGNORE INTO young_trail_signals (
                     tid, ticker, chain, entry_ts, entry_price, status, source
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    src["tid"], src.get("ticker"), src.get("chain"),
                    float(entry_ts), float(src["entry_price"]),
                    STATUS_PENDING, "sync",
                ),
            )
            if cur.rowcount:
                n += 1
        db.commit()
        if n:
            log.info("young_trail_signals sync entries=%d paper=1", n)
    except Exception as e:
        log.warning("young_trail_signals sync: %s", safe_err(e))
    return n


def _store_features(tid: str, feat: dict, now: float, source: str) -> None:
    flags = skip_flags(feat)
    extra = feat.get("features_json")
    extra_s = json.dumps(extra) if extra is not None else None
    err = feat.get("error")
    if err is not None:
        err = str(err)[:400]
    db = _connect()
    db.execute(
        """UPDATE young_trail_signals SET
             status = ?, rugcheck_score = ?, rugcheck_score_norm = ?,
             lp_locked_pct = ?, lp_burned_pct = ?,
             mint_authority = ?, freeze_authority = ?,
             top_holder_pct = ?, top_holder_flag = ?,
             insider_flag = ?, creator_flag = ?,
             danger_risk = ?, boost_amount = ?, boost_total = ?, paid_promo = ?,
             holder_count = ?, buy_30m = ?, sell_30m = ?,
             vol_buy_30m = ?, vol_sell_30m = ?, unique_wallets_30m = ?,
             buy_1h = ?, sell_1h = ?, vol_buy_1h = ?, vol_sell_1h = ?,
             unique_wallets_1h = ?, price_change_30m = ?, price_change_1h = ?,
             skip_danger = ?, skip_lp = ?, skip_authority = ?, skip_score = ?,
             skip_promo = ?, skip_momentum = ?,
             features_json = ?, error = ?, source = ?, computed_ts = ?,
             birdeye_skipped = ?
           WHERE tid = ?""",
        (
            feat.get("status") or STATUS_ERROR,
            feat.get("rugcheck_score"), feat.get("rugcheck_score_norm"),
            feat.get("lp_locked_pct"), feat.get("lp_burned_pct"),
            feat.get("mint_authority"), feat.get("freeze_authority"),
            feat.get("top_holder_pct"), feat.get("top_holder_flag"),
            feat.get("insider_flag"),
            feat.get("creator_flag"), feat.get("danger_risk"),
            feat.get("boost_amount"), feat.get("boost_total"),
            None if feat.get("paid_promo") is None else int(bool(feat.get("paid_promo"))),
            feat.get("holder_count"), feat.get("buy_30m"), feat.get("sell_30m"),
            feat.get("vol_buy_30m"), feat.get("vol_sell_30m"),
            feat.get("unique_wallets_30m"),
            feat.get("buy_1h"), feat.get("sell_1h"),
            feat.get("vol_buy_1h"), feat.get("vol_sell_1h"),
            feat.get("unique_wallets_1h"),
            feat.get("price_change_30m"), feat.get("price_change_1h"),
            None if flags["skip_danger"] is None else int(flags["skip_danger"]),
            None if flags["skip_lp"] is None else int(flags["skip_lp"]),
            None if flags["skip_authority"] is None else int(flags["skip_authority"]),
            None if flags["skip_score"] is None else int(flags["skip_score"]),
            None if flags["skip_promo"] is None else int(flags["skip_promo"]),
            None if flags["skip_momentum"] is None else int(flags["skip_momentum"]),
            extra_s, err, source, float(now),
            None if feat.get("birdeye_skipped") is None else int(bool(feat.get("birdeye_skipped"))),
            tid,
        ),
    )
    db.commit()


def compute_pending(now: float | None = None, limit: int | None = None,
                    source: str = "live") -> int:
    ts = current_time() if now is None else float(now)
    n = 0
    try:
        db = _connect()
        rows = db.execute(
            """SELECT * FROM young_trail_signals
               WHERE status = 'pending'
               ORDER BY entry_ts DESC""",
        ).fetchall()
        cap = 8 if limit is None else max(0, int(limit))
        for row in rows[:cap]:
            tid = row["tid"]
            mint, net = parse_tid(tid)
            chain = dex_chain_id(row["chain"], net, tid) or "solana"
            feat = compute_features(mint or "", chain=chain, now=ts)
            _store_features(tid, feat, ts, source)
            n += 1
    except Exception as e:
        log.warning("young_trail_signals compute: %s", safe_err(e))
    return n


def note_candidate(d: dict, now: float | None = None) -> dict | None:
    """Queue (tid, chain, mint) for the sampler. No HTTP, no sleep. Never gates."""
    empty = None
    try:
        if not d or not enabled():
            return empty
        ts = current_time() if now is None else float(now)
        if ts >= until_ts(ts):
            return empty
        tid = d.get("tid")
        mint, net = parse_tid(tid)
        if not mint:
            mint = d.get("address") or d.get("mint")
        chain = dex_chain_id(d.get("chain"), net or d.get("net"), tid) or "solana"
        if not tid and not mint:
            return empty
        rec = {
            "tid": tid,
            "chain": chain,
            "mint": mint,
            "ticker": d.get("ticker"),
            "queued_ts": ts,
        }
        key = str(tid or mint)
        with _pending_lock:
            _pending_candidates[key] = rec
        return rec
    except Exception as e:
        log.warning("young_trail_signals candidate: %s", safe_err(e))
        return empty


def pending_candidates() -> list[dict]:
    with _pending_lock:
        return list(_pending_candidates.values())


def drain_candidates(now: float | None = None, source: str = "chain_check") -> int:
    """Fetch queued desk candidates on the sampler thread. Ledger row if yt tid."""
    ts = current_time() if now is None else float(now)
    with _pending_lock:
        items = list(_pending_candidates.values())
        _pending_candidates.clear()
    n = 0
    for rec in items:
        try:
            mint = rec.get("mint") or ""
            chain = rec.get("chain") or "solana"
            feat = compute_features(mint, chain=chain, now=ts)
            tid = rec.get("tid")
            if not tid:
                n += 1
                continue
            db = _connect()
            row = db.execute(
                "SELECT tid FROM young_trail_signals WHERE tid = ?", (tid,),
            ).fetchone()
            if row is None:
                try:
                    import young_trail
                    yt_row = young_trail._connect().execute(
                        "SELECT tid, ticker, chain, entry_ts, entry_price "
                        "FROM young_trail WHERE tid = ?",
                        (tid,),
                    ).fetchone()
                except Exception:
                    yt_row = None
                if yt_row is not None:
                    db.execute(
                        """INSERT OR IGNORE INTO young_trail_signals (
                             tid, ticker, chain, entry_ts, entry_price, status, source
                           ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                        (
                            tid,
                            yt_row["ticker"] if "ticker" in yt_row.keys() else rec.get("ticker"),
                            (yt_row["chain"] if "chain" in yt_row.keys() else None) or chain,
                            float(yt_row["entry_ts"]), float(yt_row["entry_price"]),
                            STATUS_PENDING, source,
                        ),
                    )
                    db.commit()
                    row = True
            if row is not None:
                _store_features(tid, feat, ts, source)
            n += 1
        except Exception as e:
            log.warning("young_trail_signals drain: %s", safe_err(e))
    return n


def on_sampler_tick(prices: dict | None = None, now: float | None = None) -> dict:
    """Sampler hook AFTER rugrisk. Copies entries, fetches queued candidates. Never gates."""
    empty = {"entries": 0, "computed": 0, "drained": 0}
    try:
        if not enabled():
            return empty
        ts = current_time() if now is None else float(now)
        begin_cycle(ts)
        until_ts(ts)
        n = sync_entries(ts)
        drained = drain_candidates(now=ts, source="chain_check")
        computed = compute_pending(now=ts, source="live")
        return {"entries": n, "computed": computed, "drained": drained}
    except Exception as e:
        log.warning("young_trail_signals sampler: %s", safe_err(e))
        return empty


def all_rows() -> list[dict]:
    try:
        return [_row_dict(r) for r in _connect().execute(
            "SELECT * FROM young_trail_signals ORDER BY entry_ts DESC"
        ).fetchall()]
    except Exception as e:
        log.warning("young_trail_signals rows: %s", safe_err(e))
        return []


def _counted_pnl(rows) -> dict[str, float]:
    out: dict[str, float] = {}
    for r in rows or []:
        tid = r.get("tid")
        if not tid:
            continue
        if r.get("exit_reason") == "unmarked":
            continue
        if r.get("exit_ts") is None:
            continue
        pnl = _num(r.get("pnl_usd"))
        if pnl is None:
            continue
        out[tid] = float(pnl)
    return out


def _pnl_maps() -> dict[str, dict[str, float]]:
    maps = {"young_trail": {}, "fast_baseline": {}, "fast_tp50": {}}
    try:
        import young_trail
        maps["young_trail"] = _counted_pnl(
            young_trail.open_rows() + young_trail.closed_rows(limit=None)
        )
    except Exception as e:
        log.warning("young_trail_signals cycle pnl: %s", safe_err(e))
    try:
        import young_trail_fast as fast
        maps["fast_baseline"] = _counted_pnl(
            fast.open_rows("baseline") + fast.closed_rows("baseline", limit=None)
        )
        maps["fast_tp50"] = _counted_pnl(
            fast.open_rows("tp_50") + fast.closed_rows("tp_50", limit=None)
        )
    except Exception as e:
        log.warning("young_trail_signals fast pnl: %s", safe_err(e))
    return maps


def counterfactuals(rows: list[dict] | None = None) -> list[dict]:
    try:
        data = rows if rows is not None else all_rows()
        maps = _pnl_maps()
        return [counterfactual_for_rule(rule["id"], data, maps) for rule in SKIP_RULES]
    except Exception as e:
        log.warning("young_trail_signals counterfactual: %s", safe_err(e))
        return []


def _summary() -> dict:
    try:
        rows = all_rows()
        n_ok = sum(1 for r in rows if r.get("status") == STATUS_OK)
        n_na = sum(1 for r in rows if r.get("status") == STATUS_NA)
        n_pending = sum(1 for r in rows if r.get("status") == STATUS_PENDING)
        n_err = sum(1 for r in rows if r.get("status") == STATUS_ERROR)
        n_unavail = sum(1 for r in rows if r.get("status") == STATUS_UNAVAILABLE)
        return {
            "n": len(rows),
            "ok": n_ok,
            "n_a": n_na,
            "pending": n_pending,
            "error": n_err,
            "unavailable": n_unavail,
            "would_skip_danger": sum(1 for r in rows if r.get("skip_danger") == 1),
            "would_skip_lp": sum(1 for r in rows if r.get("skip_lp") == 1),
            "would_skip_authority": sum(1 for r in rows if r.get("skip_authority") == 1),
            "would_skip_score": sum(1 for r in rows if r.get("skip_score") == 1),
            "would_skip_promo": sum(1 for r in rows if r.get("skip_promo") == 1),
            "would_skip_momentum": sum(1 for r in rows if r.get("skip_momentum") == 1),
        }
    except Exception as e:
        log.warning("young_trail_signals summary: %s", safe_err(e))
        return {
            "n": 0, "ok": 0, "n_a": 0, "pending": 0, "error": 0, "unavailable": 0,
            "would_skip_danger": 0, "would_skip_lp": 0, "would_skip_authority": 0,
            "would_skip_score": 0, "would_skip_promo": 0, "would_skip_momentum": 0,
        }


def briefing_lines() -> list[str]:
    """Oct 20 scorecard lines: SKIPPED vs KEPT young_trail P&L per rule."""
    try:
        now = current_time()
        until = until_ts(now) if enabled() else None
        until_iso = (
            time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until))
            if until else None
        )
        cfs = counterfactuals()
        lines = [
            "- Paper RugCheck/boosts/Birdeye skip rules (counterfactual, no gating)"
            + (f" until {until_iso}" if until_iso else ""),
        ]
        if not cfs:
            lines.append("- Signals: no scored entries yet.")
            return lines
        for c in cfs:
            sk, kp = c.get("skipped") or {}, c.get("kept") or {}
            lines.append(
                f"- {c.get('label') or c.get('id')}: SKIPPED n={sk.get('n', 0)} "
                f"YT ${float(sk.get('young_trail_pnl') or 0):+.2f} · "
                f"KEPT n={kp.get('n', 0)} "
                f"YT ${float(kp.get('young_trail_pnl') or 0):+.2f} · "
                f"unknown {c.get('unknown_n', 0)}"
            )
        return lines
    except Exception as e:
        log.warning("young_trail_signals briefing: %s", safe_err(e))
        return ["- Paper signals unavailable."]


def ops_payload() -> dict:
    """JSON for GET /ops/young_trail_signals. Paper/simulated; no live orders."""
    try:
        now = current_time()
        until = until_ts(now) if enabled() else None
        rows = all_rows()
        return {
            "paper": True,
            "simulated": True,
            "live_orders": False,
            "gates_entries": False,
            "enabled": enabled(),
            "accepting": accepting(now),
            "birdeye_enabled": birdeye_enabled(),
            "until_ts": until,
            "until_iso": (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until))
                if until else None
            ),
            "ticket_usd": TICKET_USD,
            "caveat": (
                "Paper RugCheck / DexScreener boosts / Birdeye features on "
                "young_trail entries. Skip rules are counterfactual only — they "
                "do not gate entries, picks, or the judge. Birdeye is skipped "
                "when BIRDEYE_API_KEY is unset."
            ),
            "rules": [
                {
                    "id": r["id"], "label": r["label"],
                    "threshold": (
                        score_cut() if r["id"] == RULE_SCORE
                        else lp_cut() if r["id"] == RULE_LP
                        else momentum_px_cut() if r["id"] == RULE_MOMENTUM
                        else r.get("threshold")
                    ),
                }
                for r in SKIP_RULES
            ],
            "budget": {
                "rugcheck_used": _cycle_used.get("rugcheck", 0),
                "rugcheck_cap": rugcheck_cap(),
                "orders_used": _cycle_used.get("orders", 0),
                "orders_cap": orders_cap(),
                "birdeye_used": _cycle_used.get("birdeye", 0),
                "birdeye_cap": birdeye_cap(),
            },
            "summary": _summary(),
            "counterfactuals": counterfactuals(rows),
            "entries": rows[:80],
        }
    except Exception as e:
        log.warning("young_trail_signals ops: %s", safe_err(e))
        return {
            "paper": True,
            "simulated": True,
            "live_orders": False,
            "gates_entries": False,
            "error": "unavailable",
            "summary": _summary(),
            "counterfactuals": [],
            "entries": [],
        }


def main(argv=None) -> int:
    import argparse
    ap = argparse.ArgumentParser(
        description="Paper young_trail RugCheck/boosts/Birdeye features (no gating)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("ops", help="Print ops payload JSON")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "ops":
            print(json.dumps(ops_payload(), default=str, indent=2))
            return 0
    except Exception as e:
        log.error("young_trail_signals cli: %s", safe_err(e))
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    raise SystemExit(main())
