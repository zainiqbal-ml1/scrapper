#!/usr/bin/env python3
"""Babysit runner for the CanLII scraper.

Runs the scraper and, whenever DataDome blocks the session, refreshes it
(auto-solving the slider if this machine can; otherwise it opens a window for
YOU to solve), then resumes automatically (already-downloaded files are
skipped).

Run it in your OWN terminal (it needs to read your keypresses).

Interactive (recommended) - pick state, db(s), years, workers + rate:
    python run.py

Non-interactive (pass everything):
    python run.py --juris on --db all --years all
    python run.py --juris on --db onca onsc --years 2020-2024 --workers 3 --rate 3
    python run.py --juris all --db all --years all
"""
import subprocess
import sys

import bootstrap
bootstrap.ensure_session_file()

import platform_util

import canlii_scraper as cs
import auto_refresh

AUTO_STEPS = """
============================================================
 BLOCKED by DataDome - refreshing your session:
   -> If this Mac can auto-solve (Screen Recording +
      Accessibility granted), a stealth window solves it.
   -> Otherwise a Chrome window opens - SOLVE THE SLIDER
      there; the new cookie is grabbed automatically.
============================================================"""


def _reload_session() -> None:
    import importlib
    import session as _s
    importlib.reload(_s)
    cs.HEADERS = _s.HEADERS


def refresh_until_valid() -> bool:
    """Refresh the session until --check passes. No curl.txt; solve in window."""
    while True:
        cookie = auto_refresh.harvest_cookie()
        if cookie:
            auto_refresh.update_session_cookie(cookie, getattr(auto_refresh, "LAST_UA", ""))
            _reload_session()
            if subprocess.call([sys.executable, "canlii_scraper.py", "--check"]) == 0:
                return True
        try:
            input("\nSession still blocked. A window will open - SOLVE THE SLIDER, "
                  "then press Enter to retry (Ctrl+C to quit)... ")
        except (KeyboardInterrupt, EOFError):
            return False


def _discover_dbs_with_refresh(juris: str) -> dict:
    """Fetch the database list for a jurisdiction, refreshing if blocked."""
    while True:
        session = cs.make_session()
        try:
            return cs.discover_databases(session, juris)
        except cs.SessionExpired:
            print("\nSession blocked while listing databases - refreshing first...")
            if not refresh_until_valid():
                raise
            print("Session refreshed. Listing databases...\n")


def select_workers_rate() -> tuple[int, float]:
    """Ask how many parallel workers and the max requests/second."""
    raw = input("Parallel workers [1]: ").strip()
    workers = int(raw) if raw.isdigit() and int(raw) >= 1 else 1
    raw = input("Max requests/second [2]: ").strip()
    try:
        rate = float(raw) if raw else 2.0
    except ValueError:
        rate = 2.0
    if rate <= 0:
        rate = 2.0
    return workers, rate


def interactive_select():
    """Prompt for jurisdiction -> db(s) -> years -> workers/rate.

    Returns (juris, db_list, years, workers, rate).
    """
    juris = cs.select_jurisdiction()
    if juris == "all":
        years = cs.select_years()
        workers, rate = select_workers_rate()
        print("\nAll jurisdictions selected -> every database.")
        return "all", ["all"], years, workers, rate
    dbs = _discover_dbs_with_refresh(juris)
    chosen = cs.select_databases(dbs)
    years = cs.select_years()
    workers, rate = select_workers_rate()
    return juris, chosen, years, workers, rate


def run_parallel(juris, db_list, years, workers, rate) -> int:
    """Parallel scrape (workers>=1). Cookie pool keeps 2 ahead; retries failures."""
    cmd = [sys.executable, "parallel_scraper.py", "--juris", juris, "--db", *db_list,
           "--years", years, "--workers", str(max(1, workers)), "--rate", f"{rate:g}"]
    return subprocess.call(cmd)


def _extract_opt(args, name, cast, default):
    if name in args:
        i = args.index(name)
        if i + 1 < len(args):
            try:
                return cast(args[i + 1])
            except ValueError:
                pass
    return default


def main() -> int:
    args = sys.argv[1:]

    if not args:
        try:
            juris, db_list, years, workers, rate = interactive_select()
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.")
            return 130
    else:
        # Non-interactive: read selection from flags (sensible defaults).
        juris = _extract_opt(args, "--juris", str, "on")
        years = _extract_opt(args, "--years", str, "all")
        workers = _extract_opt(args, "--workers", int, 1)
        rate = _extract_opt(args, "--rate", float, 2.0)
        if "--db" not in args:
            print("Provide --db <code...|all> (or run 'python run.py' for interactive mode).")
            return 1
        i = args.index("--db")
        db_list = []
        for tok in args[i + 1:]:
            if tok.startswith("--"):
                break
            db_list.append(tok)

    print(f"\nPlan: juris={juris} db={' '.join(db_list)} years={years} "
          f"workers={workers} rate={rate:g} req/s | OS: {platform_util.system()} "
          f"| harvest: {platform_util.harvest_backend()}\n")

    return run_parallel(juris, db_list, years, workers, rate)


if __name__ == "__main__":
    raise SystemExit(main())
