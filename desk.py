"""
DESK — the Grok Bot side, as seen from main.py. Five hooks:

  bank()                 -> float, free cash right now
  read_x(handle)         -> the X block SOCIAL collects with its plugin, or None
  log_shadow(order, st)  -> append a row for the shadow week
  report(order, stats)   -> one line to Telegram, trade or no trade
  send_to_seats(order)   -> hand it to SIZE, then FILLS, then RISK, in that order

Wiring (all optional, all via env):
  BANK_USD             free cash the desk may size against. SIZE clamps at 6% of this.
  SOCIAL_URL           SOCIAL bot endpoint. POST {"x_handle": ...} -> the X block JSON.
                       Unset: in-desk fxtwitter lookup (x_lookup.read_x). Failure is
                       None and NO_SOCIAL_CUT still applies.
  SEATS_WEBHOOK_URL    where a finished order goes (CHIEF's inbox). Unset: orders are
                       written to outbox/orders/<order_id>.json for you to hand over.
  TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID    the one line per cycle.
  SHADOW_LOG           path of the shadow JSONL (default outbox/shadow.jsonl).
"""
import json
import logging
import os
import pathlib
import time

import requests

import shadow_ledger
from secret_utils import safe_err
import x_lookup

log = logging.getLogger("desk.side")

OUTBOX = pathlib.Path(os.environ.get("DESK_OUTBOX", "outbox"))


