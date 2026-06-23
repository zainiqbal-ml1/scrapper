#!/usr/bin/env python3
"""CanLII PDF scraper (all jurisdictions).

Downloads decision PDFs from CanLII, organised as:

    data/<state>/<db>/<year>/<decision name>.pdf
    data/<state>/<db>/<year>.json   # [{title, citation, date, pdf_url, html_url}, ...]

where <state> is a CanLII jurisdiction code (on=Ontario, ca=Canada federal,
bc=British Columbia, ...). It reuses your real browser session (see session.py)
and impersonates Chrome's TLS fingerprint via curl_cffi so it sails past the
DataDome anti-bot wall without opening a browser.

If you start getting 403s, your session expired: refresh it (run.py does this
automatically; or update session.py via "Copy as cURL").

Examples:
    # One jurisdiction + database, one year (good first test)
    python canlii_scraper.py --juris on --db onafraat --years 2026

    # All databases in a jurisdiction, all years
    python canlii_scraper.py --juris ca --db all --years all

    # A few databases, a year range, max 50 docs/year
    python canlii_scraper.py --juris on --db onca onsc --years 2020-2024 --limit 50

    # List jurisdictions, or databases within one
    python canlii_scraper.py --list-jurisdictions
    python canlii_scraper.py --juris bc --list-dbs

    # Fully interactive (pick state, then db(s), then years)
    python canlii_scraper.py
"""
from __future__ import annotations

import argparse
import json
import random
import re
import sys
import time
from pathlib import Path

from curl_cffi import requests

import bootstrap
bootstrap.ensure_session_file()
from session import HEADERS, cookies_dict

import canlii_api
import tor_util

BASE = "https://www.canlii.org"
IMPERSONATE = "chrome146"
OUT_ROOT = Path("data")
CHECK_TIMEOUT = 20  # fast session probe (don't block the interactive menu)

# CanLII jurisdictions ("states"): code -> display name. This set is stable
# (Canada's federal + provinces/territories), so we don't need a live fetch to
# show the menu - and it works even while the session is temporarily blocked.
JURISDICTIONS = {
    "ca": "Canada (Federal)",
    "on": "Ontario",
    "qc": "Quebec",
    "bc": "British Columbia",
    "ab": "Alberta",
    "mb": "Manitoba",
    "sk": "Saskatchewan",
    "ns": "Nova Scotia",
    "nb": "New Brunswick",
    "nl": "Newfoundland and Labrador",
    "pe": "Prince Edward Island",
    "nt": "Northwest Territories",
    "yk": "Yukon",
    "nu": "Nunavut",
}

# Hardcoded fallback list of Ontario databases (code -> display name), used only
# if live discovery fails for Ontario. Refreshed via --list-dbs.
ONTARIO_DBS_FALLBACK = {
    "onca": "Court of Appeal for Ontario",
    "onsc": "Superior Court of Justice",
    "onscdc": "Divisional Court",
    "oncj": "Ontario Court of Justice",
}


class SessionExpired(RuntimeError):
    """Raised when CanLII returns the DataDome challenge (403)."""


COOKIE_STATE = Path(".cookie_state.json")


def _load_cookies() -> dict:
    """Prefer the freshest rotated cookies saved from a previous run."""
    if COOKIE_STATE.exists():
        try:
            saved = json.loads(COOKIE_STATE.read_text())
            base = cookies_dict()
            base.update(saved)
            return base
        except Exception:
            pass
    return cookies_dict()


def save_cookies(session: requests.Session) -> None:
    try:
        jar = {c.name: c.value for c in session.cookies.jar}
        if jar:
            COOKIE_STATE.write_text(json.dumps(jar))
    except Exception:
        pass


def make_session() -> requests.Session:
    return requests.Session(
        impersonate=IMPERSONATE, headers=HEADERS, cookies=_load_cookies(), timeout=60
    )


