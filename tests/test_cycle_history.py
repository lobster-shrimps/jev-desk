"""
Deterministic tests for cycle-history store, backfill, morning briefing, trends.

No real sleeps, no network. TZ forced to UTC.
"""
import json
import logging
import os
import pathlib
import sys
import time
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DESK_SECRET", "test-secret-for-cycle-history")
os.environ.setdefault("JUDGE_MOCK", "1")
os.environ.setdefault("DESK_OUTBOX", str(ROOT / "tests" / "_outbox"))
os.environ["TZ"] = "UTC"
if hasattr(time, "tzset"):
    time.tzset()


def ts(y, m, d, hh=12, mm=0, ss=0) -> float:
    return time.mktime((y, m, d, hh, mm, ss, 0, 0, -1))


@pytest.fixture
def hist(tmp_path, monkeypatch):
    os.environ["TZ"] = "UTC"
    if hasattr(time, "tzset"):
        time.tzset()
    monkeypatch.setenv("CYCLE_HISTORY_DB", str(tmp_path / "cycle_history.db"))
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    import cycle_history
    cycle_history.reset()
    yield cycle_history
    cycle_history.reset()


def _stats(**over):
    base = {
        "seen": 20,
        "benched": 4,
        "judged": 2,
        "requeued": 1,
        "free": {"age": 6, "liquidity": 2},
        "trade": {"no_sells": 1},
        "chain": {"top_wallet": 1},
        "soft": {"momentum_already_spent": 1},
        "carry": 3,
        "unevaluated": 5,
        "tokens": [],
        "young_free": [],
    }
    base.update(over)
    return base


def test_record_cycle_persists_required_fields(hist):
    now = ts(2026, 10, 9, 8, 0)
    cid = hist.on_cycle(_stats(), shadow=True, now=now, source="live")
    assert cid is not None
    rows = hist.cycles_since(now - 1)
    assert len(rows) == 1
    c = rows[0]
    assert c["mode"] == "shadow"
    assert c["seen"] == 20
    assert c["benched"] == 4
    assert c["judged"] == 2
    assert c["requeued"] == 1
    assert c["free"] == {"age": 6, "liquidity": 2}
    assert c["trade"] == {"no_sells": 1}
    assert c["chain"] == {"top_wallet": 1}
    assert c["soft"] == {"momentum_already_spent": 1}
    assert c["carry"] == 3
    assert c["unevaluated"] == 5
    assert c["source"] == "live"


def test_gt_cache_counts_persist(hist):
    hist.on_cycle(
        _stats(gt_cache_hits=3, gt_cache_misses=7, gt_dossier_attempts=7, gt_dossier_ok=6),
        now=ts(2026, 10, 9, 8), source="backfill",
    )
    c = hist.cycles_since(0)[0]
    assert c["gt_cache_hits"] == 3
    assert c["gt_cache_misses"] == 7
    assert c["gt_dossier_attempts"] == 7
    assert c["gt_dossier_ok"] == 6
    w = hist.summarize_window(0, ts(2026, 10, 9, 9))
    assert w["gt_cache_hits"] == 3
    assert w["gt_cache_misses"] == 7


def test_gt_defer_separate_from_gt_429(hist):
    stats = _stats(tokens=[
        {"tid": "a:1399811149", "stage": "chain", "reason": "requeued_429_backoff"},
        {"tid": "b:56", "stage": "chain", "reason": "requeued"},
        {"tid": "c:56", "stage": "chain", "reason": "top_wallet"},
    ], requeued=2, gt_429=5)
    hist.on_cycle(stats, now=ts(2026, 10, 9, 8), source="backfill")
    c = hist.cycles_since(0)[0]
    assert c["gt_429"] == 5
    assert c["gt_defer"] == 2


