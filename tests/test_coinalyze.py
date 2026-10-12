"""Deterministic Coinalyze regime capture. Fake HTTP, no network, no real sleeps."""
from __future__ import annotations

import json
import logging
import os
import pathlib
import sys
import time
from unittest.mock import Mock

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DESK_SECRET", "test-secret-for-coinalyze")
os.environ.setdefault("JUDGE_MOCK", "1")
os.environ["TZ"] = "UTC"
if hasattr(time, "tzset"):
    time.tzset()


def ts(y, m, d, hh=12, mm=0, ss=0) -> float:
    return time.mktime((y, m, d, hh, mm, ss, 0, 0, -1))


WATCH = ("BTC", "SOL", "BNB", "WIF", "BONK", "POPCAT")


def _markets():
    rows = []
    for ticker in WATCH:
        rows.append({
            "symbol": f"{ticker}USDT_PERP.A",
            "exchange": "A",
            "symbol_on_exchange": f"{ticker}USDT",
            "base_asset": ticker,
            "quote_asset": "USDT",
            "is_perpetual": True,
            "margined": "STABLE",
            "has_long_short_ratio_data": True,
        })
        rows.append({
            "symbol": f"{ticker}USD_PERP.0",
            "exchange": "0",
            "base_asset": ticker,
            "quote_asset": "USD",
            "is_perpetual": True,
            "margined": "COIN",
            "has_long_short_ratio_data": False,
        })
    return rows


def _current(values_by_ticker):
    return [
        {"symbol": f"{t}USDT_PERP.A", "value": values_by_ticker[t], "update": 1}
        for t in WATCH
    ]


def _oi(change_by_ticker):
    out = []
    for t in WATCH:
        first = 100.0
        last = first * (1.0 + change_by_ticker[t])
        out.append({
            "symbol": f"{t}USDT_PERP.A",
            "history": [
                {"t": 1, "o": first, "h": first, "l": first, "c": first},
                {"t": 2, "o": last, "h": last, "l": last, "c": last},
            ],
        })
    return out


def _liq(long_by_ticker, short_by_ticker):
    return [{
        "symbol": f"{t}USDT_PERP.A",
        "history": [{"t": 1, "l": long_by_ticker[t], "s": short_by_ticker[t]}],
    } for t in WATCH]


def _lsr(ratio_by_ticker):
    return [{
        "symbol": f"{t}USDT_PERP.A",
        "history": [{"t": 1, "r": ratio_by_ticker[t], "l": 55, "s": 45}],
    } for t in WATCH]


def _risk_on_routes():
    pos = {t: 0.01 for t in WATCH}
    return {
        "/future-markets": _markets(),
        "/funding-rate": _current(pos),
        "/predicted-funding-rate": _current({t: 0.012 for t in WATCH}),
        "/open-interest-history": _oi({t: 0.05 for t in WATCH}),
        "/liquidation-history": _liq({t: 30.0 for t in WATCH}, {t: 20.0 for t in WATCH}),
        "/long-short-ratio-history": _lsr({t: 1.2 for t in WATCH}),
    }


def _risk_off_routes():
    routes = _risk_on_routes()
    routes["/funding-rate"] = _current({t: -0.02 for t in WATCH})
    routes["/open-interest-history"] = _oi({t: -0.04 for t in WATCH})
    return routes


def _long_flush_routes():
    routes = _risk_on_routes()
    routes["/liquidation-history"] = _liq(
        {t: 90.0 for t in WATCH}, {t: 10.0 for t in WATCH},
    )
    return routes


def _neutral_routes():
    routes = _risk_on_routes()
    routes["/funding-rate"] = _current({
        "BTC": 0.01, "SOL": 0.01, "BNB": 0.01,
        "WIF": -0.01, "BONK": -0.01, "POPCAT": -0.01,
    })
    routes["/open-interest-history"] = _oi({t: 0.0 for t in WATCH})
    return routes


class FakeHTTP:
    def __init__(self, routes, clock=None):
        self.routes = routes
        self.calls = []
        self.clock = clock
        self.headers_seen = []

    def __call__(self, url, headers=None, params=None, timeout=None):
        assert "api_key=" not in str(url)
        assert "api_key" not in (params or {})
        headers = headers or {}
        self.headers_seen.append(dict(headers))
        path = url.split("coinalyze.net/v1", 1)[-1]
        path = path.split("?", 1)[0]
        self.calls.append({"path": path, "params": dict(params or {}), "timeout": timeout})
        body = self.routes[path]
        if isinstance(body, Exception):
            raise body
        resp = Mock()
        resp.status_code = 200
        resp.json = lambda: body
        resp.raise_for_status = lambda: None
        return resp


