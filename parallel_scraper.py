#!/usr/bin/env python3
"""CanLII PDF scraper with optional parallel downloads.

Same output as canlii_scraper.py:
    data/<state>/<db>/<year>/<decision>.pdf
    data/<state>/<db>/<year>.json

Session handling:
  - Starts from session.py; one cookie at a time (no pool or prefill).
  - On 429/403/challenge, harvests a fresh cookie immediately and retries.
  - A shared rate limiter caps total requests/sec.

Examples:
    python parallel_scraper.py --juris on --db all --years all --workers 1
    python parallel_scraper.py --juris ca --db scc --years 2018-2026 --workers 3 --rate 3
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from curl_cffi import requests

import auto_refresh
import bootstrap
import canlii_api
import canlii_scraper as cs
import platform_util

bootstrap.ensure_session_file()
from session import HEADERS, COOKIE


class NeedNewCookie(Exception):
    """Current cookie is blocked; harvest a fresh one."""

    pass


class AccessTemporarilyBlocked(RuntimeError):
    """CanLII/DataDome returned the hard temporary block page."""

    pass


def parse_cookie(cookie_str: str) -> dict:
    jar = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            jar[k.strip()] = v.strip()
    return jar


def parse_rate_spec(rate_spec: str | float) -> tuple[float, float]:
    """Return (min_rate, max_rate) from '0.2' or '0.1-0.2'."""
    raw = str(rate_spec).strip()
    if "-" in raw:
        left, right = raw.split("-", 1)
        a, b = float(left), float(right)
        lo, hi = sorted((a, b))
    else:
        lo = hi = float(raw)
    if lo <= 0 or hi <= 0:
        raise ValueError("rate must be positive")
    return lo, hi


class RateLimiter:
    """Global randomized minimum interval between outbound requests."""

    def __init__(self, rate_spec: str | float):
        self.min_rate, self.max_rate = parse_rate_spec(rate_spec)
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        rate = random.uniform(self.min_rate, self.max_rate)
        min_interval = 1.0 / rate
        with self._lock:
            now = time.monotonic()
            sleep_for = max(0.0, self._next - now)
            self._next = max(now, self._next) + min_interval
        if sleep_for:
            time.sleep(sleep_for)

    def label(self) -> str:
        if self.min_rate == self.max_rate:
            return f"{self.min_rate:g}"
        return f"{self.min_rate:g}-{self.max_rate:g}"


class SessionCookies:
    """Sequential session plus one background backup cookie."""

    MAX_HARVEST_RETRIES = 2

    def __init__(self, *, backup_enabled: bool = True) -> None:
        self._harvest_lock = threading.Lock()
        self._backup_lock = threading.Lock()
        self._backup_cookie: str | None = None
        self._backup_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._backup_enabled = backup_enabled
        if backup_enabled:
            self.kick_backup()

    def harvest_fresh(self, *, quiet: bool = False) -> str:
        with self._harvest_lock:
            if not quiet:
                print("\n>>> Harvesting a fresh cookie...\n", flush=True)
            for _ in range(self.MAX_HARVEST_RETRIES):
                cookie = auto_refresh.harvest_cookie()
                if cookie and "datadome=" in cookie:
                    if not quiet:
                        print(">>> Cookie ready.\n", flush=True)
                    return cookie
            raise RuntimeError("Could not harvest a fresh cookie (captcha not solved in time).")

    def _fill_backup(self) -> None:
        try:
            cookie = self.harvest_fresh(quiet=True)
            with self._backup_lock:
                if not self._stop.is_set():
                    self._backup_cookie = cookie
                    print("Backup cookie ready.\n", flush=True)
        except Exception as e:
            if not self._stop.is_set():
                print(f"[backup-cookie] harvest failed: {e}", file=sys.stderr, flush=True)

    def kick_backup(self) -> None:
        if not self._backup_enabled or self._stop.is_set():
            return
        with self._backup_lock:
            if self._backup_cookie:
                return
            if self._backup_thread and self._backup_thread.is_alive():
                return
            self._backup_thread = threading.Thread(
                target=self._fill_backup, daemon=True, name="backup-cookie",
            )
            self._backup_thread.start()

    def _take_backup(self, current: str | None = None) -> str | None:
        with self._backup_lock:
            if self._backup_cookie and self._backup_cookie != current:
                backup_cookie = self._backup_cookie
                self._backup_cookie = None
                return backup_cookie
        return None

    def acquire_for_swap(self, current: str | None = None) -> str:
        backup_cookie = self._take_backup(current)
        if not backup_cookie and self._backup_thread and self._backup_thread.is_alive():
            self._backup_thread.join()
            backup_cookie = self._take_backup(current)
        if backup_cookie:
            print("    (using backup cookie)", flush=True)
            self.kick_backup()
            return backup_cookie
        cookie = self.harvest_fresh()
        self.kick_backup()
        return cookie

    def stop(self) -> None:
        self._stop.set()


_local = threading.local()


def get_session(sessions: SessionCookies, force_new: bool = False) -> requests.Session:
    current_cookie = getattr(_local, "cookie", None)

    if not force_new and getattr(_local, "session", None):
        return _local.session

    if force_new and current_cookie:
        print("    (cookie swap — blocked)", flush=True)

    cookie = sessions.acquire_for_swap(current_cookie) if force_new else COOKIE

    _local.session = requests.Session(
        impersonate=cs.IMPERSONATE, headers=HEADERS,
        cookies=parse_cookie(cookie), timeout=60,
    )
    _local.cookie = cookie
    return _local.session


def worker_get(sessions: SessionCookies, limiter: RateLimiter, url: str, referer: str | None):
    """One GET with rate limiting. 429/block -> NeedNewCookie (harvest and retry)."""
    s = get_session(sessions)
    headers = {"referer": referer} if referer else None
    net_err = 0
    while True:
        limiter.wait()
        try:
            r = s.get(url, headers=headers)
        except Exception:
            net_err += 1
            if net_err >= 2:
                raise NeedNewCookie()
            continue
        if r.status_code == 429 or cs._is_challenge(r):
            if cs.is_ip_blocked_response(r):
                auto_refresh.mark_ip_blocked(quiet=False)
                raise AccessTemporarilyBlocked("access temporarily blocked")
            raise NeedNewCookie()
        return r


def _permanent_fail(msg: str) -> bool:
    """True when retrying with a new cookie cannot help."""
    return msg.startswith(("HTTP 404", "HTTP 410", "HTTP 451", "not a PDF"))


def download_task(
    sessions: SessionCookies, limiter: RateLimiter, task: dict,
) -> tuple[dict, bool, str]:
    dest = Path(task["dest"])
    if dest.exists() and dest.stat().st_size > 0:
        return task, True, "exists"
    for attempt in range(4):
        try:
            r = worker_get(sessions, limiter, task["pdf_url"], task["html_url"])
            if r.status_code != 200:
                if r.status_code in (403, 429) or cs._is_challenge(r):
                    raise NeedNewCookie()
                msg = f"HTTP {r.status_code}"
                task["_fail_msg"] = msg
                return task, False, msg
            if not r.content.startswith(b"%PDF"):
                msg = "not a PDF"
                task["_fail_msg"] = msg
                return task, False, msg
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.content)
            task.pop("_fail_msg", None)
            return task, True, f"{len(r.content)//1024} KB"
        except NeedNewCookie:
            get_session(sessions, force_new=True)
    msg = "blocked (gave up after refreshes)"
    task["_fail_msg"] = msg
    return task, False, msg


def _year_complete(out: Path, juris: str, db: str, year: int) -> bool:
    """True when the year JSON exists and every listed PDF is on disk."""
    jpath = out / juris / db / f"{year}.json"
    if not jpath.exists():
        return False
    try:
        records = json.loads(jpath.read_text())
    except Exception:
        return False
    if not records:
        return False
    for rec in records:
        if rec.get("error"):
            return False
        fp = rec.get("file")
        if not fp:
            return False
        p = out / fp
        if not p.exists() or p.stat().st_size == 0:
            return False
    return True


def _run_downloads(
    sessions: SessionCookies,
    limiter: RateLimiter,
    tasks: list[dict],
    workers: int,
    label: str,
) -> tuple[int, list[dict]]:
    """Download a batch; return (ok_count, failed_tasks)."""
    total = len(tasks)
    if not total:
        return 0, []
    ok = 0
    failed: list[dict] = []
    done = 0
    with ThreadPoolExecutor(max_workers=workers) as ex:
        futs = [ex.submit(download_task, sessions, limiter, t) for t in tasks]
        for fut in as_completed(futs):
            try:
                task, got, msg = fut.result()
            except AccessTemporarilyBlocked:
                for pending in futs:
                    pending.cancel()
                raise
            if got:
                ok += 1
            else:
                task["_fail_msg"] = msg
                failed.append(task)
            done += 1
            bar = f"  {label}: [{done}/{total}] ok={ok} fail={len(failed)}"
            sys.stdout.write("\r" + bar.ljust(72))
            sys.stdout.flush()
    sys.stdout.write("\r" + " " * 72 + "\r")
    return ok, failed


def _print_failures(label: str, failed: list[dict]) -> None:
    if not failed:
        return
    print(f"  {label}: {len(failed)} could not download:", flush=True)
    for t in failed[:8]:
        cite = t.get("citation", "?")
        msg = t.get("_fail_msg", "unknown")
        print(f"    - {cite}: {msg}", flush=True)
    if len(failed) > 8:
        print(f"    ... and {len(failed) - 8} more", flush=True)


def _download_year_with_retries(
    sessions: SessionCookies,
    limiter: RateLimiter,
    tasks: list[dict],
    records: list[dict],
    workers: int,
    label: str,
    max_retry_rounds: int = 3,
) -> tuple[int, int]:
    """Download all tasks, retry transient failures, update records. Returns (ok, fail)."""
    ok, failed = _run_downloads(sessions, limiter, tasks, workers, label)
    permanent = [t for t in failed if _permanent_fail(t.get("_fail_msg", ""))]
    retryable = [t for t in failed if not _permanent_fail(t.get("_fail_msg", ""))]

    round_no = 0
    prev = len(retryable) + 1
    while retryable and round_no < max_retry_rounds:
        round_no += 1
        print(f"  {label}: retrying {len(retryable)} failed (round {round_no}/{max_retry_rounds})...", flush=True)
        ok2, failed = _run_downloads(sessions, limiter, retryable, workers, f"{label} retry")
        ok += ok2
        permanent.extend(t for t in failed if _permanent_fail(t.get("_fail_msg", "")))
        retryable = [t for t in failed if not _permanent_fail(t.get("_fail_msg", ""))]
        if len(retryable) >= prev:
            print(f"  {label}: no progress this round — stopping retries.", flush=True)
            break
        prev = len(retryable)

    failed = permanent + retryable
    if failed:
        _print_failures(label, failed)

    out = cs.OUT_ROOT
    for rec in records:
        match = next((t for t in tasks if t["citation"] == rec.get("citation")), None)
        if not match:
            continue
        p = Path(match["dest"])
        if p.exists() and p.stat().st_size > 0:
            rec["file"] = str(p.relative_to(out))
            rec.pop("error", None)
        else:
            rec["file"] = None
            rec["error"] = "download failed after retries"

    return ok, len(failed)


def manager_get(sessions: SessionCookies, limiter: RateLimiter, url: str, referer: str | None = None):
    """Main-thread listings; harvest fresh cookie on block."""
    for _ in range(4):
        try:
            return worker_get(sessions, limiter, url, referer)
        except NeedNewCookie:
            get_session(sessions, force_new=True)
    raise RuntimeError(f"Could not fetch {url}")


def _discover_databases(sessions: SessionCookies, limiter: RateLimiter, juris: str) -> dict[str, str]:
    if canlii_api.enabled():
        try:
            return canlii_api.discover_databases(juris)
        except Exception as e:
            print(f"[api] database list failed ({e}) — using website", file=sys.stderr, flush=True)
    r = manager_get(sessions, limiter, f"{cs.BASE}/en/{juris}/")
    return cs.parse_databases_html(r.text, juris)


def _get_years(sessions: SessionCookies, limiter: RateLimiter, juris: str, db: str) -> list[int]:
    if canlii_api.enabled():
        try:
            return canlii_api.get_years(juris, db, cs.OUT_ROOT)
        except Exception as e:
            print(f"[api] year list failed ({e}) — using website", file=sys.stderr, flush=True)
    cache = cs.OUT_ROOT / ".years_cache" / f"{juris}_{db}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass
    r = manager_get(
        sessions, limiter, f"{cs.BASE}/en/{juris}/{db}/", referer=f"{cs.BASE}/en/{juris}/",
    )
    years = sorted(
        {int(y) for y in re.findall(rf"/{juris}/{db}/nav/date/(\d{{4}})", r.text)},
        reverse=True,
    )
    if years:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(years))
    return years


def _get_items(
    sessions: SessionCookies, limiter: RateLimiter, juris: str, db: str, year: int,
) -> list[dict]:
    if canlii_api.enabled():
        try:
            return canlii_api.get_items(juris, db, year, cs.OUT_ROOT)
        except Exception as e:
            print(f"[api] item list failed ({e}) — using website", file=sys.stderr, flush=True)
    r = manager_get(
        sessions,
        limiter,
        f"{cs.BASE}/{juris}/{db}/nav/date/{year}/items",
        referer=f"{cs.BASE}/en/{juris}/{db}/",
    )
    try:
        return r.json()
    except Exception:
        return []


def _scrape_juris(sessions, limiter, juris, db_arg, args, grand) -> None:
    out = cs.OUT_ROOT
    all_dbs = _discover_databases(sessions, limiter, juris)
    targets = list(all_dbs.keys()) if db_arg == ["all"] else db_arg
    print(f"\n=== {juris} ({cs.JURISDICTIONS.get(juris, juris)}): {len(targets)} db(s) ===")

    for db in targets:
        print(f"\n[{juris}/{db}] {all_dbs.get(db, '?')}")
        years_available = _get_years(sessions, limiter, juris, db)
        years = cs.parse_years_arg(args.years, years_available)
        for year in years:
            if _year_complete(out, juris, db, year):
                print(f"  {juris}/{db}/{year}: already complete, skipping")
                continue
            print(f"  {juris}/{db}/{year}: fetching decision list...", flush=True)
            items = _get_items(sessions, limiter, juris, db, year)
            if not items:
                print(f"  {juris}/{db}/{year}: no items, skipping")
                continue
            print(f"  {juris}/{db}/{year}: {len(items)} decisions — downloading...", flush=True)

            year_dir = out / juris / db / str(year)
            records, tasks = [], []
            for it in items:
                html_url = it.get("url")
                if not html_url:
                    continue
                style = (it.get("styleOfCause") or "").strip()
                citation = (it.get("citation") or "").strip().replace(" (CanLII)", "")
                pdf_url = cs.html_to_pdf_url(html_url)
                name = cs.sanitize_filename(f"{citation} - {style}" if style else citation)
                dest = year_dir / f"{name}.pdf"
                full_html = cs.BASE + html_url if html_url.startswith("/") else html_url
                records.append({"title": style or citation, "citation": citation,
                                "date": it.get("judgmentDate", ""), "pdf_url": pdf_url,
                                "html_url": full_html, "file": str(dest.relative_to(out))})
                tasks.append({"citation": citation, "pdf_url": pdf_url,
                              "html_url": full_html, "dest": str(dest)})

            label = f"{juris}/{db}/{year}"
            ok, fail = _download_year_with_retries(
                sessions, limiter, tasks, records, args.workers, label,
            )
            print(f"  {label}: {len(tasks)} decisions -> {ok} ok, {fail} failed")

            year_dir.mkdir(parents=True, exist_ok=True)
            (out / juris / db / f"{year}.json").write_text(
                json.dumps(records, indent=2, ensure_ascii=False)
            )
            grand["total"] += len(tasks)
            grand["downloaded"] += ok
            grand["failed"] += fail


def main() -> int:
    ap = argparse.ArgumentParser(description="CanLII scraper (sequential session, optional parallel downloads)")
    ap.add_argument("--juris", default="on", help="Jurisdiction code e.g. on ca bc, or 'all'")
    ap.add_argument("--db", nargs="+", required=True, help="DB code(s) or 'all'")
    ap.add_argument("--years", default="all")
    ap.add_argument("--out", default="data")
    ap.add_argument("--workers", type=int, default=1,
                    help="Concurrent download workers (default 1; rate is the real ceiling)")
    ap.add_argument("--rate", default=f"{platform_util.default_rate():g}",
                    help="Requests/sec, or a range like 0.1-0.2")
    ap.add_argument("--no-backup-cookie", action="store_true",
                    help="Disable the single background backup cookie harvest")
    args = ap.parse_args()

    cs.OUT_ROOT = Path(args.out)
    limiter = RateLimiter(args.rate)

    jurisdictions = list(cs.JURISDICTIONS.keys()) if args.juris == "all" else [args.juris]
    grand = {"total": 0, "downloaded": 0, "failed": 0}

    print(f"Scrape: {args.workers} workers, {limiter.label()} req/s "
          f"| backup cookie: {'off' if args.no_backup_cookie else 'one'} "
          f"(structure: {cs.OUT_ROOT}/<state>/<db>/<year>/)\n")
    sessions = SessionCookies(backup_enabled=not args.no_backup_cookie)

    try:
        for juris in jurisdictions:
            _scrape_juris(sessions, limiter, juris, args.db, args, grand)
    except AccessTemporarilyBlocked:
        print("\nAccess temporarily blocked — stopping this run for supervisor restart.", flush=True)
        return 75
    finally:
        sessions.stop()

    print("\n" + "=" * 60)
    print(f" DONE - {grand['total']} decisions: {grand['downloaded']} ok, {grand['failed']} failed")
    if grand["failed"]:
        print(" Some PDFs still failed after retries - re-run to try again.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
