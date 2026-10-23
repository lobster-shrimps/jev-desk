"""
Shadow PnL Ledger — hypothetical tracking of what the desk would have traded.

Records entry, marks to market over time, and closes positions using the desk's exit rules.
Persists to outbox/shadow_ledger.jsonl (append-only, gitignored).

The owner's success metric is REALIZED PnL from FILLED CLOSES. Shadow results are
hypothetical and must be labeled as such everywhere.
"""
import calendar
import json
import logging
import os
import pathlib
import time

from secret_utils import safe_err

log = logging.getLogger("shadow_ledger")

OUTBOX = pathlib.Path(os.environ.get("DESK_OUTBOX", "outbox"))
LEDGER_PATH = OUTBOX / "shadow_ledger.jsonl"

# Hypothetical sizing (owner specified $50 for shadow)
SHADOW_TICKET_USD = 50.0

# Exit rule from prompts/RISK.txt: close when volume.h6 / (volume.h24 / 4) < 0.20
# NOTE: RISK's h6 volume exit is NOT modelled in shadow because real h6 volume
# isn't available from FOMO (only vol24), and computing it from rolling vol24 snapshots
# is backwards (closes on surges, not collapses). Shadow uses time/price exits only.

# Shadow ledger fallback exits (NOT trading thresholds, only for shadow tracking):
# These are conservative defaults to ensure shadow positions eventually close.
SHADOW_MAX_HOLD_HOURS = 6.0        # Max hold time before forced exit
SHADOW_STOP_LOSS_PCT = -30.0       # Stop loss at -30% (conservative)
SHADOW_TAKE_PROFIT_PCT = 100.0     # Legacy take-profit (+100%). Existing opens keep this.
SHADOW_TP50_PCT = 50.0             # New fills: same +50% take-profit as young_trail tp_50
EXIT_POLICY_TP50 = "tp_50"
EXIT_POLICY_LEGACY = "legacy"


def _as_price(v):
    if v is None or v == "":
        return None
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    return f if f > 0 else None


def _price_from(bag, keys) -> float | None:
    if not isinstance(bag, dict):
        return None
    for key in keys:
        px = _as_price(bag.get(key))
        if px:
            return px
    return None


def resolve_entry_price(order: dict | None, fomo_data: dict | None = None,
                        token_row: dict | None = None) -> tuple[float | None, str | None]:
    """Same chain as young_trail: FOMO/shortlist price_usd, dossier priceUsd, Dex fetch.

    One-off DexScreener lookup uses young_trail's 5s timeout. Fail open.
    """
    bags = (fomo_data, token_row, (order or {}).get("token"))
    for bag in bags:
        px = _price_from(bag, ("price_usd", "price"))
        if px:
            return px, "fomo"
    for bag in bags:
        px = _price_from(bag, ("priceUsd",))
        if px:
            return px, "dex"
    token = (order or {}).get("token") or {}
    addr = token.get("address") or token.get("addr")
    net = token.get("network_id") if token.get("network_id") is not None else token.get("net")
    tid = None
    if isinstance(token_row, dict) and token_row.get("tid"):
        tid = token_row["tid"]
    elif addr is not None and net is not None:
        tid = f"{addr}:{net}"
    if tid:
        try:
            import young_trail
            px = young_trail.fetch_dex_price(tid)
            if px:
                return float(px), "dexscreener"
        except Exception as e:
            log.warning("shadow dex price: %s", safe_err(e))
    return None, None


