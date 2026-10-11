"""
LOG-ONLY judge verdict visibility. No network, no real judge.

Covers the per-token `judge tid=` line, cycle_history columns, briefing, and
/ops section. Does not change filters, thresholds, or pick decisions.
"""
import logging
import os
import pathlib
import sqlite3
import sys
import time
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

os.environ.setdefault("DESK_SECRET", "test-secret-for-judge-verdicts")
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


class _Desk:
    def bank(self):
        return 1000.0

    def read_x(self, h):
        return None

    def log_shadow(self, o, s, fomo_data=None):
        pass

    def report(self, o, s):
        pass

    def send_to_seats(self, o):
        pass


def _clean_book():
    import book
    book.release()
    book.DB.execute("DELETE FROM defer")
    book.DB.execute("DELETE FROM bench")
    book.DB.execute("DELETE FROM carry")
    book.DB.commit()


def _pass_soft_answers():
    return {
        "concentration_is_exit_risk": {"type": "noul", "noul": 0.30},
        "momentum_already_spent": {"type": "noul", "noul": 0.25},
        "liquidity_fits_ticket": {"type": "noul", "noul": 0.72},
        "shape": {
            "type": "choice", "choice": "crowd", "confidence": 0.80,
            "probabilities": {"crowd": 0.80, "one_buyer": 0.10, "fading": 0.05, "too_early": 0.05},
        },
    }


def _solana_token(tid="PepperMint:1399811149", ticker="PPR", age=51.6):
    return {
        "addr": tid.split(":")[0], "net": 1399811149, "tid": tid, "ticker": ticker,
        "mcap_usd": 300_000, "liquidity_usd": 48_000, "volume_h24": 610_000,
        "price_usd": 0.001, "holder_count": 310,
        "change": {"5m": 0.04, "1h": 0.22, "4h": 0.4, "24h": 0.61},
        "age_minutes": age, "chain": "solana",
    }


def _wire_one_token(monkeypatch, token, judge):
    import collect
    import main as shift
    _clean_book()
    tid = token["tid"]

    def fake_shortlist(fomo, id_list):
        return [dict(token)]

    def fake_dossier(t, limiter=None):
        return {
            **t, "chain": "solana", "top_10_percent": 30, "top_wallet_percent": 0.02,
            "developer_holding_percentage": 2, "gt_score_details": None,
            "is_honeypot": None, "mint_authority": None, "freeze_authority": None,
            "description": "a token", "x_handle": None, "net": 1399811149,
        }

    monkeypatch.setattr(shift, "universe", lambda limiter=None, fomo=None: ([tid], {}))
    monkeypatch.setattr(shift, "shortlist", fake_shortlist)
    monkeypatch.setattr(collect, "shortlist", fake_shortlist)
    monkeypatch.setattr(shift, "trade_counts", lambda t, gt_txns_cache=None: (
        {"buys_h1": 540, "sells_h1": 120, "buys_h6": 900, "sells_h6": 400,
         "trades_h24": 4000}, "ok"))
    monkeypatch.setattr(shift, "dossier", fake_dossier)
    return shift


def test_truncate_judge_reason_is_about_300():
    import main as shift
    assert shift._truncate_judge_reason(None) == ""
    assert shift._truncate_judge_reason("short") == "short"
    long = "x" * 400
    out = shift._truncate_judge_reason(long)
    assert len(out) == 300
    assert out.endswith("...")
    assert "\n" not in shift._truncate_judge_reason("a\n\nb   c")


def test_judge_mode_mock_and_live(monkeypatch):
    import main as shift
    monkeypatch.setenv("JUDGE_MOCK", "1")
    assert shift._judge_mode() == "mock"
    monkeypatch.setenv("JUDGE_MOCK", "0")
    assert shift._judge_mode() == "live"
    monkeypatch.delenv("JUDGE_MOCK", raising=False)
    assert shift._judge_mode() == "live"


