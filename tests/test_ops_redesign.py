"""/ops redesign: summary shape, compact briefing, bounded log tail, no secrets.

Does not change thresholds, questions, pick, filter, collect, or young_trail exits.
"""
import json
import os
import pathlib
import re
import sys
import time

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DESK_SECRET", "test-secret-for-ops-redesign")
os.environ.setdefault("JUDGE_MOCK", "1")
os.environ["TZ"] = "UTC"
if hasattr(time, "tzset"):
    time.tzset()


def ts(y, m, d, hh=12, mm=0, ss=0) -> float:
    return time.mktime((y, m, d, hh, mm, ss, 0, 0, -1))


@pytest.fixture
def ops(tmp_path, monkeypatch):
    os.environ["TZ"] = "UTC"
    if hasattr(time, "tzset"):
        time.tzset()
    monkeypatch.setenv("DESK_OUTBOX", str(tmp_path))
    monkeypatch.setenv("CYCLE_HISTORY_DB", str(tmp_path / "cycle_history.db"))
    monkeypatch.setenv("OUTCOMES_DB", str(tmp_path / "outcomes.db"))
    monkeypatch.delenv("CONFIRM_LIVE", raising=False)
    monkeypatch.setenv("JUDGE_MOCK", "1")
    import cycle_history
    import outcomes
    import ops_summary
    import young_trail
    cycle_history.reset()
    outcomes.reset()
    ops_summary.reset()
    young_trail.reset()
    yield {
        "tmp": tmp_path,
        "cycle_history": cycle_history,
        "outcomes": outcomes,
        "ops_summary": ops_summary,
        "young_trail": young_trail,
    }
    cycle_history.reset()
    outcomes.reset()
    ops_summary.reset()
    young_trail.reset()


def _stats(**over):
    base = {
        "seen": 8, "benched": 1, "judged": 1, "requeued": 0,
        "free": {}, "trade": {}, "chain": {}, "soft": {},
        "tokens": [], "young_free": [],
    }
    base.update(over)
    return base


def _client():
    from fastapi.testclient import TestClient
    import server
    return TestClient(server.app)


FAKE_SECRETS = {
    "COINGECKO_API_KEY": "cg-FAKESECRET-should-never-appear",
    "TYPESAFE_API_KEY": "ts-FAKESECRET-should-never-appear",
    "DESK_SECRET": "desk-secret-xyz-never",
    "FOMO_BEARER": "fomo-bearer-secret-xyz",
    "COINALYZE_API_KEY": "coinalyze-secret-xyz",
}


def test_ops_summary_shape_and_no_secrets(ops, monkeypatch):
    hist = ops["cycle_history"]
    now = ts(2026, 10, 14, 0, 20)
    hist.on_cycle(_stats(tokens=[{
        "tid": "xWy9yhYxsibHdd1CD71Qgv87uVrhsmtbvRQVXfWpump:1399811149",
        "ticker": "CHILL",
        "net": 1399811149,
        "chain": "solana",
        "stage": "judged",
        "judge_score": 0.64,
        "judge_confidence": 0.58,
        "judge_verdict": "pick",
    }]), now=now - 3600, source="backfill")
    hist.on_cycle(_stats(judged=0, tokens=[]), now=now - 1800, source="backfill")
    db = hist._connect()
    db.execute(
        """INSERT INTO market_regimes (cycle_id, ts, ts_iso, regime, metrics_json, symbols_json)
           VALUES (1, ?, ?, 'long_flush', ?, '{}')""",
        (now - 60, "2026-10-14T00:03:35Z", json.dumps({
            "reason": "long-liq share 0.95 >= 0.70",
            "long_liq_usd": 6225002,
            "short_liq_usd": 327433,
            "majors_funding_median": 0.0019,
            "memes_funding_median": 0.005,
            "oi_change_median": -0.0026,
        })),
    )
    db.commit()
    for name, val in FAKE_SECRETS.items():
        monkeypatch.setenv(name, val)
    monkeypatch.setenv("COINGECKO_PLAN", "demo")
    ops["ops_summary"].set_launchctl_fn(
        lambda: "97914\t0\tcom.robbrown.jevdesk.desk\n97923\t0\tcom.robbrown.jevdesk.ops\n"
    )
    payload = ops["ops_summary"].summary_payload(now=now, force=True)
    blob = json.dumps(payload)
    for val in FAKE_SECRETS.values():
        assert val not in blob
    assert "cg-FAKESECRET" not in blob
    assert payload["health"]["desk"]["loaded"] is True
    assert payload["health"]["desk"]["pid"] == 97914
    assert payload["health"]["ops"]["loaded"] is True
    assert payload["health"]["mode"] == "shadow"
    assert payload["health"]["judge_mode"] == "mock"
    assert payload["health"]["cycles_24h"] == 2
    assert payload["health"]["shadow_24h"] == 1
    assert payload["health"]["no_trade_24h"] == 1
    assert isinstance(payload["budget"]["cg_key_present"], bool)
    assert payload["budget"]["cg_key_present"] is True
    assert payload["budget"]["cg_plan"] == "demo"
    hi = payload["judge_hi"]
    assert any(j["ticker"] == "CHILL" and j["score"] == pytest.approx(0.64) for j in hi)
    assert payload["regime"]["tag"] == "long_flush"
    assert payload["regime"]["long_liq_usd"] == pytest.approx(6225002)
    assert isinstance(payload["young_trail_curve"], list)
    assert set(payload) >= {
        "generated_at_iso", "health", "judge_hi", "outcomes",
        "budget", "regime", "young_trail_curve",
    }
    resp = _client().get("/ops/summary")
    assert resp.status_code == 200
    data = resp.json()
    assert "health" in data
    assert "judge_hi" in data
    for val in FAKE_SECRETS.values():
        assert val not in json.dumps(data)


