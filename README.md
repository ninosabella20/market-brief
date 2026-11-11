# Market Brief

A free, self-updating market dashboard: sector performance for the 11 GICS sectors + S&P 500, and the top 5 verified stories per sector, refreshed once a day, **ready by 07:00 Europe/Madrid**.

- **Site:** static page in `docs/` (served by GitHub Pages), reads `docs/data.json`.
- **Data:** `scripts/build.py`, run by GitHub Actions (`.github/workflows/refresh.yml`).
  - Prices, returns, company facts, consensus and analyst ratings: **Yahoo Finance via yfinance** (no key).
  - News candidates: **Yahoo Finance news search** (no key): for each sector the ETF's top 10 holdings plus a few themed queries (e.g. "European banks"), last 4 days, with each article's description and picture.
  - Story selection and writing, company one-liners, market recap and "why it moved": **Google Gemini API free tier**, *without* Google Search grounding (grounding isn't in the free tier for new keys). Gemini only picks from the real articles it is given, so every link is real.
- **Cost:** €0. Public repo + GitHub Pages + Actions minutes (free and unlimited for public repos) + Gemini free tier. Each run makes 12 Gemini calls (one per sector plus one for explanations), once a day.

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
| Change the schedule (e.g. add 17:00: `SLOTS = (7, 17)` and cron hours `4,5,14,15`) | edit `SLOTS` / `LEAD_MINUTES` in `scripts/build.py` **and** the cron line in the workflow (cron is UTC; cover both UTC+1 and UTC+2) |
| Run locally | `pip install -r requirements.txt`, `export GEMINI_API_KEY=...`, `python scripts/build.py --force`, then `python -m http.server -d docs 8000` and open http://localhost:8000 |
| Refresh only some sectors | Actions → Run workflow → fill *sectors* (e.g. `XLK,XLE`). Code pushes automatically test only `XLK`, unless the commit message contains `[full]`. |
| Quick local test of 2 sectors | `python scripts/build.py --force --sectors XLK,XLE` |

GitHub disables scheduled workflows after 60 days with **no repository activity**. The daily data commits count as activity, so this shouldn't happen. If it does (for example after a long string of failures), open Actions and click **Enable workflow**.

You'll get an email from GitHub only if a run fails completely (no numbers and no stories). Partial failures show as notices on the site.

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
