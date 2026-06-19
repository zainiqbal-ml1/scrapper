"""CanLII REST API client for metadata discovery (no PDFs).

Set CANLII_API_KEY in the environment. Discovery via API avoids website
listing requests that trigger DataDome; PDF downloads still use the scraper.

Limits: 5,000 requests/day, 2 req/sec, 1 concurrent (enforced here).
"""
from __future__ import annotations

import json
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date
from pathlib import Path

import bootstrap

API_BASE = "https://api.canlii.org/v1"
MAX_RESULT_COUNT = 10_000
RATE_INTERVAL = 0.51  # ~2 req/sec
DAILY_LIMIT = 5_000
USAGE_FILE = Path(".api_usage.json")
YEAR_RE = re.compile(r"/doc/(\d{4})/")

_client: "CanLIIClient | None" = None
_client_lock = threading.Lock()


def enabled() -> bool:
    bootstrap.load_env_file()
    return bool(os.environ.get("CANLII_API_KEY", "").strip())


def _today() -> str:
    return date.today().isoformat()


def _load_usage() -> tuple[str, int]:
    if not USAGE_FILE.exists():
        return _today(), 0
    try:
        data = json.loads(USAGE_FILE.read_text())
        day = data.get("date", _today())
        count = int(data.get("count", 0))
        if day != _today():
            return _today(), 0
        return day, count
    except Exception:
        return _today(), 0


def _save_usage(count: int) -> None:
    try:
        USAGE_FILE.write_text(json.dumps({"date": _today(), "count": count}))
    except Exception:
        pass


class CanLIIAPIError(RuntimeError):
    pass


class CanLIIClient:
    def __init__(self, api_key: str):
        self.api_key = api_key.strip()
        self._lock = threading.Lock()
        self._next_at = 0.0
        self._day, self._count = _load_usage()

    def _throttle(self) -> None:
        with self._lock:
            if self._day != _today():
                self._day, self._count = _today(), 0
            if self._count >= DAILY_LIMIT:
                raise CanLIIAPIError(
                    f"CanLII API daily limit reached ({DAILY_LIMIT} requests). "
                    "Try again tomorrow or unset CANLII_API_KEY to use website discovery."
                )
            now = time.monotonic()
            wait = max(0.0, self._next_at - now)
            self._next_at = max(now, self._next_at) + RATE_INTERVAL
            self._count += 1
            _save_usage(self._count)
        if wait:
            time.sleep(wait)

    def get_json(self, path: str, params: dict | None = None) -> dict:
        self._throttle()
        q = dict(params or {})
        q["api_key"] = self.api_key
        url = f"{API_BASE}{path}?{urllib.parse.urlencode(q)}"
        req = urllib.request.Request(url, headers={"User-Agent": "canlii-scraper/1.0"})
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.loads(resp.read().decode())
        except urllib.error.HTTPError as e:
            body = e.read().decode(errors="replace")[:500]
            raise CanLIIAPIError(f"HTTP {e.code} for {path}: {body}") from e
        except urllib.error.URLError as e:
            raise CanLIIAPIError(f"Network error for {path}: {e}") from e

    def requests_today(self) -> int:
        with self._lock:
            if self._day != _today():
                return 0
            return self._count


def client() -> CanLIIClient:
    global _client
    if not enabled():
        raise CanLIIAPIError("CANLII_API_KEY is not set")
    with _client_lock:
        if _client is None:
            _client = CanLIIClient(os.environ["CANLII_API_KEY"])
        return _client


def discover_databases(juris: str) -> dict[str, str]:
    """All database codes -> names within a jurisdiction."""
    data = client().get_json("/caseBrowse/en/")
    dbs: dict[str, str] = {}
    for row in data.get("caseDatabases", []):
        if row.get("jurisdiction") == juris:
            code = row.get("databaseId", "")
            name = (row.get("name") or code).strip()
            if code:
                dbs[code] = name
    if not dbs:
        raise CanLIIAPIError(f"No databases returned for jurisdiction {juris!r}")
    return dbs


def get_years(juris: str, db: str, cache_root: Path) -> list[int]:
    """Years with decisions in a database (cached on disk)."""
    cache = cache_root / ".years_cache" / f"{juris}_{db}.json"
    if cache.exists():
        try:
            years = json.loads(cache.read_text())
            if years:
                return years
        except Exception:
            pass

    years: set[int] = set()
    offset = 0
    c = client()
    while True:
        data = c.get_json(
            f"/caseBrowse/en/{db}/",
            {"offset": offset, "resultCount": MAX_RESULT_COUNT},
        )
        batch = data.get("cases", [])
        if not batch:
            break
        for case in batch:
            long_url = case.get("longUrl") or ""
            m = YEAR_RE.search(long_url)
            if m:
                years.add(int(m.group(1)))
            else:
                cid = (case.get("caseId") or {}).get("en", "")
                if len(cid) >= 4 and cid[:4].isdigit():
                    years.add(int(cid[:4]))
        if len(batch) < MAX_RESULT_COUNT:
            break
        offset += MAX_RESULT_COUNT

    result = sorted(years, reverse=True)
    if result:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(result))
    return result


def _case_to_item(case: dict, *, year: int) -> dict | None:
    long_url = (case.get("longUrl") or "").strip()
    if not long_url:
        return None
    if long_url.startswith("https://www.canlii.org"):
        html_path = long_url[len("https://www.canlii.org") :]
    elif long_url.startswith("http://www.canlii.org"):
        html_path = long_url[len("http://www.canlii.org") :]
    else:
        html_path = long_url

    title = (case.get("title") or "").strip()
    citation = (case.get("citation") or "").strip().replace(" (CanLII)", "")
    if not citation and not title:
        return None

    m = YEAR_RE.search(html_path)
    y = int(m.group(1)) if m else year

    return {
        "url": html_path,
        "styleOfCause": title,
        "citation": citation,
        "judgmentDate": f"{y}-01-01",
    }


def get_items(juris: str, db: str, year: int, cache_root: Path) -> list[dict]:
    """Decision list for one database/year — same shape as the website JSON endpoint."""
    cache = cache_root / ".api_cache" / f"{juris}_{db}_{year}.json"
    if cache.exists():
        try:
            items = json.loads(cache.read_text())
            if isinstance(items, list):
                return items
        except Exception:
            pass

    items: list[dict] = []
    offset = 0
    c = client()
    params = {
        "offset": offset,
        "resultCount": MAX_RESULT_COUNT,
        "decisionDateAfter": f"{year}-01-01",
        "decisionDateBefore": f"{year}-12-31",
    }
    while True:
        params["offset"] = offset
        data = c.get_json(f"/caseBrowse/en/{db}/", params)
        batch = data.get("cases", [])
        if not batch:
            break
        for case in batch:
            item = _case_to_item(case, year=year)
            if item:
                items.append(item)
        if len(batch) < MAX_RESULT_COUNT:
            break
        offset += MAX_RESULT_COUNT

    if items:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(items, ensure_ascii=False))
    return items


def print_status() -> None:
    """Print whether API discovery is active."""
    if not enabled():
        print("Discovery: website (set CANLII_API_KEY for API metadata listing)", flush=True)
        return
    try:
        c = client()
        used = c.requests_today()
        print(
            f"Discovery: CanLII API ({used}/{DAILY_LIMIT} requests today)",
            flush=True,
        )
    except Exception as e:
        print(f"Discovery: website (API unavailable: {e})", flush=True)
