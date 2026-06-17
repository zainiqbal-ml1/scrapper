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

### Performance and rate limiting

CanLII's anti-bot (DataDome) throttles by **IP address**, so the scraper uses a **single download worker** and controls throughput with **requests/second**.

- A **cookie pool** keeps cookies ready (4 on Linux with fast harvest; **on-demand only on Mac** without Apple Events).
- On **any error** (429, 403, network) the scraper **discards that cookie and grabs the next one** — never retries with a burned cookie.
- **Mac without Apple Events**: only **one browser window** opens when a cookie is actually burned — no background pop-up storm during downloads.
- Pick a `--rate` your IP tolerates. Start around `2`-`4`; lower it if you see frequent 429s.

```bash
python run.py --juris on --db onca --years 2024 --rate 3
```

If downloads stall after solving captcha, enable **Chrome > View > Developer > Allow JavaScript from Apple Events** (required for automatic cookie capture on macOS).

Also allow **Terminal/Cursor to control Google Chrome** when macOS prompts (System Settings > Privacy & Security > Automation). Without this, the scraper falls back to SeleniumBase — a visible Chrome window you solve manually.

## Run

Interactive (recommended):

```bash
python run.py
```

You will be prompted for:

1. Jurisdiction (`on`, `ca`, `bc`, … or `all`)
2. Database(s) (numbers/codes or `all`)
3. Years (`all`, `2024`, `2020-2024`, etc.)
4. Max requests/second

Non-interactive:

```bash
python run.py --juris on --db onca onsc --years 2020-2024 --rate 3
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
- When cookies run out, a **cookie pool** harvests replacements in the background (up to 2 parallel on macOS, 1 on Linux).
- Existing PDFs and completed years are skipped on resume.
- Failed downloads are retried until they succeed.

## Notes

- `data/` and `session.py` are not committed (scraped files and live cookies stay local).
- If blocked, solve the captcha in the Chrome window that appears; scraping resumes automatically.