def test_soft_and_judged_tokens_and_chain_ids(hist):
    hist.on_soft_token(
        tid="So1:1399811149", ticker="SOLX", net=1399811149, chain="solana",
        reason="momentum_already_spent", noul=0.71, age_minutes=22.5,
        top_10_percent=28, top_wallet_percent=0.03,
        developer_holding_percentage=4, holder_count=210,
        soft_scores={"momentum_already_spent": 0.71, "effort": 1.0},
        ts=ts(2026, 10, 9, 8, 1),
    )
    stats = _stats(
        judged=1,
        tokens=[{
            "tid": "0xbase:8453", "ticker": "BASC", "net": 8453, "chain": "bsc",
            "stage": "judged", "reason": None, "age_minutes": 40,
            "top_10_percent": 22, "top_wallet_percent": 0.02,
            "developer_holding_percentage": 1, "holder_count": 400,
            "soft_scores": {"momentum_already_spent": 0.41},
        }, {
            "tid": "0xmon:143", "ticker": "MON", "net": 143,
            "stage": "judged", "reason": None, "age_minutes": 18,
            "soft_scores": {"momentum_already_spent": 0.22},
        }, {
            "tid": "0xbsc:56", "ticker": "BNB", "net": 56,
            "stage": "soft", "reason": "dev_still_loaded", "soft_noul": 0.6,
            "age_minutes": 50, "soft_scores": {"dev_still_loaded": 0.6},
        }],
    )
    hist.on_cycle(stats, now=ts(2026, 10, 9, 8, 2), source="backfill")
    tokens = hist._tokens_since(0)
    kinds = {(t["ticker"], t["kind"], t["chain"], t["chain_id"]) for t in tokens}
    assert ("SOLX", "soft", "solana", 1399811149) in kinds
    assert ("BASC", "judged", "base", 8453) in kinds  # 8453 is Base, not the bsc seat
    assert ("MON", "judged", "monad", 143) in kinds
    assert ("BNB", "soft", "bsc", 56) in kinds
    sol = next(t for t in tokens if t["ticker"] == "SOLX")
    assert sol["noul"] == 0.71
    assert sol["age_minutes"] == 22.5
    assert sol["top_10_percent"] == 28
    assert sol["top_wallet_percent"] == 0.03
    assert sol["developer_holding_percentage"] == 4
    assert sol["holder_count"] == 210
    assert sol["soft_scores"]["momentum_already_spent"] == 0.71
    assert sol["reason"] == "momentum_already_spent"


def test_chain_name_mapping():
    import cycle_history as ch
    assert ch.chain_name(net=1399811149) == "solana"
    assert ch.chain_name(net=56) == "bsc"
    assert ch.chain_name(net=8453) == "base"
    assert ch.chain_name(net=4663) == "robinhood"
    assert ch.chain_name(net=143) == "monad"
    assert ch.chain_id_of(tid="addr:4663") == 4663


def test_young_free_pass_outcomes(hist):
    stats = _stats(
        seen=10, benched=0, judged=1,
        tokens=[
            {"tid": "y1:1399811149", "stage": "chain", "reason": "requeued_429_backoff"},
            {"tid": "y2:1399811149", "stage": "chain", "reason": "top_wallet"},
            {"tid": "y3:56", "stage": "soft", "reason": "momentum_already_spent",
             "soft_noul": 0.8, "soft_scores": {"momentum_already_spent": 0.8}},
            {"tid": "y4:56", "stage": "judged", "reason": None,
             "soft_scores": {"momentum_already_spent": 0.3}},
        ],
        young_free=[
            {"tid": "y1:1399811149", "ticker": "A", "net": 1399811149, "age_minutes": 20},
            {"tid": "y2:1399811149", "ticker": "B", "net": 1399811149, "age_minutes": 25},
            {"tid": "y3:56", "ticker": "C", "net": 56, "age_minutes": 30},
            {"tid": "y4:56", "ticker": "D", "net": 56, "age_minutes": 35},
            {"tid": "y5:8453", "ticker": "E", "net": 8453, "age_minutes": 12},
        ],
    )
    hist.on_cycle(stats, now=ts(2026, 10, 9, 8), source="backfill")
    young = hist._young_since(0)
    by_tid = {y["tid"]: y["outcome"] for y in young}
    assert by_tid["y1:1399811149"] == "deferred_429"
    assert by_tid["y2:1399811149"] == "chain"
    assert by_tid["y3:56"] == "soft"
    assert by_tid["y4:56"] == "judged"
    assert by_tid["y5:8453"] == "unevaluated"
    window = hist.summarize_window(0, ts(2026, 10, 9, 9))
    assert window["young_outcomes"]["deferred_429"] == 1
    assert window["young_outcomes"]["chain"] == 1
    assert window["young_outcomes"]["soft"] == 1
    assert window["young_outcomes"]["judged"] == 1
    assert window["young_outcomes"]["unevaluated"] == 1
    assert window["young_free_pass_rate"] == pytest.approx(0.5)  # 5 young / 10 examined