def test_tail_scan_bounded(ops):
    huge = ops["tmp"] / "run.log"
    secret = "SECRET_HEAD_SHOULD_NOT_BE_SCANNED"
    with huge.open("wb") as f:
        f.write(
            f"2020-01-01 00:00:00,000 desk ERROR {secret} "
            "Traceback (most recent call last):\n".encode()
        )
        f.write(b"X" * (8 * 1024 * 1024))
        f.write(
            b"2026-10-14 00:00:00,000 desk ERROR boom\n"
            b"Traceback (most recent call last):\n"
            b"  File desk.py, line 1\n"
            b"ZeroDivisionError: boom\n"
        )
    text = ops["ops_summary"].read_log_tail(huge)
    assert secret not in text
    assert "ZeroDivisionError" in text
    assert len(text.encode("utf-8", errors="replace")) <= 5 * 1024 * 1024 + 4096
    now = ts(2026, 10, 14, 0, 20)
    n, last = ops["ops_summary"].scan_tracebacks(text, now)
    assert n >= 1
    assert last is not None
    assert "Traceback" in (last.get("first_line") or "")


def test_briefing_compact_size(ops):
    hist = ops["cycle_history"]
    now = ts(2026, 10, 14, 8)
    tokens = [{
        "tid": f"tok{i}:1399811149",
        "ticker": f"T{i}",
        "net": 1399811149,
        "stage": "judged",
        "age_minutes": 21,
        "soft_scores": {"momentum_already_spent": 0.3},
        "judge_score": 0.2,
        "judge_verdict": "pass",
    } for i in range(40)]
    hist.on_cycle(_stats(judged=40, tokens=tokens), now=now - 10, source="backfill")
    full = hist.briefing_payload(now)
    compact = hist.briefing_payload(now, compact=True)
    assert set(compact) <= {"date_local", "generated_at_iso", "markdown", "morning"}
    assert "last_24h" not in compact
    assert "trends" not in compact
    assert compact["markdown"]
    assert compact["date_local"]
    fat = dict(full)
    fat["last_24h"] = {"tokens": ["x" * 2000] * 400}
    assert len(json.dumps(fat)) > 50_000
    assert len(json.dumps(compact)) < 50_000
    resp = _client().get("/ops/briefing?compact=1")
    assert resp.status_code == 200
    data = resp.json()
    assert "last_24h" not in data
    assert "markdown" in data
    assert len(resp.content) < 50_000
    full_resp = _client().get("/ops/briefing")
    assert full_resp.status_code == 200
    assert "last_24h" in full_resp.json()


def test_ops_html_no_external_urls():
    html = (ROOT / "ops.html").read_text()
    # Same-origin fetch paths only. No CDN fonts/scripts, no http(s) href/src.
    assert not re.search(r"""(?:src|href)\s*=\s*['"]https?://""", html, re.I)
    assert "https://" not in html
    assert "http://" not in html
    assert "fonts.googleapis" not in html
    assert "cdn." not in html
    assert "/ops/summary" in html
    assert "/ops/briefing?compact=1" in html
    assert 'id="fomo-fix-button"' in html
    assert "X-Ops-Action" in html
    assert "activate-fomo" in html