def check_session(juris: str = "on") -> bool:
    """True when the session can load a jurisdiction landing page (fast, ~20s max)."""
    cookies = _load_cookies()
    if "datadome" not in cookies:
        return False
    try:
        session = requests.Session(
            impersonate=IMPERSONATE,
            headers=HEADERS,
            cookies=_load_cookies(),
            timeout=CHECK_TIMEOUT,
        )
        r = tor_util.session_get(session, f"{BASE}/en/{juris}/")
        if _is_challenge(r) or r.status_code != 200:
            return False
        dbs = parse_databases_html(r.text, juris)
        return bool(dbs)
    except Exception:
        return False


def _is_challenge(r) -> bool:
    """True if the response is an anti-bot challenge, not real content.

    Covers both the DataDome interstitial (403 + JS challenge) and CanLII's own
    native captcha page ("CanLII calls upon users...") which is served with 200.
    """
    head = r.content[:4000].lower()
    if is_ip_blocked_response(r):
        return True
    if r.status_code in (403, 405):
        if b"please enable js" in head or b"captcha-delivery" in head or b"datadome" in head:
            return True
    # CanLII's native captcha page (can return HTTP 200)
    if b"calls upon users accessing" in head or b"proceed with our captcha" in head:
        return True
    return False


def is_ip_blocked_response(r) -> bool:
    """True when DataDome has hard-blocked this IP (new cookies won't help yet)."""
    head = (r.content[:8000] if r.content else b"").lower()
    return (
        b"temporarily blocked" in head
        or b"access is temporarily blocked" in head
        or b"you have been blocked" in head
    )


def fetch(session: requests.Session, url: str, *, tries: int = 5, referer: str | None = None):
    """GET with retries on transient connection resets / brief challenges.

    Raises SessionExpired if the DataDome challenge persists (session needs a
    fresh 'Copy as cURL').
    """
    headers = {"referer": referer} if referer else None
    net_err = 0
    while True:
        try:
            r = tor_util.session_get(session, url, headers=headers)
        except Exception as e:  # connection reset / TLS hiccup
            net_err += 1
            if net_err > tries:
                raise RuntimeError(f"Failed after {tries} network errors: {url} ({e})")
            continue

        if r.status_code == 429:
            # Rate limited. Don't sit and wait - refresh the session (a fresh
            # cookie from a new incognito usually resets the rate counter).
            raise SessionExpired(
                f"HTTP 429 (rate limited) for {url}.\n"
                "  -> Refreshing the session (new cookie) instead of waiting."
            )

        if _is_challenge(r):
            raise SessionExpired(
                f"DataDome challenge (HTTP {r.status_code}) for {url}.\n"
                "  -> Refresh your session (solve the slider) and it will resume."
            )
        return r


def discover_databases(session: requests.Session, juris: str) -> dict[str, str]:
    """All database codes -> names within a jurisdiction."""
    if canlii_api.enabled():
        try:
            return canlii_api.discover_databases(juris)
        except Exception as e:
            print(f"[api] database list failed ({e}) — using website", file=sys.stderr, flush=True)
    return _discover_databases_web(session, juris)


def discover_databases_for_juris(juris: str, session: requests.Session | None = None) -> dict[str, str]:
    """List databases; uses API when configured (no session/captcha needed)."""
    if canlii_api.enabled():
        return canlii_api.discover_databases(juris)
    if session is None:
        session = make_session()
    return _discover_databases_web(session, juris)


def _discover_databases_web(session: requests.Session, juris: str) -> dict[str, str]:
    r = fetch(session, f"{BASE}/en/{juris}/")
    return parse_databases_html(r.text, juris)


def parse_databases_html(html: str, juris: str) -> dict[str, str]:
    """Parse database links from a jurisdiction landing page."""
    pairs = re.findall(rf'<a class="canlii" href="/{juris}/([a-z0-9]+)">([^<]+)</a>', html)
    dbs: dict[str, str] = {}
    for code, name in pairs:
        dbs.setdefault(code, name.strip())
    if not dbs and juris == "on":
        return dict(ONTARIO_DBS_FALLBACK)
    return dbs