def test_backfill_parses_cycle_and_soft_lines(hist):
    log_text = """
2026-10-08 10:00:01,001 desk INFO free tid=Young1:1399811149 reason=pass age_minutes=22.0 liquidity_usd=48000 volume_usd=610000 mcap_usd=300000
2026-10-08 10:00:02,002 desk INFO young token YNG (age 22.0m) hit 429, backoff 45.0s > cap 30.0s, deferring
2026-10-08 10:00:03,003 desk INFO chain tid=Young1:1399811149 ticker=YNG reason=requeued_429_backoff age_minutes=22.0
2026-10-08 10:00:04,004 desk INFO soft tid=Soft1:56 ticker=SFT reason=momentum_already_spent noul=0.77 age_minutes=44 top_10_percent=31 top_wallet_percent=0.02 developer_holding_percentage=3 holder_count=190 rpc_ok=True soft_scores={'momentum_already_spent': 0.77, 'effort': 1.0}
2026-10-08 10:00:05,005 desk INFO carrying 2 of 4 unevaluated ids (cap=2 due to 0 due ids)
2026-10-08 10:00:06,006 desk INFO unevaluated 4 ids (dex_slots=0, gt_available=0)
2026-10-08 10:00:07,007 desk INFO cycle: 12 seen, 3 benched, free {'age': 4}, trade {}, chain {'requeued_429_backoff': 1}, soft {'momentum_already_spent': 1}, judged 0, requeued 1
"""
    n = hist.backfill_text(log_text)
    assert n["inserted"] == 1
    c = hist.cycles_since(0)[0]
    assert c["seen"] == 12
    assert c["benched"] == 3
    assert c["judged"] == 0
    assert c["requeued"] == 1
    assert c["free"] == {"age": 4}
    assert c["carry"] == 4
    assert c["unevaluated"] == 4
    assert c["source"] == "backfill"
    tokens = hist._tokens_since(0)
    soft = [t for t in tokens if t["kind"] == "soft"]
    assert len(soft) == 1
    assert soft[0]["ticker"] == "SFT"
    assert soft[0]["chain"] == "bsc"
    assert soft[0]["noul"] == pytest.approx(0.77)
    assert soft[0]["holder_count"] == 190
    young = hist._young_since(0)
    assert len(young) == 1
    assert young[0]["outcome"] == "deferred_429"
    assert young[0]["tid"] == "Young1:1399811149"
    assert c["gt_429"] == 1
    assert c["gt_defer"] == 1


def test_backfill_script_reads_file(hist, tmp_path, caplog):
    log_path = tmp_path / "run.log"
    log_path.write_text(
        "2026-10-08 11:00:00,000 desk INFO cycle: 1 seen, 0 benched, "
        "free {}, trade {}, chain {}, soft {}, judged 0, requeued 0\n"
    )
    import backfill_cycle_history
    with caplog.at_level(logging.INFO):
        rc = backfill_cycle_history.main([str(log_path)])
    assert rc == 0
    assert hist.cycles_since(0)[0]["seen"] == 1
    assert "backfilled 1 cycles" in caplog.text


def test_briefing_last_24h_summary(hist):
    now = ts(2026, 10, 9, 8, 0)
    hist.on_cycle(
        _stats(
            seen=10, benched=1, judged=1,
            tokens=[{
                "tid": "j:1399811149", "ticker": "JK", "net": 1399811149,
                "stage": "judged", "age_minutes": 33,
                "soft_scores": {"momentum_already_spent": 0.35, "effort": 1.0},
            }],
            young_free=[{"tid": "j:1399811149", "ticker": "JK", "net": 1399811149, "age_minutes": 33}],
        ),
        now=now - 100, source="backfill",
    )
    payload = hist.build_briefing(now)
    w = payload["last_24h"]
    assert w["cycles"] == 1
    assert w["seen"] == 10
    assert w["judged"] == 1
    assert w["kills"]["free"]["age"] == 6
    assert len(w["judged_tokens"]) == 1
    assert w["judged_tokens"][0]["ticker"] == "JK"
    assert w["median_momentum"] == pytest.approx(0.35)
    assert w["solana"]["judged"] == 1
    assert "Morning briefing" in payload["markdown"]
    assert "Solana" in payload["markdown"]
    assert "JK" in payload["markdown"]
    assert "## Momentum" in payload["markdown"]
    assert "shadow only" in payload["markdown"].lower()
    assert "reached judge 1" in payload["markdown"]
    assert "## Passed judge/picks" in payload["markdown"]
    assert "## Judge verdicts" in payload["markdown"]
    assert "No tokens judged in the window." not in payload["markdown"]
    assert w["shadow"]["fills"] == 0
    assert "picks are not fills" in w["shadow"]["fills_note"]


def test_briefing_not_emitted_before_0700(hist):
    hist.on_cycle(_stats(), now=ts(2026, 10, 9, 6, 45), source="live")
    assert not hist.briefing_exists("2026-10-09")
    assert not (hist.briefings_dir() / "2026-10-09.md").exists()


def test_briefing_persists_after_0700_once(hist):
    hist.on_cycle(_stats(), now=ts(2026, 10, 9, 7, 15), source="live")
    assert hist.briefing_exists("2026-10-09")
    path = hist.briefings_dir() / "2026-10-09.md"
    assert path.exists()
    text = path.read_text()
    assert text.startswith("# Morning briefing — 2026-10-09")
    hist.on_cycle(_stats(), now=ts(2026, 10, 9, 8, 0), source="live")
    n = hist._connect().execute("SELECT COUNT(*) FROM briefings").fetchone()[0]
    assert n == 1


def test_backfill_does_not_auto_persist_briefing(hist):
    hist.on_cycle(_stats(), now=ts(2026, 10, 9, 7, 30), source="backfill")
    assert not hist.briefing_exists("2026-10-09")


