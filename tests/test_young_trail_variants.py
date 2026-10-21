"""Parallel young_trail exit variants. Same marks; live rules unchanged."""
import json
import os
import pathlib
import subprocess
import sys

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DESK_SECRET", "test-secret-for-young-trail-variants")
os.environ.setdefault("JUDGE_MOCK", "1")

ENTRY = 1.0
T0 = 1_700_000_000.0
TID = "Young1:1399811149"


def _tok(tid=TID, ticker="YNG", age=22.0, price=ENTRY, **extra):
    row = {
        "tid": tid, "ticker": ticker, "chain": "solana", "net": 1399811149,
        "age_minutes": age, "price_usd": price,
    }
    row.update(extra)
    return row


@pytest.fixture
def yt(monkeypatch):
    import young_trail
    monkeypatch.delenv("YOUNG_TRAIL_UNTIL", raising=False)
    monkeypatch.delenv("YOUNG_TRAIL_ENABLED", raising=False)
    monkeypatch.delenv("YOUNG_TRAIL_EXIT_POLICY", raising=False)
    young_trail.reset()
    young_trail.set_time_fn(lambda: T0)
    young_trail.set_sleep_fn(lambda _d: None)
    young_trail.set_get_fn(lambda *_a, **_k: (_ for _ in ()).throw(
        AssertionError("young_trail tests must not use the network")
    ))
    yield young_trail
    young_trail.reset()
    young_trail.set_get_fn(None)
    young_trail.set_sleep_fn(None)
    young_trail.set_time_fn(None)


def _v(tid=TID, variant="baseline"):
    import young_trail_variants as ytv
    return ytv.get_variant(tid, variant)


def test_baseline_variant_matches_live_ledger(yt):
    t = _tok()
    yt.consider_entries([t], now=T0)
    yt.mark_open({TID: 0.7}, now=T0 + 60)
    live = yt.closed_rows()[0]
    row = _v(variant="baseline")
    assert row["exit_reason"] == live["exit_reason"] == "sl"
    assert row["exit_price"] == pytest.approx(live["exit_price"])
    assert row["pnl_usd"] == pytest.approx(live["pnl_usd"])
    assert row["pnl_usd"] == pytest.approx(-15.0)


def test_tp_50_and_tp_100(yt):
    yt.consider_entries([_tok()], now=T0)
    yt.mark_open({TID: 1.6}, now=T0 + 60)
    assert _v(variant="tp_50")["exit_reason"] == "tp"
    assert _v(variant="tp_50")["pnl_usd"] == pytest.approx(30.0)
    assert _v(variant="tp_100")["exit_ts"] is None
    yt.mark_open({TID: 2.1}, now=T0 + 120)
    assert _v(variant="tp_100")["exit_reason"] == "tp"
    assert _v(variant="tp_100")["pnl_usd"] == pytest.approx(55.0)
    # Live baseline still uses SL/trail/6h only — no take-profit.
    assert yt.open_rows()[0]["tid"] == TID
    assert yt.open_rows()[0]["exit_ts"] is None


def test_half_50_then_trail(yt):
    import young_trail_variants as ytv
    yt.consider_entries([_tok()], now=T0)
    yt.mark_open({TID: 1.5}, now=T0 + 60)
    mid = _v(variant="half_50_trail")
    assert mid["exit_ts"] is None
    assert mid["remaining_frac"] == pytest.approx(0.5)
    assert mid["partial_pnl"] == pytest.approx(12.5)
    # Clearly through peak*0.7 (1.5*0.7 is 1.05, float). Remaining trails.
    yt.mark_open({TID: 1.04}, now=T0 + 120)
    done = _v(variant="half_50_trail")
    assert done["exit_reason"] == "trail"
    assert done["pnl_usd"] == pytest.approx(12.5 + ytv.pnl_usd(1.04, 1.0, 25.0))


def test_tighter_and_wider_trail(yt):
    yt.consider_entries([_tok()], now=T0)
    yt.mark_open({TID: 2.0}, now=T0 + 60)
    yt.mark_open({TID: 1.5}, now=T0 + 120)
    assert _v(variant="trail_20")["exit_reason"] == "trail"  # stop 1.6
    assert _v(variant="trail_20")["pnl_usd"] == pytest.approx(25.0)
    assert _v(variant="trail_40")["exit_ts"] is None  # stop 1.2
    assert _v(variant="baseline")["exit_ts"] is None  # stop 1.4
    yt.mark_open({TID: 1.2}, now=T0 + 180)
    assert _v(variant="trail_40")["exit_reason"] == "trail"
    assert yt.closed_rows()[0]["exit_reason"] == "trail"  # live 30% trail