def test_schema_migration_adds_judge_columns(tmp_path, monkeypatch):
    db_path = tmp_path / "old.db"
    raw = sqlite3.connect(db_path)
    raw.executescript("""
    CREATE TABLE tokens (
      id INTEGER PRIMARY KEY AUTOINCREMENT,
      cycle_id INTEGER,
      ts REAL NOT NULL,
      kind TEXT NOT NULL,
      tid TEXT,
      chain_id INTEGER,
      chain TEXT,
      ticker TEXT,
      reason TEXT,
      noul REAL,
      age_minutes REAL,
      top_10_percent REAL,
      top_wallet_percent REAL,
      developer_holding_percentage REAL,
      holder_count INTEGER,
      soft_scores_json TEXT
    );
    """)
    raw.execute(
        "INSERT INTO tokens (ts, kind, tid, ticker, soft_scores_json) VALUES (1,'judged','t:1','OLD','{}')"
    )
    raw.commit()
    raw.close()

    monkeypatch.setenv("CYCLE_HISTORY_DB", str(db_path))
    import cycle_history
    cycle_history.reset()
    db = cycle_history._connect()
    cols = {row[1] for row in db.execute("PRAGMA table_info(tokens)")}
    assert "judge_verdict" in cols
    assert "judge_reason" in cols
    assert "judge_score" in cols
    assert "judge_confidence" in cols
    leftover = db.execute("SELECT ticker, judge_verdict FROM tokens").fetchone()
    assert leftover[0] == "OLD"
    assert leftover[1] is None
    cycle_history.reset()


def test_on_cycle_persists_judge_verdict_and_reason(hist):
    hist.on_cycle({
        "seen": 1, "benched": 0, "judged": 1, "requeued": 0,
        "free": {}, "trade": {}, "chain": {}, "soft": {},
        "carry": 0, "unevaluated": 0,
        "tokens": [{
            "tid": "pep:1399811149", "ticker": "PPR", "net": 1399811149,
            "chain": "solana", "stage": "judged", "reason": None,
            "age_minutes": 51.6,
            "soft_scores": {"momentum_already_spent": 0.25, "liquidity_fits_ticket": 0.72},
            "judge_verdict": "pass",
            "judge_reason": "worth_trading_at_all=0.2 below 0.6",
            "judge_score": 0.2,
            "judge_confidence": 0.9,
        }],
    }, now=ts(2026, 10, 10, 9, 33), source="backfill")
    tokens = hist._tokens_since(0)
    judged = [t for t in tokens if t["kind"] == "judged"]
    assert len(judged) == 1
    t = judged[0]
    assert t["ticker"] == "PPR"
    assert t["judge_verdict"] == "pass"
    assert t["judge_reason"] == "worth_trading_at_all=0.2 below 0.6"
    assert t["judge_score"] == pytest.approx(0.2)
    assert t["judge_confidence"] == pytest.approx(0.9)
    assert t["soft_scores"]["momentum_already_spent"] == pytest.approx(0.25)
    w = hist.summarize_window(0, ts(2026, 10, 10, 10))
    assert w["judged_tokens"][0]["judge_verdict"] == "pass"
    md = hist.build_briefing(ts(2026, 10, 10, 10))["markdown"]
    assert "## Judge verdicts" in md
    assert "PPR" in md
    assert "worth_trading_at_all=0.2 below 0.6" in md
    assert "momentum_already_spent=0.250" in md