def test_trends_flags_notable_changes(hist):
    # Prior 7 days at 00:30 so they sit outside a 10-09 08:00 rolling 24h window.
    for day in range(2, 9):
        hist.on_cycle(
            _stats(
                seen=20, benched=0, judged=4,
                free={"age": 10}, trade={}, chain={}, soft={},
                tokens=[{
                    "tid": f"p{day}:1399811149", "ticker": f"P{day}",
                    "net": 1399811149, "stage": "judged", "age_minutes": 40,
                    "soft_scores": {"momentum_already_spent": 0.40},
                }] * 4,
            ),
            now=ts(2026, 10, day, 0, 30), source="backfill",
        )
    # Today: judged jumps, momentum drops, more GT defers, mix shifts toward soft.
    hist.on_cycle(
        _stats(
            seen=20, benched=0, judged=12, requeued=8,
            free={"age": 1}, trade={}, chain={},
            soft={"momentum_already_spent": 8},
            tokens=(
                [{
                    "tid": f"t{i}:56", "ticker": f"T{i}", "net": 56,
                    "stage": "judged", "age_minutes": 20,
                    "soft_scores": {"momentum_already_spent": 0.15},
                } for i in range(12)]
                + [{
                    "tid": f"d{i}:1399811149", "stage": "chain",
                    "reason": "requeued_429_backoff",
                } for i in range(8)]
            ),
            young_free=[{"tid": f"t{i}:56", "ticker": f"T{i}", "net": 56, "age_minutes": 20}
                        for i in range(12)],
        ),
        now=ts(2026, 10, 9, 7, 30), source="backfill",
    )
    payload = hist.build_briefing(ts(2026, 10, 9, 8, 0))
    by_metric = {f["metric"]: f for f in payload["trends"]["flags"]}
    assert by_metric["judged_per_day"]["notable"] is True
    assert by_metric["median_momentum"]["notable"] is True
    assert by_metric["gt_defers"]["notable"] is True
    assert by_metric["kill_mix_soft"]["notable"] is True
    assert any(f["metric"] == "chain_share_bsc" and f["notable"] for f in payload["trends"]["flags"])
    assert payload["trends"]["notable"]
    assert "## Trends vs prior 7 days" in payload["markdown"]
    assert "judged_per_day" in payload["markdown"]
    assert len(payload["trends"]["days"]) == 7
    assert payload["trends"]["days"][0]["date"] == "2026-10-02"
    assert payload["trends"]["days"][-1]["date"] == "2026-10-08"


def test_median_momentum_includes_soft_and_per_chain(hist):
    hist.on_cycle(
        _stats(tokens=[
            {"tid": "a:1399811149", "stage": "judged", "chain": "solana",
             "soft_scores": {"momentum_already_spent": 0.2}},
            {"tid": "b:1399811149", "stage": "judged", "chain": "solana",
             "soft_scores": {"momentum_already_spent": 0.4}},
            {"tid": "c:56", "stage": "judged", "chain": "bsc",
             "soft_scores": {"momentum_already_spent": 0.9}},
            {"tid": "d:1399811149", "stage": "soft", "chain": "solana",
             "reason": "momentum_already_spent",
             "soft_noul": 0.99, "soft_scores": {"momentum_already_spent": 0.99}},
        ], judged=3),
        now=ts(2026, 10, 9, 8), source="backfill",
    )
    w = hist.summarize_window(0, ts(2026, 10, 9, 9))
    # judged 0.2/0.4/0.9 plus soft 0.99 → median (0.4+0.9)/2
    assert w["median_momentum"] == pytest.approx(0.65)
    assert w["median_momentum_soft"] == pytest.approx(0.99)
    assert w["median_momentum_solana"] == pytest.approx(0.4)
    assert w["median_momentum_by_chain"]["solana"] == pytest.approx(0.4)
    assert w["median_momentum_by_chain"]["bsc"] == pytest.approx(0.9)


def test_shadow_pnl_in_briefing(hist, tmp_path, monkeypatch):
    import shadow_ledger
    ledger = tmp_path / "shadow_ledger.jsonl"
    monkeypatch.setattr(shadow_ledger, "LEDGER_PATH", ledger)
    now = ts(2026, 10, 9, 8, 0)
    iso = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now - 60))
    entry = {
        "action": "entry", "ts": iso, "ticker": "SHD", "address": "addr",
        "network_id": 1399811149, "chain": "solana",
        "entry_price_usd": 1.0, "size_usd": 50.0, "size_tokens": 50.0,
    }
    close = {
        "action": "close", "ts": iso, "ticker": "SHD", "address": "addr",
        "network_id": 1399811149, "entry_price_usd": 1.0,
        "exit_price_usd": 1.2, "size_usd": 50.0, "realized_pnl_usd": 10.0,
        "unmeasured": False,
    }
    ledger.write_text(json.dumps(entry) + "\n" + json.dumps(close) + "\n")
    hist.on_cycle(_stats(), now=now - 10, source="backfill")
    w = hist.summarize_window(now - 86400, now)
    assert w["shadow"]["picks"] == 1
    assert w["shadow"]["fills"] == 0
    assert w["shadow"]["realized_pnl_usd"] == pytest.approx(10.0)