def test_ops_summary_fail_open(ops, monkeypatch):
    def boom(*_a, **_k):
        raise RuntimeError("db down key=cg-FAKESECRET-should-never-appear")

    monkeypatch.setenv("COINGECKO_API_KEY", "cg-FAKESECRET-should-never-appear")
    monkeypatch.setattr(ops["ops_summary"], "_cycle_health", boom)
    payload = ops["ops_summary"].summary_payload(now=ts(2026, 10, 14, 1), force=True)
    assert payload["judge_hi"] == []
    assert "health" in payload
    assert "cg-FAKESECRET-should-never-appear" not in json.dumps(payload)


def test_ops_summary_cache_30s(ops, monkeypatch):
    calls = {"n": 0}
    real = ops["ops_summary"].build_summary

    def wrapped(now=None):
        calls["n"] += 1
        return real(now)

    monkeypatch.setattr(ops["ops_summary"], "build_summary", wrapped)
    ops["ops_summary"].reset()
    a = ops["ops_summary"].summary_payload(now=ts(2026, 10, 14, 1))
    b = ops["ops_summary"].summary_payload(now=ts(2026, 10, 14, 1))
    assert calls["n"] == 1
    assert a is b
    c = ops["ops_summary"].summary_payload(now=ts(2026, 10, 14, 1), force=True)
    assert calls["n"] == 2
    assert c is not a


def test_launchctl_parse_no_kickstart(ops):
    text = (
        "PID\tStatus\tLabel\n"
        "-\t0\tcom.apple.foo\n"
        "97914\t0\tcom.robbrown.jevdesk.desk\n"
        "97923\t0\tcom.robbrown.jevdesk.ops\n"
    )
    parsed = ops["ops_summary"].parse_launchctl(text)
    assert parsed["desk"] == {"loaded": True, "pid": 97914}
    assert parsed["ops"] == {"loaded": True, "pid": 97923}
    src = (ROOT / "ops_summary.py").read_text()
    assert "kickstart" not in src.lower() or "no kickstart" in src.lower()
    assert '["launchctl", "list"]' in src
    assert "kickstart" not in src.split("no kickstart")[-1].lower() or "no kickstart" in src


def test_young_trail_curve_all_closes(ops):
    yt = ops["young_trail"]
    yt.set_time_fn(lambda: 1_700_000_000.0)
    yt.set_sleep_fn(lambda _d: None)
    t0 = 1_700_000_000.0
    for i, px in enumerate((0.7, 0.6, 0.5)):
        tok = {
            "tid": f"Y{i}:1399811149",
            "ticker": f"Y{i}",
            "chain": "solana",
            "net": 1399811149,
            "age_minutes": 20,
            "price_usd": 1.0,
        }
        yt.consider_entries([tok], now=t0 + i)
        yt.mark_open({tok["tid"]: px}, now=t0 + 60 + i)
    curve = ops["ops_summary"]._young_trail_curve()
    assert len(curve) == 3
    assert curve[0][1] == pytest.approx(-15.0)
    assert curve[1][1] == pytest.approx(-35.0)
    assert curve[2][1] == pytest.approx(-60.0)


def test_outcomes_counts_split_live_backfill(ops):
    oc = ops["outcomes"]
    now = ts(2026, 10, 14, 12)
    oc.enqueue_job("live1:1399811149", now - 8 * 3600, now=now, priority=0)
    oc.enqueue_job("bf1:1399811149", now - 9 * 3600, now=now, priority=1)
    db = oc._connect()
    db.execute(
        """INSERT INTO fetch_jobs
           (tid, entry_ts, status, attempts, last_attempt, enqueued_at, priority)
           VALUES ('done:1', 1, 'complete', 1, ?, ?, 0)""",
        (now - 100, now - 200),
    )
    db.execute(
        """INSERT INTO fetch_jobs
           (tid, entry_ts, status, attempts, last_attempt, enqueued_at, priority)
           VALUES ('dead:1', 2, 'dead', 5, ?, ?, 0)""",
        (now - 100, now - 200),
    )
    db.commit()
    out = ops["ops_summary"]._outcomes(now)
    assert out["pending_live"] == 1
    assert out["pending_backfill"] == 1
    assert out["complete"] == 1
    assert out["dead"] == 1
    assert out["complete_24h"] == 1
    assert out["window"]


def test_protected_logic_untouched():
    import subprocess
    for name in (
        "thresholds.py", "questions.py", "pick.py", "filter.py", "collect.py",
        "young_trail.py",
    ):
        r = subprocess.run(
            ["git", "diff", "--", name],
            cwd=str(ROOT), capture_output=True, text=True, check=True,
        )
        assert r.stdout == "", name
