# Market Brief

A free, self-updating market dashboard: sector performance for the 11 GICS sectors + S&P 500, and the top 5 verified stories per sector, refreshed at **07:00 and 17:00 Europe/Madrid**.

- **Site:** static page in `docs/` (served by GitHub Pages), reads `docs/data.json`.
- **Data:** `scripts/build.py`, run by GitHub Actions (`.github/workflows/refresh.yml`).
  - Prices, returns, company facts, consensus and analyst ratings: **Yahoo Finance via yfinance** (no key).
  - Stories, company one-liners, market recap and "why it moved": **Google Gemini API free tier** with Google Search grounding.
- **Cost:** €0. Public repo + GitHub Pages + Actions minutes (free and unlimited for public repos) + Gemini free tier. Each run makes about 12 Gemini calls (one per sector plus one for explanations), roughly 24 a day.

## How it works

1. Cron fires at 05:05, 06:05, 15:05 and 16:05 UTC. `build.py` only proceeds when it is 07:xx/08:xx or 17:xx/18:xx in Madrid **and** that slot hasn't been built yet. This handles summer/winter time and GitHub's occasionally late or dropped cron runs.
2. Each part (numbers, explanations, each sector's stories) refreshes independently. If one fails, the previous good copy is kept and the site shows a yellow "Last refresh was partial" notice.
3. The site shows a "Data is over a day old" warning if numbers or stories haven't refreshed in 24 h.
4. The workflow commits `docs/data.json`. Pages redeploys automatically within a minute or two.

## Everyday maintenance

| Task | How |
|---|---|
| Refresh now | GitHub → **Actions** → *Refresh market brief* → **Run workflow** (any push that changes `scripts/`, `requirements.txt` or the workflow also triggers a run) |
| See why a run failed | Actions → click the red run → *Build docs/data.json* step log |
| Change the schedule | edit `SLOTS` in `scripts/build.py` **and** the cron line in the workflow (cron is UTC) |
| Run locally | `pip install -r requirements.txt`, `export GEMINI_API_KEY=...`, `python scripts/build.py --force`, then `python -m http.server -d docs 8000` and open http://localhost:8000 |
| Quick test of 2 sectors | `python scripts/build.py --force --sectors XLK,XLE` |

GitHub disables scheduled workflows after 60 days with **no repository activity**. The twice-daily data commits count as activity, so this shouldn't happen. If it does (for example after a long string of failures), open Actions and click **Enable workflow**.

You'll get an email from GitHub only if a run fails completely (no numbers and no stories). Partial failures show as notices on the site.

## If Gemini changes

**Model names change.** By default the script lists your available models (`GET /v1beta/models`) and picks the newest stable `gemini-<version>-flash` model (no `-preview`, `-exp` or `-lite`). If that model isn't on the free tier (429 with `limit: 0`) or is retired (404), it falls back automatically to the next one, then to `gemini-flash-latest`, then to `gemini-2.5-flash`.
To pin a model: repo **Settings → Secrets and variables → Actions → Variables → New variable**, name `GEMINI_MODEL`, value e.g. `gemini-2.5-flash`. Delete the variable to go back to auto-pick. The model used is shown when you hover over the "Updated…" stamp.

**Free-tier limits shrink (429 errors in the log).** Increase the pause between calls with a repo variable `GEMINI_MIN_INTERVAL` (seconds, default 12; e.g. `30`), set in the same place as `GEMINI_MODEL`. Alternatively, reduce `STORIES_PER_SECTOR` in `build.py`. If the daily quota is the problem, drop one of the two refresh slots (`SLOTS = (7,)`) so stories refresh once a day.

**Google Search grounding stops being free, or the tool name changes.** The request uses `"tools": [{"google_search": {}}]` in `Gemini.generate()`. Check the current name at https://ai.google.dev/gemini-api/docs/google-search. Without grounding the stories would be unreliable, so the right fix is the new tool name or a model that still supports it.

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