def test_uptime_uses_15min_cadence(hist):
    now = ts(2026, 10, 9, 8, 0)
    hist.on_cycle(_stats(), now=now - 1800, source="backfill")
    hist.on_cycle(_stats(), now=now - 900, source="backfill")
    w = hist.summarize_window(now - 3600, now)
    assert w["uptime"]["cycles"] == 2
    assert w["uptime"]["expected_cycles"] == 4
    assert w["uptime"]["uptime_pct"] == pytest.approx(50.0)


def test_history_errors_use_safe_err(hist, caplog, monkeypatch):
    def boom(*a, **k):
        raise RuntimeError("failed url https://rpc.example/?api-key=SUPERSECRET")

    monkeypatch.setattr(hist, "_connect", boom)
    with caplog.at_level(logging.WARNING):
        hist.on_cycle(_stats(), now=ts(2026, 10, 9, 8), source="backfill")
    assert "SUPERSECRET" not in caplog.text
    assert "REDACTED" in caplog.text or "api-key" in caplog.text


def test_history_hook_scrubs_secrets(hist, caplog, monkeypatch):
    import main as shift

    def boom(**kwargs):
        raise RuntimeError("token=LEAKEDTOKEN123")

    monkeypatch.setattr(hist, "on_cycle", boom)
    with caplog.at_level(logging.WARNING):
        shift._history_hook("cycle", stats=_stats(), shadow=True)
    assert "LEAKEDTOKEN123" not in caplog.text
    assert "cycle history:" in caplog.text


def test_held_cycle_not_recorded(hist):
    assert hist.on_cycle({"held": "ABC", "minutes": 3}, now=ts(2026, 10, 9, 8)) is None
    assert hist.cycles_since(0) == []


def test_cycle_and_soft_log_format_unchanged():
    src = (ROOT / "main.py").read_text()
    assert (
        'log.info("cycle: %(seen)s seen, %(benched)s benched, free %(free)s, "\n'
        '             "trade %(trade)s, chain %(chain)s, soft %(soft)s, '
        'judged %(judged)s, requeued %(requeued)s", stats)'
    ) in src
    assert (
        'log.info("soft tid=%s ticker=%s reason=%s noul=%s age_minutes=%s '
        'top_10_percent=%s top_wallet_percent=%s developer_holding_percentage=%s '
        'holder_count=%s rpc_ok=%s soft_scores=%s"'
    ) in src
    assert (
        'log.info("unevaluated %d ids (dex_slots=%d, gt_available=%d)",'
    ) in src


def test_run_once_writes_history_and_keeps_log_lines(hist, monkeypatch, caplog):
    import book
    import collect
    import main as shift

    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.execute("DELETE FROM carry")
    book.DB.commit()

    tid = f"HistAddr:{1399811149}"

    def fake_shortlist(fomo, id_list):
        return [{
            "addr": "HistAddr", "net": 1399811149, "tid": tid, "ticker": "HST",
            "mcap_usd": 300_000, "liquidity_usd": 48_000, "volume_h24": 610_000,
            "price_usd": 0.001, "holder_count": 310,
            "change": {"5m": 0.04, "1h": 0.22, "4h": 0.4, "24h": 0.61},
            "age_minutes": 42, "chain": "solana",
        }]

    def fake_judge(question_set, state):
        if question_set == "market":
            return {"model": "test", "answers": {
                "concentration_is_exit_risk": {"type": "noul", "noul": 0.75},
                "momentum_already_spent": {"type": "noul", "noul": 0.40},
            }, "usage": {}}
        return {"model": "test", "answers": {}, "usage": {}}

    def fake_dossier(t, limiter=None):
        return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                "developer_holding_percentage": 2, "gt_score_details": None,
                "is_honeypot": None, "mint_authority": None, "freeze_authority": None,
                "description": "a token", "x_handle": None, "net": 1399811149}

    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: ([tid], {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist)
    monkeypatch.setattr(collect, "shortlist", fake_shortlist)
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: (
        {"buys_h1": 540, "sells_h1": 120, "buys_h6": 900, "sells_h6": 400,
         "trades_h24": 4000}, "ok"))
    monkeypatch.setattr(shift, "dossier", fake_dossier)

    class Desk:
        def bank(self): return 1000.0
        def read_x(self, h): return None
        def log_shadow(self, o, s, fomo_data=None): pass
        def report(self, o, s): pass
        def send_to_seats(self, o): pass

    caplog.clear()
    with caplog.at_level(logging.INFO):
        order, stats = shift.run_once(type("F", (), {"token": lambda self: None})(),
                                      fake_judge, Desk(), 1000.0, shadow=True,
                                      gt_dossier_reserve=3)
    assert order is None
    cycle_logs = [r.message for r in caplog.records if r.message.startswith("cycle:")]
    assert cycle_logs
    assert "seen" in cycle_logs[0] and "judged" in cycle_logs[0]
    soft_logs = [r.message for r in caplog.records if r.message.startswith("soft tid=")]
    assert soft_logs
    assert "noul=0.75" in soft_logs[0]
    rows = hist.cycles_since(0)
    assert len(rows) == 1
    assert rows[0]["seen"] == 1
    soft = [t for t in hist._tokens_since(0) if t["kind"] == "soft"]
    assert len(soft) == 1
    assert soft[0]["ticker"] == "HST"