def get_years(session: requests.Session, juris: str, db: str) -> list[int]:
    """All years that have decisions for a database.

    Cached to disk so resumes don't spend cookie budget re-fetching it.
    """
    if canlii_api.enabled():
        try:
            return canlii_api.get_years(juris, db, OUT_ROOT)
        except Exception as e:
            print(f"[api] year list failed ({e}) — using website", file=sys.stderr, flush=True)
    return _get_years_web(session, juris, db)


def _get_years_web(session: requests.Session, juris: str, db: str) -> list[int]:
    cache = OUT_ROOT / ".years_cache" / f"{juris}_{db}.json"
    if cache.exists():
        try:
            return json.loads(cache.read_text())
        except Exception:
            pass
    r = fetch(session, f"{BASE}/en/{juris}/{db}/", referer=f"{BASE}/en/{juris}/")
    years = sorted({int(y) for y in re.findall(rf"/{juris}/{db}/nav/date/(\d{{4}})", r.text)}, reverse=True)
    if years:
        cache.parent.mkdir(parents=True, exist_ok=True)
        cache.write_text(json.dumps(years))
    return years


def get_items(session: requests.Session, juris: str, db: str, year: int) -> list[dict]:
    """List of decisions for a database/year."""
    if canlii_api.enabled():
        try:
            return canlii_api.get_items(juris, db, year, OUT_ROOT)
        except Exception as e:
            print(f"[api] item list failed ({e}) — using website", file=sys.stderr, flush=True)
    return _get_items_web(session, juris, db, year)


def _get_items_web(session: requests.Session, juris: str, db: str, year: int) -> list[dict]:
    r = fetch(session, f"{BASE}/{juris}/{db}/nav/date/{year}/items", referer=f"{BASE}/en/{juris}/{db}/")
    if r.status_code != 200:
        return []
    try:
        return r.json()
    except Exception:
        return []


_ILLEGAL = re.compile(r'[<>:"/\\|?*\x00-\x1f]')


def sanitize_filename(name: str, max_len: int = 180) -> str:
    name = _ILLEGAL.sub(" ", name)
    name = re.sub(r"\s+", " ", name).strip().strip(".")
    if len(name) > max_len:
        name = name[:max_len].rstrip()
    return name or "untitled"


def html_to_pdf_url(html_url: str) -> str:
    path = html_url
    if path.endswith(".html"):
        path = path[: -len(".html")] + ".pdf"
    if path.startswith("/"):
        return BASE + path
    return path


def download_pdf(session: requests.Session, pdf_url: str, dest: Path, referer: str | None = None) -> tuple[bool, str]:
    """Download a single PDF. Returns (ok, message)."""
    r = fetch(session, pdf_url, referer=referer)
    if r.status_code != 200:
        return False, f"HTTP {r.status_code}"
    if not r.content.startswith(b"%PDF"):
        return False, f"not a PDF (got {r.headers.get('content-type')})"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(r.content)
    return True, f"{len(r.content)//1024} KB"


def _fmt_size(num_bytes: int) -> str:
    units = ["B", "KB", "MB", "GB", "TB"]
    size = float(num_bytes)
    for u in units:
        if size < 1024 or u == units[-1]:
            return f"{size:.1f} {u}" if u != "B" else f"{int(size)} B"
        size /= 1024


def year_is_complete(juris: str, db: str, year: int) -> bool:
    """True when JSON exists and every listed PDF is present (no errors)."""
    jpath = OUT_ROOT / juris / db / f"{year}.json"
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
        p = OUT_ROOT / fp
        if not p.exists() or p.stat().st_size == 0:
            return False
    return True


