#!/usr/bin/env python3
"""Babysit runner for the CanLII scraper.

Runs the scraper and, whenever DataDome blocks the session, refreshes it
(auto-solving the slider if this machine can; otherwise it opens a window for
YOU to solve), then resumes automatically (already-downloaded files are
skipped).

Run it in your OWN terminal (it needs to read your keypresses).

Interactive (recommended) - pick state, db(s), years, rate, Tor, good-exit threshold:
    python run.py

Non-interactive (pass everything):
    python run.py --juris on --db all --years all
    python run.py --juris on --db onca onsc --years 2020-2024 --rate 3
    python run.py --juris on --db onca --years 2024 --rate 0.1-0.2
    python run.py --tor --juris on --db onca --years 2024 --rate 0.1-0.2 --good-exit-threshold 10
    python run.py --juris all --db all --years all
"""
import subprocess
import os
import sys
import time

import bootstrap
bootstrap.ensure_session_file()

import platform_util

import canlii_scraper as cs
import canlii_api
import auto_refresh
import tor_util

AUTO_STEPS = """
============================================================
 BLOCKED by DataDome - refreshing your session:
   -> If this Mac can auto-solve (Screen Recording +
      Accessibility granted), a stealth window solves it.
   -> Otherwise a Chrome window opens - SOLVE THE SLIDER
      there; the new cookie is grabbed automatically.
============================================================"""

TEMP_BLOCK_EXIT = 75
DEFAULT_RESTART_DELAY = 60.0
DEFAULT_MAX_RESTARTS = 20


def _reload_session() -> None:
    import importlib
    import session as _s
    importlib.reload(_s)
    cs.HEADERS = _s.HEADERS


def refresh_until_valid(juris: str) -> bool:
    """Refresh the session until check_session passes (one window at a time)."""
    print("\nSession blocked — need a fresh cookie.\n", flush=True)
    if tor_util.enabled():
        tor_util.set_current_lane(0)
    while True:
        cookie = auto_refresh.harvest_cookie()
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
    """Fetch the database list for a jurisdiction (API when configured)."""
    if canlii_api.enabled():
        try:
            print(f"Listing databases for {juris} (API)...", flush=True)
            dbs = canlii_api.discover_databases(juris)
            print(f"Found {len(dbs)} databases.\n", flush=True)
            return dbs
        except Exception as e:
            print(f"[api] database list failed ({e}) — using website session\n", flush=True)
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


def select_rate() -> str:
    """Ask the max requests/second, or a range like 0.1-0.2."""
    dr = platform_util.default_rate()
    raw = input(f"Requests/second or range [{dr:g}] (example 0.1-0.2): ").strip()
    return raw or f"{dr:g}"


def select_good_exit_threshold(*, use_tor: bool) -> int:
    """Ask how many PDFs per cookie before reusing the same Tor exit."""
    default = tor_util.DEFAULT_GOOD_EXIT_PDF_THRESHOLD
    hint = "" if use_tor else " (only used with Tor)"
    raw = input(
        f"Reuse Tor exit after this many PDFs per cookie [{default}]{hint}: ",
    ).strip()
    if not raw:
        return default
    try:
        return max(1, int(raw))
    except ValueError:
        print(f"Invalid number — using {default}.", flush=True)
        return default


def select_tor_lanes() -> int:
    """Ask how many parallel Tor exits (each needs its own SOCKS port)."""
    raw = input("Tor parallel lanes (2 = two IPs + cookies) [2]: ").strip()
    if not raw:
        return 2
    try:
        return max(1, int(raw))
    except ValueError:
        print("Invalid number — using 2 lanes.", flush=True)
        return 2


def select_tor(*, default: bool | None = None) -> bool:
    """Ask whether to route CanLII traffic through local Tor."""
    if default is not None:
        return default
    raw = input("Route through Tor? [y/N] (Tor Browser on port 9150): ").strip().lower()
    return raw in {"y", "yes", "1"}


def _tor_from_flags(args: list[str]) -> bool | None:
    if "--tor" in args:
        return True
    env = os.environ.get("CANLII_USE_TOR", "").strip().lower()
    if env in {"1", "true", "yes"}:
        return True
    return None


def interactive_select(*, tor_default: bool | None = None):
    """Prompt for jurisdiction -> db(s) -> years -> rate -> good-exit -> Tor.

    Returns (juris, db_list, years, rate, use_tor, good_exit_threshold, tor_lanes).
    """
    juris = cs.select_jurisdiction()
    print(flush=True)  # newline after selection so status line is visible
    use_tor = select_tor(default=tor_default)
    tor_lanes = 1
    if use_tor:
        try:
            tor_util.configure(use_tor=True, lanes=1)
        except RuntimeError as e:
            print(f"Tor error: {e}", file=sys.stderr)
            print("Continuing without Tor.\n", flush=True)
            use_tor = False
            tor_util.configure(use_tor=False)
        else:
            tor_lanes = select_tor_lanes()
            try:
                tor_util.configure(use_tor=True, lanes=tor_lanes)
            except RuntimeError as e:
                print(f"Tor lanes error: {e}", file=sys.stderr)
                print("Falling back to 1 lane.\n", flush=True)
                tor_lanes = 1
                tor_util.configure(use_tor=True, lanes=1)
    if juris == "all":
        years = cs.select_years()
        rate = select_rate()
        good_exit = select_good_exit_threshold(use_tor=use_tor)
        print("\nAll jurisdictions selected -> every database.")
        return "all", ["all"], years, rate, use_tor, good_exit, tor_lanes
    dbs = _discover_dbs_with_refresh(juris)
    chosen = cs.select_databases(dbs)
    years = cs.select_years()
    rate = select_rate()
    good_exit = select_good_exit_threshold(use_tor=use_tor)
    return juris, chosen, years, rate, use_tor, good_exit, tor_lanes


