#!/usr/bin/env python3
"""Parallel CanLII PDF scraper (cookie pool + worker threads).

Same output as canlii_scraper.py:
    data/<state>/<db>/<year>/<decision>.pdf
    data/<state>/<db>/<year>.json

How it scales (and its limits):
  - Runs N worker threads, each holding its OWN datadome cookie.
  - Up to 3 incognito Chrome windows harvest cookies in parallel to fill the pool.
  - Workers grab the next cookie instantly on 403/429 (never retry with a burned one).
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
    """Queue of ready cookies; background harvesters refill when low.

    Only one browser harvest runs at a time. Prefetch ramps up after the first
    successful API call so startup never opens multiple windows.
    """

    COOKIE_BUDGET = 75
    MIN_READY = 2
    ACQUIRE_POLL = 0.02
    ACQUIRE_MAX_WAIT = 120
    MAX_PARALLEL_HARVESTS = 1

    def __init__(self, workers: int = 3):
        self._workers = max(1, workers)
        self.TARGET_READY = max(1, min(self._workers, 2))
        self._prefetch_enabled = False
        self._ready: queue.Queue[str] = queue.Queue()
        self._stop = threading.Event()
        self._harvests_lock = threading.Lock()
        self._active_harvests = 0
        self._harvest_serial = 0
        self._ready.put(COOKIE)
        self._maintainer = threading.Thread(target=self._maintainer_loop, daemon=True, name="cookie-pool")
        self._maintainer.start()

    def enable_prefetch(self) -> None:
        """After session is proven good, keep more cookies ready (macOS: up to 2 parallel)."""
        if self._prefetch_enabled:
            return
        self._prefetch_enabled = True
        if platform_util.is_macos():
            self.MAX_PARALLEL_HARVESTS = 2
            self.TARGET_READY = max(self.MIN_READY, self._workers + 2)
        else:
            self.MAX_PARALLEL_HARVESTS = 1
            self.TARGET_READY = max(1, min(self._workers, 2))
        self.kick_fill()

    def _do_harvest(self) -> str:
        with self._harvests_lock:
            self._harvest_serial += 1
            n = self._harvest_serial
        print(f"\n>>> Harvesting cookie #{n} (parallel window)...\n", flush=True)
        cookie = auto_refresh.harvest_cookie_pool()
        if cookie:
            print(f">>> Cookie #{n} ready (pool has ~{self._ready.qsize() + 1}).\n", flush=True)
        return cookie

    def _harvest_worker(self) -> None:
        try:
            if self._stop.is_set():
                return
            cookie = self._do_harvest()
            if cookie and "datadome=" in cookie:
                self._ready.put(cookie)
        except Exception as e:
            print(f"[cookie-pool] harvest failed: {e}", file=sys.stderr, flush=True)
        finally:
            with self._harvests_lock:
                self._active_harvests -= 1
            if not self._stop.is_set():
                self.kick_fill()

    def _start_harvests(self) -> None:
        """Launch as many parallel harvests as needed (up to MAX_PARALLEL_HARVESTS)."""
        if self._stop.is_set():
            return
        with self._harvests_lock:
            needed = self.TARGET_READY - self._ready.qsize()
            slots = self.MAX_PARALLEL_HARVESTS - self._active_harvests
            to_start = min(max(needed, 0), max(slots, 0))
            for _ in range(to_start):
                self._active_harvests += 1
                threading.Thread(
                    target=self._harvest_worker, daemon=True, name="cookie-harvest"
                ).start()

    def _maintainer_loop(self) -> None:
        while not self._stop.is_set():
            if self._ready.qsize() < 1:
                self._start_harvests()
            elif self._prefetch_enabled and self._ready.qsize() < self.TARGET_READY:
                self._start_harvests()
            time.sleep(0.5)

    def ready_count(self) -> int:
        return self._ready.qsize()

    def kick_fill(self) -> None:
        self._start_harvests()

    def acquire(self) -> str:
        """Take a ready cookie; harvesters refill when the pool runs low."""
        try:
            return self._ready.get_nowait()
        except queue.Empty:
            pass
        self.kick_fill()
        deadline = time.monotonic() + self.ACQUIRE_MAX_WAIT
        while time.monotonic() < deadline and not self._stop.is_set():
            need = 1 if self._ready.qsize() < 1 else (
                self.TARGET_READY if self._prefetch_enabled else 1
            )
            if self._ready.qsize() < need:
                self.kick_fill()
            try:
                return self._ready.get(timeout=self.ACQUIRE_POLL)
            except queue.Empty:
                continue
        cookie = self._do_harvest()
        self.kick_fill()
        return cookie

    def stop(self) -> None:
        self._stop.set()


_local = threading.local()


def _discard_session() -> None:
    """Drop the worker's burned session so the next request cannot reuse it."""
    _local.session = None