def test_tighter_sl_20(yt):
    yt.consider_entries([_tok()], now=T0)
    yt.mark_open({TID: 0.80}, now=T0 + 60)
    assert _v(variant="sl_20")["exit_reason"] == "sl"
    assert _v(variant="sl_20")["pnl_usd"] == pytest.approx(-10.0)
    assert yt.open_rows()[0]["exit_ts"] is None
    assert _v(variant="baseline")["exit_ts"] is None


def test_time_exits_1h_2h_3h(yt):
    yt.consider_entries([_tok()], now=T0)
    yt.mark_open({TID: 1.1}, now=T0 + 3600)
    assert _v(variant="time_1h")["exit_reason"] == "time"
    assert _v(variant="time_2h")["exit_ts"] is None
    yt.mark_open({TID: 1.1}, now=T0 + 2 * 3600)
    assert _v(variant="time_2h")["exit_reason"] == "time"
    yt.mark_open({TID: 1.1}, now=T0 + 3 * 3600)
    assert _v(variant="time_3h")["exit_reason"] == "time"
    assert yt.open_rows()[0]["exit_ts"] is None


def test_breakeven_stop_after_plus_30(yt):
    yt.consider_entries([_tok()], now=T0)
    yt.mark_open({TID: 1.3}, now=T0 + 60)
    assert _v(variant="be_after_30")["be_armed"] == 1
    assert _v(variant="be_after_30")["exit_ts"] is None
    yt.mark_open({TID: 1.0}, now=T0 + 120)
    done = _v(variant="be_after_30")
    assert done["exit_reason"] == "be"
    assert done["exit_price"] == pytest.approx(1.0)
    assert done["pnl_usd"] == pytest.approx(0.0)
    assert yt.open_rows()[0]["exit_ts"] is None


def test_time_to_peak_and_peak_multiple(yt):
    import young_trail_variants as ytv
    yt.consider_entries([_tok()], now=T0)
    yt.mark_open({TID: 1.2}, now=T0 + 60)
    yt.mark_open({TID: 1.8}, now=T0 + 180)
    yt.mark_open({TID: 1.5}, now=T0 + 240)
    stats = ytv.entry_stats()
    assert len(stats) == 1
    s = stats[0]
    assert s["peak"] == pytest.approx(1.8)
    assert s["peak_multiple"] == pytest.approx(1.8)
    assert s["time_to_peak_sec"] == pytest.approx(180.0)


def test_ranking_by_realized_pnl(yt):
    import young_trail_variants as ytv
    yt.consider_entries([_tok()], now=T0)
    yt.mark_open({TID: 1.6}, now=T0 + 60)  # tp_50 wins +30; others stay
    rank = ytv.ranking()
    assert rank[0]["variant"] == "tp_50"
    assert rank[0]["n"] == 1
    assert rank[0]["realized_pnl_usd"] == pytest.approx(30.0)
    assert rank[0]["win_rate"] == pytest.approx(1.0)
    by_id = {r["variant"]: r for r in rank}
    assert by_id["baseline"]["n"] == 0
    assert by_id["tp_50"]["n"] == 1


def test_ops_young_trail_variants_json(yt):
    yt.consider_entries([_tok()], now=T0)
    yt.mark_open({TID: 0.7}, now=T0 + 60)
    from fastapi.testclient import TestClient
    import server
    client = TestClient(server.app)
    resp = client.get("/ops/young_trail_variants")
    assert resp.status_code == 200
    data = resp.json()
    assert data["paper"] is True
    assert data["live_orders"] is False
    assert "15 min" in data["caveat"]
    assert data["ranking"]
    assert any(r["variant"] == "baseline" and r["n"] == 1 for r in data["ranking"])
    assert data["entry_stats"][0]["peak_multiple"] == pytest.approx(1.0)
    assert "SUPERSECRET" not in json.dumps(data)