class FakeClock:
    def __init__(self, start):
        self.now = float(start)

    def time(self):
        return self.now

    def advance(self, seconds):
        self.now += seconds


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
        "seen": 20, "benched": 4, "judged": 2, "requeued": 1,
        "free": {"age": 6}, "trade": {}, "chain": {}, "soft": {},
        "carry": 0, "unevaluated": 0, "tokens": [], "young_free": [],
    }
    base.update(over)
    return base


def _arm(monkeypatch, routes, now, clock=None):
    import coinalyze
    clock = clock or FakeClock(now)
    http = FakeHTTP(routes, clock=clock)
    monkeypatch.setenv("COINALYZE_API_KEY", "test-coinalyze-key")
    coinalyze.set_time_fn(clock.time)
    coinalyze.set_get_fn(http)
    return http, clock


def test_resolve_prefers_binance_usdt_perp():
    import coinalyze
    mapping = coinalyze.resolve_symbols(_markets())
    assert mapping["BTC"]["symbol"] == "BTCUSDT_PERP.A"
    assert mapping["POPCAT"]["symbol"] == "POPCATUSDT_PERP.A"
    assert mapping["BONK"]["exchange"] == "A"


def test_derive_regime_rules():
    import coinalyze

    def metrics(long_share=None, majors=None, memes=None, oi=None, long_usd=0, short_usd=0):
        return {"aggregates": {
            "long_liq_share": long_share,
            "long_liq_usd": long_usd,
            "short_liq_usd": short_usd,
            "majors_funding_median": majors,
            "memes_funding_median": memes,
            "oi_change_median": oi,
        }}

    assert coinalyze.derive_regime(metrics(0.80, 0.01, 0.01, 0.05, 80, 20))[0] == "long_flush"
    assert coinalyze.derive_regime(metrics(0.40, 0.01, 0.02, 0.05, 40, 60))[0] == "risk_on"
    assert coinalyze.derive_regime(metrics(0.40, -0.01, 0.02, 0.05, 40, 60))[0] == "risk_off"
    assert coinalyze.derive_regime(metrics(0.40, 0.01, -0.02, -0.05, 40, 60))[0] == "risk_off"
    assert coinalyze.derive_regime(metrics(0.40, 0.01, -0.02, 0.0, 40, 60))[0] == "neutral"
    assert coinalyze.derive_regime(metrics())[0] == "neutral"


def test_skip_no_key_one_info(hist, caplog):
    import coinalyze
    with caplog.at_level(logging.INFO):
        assert coinalyze.maybe_capture(now=ts(2026, 10, 10, 8)) is None
        hist.on_cycle(_stats(), now=ts(2026, 10, 10, 8), source="live")
    skips = [r.message for r in caplog.records if r.message.startswith("coinalyze: skipped")]
    assert skips
    assert all("no key" in m for m in skips)
    assert hist._regimes_since(0) == []


def test_skip_error_one_info_never_fails_cycle(hist, monkeypatch, caplog):
    import coinalyze
    monkeypatch.setenv("COINALYZE_API_KEY", "secret-key-xyz")
    coinalyze.set_get_fn(lambda *a, **k: (_ for _ in ()).throw(RuntimeError("boom api_key=secret-key-xyz")))
    with caplog.at_level(logging.INFO):
        cid = hist.on_cycle(_stats(), now=ts(2026, 10, 10, 8), source="live")
    assert cid is not None
    assert hist.cycles_since(0)[0]["seen"] == 20
    skips = [r.message for r in caplog.records if r.message.startswith("coinalyze: skipped")]
    assert len(skips) == 1
    assert "secret-key-xyz" not in caplog.text
    assert "REDACTED" in caplog.text or "boom" in caplog.text


def test_skip_missing_key_does_not_log_key(hist, monkeypatch, caplog):
    monkeypatch.setenv("COINALYZE_API_KEY", "")
    with caplog.at_level(logging.INFO):
        hist.on_cycle(_stats(), now=ts(2026, 10, 10, 8), source="live")
    assert "COINALYZE_API_KEY" not in caplog.text or "skipped (no key)" in caplog.text
    assert "test-coinalyze-key" not in caplog.text