def test_run_once_logs_pass_when_pick_declines(hist, monkeypatch, caplog):
    token = _solana_token()

    def fake_judge(question_set, state):
        if question_set == "market":
            return {"model": "test", "answers": _pass_soft_answers(), "usage": {}}
        if question_set == "pick":
            return {"model": "test", "answers": {
                "best": {"type": "choice", "choice": "PPR", "confidence": 0.90,
                         "probabilities": {"PPR": 0.90}},
                "worth_trading_at_all": {"type": "noul", "noul": 0.20},
            }, "usage": {}}
        return {"model": "test", "answers": {}, "usage": {}}

    shift = _wire_one_token(monkeypatch, token, fake_judge)
    monkeypatch.setenv("JUDGE_MOCK", "1")
    caplog.clear()
    with caplog.at_level(logging.INFO):
        order, stats = shift.run_once(
            type("F", (), {"token": lambda self: None})(),
            fake_judge, _Desk(), 1000.0, shadow=True, gt_dossier_reserve=3,
        )
    assert order is None
    assert stats["judged"] == 1
    judged = [t for t in stats["tokens"] if t.get("stage") == "judged"]
    assert len(judged) == 1
    assert judged[0]["judge_verdict"] == "pass"
    assert "worth_trading_at_all" in (judged[0].get("judge_reason") or "")
    assert judged[0]["soft_scores"]["momentum_already_spent"] == pytest.approx(0.25)

    lines = [r.message for r in caplog.records if r.message.startswith("judge tid=")]
    assert len(lines) == 1
    line = lines[0]
    assert "tid=PepperMint:1399811149" in line
    assert "ticker=PPR" in line
    assert "verdict=pass" in line
    assert "score=0.2" in line
    assert "confidence=0.9" in line
    assert "reason=" in line
    assert "mode=mock" in line
    assert "worth_trading_at_all" in line

    cycle_logs = [r.message for r in caplog.records if r.message.startswith("cycle:")]
    assert cycle_logs and "judged" in cycle_logs[0]
    stored = [t for t in hist._tokens_since(0) if t["kind"] == "judged"]
    assert len(stored) == 1
    assert stored[0]["judge_verdict"] == "pass"
    assert stored[0]["soft_scores"]["liquidity_fits_ticket"] == pytest.approx(0.72)


def test_run_once_logs_pick_when_chosen(hist, monkeypatch, caplog):
    token = _solana_token(ticker="WIN")

    def fake_judge(question_set, state):
        if question_set == "market":
            return {"model": "test", "answers": _pass_soft_answers(), "usage": {}}
        if question_set == "pick":
            return {"model": "test", "answers": {
                "best": {"type": "choice", "choice": "WIN", "confidence": 0.85,
                         "probabilities": {"WIN": 0.85}},
                "worth_trading_at_all": {"type": "noul", "noul": 0.75},
            }, "usage": {}}
        return {"model": "test", "answers": {}, "usage": {}}

    shift = _wire_one_token(monkeypatch, token, fake_judge)
    monkeypatch.setenv("JUDGE_MOCK", "0")
    caplog.clear()
    with caplog.at_level(logging.INFO):
        order, stats = shift.run_once(
            type("F", (), {"token": lambda self: None})(),
            fake_judge, _Desk(), 1000.0, shadow=True, gt_dossier_reserve=3,
        )
    assert order is None  # shadow
    judged = [t for t in stats["tokens"] if t.get("stage") == "judged"]
    assert judged[0]["judge_verdict"] == "pick"
    lines = [r.message for r in caplog.records if r.message.startswith("judge tid=")]
    assert len(lines) == 1
    assert "verdict=pick" in lines[0]
    assert "confidence=0.85" in lines[0]
    assert "mode=live" in lines[0]
    stored = [t for t in hist._tokens_since(0) if t["kind"] == "judged"]
    assert stored[0]["judge_verdict"] == "pick"
    assert "picked WIN" in (stored[0]["judge_reason"] or "")


