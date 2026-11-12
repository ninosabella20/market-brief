# Market Brief

A free, self-updating market dashboard: sector performance for the 11 GICS sectors + S&P 500, and the top 5 verified stories per sector, refreshed once a day, **ready by 07:00 Europe/Madrid**.

- **Site:** static page in `docs/` (served by GitHub Pages), reads `docs/data.json`.
- **Data:** `scripts/build.py`, run by GitHub Actions (`.github/workflows/refresh.yml`).
  - Prices, returns, company facts, consensus and analyst ratings: **Yahoo Finance via yfinance** (no key).
  - News candidates: **Yahoo Finance news search** (no key): for each sector the ETF's top 10 holdings plus a few themed queries (e.g. "European banks"), last 4 days, with each article's description and picture.
  - **Long-term picks** (tap a sector): 3 companies per sector chosen by Gemini for a buy-and-hold investor (minimum 5-year holds, ideally 10+): quality businesses at a reasonable price. Each pick has a horizon ("5+ years" / "10+ years"), an angle (Compounder, Quality at a discount, Dividend grower, Long-term growth) and a short reason. Candidates are the ETF's top holdings + that sector's story companies; Gemini only sees real Yahoo numbers (5Y/1Y return, ROE, margin, revenue growth, debt/equity, FCF and dividend yield, P/E, distance from 52-week high, consensus). Numbers shown come from Yahoo, not the model, and tickers outside the candidate list are dropped. Tune the philosophy in `PICKS_PROMPT`, the count with `PICKS_PER_SECTOR`, or turn picks off with `SKIP_PICKS=1`. Ideas to research, not advice.
  - Story selection and writing, company one-liners, market recap and "why it moved": **Google Gemini API free tier**, *without* Google Search grounding (grounding isn't in the free tier for new keys). Gemini only picks from the real articles it is given, so every link is real.
- **Cost:** €0. Public repo + GitHub Pages + Actions minutes (free and unlimited for public repos) + Gemini free tier. Each run makes 13 Gemini calls (one per sector, one for explanations, one for AI picks), once a day. The free tier allows **20 requests per day per model** (measured Nov 2026; resets at midnight Pacific), so a daily run fits within one model, and other models act as a backup.

## How it works

1. The goal is fresh data **ready by 07:00 Madrid**, once a day. Cron fires at 04:10, 04:40, 05:10 and 05:40 UTC. `build.py` only proceeds inside the window 06:00–08:00 Madrid **and** only if today's refresh hasn't been built yet. In practice the build starts around 06:10 and finishes 10–15 minutes later, plus GitHub's usual 5–30 minute start delay. This handles summer/winter time and late or dropped cron runs. Numbers are the previous US close.
2. Each part (numbers, explanations, each sector's stories) refreshes independently. If one fails, the previous good copy is kept and the site shows a yellow "Last refresh was partial" notice.
3. The site shows a "Data is over a day old" warning if numbers or stories haven't refreshed in 24 h.
4. The workflow commits `docs/data.json`. Pages redeploys automatically within a minute or two.

## Everyday maintenance

| Task | How |
|---|---|
| Refresh now | GitHub → **Actions** → *Refresh market brief* → **Run workflow** (any push that changes `scripts/`, `requirements.txt` or the workflow also triggers a run) |
| See why a run failed | Actions → click the red run → *Build docs/data.json* step log |
| Change the schedule (don't use `[skip ci]` in that commit, or GitHub may not pick up the new times; e.g. add 17:00: `SLOTS = (7, 17)` and cron hours `4,5,14,15`) | edit `SLOTS` / `LEAD_MINUTES` in `scripts/build.py` **and** the cron line in the workflow (cron is UTC; cover both UTC+1 and UTC+2) |
| Run locally | `pip install -r requirements.txt`, `export GEMINI_API_KEY=...`, `python scripts/build.py --force`, then `python -m http.server -d docs 8000` and open http://localhost:8000 |
| Refresh only some sectors | Actions → Run workflow → fill *sectors* (e.g. `XLK,XLE`). Code pushes automatically test only `XLK`, unless the commit message contains `[full]`. |
| Quick local test of 2 sectors | `python scripts/build.py --force --sectors XLK,XLE` |

GitHub disables scheduled workflows after 60 days with **no repository activity**. The daily data commits count as activity, so this shouldn't happen. If it does (for example after a long string of failures), open Actions and click **Enable workflow**.

You'll get an email from GitHub only if a run fails completely (no numbers and no stories). Partial failures show as notices on the site.

## Reliable 06:05 trigger (cron-job.org)

GitHub's own scheduler can start runs hours late or skip them, so a free external timer presses
"Run workflow" every morning. The GitHub schedule stays on as a backup. Whichever run comes first
builds the day's data; the other sees it's done and skips.

- **Token:** a fine-grained GitHub token limited to *only* this repo with *Actions: Read and write*
  (github.com/settings/personal-access-tokens). It is stored only in cron-job.org.
- **cron-job.org job:** daily at 06:05 Europe/Madrid, `POST https://api.github.com/repos/ninosabella20/market-brief/actions/workflows/refresh.yml/dispatches`
  - headers: `Authorization: Bearer <token>`, `Accept: application/vnd.github+json`, `X-GitHub-Api-Version: 2022-11-28`, `Content-Type: application/json`
  - body: `{"ref":"main","inputs":{"force":"false"}}`. `force:false` makes the run respect the 06:00–08:00 window and skip if the day is already built, so retries can't burn Gemini quota.
  - success = HTTP **204**. Turn on "notify on failure" in cron-job.org.
- **When the token expires** cron-job.org starts failing with 401 (you get an email). Create a new token the same way and paste it into the job's header.

## Geopolitics tab

`scripts/geo.py` builds `docs/geo.json`. It runs as a separate workflow step **after** the market build, with `continue-on-error`, so it can never block the stock data, and it only runs when the market build actually ran.

- **Players:** 10 countries/blocs in `PLAYERS` (China, Canada, Mexico, EU, Japan, Taiwan, UK, India, Saudi Arabia, Russia), plus a pinned US "Home base" card. Flags for reserves (South Korea `kr`, Germany `de`) are already in `docs/flags/`.
- **Ranking:** `WEIGHTS` = US goods trade YTD (US Census API; needs a free key from api.census.gov/data/key_signup.html saved as repo secret `CENSUS_API_KEY`) 35, holdings of US Treasuries (US Treasury TIC table) 20, GDP (World Bank; Taiwan uses a fixed approximate value, `TAIWAN_GDP_APPROX_B`) 15, and a hand-set `strategic` leverage weight (0–1 per country) 30. If a data source fails, its numbers show as missing and a notice explains it; diagnostics are saved under `debug` in `geo.json`.
- **Profiles (weekly):** rebuilt when older than 7 days (`PROFILE_MAX_AGE_DAYS`); 3 Gemini calls (4 countries per call). Each has: relation to the US, main lever, how the country sees itself, 4 strengths / 4 weaknesses from its own point of view, and 3 ways it could move the US economy over 5–10 years (upside / risk / either way, with affected sector ETFs).
- **"This week" lines (daily):** 1 Gemini call, written only from real Yahoo headlines of the last 7 days (with links).
- **News queries** must be short names ("China", "ECB", "OPEC"): Yahoo's news search matches names, not phrases. Avoid leaders' names, which go stale.
- **Force a rebuild:** put `[geo]` in a code commit message (all profiles) or `[geo:cn,jp]` (only those).

## If Gemini changes

**Model names change.** By default the script lists your available models (`GET /v1beta/models`) and picks the newest stable `gemini-<version>-flash` model (no `-preview`, `-exp` or `-lite`). If that model is out of free quota (429), retired (404) or doesn't answer within 150 s (`GEMINI_TIMEOUT`; a model that already answered gets one retry), it is skipped for the rest of the run. The script then falls back to the next stable flash, `gemini-flash-latest`, the preview flash models, and finally the flash-lite models.
To pin a model: repo **Settings → Secrets and variables → Actions → Variables → New variable**, name `GEMINI_MODEL`, value e.g. `gemini-2.5-flash`. Delete the variable to go back to auto-pick. The model used is shown when you hover over the "Updated…" stamp.

**Free-tier limits shrink (429 errors in the log).** Increase the pause between calls with a repo variable `GEMINI_MIN_INTERVAL` (seconds, default 12; e.g. `30`), set in the same place as `GEMINI_MODEL`. Alternatively, reduce `STORIES_PER_SECTOR` in `build.py`.

**Stories look thin or off-topic for a sector.** Edit that sector's themed queries in `SECTOR_QUERIES` in `build.py`. Gemini can only choose from what those searches return.

**Diagnosing Gemini problems without reading logs.** Each run writes a `gemini_debug` block into `docs/data.json`, showing the candidate models, which ones answered, and the exact reason any model was skipped (quota, retired, timed out).

**Key expired or revoked.** Create a new one at https://aistudio.google.com/apikey, then run:
```bash
gh secret set GEMINI_API_KEY --repo <you>/market-brief
```
(it prompts for the value), or set it at repo Settings → Secrets and variables → Actions.

**Free tier disappears entirely.** The sector numbers and colours keep working (they come only from Yahoo). Stories and explanations freeze at their last good version, and the site shows a notice. You'd need another LLM API with web search to replace `Gemini.generate()`.

## If Yahoo Finance changes

- The workflow installs the newest `yfinance` on every run, which usually fixes Yahoo breakages within days.
- Sector ETF prices fall back to Yahoo's chart API directly, then to Stooq (`stooq.com`), if yfinance fails.
- Non-US companies are resolved to their home listing (e.g. UniCredit → `UCG.MI`, Toyota → `7203.T`) using Yahoo search. Yahoo rarely has firm-level rating changes for non-US listings, so the script uses the company's US ADR/OTC line (e.g. `NVO` for Novo Nordisk) when that has them. Otherwise the card shows the consensus only.

## Files

```
docs/index.html, style.css, app.js   the site (no framework)
docs/data.json                       generated data (committed by the workflow)
scripts/build.py                     data builder
.github/workflows/refresh.yml        schedule
```

Not investment advice. LLM-written text can be wrong. Always check the linked source.
