"""
One uvicorn process for the bots to reach: the judge plus two book endpoints RISK needs,
because the Grok Bots run in xAI's cloud and the book (desk.db) lives on this machine.

    uvicorn server:app --host 0.0.0.0 --port 8080
    cloudflared tunnel --url http://localhost:8080

  POST /judge          the judge (see judge.py)
  GET  /book/held      {"held": {...}|null}
  POST /book/release   RISK calls this the moment a close is filled. Nothing else does.
  GET  /ops            ops panel HTML
  GET  /ops/summary    read-only desk aggregate (cached 30s)
  GET  /ops/briefing   last-24h briefing + 7-day trends
  GET  /ops/briefing?compact=1  markdown + morning only (<50 KB)
  GET  /ops/backtest   latest weekly signal-backtest JSON
  GET  /ops/young_trail paper young-trail ledger (simulated, no live orders)
  GET  /ops/young_trail_variants paper exit-variant ranking (same marks)
  GET  /ops/young_trail_fast paper 60s-mark ledger vs per-cycle young_trail
  GET  /ops/research/query?sql=  read-only SELECT on research.db (limit 500)
  GET  /ops/research/report?kind= rugs|runners|features
  GET  /api/state      outbox/state.json for ops panel
"""
import json
import logging
import os
import pathlib

from fastapi import APIRouter, Header, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse

log = logging.getLogger("desk.ops")

import book
import shadow_ledger
from fomo_api import activate_fomo_tab
from judge import app, DESK_SECRET
from secret_utils import safe_err

HERE = pathlib.Path(__file__).parent
router = APIRouter(prefix="/book")


def _outbox() -> pathlib.Path:
    """Resolve at call time so tests (and ops) follow the current DESK_OUTBOX."""
    return pathlib.Path(os.environ.get("DESK_OUTBOX", "outbox"))


def _auth(authorization: str):
    if authorization != f"Bearer {DESK_SECRET}":
        raise HTTPException(401, "bad desk secret")


@router.get("/held")
async def held(authorization: str = Header("")):
    _auth(authorization)
    return {"held": book.held()}


@router.post("/release")
async def release(authorization: str = Header("")):
    """RISK only. The seat that closed the position is the seat that frees the book."""
    _auth(authorization)
    was = book.held()
    book.release()
    return {"released": was}


app.include_router(router)


@app.get("/ops", response_class=HTMLResponse)
async def ops_panel():
    """Serve the ops panel HTML."""
    html_path = HERE / "ops.html"
    if not html_path.exists():
        raise HTTPException(404, "ops.html not found")
    return html_path.read_text()


@app.get("/ops/summary")
async def ops_summary():
    """Read-only desk aggregate. Cached 30s. Fail open. Local-only (127.0.0.1)."""
    try:
        import ops_summary as _ops_summary
        return JSONResponse(_ops_summary.summary_payload())
    except Exception as e:
        log.warning("ops summary: %s", safe_err(e))
        return JSONResponse({
            "error": "summary unavailable",
            "generated_at_iso": None,
            "health": {
                "desk": {"loaded": False, "pid": None},
                "ops": {"loaded": False, "pid": None},
                "mode": "shadow",
                "judge_mode": "live",
                "last_cycle_start_iso": None,
                "last_cycle_secs": None,
                "next_cycle_in_s": None,
                "cycles_24h": 0,
                "no_trade_24h": 0,
                "shadow_24h": 0,
                "tracebacks_24h": {"run_log": 0, "uvicorn_log": 0},
                "last_traceback": None,
                "fomo_bearer_ok": False,
            },
            "judge_hi": [],
            "outcomes": {
                "complete": 0, "pending_live": 0, "pending_backfill": 0,
                "dead": 0, "complete_24h": 0, "window": "01:00-06:00",
                "trickle": True, "paused": False,
            },
            "budget": {
                "cg_plan": None, "cg_key_present": False,
                "cg_month_calls": 0, "cg_month_cap": 9000,
                "cg_daily_cap": 0, "cg_today": 0, "demo_active": False,
                "gt_calls_per_min": 5, "gt_429_24h": 0, "outcomes_spacing_s": 20.0,
            },
            "regime": {
                "tag": None, "ts_iso": None, "reason": None,
                "long_liq_usd": None, "short_liq_usd": None,
                "majors_funding": None, "memes_funding": None,
                "oi_change": None, "history": [],
            },
            "young_trail_curve": [],
        })


@app.get("/ops/briefing")
async def ops_briefing(compact: int = 0):
    """Last-24h morning briefing plus 7-day trends. Local-only (bind 127.0.0.1).

    ?compact=1 returns date_local / generated_at_iso / markdown / morning only.
    """
    try:
        import cycle_history
        return JSONResponse(cycle_history.briefing_payload(compact=bool(compact)))
    except Exception as e:
        log.warning("ops briefing: %s", safe_err(e))
        if compact:
            return JSONResponse({
                "error": "briefing unavailable",
                "date_local": None,
                "generated_at_iso": None,
                "markdown": "",
                "morning": None,
            })
        return JSONResponse({
            "error": "briefing unavailable",
            "last_24h": {},
            "trends": {"days": [], "flags": [], "notable": []},
            "markdown": "",
        })


@app.get("/ops/young_trail")
async def ops_young_trail():
    """Paper young-trail ledger. Simulated; no live orders. Fail open."""
    try:
        import young_trail
        return JSONResponse(young_trail.ops_payload())
    except Exception as e:
        log.warning("ops young_trail: %s", safe_err(e))
        return JSONResponse({
            "paper": True,
            "simulated": True,
            "live_orders": False,
            "error": "young_trail unavailable",
            "open_positions": [],
            "closed_trades": [],
            "summary": {
                "open_count": 0, "closed_count": 0, "win_rate": 0.0,
                "realized_pnl_usd": 0.0, "winning_trades": 0, "losing_trades": 0,
            },
        })


