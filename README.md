# CanLII Scraper

Download CanLII decision PDFs and per-year JSON metadata for any jurisdiction (Ontario, Canada federal, BC, etc.).

Output layout:

```
data/<state>/<db>/<year>/<decision>.pdf
data/<state>/<db>/<year>.json
```

Examples: `data/on/onca/2024/...`, `data/ca/scc/2023/...`

## Requirements

- Python 3.11+ (3.12 recommended)
- Google Chrome or Chromium
- macOS, Linux, or Windows with a graphical display (for cookie refresh / captcha)

## Setup (new machine)

```bash
git clone https://github.com/zainiqbal-ml1/scrapper.git
cd scrapper

python3 -m venv .venv
source .venv/bin/activate          # Linux/macOS
# .venv\Scripts\activate           # Windows

pip install --upgrade pip
pip install -r requirements.txt
```

On first run, `session.py` is created automatically from `session.py.template`. A browser window opens so you can pass DataDome/captcha and mint a live cookie.

### Linux extras

```bash
# Ubuntu/Debian
sudo apt install google-chrome-stable   # or chromium-browser

# headless server (no display)
sudo apt install xvfb
xvfb-run python run.py
```

## Run

Interactive (recommended):

```bash
python run.py
```

You will be prompted for:

1. Jurisdiction (`on`, `ca`, `bc`, … or `all`)
2. Database(s) (numbers/codes or `all`)
3. Years (`all`, `2024`, `2020-2024`, etc.)
4. Workers and requests/second

Non-interactive:

```bash
python run.py --juris on --db onca onsc --years 2020-2024 --workers 3 --rate 3
python run.py --juris ca --db all --years all
```

### Other commands

```bash
# list jurisdictions
python canlii_scraper.py --list-jurisdictions

# list databases in a jurisdiction
python canlii_scraper.py --juris bc --list-dbs

# test session
python canlii_scraper.py --check

# refresh session manually
python auto_refresh.py
```

## How it works

- Downloads use `curl_cffi` with Chrome TLS impersonation and a `datadome` cookie.
- When cookies expire, a **cookie pool** opens background Chrome windows (up to 3 in parallel) to harvest new ones while downloads continue.
- Existing PDFs and completed years are skipped on resume.
- Failed downloads are retried until they succeed.

## Notes

- `data/` and `session.py` are not committed (scraped files and live cookies stay local).
- If blocked, solve the captcha in the Chrome window that appears; scraping resumes automatically.
