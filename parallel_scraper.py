#!/usr/bin/env python3
"""Parallel CanLII PDF scraper (cookie pool + worker threads).

Same output as canlii_scraper.py:
    data/<state>/<db>/<year>/<decision>.pdf
    data/<state>/<db>/<year>.json

How it scales (and its limits):
  - Runs N worker threads, each holding its OWN datadome cookie.
  - A background thread keeps workers+2 fresh cookies queued ahead; harvest
    starts immediately when the pool drains (no startup wait, no 15s backoff).
  - Cookies are rotated proactively at ~75 downloads before DataDome burns them.
  - A shared RATE LIMITER caps total requests/sec so the single IP doesn't get
    429'd (parallel workers share one IP, so this is the real ceiling).
  - Failed PDFs are retried up to 3 rounds before moving to the next year.
  - Years with missing/failed PDFs are NOT skipped on resume.

Examples:
    python parallel_scraper.py --juris on --db all --years all --workers 3
    python parallel_scraper.py --juris ca --db scc --years 2018-2026 --workers 4 --rate 3
"""
from __future__ import annotations

import argparse
import json
import queue
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from curl_cffi import requests

import auto_refresh
import canlii_scraper as cs
from session import HEADERS, COOKIE


class NeedNewCookie(Exception):
    pass


def parse_cookie(cookie_str: str) -> dict:
    jar = {}
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" in part:
            k, v = part.split("=", 1)
            jar[k.strip()] = v.strip()
    return jar


class RateLimiter:
    """Global minimum interval between any outbound requests."""

    def __init__(self, rate_per_sec: float):
        self.min_interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0

    def wait(self):
        if self.min_interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            sleep_for = max(0.0, self._next - now)
            self._next = max(now, self._next) + self.min_interval
        if sleep_for:
            time.sleep(sleep_for)


class CookiePool:
    """Queue of ready cookies; background thread keeps the pool topped up.

    Workers never wait on captcha if a cookie is queued. When the pool drains,
    harvest is kicked immediately (not on a slow poll loop). Cookies are also
    rotated proactively before DataDome burns them (~75 requests).
    """

    COOKIE_BUDGET = 75       # swap out before the ~85-request burn
    MIN_READY = 2
    MAX_RETRIES = 2
    ACQUIRE_POLL = 0.05      # seconds between queue polls
    ACQUIRE_MAX_WAIT = 45    # then fall back to synchronous harvest

    def __init__(self, workers: int = 3):
        self._workers = max(1, workers)
        # Keep enough ready cookies for every worker to swap instantly.
        self.TARGET_READY = max(self.MIN_READY, self._workers + 2)
        self._ready: queue.Queue[str] = queue.Queue()
        self._harvest_lock = threading.Lock()
        self._harvesting = threading.Event()
        self._need_fill = threading.Event()
        self._stop = threading.Event()
        self._ready.put(COOKIE)  # seed from session.py
        self._need_fill.set()    # kick first background fill immediately
        self._thread = threading.Thread(target=self._maintainer, daemon=True, name="cookie-pool")
        self._thread.start()

    def _harvest(self) -> str:
        with self._harvest_lock:
            print("\n>>> Harvesting a fresh cookie (background)...\n", flush=True)
            for _ in range(self.MAX_RETRIES):
                cookie = auto_refresh.harvest_cookie()
                if cookie:
                    print(">>> Cookie ready.\n", flush=True)
                    return cookie
            raise RuntimeError("Could not harvest a fresh cookie (captcha not solved in time).")

    def _fill_one(self) -> None:
        if self._stop.is_set() or self._ready.qsize() >= self.TARGET_READY:
            return
        if self._harvesting.is_set():
            return
        self._harvesting.set()
        try:
            self._ready.put(self._harvest())
        except Exception as e:
            print(f"[cookie-pool] harvest failed: {e}", file=sys.stderr, flush=True)
            time.sleep(2)
        finally:
            self._harvesting.clear()
            self._need_fill.set()

    def _maintainer(self) -> None:
        while not self._stop.is_set():
            self._need_fill.wait(timeout=0.2)
            self._need_fill.clear()
            if self._stop.is_set():
                break
            while self._ready.qsize() < self.TARGET_READY and not self._stop.is_set():
                self._fill_one()

    def ready_count(self) -> int:
        return self._ready.qsize()

    def kick_fill(self) -> None:
        """Ask the background thread to top up the pool now."""
        self._need_fill.set()

    def acquire(self) -> str:
        """Take a ready cookie. Non-blocking when the pool has stock."""
        try:
            cookie = self._ready.get_nowait()
            self.kick_fill()
            return cookie
        except queue.Empty:
            pass

        self.kick_fill()
        deadline = time.monotonic() + self.ACQUIRE_MAX_WAIT
        while time.monotonic() < deadline:
            try:
                cookie = self._ready.get(timeout=self.ACQUIRE_POLL)
                self.kick_fill()
                return cookie
            except queue.Empty:
                continue
        # Pool still empty - last resort (blocks on captcha).
        cookie = self._harvest()
        self.kick_fill()
        return cookie

    def stop(self) -> None:
        self._stop.set()
        self._need_fill.set()


