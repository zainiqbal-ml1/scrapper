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

### Linux performance

Linux used to feel much slower than macOS because every cookie refresh launched a full SeleniumBase Chrome instance (30–60s+ each), and the pool tried to open several at startup.

The scraper now:

- Uses **system Chrome** for cookie harvest on Linux (faster than SeleniumBase UC).
- **Lazy cookie pool** — only opens a browser when a cookie is actually needed (one at a time).
- **429 backoff** — waits 2s with the same cookie before swapping (avoids unnecessary harvests).
- Defaults to **4 workers** and **4 req/s** on Linux (press Enter at the prompts to accept).

If downloads are still slow, try raising throughput (if your IP tolerates it):

```bash
python run.py --juris on --db onca --years 2024 --workers 6 --rate 6
```

If downloads stall after solving captcha, enable **Chrome > View > Developer > Allow JavaScript from Apple Events** (required for automatic cookie capture on macOS).

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
- When cookies expire, a **cookie pool** harvests new ones in the background (macOS: up to 3 AppleScript windows; Linux: one system-Chrome window at a time).
- Existing PDFs and completed years are skipped on resume.
- Failed downloads are retried until they succeed.

## Notes

- `data/` and `session.py` are not committed (scraped files and live cookies stay local).
- If blocked, solve the captcha in the Chrome window that appears; scraping resumes automatically.
