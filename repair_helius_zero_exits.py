"""Recompute young_trail_helius $0 closes from stored marks. Dry-run by default.

Paper-only. Never places orders. Refuses the live outcomes.db (realpath,
symlink, relative path, samefile). Only copies under outbox/backups/ or a
temp dir are allowed. The live-DB check runs before OUTCOMES_DB is set
or young_trail_helius is imported.
"""
from __future__ import annotations

import argparse
import os
import pathlib
import sys
import tempfile

from secret_utils import safe_err


class RepairDbRefused(ValueError):
    """--db points at the live desk DB or is not under backups/temp."""


def _realpath(path) -> pathlib.Path:
    return pathlib.Path(os.path.realpath(os.path.abspath(os.path.expanduser(str(path)))))


def _repo_root() -> pathlib.Path:
    return pathlib.Path(__file__).resolve().parent


def _is_under(path: pathlib.Path, root: pathlib.Path) -> bool:
    try:
        path.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def _temp_roots() -> list[pathlib.Path]:
    roots: list[str] = [tempfile.gettempdir(), "/tmp", "/var/tmp"]
    for key in ("TMPDIR", "TMP", "TEMP"):
        raw = os.environ.get(key)
        if raw:
            roots.append(raw)
    out: list[pathlib.Path] = []
    seen: set[str] = set()
    for raw in roots:
        try:
            p = _realpath(raw)
        except OSError:
            continue
        key = str(p)
        if key in seen:
            continue
        seen.add(key)
        out.append(p)
    return out


def _is_temp_path(path: pathlib.Path) -> bool:
    resolved = _realpath(path)
    for root in _temp_roots():
        if _is_under(resolved, root):
            return True
    return any(part.startswith("pytest-") or part == "pytest" for part in resolved.parts)


def _is_backups_path(path: pathlib.Path) -> bool:
    """True when a resolved parent pair is outbox/backups."""
    parts = _realpath(path).parts
    for i in range(len(parts) - 1):
        if parts[i] == "outbox" and parts[i + 1] == "backups":
            return True
    return False


def live_outcomes_paths() -> list[pathlib.Path]:
    """Desk live outcomes.db locations. Does not import paper ledgers."""
    repo = _repo_root()
    cands = [repo / "outbox" / "outcomes.db"]
    desk = os.environ.get("DESK_OUTBOX") or "outbox"
    desk_p = pathlib.Path(desk)
    if not desk_p.is_absolute():
        cands.append(pathlib.Path.cwd() / desk_p / "outcomes.db")
        cands.append(repo / desk_p / "outcomes.db")
    else:
        cands.append(desk_p / "outcomes.db")
    env = os.environ.get("OUTCOMES_DB")
    if env:
        ep = pathlib.Path(env)
        if not _is_temp_path(ep) and not _is_backups_path(ep):
            cands.append(ep)
    out: list[pathlib.Path] = []
    seen: set[str] = set()
    for c in cands:
        try:
            rp = _realpath(c)
        except OSError:
            continue
        key = str(rp)
        if key in seen:
            continue
        seen.add(key)
        out.append(rp)
    return out


def _same_file(a: pathlib.Path, b: pathlib.Path) -> bool:
    try:
        if a.exists() and b.exists() and not a.is_dir() and not b.is_dir():
            return os.path.samefile(a, b)
    except OSError:
        pass
    return str(_realpath(a)) == str(_realpath(b))


def is_live_outcomes(path) -> bool:
    resolved = _realpath(path)
    return any(_same_file(resolved, live) for live in live_outcomes_paths())


def assert_repair_db_allowed(path) -> pathlib.Path:
    """Resolve realpath. Raise RepairDbRefused for live DB or disallowed location."""
    resolved = _realpath(path)
    if is_live_outcomes(resolved):
        raise RepairDbRefused(f"refusing live outcomes.db: {resolved}")
    if not _is_backups_path(resolved) and not _is_temp_path(resolved):
        raise RepairDbRefused(
            f"refusing db not under outbox/backups/ or a temp dir: {resolved}"
        )
    return resolved


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(
        description="Repair young_trail_helius rows that closed at entry_price.",
    )
    ap.add_argument(
        "--db",
        required=True,
        help="Copy of outcomes.db under outbox/backups/ or a temp dir.",
    )
    ap.add_argument(
        "--apply",
        action="store_true",
        help="Write the recomputed exits. Default is dry-run.",
    )
    args = ap.parse_args(argv)
    try:
        resolved = assert_repair_db_allowed(args.db)
    except RepairDbRefused as e:
        print(str(e), file=sys.stderr)
        return 2
    if not resolved.exists():
        print("db not found:", resolved, file=sys.stderr)
        return 2
    try:
        import young_trail_helius as h
        h.reset()
        plan = h.plan_zero_exit_repairs(resolved)
        print(h.format_repair_diff(plan))
        if args.apply:
            n = h.apply_zero_exit_repairs(resolved, plan)
            print("applied", n)
        else:
            print("dry-run (pass --apply to write)")
    except RepairDbRefused as e:
        print(str(e), file=sys.stderr)
        return 2
    except Exception as e:
        print("repair failed:", safe_err(e), file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
