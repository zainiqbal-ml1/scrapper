#!/usr/bin/env python3
"""Parallel CanLII PDF scraper (cookie pool + worker threads).

Same output as canlii_scraper.py:
    data/<state>/<db>/<year>/<decision>.pdf
    data/<state>/<db>/<year>.json

How it scales (and its limits):
  - Background thread keeps 2–3 fresh cookies queued (one harvest at a time).
  - Cookies rotate proactively at ~75 downloads before DataDome burns them.
  - On 429/403 the worker swaps to the next pooled cookie immediately.
  - A shared rate limiter caps total requests/sec.
  - Failed PDFs retry up to 3 rounds; permanent errors (404, not a PDF) are not retried.

Examples:
    python parallel_scraper.py --juris on --db all --years all --workers 3
    python parallel_scraper.py --juris ca --db scc --years 2018-2026 --workers 4 --rate 3
"""
from __future__ import annotations

import argparse
import json
import queue
import re
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from curl_cffi import requests

import auto_refresh
import bootstrap
import canlii_scraper as cs
import platform_util

bootstrap.ensure_session_file()
from session import HEADERS, COOKIE


class NeedNewCookie(Exception):
    """Pool has no usable cookies left."""

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
    """Queue of ready cookies; background harvester keeps 2–3 spares without blocking downloads.

    - Prefetch starts immediately (maintainer always runs).
    - Downloads never wait on an empty pool for proactive rotation.
    - On 429/403, swap from pool instantly; harvest only if pool has no spare.
    """

    COOKIE_BUDGET = 75
    TARGET_READY = 3
    MAX_RETRIES = 2

    def __init__(self, workers: int = 1, *, prefill: int = 3):
        self._workers = max(1, workers)
        self._background = auto_refresh.can_background_harvest()
        self.TARGET_READY = prefill if prefill > 0 else 3
        self._ready: queue.Queue[str] = queue.Queue()
        self._harvest_lock = threading.Lock()
        self._harvesting = threading.Event()
        self._need_fill = threading.Event()
        self._stop = threading.Event()
        if prefill > 0:
            self._prefill_sync(prefill)
        elif COOKIE and "datadome=" in COOKIE:
            print("Cookie pool: using session cookie (no prefill on manual Mac).\n", flush=True)
        self._need_fill.set()
        self._thread = threading.Thread(target=self._maintainer, daemon=True, name="cookie-pool")
        self._thread.start()

    def _prefill_sync(self, count: int) -> int:
        """Harvest `count` cookies up front so swaps never wait at startup."""
        print(f"Cookie pool: prefilling {count} cookies...", flush=True)
        while self.ready_count() < count and not self._stop.is_set():
            n = self.ready_count()
            print(f"  harvesting cookie {n + 1}/{count}...", flush=True)
            try:
                self._ready.put(self._harvest(quiet=n > 0))
            except Exception as e:
                print(f"[cookie-pool] prefill stopped at {n}/{count}: {e}", file=sys.stderr, flush=True)
                break
        n = self.ready_count()
        print(f"Cookie pool: {n}/{count} ready.\n", flush=True)
        return n

    def drain(self) -> None:
        """Drop pooled cookies (all burned during an IP block)."""
        while not self._ready.empty():
            try:
                self._ready.get_nowait()
            except queue.Empty:
                break

    def _harvest(self, *, quiet: bool = True) -> str:
        if auto_refresh.ip_blocked_cooldown_active():
            auto_refresh.wait_ip_cooldown(quiet=quiet)
        with self._harvest_lock:
            if not quiet:
                print("\n>>> Harvesting a fresh cookie...\n", flush=True)
            for _ in range(self.MAX_RETRIES):
                if auto_refresh.can_background_harvest():
                    cookie = auto_refresh.harvest_cookie_pool(quiet=quiet)
                else:
                    cookie = auto_refresh.harvest_cookie()
                if cookie and "datadome=" in cookie:
                    if not quiet:
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
            self._ready.put(self._harvest(quiet=True))
        except Exception as e:
            print(f"[cookie-pool] harvest failed: {e}", file=sys.stderr, flush=True)
        finally:
            self._harvesting.clear()
            self._need_fill.set()

    def _maintainer(self) -> None:
        while not self._stop.is_set():
            self._need_fill.wait(timeout=0.2)
            self._need_fill.clear()
            if self._stop.is_set():
                break
            if auto_refresh.ip_blocked_cooldown_active():
                continue
            while self._ready.qsize() < self.TARGET_READY and not self._stop.is_set():
                self._fill_one()

    def ready_count(self) -> int:
        return self._ready.qsize()

    def kick_fill(self) -> None:
        self._need_fill.set()

    def try_acquire(self) -> str | None:
        """Take a pooled cookie if one is ready; never blocks."""
        try:
            cookie = self._ready.get_nowait()
            self.kick_fill()
            return cookie
        except queue.Empty:
            self.kick_fill()
            return None

    def acquire_for_swap(self, current: str | None = None) -> str:
        """Get a different cookie when blocked. No waiting — pool, session, or harvest."""
        for _ in range(2):
            cookie = self.try_acquire()
            if cookie and cookie != current:
                return cookie
        if COOKIE and COOKIE != current and "datadome=" in COOKIE:
            self.kick_fill()
            return COOKIE
        # Pool and session exhausted — harvest once (avoid repeated browser opens per PDF).
        cookie = self._harvest(quiet=False)
        self.kick_fill()
        return cookie

    def stop(self) -> None:
        self._stop.set()
        self._need_fill.set()