def test_ops_briefing_endpoint(hist):
    hist.on_cycle(_stats(judged=1, tokens=[{
        "tid": "x:1399811149", "ticker": "XX", "net": 1399811149,
        "stage": "judged", "age_minutes": 21,
        "soft_scores": {"momentum_already_spent": 0.3},
    }]), now=time.time() - 10, source="backfill")
    from fastapi.testclient import TestClient
    import server
    client = TestClient(server.app)
    resp = client.get("/ops/briefing")
    assert resp.status_code == 200
    data = resp.json()
    assert "last_24h" in data
    assert "trends" in data
    assert "markdown" in data
    assert data["last_24h"]["judged"] >= 1
    assert "SUPERSECRET" not in json.dumps(data)


def test_ops_html_has_briefing_and_trends():
    from fastapi.testclient import TestClient
    import server
    client = TestClient(server.app)
    resp = client.get("/ops")
    assert resp.status_code == 200
    html = resp.text
    assert 'id="briefing-panel"' in html
    assert 'id="trends-panel"' in html
    assert 'id="briefing-momentum"' in html
    assert "/ops/briefing" in html
    assert "renderBriefing" in html
    assert "renderTrends" in html
    assert "Solana only" in html
    assert "insufficient history" in html
    assert "Young free-pass" in html or "young free-pass" in html.lower()
    assert "Reached judge" in html
    assert "Passed judge/picks" in html
    assert "No tokens passed judge/picks" in html
    assert "Judge verdicts" in html
    assert 'id="briefing-judge-verdicts"' in html
    assert 'id="judge-verdicts-section"' in html
    assert "renderJudgeVerdicts" in html
    assert 'id="briefing-regime"' in html
    assert 'id="trends-regime"' in html
    assert "Market regime (log-only)" in html
    assert "regime / day" in html


def test_briefing_payload_empty_store(hist):
    payload = hist.briefing_payload(ts(2026, 10, 9, 8))
    assert payload["last_24h"]["cycles"] == 0
    assert payload["markdown"].startswith("# Morning briefing")
    assert payload["trends"]["days"]
    assert payload["trends"]["insufficient_history"] is True
    assert payload["trends"]["flags"] == []
    assert "insufficient history" in payload["markdown"]
    assert "## Market regime (log-only)" in payload["markdown"]
    assert "does not affect filtering or judging" in payload["markdown"]


def test_no_env_leak_in_briefing_md(hist, monkeypatch):
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-should-never-appear")
    monkeypatch.setenv("DESK_SECRET", "desk-secret-should-never-appear")
    hist.on_cycle(_stats(), now=ts(2026, 10, 9, 8), source="backfill")
    md = hist.build_briefing(ts(2026, 10, 9, 8))["markdown"]
    assert "ts-should-never-appear" not in md
    assert "desk-secret-should-never-appear" not in md
    assert ".env" not in md


# ---------------------------------------------------------------------------
# Review 1 fixes F1–F6
# ---------------------------------------------------------------------------

_CYCLE_LINE = (
    "2026-10-08 11:00:00,000 desk INFO cycle: 12 seen, 3 benched, "
    "free {'age': 4, 'pass': 8}, trade {}, chain {}, soft {}, judged 0, requeued 0\n"
)


def test_backfill_idempotent_does_not_double_count(hist):
    first = hist.backfill_text(_CYCLE_LINE)
    second = hist.backfill_text(_CYCLE_LINE)
    assert first["inserted"] == 1
    assert second["inserted"] == 0
    assert second["skipped"] == 1
    rows = hist.cycles_since(0)
    assert len(rows) == 1
    assert rows[0]["seen"] == 12
    w = hist.summarize_window(0, ts(2026, 10, 9, 9))
    assert w["cycles"] == 1
    assert w["seen"] == 12


def test_backfill_skips_live_captured_cycle(hist):
    live_ts = time.mktime(time.strptime("2026-10-08 11:00:00", "%Y-%m-%d %H:%M:%S"))
    cid = hist.on_cycle(_stats(seen=99, judged=3), now=live_ts, source="live")
    assert cid is not None
    result = hist.backfill_text(_CYCLE_LINE)
    assert result["inserted"] == 0
    assert result["skipped"] == 1
    rows = hist.cycles_since(0)
    assert len(rows) == 1
    assert rows[0]["source"] == "live"
    assert rows[0]["seen"] == 99
    assert rows[0]["judged"] == 3