class Desk:
    def __init__(self):
        self.bank_usd = float(os.environ.get("BANK_USD", "0"))
        self.social_url = os.environ.get("SOCIAL_URL")
        self.seats_url = os.environ.get("SEATS_WEBHOOK_URL")
        self.tg_token = os.environ.get("TELEGRAM_BOT_TOKEN")
        self.tg_chat = os.environ.get("TELEGRAM_CHAT_ID")
        self.shadow_log = pathlib.Path(os.environ.get("SHADOW_LOG", OUTBOX / "shadow.jsonl"))
        self.fomo_client = None  # Set by main.py when wiring
        (OUTBOX / "orders").mkdir(parents=True, exist_ok=True)

    # ---- inputs ---------------------------------------------------------------
    def bank(self) -> float:
        return self.bank_usd

    def read_x(self, handle: str, tid: str | None = None):
        """X block for the SOCIAL question set. Missing is missing: never substitute.

        SOCIAL_URL still POSTs to the bot. Unset: fxtwitter via x_lookup.read_x.
        tid is the token id for the `x tid=` log line.
        """
        if not handle:
            return None
        if self.social_url:
            try:
                r = requests.post(self.social_url, json={"x_handle": handle}, timeout=60)
                r.raise_for_status()
                block = r.json()
                x = block.get("x_account", block) if isinstance(block, dict) else None
                return x or None
            except Exception as e:
                log.warning("SOCIAL read failed for @%s: %s", handle, safe_err(e))
                return None
        return x_lookup.read_x(handle, tid=tid)

    # ---- outputs --------------------------------------------------------------
    def log_shadow(self, order: dict, stats: dict, fomo_data: dict = None):
        """
        One row per would-be trade. Read only the rows where you disagree.
        Also records hypothetical entry in the shadow PnL ledger.
        """
        row = {"ts": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
               "order": order, "stats": stats, "your_call": None}   # fill your_call by hand
        self.shadow_log.parent.mkdir(parents=True, exist_ok=True)
        with self.shadow_log.open("a") as f:
            f.write(json.dumps(row, default=str) + "\n")
        log.info("SHADOW would trade %s (%s) size x%s conf %s",
                 order["token"]["ticker"], order["token"]["chain"],
                 order["size_factor"], order.get("confidence"))
        
        # Record hypothetical entry in shadow PnL ledger
        if fomo_data:
            shadow_ledger.entry(order, fomo_data)

    def report(self, order, stats):
        self.write_state(order, stats)
        if order:
            t = order["token"]
            line = (f"ORDER {order.get('order_id')} {t['ticker']} on {t['chain']} "
                    f"size x{order['size_factor']} conf {order.get('confidence')} "
                    f"model {order['model']}")
        elif "held" in stats:
            line = f"HOLDING {stats['held']} for {stats['minutes']} min, no scan"
        else:
            kills = {k: v for k, v in stats.items() if isinstance(v, dict) and v}
            line = (f"NO TRADE. seen {stats.get('seen', 0)}, benched {stats.get('benched', 0)}, "
                    f"judged {stats.get('judged', 0)}, killed {json.dumps(kills)}")
        log.info(line)
        self._telegram(line)

    def send_to_seats(self, order: dict):
        """SIZE -> FILLS -> RISK, in that order, nobody skips ahead. CHIEF works it."""
        path = OUTBOX / "orders" / f"{order['order_id'].replace(':', '-')}.json"
        path.write_text(json.dumps(order, indent=2, default=str))
        if self.seats_url:
            try:
                requests.post(self.seats_url, json=order, timeout=30).raise_for_status()
                log.info("order handed to seats: %s", order["token"]["ticker"])
            except Exception as e:
                log.error("seat handoff failed, order left in %s: %s", path, e)
                self._telegram(f"HANDOFF FAILED for {order['token']['ticker']}: {e}")
        else:
            log.info("no SEATS_WEBHOOK_URL, order written to %s", path)

    def write_state(self, order, stats):
        """Write outbox/state.json for the ops panel. Real data only, no demo tokens."""
        import book
        state_path = OUTBOX / "state.json"
        now = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())
        
        tokens = stats.get("tokens", [])
        if len(tokens) > 100:
            tokens = tokens[:100]
        
        kill_histograms = {
            "free": stats.get("free", {}),
            "trade": stats.get("trade", {}),
            "chain": stats.get("chain", {}),
            "soft": stats.get("soft", {})
        }
        
        if "held" in stats:
            outcome = "HOLDING"
            error = None
        elif order:
            outcome = "SHADOW" if not os.environ.get("CONFIRM_LIVE") else "ORDER"
            error = None
        elif stats.get("error"):
            outcome = "ERROR"
            error = stats.get("error")
        else:
            outcome = "NO TRADE"
            error = None
        
        # Determine judge mode
        judge_mode = "mock" if os.environ.get("JUDGE_MOCK") == "1" else "live"
        
        # Get FOMO health if available
        fomo_health = None
        if self.fomo_client:
            try:
                fomo_health = self.fomo_client.health()
            except Exception as e:
                log.warning("failed to get FOMO health: %s", e)
        
        state = {
            "updated_at": now,
            "demo": False,
            "mode": "live" if os.environ.get("CONFIRM_LIVE") == "yes" else "shadow",
            "judge_mode": judge_mode,
            "fomo_health": fomo_health,
            "cycle": {
                "seen": stats.get("seen", 0),
                "benched": stats.get("benched", 0),
                "judged": stats.get("judged", 0),
                "requeued": stats.get("requeued", 0),
                "killed": kill_histograms,
                "outcome": outcome,
                "error": error,
                "fomo_error": stats.get("fomo_error"),  # Track FomoAuthError
                "dex_degraded": stats.get("dex_degraded", False),  # Track DexScreener health
            },
            "tokens": tokens,
            "held": [book.held()] if book.held() else [],
            "bench": book.bench_count(),
            "pick": order
        }
        
        state_path.parent.mkdir(parents=True, exist_ok=True)
        state_path.write_text(json.dumps(state, default=str))

    def _telegram(self, text: str):
        if not (self.tg_token and self.tg_chat):
            return
        try:
            requests.post(f"https://api.telegram.org/bot{self.tg_token}/sendMessage",
                          json={"chat_id": self.tg_chat, "text": text}, timeout=15)
        except Exception as e:
            log.warning("telegram failed: %s", e)