def entry(order: dict, fomo_data: dict = None, token_row: dict | None = None) -> dict:
    """
    Record a hypothetical entry when the desk would have traded in shadow mode.
    
    Args:
        order: The shadow order dict from pick/single-survivor
        fomo_data: Shortlist/FOMO row or {price, price_usd, ...}
        token_row: Optional dossier/shortlist token (Dex priceUsd, tid)
    
    Returns:
        Entry record dict
    """
    token = order["token"]
    entry_price, source = resolve_entry_price(order, fomo_data, token_row)
    
    if entry_price is None or entry_price <= 0:
        log.warning("cannot record shadow entry for %s: missing or invalid price", token["ticker"])
        return None
    
    # Calculate size in tokens based on hypothetical $50 USD and entry price
    size_tokens = SHADOW_TICKET_USD / entry_price
    
    entry_record = {
        "action": "entry",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time())),
        "ticker": token["ticker"],
        "address": token["address"],
        "network_id": token["network_id"],
        "chain": token["chain"],
        "entry_price_usd": entry_price,
        "last_price_usd": entry_price,  # For stale marking
        "size_usd": SHADOW_TICKET_USD,
        "size_tokens": size_tokens,
        "size_factor": order.get("size_factor", 1.0),
        "confidence": order.get("confidence"),
        "model": order.get("model"),
        "order_id": order.get("order_id"),
        "price_source": source,
        "exit_policy": EXIT_POLICY_TP50,
        "take_profit_pct": SHADOW_TP50_PCT,
        "legacy_take_profit_pct": SHADOW_TAKE_PROFIT_PCT,
    }
    
    _append(entry_record)
    log.info("shadow entry %s at $%.6f source=%s, %s tokens, $%.2f ticket",
             token["ticker"], entry_price, source or "-", f"{size_tokens:.2f}", SHADOW_TICKET_USD)
    return entry_record


def mark(address: str, network_id: int, current_price: float = None, volume_h24: float = None) -> dict:
    """
    Mark an open shadow position to market and check exit conditions.
    Shadow exits: time stop (6h), stop-loss (-30%), take-profit
    (+50% on new tp_50 fills; +100% on existing/legacy positions).
    RISK's h6 volume exit is NOT modelled (no real h6 data available).
    
    Args:
        address: Token address
        network_id: Network ID
        current_price: Current price in USD (if None, position flagged as stale)
        volume_h24: 24-hour volume (for tracking, not used for exits)
    
    Returns:
        Mark record dict, or close record if exit triggered
    """
    positions = open_positions()
    pos = next((p for p in positions if p["address"] == address and p["network_id"] == network_id), None)
    
    if not pos:
        return None
    
    entry_price = pos["entry_price_usd"]
    entry_time = _parse_ts(pos["ts"])
    held_hours = (time.time() - entry_time) / 3600
    last_price = pos.get("last_price_usd", entry_price)
    
    # Check time stop FIRST (applies even to stale positions)
    if held_hours >= SHADOW_MAX_HOLD_HOURS:
        # Close at last good mark (or entry if never marked)
        close_price = last_price if current_price is None else current_price
        has_good_mark = pos.get("last_price_usd") is not None and pos.get("last_price_usd") != entry_price
        
        if current_price is None:
            # Closing stale position at last good mark
            reason = f"time_stop_{held_hours:.1f}h_stale_mark"
            unmeasured = not has_good_mark  # Flag as unmeasured if never had a good mark
        else:
            reason = f"time_stop_{held_hours:.1f}h"
            unmeasured = False
        
        return close(address, network_id, current_price=close_price, reason=reason, unmeasured=unmeasured)
    
    # If no current price, flag as stale and keep open (time stop didn't fire yet)
    if current_price is None or current_price <= 0:
        log.warning("shadow position %s has stale price, keeping open with last mark", pos["ticker"])
        mark_record = {
            "action": "mark",
            "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time())),
            "ticker": pos["ticker"],
            "address": address,
            "network_id": network_id,
            "stale": True,
            "last_price_usd": last_price,
            "held_hours": held_hours,
        }
        _append(mark_record)
        return mark_record
    
    # Calculate unrealized PnL
    size_tokens = pos["size_tokens"]
    current_value_usd = size_tokens * current_price
    unrealized_pnl_usd = current_value_usd - pos["size_usd"]
    unrealized_pnl_pct = (current_price / entry_price - 1) * 100
    
    # Check price-based exits (stop-loss, take-profit)
    # Shadow ledger defaults, NOT trading thresholds
    policy = pos.get("exit_policy") or EXIT_POLICY_LEGACY
    active_tp = SHADOW_TP50_PCT if policy == EXIT_POLICY_TP50 else SHADOW_TAKE_PROFIT_PCT
    tp50_would_exit = unrealized_pnl_pct >= SHADOW_TP50_PCT
    legacy_would_exit = unrealized_pnl_pct >= SHADOW_TAKE_PROFIT_PCT
    should_exit = False
    exit_reason = None
    
    if unrealized_pnl_pct <= SHADOW_STOP_LOSS_PCT:
        should_exit = True
        exit_reason = f"stop_loss_{unrealized_pnl_pct:.1f}pct"
    elif unrealized_pnl_pct >= active_tp:
        should_exit = True
        if policy == EXIT_POLICY_TP50:
            exit_reason = f"tp_50_{unrealized_pnl_pct:.1f}pct"
        else:
            exit_reason = f"take_profit_{unrealized_pnl_pct:.1f}pct"
    
    if should_exit:
        rec = close(address, network_id, current_price=current_price, reason=exit_reason, unmeasured=False)
        if isinstance(rec, dict):
            rec["exit_policy"] = policy
            rec["tp50_would_exit"] = tp50_would_exit
            rec["legacy_would_exit"] = legacy_would_exit
            rec["legacy_take_profit_pct"] = SHADOW_TAKE_PROFIT_PCT
            rec["tp50_pnl_pct"] = unrealized_pnl_pct
        return rec
    
    # Mark to market (stays open). Log both exits for comparison.
    mark_record = {
        "action": "mark",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time())),
        "ticker": pos["ticker"],
        "address": address,
        "network_id": network_id,
        "current_price_usd": current_price,
        "volume_h24": volume_h24,
        "unrealized_pnl_usd": unrealized_pnl_usd,
        "unrealized_pnl_pct": unrealized_pnl_pct,
        "held_hours": held_hours,
        "stale": False,
        "exit_policy": policy,
        "tp50_would_exit": tp50_would_exit,
        "legacy_would_exit": legacy_would_exit,
        "legacy_take_profit_pct": SHADOW_TAKE_PROFIT_PCT,
        "tp50_pnl_pct": unrealized_pnl_pct,
    }
    
    _append(mark_record)
    return mark_record