def scrape_db_year(
    session: requests.Session,
    juris: str,
    db: str,
    year: int,
    *,
    download: bool = True,
    limit: int | None = None,
    delay: float = 0.4,
    skip_existing: bool = True,
) -> dict:
    items = get_items(session, juris, db, year)
    if limit:
        items = items[:limit]
    total = len(items)

    year_dir = OUT_ROOT / juris / db / str(year)
    index: list[dict] = []
    ok = fail = skipped = 0
    bytes_dl = 0
    label = f"{juris}/{db}/{year}"
    failed: list[tuple[dict, str, Path, str, str]] = []  # record, pdf_url, dest, referer, citation

    def progress(done: int) -> None:
        bar = f"  {label}: [{done}/{total}] ok={ok} skip={skipped} fail={fail} ({_fmt_size(bytes_dl)})"
        sys.stdout.write("\r" + bar.ljust(78))
        sys.stdout.flush()

    progress(0)
    for i, it in enumerate(items, 1):
        html_url = it.get("url")
        if not html_url:
            continue
        style = (it.get("styleOfCause") or "").strip()
        citation = (it.get("citation") or "").strip().replace(" (CanLII)", "")
        date = it.get("judgmentDate", "")
        pdf_url = html_to_pdf_url(html_url)

        base_name = sanitize_filename(f"{citation} - {style}" if style else citation)
        dest = year_dir / f"{base_name}.pdf"

        record = {
            "title": style or citation,
            "citation": citation,
            "date": date,
            "pdf_url": pdf_url,
            "html_url": BASE + html_url if html_url.startswith("/") else html_url,
            "file": str(dest.relative_to(OUT_ROOT)) if download else None,
        }

        if download:
            if skip_existing and dest.exists() and dest.stat().st_size > 0:
                skipped += 1
            else:
                doc_html = BASE + html_url if html_url.startswith("/") else html_url
                got, msg = download_pdf(session, pdf_url, dest, referer=doc_html)
                if got:
                    ok += 1
                    if dest.exists():
                        bytes_dl += dest.stat().st_size
                else:
                    fail += 1
                    record["file"] = None
                    record["error"] = msg
                    failed.append((record, pdf_url, dest, doc_html, citation))
                    sys.stdout.write("\r" + " " * 78 + "\r")
                    print(f"    [{i}/{total}] FAILED {citation}: {msg}")
                save_cookies(session)  # persist rotated datadome cookie
                time.sleep(delay + random.uniform(0, delay))  # jitter
            progress(i)
        index.append(record)

    # Retry failed downloads before moving on.
    for rnd in range(1, 4):
        if not failed:
            break
        print(f"  {label}: retrying {len(failed)} failed (round {rnd}/3)...")
        still: list[tuple[dict, str, Path, str, str]] = []
        for record, pdf_url, dest, referer, citation in failed:
            got, msg = download_pdf(session, pdf_url, dest, referer=referer)
            if got:
                ok += 1
                fail -= 1
                record["file"] = str(dest.relative_to(OUT_ROOT))
                record.pop("error", None)
                if dest.exists():
                    bytes_dl += dest.stat().st_size
            else:
                record["file"] = None
                record["error"] = msg
                still.append((record, pdf_url, dest, referer, citation))
                print(f"    RETRY FAILED {citation}: {msg}")
            save_cookies(session)
            time.sleep(delay + random.uniform(0, delay))
        failed = still

    # write per-year JSON index (title + url of everything found)
    if index:
        year_dir.mkdir(parents=True, exist_ok=True)
        (OUT_ROOT / juris / db / f"{year}.json").write_text(
            json.dumps(index, indent=2, ensure_ascii=False)
        )
    # finalise the line
    sys.stdout.write("\r" + " " * 78 + "\r")
    print(f"  {label}: {total} decisions -> downloaded {ok}, skipped {skipped}, "
          f"failed {fail}  ({_fmt_size(bytes_dl)})")
    return {
        "year": year, "total": total, "downloaded": ok,
        "skipped": skipped, "failed": fail, "bytes": bytes_dl,
    }


def parse_years_arg(arg: str, available: list[int]) -> list[int]:
    if arg == "all":
        return available
    out: set[int] = set()
    for part in arg.split(","):
        part = part.strip()
        if "-" in part:
            a, b = part.split("-", 1)
            out.update(range(int(a), int(b) + 1))
        elif part:
            out.add(int(part))
    # keep only years that actually exist, newest first
    return sorted(out & set(available), reverse=True) if available else sorted(out, reverse=True)


# --------------------------------------------------------------------------- #
# Interactive selection helpers (used by run.py and standalone interactive mode)
# --------------------------------------------------------------------------- #

