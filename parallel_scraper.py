#!/usr/bin/env python3
"""Parallel CanLII PDF scraper (cookie pool + worker threads).

Same output as canlii_scraper.py:
    data/<state>/<db>/<year>/<decision>.pdf
    data/<state>/<db>/<year>.json

How it scales (and its limits):
  - Runs N worker threads that all SHARE one active datadome cookie.
    DataDome throttles by IP, not by cookie, so more cookies don't raise
    throughput — the shared rate limiter is the real ceiling.
  - On HTTP 429 the rate limiter backs off globally (no cookie swap).
  - On HTTP 403 / captcha the active cookie is burned: exactly one browser
    harvest runs to replace it and every worker switches to the new cookie.
  - Failed PDFs are retried until they all succeed.
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
    """Global minimum interval between requests, with adaptive slowdown on 429."""

    def __init__(self, rate_per_sec: float):
        self.base_interval = 1.0 / rate_per_sec if rate_per_sec > 0 else 0.0
        self._lock = threading.Lock()
        self._next = 0.0
        self._interval = self.base_interval
        self._ok_streak = 0

    def wait(self):
        with self._lock:
            interval = self._interval
        if interval <= 0:
            return
        with self._lock:
            now = time.monotonic()
            sleep_for = max(0.0, self._next - now)
            self._next = max(now, self._next) + interval
        if sleep_for:
            time.sleep(sleep_for)

    def slow_down(self):
        """Back off: requests came too fast (429). Halve the rate, capped at 1/5s."""
        with self._lock:
            base = self.base_interval or 0.2
            self._interval = min(max(self._interval, base) * 1.7, 5.0)
            self._ok_streak = 0

    def on_success(self):
        """Gradually recover toward the base rate after a run of clean responses."""
        with self._lock:
            if self._interval <= self.base_interval:
                return
            self._ok_streak += 1
            if self._ok_streak >= 25:
                self._interval = max(self.base_interval, self._interval * 0.8)
                self._ok_streak = 0

    @property
    def current_rate(self) -> float:
        with self._lock:
            return 1.0 / self._interval if self._interval > 0 else 0.0


class CookiePool:
    """Manages ONE shared 'active' cookie that all workers use concurrently.

    DataDome rate-limits by IP, not by cookie, so handing every worker a
    different cookie does not raise throughput — it just forces slow browser
    harvests. Instead:

      - All workers share the current active cookie (built per-thread, rebuilt
        only when the active cookie's generation changes).
      - On 429 (rate limit) we DON'T swap cookies (a new cookie from the same
        IP is throttled too) — the rate limiter backs off globally.
      - On 403 / captcha the active cookie is genuinely burned: exactly one
        harvest runs to replace it, and every worker switches to the new one.
      - Spare cookies harvested ahead of time live in `_ready` as a fast
        replacement so a burn doesn't always block on a browser window.
    """

    SPARE_TARGET = 1  # keep at most one spare ready (harvest is expensive)

    def __init__(self, workers: int = 3):
        self._workers = max(1, workers)
        self._active = COOKIE
        self._generation = 0
        self._lock = threading.Lock()
        self._ready: queue.Queue[str] = queue.Queue()
        self._harvesting = threading.Event()
        self._stop = threading.Event()
        self._pause_until = 0.0
        self._prefetch_enabled = False

    # -- active cookie ---------------------------------------------------- #
    def current(self) -> tuple[str, int]:
        with self._lock:
            return self._active, self._generation

    def report_burned(self, generation: int) -> str:
        """The cookie at `generation` got a 403/challenge. Replace it once.

        Returns the new active cookie. If another thread already replaced this
        generation (or is replacing it now), this waits for that rotation
        instead of harvesting a second time.
        """
        with self._lock:
            if generation != self._generation:
                return self._active  # already rotated by someone else
            if self._harvesting.is_set():
                already = True
            else:
                already = False
                self._harvesting.set()

        if already:
            return self._wait_for_rotation(generation)

        try:
            new = self._take_spare() or auto_refresh.harvest_cookie_pool() or ""
        finally:
            self._harvesting.clear()
        with self._lock:
            if new and "datadome=" in new and generation == self._generation:
                self._active = new
                self._generation += 1
                print(f"    >>> active cookie rotated (gen {self._generation}, "
                      f"tok {_dd(new)})", flush=True)
            return self._active

    def _wait_for_rotation(self, generation: int) -> str:
        """Block until another thread rotates past `generation`."""
        deadline = time.monotonic() + self.ACQUIRE_MAX_WAIT
        while time.monotonic() < deadline and not self._stop.is_set():
            with self._lock:
                if self._generation != generation:
                    return self._active
            time.sleep(0.1)
        return self.current()[0]

    def _take_spare(self) -> str:
        try:
            return self._ready.get_nowait()
        except queue.Empty:
            return ""

    # -- backoff ---------------------------------------------------------- #
    def backoff(self, seconds: float) -> None:
        with self._lock:
            self._pause_until = max(self._pause_until, time.monotonic() + seconds)

    def wait_if_paused(self) -> None:
        while not self._stop.is_set():
            with self._lock:
                remaining = self._pause_until - time.monotonic()
            if remaining <= 0:
                return
            time.sleep(min(remaining, 0.5))

    # -- spare prefetch (optional, non-blocking) -------------------------- #
    ACQUIRE_MAX_WAIT = 180

    def enable_prefetch(self) -> None:
        """Allow keeping one spare cookie ready (only after session proven)."""
        self._prefetch_enabled = True

    def maybe_prefetch_spare(self) -> None:
        """Harvest one spare in the background if none is ready (best effort)."""
        if not self._prefetch_enabled or self._stop.is_set():
            return
        if self._ready.qsize() >= self.SPARE_TARGET or self._harvesting.is_set():
            return
        threading.Thread(target=self._prefetch_worker, daemon=True,
                         name="cookie-prefetch").start()

    def _prefetch_worker(self) -> None:
        if self._harvesting.is_set():
            return
        self._harvesting.set()
        try:
            cookie = auto_refresh.harvest_cookie_pool()
            if cookie and "datadome=" in cookie:
                self._ready.put(cookie)
        except Exception as e:
            print(f"[cookie-pool] spare harvest failed: {e}", file=sys.stderr, flush=True)
        finally:
            self._harvesting.clear()

    def stop(self) -> None:
        self._stop.set()


_local = threading.local()


def _dd(cookie: str) -> str:
    i = (cookie or "").find("datadome=")
    return cookie[i + 9:i + 19] if i >= 0 else "NONE"


def get_session(pool: CookiePool) -> requests.Session:
    """Per-thread session built from the shared active cookie.

    Rebuilds only when the active cookie generation changes (i.e. after a burn).
    """
    cookie, gen = pool.current()
    if getattr(_local, "session", None) is None or getattr(_local, "gen", -1) != gen:
        _local.session = requests.Session(
            impersonate=cs.IMPERSONATE, headers=HEADERS,
            cookies=parse_cookie(cookie), timeout=60,
        )
        _local.gen = gen
    return _local.session


def worker_get(pool: CookiePool, limiter: RateLimiter, url: str, referer: str | None):
    """One GET. 429 -> global backoff (no swap). 403/challenge -> rotate cookie."""
    headers = {"referer": referer} if referer else None
    for attempt in range(6):
        pool.wait_if_paused()
        s = get_session(pool)
        gen = getattr(_local, "gen", 0)
        limiter.wait()
        try:
            r = s.get(url, headers=headers)
        except Exception:
            time.sleep(1.0 + attempt)
            continue
        if r.status_code == 429:
            # IP-wide throttle. Slow everyone down; reusing/replacing the cookie
            # would not help and just burns it toward a 403.
            limiter.slow_down()
            pool.backoff(4.0 + attempt * 4.0)
            continue
        if cs._is_challenge(r):
            # Genuine 403/captcha: the active cookie is burned. Rotate once.
            pool.report_burned(gen)
            continue
        limiter.on_success()
        return r
    raise NeedNewCookie()


def download_task(pool: CookiePool, limiter: RateLimiter, task: dict) -> tuple[dict, bool, str]:
    """Download one PDF using the shared cookie + adaptive rate limiting."""
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
        pool.maybe_prefetch_spare()
        return task, True, f"{len(r.content)//1024} KB"
    except NeedNewCookie:
        return task, False, "blocked after retries"


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
    """Main-thread listings; share the active cookie, rotate only on burn."""
    try:
        return worker_get(pool, limiter, url, referer)
    except NeedNewCookie:
        raise RuntimeError(
            f"Could not fetch {url} — session is blocked (403). "
            "Refresh the cookie via run.py and retry."
        )


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
    ap.add_argument("--workers", type=int, default=1,
                    help="Concurrent download workers (default 1; rate is the real ceiling)")
    ap.add_argument("--rate", type=float, default=platform_util.default_rate(),
                    help="Max total requests/sec across all workers")
    args = ap.parse_args()

    cs.OUT_ROOT = Path(args.out)
    pool = CookiePool(workers=args.workers)
    limiter = RateLimiter(args.rate)

    print("Cookie model: all workers share ONE active cookie; rotate only when "
          "it's burned (403). 429 -> global slowdown, not a swap.\n")

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