@app.get("/ops/young_trail_fast")
async def ops_young_trail_fast():
    """Paper 60s-mark young_trail_fast vs per-cycle. Simulated; no live orders."""
    try:
        import young_trail_fast
        return JSONResponse(young_trail_fast.ops_payload())
    except Exception as e:
        log.warning("ops young_trail_fast: %s", safe_err(e))
        return JSONResponse({
            "paper": True,
            "simulated": True,
            "live_orders": False,
            "error": "young_trail_fast unavailable",
            "summary": {
                "baseline": {
                    "open_count": 0, "closed_count": 0, "win_rate": 0.0,
                    "realized_pnl_usd": 0.0,
                },
                "tp_50": {
                    "open_count": 0, "closed_count": 0, "win_rate": 0.0,
                    "realized_pnl_usd": 0.0,
                },
            },
            "comparison": [],
            "open_positions": [],
            "closed_trades": [],
        })


@app.get("/ops/young_trail_variants")
async def ops_young_trail_variants():
    """Paper exit-variant ranking on the same young_trail marks. Fail open."""
    try:
        import young_trail_variants
        return JSONResponse(young_trail_variants.ops_payload())
    except Exception as e:
        log.warning("ops young_trail_variants: %s", safe_err(e))
        return JSONResponse({
            "paper": True,
            "simulated": True,
            "live_orders": False,
            "error": "young_trail_variants unavailable",
            "caveat": "Marks are only every ~15 min cycle (intra-cycle moves missed).",
            "ranking": [],
            "top3": [],
            "closed_trades": [],
            "entry_stats": [],
        })


@app.get("/ops/research/query")
async def ops_research_query(sql: str = ""):
    """Read-only SELECT on research.db. Local-only (bind 127.0.0.1). Limit 500."""
    try:
        import research
        if not sql or not str(sql).strip():
            return JSONResponse({"error": "sql required", "rows": [], "n": 0, "limit": 500},
                                status_code=400)
        result = research.ops_query(sql)
        status = 400 if result.get("error") else 200
        return JSONResponse(result, status_code=status)
    except Exception as e:
        log.warning("ops research query: %s", safe_err(e))
        return JSONResponse({"error": "query failed", "rows": [], "n": 0, "limit": 500})


@app.get("/ops/research/report")
async def ops_research_report(kind: str = "features"):
    """Canned rugs|runners|features. Local-only. Fail open."""
    try:
        import research
        return JSONResponse(research.report(kind))
    except ValueError as e:
        return JSONResponse({"error": str(e), "kind": kind}, status_code=400)
    except Exception as e:
        log.warning("ops research report: %s", safe_err(e))
        return JSONResponse({"error": "report unavailable", "kind": kind})


@app.get("/ops/backtest")
async def ops_backtest():
    """Latest weekly signal backtest JSON. Local-only (bind 127.0.0.1). Fail open."""
    try:
        import backtest_signals
        data = backtest_signals.latest_payload()
        if data is None:
            return JSONResponse({
                "error": "no backtest yet",
                "n": 0,
                "winners": {"rows": []},
                "kill_reasons": [],
                "judge_buckets": [],
                "features": {},
                "n_caveat": backtest_signals.N_CAVEAT,
                "shadow_only": True,
            })
        return JSONResponse(data)
    except Exception as e:
        log.warning("ops backtest: %s", safe_err(e))
        return JSONResponse({
            "error": "backtest unavailable",
            "n": 0,
            "winners": {"rows": []},
            "kill_reasons": [],
            "shadow_only": True,
        })


@app.get("/api/state")
async def api_state():
    """Serve outbox/state.json for the ops panel."""
    state_path = _outbox() / "state.json"
    if not state_path.exists():
        return JSONResponse({"demo": False, "tokens": [], "cycle": None}, status_code=404)
    return JSONResponse(json.loads(state_path.read_text()))


@app.get("/api/shadow_ledger")
async def api_shadow_ledger():
    """
    Return shadow PnL ledger: open positions, closed positions, summary.
    All data is hypothetical and labeled as such.
    """
    return JSONResponse({
        "open_positions": shadow_ledger.open_positions(),
        "closed_positions": shadow_ledger.closed_positions(limit=50),
        "summary": shadow_ledger.summary()
    })


@app.get("/api/fomo_status")
async def api_fomo_status():
    """
    Return FOMO health status. NEVER returns the bearer token itself.
    Safe for ops panel display.
    """
    # Try to get FOMO health from the most recent state.json
    state_path = _outbox() / "state.json"
    if state_path.exists():
        try:
            state = json.loads(state_path.read_text())
            if state.get("fomo_health"):
                return JSONResponse(state["fomo_health"])
        except Exception:
            pass
    
    # Fallback: return minimal status
    return JSONResponse({
        "bearer_present": False,
        "message": "No FOMO health data available yet. Wait for a cycle to complete."
    })


@app.post("/api/fomo_activate")
async def api_fomo_activate(x_ops_action: str = Header(None, alias="X-Ops-Action")):
    """
    Activate the fomo.family tab in the CDP Chrome instance.
    Requires X-Ops-Action header to prevent cross-site requests.
    Local-only endpoint (bind server to 127.0.0.1 for safety).
    """
    # CSRF protection: require custom header that browsers won't send cross-origin
    if x_ops_action != "activate-fomo":
        raise HTTPException(
            403, 
            "Forbidden: missing or invalid X-Ops-Action header. "
            "This endpoint requires same-origin requests from /ops."
        )
    
    result = activate_fomo_tab()
    status_code = 200 if result["success"] else 500
    return JSONResponse(result, status_code=status_code)
