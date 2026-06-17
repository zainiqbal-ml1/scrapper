#!/usr/bin/env python3
"""Babysit runner for the CanLII scraper.

Runs the scraper and, whenever DataDome blocks the session, refreshes it
(auto-solving the slider if this machine can; otherwise it opens a window for
YOU to solve), then resumes automatically (already-downloaded files are
skipped).

Run it in your OWN terminal (it needs to read your keypresses).

Interactive (recommended) - pick state, db(s), years, rate:
    python run.py

Non-interactive (pass everything):
    python run.py --juris on --db all --years all
    python run.py --juris on --db onca onsc --years 2020-2024 --rate 3
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


def _harvest_one_window(juris: str) -> str:
    """Open ONE Chrome window and keep it open until the captcha is solved.

    macOS: incognito via AppleScript when Chrome allows JS over Apple Events;
    otherwise (and on Linux/Windows) a SeleniumBase window. Never opens two.
    """
    if platform_util.has_osascript() and platform_util.chrome_macos_installed():
        cookie = auto_refresh.harvest_cookie_macos(keep_open=True)
        if cookie and "datadome=" in cookie:
            return cookie
        if not auto_refresh.LAST_MAC_NOJS:
            # Window worked but timed out without solving; let the caller retry.
            return ""
    # Fallback / non-macOS: a single long-lived SeleniumBase window.
    return auto_refresh.harvest_cookie_browser()


def refresh_until_valid(juris: str) -> bool:
    """Refresh the session until check_session passes (one window at a time)."""
    print("\nSession blocked — need a fresh cookie.\n", flush=True)
    while True:
        cookie = _harvest_one_window(juris)
        if cookie and "datadome=" in cookie:
            auto_refresh.update_session_cookie(cookie, getattr(auto_refresh, "LAST_UA", ""))
            _reload_session()
            print("Verifying session...", flush=True)
            if cs.check_session(juris):
                print("Session OK.\n", flush=True)
                return True
            print("Cookie saved but CanLII still blocked for this jurisdiction.", flush=True)
        try:
            input(
                "\nCaptcha not solved yet. Press Enter to open a fresh window and "
                "try again (Ctrl+C to quit)... "
            )
        except (KeyboardInterrupt, EOFError):
            return False


def ensure_session_or_refresh(juris: str) -> bool:
    """Validate session for this jurisdiction; refresh first if blocked."""
    print(f"Checking session for {juris}...", flush=True)
    if cs.check_session(juris):
        return True
    print("\nSession needs a fresh cookie before listing databases.\n")
    return refresh_until_valid(juris)


def _discover_dbs_with_refresh(juris: str) -> dict:
    """Fetch the database list for a jurisdiction, refreshing if blocked."""
    if not ensure_session_or_refresh(juris):
        raise KeyboardInterrupt()
    while True:
        session = cs.make_session()
        try:
            print(f"Listing databases for {juris}...", flush=True)
            dbs = cs.discover_databases(session, juris)
            print(f"Found {len(dbs)} databases.\n", flush=True)
            return dbs
        except cs.SessionExpired:
            print("\nSession blocked while listing databases — refreshing...")
            if not refresh_until_valid(juris):
                raise
            print("Session refreshed. Retrying...\n")


def select_rate() -> float:
    """Ask the max requests/second (single worker; throughput is rate-limited)."""
    dr = platform_util.default_rate()
    raw = input(f"Max requests/second [{dr:g}]: ").strip()
    try:
        rate = float(raw) if raw else dr
    except ValueError:
        rate = dr
    if rate <= 0:
        rate = dr
    return rate


def interactive_select():
    """Prompt for jurisdiction -> db(s) -> years -> rate.

    Returns (juris, db_list, years, rate).
    """
    juris = cs.select_jurisdiction()
    print(flush=True)  # newline after selection so status line is visible
    if juris == "all":
        years = cs.select_years()
        rate = select_rate()
        print("\nAll jurisdictions selected -> every database.")
        return "all", ["all"], years, rate
    dbs = _discover_dbs_with_refresh(juris)
    chosen = cs.select_databases(dbs)
    years = cs.select_years()
    rate = select_rate()
    return juris, chosen, years, rate


def run_scrape(juris, db_list, years, rate) -> int:
    """Single-worker scrape with a request/sec cap; retries failures."""
    cmd = [sys.executable, "parallel_scraper.py", "--juris", juris, "--db", *db_list,
           "--years", years, "--workers", "1", "--rate", f"{rate:g}"]
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
            juris, db_list, years, rate = interactive_select()
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.")
            return 130
    else:
        # Non-interactive: read selection from flags (sensible defaults).
        juris = _extract_opt(args, "--juris", str, "on")
        years = _extract_opt(args, "--years", str, "all")
        rate = _extract_opt(args, "--rate", float, platform_util.default_rate())
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
          f"rate={rate:g} req/s | OS: {platform_util.system()} "
          f"| harvest: {platform_util.harvest_backend()}\n")

    if juris != "all" and not ensure_session_or_refresh(juris):
        return 130

    return run_scrape(juris, db_list, years, rate)


if __name__ == "__main__":
    raise SystemExit(main())