_local = threading.local()


def get_session(pool: CookiePool, force_new: bool = False) -> requests.Session:
    used = getattr(_local, "used", 0)
    current_cookie = getattr(_local, "cookie", None)
    cookie: str | None = None

    # Proactive rotation: swap only when a spare is already in the pool (never block).
    if not force_new and used >= CookiePool.COOKIE_BUDGET:
        cookie = pool.try_acquire()
        if cookie:
            print(f"    (cookie swap after {used} downloads)", flush=True)
        else:
            pool.kick_fill()
            if getattr(_local, "session", None):
                return _local.session

    if not force_new and cookie is None and getattr(_local, "session", None):
        return _local.session

    if force_new and used:
        print(f"    (cookie swap — blocked)", flush=True)

    if cookie is None:
        if force_new:
            cookie = pool.acquire_for_swap(current_cookie)
        else:
            cookie = COOKIE  # first use: session.py cookie, do not drain the pool

    _local.session = requests.Session(
        impersonate=cs.IMPERSONATE, headers=HEADERS,
        cookies=parse_cookie(cookie), timeout=60,
    )
    _local.cookie = cookie
    _local.used = 0
    return _local.session


def worker_get(pool: CookiePool, limiter: RateLimiter, url: str, referer: str | None):
    """One GET with rate limiting. 429/block -> NeedNewCookie (swap via download_task)."""
    s = get_session(pool)
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
                auto_refresh.mark_ip_blocked()
                pool.drain()
                auto_refresh.wait_ip_cooldown()
            raise NeedNewCookie()
        return r


def _permanent_fail(msg: str) -> bool:
    """True when retrying with a new cookie cannot help."""
    return msg.startswith(("HTTP 404", "HTTP 410", "HTTP 451", "not a PDF"))