def test_capture_stores_regime_and_raw_metrics(hist, monkeypatch, caplog):
    now = ts(2026, 10, 10, 8)
    http, _clock = _arm(monkeypatch, _risk_on_routes(), now)
    with caplog.at_level(logging.INFO):
        cid = hist.on_cycle(_stats(), now=now, source="live")
    assert cid is not None
    rows = hist._regimes_since(0)
    assert len(rows) == 1
    assert rows[0]["cycle_id"] == cid
    assert rows[0]["regime"] == "risk_on"
    metrics = rows[0]["metrics"]
    assert metrics["assets"]["BTC"]["funding"] == pytest.approx(0.01)
    assert metrics["assets"]["WIF"]["predicted_funding"] == pytest.approx(0.012)
    assert metrics["assets"]["SOL"]["oi_change_pct"] == pytest.approx(0.05)
    assert metrics["raw"]["funding"]
    assert 5 <= metrics["http_calls"] <= 10
    assert len(http.calls) <= 10
    assert http.calls[0]["path"] == "/future-markets"
    paths = [c["path"] for c in http.calls]
    assert "/funding-rate" in paths
    assert "/predicted-funding-rate" in paths
    assert "/open-interest-history" in paths
    assert "/liquidation-history" in paths
    assert "/long-short-ratio-history" in paths
    assert any(c["params"].get("convert_to_usd") == "true" for c in http.calls
               if c["path"] == "/open-interest-history")
    assert "coinalyze: regime=risk_on" in caplog.text
    assert "test-coinalyze-key" not in caplog.text
    assert all(h.get("api_key") == "test-coinalyze-key" for h in http.headers_seen)


def test_symbol_cache_skips_future_markets_second_cycle(hist, monkeypatch):
    now = ts(2026, 10, 10, 8)
    http, clock = _arm(monkeypatch, _risk_on_routes(), now)
    hist.on_cycle(_stats(), now=now, source="live")
    first_markets = sum(1 for c in http.calls if c["path"] == "/future-markets")
    assert first_markets == 1
    clock.advance(901)
    hist.on_cycle(_stats(), now=now + 901, source="live")
    assert sum(1 for c in http.calls if c["path"] == "/future-markets") == 1
    assert len(hist._regimes_since(0)) == 2


def test_cooldown_skips_within_15_min(hist, monkeypatch, caplog):
    now = ts(2026, 10, 10, 8)
    http, clock = _arm(monkeypatch, _risk_on_routes(), now)
    hist.on_cycle(_stats(), now=now, source="live")
    n_calls = len(http.calls)
    clock.advance(60)
    with caplog.at_level(logging.INFO):
        hist.on_cycle(_stats(), now=now + 60, source="live")
    assert len(http.calls) == n_calls
    assert any("cooldown" in r.message for r in caplog.records)


def test_backfill_does_not_call_coinalyze(hist, monkeypatch):
    now = ts(2026, 10, 10, 8)
    http, _clock = _arm(monkeypatch, _risk_on_routes(), now)
    hist.on_cycle(_stats(), now=now, source="backfill")
    assert http.calls == []
    assert hist._regimes_since(0) == []


def test_timeout_does_not_fail_cycle(hist, monkeypatch, caplog):
    now = ts(2026, 10, 10, 8)
    http, _clock = _arm(monkeypatch, _risk_on_routes(), now)
    import coinalyze

    def boom(*a, **k):
        raise TimeoutError("coinalyze overall deadline")

    coinalyze.set_get_fn(boom)
    with caplog.at_level(logging.INFO):
        cid = hist.on_cycle(_stats(), now=now, source="live")
    assert cid is not None
    assert hist.cycles_since(0)
    assert any("skipped" in r.message for r in caplog.records if r.name == "coinalyze")


def test_http_429_skips(hist, monkeypatch, caplog):
    now = ts(2026, 10, 10, 8)
    resp = Mock()
    resp.status_code = 429
    resp.json = lambda: {"message": "slow down"}
    resp.raise_for_status = lambda: None
    monkeypatch.setenv("COINALYZE_API_KEY", "k")
    import coinalyze
    coinalyze.set_time_fn(lambda: now)
    coinalyze.set_get_fn(lambda *a, **k: resp)
    with caplog.at_level(logging.INFO):
        assert coinalyze.maybe_capture(now=now) is None
    assert any("429" in r.message for r in caplog.records)


def test_long_flush_and_risk_off_tags(hist, monkeypatch):
    now = ts(2026, 10, 10, 8)
    _arm(monkeypatch, _long_flush_routes(), now)
    hist.on_cycle(_stats(), now=now, source="live")
    assert hist._regimes_since(0)[0]["regime"] == "long_flush"

    import coinalyze
    coinalyze.reset(hooks=False)
    later = now + 901
    _arm(monkeypatch, _risk_off_routes(), later)
    hist.on_cycle(_stats(), now=later, source="live")
    assert hist._regimes_since(0)[-1]["regime"] == "risk_off"


def test_neutral_tag(hist, monkeypatch):
    now = ts(2026, 10, 10, 8)
    _arm(monkeypatch, _neutral_routes(), now)
    hist.on_cycle(_stats(), now=now, source="live")
    assert hist._regimes_since(0)[0]["regime"] == "neutral"


