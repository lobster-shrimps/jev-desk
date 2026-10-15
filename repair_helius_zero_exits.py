"""Recompute young_trail_helius $0 closes from stored marks. Dry-run by default.

Paper-only. Never places orders. Refuses the live outcomes.db: pass --db PATH
to a copy. Use --apply to write; otherwise prints a diff and exits.
"""
from __future__ import annotations

import argparse
import os
import sys

from secret_utils import safe_err


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Repair young_trail_helius rows that closed at entry_price.",
    )
    ap.add_argument(
        "--db",
        required=True,
        help="Path to a copy of outcomes.db (refuses the live desk DB).",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Write the recomputed exits. Default is dry-run.",
    )
    args = ap.parse_args(argv)
    path = os.path.abspath(args.db)
    if not os.path.exists(path):
        print("db not found:", path, file=sys.stderr)
        return 2
    os.environ["OUTCOMES_DB"] = path
    try:
        import young_trail_helius as h
        h.reset()
        plan = h.plan_zero_exit_repairs()
        print(h.format_repair_diff(plan))
        if args.apply:
            n = h.apply_zero_exit_repairs(plan)
            print("applied", n)
        else:
            print("dry-run (pass --apply to write)")
    except Exception as e:
        print("repair failed:", safe_err(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