_local = threading.local()


def get_session(pool: CookiePool, force_new: bool = False) -> requests.Session:
    used = getattr(_local, "used", 0)
    # Rotate before DataDome burns the cookie (~85 req) so we never hit 403.
    if not force_new and used >= CookiePool.COOKIE_BUDGET:
        force_new = True
    if force_new or not getattr(_local, "session", None):
        if force_new and used:
            print(f"    (cookie swap after {used} downloads)", flush=True)
        cookie = pool.acquire()
        _local.session = requests.Session(
            impersonate=cs.IMPERSONATE, headers=HEADERS, cookies=parse_cookie(cookie), timeout=60
        )
        _local.used = 0
    return _local.session


def worker_get(pool: CookiePool, limiter: RateLimiter, url: str, referer: str | None):
    """One GET with rate limiting.

    On 429 (rate limited) we refresh the cookie immediately rather than waiting
    - a fresh cookie usually resets the rate counter. A captcha-block also
    raises NeedNewCookie.
    """
    s = get_session(pool)
    headers = {"referer": referer} if referer else None
    while True:
        limiter.wait()
        try:
            r = s.get(url, headers=headers)
        except Exception:
            time.sleep(0.3)
            continue
        if r.status_code == 429 or cs._is_challenge(r):
            raise NeedNewCookie()
        return r


def download_task(pool: CookiePool, limiter: RateLimiter, task: dict) -> tuple[dict, bool, str]:
    """Download one PDF, refreshing the worker's cookie if it gets blocked."""
    dest = Path(task["dest"])
    if dest.exists() and dest.stat().st_size > 0:
        return task, True, "exists"
    for attempt in range(4):
        try:
            r = worker_get(pool, limiter, task["pdf_url"], task["html_url"])
            if r.status_code != 200:
                return task, False, f"HTTP {r.status_code}"
            if not r.content.startswith(b"%PDF"):
                return task, False, "not a PDF"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.content)
            _local.used = getattr(_local, "used", 0) + 1
            if _local.used >= CookiePool.COOKIE_BUDGET - 10:
                pool.kick_fill()  # top up pool before this cookie expires
            return task, True, f"{len(r.content)//1024} KB"
        except NeedNewCookie:
            get_session(pool, force_new=True)  # swap in a fresh cookie, retry
    return task, False, "blocked (gave up after refreshes)"


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
    pool: CookiePool,
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
        futs = [ex.submit(download_task, pool, limiter, t) for t in tasks]
        for fut in as_completed(futs):
            task, got, _msg = fut.result()
            if got:
                ok += 1
            else:
                failed.append(task)
            done += 1
            bar = f"  {label}: [{done}/{total}] ok={ok} fail={len(failed)}"
            sys.stdout.write("\r" + bar.ljust(72))
            sys.stdout.flush()
    sys.stdout.write("\r" + " " * 72 + "\r")
    return ok, failed