def close(address: str, network_id: int, current_price: float = None, reason: str = "exit_rule", 
          unmeasured: bool = False) -> dict:
    """
    Close a shadow position and record realized PnL.
    
    Args:
        address: Token address
        network_id: Network ID
        current_price: Exit price (if None, PnL marked unmeasured)
        reason: Why we're closing (time_stop_Xh, stop_loss_Xpct, etc.)
        unmeasured: If True, PnL excluded from realized totals (never had good mark)
    
    Returns:
        Close record dict
    """
    positions = open_positions()
    pos = next((p for p in positions if p["address"] == address and p["network_id"] == network_id), None)
    
    if not pos:
        log.warning("attempted to close non-existent shadow position %s:%s", address, network_id)
        return None
    
    if current_price is None or current_price <= 0 or unmeasured:
        # Stale data or never had a good mark, PnL marked as unmeasured
        size_tokens = pos["size_tokens"]
        exit_value_usd = size_tokens * current_price if current_price else None
        realized_pnl_usd = None
        realized_pnl_pct = None
    else:
        size_tokens = pos["size_tokens"]
        exit_value_usd = size_tokens * current_price
        realized_pnl_usd = exit_value_usd - pos["size_usd"]
        realized_pnl_pct = (current_price / pos["entry_price_usd"] - 1) * 100
    
    close_record = {
        "action": "close",
        "ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time())),
        "ticker": pos["ticker"],
        "address": address,
        "network_id": network_id,
        "chain": pos.get("chain"),  # Preserve chain from entry for proper links
        "entry_price_usd": pos["entry_price_usd"],
        "exit_price_usd": current_price,
        "size_usd": pos["size_usd"],
        "exit_value_usd": exit_value_usd,
        "realized_pnl_usd": realized_pnl_usd,
        "realized_pnl_pct": realized_pnl_pct,
        "unmeasured": unmeasured,  # Flag for excluding from totals
        "reason": reason,
        "held_seconds": time.time() - _parse_ts(pos["ts"]),
        "exit_policy": pos.get("exit_policy") or EXIT_POLICY_LEGACY,
        "legacy_take_profit_pct": SHADOW_TAKE_PROFIT_PCT,
        "tp50_pnl_pct": realized_pnl_pct,
    }
    
    _append(close_record)
    if realized_pnl_usd is not None:
        log.info("shadow close %s at $%.6f, realized PnL $%.2f (%.1f%%), reason=%s",
                 pos["ticker"], current_price, realized_pnl_usd, realized_pnl_pct, reason)
    else:
        log.info("shadow close %s with stale price, PnL unmeasured, reason=%s", pos["ticker"], reason)
    _maybe_legacy_track(pos, close_record)
    return close_record