def test_run_once_logs_error_with_safe_err(hist, monkeypatch, caplog):
    token = _solana_token(ticker="ERR")

    def fake_judge(question_set, state):
        raise TimeoutError("judge timeout api-key=SUPERSECRET123")

    shift = _wire_one_token(monkeypatch, token, fake_judge)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        order, stats = shift.run_once(
            type("F", (), {"token": lambda self: None})(),
            fake_judge, _Desk(), 1000.0, shadow=True, gt_dossier_reserve=3,
        )
    assert order is None
    assert stats["judged"] == 0
    lines = [r.message for r in caplog.records if r.message.startswith("judge tid=")]
    assert len(lines) == 1
    assert "verdict=error" in lines[0]
    assert "SUPERSECRET123" not in lines[0]
    assert "REDACTED" in lines[0] or "timeout" in lines[0]
    assert "mode=" in lines[0]
    judged = [t for t in stats["tokens"] if t.get("judge_verdict") == "error"]
    assert len(judged) == 1
    assert judged[0]["stage"] == "judge_error"
    assert "SUPERSECRET123" not in (judged[0].get("judge_reason") or "")
    stored = [t for t in hist._tokens_since(0) if t.get("kind") == "judge_error"]
    assert len(stored) == 1
    assert stored[0]["judge_verdict"] == "error"
    w = hist.summarize_window(0, time.time() + 10)
    assert w["judged_tokens"] == []
    assert w["judge_error_tokens"][0]["ticker"] == "ERR"
    md = hist.build_briefing(time.time() + 10)["markdown"]
    passed = md.split("## Passed judge/picks")[1].split("## Judge verdicts")[0]
    verdicts = md.split("## Judge verdicts")[1]
    assert "ERR" not in passed
    assert "ERR" in verdicts
    warn = [r.message for r in caplog.records if "judge failed" in r.message]
    assert warn
    assert "SUPERSECRET123" not in warn[0]
    assert "REDACTED" in warn[0] or "timeout" in warn[0]


def test_pick_judge_error_is_verdict_error(hist, monkeypatch, caplog):
    token = _solana_token(ticker="PKE")

    def fake_judge(question_set, state):
        if question_set == "pick":
            raise TimeoutError("pick timed out token=LEAKEDTOKEN")
        if question_set == "market":
            return {"model": "test", "answers": _pass_soft_answers(), "usage": {}}
        return {"model": "test", "answers": {}, "usage": {}}

    shift = _wire_one_token(monkeypatch, token, fake_judge)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        order, stats = shift.run_once(
            type("F", (), {"token": lambda self: None})(),
            fake_judge, _Desk(), 1000.0, shadow=True, gt_dossier_reserve=3,
        )
    assert order is None
    lines = [r.message for r in caplog.records if r.message.startswith("judge tid=")]
    assert lines and "verdict=error" in lines[0]
    assert "LEAKEDTOKEN" not in caplog.text
    judged = [t for t in stats["tokens"] if t.get("stage") == "judged"]
    assert judged[0]["judge_verdict"] == "error"


def test_soft_kill_does_not_emit_judge_line(hist, monkeypatch, caplog):
    token = _solana_token(ticker="SFT")

    def fake_judge(question_set, state):
        if question_set == "market":
            return {"model": "test", "answers": {
                "concentration_is_exit_risk": {"type": "noul", "noul": 0.75},
                "momentum_already_spent": {"type": "noul", "noul": 0.40},
            }, "usage": {}}
        return {"model": "test", "answers": {}, "usage": {}}

    shift = _wire_one_token(monkeypatch, token, fake_judge)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        shift.run_once(
            type("F", (), {"token": lambda self: None})(),
            fake_judge, _Desk(), 1000.0, shadow=True, gt_dossier_reserve=3,
        )
    assert not [r.message for r in caplog.records if r.message.startswith("judge tid=")]
    soft = [r.message for r in caplog.records if r.message.startswith("soft tid=")]
    assert soft and "noul=0.75" in soft[0]


def test_existing_log_lines_unchanged_in_source():
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
    assert 'log.info("unevaluated %d ids (dex_slots=%d, gt_available=%d)",' in src
    assert 'log.warning("judge failed %s: %s", d["ticker"], safe_err(e))' in src
    assert 'ticker=(.+?) verdict=' in (ROOT / "cycle_history.py").read_text()