def test_ops_html_has_variants_panel():
    from fastapi.testclient import TestClient
    import server
    html = TestClient(server.app).get("/ops").text
    assert 'id="variants"' in html
    assert "/ops/young_trail_variants" in html
    assert "Exit variants" in html
    assert "intra-cycle" in html


def test_briefing_includes_top_variants(yt):
    import cycle_history
    yt.consider_entries([_tok()], now=T0)
    yt.mark_open({TID: 1.6}, now=T0 + 60)
    payload = cycle_history.build_briefing(now=T0 + 120)
    md = payload["markdown"]
    assert "Exit variants top 3" in md
    assert "tp_50" in md
    assert "15 min" in md
    assert payload["young_trail_variants"]["top3"][0]["variant"] == "tp_50"
    live = yt.open_rows()
    assert live and live[0]["exit_ts"] is None


def test_live_decide_exit_unchanged(yt):
    # Variants must not alter the live paper rule function.
    assert yt.decide_exit(1.0, 2.0, 1.4, T0, T0 + 10) == ("trail", 1.4)
    assert yt.decide_exit(1.0, 1.0, 0.7, T0, T0 + 10) == ("sl", 0.7)
    assert yt.SL_PCT == -0.30
    assert yt.TRAIL_PCT == 0.30
    assert yt.WINDOW_SEC == 6 * 3600


def test_protected_modules_untouched():
    for name in ("thresholds.py", "questions.py", "pick.py", "filter.py",
                 "collect.py", "judge.py"):
        r = subprocess.run(
            ["git", "diff", "--", name],
            cwd=str(ROOT), capture_output=True, text=True, check=True,
        )
        assert r.stdout == "", name
    src = (ROOT / "young_trail_variants.py").read_text()
    for banned in ("from filter", "from pick", "from questions", "from thresholds",
                   "geckoterminal", "send_to_seats", "book.take"):
        assert banned not in src


CATNIP_TID = "61q3T4MD8UtFxyRgV1CYzgjJfpJNAb11kBgttW7incM:1399811149"
CATNIP_ENTRY = 9.24e-07
CATNIP_GOOD = 1.184e-06
CATNIP_BAD = 0.009226
CATNIP_PNL = 50.0 * (CATNIP_GOOD / CATNIP_ENTRY - 1.0)


def _seed_catnip(yt, *, also_normal=True):
    """Historical Catnip: good mark then a 10,000× print. Parent is bad_mark."""
    import young_trail_variants as ytv
    db = yt._connect()
    ytv.init_schema(db)
    entry_ts = T0
    good_ts = T0 + 60
    bad_ts = T0 + 5 * 3600 + 10 * 60
    huge = ytv.pnl_usd(CATNIP_BAD, CATNIP_ENTRY)
    db.execute(
        """INSERT INTO young_trail (
             tid, ticker, chain, entry_ts, entry_price, peak, last_price,
             last_mark_ts, exit_ts, exit_price, exit_reason, pnl_usd
           ) VALUES (?, 'Catnip', 'solana', ?, ?, ?, ?, ?, ?, ?, 'bad_mark', ?)""",
        (CATNIP_TID, entry_ts, CATNIP_ENTRY, CATNIP_BAD, CATNIP_BAD,
         bad_ts, bad_ts, CATNIP_BAD, huge),
    )
    for ts, px in ((entry_ts, CATNIP_ENTRY), (good_ts, CATNIP_GOOD), (bad_ts, CATNIP_BAD)):
        db.execute(
            "INSERT INTO young_trail_marks (tid, ts, price) VALUES (?, ?, ?)",
            (CATNIP_TID, ts, px),
        )
    db.execute(
        """INSERT INTO young_trail_entry_stats
           (tid, peak, peak_ts, peak_multiple, time_to_peak_sec)
           VALUES (?, ?, ?, ?, ?)""",
        (CATNIP_TID, CATNIP_BAD, bad_ts, CATNIP_BAD / CATNIP_ENTRY, bad_ts - entry_ts),
    )
    for rule in ytv.VARIANTS:
        db.execute(
            """INSERT INTO young_trail_variants
               (tid, variant, exit_ts, exit_price, exit_reason, pnl_usd, peak, last_price)
               VALUES (?, ?, ?, ?, 'tp', ?, ?, ?)""",
            (CATNIP_TID, rule["id"], bad_ts, CATNIP_BAD, huge, CATNIP_BAD, CATNIP_BAD),
        )
    if also_normal:
        yt.consider_entries([_tok()], now=T0)
        yt.mark_open({TID: 0.70}, now=T0 + 60)
    db.commit()
    return huge