def _swap_cookie(pool: CookiePool, *, reason: str = "") -> requests.Session:
    used = getattr(_local, "used", 0)
    if reason or used:
        print(f"    (fresh cookie from pool{': ' + reason if reason else ''}, had {used} downloads)", flush=True)
    _discard_session()
    pool.kick_fill()
    cookie = pool.acquire()
    _local.session = requests.Session(
        impersonate=cs.IMPERSONATE, headers=HEADERS, cookies=parse_cookie(cookie), timeout=60
    )
    _local.used = 0
    return _local.session


def get_session(pool: CookiePool, force_new: bool = False) -> requests.Session:
    used = getattr(_local, "used", 0)
    if not force_new and used >= CookiePool.COOKIE_BUDGET:
        force_new = True
    if force_new:
        return _swap_cookie(pool, reason="proactive rotation")
    if not getattr(_local, "session", None):
        cookie = pool.acquire()
        _local.session = requests.Session(
            impersonate=cs.IMPERSONATE, headers=HEADERS, cookies=parse_cookie(cookie), timeout=60
        )
        _local.used = 0
    return _local.session


def worker_get(pool: CookiePool, limiter: RateLimiter, url: str, referer: str | None):
    """One GET. On 429: brief backoff with same cookie before swapping."""
    headers = {"referer": referer} if referer else None
    for attempt in range(4):
        s = get_session(pool)
        limiter.wait()
        try:
            r = s.get(url, headers=headers)
        except Exception:
            _swap_cookie(pool, reason="network error")
            continue
        if r.status_code == 429:
            if attempt < 2:
                time.sleep(2.0)
                continue
            _swap_cookie(pool, reason="429 rate limit")
            continue
        if cs._is_challenge(r):
            _swap_cookie(pool, reason="captcha/block")
            continue
        return r
    raise NeedNewCookie()


def download_task(pool: CookiePool, limiter: RateLimiter, task: dict) -> tuple[dict, bool, str]:
    """Download one PDF; on block/rate-limit swap to pool cookie immediately."""
    dest = Path(task["dest"])
    if dest.exists() and dest.stat().st_size > 0:
        return task, True, "exists"
    try:
        r = worker_get(pool, limiter, task["pdf_url"], task["html_url"])
        if r.status_code != 200:
            return task, False, f"HTTP {r.status_code}"
        if not r.content.startswith(b"%PDF"):
            return task, False, "not a PDF"
        dest.parent.mkdir(parents=True, exist_ok=True)
        dest.write_bytes(r.content)
        _local.used = getattr(_local, "used", 0) + 1
        if _local.used >= CookiePool.COOKIE_BUDGET - 15 and pool._prefetch_enabled:
            pool.kick_fill()
        return task, True, f"{len(r.content)//1024} KB"
    except NeedNewCookie:
        try:
            _swap_cookie(pool, reason="pool exhausted")
            r = worker_get(pool, limiter, task["pdf_url"], task["html_url"])
            if r.status_code != 200:
                return task, False, f"HTTP {r.status_code}"
            if not r.content.startswith(b"%PDF"):
                return task, False, "not a PDF"
            dest.parent.mkdir(parents=True, exist_ok=True)
            dest.write_bytes(r.content)
            _local.used = getattr(_local, "used", 0) + 1
            return task, True, f"{len(r.content)//1024} KB"
        except Exception:
            return task, False, "blocked (no pool cookie)"


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
    max_retry_rounds: int = 0,
) -> tuple[int, int]:
    """Download all tasks, retry failures, update records. Returns (ok, fail).

    max_retry_rounds:
      - 0 => retry until everything succeeds (recommended)
      - N => stop after N retry rounds
    """
    ok, failed = _run_downloads(pool, limiter, tasks, workers, label)
    round_no = 0
    while failed and (max_retry_rounds <= 0 or round_no < max_retry_rounds):
        round_no += 1
        suffix = f"{round_no}/{max_retry_rounds}" if max_retry_rounds > 0 else f"{round_no}"
        print(f"  {label}: retrying {len(failed)} failed (round {suffix})...")
        ok2, failed = _run_downloads(pool, limiter, failed, workers, f"{label} retry")
        ok += ok2
        if failed:
            # Keep pressure low while still pushing toward zero failed.
            time.sleep(0.5)

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
    """Main-thread listings; instant pool swap on block/rate-limit."""
    for _ in range(2):
        try:
            return worker_get(pool, limiter, url, referer)
        except NeedNewCookie:
            _swap_cookie(pool, reason="listing blocked")
    raise RuntimeError(f"Could not fetch {url}")


def _pooled_discover_databases(pool: CookiePool, limiter: RateLimiter, juris: str) -> dict[str, str]:
    """List databases using the cookie pool (swap on block)."""
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
    pool.enable_prefetch()
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
    ap.add_argument("--workers", type=int, default=platform_util.default_workers(),
                    help="Concurrent download workers")
    ap.add_argument("--rate", type=float, default=platform_util.default_rate(),
                    help="Max total requests/sec across all workers")
    args = ap.parse_args()

    cs.OUT_ROOT = Path(args.out)
    pool = CookiePool(workers=args.workers)
    limiter = RateLimiter(args.rate)

    print(f"Cookie pool: one harvest window until session proven; then up to "
          f"{pool.MAX_PARALLEL_HARVESTS} parallel.\n")

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
