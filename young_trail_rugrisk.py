"""Paper rug-avoidance features on young_trail entries. Analysis only.

Copies the same tids as the young_trail / fast / helius ledgers. For Solana
tokens, budgeted Helius RPC (SOLANA_RPC_URL) computes entry-time rug-risk
features: sniper share, bundled launch buys, creator prior rugs, and fresh
wallets among top holders. The create tx is found via the pump.fun bonding
curve, the creator wallet, or the pool account — not by paging mint history.
Non-Solana rows are stored as status='n/a'.

Skip rules are counterfactual only: they never gate entries, picks, or the
judge. Fail open. Exceptions through safe_err. Never logs SOLANA_RPC_URL.

Does NOT change young_trail, fast, variants, helius, judging, or live orders.
Runs after young_trail_helius on the sampler so helius keeps first claim on
the shared Helius day budget.
"""
from __future__ import annotations

import calendar
import hashlib
import json
import logging
import os
import pathlib
import re
import sqlite3
import threading
import time

from secret_utils import safe_err

log = logging.getLogger("young_trail_rugrisk")

TICKET_USD = 50.0
LOCK_TIMEOUT_SEC = 0.2
RPC_TIMEOUT_SEC = 8.0
# 2026-10-20 23:59 America/New_York (EDT). Override with YOUNG_TRAIL_UNTIL /
# YOUNG_TRAIL_RUGRISK_UNTIL.
DEFAULT_UNTIL_ISO = "2026-10-21T03:59:59Z"
MAX_TX_VERSION = 1
MAX_TX_VERSION_CAP = 2
GTFA_RETRY_SEC = 30 * 60
RETRY_AFTER_SEC = 10 * 60
# Unavailable+truncated rows computed on/after this instant are requeued as v1tx.
V1TX_AFTER_TS = calendar.timegm(
    time.strptime("2026-10-15T00:00:00Z", "%Y-%m-%dT%H:%M:%SZ")
)
SOL_NET = 1399811149
_TX_VERSION_RE = re.compile(
    r"Transaction version \((\d+)\) is not supported", re.IGNORECASE
)

# Own caps (same per-tick shape as helius, smaller day cap). Sit out when
# helius day_left drops below the reserve so the rug-signal ledger is not starved.
DEFAULT_CYCLE_CAP = 12
DEFAULT_DAY_CAP = 800
DEFAULT_OVERNIGHT_CYCLE_CAP = 24
DEFAULT_HELIUS_DAY_RESERVE = 400

# ~3.2s at 400ms/slot. Share is of total supply, not of the early-window buys.
SNIPER_SLOTS = 8
SNIPER_SKIP_PCT = 0.30
# 1–2 bonding-curve buys must not fire the skip rule by themselves.
SNIPER_MIN_WALLETS = 4
BUNDLED_MIN_BUYERS = 3
FRESH_MAX_SIGS = 2
DEV_RUG_SKIP = 1
TOP_HOLDERS = 10
# Do not page mint history. Hot pump mints already have >5k sigs at entry.
# Find the create tx via the bonding-curve PDA, the creator wallet, or the
# pool account (short histories), then take txs in the sniper slot window.
# Truncated (none of those paths work) is unavailable, never skip.
SIG_PAGE_SIZE = 1000
MINT_SIG_PAGES = 1
WALLET_SIG_LIMIT = 8
DEV_SIG_LIMIT = 25
DEV_TX_CAP = 4
# Fresh-wallet share is not a skip rule. Probe only leftover budget / cache.
FRESH_WALLET_RPC_CAP = 0
CREATOR_CREATE_TX_CAP = 8
# Create tx plus non-create txs inside the sniper window. Target ≤8 RPC.
WINDOW_NON_CREATE_CAP = 8
TARGET_ENTRY_CALLS = 8
DEFAULT_TICK_ENTRIES = 8
START_CALLS = 4
PUMP_PROGRAM_ID = "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P"
ATA_PROGRAM_ID = "ATokenGPvbdGVxr1b2hvZbsiqW5xWH25efTNsLJA8knL"
WSOL_MINT = "So11111111111111111111111111111111111111112"
BONDING_CURVE_SEED = b"bonding-curve"
BONDING_CURVE_V2_SEED = b"bonding-curve-v2"
_B58_ALPHABET = "123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz"
_ED25519_P = 2**255 - 19
_ED25519_D = (_ED25519_P - 121665) * pow(121666, _ED25519_P - 2, _ED25519_P) % _ED25519_P
_ED25519_I = pow(2, (_ED25519_P - 1) // 4, _ED25519_P)

STATUS_OK = "ok"
STATUS_NA = "n/a"
STATUS_PENDING = "pending"
STATUS_ERROR = "error"
STATUS_UNAVAILABLE = "unavailable"
STATUS_BUDGET = "budget"
SOURCE_SNIPER_V2 = "sniper_v2"
SOURCE_V1TX = "v1tx"
SOURCE_DEVHIST = "devhist"
SNIPER_V2_OK = "ok"
SNIPER_V2_NEEDS = "needs_recompute"
META_SNIPER_V2 = "sniper_v2_invalidate"
META_V1TX = "requeue_v1tx"
META_TRANSIENT = "requeue_transient_rpc_errors"
META_LAST_OVERNIGHT = "last_overnight_ts"
META_BACKLOG_LOG_HOUR = "backlog_log_hour"
META_DEV_MINTS = "fixup_dev_prior_mints"
META_RESTORE_DEV_UNKNOWN = "ytr_restore_dev_unknown"

RULE_SNIPERS = "snipers_gt_30"
RULE_BUNDLED = "bundled"
RULE_DEV_RUGS = "dev_prior_rugs"

SKIP_RULES = (
    {
        "id": RULE_SNIPERS,
        "label": "snipers > 30%",
        "field": "skip_snipers",
        "threshold": SNIPER_SKIP_PCT,
        "min_wallets": SNIPER_MIN_WALLETS,
    },
    {
        "id": RULE_BUNDLED,
        "label": "bundled launch buys",
        "field": "skip_bundled",
        "threshold": None,
    },
    {
        "id": RULE_DEV_RUGS,
        "label": "dev with prior rugs",
        "field": "skip_dev_rugs",
        "threshold": DEV_RUG_SKIP,
    },
)

CREATE_IX = {
    "initializeMint", "initializeMint2", "InitializeMint", "InitializeMint2",
    "create", "Create", "create_v2", "CreateV2", "createMetadataAccountV3",
}
# Prior-launch instructions: pump.fun create or SPL initializeMint. ATA
# `create` and metadata creates are not launches.
SPL_MINT_IX = {
    "initializeMint", "initializeMint2", "InitializeMint", "InitializeMint2",
}
PUMP_CREATE_IX = {"create", "Create", "create_v2", "CreateV2"}
RUG_IX = {
    "removeLiquidity", "remove_liquidity", "withdraw", "withdrawAll",
    "closeAccount", "sell", "withdrawProtocolFee",
}

_tls = threading.local()
_schema_lock = threading.Lock()
_time_fn = time.time
_rpc_fn = None
_overnight_fn = None
_cycle_calls = 0
_last_new_tids: list[str] = []
_gtfa_supported: bool | None = None
_gtfa_retry_after: float = 0.0
_tick_rpc_ok: bool | None = None
_dev_cache: dict[str, dict] = {}
_creator_cache: dict[str, str | None] = {}
_wallet_fresh_cache: dict[str, bool] = {}


def set_time_fn(fn) -> None:
    global _time_fn
    _time_fn = time.time if fn is None else fn


def set_rpc_fn(fn) -> None:
    """Inject rpc(method, params, timeout) for tests. None restores."""
    global _rpc_fn
    _rpc_fn = fn


def set_overnight_fn(fn) -> None:
    """Inject overnight predicate for tests. None restores OUTCOMES_WINDOW."""
    global _overnight_fn
    _overnight_fn = fn


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
    global _cycle_calls, _last_new_tids, _gtfa_supported, _gtfa_retry_after
    global _tick_rpc_ok, _dev_cache, _creator_cache, _wallet_fresh_cache
    _close_local()
    _cycle_calls = 0
    _last_new_tids = []
    _gtfa_supported = None
    _gtfa_retry_after = 0.0
    _tick_rpc_ok = None
    _dev_cache = {}
    _creator_cache = {}
    _wallet_fresh_cache = {}


def _schema_step(db: sqlite3.Connection, name: str, fn) -> None:
    """One schema change. Failure is logged and does not skip later steps."""
    try:
        fn()
        db.commit()
    except Exception as e:
        log.warning("young_trail_rugrisk schema %s: %s", name, safe_err(e))
        try:
            db.rollback()
        except Exception:
            pass


def init_schema(db: sqlite3.Connection | None = None) -> None:
    """Idempotent. Each step commits on its own (live-DB safe)."""
    conn = db if db is not None else _connect()
    _schema_step(conn, "young_trail_rugrisk", lambda: conn.execute("""
    CREATE TABLE IF NOT EXISTS young_trail_rugrisk (
      tid TEXT PRIMARY KEY,
      ticker TEXT,
      chain TEXT,
      entry_ts REAL NOT NULL,
      entry_price REAL,
      status TEXT NOT NULL DEFAULT 'pending',
      sniper_share REAL,
      sniper_wallets INTEGER,
      sniper_slots INTEGER,
      bundled INTEGER,
      bundled_buys INTEGER,
      dev_wallet TEXT,
      dev_prior_launches INTEGER,
      dev_prior_rugs INTEGER,
      fresh_holder_share REAL,
      top_holder_n INTEGER,
      skip_snipers INTEGER,
      skip_bundled INTEGER,
      skip_dev_rugs INTEGER,
      features_json TEXT,
      error TEXT,
      source TEXT,
      computed_ts REAL
    )"""))
    _schema_step(conn, "idx_ytr_status", lambda: conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ytr_status ON young_trail_rugrisk(status)"
    ))
    _schema_step(conn, "idx_ytr_entry", lambda: conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_ytr_entry ON young_trail_rugrisk(entry_ts)"
    ))
    _schema_step(conn, "young_trail_rugrisk_meta", lambda: conn.execute("""
    CREATE TABLE IF NOT EXISTS young_trail_rugrisk_meta (
      key TEXT PRIMARY KEY,
      value TEXT
    )"""))
    _schema_step(conn, "ytr_dev_initial_share", lambda: _add_column(
        conn, "young_trail_rugrisk", "dev_initial_share", "REAL"
    ))
    _schema_step(conn, "ytr_sniper_denominator", lambda: _add_column(
        conn, "young_trail_rugrisk", "sniper_denominator", "REAL"
    ))
    _schema_step(conn, "ytr_sniper_min_wallets", lambda: _add_column(
        conn, "young_trail_rugrisk", "sniper_min_wallets", "INTEGER"
    ))
    _schema_step(conn, "ytr_reset_truncated_create_path",
                 lambda: _reset_truncated_rows(conn))
    _schema_step(conn, "ytr_backfill_sniper_v2",
                 lambda: _backfill_sniper_v2(conn))
    _schema_step(conn, "ytr_requeue_v1tx",
                 lambda: _requeue_v1tx_rows(conn))
    _schema_step(conn, "ytr_requeue_transient_rpc_errors",
                 lambda: _requeue_transient_rpc_errors(conn))
    _schema_step(conn, "ytr_fixup_dev_prior_mints",
                 lambda: _fixup_dev_prior_mints(conn))
    _schema_step(conn, "ytr_restore_dev_unknown",
                 lambda: _restore_dev_unknown(conn))


def _table_columns(conn: sqlite3.Connection, table: str) -> set[str]:
    return {row[1] for row in conn.execute(f"PRAGMA table_info({table})")}


def _add_column(conn: sqlite3.Connection, table: str, name: str, decl: str) -> None:
    if name in _table_columns(conn, table):
        return
    conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