def _maybe_legacy_track(pos: dict, close_record: dict) -> None:
    """Log-only: keep marking a tp_50 close until the legacy rule resolves."""
    try:
        policy = close_record.get("exit_policy") or pos.get("exit_policy") or EXIT_POLICY_LEGACY
        if policy != EXIT_POLICY_TP50:
            return
        import legacy_shadow_track as lst
        entry_ts = _parse_ts(pos.get("ts"))
        lst.on_tp50_policy_close(
            source=lst.SOURCE_LEDGER,
            key=f"{close_record.get('address')}:{close_record.get('network_id')}:{entry_ts}",
            ticker=pos.get("ticker"),
            address=close_record.get("address"),
            network_id=close_record.get("network_id"),
            tid=f"{close_record.get('address')}:{close_record.get('network_id')}",
            entry_price=pos.get("entry_price_usd"),
            entry_ts=entry_ts,
            size_usd=pos.get("size_usd") or SHADOW_TICKET_USD,
            tp50_exit_price=close_record.get("exit_price_usd"),
            tp50_exit_ts=time.time(),
            tp50_pnl=close_record.get("realized_pnl_usd"),
            tp50_reason=close_record.get("reason"),
        )
    except Exception as e:
        log.warning("legacy shadow track: %s", safe_err(e))


def open_positions() -> list[dict]:
    """
    Return currently open shadow positions (entries without corresponding closes).
    Includes last_price_usd from most recent mark for stale tracking.
    """
    if not LEDGER_PATH.exists():
        return []
    
    entries = {}
    last_marks = {}  # Track most recent mark per position
    
    with LEDGER_PATH.open() as f:
        for line in f:
            if not line.strip():
                continue
            record = json.loads(line)
            addr = record.get("address")
            net_id = record.get("network_id")
            key = (addr, net_id)
            
            if record["action"] == "entry":
                entries[key] = record
            elif record["action"] == "mark" and key in entries:
                # Track last good price from marks
                if not record.get("stale") and record.get("current_price_usd"):
                    last_marks[key] = record["current_price_usd"]
            elif record["action"] == "close" and key in entries:
                del entries[key]
                if key in last_marks:
                    del last_marks[key]
    
    # Update entries with last good mark prices
    positions = []
    for key, entry in entries.items():
        if key in last_marks:
            entry = dict(entry)  # Copy to avoid mutating ledger data
            entry["last_price_usd"] = last_marks[key]
        positions.append(entry)
    
    return positions


def closed_positions(limit: int = 50) -> list[dict]:
    """
    Return recently closed shadow positions with realized PnL.
    """
    if not LEDGER_PATH.exists():
        return []
    
    # Read all closes, most recent first
    closes = []
    with LEDGER_PATH.open() as f:
        for line in f:
            if not line.strip():
                continue
            record = json.loads(line)
            if record["action"] == "close":
                closes.append(record)
    
    closes.reverse()  # Most recent first
    return closes[:limit]