def test_backfill_parses_judge_line(hist):
    log_text = """
2026-10-10 09:33:01,001 desk INFO judge tid=PepperMint:1399811149 ticker=PPR verdict=pass score=0.2 confidence=0.9 reason=worth_trading_at_all=0.2 below 0.6; solana, 52m old mode=live
2026-10-10 09:33:02,002 desk INFO cycle: 4 seen, 0 benched, free {}, trade {}, chain {}, soft {}, judged 1, requeued 0
"""
    n = hist.backfill_text(log_text)
    assert n["inserted"] == 1
    judged = [t for t in hist._tokens_since(0) if t["kind"] == "judged"]
    assert len(judged) == 1
    assert judged[0]["ticker"] == "PPR"
    assert judged[0]["judge_verdict"] == "pass"
    assert "worth_trading_at_all=0.2 below 0.6" in (judged[0]["judge_reason"] or "")
    assert judged[0]["judge_score"] == pytest.approx(0.2)
    assert judged[0]["judge_confidence"] == pytest.approx(0.9)


def test_backfill_judge_ticker_with_spaces(hist):
    log_text = """
2026-10-10 09:33:01,001 desk INFO judge tid=Spc:1399811149 ticker=Hello World verdict=pass score=0.2 confidence=0.9 reason=ok mode=live
2026-10-10 09:33:02,002 desk INFO cycle: 1 seen, 0 benched, free {}, trade {}, chain {}, soft {}, judged 1, requeued 0
"""
    n = hist.backfill_text(log_text)
    assert n["inserted"] == 1
    judged = [t for t in hist._tokens_since(0) if t["kind"] == "judged"]
    assert judged[0]["ticker"] == "Hello World"


def test_judge_error_not_in_passed_judge_table(hist):
    hist.on_cycle({
        "seen": 2, "benched": 0, "judged": 1, "requeued": 0,
        "free": {}, "trade": {}, "chain": {}, "soft": {},
        "carry": 0, "unevaluated": 0,
        "tokens": [{
            "tid": "ok:1399811149", "ticker": "OKT", "net": 1399811149,
            "stage": "judged", "age_minutes": 40,
            "soft_scores": {"momentum_already_spent": 0.2},
            "judge_verdict": "pass", "judge_reason": "no pick",
        }, {
            "tid": "bad:1399811149", "ticker": "BAD", "net": 1399811149,
            "stage": "judge_error", "age_minutes": 22,
            "judge_verdict": "error", "judge_reason": "TimeoutError: timeout",
        }],
    }, now=ts(2026, 10, 10, 9, 33), source="backfill")
    w = hist.summarize_window(0, ts(2026, 10, 10, 10))
    assert [t["ticker"] for t in w["judged_tokens"]] == ["OKT"]
    assert [t["ticker"] for t in w["judge_error_tokens"]] == ["BAD"]
    md = hist.build_briefing(ts(2026, 10, 10, 10))["markdown"]
    passed = md.split("## Passed judge/picks")[1].split("## Judge verdicts")[0]
    assert "OKT" in passed
    assert "BAD" not in passed
    assert "BAD" in md.split("## Judge verdicts")[1]


def test_helpers_tolerate_malformed_pick_answers():
    import main as shift
    cases = [
        {"best": "A", "worth_trading_at_all": {"noul": 0.2}},
        {"best": {"choice": "PPR", "confidence": 0.9, "probabilities": {"PPR": 0.9}},
         "worth_trading_at_all": "x"},
        {"best": {"choice": "PPR", "confidence": 0.9, "probabilities": ["A"]},
         "worth_trading_at_all": {"noul": 0.2}},
        {"best": {"choice": "PPR", "confidence": "high", "probabilities": {"PPR": 0.9}},
         "worth_trading_at_all": {"noul": 0.75}},
    ]
    for answers in cases:
        verdict, extra = shift._pick_verdict_extra(None, answers, None, "PPR")
        assert verdict in ("pass", "error")
        score, conf = shift._score_and_confidence({}, answers, None, "PPR")
        assert score is None or isinstance(score, (int, float))
        assert conf is None or isinstance(conf, (int, float))