def _reset_truncated_rows(conn: sqlite3.Connection) -> None:
    """Requeue history_truncated rows once so overnight backfill uses the new path."""
    row = conn.execute(
        "SELECT value FROM young_trail_rugrisk_meta WHERE key = ?",
        ("reset_truncated_create_path",),
    ).fetchone()
    if row and str(row["value"]) == "1":
        return
    conn.execute(
        """UPDATE young_trail_rugrisk
           SET status = 'pending',
               sniper_share = NULL, sniper_wallets = NULL,
               bundled = NULL, bundled_buys = NULL,
               skip_snipers = NULL, skip_bundled = NULL, skip_dev_rugs = NULL,
               features_json = NULL, error = NULL, computed_ts = NULL,
               source = 'requeue'
           WHERE status = 'unavailable'
             AND (
               IFNULL(error, '') LIKE '%history_truncated%'
               OR IFNULL(features_json, '') LIKE '%history_truncated%'
             )"""
    )
    conn.execute(
        """INSERT INTO young_trail_rugrisk_meta (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        ("reset_truncated_create_path", "1"),
    )


def _backfill_sniper_v2(conn: sqlite3.Connection) -> None:
    """Recompute sniper share from cached buys+supply only. No RPC.

    Rows without buys or total supply keep status=ok but sniper_share and
    skip_snipers are NULLed and stamped sniper_v2=needs_recompute (unknown
    in the counterfactual). They are re-queued overnight, not here.
    """
    row = conn.execute(
        "SELECT value FROM young_trail_rugrisk_meta WHERE key = ?",
        (META_SNIPER_V2,),
    ).fetchone()
    if row and str(row["value"]) == "1":
        return
    cols = _table_columns(conn, "young_trail_rugrisk")
    if "sniper_min_wallets" not in cols:
        return
    try:
        rows = conn.execute(
            "SELECT * FROM young_trail_rugrisk WHERE status = 'ok'"
        ).fetchall()
        for raw in rows:
            d = _row_dict(raw)
            updated = recompute_sniper_from_cached(d)
            if not updated:
                continue
            extra = updated.get("features_json")
            extra_s = json.dumps(extra) if extra is not None else d.get("features_json")
            skip = updated.get("skip_snipers")
            conn.execute(
                """UPDATE young_trail_rugrisk SET
                     sniper_share = ?, sniper_wallets = ?, sniper_slots = ?,
                     sniper_denominator = ?, sniper_min_wallets = ?,
                     skip_snipers = ?, features_json = ?
                   WHERE tid = ?""",
                (
                    updated.get("sniper_share"),
                    updated.get("sniper_wallets"),
                    updated.get("sniper_slots"),
                    updated.get("sniper_denominator"),
                    updated.get("sniper_min_wallets"),
                    None if skip is None else int(bool(skip)),
                    extra_s,
                    d["tid"],
                ),
            )
    except Exception as e:
        log.warning("young_trail_rugrisk sniper v2 backfill: %s", safe_err(e))
        raise
    conn.execute(
        """INSERT INTO young_trail_rugrisk_meta (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (META_SNIPER_V2, "1"),
    )


def _requeue_v1tx_rows(conn: sqlite3.Connection) -> None:
    """One-time: truncated unavailable rows computed after Oct 15 → pending v1tx."""
    row = conn.execute(
        "SELECT value FROM young_trail_rugrisk_meta WHERE key = ?",
        (META_V1TX,),
    ).fetchone()
    if row and str(row["value"]) == "1":
        return
    conn.execute(
        """UPDATE young_trail_rugrisk
           SET status = 'pending',
               sniper_share = NULL, sniper_wallets = NULL,
               bundled = NULL, bundled_buys = NULL,
               skip_snipers = NULL, skip_bundled = NULL, skip_dev_rugs = NULL,
               features_json = NULL, error = NULL, computed_ts = NULL,
               source = ?
           WHERE status = 'unavailable'
             AND computed_ts IS NOT NULL
             AND computed_ts >= ?
             AND (
               IFNULL(error, '') LIKE '%history_truncated%'
               OR IFNULL(features_json, '') LIKE '%history_truncated%'
             )""",
        (SOURCE_V1TX, float(V1TX_AFTER_TS)),
    )
    conn.execute(
        """INSERT INTO young_trail_rugrisk_meta (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (META_V1TX, "1"),
    )


def _requeue_transient_rpc_errors(conn: sqlite3.Connection) -> None:
    """One-time: ConnectionError / JSONDecodeError rows go back to pending."""
    row = conn.execute(
        "SELECT value FROM young_trail_rugrisk_meta WHERE key = ?",
        (META_TRANSIENT,),
    ).fetchone()
    if row and str(row["value"]) == "1":
        return
    conn.execute(
        """UPDATE young_trail_rugrisk
           SET status = 'pending', error = NULL, computed_ts = NULL
           WHERE status = 'error'
             AND (
               IFNULL(error, '') LIKE 'ConnectionError%'
               OR IFNULL(error, '') LIKE 'JSONDecodeError%'
             )"""
    )
    conn.execute(
        """INSERT INTO young_trail_rugrisk_meta (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (META_TRANSIENT, "1"),
    )


def _fixup_dev_prior_mints(conn: sqlite3.Connection) -> None:
    """One-time: drop wSOL / current mint / same-slot noise from stored prior_mints.

    No RPC. Recomputes dev_prior_launches, dev_prior_rugs, skip_dev_rugs.
    Rows that never ran dev history (launches NULL) stay unknown — an empty
    prior_mints placeholder is not evidence of 0.
    """
    row = conn.execute(
        "SELECT value FROM young_trail_rugrisk_meta WHERE key = ?",
        (META_DEV_MINTS,),
    ).fetchone()
    if row and str(row["value"]) == "1":
        return
    try:
        rows = conn.execute(
            "SELECT * FROM young_trail_rugrisk WHERE status = 'ok'"
        ).fetchall()
        for raw in rows:
            d = _row_dict(raw)
            updated = recompute_dev_from_cached(d)
            if not updated:
                continue
            extra = updated.get("features_json")
            extra_s = json.dumps(extra) if extra is not None else d.get("features_json")
            skip = updated.get("skip_dev_rugs")
            conn.execute(
                """UPDATE young_trail_rugrisk SET
                     dev_prior_launches = ?, dev_prior_rugs = ?,
                     skip_dev_rugs = ?, features_json = ?
                   WHERE tid = ?""",
                (
                    updated.get("dev_prior_launches"),
                    updated.get("dev_prior_rugs"),
                    None if skip is None else int(bool(skip)),
                    extra_s, d["tid"],
                ),
            )
    except Exception as e:
        log.warning("young_trail_rugrisk dev mints fixup: %s", safe_err(e))
        raise
    conn.execute(
        """INSERT INTO young_trail_rugrisk_meta (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (META_DEV_MINTS, "1"),
    )


def devhist_restart_ts() -> float:
    """#79 restart: 2026-10-19 08:41:52 America/New_York. Computed, not a raw epoch."""
    from datetime import datetime
    try:
        from zoneinfo import ZoneInfo
        tz = ZoneInfo("America/New_York")
        return datetime(2026, 10, 19, 8, 41, 52, tzinfo=tz).timestamp()
    except Exception:
        # 2026-10-19 is EDT (UTC-4).
        return float(calendar.timegm((2026, 10, 19, 12, 41, 52)))


def _has_dev_lookup_evidence(extra: dict) -> bool:
    """True when features_json shows a real _dev_history lookup, not a placeholder."""
    if not isinstance(extra, dict):
        return False
    if extra.get("dev"):
        return True
    prog = extra.get("progress")
    if isinstance(prog, dict):
        if prog.get("dev_done") or prog.get("dev"):
            return True
    return False


def _restore_dev_unknown(conn: sqlite3.Connection) -> None:
    """One-time: undo 0/0 written onto rows that never ran dev history. No RPC.

    Matching ok rows go back to unknown (NULL launches/rugs/skip) and are
    queued overnight as source=devhist for a 2-call lookup.
    """
    row = conn.execute(
        "SELECT value FROM young_trail_rugrisk_meta WHERE key = ?",
        (META_RESTORE_DEV_UNKNOWN,),
    ).fetchone()
    if row and str(row["value"]) == "1":
        return
    cutoff = devhist_restart_ts()
    n = 0
    try:
        rows = conn.execute(
            "SELECT * FROM young_trail_rugrisk WHERE status = 'ok'"
        ).fetchall()
        for raw in rows:
            d = _row_dict(raw)
            extra = _parse_features_blob(d.get("features_json"))
            if not _row_is_false_dev_zero(d, extra, cutoff):
                continue
            conn.execute(
                """UPDATE young_trail_rugrisk SET
                     dev_prior_launches = NULL,
                     dev_prior_rugs = NULL,
                     skip_dev_rugs = NULL,
                     status = 'pending',
                     error = NULL,
                     source = ?
                   WHERE tid = ? AND status = 'ok'""",
                (SOURCE_DEVHIST, d["tid"]),
            )
            n += 1
    except Exception as e:
        log.warning("young_trail_rugrisk restore dev unknown: %s", safe_err(e))
        raise
    conn.execute(
        """INSERT INTO young_trail_rugrisk_meta (key, value) VALUES (?, ?)
           ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
        (META_RESTORE_DEV_UNKNOWN, "1"),
    )
    if n:
        log.info("young_trail_rugrisk restore dev unknown n=%d paper=1", n)


def _row_is_false_dev_zero(d: dict, extra: dict, cutoff: float) -> bool:
    if (d.get("status") or "") != STATUS_OK:
        return False
    cts = _num(d.get("computed_ts"))
    if cts is None or float(cts) >= float(cutoff):
        return False
    try:
        if int(d.get("dev_prior_launches")) != 0:
            return False
        if int(d.get("dev_prior_rugs")) != 0:
            return False
    except (TypeError, ValueError):
        return False
    prior = extra.get("prior_mints") if isinstance(extra, dict) else None
    if not isinstance(prior, list) or len(prior) != 0:
        return False
    if _has_dev_lookup_evidence(extra):
        return False
    return True


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
    raw = os.environ.get("YOUNG_TRAIL_RUGRISK_ENABLED")
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
            "SELECT value FROM young_trail_rugrisk_meta WHERE key = ?", (key,),
        ).fetchone()
        return None if row is None else row["value"]
    except Exception as e:
        log.warning("young_trail_rugrisk meta read: %s", safe_err(e))
        return None


def _meta_set(key: str, value: str) -> None:
    try:
        db = _connect()
        db.execute(
            """INSERT INTO young_trail_rugrisk_meta (key, value) VALUES (?, ?)
               ON CONFLICT(key) DO UPDATE SET value = excluded.value""",
            (key, value),
        )
        db.commit()
    except Exception as e:
        log.warning("young_trail_rugrisk meta write: %s", safe_err(e))


def deploy_ts(now: float | None = None) -> float:
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
    raw = os.environ.get("YOUNG_TRAIL_RUGRISK_UNTIL")
    parsed = _parse_until(raw) if raw else None
    if parsed is not None:
        return parsed
    try:
        import young_trail
        ts = float(young_trail.until_ts(now))
    except Exception as e:
        log.warning("young_trail_rugrisk until from young_trail: %s", safe_err(e))
        ts = _parse_until(DEFAULT_UNTIL_ISO) or 0.0
    stored = _meta_get("until_ts")
    if stored != str(ts):
        _meta_set("until_ts", str(ts))
    return ts


def accepting(now: float | None = None) -> bool:
    ts = current_time() if now is None else float(now)
    return enabled() and ts < until_ts(ts)


def is_overnight(now: float | None = None) -> bool:
    """True in OUTCOMES_WINDOW (default 01:00–06:00 local). Tests inject."""
    if _overnight_fn is not None:
        try:
            return bool(_overnight_fn(now))
        except Exception:
            return False
    try:
        import gt_api
        return bool(gt_api.in_outcomes_window(now))
    except Exception as e:
        log.warning("young_trail_rugrisk overnight: %s", safe_err(e))
        return False


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


def cycle_cap(now: float | None = None) -> int:
    if is_overnight(now):
        return _env_int(
            "YOUNG_TRAIL_RUGRISK_OVERNIGHT_CYCLE_CAP",
            DEFAULT_OVERNIGHT_CYCLE_CAP,
        )
    return _env_int("YOUNG_TRAIL_RUGRISK_CYCLE_CAP", DEFAULT_CYCLE_CAP)


def day_cap() -> int:
    return _env_int("YOUNG_TRAIL_RUGRISK_DAY_CAP", DEFAULT_DAY_CAP)


def helius_day_reserve() -> int:
    return _env_int(
        "YOUNG_TRAIL_RUGRISK_HELIUS_DAY_RESERVE", DEFAULT_HELIUS_DAY_RESERVE,
    )


def sniper_skip_pct() -> float:
    n = _env_float("YOUNG_TRAIL_RUGRISK_SNIPER_PCT", SNIPER_SKIP_PCT)
    return min(1.0, max(0.0, n))


def sniper_slots() -> int:
    return _env_int("YOUNG_TRAIL_RUGRISK_SNIPER_SLOTS", SNIPER_SLOTS)


def sniper_min_wallets() -> int:
    return _env_int("YOUNG_TRAIL_RUGRISK_SNIPER_MIN_WALLETS", SNIPER_MIN_WALLETS)


def tick_entry_cap(now: float | None = None) -> int:
    """Pending rows to attempt per tick. Budget still binds; fair-share splits."""
    if is_overnight(now):
        return _env_int(
            "YOUNG_TRAIL_RUGRISK_OVERNIGHT_TICK_ENTRIES", DEFAULT_TICK_ENTRIES,
        )
    return _env_int("YOUNG_TRAIL_RUGRISK_TICK_ENTRIES", DEFAULT_TICK_ENTRIES)


def _day_key(now: float | None = None) -> str:
    ts = current_time() if now is None else float(now)
    return time.strftime("%Y-%m-%d", time.gmtime(ts))


def _day_used(now: float | None = None) -> int:
    key = _day_key(now)
    if _meta_get("budget_day") != key:
        return 0
    raw = _meta_get("budget_day_calls")
    try:
        return int(raw or 0)
    except (TypeError, ValueError):
        return 0


def begin_cycle(now: float | None = None) -> None:
    """Reset the per-tick call counter. Does not touch young_trail_helius."""
    global _cycle_calls, _tick_rpc_ok
    _cycle_calls = 0
    _tick_rpc_ok = None
    key = _day_key(now)
    if _meta_get("budget_day") != key:
        _meta_set("budget_day", key)
        _meta_set("budget_day_calls", "0")


def budget_remaining(now: float | None = None) -> dict:
    day_left = max(0, day_cap() - _day_used(now))
    cap = cycle_cap(now)
    cycle_left = max(0, cap - _cycle_calls)
    return {
        "cycle_used": _cycle_calls,
        "cycle_cap": cap,
        "cycle_left": cycle_left,
        "day_used": _day_used(now),
        "day_cap": day_cap(),
        "day_left": day_left,
        "day": _day_key(now),
        "overnight": is_overnight(now),
        "helius_day_reserve": helius_day_reserve(),
    }


def _helius_day_left(now: float | None = None) -> int | None:
    """Leftover helius day budget. None if unreadable (fail open)."""
    try:
        import young_trail_helius as helius
        left = helius.budget_remaining(now)["day_left"]
        return int(left)
    except Exception as e:
        log.warning("young_trail_rugrisk helius budget: %s", safe_err(e))
        return None


def helius_allows(now: float | None = None) -> bool:
    """False when the helius ledger is low on its day cap (don't starve it)."""
    left = _helius_day_left(now)
    if left is None:
        return True
    return left >= helius_day_reserve()


def _can_charge(n: int = 1, now: float | None = None) -> bool:
    if n <= 0:
        return True
    if not helius_allows(now):
        return False
    left = budget_remaining(now)
    return left["cycle_left"] >= n and left["day_left"] >= n


def _charge(n: int = 1, now: float | None = None) -> bool:
    """Count n Helius calls against rugrisk caps only. False if gone."""
    global _cycle_calls
    if n <= 0:
        return True
    if not _can_charge(n, now):
        return False
    _cycle_calls += n
    used = _day_used(now) + n
    _meta_set("budget_day", _day_key(now))
    _meta_set("budget_day_calls", str(used))
    return True


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


def _clamp_tx_version(version) -> int:
    try:
        ver = int(version)
    except (TypeError, ValueError):
        ver = MAX_TX_VERSION
    return min(max(0, ver), MAX_TX_VERSION_CAP)


def _with_max_tx_version(method: str, params, version: int | None = None):
    """Ensure getTransaction / getBlock / GTFA request versioned txs."""
    if method not in ("getTransaction", "getBlock", "getTransactionsForAddress"):
        return params
    params = list(params or [])
    if not params:
        return params
    ver = MAX_TX_VERSION if version is None else _clamp_tx_version(version)
    if len(params) < 2 or not isinstance(params[1], dict):
        if len(params) == 1:
            params.append({"maxSupportedTransactionVersion": ver})
        return params
    cfg = dict(params[1])
    cfg["maxSupportedTransactionVersion"] = ver
    params[1] = cfg
    return params


def _tx_version_from_error(err) -> int | None:
    """Parse `Transaction version (N) is not supported`. None if not that error."""
    if err is None:
        return None
    if isinstance(err, dict):
        msg = str(err.get("message") or err)
    else:
        msg = str(err)
    m = _TX_VERSION_RE.search(msg)
    if not m:
        return None
    return _clamp_tx_version(m.group(1))


def _is_transient_rpc_error(err) -> bool:
    if err is None:
        return False
    if isinstance(err, json.JSONDecodeError):
        return True
    if isinstance(err, (ConnectionError, TimeoutError)):
        return True
    name = type(err).__name__
    if name in (
        "ConnectionError", "ConnectTimeout", "ReadTimeout", "Timeout",
        "TimeoutError", "JSONDecodeError", "ChunkedEncodingError",
        "ProtocolError", "SSLError",
    ):
        return True
    msg = safe_err(err) if isinstance(err, BaseException) else str(err)
    if msg.startswith("ConnectionError") or msg.startswith("JSONDecodeError"):
        return True
    low = msg.lower()
    if "timed out" in low or "timeout" in low:
        return True
    if "json" in low and "decode" in low:
        return True
    if re.search(r"\bHTTP 5\d{2}\b", msg):
        return True
    if re.search(r"\bstatus(?:\s+code)?[:\s]+5\d{2}\b", low):
        return True
    return False


def _rpc_raw(method: str, params, timeout: float):
    """One injected or HTTP JSON-RPC call. No budget, no version retry."""
    if _rpc_fn is not None:
        return _rpc_fn(method, params, timeout)
    import requests
    rpc_url = os.environ.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"
    try:
        resp = requests.post(
            rpc_url,
            json={"jsonrpc": "2.0", "id": 1, "method": method, "params": params},
            timeout=max(0.5, float(timeout)),
        )
        if getattr(resp, "status_code", 200) >= 500:
            raise RuntimeError(f"HTTP {resp.status_code}")
        data = resp.json()
    except Exception as e:
        log.warning("young_trail_rugrisk rpc %s: %s", method, safe_err(e))
        raise
    if not isinstance(data, dict):
        raise RuntimeError("solana rpc: non-object response")
    if "error" in data:
        err = data["error"]
        if isinstance(err, dict):
            raise RuntimeError(err.get("message") or "solana rpc error")
        raise RuntimeError(str(err))
    if "result" not in data:
        raise RuntimeError("solana rpc: missing result")
    return data["result"]


def _rpc_call(method: str, params, timeout: float, now: float | None,
              allow_version_retry: bool):
    """Charge once, call, optionally retry once on a tx-version error."""
    if not _charge(1, now):
        raise RuntimeError("rugrisk budget exhausted")
    try:
        return _rpc_raw(method, params, timeout)
    except Exception as e:
        if "budget" in str(e).lower():
            raise
        ver = _tx_version_from_error(e) if allow_version_retry else None
        if ver is None:
            raise
        retry_params = _with_max_tx_version(method, params, version=ver)
        if not _charge(1, now):
            raise RuntimeError("rugrisk budget exhausted")
        return _rpc_raw(method, retry_params, timeout)


def _rpc(method: str, params, timeout: float = RPC_TIMEOUT_SEC, now: float | None = None):
    """One Helius JSON-RPC call. Charges rugrisk budget first. Never logs the URL."""
    params = _with_max_tx_version(method, params)
    return _rpc_call(method, params, timeout, now, allow_version_retry=True)


def _retry_version_item(method: str, params, err, timeout: float,
                        now: float | None):
    """Retry one batch item at the unsupported version. None if not that error."""
    ver = _tx_version_from_error(err)
    if ver is None:
        return None, False
    retry_params = _with_max_tx_version(method, params, version=ver)
    try:
        return _rpc_call(method, retry_params, timeout, now, False), True
    except Exception as e:
        if "budget" in str(e).lower() or isinstance(e, AssertionError):
            raise
        if _is_transient_rpc_error(e):
            raise
        log.warning("young_trail_rugrisk rpc %s: %s", method, safe_err(e))
        return None, True


def _rpc_batch(calls: list[tuple[str, list]], timeout: float = RPC_TIMEOUT_SEC,
               now: float | None = None) -> list:
    """One HTTP round-trip for many methods. Charges 1 against the rugrisk cap."""
    if not calls:
        return []
    prepared = [(m, _with_max_tx_version(m, p)) for m, p in calls]
    if not _charge(1, now):
        raise RuntimeError("rugrisk budget exhausted")
    if _rpc_fn is not None:
        out = []
        for method, params in prepared:
            try:
                out.append(_rpc_fn(method, params, timeout))
                continue
            except Exception as e:
                msg = str(e).lower()
                if "budget" in msg or isinstance(e, AssertionError):
                    raise
                retried, handled = _retry_version_item(
                    method, params, e, timeout, now,
                )
                if handled:
                    out.append(retried)
                    continue
                if _is_transient_rpc_error(e):
                    raise
                log.warning("young_trail_rugrisk rpc %s: %s", method, safe_err(e))
                out.append(None)
        return out
    import requests
    rpc_url = os.environ.get("SOLANA_RPC_URL") or "https://api.mainnet-beta.solana.com"
    payload = [
        {"jsonrpc": "2.0", "id": i + 1, "method": method, "params": params}
        for i, (method, params) in enumerate(prepared)
    ]
    try:
        resp = requests.post(
            rpc_url, json=payload, timeout=max(0.5, float(timeout)),
        )
        if getattr(resp, "status_code", 200) >= 500:
            raise RuntimeError(f"HTTP {resp.status_code}")
        data = resp.json()
    except Exception as e:
        log.warning("young_trail_rugrisk rpc batch: %s", safe_err(e))
        raise
    by_id: dict = {}
    if isinstance(data, list):
        for item in data:
            if isinstance(item, dict) and "id" in item:
                by_id[item["id"]] = item
    elif isinstance(data, dict) and "id" in data:
        by_id[data["id"]] = data
    out = []
    for i, (method, params) in enumerate(prepared):
        item = by_id.get(i + 1)
        if not isinstance(item, dict):
            out.append(None)
            continue
        if "error" in item:
            err = item["error"]
            retried, handled = _retry_version_item(
                method, params, err, timeout, now,
            )
            if handled:
                out.append(retried)
                continue
            msg = err.get("message") if isinstance(err, dict) else str(err)
            log.warning(
                "young_trail_rugrisk rpc %s: %s", method,
                safe_err(RuntimeError(str(msg))),
            )
            out.append(None)
            continue
        out.append(item.get("result"))
    return out


def _rpc_reachable(now: float | None = None) -> bool:
    """One cheap getHealth per tick. False if the host is down. Counts against budget."""
    global _tick_rpc_ok
    if _tick_rpc_ok is not None:
        return _tick_rpc_ok
    ts = current_time() if now is None else float(now)
    if not _can_charge(1, ts):
        return False
    try:
        _rpc("getHealth", [], now=ts)
        _tick_rpc_ok = True
    except Exception as e:
        log.warning("young_trail_rugrisk rpc health: %s", safe_err(e))
        _tick_rpc_ok = False
    return bool(_tick_rpc_ok)


def _tok_amt(obj) -> float:
    if not isinstance(obj, dict):
        return 0.0
    ui = _num(obj.get("uiAmount"))
    if ui is not None:
        return float(ui)
    amt = _num(obj.get("amount"))
    return float(amt) if amt is not None else 0.0


# --- pure feature math (no I/O) -------------------------------------------

def _owner_set(owners) -> set[str]:
    return {str(x) for x in (owners or set()) if x}


def sniper_exclude_owners(mint: str | None, creators=None, holders=None) -> set[str]:
    """Creator, bonding-curve PDAs, and pool/AMM accounts are not sniper wallets."""
    skip = _owner_set(creators)
    skip.add(PUMP_PROGRAM_ID)
    if mint:
        for addr in (
            bonding_curve_pda(mint),
            bonding_curve_v2_pda(mint),
            _helius_pool_account(mint),
        ):
            if addr:
                skip.add(str(addr))
    for h in holders or []:
        if not h.get("is_pool"):
            continue
        if h.get("owner"):
            skip.add(str(h["owner"]))
        if h.get("account"):
            skip.add(str(h["account"]))
    return skip


def sniper_share_from_buys(buys: list[dict], supply: float, create_slot: int,
                           n_slots: int, exclude=None) -> dict:
    """Share of *total supply* bought in [create_slot, create_slot+n_slots].

    `exclude` should include the creator, bonding-curve PDA, and pool accounts.
    Denominator is total supply, never the sum of early-window buys.
    """
    n_slots = max(0, int(n_slots if n_slots is not None else sniper_slots()))
    empty = {
        "sniper_share": None, "sniper_wallets": 0, "sniper_amount": 0.0,
        "sniper_denominator": None if not supply or supply <= 0 else float(supply),
        "sniper_slots": n_slots,
    }
    if not supply or supply <= 0 or create_slot is None:
        return empty
    skip = _owner_set(exclude)
    end = int(create_slot) + n_slots
    wallets: dict[str, float] = {}
    for b in buys or []:
        slot = b.get("slot")
        owner = b.get("owner")
        delta = _num(b.get("delta"), 0.0) or 0.0
        if not owner or slot is None or delta <= 0:
            continue
        if str(owner) in skip:
            continue
        if int(create_slot) <= int(slot) <= end:
            wallets[str(owner)] = wallets.get(str(owner), 0.0) + float(delta)
    amount = sum(wallets.values())
    return {
        "sniper_share": amount / float(supply),
        "sniper_wallets": len(wallets),
        "sniper_amount": amount,
        "sniper_denominator": float(supply),
        "sniper_slots": n_slots,
    }


def _parse_features_blob(raw) -> dict:
    if isinstance(raw, dict):
        return dict(raw)
    if isinstance(raw, str) and raw:
        try:
            blob = json.loads(raw)
        except (TypeError, ValueError):
            return {}
        return dict(blob) if isinstance(blob, dict) else {}
    return {}


def _slim_sniper_buys(buys: list | None, create_slot, n_slots: int) -> list[dict]:
    """Compact {owner, delta, slot} for the sniper window only."""
    if create_slot is None:
        return []
    end = int(create_slot) + max(0, int(n_slots))
    out: list[dict] = []
    for b in buys or []:
        if not isinstance(b, dict):
            continue
        slot = b.get("slot")
        owner = b.get("owner")
        delta = _num(b.get("delta"), 0.0) or 0.0
        if not owner or slot is None or delta <= 0:
            continue
        if int(create_slot) <= int(slot) <= end:
            out.append({
                "owner": str(owner), "delta": float(delta), "slot": int(slot),
            })
        if len(out) >= 40:
            break
    return out


def recompute_sniper_from_cached(row: dict) -> dict | None:
    """Rebuild sniper share from stored buys + total supply. No RPC.

    Without both, the old window-relative share is discarded (NULL share /
    NULL skip) and stamped needs_recompute so the comparison counts unknown.
    """
    if not row or row.get("status") not in (STATUS_OK, None):
        if row and row.get("status") != STATUS_OK:
            return None
    extra = _parse_features_blob(row.get("features_json"))
    buys = extra.get("buys")
    if not isinstance(buys, list) or not buys:
        buys = None
    supply = (
        _num(row.get("sniper_denominator"))
        or _num(extra.get("sniper_denominator"))
        or _num(extra.get("supply"))
        or _num(row.get("supply"))
    )
    create_slot = extra.get("create_slot")
    if create_slot is None:
        create_slot = row.get("create_slot")
    n_slots = row.get("sniper_slots")
    if n_slots is None:
        n_slots = extra.get("sniper_slots")
    if n_slots is None:
        n_slots = sniper_slots()
    n_slots = int(n_slots)
    creators = extra.get("creators") or []
    mint, _net = parse_tid(row.get("tid"))
    min_w = sniper_min_wallets()
    out = dict(row)
    out["sniper_min_wallets"] = min_w
    out["sniper_slots"] = n_slots
    out["status"] = row.get("status") or STATUS_OK
    if buys and supply and supply > 0 and create_slot is not None:
        exclude = sniper_exclude_owners(mint, creators, extra.get("holders"))
        sn = sniper_share_from_buys(
            buys, float(supply), int(create_slot), n_slots, exclude=exclude,
        )
        out.update(sn)
        extra["buys"] = _slim_sniper_buys(buys, create_slot, n_slots)
        extra["supply"] = float(supply)
        extra["sniper_denominator"] = sn.get("sniper_denominator") or float(supply)
        extra["sniper_slots"] = n_slots
        extra["sniper_min_wallets"] = min_w
        extra["sniper_v2"] = SNIPER_V2_OK
        extra["create_slot"] = int(create_slot)
        out["features_json"] = extra
        out.update(skip_flags(out))
        return out
    extra["sniper_v2"] = SNIPER_V2_NEEDS
    extra.pop("sniper_share", None)
    out["sniper_share"] = None
    out["sniper_denominator"] = None
    out["skip_snipers"] = None
    out["features_json"] = extra
    flags = skip_flags(out)
    flags["skip_snipers"] = None
    out.update(flags)
    return out


def bundled_from_buys(buys: list[dict], create_slot: int,
                      fresh_owners: set | None = None, exclude=None) -> dict:
    """3+ distinct non-creator buys in the creation slot. Pure."""
    if create_slot is None:
        return {"bundled": None, "bundled_buys": 0, "create_slot_buyers": 0,
                "create_slot_fresh": 0}
    skip = _owner_set(exclude)
    buyers = set()
    fresh = set()
    known_fresh = _owner_set(fresh_owners)
    for b in buys or []:
        if b.get("slot") is None or int(b["slot"]) != int(create_slot):
            continue
        owner = b.get("owner")
        delta = _num(b.get("delta"), 0.0) or 0.0
        if not owner or delta <= 0:
            continue
        if str(owner) in skip:
            continue
        buyers.add(str(owner))
        if str(owner) in known_fresh:
            fresh.add(str(owner))
    n = len(buyers)
    return {
        "bundled": n >= BUNDLED_MIN_BUYERS,
        "bundled_buys": n,
        "create_slot_buyers": n,
        "create_slot_fresh": len(fresh),
    }


def dev_initial_share_from_buys(buys: list[dict], supply: float, create_slot: int,
                               creators) -> float | None:
    """Creator's create-slot buy as a fraction of supply. Pure."""
    if not supply or supply <= 0 or create_slot is None:
        return None
    owners = _owner_set(creators)
    if not owners:
        return None
    amount = 0.0
    for b in buys or []:
        if b.get("slot") is None or int(b["slot"]) != int(create_slot):
            continue
        owner = b.get("owner")
        delta = _num(b.get("delta"), 0.0) or 0.0
        if not owner or delta <= 0 or str(owner) not in owners:
            continue
        amount += float(delta)
    return amount / float(supply)


def fresh_holder_share(holders: list[dict], fresh_owners: set) -> dict:
    """Fresh-wallet share of non-pool top-holder amounts. Pure."""
    fresh = {str(x) for x in (fresh_owners or set())}
    held = []
    for h in holders or []:
        if h.get("is_pool"):
            continue
        owner = h.get("owner")
        amt = _num(h.get("amount"), 0.0) or 0.0
        if not owner or amt <= 0:
            continue
        held.append((str(owner), float(amt)))
    total = sum(a for _o, a in held)
    if total <= 0:
        return {"fresh_holder_share": None, "top_holder_n": len(held)}
    fresh_amt = sum(a for o, a in held if o in fresh)
    return {
        "fresh_holder_share": fresh_amt / total,
        "top_holder_n": len(held),
    }


def skip_flags(features: dict) -> dict:
    """Pure skip-rule evaluation. None = unknown (fail open: do not skip)."""
    status = features.get("status")
    if status in (STATUS_NA, STATUS_PENDING, STATUS_ERROR, STATUS_UNAVAILABLE,
                  STATUS_BUDGET):
        return {
            "skip_snipers": None, "skip_bundled": None, "skip_dev_rugs": None,
        }
    sniper = _num(features.get("sniper_share"))
    n_wallets = features.get("sniper_wallets")
    min_w = features.get("sniper_min_wallets")
    if min_w is None:
        min_w = sniper_min_wallets()
    try:
        min_w = int(min_w)
    except (TypeError, ValueError):
        min_w = sniper_min_wallets()
    skip_s = None
    if sniper is None:
        skip_s = None
    elif n_wallets is not None:
        try:
            n_w = int(n_wallets)
        except (TypeError, ValueError):
            n_w = 0
        if n_w < max(0, min_w):
            skip_s = False
        else:
            skip_s = bool(sniper > sniper_skip_pct())
    else:
        # Wallet count missing: fail open (do not skip).
        skip_s = False
    bundled = features.get("bundled")
    skip_b = None if bundled is None else bool(bundled)
    rugs = features.get("dev_prior_rugs")
    skip_d = None if rugs is None else bool(int(rugs) >= DEV_RUG_SKIP)
    return {
        "skip_snipers": skip_s,
        "skip_bundled": skip_b,
        "skip_dev_rugs": skip_d,
    }


def would_skip(row: dict, rule_id: str) -> bool | None:
    """True if this rule would have skipped the entry. None = unknown/n/a."""
    if not row or row.get("status") in (
        STATUS_NA, STATUS_UNAVAILABLE, STATUS_ERROR, STATUS_BUDGET, STATUS_PENDING,
    ):
        return None
    flags = skip_flags(row)
    by_id = {
        RULE_SNIPERS: flags["skip_snipers"],
        RULE_BUNDLED: flags["skip_bundled"],
        RULE_DEV_RUGS: flags["skip_dev_rugs"],
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
    return {
        "id": rule_id,
        "label": rule.get("label") or rule_id,
        "threshold": rule.get("threshold"),
        "skipped": side(skipped),
        "kept": side(kept),
        "unknown_n": len(unknown),
    }


# --- RPC fetch helpers ----------------------------------------------------

def _supply(mint: str, now: float | None = None) -> float | None:
    result = _rpc("getTokenSupply", [mint], now=now)
    value = result.get("value") if isinstance(result, dict) else result
    if not isinstance(value, dict):
        return _num(value)
    amt = _tok_amt(value)
    return amt if amt > 0 else _num(value.get("amount"))


def _largest_accounts(mint: str, now: float | None = None) -> list[dict]:
    result = _rpc("getTokenLargestAccounts", [mint], now=now)
    rows = result.get("value") if isinstance(result, dict) else result
    out = []
    for acc in rows or []:
        if not isinstance(acc, dict) or not acc.get("address"):
            continue
        amt = _tok_amt(acc)
        if amt == 0:
            amt = _num(acc.get("amount"), 0.0) or 0.0
        out.append({"address": acc["address"], "amount": float(amt)})
    return out


def _owners_for(accounts: list[str], now: float | None = None) -> dict[str, dict]:
    if not accounts:
        return {}
    addrs = accounts[:20]
    result = _rpc(
        "getMultipleAccounts",
        [addrs, {"encoding": "jsonParsed"}],
        now=now,
    )
    values = result.get("value") if isinstance(result, dict) else result
    out: dict[str, dict] = {}
    for i, acc in enumerate(values or []):
        if not acc or i >= len(addrs):
            continue
        owner = None
        program = acc.get("owner")
        parsed = acc.get("data")
        if isinstance(parsed, dict):
            info = ((parsed.get("parsed") or {}).get("info") or {})
            owner = info.get("owner")
        out[addrs[i]] = {"owner": owner, "program": program}
    return out


def _signatures(address: str, limit: int = 20, before: str | None = None,
                now: float | None = None) -> list[dict]:
    """One getSignaturesForAddress page. Charges the rugrisk budget."""
    opts: dict = {"limit": max(1, min(int(limit), 1000))}
    if before:
        opts["before"] = before
    result = _rpc("getSignaturesForAddress", [address, opts], now=now)
    rows = result if isinstance(result, list) else (result or [])
    out = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("signature"):
            continue
        out.append({
            "signature": row["signature"],
            "slot": row.get("slot"),
            "blockTime": row.get("blockTime"),
            "err": row.get("err"),
        })
    return out


def _mint_signature_history(mint: str, now: float | None = None,
                            collected: list | None = None, pages: int = 0,
                            before: str | None = None) -> dict:
    """Page mint signatures newest→oldest until exhausted, the page cap, or budget.

    state: exhausted | truncated (cap hit) | budget (resume later).
    """
    page_size = max(1, min(int(SIG_PAGE_SIZE), 1000))
    max_pages = max(1, int(MINT_SIG_PAGES))
    collected = list(collected or [])
    pages = int(pages or 0)
    exhausted = False
    budget_stop = False
    while pages < max_pages:
        if not _can_charge(1, now):
            budget_stop = True
            break
        raw = _signatures(mint, limit=page_size, before=before, now=now)
        pages += 1
        if not raw:
            exhausted = True
            before = None
            break
        collected.extend(raw)
        if len(raw) < page_size:
            exhausted = True
            before = None
            break
        before = raw[-1]["signature"]
    if budget_stop:
        state = "budget"
    elif exhausted:
        state = "exhausted"
    else:
        state = "truncated"
    return {
        "sigs": collected, "pages": pages, "before": before, "state": state,
    }


def _oldest_slot(sigs: list[dict]) -> int | None:
    for row in reversed(sigs or []):
        if row.get("slot") is not None:
            return int(row["slot"])
    return None


def _b58decode(s: str) -> bytes | None:
    if not s:
        return None
    n = 0
    try:
        for ch in s:
            n = n * 58 + _B58_ALPHABET.index(ch)
    except ValueError:
        return None
    raw = n.to_bytes((n.bit_length() + 7) // 8, "big") if n else b""
    pad = len(s) - len(s.lstrip("1"))
    out = b"\x00" * pad + raw
    if len(out) != 32:
        return None
    return out


def _b58encode(raw: bytes) -> str:
    n = int.from_bytes(raw, "big")
    out = ""
    while n:
        n, r = divmod(n, 58)
        out = _B58_ALPHABET[r] + out
    pad = 0
    for b in raw:
        if b:
            break
        pad += 1
    return ("1" * pad) + (out or "1")


def _ed25519_on_curve(pubkey: bytes) -> bool:
    if not pubkey or len(pubkey) != 32:
        return False
    y = int.from_bytes(pubkey, "little")
    sign = y >> 255
    y &= (1 << 255) - 1
    y2 = pow(y, 2, _ED25519_P)
    u = (y2 - 1) % _ED25519_P
    v = (_ED25519_D * y2 + 1) % _ED25519_P
    x2 = u * pow(v, _ED25519_P - 2, _ED25519_P) % _ED25519_P
    x = pow(x2, (_ED25519_P + 3) // 8, _ED25519_P)
    if (x * x - x2) % _ED25519_P != 0:
        x = (x * _ED25519_I) % _ED25519_P
    if (x * x - x2) % _ED25519_P != 0:
        return False
    if (x % 2) != sign:
        x = _ED25519_P - x
    return True


def find_program_address(seeds: list[bytes], program_id: str) -> str | None:
    """Solana PDA. None if seeds/program are invalid."""
    prog = _b58decode(program_id)
    if prog is None or len(prog) != 32:
        return None
    nonce = 255
    while nonce >= 0:
        buf = b"".join(seeds) + bytes([nonce]) + prog + b"ProgramDerivedAddress"
        digest = hashlib.sha256(buf).digest()
        if not _ed25519_on_curve(digest):
            return _b58encode(digest)
        nonce -= 1
    return None


def bonding_curve_pda(mint: str, seed: bytes = BONDING_CURVE_SEED) -> str | None:
    raw = _b58decode(mint)
    if raw is None or len(raw) != 32:
        return None
    return find_program_address([seed, raw], PUMP_PROGRAM_ID)


def bonding_curve_v2_pda(mint: str) -> str | None:
    return bonding_curve_pda(mint, BONDING_CURVE_V2_SEED)


def _helius_pool_account(mint: str) -> str | None:
    if not mint:
        return None
    tid = f"{mint}:{SOL_NET}"
    try:
        import young_trail_helius as helius
        row = helius._connect().execute(
            "SELECT pool_account FROM young_trail_helius_watch WHERE tid = ?",
            (tid,),
        ).fetchone()
        if row and row["pool_account"]:
            return str(row["pool_account"])
    except Exception:
        return None
    return None


def _candidate_history_addrs(mint: str, holders: list | None) -> list[tuple[str, str]]:
    """Short-history accounts: pump bonding curve, then LP/pool."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()

    def add(src: str, addr: str | None) -> None:
        if addr and addr not in seen:
            seen.add(addr)
            out.append((src, addr))

    add("bonding_curve", bonding_curve_pda(mint))
    add("bonding_curve_v2", bonding_curve_v2_pda(mint))
    for h in holders or []:
        if h.get("is_pool"):
            add("pool", h.get("owner") or h.get("account"))
    add("pool", _helius_pool_account(mint))
    return out


def _set_gtfa_supported(val: bool | None) -> None:
    global _gtfa_supported
    _gtfa_supported = val


def _is_rpc_method_missing(err) -> bool:
    msg = str(err).lower()
    return (
        "method not found" in msg
        or "methodnotfound" in msg
        or "-32601" in msg
        or "unknown method" in msg
        or "does not exist" in msg
    )


def _gtfa_usable(now: float | None = None) -> bool:
    """False when the RPC lacks GTFA, or a transient miss is still in cooldown."""
    if _gtfa_supported is False:
        return False
    if _gtfa_supported is True:
        return True
    ts = current_time() if now is None else float(now)
    return ts >= float(_gtfa_retry_after)


def _gtfa_oldest(address: str, now: float | None = None, limit: int = 16,
                 slot_gte: int | None = None, slot_lte: int | None = None):
    """Helius getTransactionsForAddress, oldest first. None if unsupported.

    Cache 'unsupported' only on method-not-found. Network blips sit out the
    cooldown then retry. Budget exhaustion is re-raised.
    """
    if not address or not _gtfa_usable(now):
        return None
    opts: dict = {
        "transactionDetails": "full",
        "encoding": "jsonParsed",
        "sortOrder": "asc",
        "limit": max(1, min(int(limit), 1000)),
        "maxSupportedTransactionVersion": MAX_TX_VERSION,
        "filters": {"status": "succeeded"},
    }
    slot_f = {}
    if slot_gte is not None:
        slot_f["gte"] = int(slot_gte)
    if slot_lte is not None:
        slot_f["lte"] = int(slot_lte)
    if slot_f:
        opts["filters"]["slot"] = slot_f
    try:
        result = _rpc("getTransactionsForAddress", [address, opts], now=now)
    except Exception as e:
        msg = str(e).lower()
        if "budget" in msg:
            raise
        global _gtfa_retry_after
        # Tx-version errors are retried inside _rpc. Never treat them as
        # "method missing" — cool down and probe again later.
        if _tx_version_from_error(e) is not None:
            _set_gtfa_supported(None)
            ts = current_time() if now is None else float(now)
            _gtfa_retry_after = ts + max(0.0, float(GTFA_RETRY_SEC))
            log.warning("young_trail_rugrisk gtfa probe: %s", safe_err(e))
            return None
        if _is_rpc_method_missing(e):
            _set_gtfa_supported(False)
            log.warning("young_trail_rugrisk gtfa unsupported: %s", safe_err(e))
        else:
            _set_gtfa_supported(None)
            ts = current_time() if now is None else float(now)
            _gtfa_retry_after = ts + max(0.0, float(GTFA_RETRY_SEC))
            log.warning("young_trail_rugrisk gtfa probe: %s", safe_err(e))
        return None
    _set_gtfa_supported(True)
    rows = []
    if isinstance(result, dict):
        rows = result.get("data") or result.get("transactions") or []
    elif isinstance(result, list):
        rows = result
    out = []
    for row in rows or []:
        if not isinstance(row, dict):
            continue
        parsed = _gtfa_row_to_tx(row)
        if parsed:
            out.append(parsed)
    return out


def _gtfa_row_to_tx(row: dict) -> dict | None:
    try:
        if not isinstance(row, dict):
            return None
        tx_body = row.get("transaction") if isinstance(row.get("transaction"), dict) else None
        meta = row.get("meta") if isinstance(row.get("meta"), dict) else None
        if tx_body is None and isinstance(row.get("value"), dict):
            tx_body = (row["value"] or {}).get("transaction")
            meta = (row["value"] or {}).get("meta")
        sig = row.get("signature")
        if not sig and isinstance(tx_body, dict):
            sigs = tx_body.get("signatures") or []
            if sigs:
                sig = sigs[0]
        if not sig:
            return None
        tx = {
            "slot": row.get("slot"),
            "version": row.get("version"),
            "transaction": tx_body or {"message": {}, "signatures": [sig]},
            "meta": meta or {},
        }
        return {"signature": str(sig), "slot": row.get("slot"), "tx": tx}
    except Exception:
        return None


def _commit_window(prog: dict, recs: dict, ordered_sigs: list[str], src: str,
                   allow_oldest: bool = False) -> bool:
    """True when ordered recs contain a CREATE_IX tx. Mutates prog.

    Exhausted short histories may use the oldest sig as the create slot when
    the create instruction wasn't parsed. Non-create txs are kept only when
    their slot is inside the sniper window (not an arbitrary N-tx cap).
    """
    cap_n = max(0, int(WINDOW_NON_CREATE_CAP))
    n_slots = sniper_slots()
    create_sig = None
    for sig in ordered_sigs:
        rec = recs.get(sig) or {}
        if rec.get("has_create_ix") and not create_sig:
            create_sig = sig
            break
    if not create_sig:
        if not (allow_oldest and ordered_sigs):
            return False
        first = recs.get(ordered_sigs[0]) or {}
        if first.get("slot") is None:
            return False
        create_sig = ordered_sigs[0]
    slot = (recs.get(create_sig) or {}).get("slot")
    end = None if slot is None else int(slot) + max(0, int(n_slots))
    non: list[str] = []
    extra_in_window = 0
    for sig in ordered_sigs:
        if sig == create_sig:
            continue
        rec = recs.get(sig) or {}
        rec_slot = rec.get("slot")
        if rec_slot is None or slot is None:
            if len(non) < cap_n:
                non.append(sig)
            continue
        if int(slot) <= int(rec_slot) <= int(end):
            extra_in_window += 1
            if len(non) < cap_n:
                non.append(sig)
    to_fetch = [create_sig] + [s for s in non if s != create_sig]
    scored = set(to_fetch)
    prog["tx_recs"] = recs
    prog["create_sig"] = create_sig
    prog["create_slot"] = slot
    prog["non_create_sigs"] = non
    prog["to_fetch"] = to_fetch
    prog["window_capped"] = extra_in_window > len(non) or any(
        s not in scored for s in ordered_sigs
    )
    prog["window_src"] = src
    prog["window_done"] = True
    return True


def _fill_create_window(mint: str, creator: str | None, holders: list | None,
                        now: float, prog: dict) -> str:
    """Locate create + first 15 non-create txs without paging mint history.

    Returns 'ok' | 'pending' | 'truncated'.
    """
    if prog.get("window_done") and prog.get("create_sig"):
        return "ok"
    cap_n = max(0, int(WINDOW_NON_CREATE_CAP))
    recs: dict = dict(prog.get("tx_recs") or {})
    tried = set(prog.get("tried_addrs") or [])
    addr_sigs: dict = dict(prog.get("addr_sigs") or {})
    addrs = _candidate_history_addrs(mint, holders)

    def save():
        prog["tx_recs"] = recs
        prog["tried_addrs"] = sorted(tried)
        prog["addr_sigs"] = addr_sigs

    def ingest_gtfa(rows, src) -> bool:
        if not rows:
            return False
        ordered = []
        for row in rows:
            sig = row["signature"]
            recs[sig] = _tx_record(sig, row.get("tx"), mint)
            ordered.append(sig)
        if _commit_window(prog, recs, ordered, src):
            if len(ordered) >= cap_n + 1:
                prog["window_capped"] = True
            return True
        return False

    def ingest_exhausted_sigs(sigs, src) -> str | None:
        """Newest-first page. None = keep looking; pending/ok otherwise."""
        if not sigs or len(sigs) >= max(1, min(int(SIG_PAGE_SIZE), 1000)):
            return None
        oldest = list(reversed([s for s in sigs if s.get("signature") and not s.get("err")]))
        take = oldest[: cap_n + 1]
        need = [row["signature"] for row in take if row["signature"] not in recs]
        if need:
            if not _can_charge(1, now):
                save()
                return "pending"
            try:
                fetched = _transactions(need, now=now)
            except Exception as e:
                if "budget" in str(e).lower():
                    save()
                    return "pending"
                log.warning("young_trail_rugrisk tx: %s", safe_err(e))
                fetched = {}
            for sig in need:
                recs[sig] = _tx_record(sig, fetched.get(sig), mint)
        ordered = [r["signature"] for r in take]
        if _commit_window(prog, recs, ordered, src, allow_oldest=True):
            prog["window_capped"] = len(oldest) > len(prog.get("to_fetch") or [])
            return "ok"
        return None

    for src, addr in addrs:
        if addr in tried:
            continue
        if addr not in addr_sigs and _gtfa_usable(now):
            if not _can_charge(1, now):
                save()
                return "pending"
            rows = _gtfa_oldest(addr, now=now, limit=cap_n + 1)
            if ingest_gtfa(rows, src + "+gtfa"):
                return "ok"
        if addr not in addr_sigs:
            if not _can_charge(1, now):
                save()
                return "pending"
            try:
                sigs = _signatures(addr, limit=min(int(SIG_PAGE_SIZE), 1000), now=now)
            except Exception as e:
                log.warning("young_trail_rugrisk curve sigs: %s", safe_err(e))
                sigs = []
            addr_sigs[addr] = sigs
            save()
        hit = ingest_exhausted_sigs(addr_sigs.get(addr) or [], src)
        if hit == "pending":
            return "pending"
        if hit == "ok":
            return "ok"
        tried.add(addr)
        save()

    # Creator wallet around launch (short; at entry the create tx is recent).
    if creator and not prog.get("creator_scanned"):
        if not _can_charge(1, now):
            save()
            return "pending"
        try:
            csigs = [s for s in _signatures(creator, limit=DEV_SIG_LIMIT, now=now)
                     if not s.get("err")]
        except Exception as e:
            log.warning("young_trail_rugrisk creator sigs: %s", safe_err(e))
            csigs = []
        prog["creator_sigs"] = csigs
        idx = int(prog.get("creator_scan_i") or 0)
        cap_tx = min(len(csigs), max(0, int(CREATOR_CREATE_TX_CAP)))
        create_sig = None
        need = []
        walk = []
        while idx < cap_tx:
            sig = csigs[idx]["signature"]
            walk.append(sig)
            idx += 1
            if sig not in recs:
                need.append(sig)
        if need:
            if not _can_charge(1, now):
                prog["creator_scan_i"] = int(prog.get("creator_scan_i") or 0)
                save()
                return "pending"
            try:
                fetched = _transactions(need, now=now)
            except Exception as e:
                if "budget" in str(e).lower():
                    save()
                    return "pending"
                log.warning("young_trail_rugrisk creator tx: %s", safe_err(e))
                fetched = {}
            for sig in need:
                recs[sig] = _tx_record(sig, fetched.get(sig), mint)
        for sig in walk:
            if recs.get(sig, {}).get("has_create_ix"):
                create_sig = sig
                break
        prog["creator_scan_i"] = idx
        prog["creator_scanned"] = True
        save()
        if create_sig:
            slot = (recs.get(create_sig) or {}).get("slot")
            if _gtfa_usable(now) and slot is not None:
                for src, addr in addrs:
                    if not _can_charge(1, now):
                        save()
                        return "pending"
                    rows = _gtfa_oldest(
                        addr, now=now, limit=cap_n + 1,
                        slot_gte=int(slot),
                        slot_lte=int(slot) + sniper_slots(),
                    )
                    if ingest_gtfa(rows, src + "+creator+gtfa"):
                        return "ok"
            # Window from the create tx alone if it already carries the buys
            # (bundled same-tx). Still mark src=creator.
            if _commit_window(prog, recs, [create_sig], "creator"):
                # Only the create tx: sniper/bundled from that tx's buyers.
                prog["window_capped"] = False
                return "ok"

    # Last resort: one newest-first mint page, trusted only when exhausted.
    mint_sigs = prog.get("mint_sigs")
    if mint_sigs is None and not prog.get("mint_last_resort"):
        if not _can_charge(1, now):
            save()
            return "pending"
        try:
            mint_sigs = _signatures(mint, limit=min(int(SIG_PAGE_SIZE), 1000), now=now)
        except Exception as e:
            log.warning("young_trail_rugrisk mint sigs: %s", safe_err(e))
            mint_sigs = []
        prog["mint_last_resort"] = True
        prog["mint_sigs"] = mint_sigs
        prog["sig_pages"] = 1
        save()
    if mint_sigs is not None:
        hit = ingest_exhausted_sigs(mint_sigs, "mint")
        if hit == "pending":
            return "pending"
        if hit == "ok":
            return "ok"
        # Full page: true oldest unknown. Do not page further.
        if mint_sigs and len(mint_sigs) >= max(1, min(int(SIG_PAGE_SIZE), 1000)):
            return "truncated"

    if prog.get("create_sig") and prog.get("window_done"):
        return "ok"
    return "truncated"


def _empty_tx_record(sig: str) -> dict:
    return {
        "signature": sig, "slot": None, "buyers": [], "signers": [],
        "has_create_ix": False, "created_mints": [], "rugged_mints": [],
    }


def _tx_has_create_ix(tx: dict, mint: str | None = None) -> bool:
    """True when this tx contains a mint/create instruction for `mint`."""
    if not tx:
        return False
    try:
        for ix in _iter_parsed_ix(tx):
            if not _ix_is_launch(ix):
                continue
            ix_mint = _ix_mint(ix)
            if mint is None or not ix_mint or ix_mint == mint:
                return True
    except Exception:
        return False
    return False


def _tx_record(sig: str, tx: dict | None, mint: str) -> dict:
    """Compact resume record: buyers, signers, create/rug ixs. No raw RPC blob.

    `created_mints` is only pump.fun create / SPL initializeMint, never wSOL
    or ATA `create` mints from token-account setup.
    """
    if not tx or not isinstance(tx, dict):
        return _empty_tx_record(sig)
    try:
        created, rugged = [], []
        seen_c: set[str] = set()
        seen_r: set[str] = set()
        for ix in _iter_parsed_ix(tx):
            ix_mint = _ix_mint(ix)
            if not ix_mint or ix_mint == WSOL_MINT:
                continue
            if _ix_is_launch(ix) and ix_mint not in seen_c:
                seen_c.add(ix_mint)
                created.append(str(ix_mint))
            if _ix_type(ix) in RUG_IX and ix_mint not in seen_r:
                seen_r.add(ix_mint)
                rugged.append(str(ix_mint))
        return {
            "signature": sig,
            "slot": tx.get("slot"),
            "buyers": _buyers_from_tx(tx, mint),
            "signers": _tx_signers(tx),
            "has_create_ix": _tx_has_create_ix(tx, mint),
            "created_mints": created,
            "rugged_mints": rugged,
        }
    except Exception:
        return _empty_tx_record(sig)


def _in_window_sigs(mint_sigs: list[dict], create_slot: int | None,
                    n_slots: int) -> list[dict]:
    if create_slot is None:
        return []
    end = int(create_slot) + max(0, int(n_slots))
    out = []
    for row in mint_sigs or []:
        if row.get("err") or row.get("slot") is None:
            continue
        slot = int(row["slot"])
        if int(create_slot) <= slot <= end:
            out.append({
                "signature": row["signature"], "slot": slot,
            })
    out.sort(key=lambda r: (int(r["slot"]), r["signature"]))
    return out


def _tx_signers(tx: dict) -> list[str]:
    """Fee-payer / required signers of a jsonParsed transaction (v0 or v1)."""
    if not tx or not isinstance(tx, dict):
        return []
    try:
        body = tx.get("transaction")
        if isinstance(body, dict):
            msg = body.get("message") or {}
        else:
            msg = tx.get("message") or {}
        if not isinstance(msg, dict):
            return []
        keys = msg.get("accountKeys") or []
        signers = []
        for k in keys:
            if isinstance(k, dict) and k.get("signer") and k.get("pubkey"):
                signers.append(str(k["pubkey"]))
        if signers:
            return signers
        n = _num((msg.get("header") or {}).get("numRequiredSignatures"), 0) or 0
        for i, k in enumerate(keys):
            if i >= int(n):
                break
            if isinstance(k, str):
                signers.append(k)
            elif isinstance(k, dict) and k.get("pubkey"):
                signers.append(str(k["pubkey"]))
        return signers
    except Exception:
        return []


def _transaction(sig: str, now: float | None = None) -> dict | None:
    got = _transactions([sig], now=now)
    rec = got.get(sig)
    return rec if isinstance(rec, dict) else None


def _transactions(sigs: list[str], now: float | None = None) -> dict[str, dict | None]:
    """Batch getTransaction. One budget charge for the whole list."""
    out: dict[str, dict | None] = {}
    pending = [s for s in sigs if s]
    if not pending:
        return out
    opts = {
        "encoding": "jsonParsed",
        "maxSupportedTransactionVersion": MAX_TX_VERSION,
    }
    calls = [("getTransaction", [s, opts]) for s in pending]
    results = _rpc_batch(calls, now=now)
    for sig, rec in zip(pending, results):
        out[sig] = rec if isinstance(rec, dict) else None
    return out


def _iter_parsed_ix(tx: dict):
    """Yield jsonParsed instructions from a v0 or v1 transaction. Empty on junk."""
    try:
        if not isinstance(tx, dict):
            return
        body = tx.get("transaction")
        if isinstance(body, dict):
            msg = body.get("message") or {}
        elif isinstance(tx.get("message"), dict):
            msg = tx.get("message") or {}
        else:
            msg = {}
        if not isinstance(msg, dict):
            return
        for ix in msg.get("instructions") or []:
            if isinstance(ix, dict):
                yield ix
        meta = tx.get("meta") if isinstance(tx.get("meta"), dict) else {}
        for inner in meta.get("innerInstructions") or []:
            if not isinstance(inner, dict):
                continue
            for ix in (inner.get("instructions") or []):
                if isinstance(ix, dict):
                    yield ix
    except Exception:
        return


def _ix_type(ix: dict) -> str:
    parsed = ix.get("parsed")
    if isinstance(parsed, dict):
        return str(parsed.get("type") or "")
    return ""


def _ix_program_id(ix: dict) -> str:
    if not isinstance(ix, dict):
        return ""
    return str(ix.get("programId") or "")


def _ix_is_ata(ix: dict) -> bool:
    if not isinstance(ix, dict):
        return False
    pid = _ix_program_id(ix)
    if pid == ATA_PROGRAM_ID:
        return True
    prog = str(ix.get("program") or "").lower()
    return "associated-token" in prog


def _ix_is_launch(ix: dict) -> bool:
    """True for a pump.fun create or SPL initializeMint/initializeMint2.

    ATA `create` (same parsed type as pump create) is not a launch.
    """
    kind = _ix_type(ix)
    if kind in SPL_MINT_IX:
        return True
    if kind not in PUMP_CREATE_IX:
        return False
    if _ix_is_ata(ix):
        return False
    pid = _ix_program_id(ix)
    if pid and pid != PUMP_PROGRAM_ID:
        return False
    return True


def _ix_mint(ix: dict) -> str | None:
    parsed = ix.get("parsed")
    if not isinstance(parsed, dict):
        return None
    info = parsed.get("info") or {}
    mint = info.get("mint") or info.get("tokenMint")
    return str(mint) if mint else None


def _buyers_from_tx(tx: dict, mint: str) -> list[dict]:
    """Wallets whose balance of `mint` rose in this transaction."""
    if not tx or not mint or not isinstance(tx, dict):
        return []
    try:
        slot = tx.get("slot")
        meta = tx.get("meta") if isinstance(tx.get("meta"), dict) else {}
        pre = {}
        for row in meta.get("preTokenBalances") or []:
            if not isinstance(row, dict) or row.get("mint") != mint:
                continue
            owner = row.get("owner")
            if owner:
                pre[owner] = _tok_amt(row.get("uiTokenAmount") or row)
        post = {}
        for row in meta.get("postTokenBalances") or []:
            if not isinstance(row, dict) or row.get("mint") != mint:
                continue
            owner = row.get("owner")
            if owner:
                post[owner] = _tok_amt(row.get("uiTokenAmount") or row)
        out = []
        for owner, after in post.items():
            before = pre.get(owner, 0.0)
            delta = after - before
            if delta > 0:
                out.append({"owner": owner, "delta": delta, "slot": slot})
        return out
    except Exception:
        return []


def lookup_dev_wallet(tid: str) -> str | None:
    """Read-only stored creator for this tid. Cycle-thread only. No RPC."""
    if not tid:
        return None
    try:
        row = _connect().execute(
            """SELECT dev_wallet FROM young_trail_rugrisk
               WHERE tid = ? AND dev_wallet IS NOT NULL
                 AND TRIM(dev_wallet) != ''""",
            (tid,),
        ).fetchone()
        if row and row["dev_wallet"]:
            wallet = str(row["dev_wallet"]).strip()
            return wallet or None
    except Exception as e:
        log.warning("young_trail_rugrisk lookup_dev_wallet: %s", safe_err(e))
    return None


def _lookup_creator(tid: str) -> str | None:
    if not tid:
        return None
    if tid in _creator_cache:
        return _creator_cache[tid]
    try:
        import young_trail_helius as helius
        val = helius.lookup_creator(tid)
    except Exception as e:
        log.warning("young_trail_rugrisk creator: %s", safe_err(e))
        val = None
    _creator_cache[tid] = val
    return val


def _label_rugged(mint: str) -> bool | None:
    if not mint:
        return None
    tid = f"{mint}:{SOL_NET}"
    try:
        import research
        row = research._connect().execute(
            "SELECT rugged FROM token_labels WHERE tid = ?", (tid,),
        ).fetchone()
        if row is None:
            return None
        return bool(row["rugged"])
    except Exception:
        return None


def filter_stored_prior_mints(prior_mints, current_mint: str | None,
                              create_slot=None) -> list[dict]:
    """Drop wSOL, the current mint, and same-slot (not-prior) entries. Pure."""
    out = []
    seen: set[str] = set()
    cur = str(current_mint) if current_mint else ""
    slot_now = None
    if create_slot is not None:
        try:
            slot_now = int(create_slot)
        except (TypeError, ValueError):
            slot_now = None
    for item in prior_mints or []:
        if not isinstance(item, dict):
            continue
        mint = str(item.get("mint") or "")
        if not mint or mint == WSOL_MINT or (cur and mint == cur):
            continue
        if mint in seen:
            continue
        slot = item.get("slot")
        if slot_now is not None and slot is not None:
            try:
                if int(slot) == slot_now:
                    continue
            except (TypeError, ValueError):
                pass
        seen.add(mint)
        out.append(dict(item))
    return out


def recompute_dev_from_cached(row: dict) -> dict | None:
    """Rebuild launch/rug counts from stored prior_mints. No RPC.

    Returns None when pre-fixup launches were unknown (NULL). An empty
    prior_mints list is not evidence of 0.
    """
    if not row or row.get("status") not in (STATUS_OK, None):
        if row and row.get("status") != STATUS_OK:
            return None
    if row.get("dev_prior_launches") is None:
        return None
    extra = _parse_features_blob(row.get("features_json"))
    if "prior_mints" not in extra:
        return None
    mint, _net = parse_tid(row.get("tid"))
    create_slot = extra.get("create_slot")
    if create_slot is None:
        create_slot = row.get("create_slot")
    filtered = filter_stored_prior_mints(
        extra.get("prior_mints"), mint, create_slot,
    )
    n_launch = len(filtered)
    n_rug = sum(1 for p in filtered if p.get("rugged"))
    out = dict(row)
    out["dev_prior_launches"] = n_launch
    out["dev_prior_rugs"] = n_rug
    extra = dict(extra)
    extra["prior_mints"] = filtered[:8]
    out["features_json"] = extra
    flags = skip_flags({
        "status": STATUS_OK,
        "dev_prior_launches": n_launch,
        "dev_prior_rugs": n_rug,
        "sniper_share": row.get("sniper_share"),
        "sniper_wallets": row.get("sniper_wallets"),
        "sniper_min_wallets": row.get("sniper_min_wallets"),
        "bundled": row.get("bundled"),
    })
    out["skip_dev_rugs"] = flags["skip_dev_rugs"]
    return out


def _rec_dev_signed(rec: dict, dev_wallet: str | None) -> bool:
    if not dev_wallet:
        return False
    signers = rec.get("signers") or []
    return str(dev_wallet) in {str(s) for s in signers if s}


def _dev_from_recs(recs, current_mint: str, dev_wallet: str | None = None,
                   create_slot=None) -> dict:
    """Prior launches / rugs already sitting in compact tx recs. No RPC.

    A mint counts only when the rec has a pump/SPL create for it, the dev
    wallet signed, the mint is not wSOL/current, and the tx is not in the
    current mint's create slot.
    """
    launches: dict[str, dict] = {}
    rug_hits: list[tuple[str, object]] = []
    if isinstance(recs, dict):
        items = recs.values()
    else:
        items = recs or []
    slot_now = None
    if create_slot is not None:
        try:
            slot_now = int(create_slot)
        except (TypeError, ValueError):
            slot_now = None
    for rec in items:
        if not isinstance(rec, dict):
            continue
        slot = rec.get("slot")
        if slot_now is not None and slot is not None:
            try:
                if int(slot) == slot_now:
                    continue
            except (TypeError, ValueError):
                pass
        if dev_wallet and not _rec_dev_signed(rec, dev_wallet):
            continue
        for mint in rec.get("created_mints") or []:
            mint = str(mint)
            if not mint or mint == WSOL_MINT or mint == current_mint:
                continue
            launches.setdefault(mint, {"mint": mint, "slot": slot, "rugged": False})
        for mint in rec.get("rugged_mints") or []:
            mint = str(mint)
            if not mint or mint == WSOL_MINT or mint == current_mint:
                continue
            rug_hits.append((mint, slot))
    for mint, slot in rug_hits:
        if mint in launches:
            launches[mint]["rugged"] = True
    for mint, rec in list(launches.items()):
        labeled = _label_rugged(mint)
        if labeled:
            rec["rugged"] = True
    n_launch = len(launches)
    n_rug = sum(1 for r in launches.values() if r.get("rugged"))
    return {
        "dev_prior_launches": n_launch,
        "dev_prior_rugs": n_rug,
        "prior_mints": list(launches.values()),
    }


def _compact_dev(dev: dict) -> dict:
    return {
        "dev_prior_launches": dev.get("dev_prior_launches"),
        "dev_prior_rugs": dev.get("dev_prior_rugs"),
        "prior_mints": (dev.get("prior_mints") or [])[:8],
    }


def _dev_history(creator: str, current_mint: str,
                 now: float | None = None, recs=None, sigs=None,
                 create_slot=None) -> dict:
    """Prior launches by creator + cheap rug flags (LP pull / labels).

    Reuses creator-scan recs / cached history so a low remaining budget does
    not skip the feature. Unknown (None) only when we could not look.
    """
    unknown = {
        "dev_prior_launches": None, "dev_prior_rugs": None, "prior_mints": [],
    }
    if not creator:
        return unknown
    cached = _dev_cache.get(creator)
    if cached is not None:
        return dict(cached)

    from_recs = _dev_from_recs(
        recs, current_mint, dev_wallet=creator, create_slot=create_slot,
    )
    if from_recs["dev_prior_launches"] or from_recs["dev_prior_rugs"]:
        _dev_cache[creator] = _compact_dev(from_recs)
        log.info(
            "young_trail_rugrisk dev history src=recs launches=%s rugs=%s",
            from_recs["dev_prior_launches"], from_recs["dev_prior_rugs"],
        )
        return from_recs

    sig_rows = list(sigs) if sigs is not None else []
    rpc_sigs = False
    if sigs is None:
        if not _can_charge(1, now):
            log.warning("young_trail_rugrisk dev history skipped budget")
            return unknown
        try:
            sig_rows = [s for s in _signatures(creator, limit=DEV_SIG_LIMIT, now=now)
                        if not s.get("err")]
            rpc_sigs = True
        except Exception as e:
            log.warning("young_trail_rugrisk dev sigs: %s", safe_err(e))
            return unknown
    elif from_recs["dev_prior_launches"] is not None and not sig_rows:
        _dev_cache[creator] = _compact_dev(from_recs)
        log.info(
            "young_trail_rugrisk dev history src=recs launches=%s rugs=%s",
            from_recs["dev_prior_launches"], from_recs["dev_prior_rugs"],
        )
        return from_recs

    rec_map: dict = {}
    if isinstance(recs, dict):
        rec_map = dict(recs)
    need = []
    for row in sig_rows[: max(0, int(DEV_TX_CAP))]:
        sig = row.get("signature") if isinstance(row, dict) else None
        if not sig:
            continue
        if sig in rec_map and (
            rec_map[sig].get("created_mints") is not None
            or rec_map[sig].get("rugged_mints") is not None
        ):
            continue
        need.append(sig)
    rpc_txs = 0
    if need:
        if not _can_charge(1, now):
            log.warning(
                "young_trail_rugrisk dev history skipped budget txs need=%d",
                len(need),
            )
            if from_recs["dev_prior_launches"] is not None:
                return from_recs
            return unknown
        try:
            fetched = _transactions(need, now=now)
            rpc_txs = 1
        except Exception as e:
            if "budget" in str(e).lower():
                log.warning("young_trail_rugrisk dev history skipped budget")
                return unknown
            log.warning("young_trail_rugrisk dev tx: %s", safe_err(e))
            fetched = {}
        for sig in need:
            rec_map[sig] = _tx_record(sig, fetched.get(sig), current_mint)

    merged = _dev_from_recs(
        rec_map, current_mint, dev_wallet=creator, create_slot=create_slot,
    )
    _dev_cache[creator] = _compact_dev(merged)
    log.info(
        "young_trail_rugrisk dev history src=rpc sigs=%d rpc_sigs=%s rpc_txs=%d "
        "launches=%s rugs=%s",
        len(sig_rows), int(rpc_sigs), rpc_txs,
        merged["dev_prior_launches"], merged["dev_prior_rugs"],
    )
    return merged


def _blank_features(status: str, creator: str | None = None, **extra) -> dict:
    feat = {
        "status": status,
        "sniper_share": None,
        "sniper_wallets": None,
        "sniper_slots": sniper_slots(),
        "sniper_denominator": extra.get("supply"),
        "sniper_min_wallets": sniper_min_wallets(),
        "bundled": None,
        "bundled_buys": None,
        "dev_wallet": creator,
        "dev_prior_launches": extra.get("dev_prior_launches"),
        "dev_prior_rugs": extra.get("dev_prior_rugs"),
        "dev_initial_share": None,
        "fresh_holder_share": extra.get("fresh_holder_share"),
        "top_holder_n": extra.get("top_holder_n"),
        "create_slot": extra.get("create_slot"),
        "history_truncated": extra.get("history_truncated", False),
        "supply": extra.get("supply"),
        "window_capped": extra.get("window_capped"),
        "error": extra.get("error"),
        "reason": extra.get("reason"),
    }
    feat.update(skip_flags(feat))
    return feat


def compute_features(mint: str, creator: str | None = None,
                     now: float | None = None, progress: dict | None = None) -> dict:
    """Entry-time rug-risk features via budgeted RPC. Resumable. Mock-friendly.

    Creators are the known FOMO/Helius wallet plus signers of the tx that
    contains the mint create instruction — not every create-slot buyer.
    """
    ts = current_time() if now is None else float(now)
    if not mint:
        return _blank_features(STATUS_UNAVAILABLE, creator, error="no mint")
    prog = dict(progress or {})
    start_calls = _cycle_calls
    fresh_start = not prog

    def calls_used() -> int:
        return int(prog.get("calls_used") or 0) + max(0, _cycle_calls - start_calls)

    def pending():
        blob = dict(prog)
        blob["calls_used"] = calls_used()
        feat = _blank_features(STATUS_PENDING, creator, supply=blob.get("supply"),
                               top_holder_n=len(blob.get("top") or []),
                               create_slot=blob.get("create_slot"),
                               window_capped=blob.get("window_capped"))
        feat["features_json"] = {
            "progress": blob,
            "calls_used": blob["calls_used"],
            "window_capped": blob.get("window_capped"),
            "history_truncated": False,
        }
        return feat

    try:
        if fresh_start and not _can_charge(START_CALLS, ts):
            return _blank_features(STATUS_BUDGET, creator, error="budget")
        if (not fresh_start) and not _can_charge(1, ts):
            return pending()

        if "supply" not in prog:
            prog["supply"] = _supply(mint, now=ts)

        def load_holders() -> None:
            if prog.get("holders_tried"):
                return
            if not _can_charge(2, ts):
                return
            largest = _largest_accounts(mint, now=ts)
            addrs = [a["address"] for a in largest]
            owners = _owners_for(addrs, now=ts) if addrs and _can_charge(1, ts) else {}
            holders_out = []
            for acc in largest:
                meta = owners.get(acc["address"]) or {}
                owner = meta.get("owner")
                program = meta.get("program")
                is_pool = bool(program and _is_amm(program))
                holders_out.append({
                    "account": acc["address"],
                    "owner": owner,
                    "amount": acc["amount"],
                    "is_pool": is_pool,
                })
            holders_out.sort(key=lambda h: h["amount"], reverse=True)
            prog["holders"] = holders_out
            prog["top"] = [h for h in holders_out if not h.get("is_pool")][:TOP_HOLDERS]
            prog["holders_tried"] = True

        supply = prog.get("supply")
        holders = prog.get("holders") or []
        top = prog.get("top") or []

        if not prog.get("window_done"):
            found = _fill_create_window(mint, creator, holders, ts, prog)
            if found == "pending":
                return pending()
            if found != "ok" and not prog.get("holders_tried"):
                mint_sigs = prog.get("mint_sigs") or []
                hot = len(mint_sigs) >= max(1, min(int(SIG_PAGE_SIZE), 1000))
                if not hot:
                    load_holders()
                    holders = prog.get("holders") or []
                    top = prog.get("top") or []
                    found = _fill_create_window(mint, creator, holders, ts, prog)
                    if found == "pending":
                        return pending()
            if found != "ok":
                used = calls_used()
                feat = _blank_features(
                    STATUS_UNAVAILABLE, creator, error="history_truncated",
                    reason="history_truncated", history_truncated=True,
                    supply=supply, top_holder_n=len(top),
                )
                feat["features_json"] = {
                    "create_slot": None,
                    "history_truncated": True,
                    "window_src": prog.get("window_src"),
                    "sig_pages": prog.get("sig_pages"),
                    "sig_n": len(prog.get("mint_sigs") or []),
                    "calls_used": used,
                }
                return feat

        mint_sigs = prog.get("mint_sigs") or []
        create_slot = prog.get("create_slot")
        create_sig = prog.get("create_sig")
        tx_recs: dict = dict(prog.get("tx_recs") or {})
        to_fetch = list(prog.get("to_fetch") or [])
        if create_slot is None or not create_sig:
            used = calls_used()
            feat = _blank_features(
                STATUS_UNAVAILABLE, creator, error="history_truncated",
                reason="history_truncated", history_truncated=True,
                supply=supply, top_holder_n=len(top),
            )
            feat["features_json"] = {"calls_used": used, "history_truncated": True}
            return feat

        buys: list[dict] = []
        for sig in to_fetch:
            buys.extend((tx_recs.get(sig) or {}).get("buyers") or [])
        # Known FOMO/Helius creator + signers of the CREATE_IX tx only.
        # Bundled same-slot buyers are not creators.
        creators = _owner_set([creator] if creator else [])
        create_signers: list[str] = []
        if create_sig:
            rec = tx_recs.get(create_sig) or {}
            if rec.get("has_create_ix"):
                create_signers = [
                    str(s) for s in (rec.get("signers") or []) if s
                ]
                creators.update(create_signers)
        prog["buys"] = buys
        prog["creators"] = sorted(creators)
        exclude = sniper_exclude_owners(mint, creators, holders)

        # Dev wallet is the FOMO/Helius creator, else the create-tx fee payer
        # (first signer). Only skip the lookup when no wallet exists at all.
        dev_wallet = creator or (create_signers[0] if create_signers else None)
        dev = prog.get("dev") or {
            "dev_prior_launches": None, "dev_prior_rugs": None, "prior_mints": [],
        }
        if not dev_wallet:
            prog["dev_done"] = True
        elif not prog.get("dev_done"):
            cached = _dev_cache.get(dev_wallet)
            if cached is not None:
                dev = dict(cached)
                prog["dev"] = _compact_dev(dev)
                prog["dev_done"] = True
            elif calls_used() >= TARGET_ENTRY_CALLS:
                from_recs = _dev_from_recs(
                    tx_recs, mint, dev_wallet=dev_wallet, create_slot=create_slot,
                )
                if from_recs["dev_prior_launches"] or from_recs["dev_prior_rugs"]:
                    dev = from_recs
                    prog["dev"] = _compact_dev(dev)
                    _dev_cache[dev_wallet] = _compact_dev(dev)
                    prog["dev_done"] = True
                else:
                    log.warning("young_trail_rugrisk dev history skipped budget")
                    prog["dev_done"] = True
            else:
                reuse_sigs = prog.get("creator_sigs")
                if not creator and reuse_sigs is None:
                    reuse_sigs = None
                dev = _dev_history(
                    dev_wallet, mint, now=ts,
                    recs=tx_recs, sigs=reuse_sigs,
                    create_slot=create_slot,
                )
                prog["dev"] = _compact_dev(dev)
                if dev.get("dev_prior_launches") is None:
                    log.warning(
                        "young_trail_rugrisk dev history unknown mint=%s",
                        (mint or "")[:12],
                    )
                    # Do not mark done: retry when budget recovers.
                    if not _can_charge(1, ts):
                        return pending()
                    if calls_used() >= TARGET_ENTRY_CALLS:
                        log.warning(
                            "young_trail_rugrisk dev history skipped budget"
                        )
                        prog["dev_done"] = True
                else:
                    prog["dev_done"] = True
        dev = prog.get("dev") or dev

        # Fresh-wallet share is diagnostic, not a skip rule. Reuse cache; do
        # not spend RPC once sniper/bundled/dev are determined.
        fresh_owners = set(prog.get("fresh_owners") or [])
        checked_owners = list(prog.get("fresh_checked_owners") or [])
        checked_set = set(checked_owners)
        cap_fresh = max(0, int(FRESH_WALLET_RPC_CAP))
        if cap_fresh > 0:
            candidates = []
            seen = set()
            for b in buys:
                if create_slot is not None and b.get("slot") == create_slot:
                    o = b.get("owner")
                    if o and o not in seen and str(o) not in creators:
                        seen.add(o)
                        candidates.append(o)
            for h in top:
                o = h.get("owner")
                if o and o not in seen and str(o) not in creators:
                    seen.add(o)
                    candidates.append(o)
            for owner in candidates:
                if owner in checked_set:
                    continue
                cached_fresh = _wallet_fresh_cache.get(owner)
                if cached_fresh is not None:
                    checked_owners.append(owner)
                    checked_set.add(owner)
                    if cached_fresh:
                        fresh_owners.add(owner)
                    continue
                if len(checked_owners) >= cap_fresh:
                    break
                if calls_used() >= TARGET_ENTRY_CALLS or not _can_charge(1, ts):
                    break
                try:
                    wsigs = [s for s in _signatures(owner, limit=WALLET_SIG_LIMIT, now=ts)
                             if not s.get("err")]
                except Exception as e:
                    log.warning("young_trail_rugrisk wallet sigs: %s", safe_err(e))
                    checked_owners.append(owner)
                    checked_set.add(owner)
                    continue
                is_fresh = len(wsigs) <= FRESH_MAX_SIGS
                _wallet_fresh_cache[owner] = is_fresh
                checked_owners.append(owner)
                checked_set.add(owner)
                if is_fresh:
                    fresh_owners.add(owner)
        prog["fresh_owners"] = sorted(fresh_owners)
        prog["fresh_checked_owners"] = checked_owners
        prog["enrich_done"] = True

        sniper = sniper_share_from_buys(
            buys, supply or 0.0, create_slot, sniper_slots(), exclude=exclude,
        )
        bundled = bundled_from_buys(
            buys, create_slot, fresh_owners, exclude=exclude,
        )
        fresh = fresh_holder_share(top, fresh_owners)
        dev_share = dev_initial_share_from_buys(
            buys, supply or 0.0, create_slot, creators,
        )
        used = calls_used()
        compact_buys = _slim_sniper_buys(buys, create_slot, sniper_slots())
        features = {
            "status": STATUS_OK,
            "sniper_share": sniper["sniper_share"],
            "sniper_wallets": sniper["sniper_wallets"],
            "sniper_slots": sniper.get("sniper_slots", sniper_slots()),
            "sniper_denominator": sniper.get("sniper_denominator") or supply,
            "sniper_min_wallets": sniper_min_wallets(),
            "bundled": bundled["bundled"],
            "bundled_buys": bundled["bundled_buys"],
            "dev_wallet": dev_wallet or (next(iter(creators), None)),
            "dev_prior_launches": dev.get("dev_prior_launches"),
            "dev_prior_rugs": dev.get("dev_prior_rugs"),
            "dev_initial_share": dev_share,
            "fresh_holder_share": fresh["fresh_holder_share"],
            "top_holder_n": fresh["top_holder_n"],
            "create_slot": create_slot,
            "history_truncated": False,
            "window_capped": bool(prog.get("window_capped")),
            "supply": supply,
        }
        flags = skip_flags(features)
        features.update(flags)
        features["features_json"] = {
            "create_slot": create_slot,
            "history_truncated": False,
            "sig_pages": prog.get("sig_pages"),
            "sig_n": len(mint_sigs),
            "sniper_amount": sniper.get("sniper_amount"),
            "sniper_denominator": sniper.get("sniper_denominator") or supply,
            "sniper_min_wallets": sniper_min_wallets(),
            "sniper_slots": sniper.get("sniper_slots", sniper_slots()),
            "create_slot_buyers": bundled.get("create_slot_buyers"),
            "create_slot_fresh": bundled.get("create_slot_fresh"),
            "creators": sorted(creators),
            "fresh_checked": len(checked_owners),
            "prior_mints": (dev.get("prior_mints") or [])[:8],
            "dev_initial_share": dev_share,
            "window_capped": bool(prog.get("window_capped")),
            "calls_used": used,
            "create_sig": create_sig,
            "window_src": prog.get("window_src"),
            "supply": supply,
            "buys": compact_buys,
            "sniper_v2": SNIPER_V2_OK,
        }
        return features
    except Exception as e:
        msg = safe_err(e)
        if "budget" in str(e).lower() or "budget" in msg.lower():
            return pending()
        log.warning("young_trail_rugrisk features %s: %s", mint, msg)
        if _is_transient_rpc_error(e):
            feat = pending()
            extra = feat.get("features_json") or {}
            retry_at = ts + RETRY_AFTER_SEC
            extra["retry_after"] = retry_at
            blob = extra.get("progress") if isinstance(extra.get("progress"), dict) else {}
            blob["retry_after"] = retry_at
            extra["progress"] = blob
            extra["last_error"] = msg
            extra["calls_used"] = calls_used()
            feat["features_json"] = extra
            return feat
        feat = _blank_features(STATUS_ERROR, creator, error=msg)
        feat["features_json"] = {"calls_used": calls_used()}
        return feat


def _is_amm(program: str | None) -> bool:
    if not program:
        return False
    # Copied, not imported — collect.py stays untouched.
    amms = {
        "pAMMBay6oceH9fJKBRHGP5D4bD4sWpmSwMn52FMfXEA",
        "6EF8rrecthR5Dkzon8Nwu78hRvfCKubJ14M5uBEwF6P",
        "LBUZKhRxPF3XUpBCjp4YzTKgLccjZhTSDM9YuVaPwxo",
        "Eo7WjKq67rjJQSZxS6z3YkapzY3eMj6Xy8X5EQVn5UaB",
        "cpamdpZCGKUy5JxQXB4dcpGPiikHawvSWAd6mEn1sGG",
        "675kPX9MHTjS2zt1qfr1NYHuzeLXfQM9H24wFSUt1Mp8",
        "CPMMoo8L3F4NbTegBCKVNunggL7H1ZpdTHKxQB5qKP1C",
        "CAMMCzo5YL8w4VFF8KVHrK22GGUsp5VTaW7grrKgrWqK",
        "whirLbMiicVdio4qvUfM5KAg6Ct8VwpYzGff3uctyCc",
        "LanMV9sAd7wArD4vJFi2qDdfnVhFxYSUg6eADduJ3uj",
    }
    return program in amms


# --- ledger ---------------------------------------------------------------

def _source_entries() -> list[dict]:
    try:
        import young_trail
        rows = list(young_trail.open_rows())
        rows.extend(young_trail.closed_rows(limit=None))
        return [r for r in rows if r.get("tid") and _num(r.get("entry_price"), 0) > 0]
    except Exception as e:
        log.warning("young_trail_rugrisk source entries: %s", safe_err(e))
        return []


def sync_entries(now: float | None = None) -> int:
    """Copy every young_trail tid (including pre-deploy, for overnight backfill).

    Non-Solana rows are stored as status='n/a' with no RPC. Fail open.
    """
    if not enabled():
        return 0
    ts = current_time() if now is None else float(now)
    if ts >= until_ts(ts):
        return 0
    global _last_new_tids
    n = 0
    new_tids: list[str] = []
    try:
        db = _connect()
        for src in _source_entries():
            entry_ts = _num(src.get("entry_ts"))
            if entry_ts is None:
                continue
            sol = is_solana(src.get("tid"), src.get("chain"))
            status = STATUS_PENDING if sol else STATUS_NA
            cur = db.execute(
                """INSERT OR IGNORE INTO young_trail_rugrisk (
                     tid, ticker, chain, entry_ts, entry_price, status, source
                   ) VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (
                    src["tid"], src.get("ticker"),
                    src.get("chain") or ("solana" if sol else src.get("chain")),
                    float(entry_ts), float(src["entry_price"]),
                    status, "sync",
                ),
            )
            if cur.rowcount:
                n += 1
                new_tids.append(src["tid"])
        db.commit()
        _last_new_tids = new_tids
        if n:
            log.info("young_trail_rugrisk sync entries=%d paper=1", n)
    except Exception as e:
        log.warning("young_trail_rugrisk sync: %s", safe_err(e))
        _last_new_tids = new_tids
    return n


def compute_devhist_only(row, now: float | None = None) -> dict:
    """Fill dev_prior_* from RPC (~2 calls). Does not recompute sniper/bundled."""
    ts = current_time() if now is None else float(now)
    d = _row_dict(row) if not isinstance(row, dict) else dict(row)
    extra = _parse_features_blob(d.get("features_json"))
    mint, _net = parse_tid(d.get("tid"))
    creator = d.get("dev_wallet") or _lookup_creator(d.get("tid") or "")
    if not creator:
        creators = extra.get("creators") or []
        if creators:
            creator = str(creators[0])
    unknown = {
        "status": STATUS_OK,
        "dev_wallet": creator,
        "dev_prior_launches": None,
        "dev_prior_rugs": None,
        "prior_mints": [],
        "skip_dev_rugs": None,
        "features_json": extra,
    }
    if not creator or not mint:
        return unknown
    recs = None
    sigs = None
    prog = extra.get("progress")
    if isinstance(prog, dict):
        if prog.get("tx_recs"):
            recs = prog.get("tx_recs")
        if prog.get("creator_sigs") is not None:
            sigs = prog.get("creator_sigs")
    create_slot = extra.get("create_slot")
    if create_slot is None:
        create_slot = d.get("create_slot")
    try:
        dev = _dev_history(
            creator, mint, now=ts, recs=recs, sigs=sigs, create_slot=create_slot,
        )
    except Exception as e:
        msg = safe_err(e)
        if "budget" in str(e).lower() or "budget" in msg.lower():
            return {"status": STATUS_BUDGET, "features_json": extra}
        if _is_transient_rpc_error(e):
            extra = dict(extra)
            extra["retry_after"] = ts + RETRY_AFTER_SEC
            extra["last_error"] = msg
            return {
                "status": STATUS_PENDING,
                "dev_wallet": creator,
                "dev_prior_launches": None,
                "dev_prior_rugs": None,
                "skip_dev_rugs": None,
                "features_json": extra,
            }
        log.warning("young_trail_rugrisk devhist %s: %s", (mint or "")[:12], msg)
        return unknown
    if dev.get("dev_prior_launches") is None:
        extra = dict(extra)
        return {
            "status": STATUS_PENDING,
            "dev_wallet": creator,
            "dev_prior_launches": None,
            "dev_prior_rugs": None,
            "skip_dev_rugs": None,
            "features_json": extra,
        }
    extra = dict(extra)
    extra["prior_mints"] = (dev.get("prior_mints") or [])[:8]
    flags = skip_flags({
        "status": STATUS_OK,
        "dev_prior_launches": dev.get("dev_prior_launches"),
        "dev_prior_rugs": dev.get("dev_prior_rugs"),
        "sniper_share": d.get("sniper_share"),
        "sniper_wallets": d.get("sniper_wallets"),
        "sniper_min_wallets": d.get("sniper_min_wallets"),
        "bundled": d.get("bundled"),
    })
    return {
        "status": STATUS_OK,
        "dev_wallet": creator,
        "dev_prior_launches": dev.get("dev_prior_launches"),
        "dev_prior_rugs": dev.get("dev_prior_rugs"),
        "prior_mints": extra["prior_mints"],
        "skip_dev_rugs": flags["skip_dev_rugs"],
        "features_json": extra,
    }


def _store_devhist(tid: str, feat: dict, now: float, source: str) -> None:
    """Write only dev-history columns. Sniper / bundled fields stay as they are."""
    extra = feat.get("features_json")
    extra_s = json.dumps(extra) if extra is not None else None
    skip = feat.get("skip_dev_rugs")
    db = _connect()
    db.execute(
        """UPDATE young_trail_rugrisk SET
             status = ?,
             dev_wallet = ?,
             dev_prior_launches = ?,
             dev_prior_rugs = ?,
             skip_dev_rugs = ?,
             features_json = ?,
             error = NULL,
             source = ?,
             computed_ts = ?
           WHERE tid = ?""",
        (
            feat.get("status") or STATUS_OK,
            feat.get("dev_wallet"),
            feat.get("dev_prior_launches"),
            feat.get("dev_prior_rugs"),
            None if skip is None else int(bool(skip)),
            extra_s,
            source,
            float(now),
            tid,
        ),
    )
    db.commit()


def _store_features(tid: str, feat: dict, now: float, source: str) -> None:
    flags = skip_flags(feat)
    extra = feat.get("features_json")
    extra_s = json.dumps(extra) if extra is not None else None
    err = feat.get("error")
    if err is not None:
        err = str(err)[:400]
    db = _connect()
    db.execute(
        """UPDATE young_trail_rugrisk SET
             status = ?, sniper_share = ?, sniper_wallets = ?, sniper_slots = ?,
             bundled = ?, bundled_buys = ?, dev_wallet = ?,
             dev_prior_launches = ?, dev_prior_rugs = ?,
             fresh_holder_share = ?, top_holder_n = ?,
             skip_snipers = ?, skip_bundled = ?, skip_dev_rugs = ?,
             features_json = ?, error = ?, source = ?, computed_ts = ?,
             dev_initial_share = ?,
             sniper_denominator = ?, sniper_min_wallets = ?
           WHERE tid = ?""",
        (
            feat.get("status") or STATUS_ERROR,
            feat.get("sniper_share"), feat.get("sniper_wallets"),
            feat.get("sniper_slots"),
            None if feat.get("bundled") is None else int(bool(feat.get("bundled"))),
            feat.get("bundled_buys"), feat.get("dev_wallet"),
            feat.get("dev_prior_launches"), feat.get("dev_prior_rugs"),
            feat.get("fresh_holder_share"), feat.get("top_holder_n"),
            None if flags["skip_snipers"] is None else int(flags["skip_snipers"]),
            None if flags["skip_bundled"] is None else int(flags["skip_bundled"]),
            None if flags["skip_dev_rugs"] is None else int(flags["skip_dev_rugs"]),
            extra_s, err, source, float(now),
            feat.get("dev_initial_share"),
            feat.get("sniper_denominator"), feat.get("sniper_min_wallets"),
            tid,
        ),
    )
    db.commit()


def _progress_from_row(row) -> dict | None:
    raw = None
    try:
        raw = row["features_json"]
    except Exception:
        return None
    if not raw:
        return None
    try:
        blob = json.loads(raw) if isinstance(raw, str) else raw
    except (TypeError, ValueError):
        return None
    if not isinstance(blob, dict):
        return None
    prog = blob.get("progress")
    return dict(prog) if isinstance(prog, dict) else None


def _row_get(row, key, default=None):
    try:
        if isinstance(row, dict):
            return row.get(key, default)
        return row[key]
    except Exception:
        return default


def _is_sniper_v2_row(row) -> bool:
    src = _row_get(row, "source")
    if src == SOURCE_SNIPER_V2:
        return True
    extra = _parse_features_blob(_row_get(row, "features_json"))
    return extra.get("sniper_v2") == SNIPER_V2_NEEDS


def _is_v1tx_row(row) -> bool:
    return _row_get(row, "source") == SOURCE_V1TX


def _is_devhist_row(row) -> bool:
    return _row_get(row, "source") == SOURCE_DEVHIST


def _is_overnight_backlog_row(row) -> bool:
    return _is_sniper_v2_row(row) or _is_v1tx_row(row) or _is_devhist_row(row)


def _row_retry_after(row) -> float | None:
    extra = _parse_features_blob(_row_get(row, "features_json"))
    raw = extra.get("retry_after")
    if raw is None and isinstance(extra.get("progress"), dict):
        raw = extra["progress"].get("retry_after")
    try:
        return float(raw) if raw is not None else None
    except (TypeError, ValueError):
        return None


def _fair_share_pending(rows, priority_tids: list[str] | None, cap: int,
                        overnight: bool = False):
    """Newest-first. New entries first, then overnight backlog ≥ half of the rest.

    sniper_v2 / v1tx rows are overnight-only (or catch-up) and sit behind new
    entries. After new entries they take at least half of the remaining cap.
    """
    cap = max(0, int(cap))
    if cap <= 0:
        return []
    pset = set(priority_tids or [])
    newest_new = []
    newest_old = []
    backlog = []
    seen: set[str] = set()
    for row in rows:
        tid = row["tid"]
        if tid in seen:
            continue
        seen.add(tid)
        if tid in pset:
            newest_new.append(row)
        elif _is_overnight_backlog_row(row):
            if overnight:
                backlog.append(row)
        else:
            newest_old.append(row)
    take_new = min(len(newest_new), (cap + 1) // 2)
    remaining = cap - take_new
    take_backlog = min(len(backlog), (remaining + 1) // 2) if overnight else 0
    take_old = min(len(newest_old), remaining - take_backlog)
    leftover = remaining - take_backlog - take_old
    if leftover and overnight and len(backlog) > take_backlog:
        extra = min(leftover, len(backlog) - take_backlog)
        take_backlog += extra
        leftover -= extra
    if leftover and len(newest_old) > take_old:
        extra = min(leftover, len(newest_old) - take_old)
        take_old += extra
        leftover -= extra
    if leftover and len(newest_new) > take_new:
        extra = min(leftover, len(newest_new) - take_new)
        take_new += extra
    return (
        newest_new[:take_new] + backlog[:take_backlog] + newest_old[:take_old]
    )


def _backlog_catchup(now: float | None = None) -> bool:
    """True when overnight has not run in 24h and >50% of the day cap is free."""
    ts = current_time() if now is None else float(now)
    raw = _meta_get(META_LAST_OVERNIGHT)
    try:
        last = float(raw) if raw not in (None, "") else 0.0
    except (TypeError, ValueError):
        last = 0.0
    if ts - last < 24 * 3600:
        return False
    cap = max(0, day_cap())
    if cap <= 0:
        return False
    return _day_used(ts) < 0.5 * cap


def _mark_overnight_run(now: float | None = None) -> None:
    ts = current_time() if now is None else float(now)
    _meta_set(META_LAST_OVERNIGHT, str(ts))


def _log_backlog_hourly(now: float | None = None) -> None:
    ts = current_time() if now is None else float(now)
    if not is_overnight(ts):
        return
    hour_key = time.strftime("%Y-%m-%dT%H", time.gmtime(ts))
    if _meta_get(META_BACKLOG_LOG_HOUR) == hour_key:
        return
    try:
        rows = pending_rows()
    except Exception:
        rows = []
    n_v2 = sum(1 for r in rows if _is_sniper_v2_row(r))
    n_v1 = sum(1 for r in rows if _is_v1tx_row(r))
    log.info(
        "young_trail_rugrisk backlog sniper_v2=%d v1tx=%d pending",
        n_v2, n_v1,
    )
    _meta_set(META_BACKLOG_LOG_HOUR, hour_key)


def compute_pending(now: float | None = None, limit: int | None = None,
                    source: str = "live",
                    priority_tids: list[str] | None = None,
                    include_sniper_v2: bool | None = None) -> int:
    """Compute features for pending Solana rows. Newest, fair-shared with new tids."""
    ts = current_time() if now is None else float(now)
    n = 0
    try:
        if not helius_allows(ts):
            return 0
        db = _connect()
        rows = db.execute(
            """SELECT * FROM young_trail_rugrisk
               WHERE status = 'pending'
               ORDER BY entry_ts DESC""",
        ).fetchall()
        ready = []
        for row in rows:
            retry_at = _row_retry_after(row)
            if retry_at is not None and retry_at > ts:
                continue
            ready.append(row)
        cap = limit if limit is not None else tick_entry_cap(ts)
        if include_sniper_v2 is None:
            allow_backlog = is_overnight(ts) or _backlog_catchup(ts)
        else:
            allow_backlog = bool(include_sniper_v2)
        ordered = _fair_share_pending(
            ready, priority_tids, cap, overnight=allow_backlog,
        )
        if not ordered:
            return 0
        if not _rpc_reachable(ts):
            return 0
        for row in ordered:
            tid = row["tid"]
            if _is_devhist_row(row):
                need = 2
                if not _can_charge(need, ts):
                    break
                feat = compute_devhist_only(row, ts)
                if feat.get("status") == STATUS_BUDGET:
                    break
                _store_devhist(tid, feat, ts, SOURCE_DEVHIST)
                n += 1
                continue
            need = 1 if _progress_from_row(row) else START_CALLS
            if not _can_charge(need, ts):
                break
            mint, _net = parse_tid(tid)
            creator = _lookup_creator(tid)
            feat = compute_features(
                mint or "", creator=creator, now=ts,
                progress=_progress_from_row(row),
            )
            if feat.get("status") == STATUS_BUDGET:
                break
            store_src = source
            if feat.get("status") == STATUS_PENDING:
                prev = _row_get(row, "source")
                if prev in (SOURCE_SNIPER_V2, SOURCE_V1TX, SOURCE_DEVHIST):
                    store_src = prev
            _store_features(tid, feat, ts, store_src)
            n += 1
    except Exception as e:
        log.warning("young_trail_rugrisk compute: %s", safe_err(e))
    return n


def requeue_sniper_v2_rows(now: float | None = None, force: bool = False) -> int:
    """Overnight: mark needs_recompute ok rows pending. No RPC."""
    ts = current_time() if now is None else float(now)
    if not force and not is_overnight(ts):
        return 0
    n = 0
    try:
        db = _connect()
        rows = db.execute(
            "SELECT tid, features_json, status FROM young_trail_rugrisk "
            "WHERE status = 'ok'"
        ).fetchall()
        for raw in rows:
            extra = _parse_features_blob(raw["features_json"])
            if extra.get("sniper_v2") != SNIPER_V2_NEEDS:
                continue
            db.execute(
                """UPDATE young_trail_rugrisk SET
                     status = 'pending',
                     sniper_share = NULL, skip_snipers = NULL,
                     error = NULL, computed_ts = NULL,
                     source = ?
                   WHERE tid = ? AND status = 'ok'""",
                (SOURCE_SNIPER_V2, raw["tid"]),
            )
            n += 1
        db.commit()
        if n:
            log.info("young_trail_rugrisk sniper_v2 requeue n=%d paper=1", n)
    except Exception as e:
        log.warning("young_trail_rugrisk sniper_v2 requeue: %s", safe_err(e))
    return n


def backfill_pending(now: float | None = None, limit: int | None = None,
                     force: bool = False) -> int:
    """Overnight (or CLI) backfill of existing pending entries. Budget-limited."""
    ts = current_time() if now is None else float(now)
    if not enabled():
        return 0
    if ts >= until_ts(ts) and not force:
        return 0
    if not force and not is_overnight(ts):
        return 0
    sync_entries(ts)
    requeue_sniper_v2_rows(now=ts, force=force)
    cap = limit if limit is not None else 12
    n = compute_pending(
        now=ts, limit=cap, source="backfill", include_sniper_v2=True,
    )
    if n:
        log.info("young_trail_rugrisk backfill n=%d paper=1", n)
    return n


def on_sampler_tick(prices: dict | None = None, now: float | None = None) -> dict:
    """Sampler hook AFTER helius. Copies entries, computes a few features.

    `prices` is ignored (entry-time on-chain features). Fail open. Never gates.
    """
    empty = {"entries": 0, "computed": 0, "backfill": 0, "overnight": False}
    try:
        if not enabled():
            return empty
        ts = current_time() if now is None else float(now)
        begin_cycle(ts)
        until_ts(ts)
        deploy_ts()
        n = sync_entries(ts)
        new_tids = list(_last_new_tids)
        overnight = is_overnight(ts)
        if overnight:
            requeue_sniper_v2_rows(now=ts)
            _mark_overnight_run(ts)
            _log_backlog_hourly(ts)
        cap = tick_entry_cap(ts)
        src = "live"
        computed = compute_pending(
            now=ts, limit=cap, source=src, priority_tids=new_tids,
        )
        return {
            "entries": n, "computed": computed,
            "backfill": 0 if not overnight else max(0, computed - len(new_tids)),
            "overnight": overnight,
        }
    except Exception as e:
        log.warning("young_trail_rugrisk sampler: %s", safe_err(e))
        return empty


def all_rows() -> list[dict]:
    try:
        return [_row_dict(r) for r in _connect().execute(
            "SELECT * FROM young_trail_rugrisk ORDER BY entry_ts DESC"
        ).fetchall()]
    except Exception as e:
        log.warning("young_trail_rugrisk rows: %s", safe_err(e))
        return []


def pending_rows() -> list[dict]:
    try:
        return [_row_dict(r) for r in _connect().execute(
            "SELECT * FROM young_trail_rugrisk WHERE status = 'pending' ORDER BY entry_ts"
        ).fetchall()]
    except Exception as e:
        log.warning("young_trail_rugrisk pending: %s", safe_err(e))
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
    maps = {
        "young_trail": {},
        "fast_baseline": {},
        "fast_tp50": {},
    }
    try:
        import young_trail
        maps["young_trail"] = _counted_pnl(
            young_trail.open_rows() + young_trail.closed_rows(limit=None)
        )
    except Exception as e:
        log.warning("young_trail_rugrisk cycle pnl: %s", safe_err(e))
    try:
        import young_trail_fast as fast
        maps["fast_baseline"] = _counted_pnl(
            fast.open_rows("baseline") + fast.closed_rows("baseline", limit=None)
        )
        maps["fast_tp50"] = _counted_pnl(
            fast.open_rows("tp_50") + fast.closed_rows("tp_50", limit=None)
        )
    except Exception as e:
        log.warning("young_trail_rugrisk fast pnl: %s", safe_err(e))
    return maps


def counterfactuals(rows: list[dict] | None = None) -> list[dict]:
    """Per-rule SKIPPED vs KEPT P&L on young_trail / fast baseline / fast tp_50."""
    try:
        data = rows if rows is not None else all_rows()
        maps = _pnl_maps()
        return [counterfactual_for_rule(rule["id"], data, maps) for rule in SKIP_RULES]
    except Exception as e:
        log.warning("young_trail_rugrisk counterfactual: %s", safe_err(e))
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
            "would_skip_snipers": sum(1 for r in rows if r.get("skip_snipers") == 1),
            "would_skip_bundled": sum(1 for r in rows if r.get("skip_bundled") == 1),
            "would_skip_dev_rugs": sum(1 for r in rows if r.get("skip_dev_rugs") == 1),
        }
    except Exception as e:
        log.warning("young_trail_rugrisk summary: %s", safe_err(e))
        return {
            "n": 0, "ok": 0, "n_a": 0, "pending": 0, "error": 0,
            "unavailable": 0,
            "would_skip_snipers": 0, "would_skip_bundled": 0,
            "would_skip_dev_rugs": 0,
        }


def ops_payload() -> dict:
    """JSON for GET /ops/young_trail_rugrisk. Paper/simulated; no live orders."""
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
            "until_ts": until,
            "until_iso": (
                time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(until))
                if until else None
            ),
            "ticket_usd": TICKET_USD,
            "caveat": (
                "Paper rug-risk features on young_trail entries. Skip rules are "
                "counterfactual only — they do not gate entries, picks, or the "
                "judge. Budgeted Helius RPC; helius ledger keeps first claim on "
                "the day cap. Non-Solana is n/a."
            ),
            "rules": [
                {
                    "id": r["id"], "label": r["label"],
                    "threshold": r["threshold"] if r["id"] != RULE_SNIPERS
                    else sniper_skip_pct(),
                    **({"min_wallets": sniper_min_wallets()}
                       if r["id"] == RULE_SNIPERS else {}),
                }
                for r in SKIP_RULES
            ],
            "budget": budget_remaining(now),
            "helius_day_left": _helius_day_left(now),
            "summary": _summary(),
            "counterfactuals": counterfactuals(rows),
            "entries": rows[:80],
        }
    except Exception as e:
        log.warning("young_trail_rugrisk ops: %s", safe_err(e))
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
        description="Paper young_trail rug-risk features (no gating, no live orders)",
    )
    sub = ap.add_subparsers(dest="cmd", required=True)
    p_bf = sub.add_parser("backfill", help="Budget-limited backfill of pending entries")
    p_bf.add_argument("--limit", type=int, default=12)
    sub.add_parser("ops", help="Print ops payload JSON")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "backfill":
            n = backfill_pending(limit=args.limit, force=True)
            print(json.dumps({"backfill": n, "paper": True}, default=str))
            return 0
        if args.cmd == "ops":
            print(json.dumps(ops_payload(), default=str, indent=2))
            return 0
    except Exception as e:
        log.error("young_trail_rugrisk cli: %s", safe_err(e))
        return 1
    return 0


if __name__ == "__main__":
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO"),
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )
    raise SystemExit(main())
