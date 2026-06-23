# CanLII Scraper

Download CanLII decision PDFs and per-year JSON metadata for any jurisdiction (Ontario, Canada federal, BC, etc.).

Output layout:

```
data/<state>/<db>/<year>/<decision>.pdf
data/<state>/<db>/<year>.json
```

Examples: `data/on/onca/2024/...`, `data/ca/scc/2023/...`

## Requirements

- Python 3.11+ (3.12 recommended; 3.14 works)
- Google Chrome or Chromium
- macOS, Linux, or Windows with a display (for captcha / cookie harvest)
- Optional: [Tor Browser](https://www.torproject.org/) for `--tor` routing

## Setup

```bash
git clone https://github.com/zainiqbal-ml1/scrapper.git
cd scrapper

python3 -m venv .venv
source .venv/bin/activate          # Linux/macOS
# .venv\Scripts\activate           # Windows

pip install --upgrade pip
pip install -r requirements.txt
```

On first run, `session.py` is created from `session.py.template`. A browser window opens to pass DataDome/captcha and mint a live cookie.

### Linux headless server

```bash
sudo apt install google-chrome-stable xvfb
xvfb-run python run.py
```

## Run

Interactive (recommended):

```bash
python run.py
```

Prompts: jurisdiction → Tor yes/no → database(s) → years → rate.

Non-interactive:

```bash
python run.py --juris on --db onca --years 2024 --rate 0.1-0.2
python run.py --tor --juris on --db onca --years 2024 --rate 0.1-0.2
python run.py --juris ca --db all --years all
```

### Tor

- Interactive: answer `y` when asked, or pass `--tor` / `CANLII_USE_TOR=1`
- Requires Tor Browser (port 9150) or `tor` daemon (port 9050)
- All CanLII HTTP and cookie harvest goes through Tor; backup cookies are auto-disabled (different exit IPs)
- Outbound IP is shown at startup and on the download progress line; it updates only when the cookie is refreshed

### Rate limiting

DataDome throttles by IP. Use one worker and a low rate (e.g. `0.1-0.2` req/s with Tor, `2-4` on a stable home IP).

### Other commands

```bash
python canlii_scraper.py --list-jurisdictions
python canlii_scraper.py --juris bc --list-dbs
python canlii_scraper.py --check
python auto_refresh.py
python set_session.py          # paste a Copy-as-cURL export
```

## How it works

- PDF downloads use `curl_cffi` with Chrome TLS impersonation and a `datadome` cookie.
- On 429/403, a fresh cookie is harvested (SeleniumBase auto-slider when permitted).
- Existing PDFs and completed years are skipped on resume.
- Failed downloads retry until permanent (404, not a PDF) or success.

## Local files (not committed)

| Path | Purpose |
|------|---------|
| `session.py` | Live cookie + user-agent |
| `data/` | Scraped PDFs and JSON |
| `.env` | Optional `CANLII_API_KEY` |
| `.cookie_state.json` | Rotated cookie cache |
| `.auto_solve_capable` | Mac auto-slider capability cache |

## macOS permissions

- **Chrome > View > Developer > Allow JavaScript from Apple Events** — faster cookie capture
- **System Settings > Privacy > Automation** — allow Terminal/Cursor to control Chrome
- **Screen Recording + Accessibility** — for PyAutoGUI slider auto-solve

Without these, a visible Chrome window opens for manual captcha solve.