def select_jurisdiction() -> str:
    """Show the jurisdiction menu; return a single code, or 'all' for every one."""
    codes = list(JURISDICTIONS.keys())
    print("\nCanLII jurisdictions:\n")
    for i, code in enumerate(codes, 1):
        print(f"  {i:2d}. {code:3s}  {JURISDICTIONS[code]}")
    print("  all. every jurisdiction\n")
    while True:
        choice = input("Select a jurisdiction (number or code, 'all'): ").strip().lower()
        if choice == "all":
            return "all"
        if choice in JURISDICTIONS:
            return choice
        if choice.isdigit() and 1 <= int(choice) <= len(codes):
            return codes[int(choice) - 1]
        print("  Invalid choice, try again.")


def select_databases(all_dbs: dict[str, str]) -> list[str]:
    """Show the database menu for a jurisdiction; return chosen codes (or all)."""
    codes = list(all_dbs.keys())
    print(f"\n{len(codes)} databases:\n")
    for i, code in enumerate(codes, 1):
        print(f"  {i:3d}. {code:14s} {all_dbs[code]}")
    print("  all. every database\n")
    while True:
        raw = input("Select database(s) (numbers/codes, comma or space separated, or 'all'): ").strip().lower()
        if raw == "all":
            return codes
        chosen: list[str] = []
        ok = True
        for tok in re.split(r"[,\s]+", raw):
            if not tok:
                continue
            if tok in all_dbs:
                chosen.append(tok)
            elif tok.isdigit() and 1 <= int(tok) <= len(codes):
                chosen.append(codes[int(tok) - 1])
            else:
                print(f"  '{tok}' is not a valid number or code.")
                ok = False
                break
        if ok and chosen:
            # de-dup, preserve order
            seen: set[str] = set()
            return [c for c in chosen if not (c in seen or seen.add(c))]
        if ok:
            print("  Nothing selected, try again.")


def select_years() -> str:
    """Ask for a years spec; returns a string parse_years_arg understands."""
    raw = input("Years ('all', a year like 2024, list 2020,2022, or range 2018-2024) [all]: ").strip()
    return raw or "all"


def _scrape_one_jurisdiction(session, juris, db_arg, args, grand) -> None:
    """Scrape selected dbs for a single jurisdiction; mutate `grand` totals."""
    all_dbs = discover_databases(session, juris)
    if db_arg == ["all"]:
        targets = list(all_dbs.keys())
    else:
        targets = db_arg
        for code in targets:
            if code not in all_dbs:
                print(f"  WARNING: '{code}' not in {juris} database list", file=sys.stderr)

    juris_name = JURISDICTIONS.get(juris, juris)
    print(f"\n=== {juris} ({juris_name}): {len(targets)} database(s) ===")

    for di, db in enumerate(targets, 1):
        print(f"\n[{di}/{len(targets)}] {juris}/{db}  {all_dbs.get(db, '?')}")
        years_available = get_years(session, juris, db)
        years = parse_years_arg(args.years, years_available)
        if not years:
            print("  no matching years, skipping")
            continue

        db_ok = db_skip = db_fail = db_total = 0
        db_bytes = 0
        for year in years:
            if not args.no_pdf and not args.limit and year_is_complete(juris, db, year):
                print(f"  {juris}/{db}/{year}: already complete, skipping")
                continue
            st = scrape_db_year(
                session, juris, db, year,
                download=not args.no_pdf,
                limit=args.limit,
                delay=args.delay,
                skip_existing=not args.no_skip,
            )
            db_total += st["total"]; db_ok += st["downloaded"]
            db_skip += st["skipped"]; db_fail += st["failed"]; db_bytes += st.get("bytes", 0)

        print(f"  --- {juris}/{db} totals: {db_total} decisions | "
              f"downloaded {db_ok}, skipped {db_skip}, failed {db_fail} ({_fmt_size(db_bytes)}) ---")
        grand["dbs"] += 1
        grand["total"] += db_total; grand["downloaded"] += db_ok
        grand["skipped"] += db_skip; grand["failed"] += db_fail; grand["bytes"] += db_bytes