def test_backfill_parses_ticker_with_spaces(hist):
    log_text = """
2026-10-08 10:00:03,003 desk INFO chain tid=Spc1:1399811149 ticker=Hello World reason=top_wallet
2026-10-08 10:00:04,004 desk INFO soft tid=Spc2:56 ticker=Hello World reason=momentum_already_spent noul=0.68 age_minutes=44 top_10_percent=31 top_wallet_percent=0.02 developer_holding_percentage=3 holder_count=190 rpc_ok=True soft_scores={'momentum_already_spent': 0.68}
2026-10-08 10:00:07,007 desk INFO cycle: 4 seen, 0 benched, free {}, trade {}, chain {'top_wallet': 1}, soft {'momentum_already_spent': 1}, judged 0, requeued 0
"""
    result = hist.backfill_text(log_text)
    assert result["inserted"] == 1
    tokens = hist._tokens_since(0)
    soft = [t for t in tokens if t["kind"] == "soft"]
    assert len(soft) == 1
    assert soft[0]["ticker"] == "Hello World"
    parsed, _meta = hist.parse_run_log(log_text)
    later = parsed[0]["stats"]["tokens"]
    chain_rows = [t for t in later if t.get("stage") == "chain"]
    assert any(t.get("ticker") == "Hello World" for t in chain_rows)


def test_backfill_reports_unparsed_lines(hist):
    log_text = """
2026-10-08 10:00:04,004 desk INFO soft tid=Broken:56 this line has no ticker= reason= fields
2026-10-08 10:00:05,005 desk INFO chain tid=Broken2:56 missing ticker field entirely
2026-10-08 10:00:07,007 desk INFO cycle: 2 seen, 0 benched, free {}, trade {}, chain {}, soft {}, judged 0, requeued 0
"""
    result = hist.backfill_text(log_text)
    assert result["inserted"] == 1
    assert result["unparsed_soft"] == 1
    assert result["unparsed_chain"] == 1
    assert result["unparsed"] == 2


def test_kill_mix_excludes_free_pass(hist):
    hist.on_cycle(
        _stats(
            seen=20, benched=0, judged=0,
            free={"pass": 8, "age": 1},
            trade={}, chain={}, soft={},
        ),
        now=ts(2026, 10, 9, 8), source="backfill",
    )
    w = hist.summarize_window(0, ts(2026, 10, 9, 9))
    assert "pass" not in w["kills"]["free"]
    assert w["kills"]["free"] == {"age": 1}
    assert w["kill_mix"]["counts"]["free"] == 1
    assert w["kill_mix"]["total"] == 1
    assert w["kill_mix"]["shares"]["free"] == pytest.approx(100.0)
    md = hist.build_briefing(ts(2026, 10, 9, 9))["markdown"]
    assert "pass: 8" not in md
    assert "reached judge 0" in md
    assert "No tokens passed judge/picks in the window." in md
    assert "No tokens judged in the window." not in md


def test_backfill_gt_429_from_young_hit_429_lines(hist):
    log_text = """
2026-10-08 10:00:01,001 desk INFO free tid=Young1:1399811149 reason=pass age_minutes=22.0 liquidity_usd=48000 volume_usd=610000 mcap_usd=300000
2026-10-08 10:00:02,002 desk INFO young token Hello World (age 22.0m) hit 429, backoff 45.0s > cap 30.0s, deferring
2026-10-08 10:00:02,500 desk INFO young token Other (age 18.0m) hit 429, backoff 45.0s > cap 30.0s, deferring
2026-10-08 10:00:03,003 desk INFO chain tid=Young1:1399811149 ticker=Hello World reason=requeued_429_backoff age_minutes=22.0
2026-10-08 10:00:07,007 desk INFO cycle: 6 seen, 1 benched, free {'pass': 1}, trade {}, chain {'requeued_429_backoff': 1}, soft {}, judged 0, requeued 1
"""
    result = hist.backfill_text(log_text)
    assert result["inserted"] == 1
    c = hist.cycles_since(0)[0]
    assert c["gt_429"] == 2
    assert c["gt_defer"] == 1
    w = hist.summarize_window(0, ts(2026, 10, 9, 9))
    assert w["gt_429"] == 2
    assert w["gt_defer"] == 1


def test_limiter_stats_429_count_increments_and_resets():
    import collect
    fake_time = [1000.0]
    limiter = collect.GTRateLimiter(calls_per_min=8, time_fn=lambda: fake_time[0])
    assert limiter.stats_429_count == 0
    limiter.record_429(retry_after_sec=1.0)
    limiter.record_429(retry_after_sec=1.0)
    assert limiter.stats_429_count == 2
    limiter.set_universe_budget(5)
    assert limiter.stats_429_count == 0