@pytest.mark.parametrize("pick_answers", [
    {"best": "A", "worth_trading_at_all": {"type": "noul", "noul": 0.2}},
    {"best": {"type": "choice", "choice": "PPR", "confidence": 0.9,
              "probabilities": {"PPR": 0.9}},
     "worth_trading_at_all": "x"},
    {"best": {"type": "choice", "choice": "PPR", "confidence": 0.9,
              "probabilities": ["A"]},
     "worth_trading_at_all": {"type": "noul", "noul": 0.2}},
    {"best": {"type": "choice", "choice": "PPR", "confidence": "high",
              "probabilities": {"PPR": 0.9}},
     "worth_trading_at_all": {"type": "noul", "noul": 0.75}},
], ids=["best_str", "worth_str", "probs_list", "conf_text"])
def test_malformed_pick_answers_no_pick_and_writes_history(
    hist, monkeypatch, caplog, pick_answers,
):
    token = _solana_token(ticker="PPR")

    def fake_judge(question_set, state):
        if question_set == "market":
            return {"model": "test", "answers": _pass_soft_answers(), "usage": {}}
        if question_set == "pick":
            return {"model": "test", "answers": pick_answers, "usage": {}}
        return {"model": "test", "answers": {}, "usage": {}}

    shift = _wire_one_token(monkeypatch, token, fake_judge)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        order, stats = shift.run_once(
            type("F", (), {"token": lambda self: None})(),
            fake_judge, _Desk(), 1000.0, shadow=True, gt_dossier_reserve=3,
        )
    assert order is None
    assert stats["judged"] == 1
    rows = hist.cycles_since(0)
    assert len(rows) == 1
    stored = hist._tokens_since(0)
    assert any(t.get("kind") == "judged" for t in stored)


def test_apply_judge_verdicts_crash_still_writes_history(hist, monkeypatch, caplog):
    token = _solana_token(ticker="PPR")

    def fake_judge(question_set, state):
        if question_set == "market":
            return {"model": "test", "answers": _pass_soft_answers(), "usage": {}}
        if question_set == "pick":
            return {"model": "test", "answers": {
                "best": {"type": "choice", "choice": "PPR", "confidence": 0.9,
                         "probabilities": {"PPR": 0.9}},
                "worth_trading_at_all": {"type": "noul", "noul": 0.2},
            }, "usage": {}}
        return {"model": "test", "answers": {}, "usage": {}}

    shift = _wire_one_token(monkeypatch, token, fake_judge)

    def boom(*_a, **_k):
        raise TypeError("boom api-key=SECRET999")

    monkeypatch.setattr(shift, "_apply_judge_verdicts", boom)
    caplog.clear()
    with caplog.at_level(logging.INFO):
        order, stats = shift.run_once(
            type("F", (), {"token": lambda self: None})(),
            fake_judge, _Desk(), 1000.0, shadow=True, gt_dossier_reserve=3,
        )
    assert order is None
    assert hist.cycles_since(0)
    assert any("judge verdicts:" in r.message for r in caplog.records)
    assert "SECRET999" not in caplog.text


def test_no_env_in_judge_reason(monkeypatch):
    import main as shift
    monkeypatch.setenv("TYPESAFE_API_KEY", "ts-never-print")
    monkeypatch.setenv("DESK_SECRET", "desk-never-print")
    reason = shift._truncate_judge_reason("ok")
    assert "ts-never-print" not in reason
    assert "desk-never-print" not in reason