def test_catnip_bad_mark_sanitized_to_time_exit(yt):
    """Catnip fixture -> every variant +$14.08 time exit at 1.184e-06."""
    import young_trail_variants as ytv
    _seed_catnip(yt, also_normal=False)
    assert CATNIP_PNL == pytest.approx(14.08, abs=0.02)
    for rule in ytv.VARIANTS:
        raw = ytv.get_variant(CATNIP_TID, rule["id"])
        clean = ytv._sanitized(raw, CATNIP_ENTRY, "bad_mark")
        assert clean["adjusted"] is True
        assert clean["exit_reason"] == "time"
        assert clean["exit_price"] == pytest.approx(CATNIP_GOOD)
        assert clean["pnl_usd"] == pytest.approx(CATNIP_PNL, abs=0.02)
    closed = {r["variant"]: r for r in ytv.closed_rows(limit=None)
              if r["tid"] == CATNIP_TID}
    assert len(closed) == len(ytv.VARIANTS)
    assert all(r["adjusted"] for r in closed.values())
    assert all(r["exit_reason"] == "time" for r in closed.values())
    rank = {r["variant"]: r for r in ytv.ranking()}
    assert rank["tp_50"]["realized_pnl_usd"] == pytest.approx(CATNIP_PNL, abs=0.02)
    assert rank["tp_50"]["n"] == 1
    stats = {r["tid"]: r for r in ytv.entry_stats(limit=None)}
    assert stats[CATNIP_TID]["adjusted"] is True
    assert stats[CATNIP_TID]["peak"] == pytest.approx(CATNIP_GOOD)
    assert stats[CATNIP_TID]["peak_multiple"] == pytest.approx(CATNIP_GOOD / CATNIP_ENTRY)
    payload = ytv.ops_payload()
    assert payload["adjusted_rows"] >= 1


def test_sanitizer_leaves_normal_rows_unchanged(yt):
    import young_trail_variants as ytv
    _seed_catnip(yt, also_normal=True)
    live = yt.closed_rows()
    normal = [r for r in live if r["tid"] == TID]
    assert normal and normal[0]["exit_reason"] == "sl"
    assert normal[0]["pnl_usd"] == pytest.approx(-15.0)
    closed = {r["variant"]: r for r in ytv.closed_rows(limit=None) if r["tid"] == TID}
    assert closed["baseline"]["adjusted"] is False
    assert closed["baseline"]["exit_reason"] == "sl"
    assert closed["baseline"]["pnl_usd"] == pytest.approx(-15.0)
    rank = {r["variant"]: r for r in ytv.ranking()}
    # Catnip +14.08 beats the -15 SL, so baseline is mixed: +14.08 + -15.
    assert rank["baseline"]["n"] == 2
    assert rank["baseline"]["realized_pnl_usd"] == pytest.approx(CATNIP_PNL - 15.0, abs=0.02)
    assert rank["tp_50"]["realized_pnl_usd"] == pytest.approx(CATNIP_PNL - 15.0, abs=0.02)
    # Higher P&L ranks first; both closed variants share the same corrected total
    # but n/win_rate keep a stable order.
    ordered = ytv.ranking()
    assert ordered[0]["realized_pnl_usd"] >= ordered[-1]["realized_pnl_usd"]


def test_sanitizer_is_display_only_no_update(yt):
    import young_trail_variants as ytv
    _seed_catnip(yt, also_normal=False)
    db = yt._connect()
    writes = []

    def tracer(sql):
        head = str(sql).lstrip().split(None, 1)[0].upper()
        if head in ("UPDATE", "INSERT", "DELETE", "REPLACE"):
            writes.append(sql)

    db.set_trace_callback(tracer)
    ytv.ranking()
    ytv.closed_rows(limit=None)
    ytv.entry_stats(limit=None)
    ytv.ops_payload()
    db.set_trace_callback(None)
    assert writes == []
    raw = ytv.get_variant(CATNIP_TID, "tp_50")
    assert raw["exit_price"] == pytest.approx(CATNIP_BAD)
    assert raw["pnl_usd"] == pytest.approx(ytv.pnl_usd(CATNIP_BAD, CATNIP_ENTRY))