def test_run_once_records_limiter_gt_429(hist, monkeypatch):
    import book
    import collect
    import main as shift

    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.execute("DELETE FROM carry")
    book.DB.commit()

    tid = f"LimAddr:{1399811149}"
    limiter = collect.GTRateLimiter(calls_per_min=10, time_fn=lambda: 10_000.0)

    def fake_shortlist(fomo, id_list):
        return [{
            "addr": "LimAddr", "net": 1399811149, "tid": tid, "ticker": "LIM",
            "mcap_usd": 300_000, "liquidity_usd": 48_000, "volume_h24": 610_000,
            "price_usd": 0.001, "holder_count": 310,
            "change": {"5m": 0.04, "1h": 0.22, "4h": 0.4, "24h": 0.61},
            "age_minutes": 42, "chain": "solana",
        }]

    def fake_judge(question_set, state):
        if question_set == "market":
            return {"model": "test", "answers": {
                "concentration_is_exit_risk": {"type": "noul", "noul": 0.75},
                "momentum_already_spent": {"type": "noul", "noul": 0.40},
            }, "usage": {}}
        return {"model": "test", "answers": {}, "usage": {}}

    def fake_dossier(t, limiter=None):
        if limiter is not None:
            limiter.record_429(retry_after_sec=1.0)
            limiter.record_429(retry_after_sec=1.0)
        return {**t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
                "developer_holding_percentage": 2, "gt_score_details": None,
                "is_honeypot": None, "mint_authority": None, "freeze_authority": None,
                "description": "a token", "x_handle": None, "net": 1399811149}

    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: ([tid], {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist)
    monkeypatch.setattr(collect, "shortlist", fake_shortlist)
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: (
        {"buys_h1": 540, "sells_h1": 120, "buys_h6": 900, "sells_h6": 400,
         "trades_h24": 4000}, "ok"))
    monkeypatch.setattr(shift, "dossier", fake_dossier)

    class Desk:
        def bank(self): return 1000.0
        def read_x(self, h): return None
        def log_shadow(self, o, s, fomo_data=None): pass
        def report(self, o, s): pass
        def send_to_seats(self, o): pass

    order, stats = shift.run_once(
        type("F", (), {"token": lambda self: None})(),
        fake_judge, Desk(), 1000.0, shadow=True,
        gt_dossier_reserve=3, gt_limiter=limiter,
    )
    assert order is None
    assert stats["gt_429"] == 2
    rows = hist.cycles_since(0)
    assert len(rows) == 1
    assert rows[0]["gt_429"] == 2
    assert rows[0]["gt_defer"] == 0


def test_trends_suppressed_without_history(hist):
    for day in (7, 8):
        hist.on_cycle(
            _stats(seen=10, benched=0, judged=1, free={"age": 2}, trade={}, chain={}, soft={}),
            now=ts(2026, 10, day, 0, 30), source="backfill",
        )
    hist.on_cycle(
        _stats(seen=10, benched=0, judged=12, free={"age": 1}, trade={}, chain={}, soft={}),
        now=ts(2026, 10, 9, 7, 30), source="backfill",
    )
    payload = hist.build_briefing(ts(2026, 10, 9, 8, 0))
    trends = payload["trends"]
    assert trends["insufficient_history"] is True
    assert trends["history_days"] == 2
    assert trends["flags"] == []
    assert trends["notable"] == []
    assert "insufficient history" in payload["markdown"]
    assert "judged_per_day" not in payload["markdown"]


def test_trends_flags_after_three_prior_days(hist):
    for day in (6, 7, 8):
        hist.on_cycle(
            _stats(
                seen=20, benched=0, judged=4,
                free={"age": 10}, trade={}, chain={}, soft={},
                tokens=[{
                    "tid": f"p{day}:1399811149", "ticker": f"P{day}",
                    "net": 1399811149, "stage": "judged", "age_minutes": 40,
                    "soft_scores": {"momentum_already_spent": 0.40},
                }] * 4,
            ),
            now=ts(2026, 10, day, 0, 30), source="backfill",
        )
    hist.on_cycle(
        _stats(
            seen=20, benched=0, judged=12,
            free={"age": 1}, trade={}, chain={}, soft={},
            tokens=[{
                "tid": f"t{i}:56", "ticker": f"T{i}", "net": 56,
                "stage": "judged", "age_minutes": 20,
                "soft_scores": {"momentum_already_spent": 0.15},
            } for i in range(12)],
        ),
        now=ts(2026, 10, 9, 7, 30), source="backfill",
    )
    payload = hist.build_briefing(ts(2026, 10, 9, 8, 0))
    trends = payload["trends"]
    assert trends["insufficient_history"] is False
    assert trends["history_days"] == 3
    by_metric = {f["metric"]: f for f in trends["flags"]}
    assert by_metric["judged_per_day"]["notable"] is True
