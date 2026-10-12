"""
Entrypoint. Wires FOMO + judge + desk into main.main().

    python run.py                # shadow mode (default). Logs would-be trades, sends nothing.
    python run.py --once         # one cycle, then exit
    python run.py --live         # takes the book and hands orders to the seats. After a
                                 # week of shadow rows, not before.
    python run.py --mock-judge   # answers from mock_judge instead of JUDGE_URL (funnel testing)
    python run.py --bench        # print what is benched and why, then exit

Weekly signal backtest (log/analysis only): the first cycle after Sunday 07:00
local writes the report from cached outcomes only (no network). Outcome OHLCV
is fetched in OUTCOMES_WINDOW (default 01:00-06:00 local) plus a daytime trickle.
CLI: python backtest_signals.py --days 7 [--fetch]. Never uses the cycle GT
limiter and never changes a kill/judge/pick.
"""
import argparse
import logging
import os
import sys

logging.basicConfig(level=os.environ.get("LOG_LEVEL", "INFO"),
                    format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("run")


def build_judge(mock: bool):
    if mock:
        from mock_judge import mock_judge_fn
        log.warning("MOCK JUDGE: answers are synthetic")
        return mock_judge_fn()
    from judge_client import judge      # needs JUDGE_URL and DESK_SECRET
    return judge


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="send orders and take the book")
    ap.add_argument("--once", action="store_true", help="run one cycle and exit")
    ap.add_argument("--mock-judge", action="store_true")
    ap.add_argument("--bench", action="store_true")
    a = ap.parse_args()

    if a.bench:
        import book
        for tid, reason, mins in book.bench_report():
            print(f"{tid:60} {reason:28} {mins:>6.0f} min left")
        return

    from desk import Desk
    from fomo_api import Fomo
    import main as shift

    shadow = not a.live
    if not shadow:
        log.warning("LIVE MODE. Orders will be handed to the seats and the book will be taken.")
        if os.environ.get("CONFIRM_LIVE") != "yes":
            sys.exit("Refusing to go live without CONFIRM_LIVE=yes in the environment.")

    desk = Desk()
    if desk.bank() <= 0:
        log.warning("BANK_USD is 0: intended_ticket_usd will be 0 and liquidity_fits_ticket "
                    "is meaningless. Set BANK_USD to your free cash.")

    shift.main(Fomo(), build_judge(a.mock_judge), desk, shadow=shadow, once=a.once)


if __name__ == "__main__":
    main()