def test_briefing_and_trends_include_regime(hist, monkeypatch):
    http = None
    for day in range(2, 9):
        now = ts(2026, 10, day, 0, 30)
        http, _clock = _arm(monkeypatch, _risk_on_routes(), now)
        import coinalyze
        coinalyze.reset(hooks=False)
        coinalyze.set_time_fn(lambda t=now: t)
        coinalyze.set_get_fn(http)
        hist.on_cycle(
            _stats(seen=20, benched=0, judged=4, free={"age": 10},
                   tokens=[{
                       "tid": f"p{day}:1399811149", "ticker": f"P{day}",
                       "net": 1399811149, "stage": "judged", "age_minutes": 40,
                       "soft_scores": {"momentum_already_spent": 0.40},
                   }] * 4),
            now=now, source="backfill",
        )
        # backfill skips capture — write the row as if a live capture happened
        db = hist._connect()
        db.execute(
            """INSERT INTO market_regimes (cycle_id, ts, ts_iso, regime, metrics_json, symbols_json)
               VALUES (?,?,?,?,?,?)""",
            (hist.cycles_since(0)[-1]["id"], now,
             time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now)),
             "risk_on", json.dumps({"aggregates": {"majors_funding_median": 0.01},
                                    "reason": "seed", "rules": "test"}),
             json.dumps({})),
        )
        db.commit()
    today = ts(2026, 10, 9, 7, 30)
    http, _clock = _arm(monkeypatch, _risk_off_routes(), today)
    hist.on_cycle(
        _stats(seen=20, benched=0, judged=4, free={"age": 1}),
        now=today, source="live",
    )
    payload = hist.build_briefing(ts(2026, 10, 9, 8, 0))
    assert payload["last_24h"]["regime"]["tag"] == "risk_off"
    assert "## Market regime (log-only)" in payload["markdown"]
    assert "risk_off" in payload["markdown"]
    assert "Regime per day:" in payload["markdown"]
    days = payload["trends"]["days"]
    assert days[0]["date"] == "2026-10-02"
    assert days[0]["regime"] == "risk_on"
    assert days[-1]["regime"] == "risk_on"


def test_ops_briefing_json_has_regime(hist, monkeypatch):
    now = time.time() - 30
    _arm(monkeypatch, _risk_on_routes(), now)
    hist.on_cycle(_stats(judged=1, tokens=[{
        "tid": "x:1399811149", "ticker": "XX", "net": 1399811149,
        "stage": "judged", "age_minutes": 21,
        "soft_scores": {"momentum_already_spent": 0.3},
    }]), now=now, source="live")
    from fastapi.testclient import TestClient
    import server
    data = TestClient(server.app).get("/ops/briefing").json()
    assert data["last_24h"]["regime"]["tag"] == "risk_on"
    assert "SUPERSECRET" not in json.dumps(data)
    assert "test-coinalyze-key" not in json.dumps(data)


def test_zero_effect_on_filter_and_thresholds():
    for name in ("filter.py", "pick.py", "thresholds.py", "questions.py", "judge.py"):
        src = (ROOT / name).read_text()
        assert "coinalyze" not in src
        assert "COINALYZE" not in src
        assert "risk_on" not in src
        assert "long_flush" not in src


def test_run_once_unaffected_by_regime(hist, monkeypatch, caplog):
    import book
    import collect
    import main as shift

    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.execute("DELETE FROM carry")
    book.DB.commit()

    now = ts(2026, 10, 10, 8)
    _arm(monkeypatch, _long_flush_routes(), now)
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
        def read_x(self, h, tid=None, token=None): return None
        def log_shadow(self, o, s, fomo_data=None): pass
        def report(self, o, s): pass
        def send_to_seats(self, o): pass

    caplog.clear()
    with caplog.at_level(logging.INFO):
        order, stats = shift.run_once(
            type("F", (), {"token": lambda self: None})(),
            fake_judge, Desk(), 1000.0, shadow=True, gt_dossier_reserve=3,
        )
    assert order is None
    assert stats["soft"].get("concentration_is_exit_risk") == 1
    cycle_logs = [r.message for r in caplog.records if r.message.startswith("cycle:")]
    assert cycle_logs
    soft_logs = [r.message for r in caplog.records if r.message.startswith("soft tid=")]
    assert soft_logs and "noul=0.75" in soft_logs[0]
    assert hist._regimes_since(0)
    assert hist._regimes_since(0)[0]["regime"] == "long_flush"


def test_env_example_has_placeholder():
    text = (ROOT / ".env.example").read_text()
    assert "COINALYZE_API_KEY=" in text


def test_no_sleep_import_used_for_backoff():
    src = (ROOT / "coinalyze.py").read_text()
    assert "time.sleep" not in src
    assert "thresholds.py" not in src