def download_task(pool: CookiePool, limiter: RateLimiter, task: dict) -> tuple[dict, bool, str]:
    dest = Path(task["dest"])
    if dest.exists() and dest.stat().st_size > 0:
        return task, True, "exists"
    for attempt in range(4):
        try:
            r = worker_get(pool, limiter, task["pdf_url"], task["html_url"])
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
            _local.used = getattr(_local, "used", 0) + 1
            if _local.used >= CookiePool.COOKIE_BUDGET - 10:
                pool.kick_fill()
            task.pop("_fail_msg", None)
            return task, True, f"{len(r.content)//1024} KB"
        except NeedNewCookie:
            get_session(pool, force_new=True)
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
            task, got, msg = fut.result()
            if got:
                ok += 1
            else:
                task["_fail_msg"] = msg
                failed.append(task)
            done += 1
            bar = (f"  {label}: [{done}/{total}] ok={ok} fail={len(failed)} "
                   f"pool={pool.ready_count()}/{pool.TARGET_READY}")
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
    pool: CookiePool,
    limiter: RateLimiter,
    tasks: list[dict],
    records: list[dict],
    workers: int,
    label: str,
    max_retry_rounds: int = 3,
) -> tuple[int, int]:
    """Download all tasks, retry transient failures, update records. Returns (ok, fail)."""
    ok, failed = _run_downloads(pool, limiter, tasks, workers, label)
    permanent = [t for t in failed if _permanent_fail(t.get("_fail_msg", ""))]
    retryable = [t for t in failed if not _permanent_fail(t.get("_fail_msg", ""))]

    round_no = 0
    prev = len(retryable) + 1
    while retryable and round_no < max_retry_rounds:
        round_no += 1
        print(f"  {label}: retrying {len(retryable)} failed (round {round_no}/{max_retry_rounds})...", flush=True)
        ok2, failed = _run_downloads(pool, limiter, retryable, workers, f"{label} retry")
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
    """Main-thread listings; swap cookie on block."""
    for _ in range(4):
        try:
            return worker_get(pool, limiter, url, referer)
        except NeedNewCookie:
            get_session(pool, force_new=True)
    raise RuntimeError(f"Could not fetch {url}")


def _pooled_discover_databases(pool: CookiePool, limiter: RateLimiter, juris: str) -> dict[str, str]:
    r = manager_get(pool, limiter, f"{cs.BASE}/en/{juris}/")
    return cs.parse_databases_html(r.text, juris)


def _pooled_get_years(pool: CookiePool, limiter: RateLimiter, juris: str, db: str) -> list[int]:
    cache = cs.OUT_ROOT / ".years_cache" / f"{juris}_{db}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass
    r = manager_get(
        pool, limiter, f"{cs.BASE}/en/{juris}/{db}/", referer=f"{cs.BASE}/en/{juris}/",
    )
    years = sorted(
        {int(y) for y in re.findall(rf"/{juris}/{db}/nav/date/(\d{{4}})", r.text)},
        reverse=True,
    )
    if years:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(years))
    return years


def _scrape_juris(pool, limiter, juris, db_arg, args, grand) -> None:
    out = cs.OUT_ROOT
    all_dbs = _pooled_discover_databases(pool, limiter, juris)
    targets = list(all_dbs.keys()) if db_arg == ["all"] else db_arg
    print(f"\n=== {juris} ({cs.JURISDICTIONS.get(juris, juris)}): {len(targets)} db(s) ===")

    for db in targets:
        print(f"\n[{juris}/{db}] {all_dbs.get(db, '?')}")
        years_available = _pooled_get_years(pool, limiter, juris, db)
        years = cs.parse_years_arg(args.years, years_available)
        for year in years:
            if _year_complete(out, juris, db, year):
                print(f"  {juris}/{db}/{year}: already complete, skipping")
                continue
            print(f"  {juris}/{db}/{year}: fetching decision list...", flush=True)
            r = manager_get(pool, limiter, f"{cs.BASE}/{juris}/{db}/nav/date/{year}/items",
                            referer=f"{cs.BASE}/en/{juris}/{db}/")
            try:
                items = r.json()
            except Exception:
                items = []
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
    ap.add_argument("--workers", type=int, default=1,
                    help="Concurrent download workers (default 1; rate is the real ceiling)")
    ap.add_argument("--rate", type=float, default=platform_util.default_rate(),
                    help="Max total requests/sec across all workers")
    ap.add_argument("--prefill", type=int, default=None,
                    help="Spare cookies before downloading (default 3 silent, 0 on manual Mac)")
    args = ap.parse_args()

    cs.OUT_ROOT = Path(args.out)
    prefill = args.prefill
    if prefill is None:
        prefill = 3 if auto_refresh.can_background_harvest() else 0
    pool = CookiePool(workers=args.workers, prefill=max(0, prefill))
    limiter = RateLimiter(args.rate)

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