def main() -> int:
    ap = argparse.ArgumentParser(description="CanLII PDF scraper (all jurisdictions)")
    ap.add_argument("--juris", help="Jurisdiction code e.g. on ca bc, or 'all'")
    ap.add_argument("--db", nargs="+", help="Database code(s) e.g. onca onsc, or 'all'")
    ap.add_argument("--years", default="all", help="'all', a year (2024), list (2020,2022), or range (2020-2024)")
    ap.add_argument("--out", default="data", help="Output root folder (default: data)")
    ap.add_argument("--limit", type=int, default=None, help="Max decisions per year (testing)")
    ap.add_argument("--delay", type=float, default=0.6, help="Base seconds between PDF downloads (plus random jitter)")
    ap.add_argument("--no-pdf", action="store_true", help="Only build JSON index, don't download PDFs")
    ap.add_argument("--no-skip", action="store_true", help="Re-download even if file exists")
    ap.add_argument("--list-jurisdictions", action="store_true", help="List all jurisdictions and exit")
    ap.add_argument("--list-dbs", action="store_true", help="List databases in --juris and exit")
    ap.add_argument("--check", action="store_true", help="Test the session (exit 0 ok / 2 blocked) and exit")
    args = ap.parse_args()

    global OUT_ROOT
    OUT_ROOT = Path(args.out)

    if args.list_jurisdictions:
        print(f"{len(JURISDICTIONS)} jurisdictions:\n")
        for code, name in JURISDICTIONS.items():
            print(f"  {code:3s}  {name}")
        return 0

    session = make_session()

    # --check: validate the session against the chosen (or Ontario) jurisdiction.
    if args.check:
        juris = args.juris if args.juris and args.juris != "all" else "on"
        if check_session(juris):
            print("SESSION OK")
            return 0
        print("SESSION BLOCKED")
        return 2

    if args.list_dbs:
        if not args.juris or args.juris == "all":
            ap.error("--list-dbs needs a single --juris (e.g. --juris bc)")
        try:
            dbs = discover_databases(session, args.juris)
        except SessionExpired as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        print(f"{len(dbs)} databases in {args.juris} ({JURISDICTIONS.get(args.juris, '?')}):\n")
        for code, name in dbs.items():
            print(f"  {code:14s} {name}")
        return 0

    # Resolve selection: interactive if --juris/--db not supplied.
    juris_choice = args.juris
    if not juris_choice:
        juris_choice = select_jurisdiction()
        if juris_choice == "all":
            args.db = ["all"]
        else:
            try:
                dbs = discover_databases(session, juris_choice)
            except SessionExpired as e:
                print(f"\nSession is blocked - can't list databases right now.\n"
                      f"Use run.py (it refreshes automatically): "
                      f"python run.py --juris {juris_choice} --db all\n{e}", file=sys.stderr)
                return 2
            args.db = select_databases(dbs)
        args.years = select_years()
    elif not args.db:
        ap.error("provide --db <code...> or 'all' (or omit --juris for interactive mode)")

    jurisdictions = list(JURISDICTIONS.keys()) if juris_choice == "all" else [juris_choice]
    grand = {"dbs": 0, "total": 0, "downloaded": 0, "skipped": 0, "failed": 0, "bytes": 0}

    print(f"\nScraping into ./{OUT_ROOT}/  (structure: {OUT_ROOT}/<state>/<db>/<year>/)\n")
    try:
        for juris in jurisdictions:
            _scrape_one_jurisdiction(session, juris, args.db, args, grand)
    except SessionExpired as e:
        print(f"\nERROR: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted. Re-run with the same args to resume (existing files are skipped).")
        return 130

    print("\n" + "=" * 60)
    print(f" DONE - {grand['dbs']} databases, {grand['total']} decisions")
    print(f"   downloaded {grand['downloaded']}, skipped {grand['skipped']}, "
          f"failed {grand['failed']}  ({_fmt_size(grand['bytes'])})")
    print("=" * 60)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