def _download_year_with_retries(
    pool: CookiePool,
    limiter: RateLimiter,
    tasks: list[dict],
    records: list[dict],
    workers: int,
    label: str,
    max_retry_rounds: int = 3,
) -> tuple[int, int]:
    """Download all tasks, retry failures, update records. Returns (ok, fail)."""
    ok, failed = _run_downloads(pool, limiter, tasks, workers, label)
    round_no = 0
    while failed and round_no < max_retry_rounds:
        round_no += 1
        print(f"  {label}: retrying {len(failed)} failed (round {round_no}/{max_retry_rounds})...")
        ok2, failed = _run_downloads(pool, limiter, failed, workers, f"{label} retry")
        ok += ok2

    # Sync record file/error fields with what actually landed on disk.
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


def manager_get(pool: CookiePool, limiter: RateLimiter, url: str, referer: str | None = None):
    """Used by the main thread for listings; refreshes cookie on block."""
    for _ in range(4):
        try:
            return worker_get(pool, limiter, url, referer)
        except NeedNewCookie:
            get_session(pool, force_new=True)
    raise RuntimeError(f"Could not fetch {url}")


def _scrape_juris(pool, limiter, juris, db_arg, args, grand) -> None:
    out = cs.OUT_ROOT
    all_dbs = cs.discover_databases(get_session(pool), juris)
    targets = list(all_dbs.keys()) if db_arg == ["all"] else db_arg
    print(f"\n=== {juris} ({cs.JURISDICTIONS.get(juris, juris)}): {len(targets)} db(s) ===")

    for db in targets:
        print(f"\n[{juris}/{db}] {all_dbs.get(db, '?')}")
        years_available = cs.get_years(get_session(pool), juris, db)
        years = cs.parse_years_arg(args.years, years_available)
        for year in years:
            if _year_complete(out, juris, db, year):
                print(f"  {juris}/{db}/{year}: already complete, skipping")
                continue
            r = manager_get(pool, limiter, f"{cs.BASE}/{juris}/{db}/nav/date/{year}/items",
                            referer=f"{cs.BASE}/en/{juris}/{db}/")
            try:
                items = r.json()
            except Exception:
                items = []
            if not items:
                continue

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
                pool, limiter, tasks, records, args.workers, label,
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
    ap = argparse.ArgumentParser(description="Parallel CanLII scraper (all jurisdictions)")
    ap.add_argument("--juris", default="on", help="Jurisdiction code e.g. on ca bc, or 'all'")
    ap.add_argument("--db", nargs="+", required=True, help="DB code(s) or 'all'")
    ap.add_argument("--years", default="all")
    ap.add_argument("--out", default="data")
    ap.add_argument("--workers", type=int, default=3, help="Concurrent download workers (default 3)")
    ap.add_argument("--rate", type=float, default=3.0, help="Max total requests/sec across all workers")
    args = ap.parse_args()

    cs.OUT_ROOT = Path(args.out)
    pool = CookiePool(workers=args.workers)
    limiter = RateLimiter(args.rate)

    print(f"Cookie pool: target {pool.TARGET_READY} ready ahead "
          f"(fills in background while downloading).\n")

    jurisdictions = list(cs.JURISDICTIONS.keys()) if args.juris == "all" else [args.juris]
    grand = {"total": 0, "downloaded": 0, "failed": 0}

    print(f"Parallel scrape: {args.workers} workers, {args.rate} req/s cap "
          f"(structure: {cs.OUT_ROOT}/<state>/<db>/<year>/)\n")

    try:
        for juris in jurisdictions:
            _scrape_juris(pool, limiter, juris, args.db, args, grand)
    finally:
        pool.stop()

    print("\n" + "=" * 60)
    print(f" DONE - {grand['total']} decisions: {grand['downloaded']} ok, {grand['failed']} failed")
    if grand["failed"]:
        print(" Some PDFs still failed after retries - re-run to try again.")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