def summary() -> dict:
    """
    Return summary statistics: total realized PnL, open positions count, etc.
    Excludes closes flagged as unmeasured from PnL totals and win/loss counts.
    """
    opens = open_positions()
    closes = closed_positions(limit=None)  # Get all closes
    
    # Exclude unmeasured closes from PnL calculations
    measured_closes = [c for c in closes if not c.get("unmeasured")]
    
    total_realized_pnl = sum(c.get("realized_pnl_usd") or 0 for c in measured_closes if c.get("realized_pnl_usd") is not None)
    total_trades = len(closes)
    measured_trades = len(measured_closes)
    winning_trades = sum(1 for c in measured_closes if (c.get("realized_pnl_usd") or 0) > 0)
    losing_trades = sum(1 for c in measured_closes if (c.get("realized_pnl_usd") or 0) < 0)
    unmeasured_trades = sum(1 for c in closes if c.get("unmeasured") or c.get("realized_pnl_usd") is None)
    
    return {
        "open_positions_count": len(opens),
        "closed_trades_count": total_trades,
        "measured_trades_count": measured_trades,
        "total_realized_pnl_usd": total_realized_pnl,
        "winning_trades": winning_trades,
        "losing_trades": losing_trades,
        "unmeasured_trades": unmeasured_trades,
        "win_rate": winning_trades / measured_trades if measured_trades > 0 else 0,
    }


def _append(record: dict):
    """Append a record to the ledger file."""
    LEDGER_PATH.parent.mkdir(parents=True, exist_ok=True)
    with LEDGER_PATH.open("a") as f:
        f.write(json.dumps(record, default=str) + "\n")


def _parse_ts(iso: str) -> float:
    """Parse ISO timestamp to unix time."""
    try:
        return calendar.timegm(time.strptime(iso, "%Y-%m-%dT%H:%M:%SZ"))  # ts are UTC
    except (ValueError, TypeError):
        return time.time()


def mark_all_open_positions(fomo_client) -> dict:
    """
    Mark all open shadow positions to market using FOMO data.
    Uses fallback exits (time stop, stop-loss, take-profit) clearly labeled as
    shadow defaults, not trading thresholds.
    
    RISK's h6 volume exit is NOT modelled because real h6 volume isn't available
    from FOMO, and computing it from rolling vol24 snapshots closes on surges
    instead of collapses (backwards signal).
    
    Args:
        fomo_client: Fomo instance to fetch fresh prices
    
    Returns:
        dict with counts: marked, closed, stale
    """
    opens = open_positions()
    legacy_pending = []
    try:
        import legacy_shadow_track as lst
        legacy_pending = lst.pending_tracks()
    except Exception as e:
        log.warning("legacy pending: %s", safe_err(e))

    if not opens and not legacy_pending:
        return {"marked": 0, "closed": 0, "stale": 0}

    # One FOMO batch for open fills plus the few post-tp_50 legacy tracks.
    tids = [f"{pos['address']}:{pos['network_id']}" for pos in opens]
    for row in legacy_pending:
        tid = row.get("tid")
        if not tid and row.get("address") is not None and row.get("network_id") is not None:
            tid = f"{row['address']}:{row['network_id']}"
        if tid and tid not in tids:
            tids.append(tid)

    # Fetch fresh data from FOMO (cheap, not GeckoTerminal)
    try:
        fomo_data = fomo_client.tokens(tids) if tids else {}
    except Exception as e:
        log.warning("failed to fetch FOMO data for shadow positions: %s", safe_err(e))
        fomo_data = {}

    marked_count = 0
    closed_count = 0
    stale_count = 0

    for pos in opens:
        tid = f"{pos['address']}:{pos['network_id']}"
        token_data = fomo_data.get(tid, {}) if isinstance(fomo_data, dict) else {}

        price = token_data.get("price")
        vol24 = token_data.get("vol24")

        # Mark position (checks time stop, stop-loss, take-profit)
        result = mark(pos["address"], pos["network_id"],
                     current_price=price, volume_h24=vol24)

        if result:
            if result["action"] == "close":
                closed_count += 1
            elif result.get("stale"):
                stale_count += 1
            else:
                marked_count += 1

    try:
        import legacy_shadow_track as lst
        lst.mark_pending(fomo_data if isinstance(fomo_data, dict) else {}, now=time.time())
    except Exception as e:
        log.warning("legacy mark: %s", safe_err(e))

    log.info("shadow mark: %d marked, %d closed, %d stale", marked_count, closed_count, stale_count)
    return {"marked": marked_count, "closed": closed_count, "stale": stale_count}
