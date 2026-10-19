"""Paper young-trail ledger: entry dedupe, stops, 6h, P&L. No live orders.

Does not change thresholds, questions, pick, or filter. Mocked only.
"""
import json
import os
import pathlib
import subprocess
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DESK_SECRET", "test-secret-for-young-trail")
os.environ.setdefault("JUDGE_MOCK", "1")

TICKET = 50.0
ENTRY = 1.0
T0 = 1_700_000_000.0


def _tok(tid="Young1:1399811149", ticker="YNG", age=22.0, price=ENTRY, **extra):
    row = {
        "tid": tid,
        "ticker": ticker,
        "chain": "solana",
        "net": 1399811149,
        "age_minutes": age,
        "price_usd": price,
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


def test_pnl_math(yt):
    assert yt.pnl_usd(0.7, 1.0) == pytest.approx(-15.0)
    assert yt.pnl_usd(1.4, 1.0) == pytest.approx(20.0)
    assert yt.pnl_usd(2.0, 1.0, ticket=50) == pytest.approx(50.0)
    assert yt.pnl_usd(1.0, 0.0) == 0.0


def test_entry_dedupe_first_seen_per_tid(yt):
    t = _tok()
    n1 = yt.consider_entries([t], now=T0)
    n2 = yt.consider_entries([t], now=T0 + 60)
    n3 = yt.consider_entries([_tok(ticker="YNG2")], now=T0 + 120)
    assert n1 == 1
    assert n2 == 0
    assert n3 == 0
    opens = yt.open_rows()
    assert len(opens) == 1
    assert opens[0]["tid"] == t["tid"]
    assert opens[0]["entry_price"] == ENTRY
    assert opens[0]["peak"] == ENTRY
    assert opens[0]["exit_ts"] is None
    assert opens[0]["pnl_usd"] is None


def test_skip_age_60_and_missing_price(yt):
    old = _tok(tid="Old1:1399811149", age=60.0)
    no_px = _tok(tid="Nopx:1399811149", price=None)
    no_px.pop("price_usd")
    young = _tok(tid="Ok1:1399811149", age=59.9)
    n = yt.consider_entries([old, no_px, young], now=T0)
    assert n == 1
    tids = {r["tid"] for r in yt.open_rows()}
    assert tids == {"Ok1:1399811149"}


def test_hard_stop_minus_30(yt):
    t = _tok()
    yt.consider_entries([t], now=T0)
    stats = yt.mark_open({t["tid"]: 0.70}, now=T0 + 60)
    assert stats["closed"] == 1
    closed = yt.closed_rows()
    assert len(closed) == 1
    row = closed[0]
    assert row["exit_reason"] == "sl"
    assert row["exit_price"] == pytest.approx(0.70)
    assert row["pnl_usd"] == pytest.approx(-15.0)
    assert yt.open_rows() == []
    sm = yt.summary()
    assert sm["closed_count"] == 1
    assert sm["open_count"] == 0
    assert sm["realized_pnl_usd"] == pytest.approx(-15.0)
    assert sm["win_rate"] == 0.0


def test_trailing_exit_from_peak(yt):
    t = _tok()
    yt.consider_entries([t], now=T0)
    # New high: peak 2.0, trail stop = 1.4. Stay open.
    stay = yt.mark_open({t["tid"]: 2.0}, now=T0 + 60)
    assert stay["closed"] == 0
    assert stay["marked"] == 1
    peak = yt.open_rows()[0]["peak"]
    assert peak == pytest.approx(2.0)
    # Exactly peak * 0.7 exits trail.
    gone = yt.mark_open({t["tid"]: 1.4}, now=T0 + 120)
    assert gone["closed"] == 1
    row = yt.closed_rows()[0]
    assert row["exit_reason"] == "trail"
    assert row["exit_price"] == pytest.approx(1.4)
    assert row["peak"] == pytest.approx(2.0)
    assert row["pnl_usd"] == pytest.approx(20.0)


def test_trail_beats_sl_when_peak_has_risen(yt):
    t = _tok()
    yt.consider_entries([t], now=T0)
    yt.mark_open({t["tid"]: 2.0}, now=T0 + 60)
    yt.mark_open({t["tid"]: 0.5}, now=T0 + 120)
    row = yt.closed_rows()[0]
    assert row["exit_reason"] == "trail"
    assert row["pnl_usd"] == pytest.approx(yt.pnl_usd(0.5, 1.0))


def test_time_exit_6h(yt):
    t = _tok()
    yt.consider_entries([t], now=T0)
    yt.mark_open({t["tid"]: 1.1}, now=T0 + 6 * 3600 - 1)
    assert len(yt.open_rows()) == 1
    yt.mark_open({t["tid"]: 1.1}, now=T0 + 6 * 3600)
    row = yt.closed_rows()[0]
    assert row["exit_reason"] == "time"
    assert row["exit_price"] == pytest.approx(1.1)
    assert row["pnl_usd"] == pytest.approx(5.0)


def test_time_exit_no_price_uses_last_mark_not_peak(yt):
    t = _tok()
    yt.consider_entries([t], now=T0)
    yt.mark_open({t["tid"]: 2.0}, now=T0 + 60)
    # 1.5 is above trail (peak*0.7=1.4) so this stays open and becomes last_price.
    stay = yt.mark_open({t["tid"]: 1.5}, now=T0 + 120)
    assert stay["closed"] == 0
    open_row = yt.open_rows()[0]
    assert open_row["peak"] == pytest.approx(2.0)
    assert open_row["last_price"] == pytest.approx(1.5)
    yt.mark_open({}, now=T0 + 6 * 3600)
    row = yt.closed_rows()[0]
    assert row["exit_reason"] == "time"
    assert row["exit_price"] == pytest.approx(1.5)
    assert row["peak"] == pytest.approx(2.0)
    assert row["pnl_usd"] == pytest.approx(yt.pnl_usd(1.5, 1.0))


def test_sl_beats_time_on_same_tick(yt):
    t = _tok()
    yt.consider_entries([t], now=T0)
    yt.mark_open({t["tid"]: 0.5}, now=T0 + 6 * 3600)
    assert yt.closed_rows()[0]["exit_reason"] == "sl"


def test_realized_pnl_only_closed(yt):
    a = _tok(tid="A:1399811149")
    b = _tok(tid="B:1399811149", ticker="BBB")
    yt.consider_entries([a, b], now=T0)
    yt.mark_open({a["tid"]: 0.7, b["tid"]: 1.5}, now=T0 + 60)
    sm = yt.summary()
    assert sm["open_count"] == 1
    assert sm["closed_count"] == 1
    assert sm["realized_pnl_usd"] == pytest.approx(-15.0)
    assert sm["winning_trades"] == 0
    open_row = yt.open_rows()[0]
    assert open_row["tid"] == b["tid"]
    assert open_row["pnl_usd"] is None


def test_win_rate_on_closed(yt):
    w = _tok(tid="W:1399811149")
    l = _tok(tid="L:1399811149", ticker="LOSER")
    yt.consider_entries([w, l], now=T0)
    yt.mark_open({w["tid"]: 2.0}, now=T0 + 60)
    yt.mark_open({w["tid"]: 1.4, l["tid"]: 0.7}, now=T0 + 120)
    sm = yt.summary()
    assert sm["closed_count"] == 2
    assert sm["winning_trades"] == 1
    assert sm["losing_trades"] == 1
    assert sm["win_rate"] == pytest.approx(0.5)
    assert sm["realized_pnl_usd"] == pytest.approx(5.0)


def test_disabled_flag_blocks_entries(yt, monkeypatch):
    monkeypatch.setenv("YOUNG_TRAIL_ENABLED", "0")
    n = yt.consider_entries([_tok()], now=T0)
    assert n == 0
    assert yt.open_rows() == []
    payload = yt.ops_payload()
    assert payload["enabled"] is False
    assert payload["live_orders"] is False
    assert payload["paper"] is True


def test_until_stops_new_entries_but_mtm_closes(yt, monkeypatch):
    monkeypatch.setenv("YOUNG_TRAIL_UNTIL", "2023-01-01")
    yt.set_time_fn(lambda: T0)
    n = yt.consider_entries([_tok()], now=T0)
    assert n == 0
    # Seed a row from before the cutoff by temporarily enabling a future until.
    monkeypatch.delenv("YOUNG_TRAIL_UNTIL", raising=False)
    assert yt.consider_entries([_tok()], now=T0) == 1
    monkeypatch.setenv("YOUNG_TRAIL_UNTIL", "2023-01-01")
    # Past until: no new tids, but existing can still exit.
    other = _tok(tid="Z:1399811149", ticker="ZZZ")
    assert yt.consider_entries([other], now=T0 + 10) == 0
    yt.mark_open({_tok()["tid"]: 0.7}, now=T0 + 60)
    assert yt.closed_rows()[0]["exit_reason"] == "sl"


def test_default_until_is_oct20_et(yt):
    import calendar
    expected = calendar.timegm(
        time.strptime("2026-10-21T03:59:59Z", "%Y-%m-%dT%H:%M:%SZ")
    )
    until = yt.until_ts(T0)
    assert until == pytest.approx(expected)
    assert yt.until_ts(T0 + 100) == pytest.approx(until)


def test_until_overwrites_rolling_stored(yt):
    import calendar
    yt._meta_set("until_ts", str(T0 + 7 * 86400))
    expected = calendar.timegm(
        time.strptime("2026-10-21T03:59:59Z", "%Y-%m-%dT%H:%M:%SZ")
    )
    assert yt.until_ts(T0) == pytest.approx(expected)


def test_on_cycle_uses_young_free_and_cycle_prices(yt):
    stats = {"young_free": [_tok(price=None)]}
    stats["young_free"][0].pop("price_usd", None)
    tokens = [_tok(price=0.02)]
    out = yt.on_cycle(stats=stats, tokens=tokens, now=T0)
    assert out["entries"] == 1
    assert yt.open_rows()[0]["entry_price"] == pytest.approx(0.02)


def test_on_cycle_fail_open_on_bad_stats(yt):
    out = yt.on_cycle(stats={"young_free": None}, tokens=[object()], now=T0)
    assert out["entries"] == 0


def test_ops_young_trail_json(yt):
    yt.consider_entries([_tok()], now=T0)
    yt.mark_open({_tok()["tid"]: 0.7}, now=T0 + 60)
    from fastapi.testclient import TestClient
    import server
    client = TestClient(server.app)
    resp = client.get("/ops/young_trail")
    assert resp.status_code == 200
    data = resp.json()
    assert data["paper"] is True
    assert data["simulated"] is True
    assert data["live_orders"] is False
    assert data["summary"]["closed_count"] == 1
    assert data["summary"]["realized_pnl_usd"] == pytest.approx(-15.0)
    assert data["closed_trades"][0]["exit_reason"] == "sl"
    assert data["exit_policy"] == "tp50"
    assert "by_exit_policy" in data["summary"]
    assert data["summary"]["by_exit_policy"]["combined"]["closed_count"] == 1
    assert "SUPERSECRET" not in json.dumps(data)


def test_ops_html_has_young_trail_panel():
    from fastapi.testclient import TestClient
    import server
    client = TestClient(server.app)
    html = client.get("/ops").text
    assert 'id="yt"' in html
    assert 'id="yt-curve"' in html
    assert 'id="yt-cmp"' in html
    assert "/ops/young_trail" in html
    assert "young_trail" in html
    assert "legacy_base" in html
    assert "No live orders" in html or "no live orders" in html.lower()


def test_briefing_includes_young_trail_line(yt):
    import cycle_history
    yt.consider_entries([_tok()], now=T0)
    yt.mark_open({_tok()["tid"]: 0.7}, now=T0 + 60)
    payload = cycle_history.build_briefing(now=T0 + 120)
    md = payload["markdown"]
    assert "## Young trail (paper, simulated)" in md
    assert "no live orders" in md
    assert "Realized:" in md
    assert "tp50" in md
    assert payload["young_trail"]["summary"]["closed_count"] == 1
    assert "SUPERSECRET" not in md


def test_protected_modules_untouched():
    for name in ("thresholds.py", "questions.py", "pick.py", "filter.py",
                 "collect.py", "judge.py"):
        r = subprocess.run(
            ["git", "diff", "--", name],
            cwd=str(ROOT), capture_output=True, text=True, check=True,
        )
        assert r.stdout == "", name
    src = (ROOT / "thresholds.py").read_text()
    assert "PICK_MIN_WORTH  = 0.60" in src
    yt_src = (ROOT / "young_trail.py").read_text()
    for banned in ("from filter", "from pick", "from questions", "from thresholds",
                   "send_to_seats", "book.take"):
        assert banned not in yt_src


def test_no_live_order_hooks_in_main():
    src = (ROOT / "main.py").read_text()
    assert "_young_trail_hook" in src
    # Hook sits beside history, never on the live send path.
    send_idx = src.find("desk.send_to_seats")
    hook_idx = src.find("_young_trail_hook(stats=stats")
    assert hook_idx != -1
    assert send_idx == -1 or hook_idx < send_idx or "send_to_seats" not in src.split("def main")[0]


def test_decide_exit_helpers(yt):
    assert yt.decide_exit(1.0, 1.0, 0.7, T0, T0 + 10) == ("sl", 0.7)
    assert yt.decide_exit(1.0, 2.0, 1.4, T0, T0 + 10) == ("trail", 1.4)
    assert yt.decide_exit(1.0, 2.0, 1.41, T0, T0 + 10) is None
    assert yt.decide_exit(1.0, 1.1, 1.05, T0, T0 + 6 * 3600) == ("time", 1.05)
    assert yt.decide_exit(1.0, 2.0, None, T0, T0 + 6 * 3600, last_price=0.9) == ("time", 0.9)
    assert yt.decide_exit(1.0, 2.0, None, T0, T0 + 6 * 3600) == ("time", 1.0)


def test_dex_lookups_timeout_and_cycle_budget(yt):
    assert yt.HTTP_TIMEOUT == 5
    assert yt.DEX_CYCLE_BUDGET_SEC == 20.0
    clock = {"t": T0}
    yt.set_time_fn(lambda: clock["t"])
    calls = []

    class Resp:
        def __init__(self, addr):
            self.status_code = 200
            self._addr = addr
        def json(self):
            return [{
                "liquidity": {"usd": 50_000},
                "priceUsd": "1.0",
                "baseToken": {"address": self._addr},
            }]

    def fake_get(url, timeout=None, headers=None):
        assert timeout == 5
        calls.append(url)
        clock["t"] += 8.0
        return Resp(url.rsplit("/", 1)[-1])

    yt.set_get_fn(fake_get)
    tids = [f"Tok{i}:1399811149" for i in range(6)]
    out = yt.fetch_dex_prices(tids, cap=10)
    # t=0, 8, 16 proceed; t=24 exceeds 20s budget so the rest skip.
    assert len(calls) == 3
    assert len(out) == 3


def test_dex_priceusd_preferred_when_cycle_price_missing(yt):
    t = _tok(price=None)
    t.pop("price_usd")
    mint = t["tid"].split(":")[0]
    class Resp:
        status_code = 200
        def json(self):
            return [{
                "pairAddress": "pool1",
                "liquidity": {"usd": 90_000},
                "priceUsd": "0.0042",
                "baseToken": {"address": mint},
            }]
    yt.set_get_fn(lambda *a, **k: Resp())
    out = yt.on_cycle(stats={"young_free": [t]}, tokens=[t], now=T0)
    assert out["entries"] == 1
    assert yt.open_rows()[0]["entry_price"] == pytest.approx(0.0042)


def test_never_logs_keys(yt, caplog, monkeypatch):
    monkeypatch.setenv("COINGECKO_API_KEY", "cg-secret-should-never-appear")
    monkeypatch.setenv("DESK_SECRET", "desk-secret-should-never-appear")
    caplog.set_level("INFO")
    yt.consider_entries([_tok()], now=T0)
    yt.mark_open({_tok()["tid"]: 0.7}, now=T0 + 60)
    blob = caplog.text
    assert "cg-secret-should-never-appear" not in blob
    assert "desk-secret-should-never-appear" not in blob


def test_new_entry_tp50_on_60s_sample(yt):
    """A new entry that hits +50% on a 60s sample closes as tp_50."""
    import young_trail_fast as fast
    import young_trail_variants as ytv
    t = _tok()
    assert yt.configured_exit_policy() == "tp50"
    assert yt.consider_entries([t], now=T0) == 1
    row = yt.open_rows()[0]
    assert row["exit_policy"] == "tp50"
    assert ytv.get_variant(t["tid"], "legacy_base") is not None
    assert ytv.get_variant(t["tid"], "legacy_base")["exit_ts"] is None
    fast.set_time_fn(lambda: T0 + 60)
    fast.on_sampler_tick({t["tid"]: 1.50}, now=T0 + 60)
    closed = yt.closed_rows()
    assert len(closed) == 1
    assert closed[0]["exit_reason"] == "tp_50"
    assert closed[0]["exit_price"] == pytest.approx(1.50)
    assert closed[0]["pnl_usd"] == pytest.approx(25.0)
    assert closed[0]["exit_policy"] == "tp50"
    shadow = ytv.get_variant(t["tid"], "legacy_base")
    assert shadow is not None
    assert shadow["exit_ts"] is None
    sm = yt.summary()
    assert sm["by_exit_policy"]["tp50"]["closed_count"] == 1
    assert sm["by_exit_policy"]["legacy"]["closed_count"] == 0
    assert sm["by_exit_policy"]["combined"]["realized_pnl_usd"] == pytest.approx(25.0)


def test_pre_deploy_open_keeps_legacy_rules(yt, monkeypatch):
    """A pre-deploy open position keeps the legacy rules."""
    import young_trail_fast as fast
    monkeypatch.setenv("YOUNG_TRAIL_EXIT_POLICY", "legacy")
    t = _tok()
    assert yt.consider_entries([t], now=T0) == 1
    row = yt.open_rows()[0]
    assert row["exit_policy"] == "legacy"
    monkeypatch.setenv("YOUNG_TRAIL_EXIT_POLICY", "tp50")
    fast.set_time_fn(lambda: T0 + 60)
    fast.on_sampler_tick({t["tid"]: 1.50}, now=T0 + 60)
    still = yt.open_rows()
    assert still and still[0]["exit_ts"] is None
    assert still[0]["peak"] == pytest.approx(ENTRY)
    yt.mark_open({t["tid"]: 0.70}, now=T0 + 180)
    closed = yt.closed_rows()
    assert len(closed) == 1
    assert closed[0]["exit_reason"] == "sl"
    assert closed[0]["exit_policy"] == "legacy"


def test_legacy_base_variant_recorded_for_new_entries(yt):
    """The legacy_base variant is recorded for new entries."""
    import young_trail_variants as ytv
    t = _tok()
    yt.consider_entries([t], now=T0)
    shadow = ytv.get_variant(t["tid"], "legacy_base")
    assert shadow is not None
    assert shadow["exit_ts"] is None
    assert shadow["peak"] == pytest.approx(ENTRY)
    cmp_rows = yt.policy_comparison()
    assert cmp_rows and cmp_rows[0]["tid"] == t["tid"]
    assert cmp_rows[0]["exit_policy"] == "tp50"
    payload = yt.ops_payload()
    assert payload["exit_policy"] == "tp50"
    assert payload["legacy_base"]["variant"] == "legacy_base"
    assert payload["legacy_base"]["open_count"] == 1
    yt.mark_open({t["tid"]: 0.70}, now=T0 + 60)
    live = yt.closed_rows()[0]
    assert live["exit_reason"] == "sl"
    shadow = ytv.get_variant(t["tid"], "legacy_base")
    assert shadow["exit_reason"] == "sl"
    assert shadow["pnl_usd"] == pytest.approx(live["pnl_usd"])