def run_scrape(
    juris,
    db_list,
    years,
    rate,
    *,
    restart_delay: float = DEFAULT_RESTART_DELAY,
    max_restarts: int = DEFAULT_MAX_RESTARTS,
    use_tor: bool = False,
    good_exit_threshold: int = tor_util.DEFAULT_GOOD_EXIT_PDF_THRESHOLD,
    tor_lanes: int = 1,
) -> int:
    """Scrape with a request/sec cap; resume same settings on hard block."""
    workers = tor_lanes if use_tor and tor_lanes > 1 else 1
    cmd = [sys.executable, "parallel_scraper.py", "--juris", juris, "--db", *db_list,
           "--years", years, "--workers", str(workers), "--rate", str(rate),
           "--good-exit-threshold", str(good_exit_threshold)]
    if use_tor:
        cmd.append("--tor")
        if tor_lanes > 1:
            cmd.extend(["--lanes", str(tor_lanes)])
    restarts = 0
    while True:
        rc = subprocess.call(cmd)
        if rc != TEMP_BLOCK_EXIT:
            return rc

        restarts += 1
        if max_restarts > 0 and restarts > max_restarts:
            print(f"Reached max restart count ({max_restarts}); stopping.", flush=True)
            return rc

        print(
            f"\nAccess temporarily blocked. Restarting same run "
            f"({restarts}/{max_restarts if max_restarts > 0 else 'unlimited'})...\n",
            flush=True,
        )
        if restart_delay > 0:
            print(f"Waiting {restart_delay:g}s before restart.", flush=True)
            time.sleep(restart_delay)


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
    if "--use-api" not in args:
        os.environ["CANLII_IGNORE_API"] = "1"

    tor_flag = _tor_from_flags(args)

    if not args:
        try:
            juris, db_list, years, rate, use_tor, good_exit, tor_lanes = interactive_select(tor_default=tor_flag)
        except (KeyboardInterrupt, EOFError):
            print("\nCancelled.")
            return 130
        restart_delay = DEFAULT_RESTART_DELAY
        max_restarts = DEFAULT_MAX_RESTARTS
    else:
        use_tor = bool(tor_flag)
        tor_lanes = 1
        if use_tor:
            try:
                tor_lanes = _extract_opt(args, "--lanes", int, 2)
                tor_util.configure(use_tor=True, lanes=tor_lanes)
            except RuntimeError as e:
                print(f"Tor error: {e}", file=sys.stderr)
                return 1
        # Non-interactive: read selection from flags (sensible defaults).
        juris = _extract_opt(args, "--juris", str, "on")
        years = _extract_opt(args, "--years", str, "all")
        rate = _extract_opt(args, "--rate", str, f"{platform_util.default_rate():g}")
        good_exit = _extract_opt(
            args, "--good-exit-threshold", int, tor_util.DEFAULT_GOOD_EXIT_PDF_THRESHOLD,
        )
        restart_delay = _extract_opt(args, "--restart-delay", float, DEFAULT_RESTART_DELAY)
        max_restarts = _extract_opt(args, "--max-restarts", int, DEFAULT_MAX_RESTARTS)
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
          f"rate={rate} req/s good-exit={good_exit}+ PDFs"
          f"{' (Tor)' if use_tor else ''}"
          f"{f' lanes={tor_lanes}' if use_tor and tor_lanes > 1 else ''} "
          f"| OS: {platform_util.system()} "
          f"| harvest: {platform_util.harvest_backend()}\n"
          f"Restart on temporary block: delay={restart_delay:g}s "
          f"max={max_restarts if max_restarts > 0 else 'unlimited'}\n")
    canlii_api.print_status()
    tor_util.print_status()
    tor_util.print_ip()
    auto_refresh.print_harvest_capabilities(force_recheck=True)
    print(flush=True)

    if juris != "all" and not canlii_api.enabled() and not ensure_session_or_refresh(juris):
        return 130
    if juris != "all" and canlii_api.enabled() and not cs.check_session(juris):
        print("Session not verified yet — PDF downloads will refresh the cookie if blocked.\n", flush=True)

    return run_scrape(
        juris,
        db_list,
        years,
        rate,
        restart_delay=restart_delay,
        max_restarts=max_restarts,
        use_tor=use_tor,
        good_exit_threshold=good_exit,
        tor_lanes=tor_lanes,
    )


if __name__ == "__main__":
    raise SystemExit(main())
