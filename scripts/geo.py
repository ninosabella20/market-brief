#!/usr/bin/env python3
"""Geopolitics page builder -> docs/geo.json

Runs as a separate step AFTER the market build (scripts/build.py), so it can
never hold up or break the stock data. Two parts:

* Profiles (weekly): top-10 players ranked by impact on the US economy using
  free official data (US Census trade, US Treasury TIC holdings, IMF GDP) plus
  a fixed strategic weight. Gemini writes each profile from that data and the
  last week's headlines: how the country sees its own strengths/weaknesses,
  and 3 ways it could move the US economy over 5-10 years.
* "This week" lines (daily): one line per country from real Yahoo headlines.

Each part keeps the last good version if it fails.

Env: GEMINI_API_KEY, GEMINI_MODEL (optional), GEO_FORCE=1 (rebuild profiles now),
GEO_ONLY=cn,jp (rebuild only these profiles), COMMIT_MSG ("[geo]" / "[geo:cn]" do the same).
"""
from __future__ import annotations

import datetime as dt
import json
import os
import re
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402  (reuses Gemini client, news search, helpers)
from build import HTTP, MADRID, Gemini, iso, log, now_utc, num, parse_json  # noqa: E402

GEO_PATH = build.ROOT / "docs" / "geo.json"
PROFILE_MAX_AGE_DAYS = 7
BATCH = 4                      # countries per Gemini profile call
GEO_BUDGET = 12 * 60           # seconds for this whole step
SECTOR_CODES = [e for e, _ in build.SECTORS]

# Ranking weights (sum 100). strategic = judgment of non-trade leverage
# (oil, chips, minerals, security), 0..1, edit freely.
WEIGHTS = {"trade": 35, "treasuries": 20, "gdp": 15, "strategic": 30}

US = {"code": "us", "name": "United States",
      "queries": ["Federal Reserve", "Treasury", "tariffs", "White House"]}
PLAYERS = [
    {"code": "cn", "name": "China", "census": ["CHINA"], "tic": ["China, Mainland", "China"],
     "imf": "CHN", "strategic": 1.0,
     "queries": ["China", "Beijing", "yuan"]},
    {"code": "ca", "name": "Canada", "census": ["CANADA"], "tic": ["Canada"], "imf": "CAN",
     "strategic": 0.6, "queries": ["Canada", "Bank of Canada"]},
    {"code": "mx", "name": "Mexico", "census": ["MEXICO"], "tic": ["Mexico"], "imf": "MEX",
     "strategic": 0.6, "queries": ["Mexico", "Banxico"]},
    {"code": "eu", "name": "European Union", "census": ["EUROPEAN UNION"], "tic": "EU", "imf": "EU",
     "strategic": 0.8, "queries": ["Europe", "ECB", "EU"]},
    {"code": "jp", "name": "Japan", "census": ["JAPAN"], "tic": ["Japan"], "imf": "JPN",
     "strategic": 0.7, "queries": ["Japan", "yen", "Bank of Japan"]},
    {"code": "tw", "name": "Taiwan", "census": ["TAIWAN"], "tic": ["Taiwan"], "imf": "TWN",
     "strategic": 0.9, "queries": ["Taiwan", "TSMC"]},
    {"code": "gb", "name": "United Kingdom", "census": ["UNITED KINGDOM"], "tic": ["United Kingdom"],
     "imf": "GBR", "strategic": 0.5, "queries": ["Britain", "Bank of England"]},
    {"code": "in", "name": "India", "census": ["INDIA"], "tic": ["India"], "imf": "IND",
     "strategic": 0.5, "queries": ["India", "Reserve Bank of India"]},
    {"code": "sa", "name": "Saudi Arabia", "census": ["SAUDI ARABIA"], "tic": ["Saudi Arabia"],
     "imf": "SAU", "strategic": 0.7, "queries": ["Saudi", "OPEC"]},
    {"code": "ru", "name": "Russia", "census": ["RUSSIA"], "tic": ["Russia"], "imf": "RUS",
     "strategic": 0.6, "queries": ["Russia", "Ukraine"]},
]
EU_TIC_MEMBERS = ["Belgium", "Luxembourg", "France", "Ireland", "Germany", "Netherlands", "Spain",
                  "Italy", "Sweden", "Austria", "Denmark", "Finland", "Poland", "Portugal", "Greece"]
UA = {"User-Agent": build.UA}
# Note: Yahoo's news search matches names, not phrases ("China" works, "China economy" doesn't),
# so queries are short entity names. Avoid leaders' names: they go stale after elections.


# --------------------------------------------------------------------------
# Official data (all free, no keys). Each returns (values, debug)
# --------------------------------------------------------------------------
def _months_back(n: int) -> list[str]:
    t = now_utc().date().replace(day=1)
    out = []
    for _ in range(n):
        t = (t - dt.timedelta(days=1)).replace(day=1)
        out.append(t.strftime("%Y-%m"))
    return out


def census_trade() -> tuple[dict, dict]:
    """Year-to-date US goods exports to / imports from each partner (USD)."""
    dbg = {}
    base = "https://api.census.gov/data/timeseries/intltrade"
    for month in _months_back(6):
        try:
            ex = HTTP.get(f"{base}/exports/hs", params={"get": "CTY_CODE,CTY_NAME,ALL_VAL_YR",
                                                        "time": month}, timeout=60)
            im = HTTP.get(f"{base}/imports/hs", params={"get": "CTY_CODE,CTY_NAME,GEN_VAL_YR",
                                                        "time": month}, timeout=60)
            dbg[month] = f"exports {ex.status_code}, imports {im.status_code}"
            if ex.status_code != 200 or im.status_code != 200 or not ex.text.strip():
                continue
            rows_ex, rows_im = ex.json()[1:], im.json()[1:]
        except Exception as e:  # noqa: BLE001
            dbg[month] = f"error {e}"[:200]
            continue

        def tot(rows):
            d: dict[str, float] = {}
            for r in rows:
                try:
                    d[r[1].strip().upper()] = max(d.get(r[1].strip().upper(), 0.0), float(r[2]))
                except (ValueError, IndexError, AttributeError):
                    pass
            return d
        ex_t, im_t = tot(rows_ex), tot(rows_im)
        dbg["rows"] = f"{len(rows_ex)} export rows / {len(ex_t)} names"
        out = {}
        for p in PLAYERS:
            e = next((ex_t[n] for n in p["census"] if n in ex_t), None)
            i = next((im_t[n] for n in p["census"] if n in im_t), None)
            if e is not None or i is not None:
                out[p["code"]] = {"exports": e, "imports": i}
        if len(out) >= 6:
            label = dt.date.fromisoformat(month + "-01").strftime("%b %Y")
            return {"as_of": f"Jan-{label}", "values": out}, dbg
    return {}, dbg


def tic_holdings() -> tuple[dict, dict]:
    """Foreign holdings of US Treasury securities, latest month, USD billions."""
    dbg = {}
    urls = ["https://ticdata.treasury.gov/resource-center/data-chart-center/tic/Documents/slt_table5.txt",
            "https://ticdata.treasury.gov/Publish/mfh.txt"]
    for url in urls:
        try:
            r = HTTP.get(url, timeout=60, headers=UA)
            dbg[url.rsplit("/", 1)[-1]] = r.status_code
            if r.status_code != 200:
                continue
        except Exception as e:  # noqa: BLE001
            dbg[url.rsplit("/", 1)[-1]] = f"error {e}"[:200]
            continue
        vals: dict[str, float] = {}
        for line in r.text.splitlines():
            m = re.match(r"^\s*([A-Za-z][A-Za-z ,.'()/-]*?)\s{2,}(-?[\d,]+\.?\d*)\b", line.replace("\t", "  "))
            if m:
                name = m.group(1).strip()
                try:
                    vals.setdefault(name, float(m.group(2).replace(",", "")))
                except ValueError:
                    pass
        month = re.search(r"\b(20\d\d)-(\d\d)\b", r.text) or re.search(
            r"\b(Jan|Feb|Mar|Apr|May|Jun|Jul|Aug|Sep|Oct|Nov|Dec)[a-z]* +(20\d\d)\b", r.text)
        dbg["parsed_names"] = len(vals)
        dbg["sample"] = list(vals.items())[:4]
        out = {}
        for p in PLAYERS:
            if p["tic"] == "EU":
                s = sum(vals[n] for n in EU_TIC_MEMBERS if n in vals)
                out[p["code"]] = s or None
            else:
                out[p["code"]] = next((vals[n] for n in p["tic"] if n in vals), None)
        if sum(1 for v in out.values() if v) >= 5:
            return {"as_of": month.group(0) if month else "", "values": out}, dbg
    return {}, dbg


def imf_gdp() -> tuple[dict, dict]:
    """Nominal GDP, USD billions, from the IMF DataMapper (latest year <= this year)."""
    dbg = {}
    codes = "/".join(p["imf"] for p in PLAYERS) + "/USA"
    try:
        r = HTTP.get(f"https://www.imf.org/external/datamapper/api/v1/NGDPD/{codes}", timeout=60,
                     headers=UA)
        dbg["status"] = r.status_code
        data = r.json()["values"]["NGDPD"]
    except Exception as e:  # noqa: BLE001
        dbg["error"] = str(e)[:200]
        return {}, dbg
    year = str(now_utc().year)
    out, used = {}, set()
    for p in PLAYERS + [{"code": "us", "imf": "USA"}]:
        series = data.get(p["imf"]) or {}
        yrs = sorted(y for y in series if y <= year)
        if yrs:
            out[p["code"]] = series[yrs[-1]]
            used.add(yrs[-1])
    dbg["found"] = len(out)
    return ({"as_of": "IMF " + "/".join(sorted(used)), "values": out} if out else {}), dbg


def rank_players(trade: dict, tic: dict, gdp: dict) -> list[dict]:
    tv = trade.get("values", {})
    tr = {c: ((v.get("exports") or 0) + (v.get("imports") or 0)) or None for c, v in tv.items()}
    hv, gv = tic.get("values", {}), gdp.get("values", {})

    def norm(d: dict, code: str) -> float:
        mx = max((v for v in d.values() if v), default=0)
        return (d.get(code) or 0) / mx if mx else 0.0

    ranked = []
    for p in PLAYERS:
        c = p["code"]
        score = (WEIGHTS["trade"] * norm(tr, c) + WEIGHTS["treasuries"] * norm(hv, c)
                 + WEIGHTS["gdp"] * norm(gv, c) + WEIGHTS["strategic"] * p["strategic"])
        t = tv.get(c, {})
        ranked.append({"code": c, "name": p["name"], "score": round(score, 1), "numbers": {
            "us_exports_b": num((t.get("exports") or 0) / 1e9, 1) if t.get("exports") else None,
            "us_imports_b": num((t.get("imports") or 0) / 1e9, 1) if t.get("imports") else None,
            "trade_b": num(tr[c] / 1e9, 1) if tr.get(c) else None,
            "balance_b": (num(((t.get("exports") or 0) - (t.get("imports") or 0)) / 1e9, 1)
                          if t.get("exports") and t.get("imports") else None),
            "treasuries_b": num(hv.get(c), 1),
            "gdp_b": num(gv.get(c), 0),
        }})
    ranked.sort(key=lambda r: -r["score"])
    for i, r in enumerate(ranked, 1):
        r["rank"] = i
    return ranked


# --------------------------------------------------------------------------
# Gemini writing
# --------------------------------------------------------------------------
PROFILE_PROMPT = """Today is {today}. You write country profiles for the Geopolitics page of a
US-focused investor's market brief. The reader is a long-term US investor who wants a holistic view
of how the world affects the US economy and stock market. Be balanced, factual and non-partisan.

For each country below you get REAL data (US goods trade year-to-date, holdings of US Treasuries,
GDP) and headlines from the last 7 days. Use the headlines only as context for what is current;
use your general knowledge for structural facts, but do NOT invent statistics.

{blocks}

For EACH country code return:
- "relation": relationship with the US today, one of "Ally", "Partner", "Competitor", "Rival"
  (use null for "us")
- "lever": its main lever over the US economy, max 6 words (null for "us")
- "self_view": ONE sentence on how this country sees its own position in the world today,
  written from ITS OWN national perspective (not the US view)
- "strengths": exactly 4 short bullets (max 14 words each): strengths as the country itself
  would describe them
- "weaknesses": exactly 4 short bullets (max 14 words each): the vulnerabilities the country
  itself worries about
- "impacts": exactly 3 items (null for "us"), from the US perspective: the most important ways
  this country could influence the US economy and markets over the next 5-10 years, for good or bad:
  {{"title": "max 8 words", "detail": "max 35 words, concrete mechanism",
    "direction": "positive" | "negative" | "mixed",
    "sectors": [US sector ETF codes most affected, from {sectors}]}}

Return ONLY a JSON object (no markdown) keyed by country code, e.g. {{"cn": {{...}}, "jp": {{...}}}}."""

WEEK_PROMPT = """Today is {today}. For each country below you get numbered headlines from the last
7 days. Write ONE line (max 30 words) on the most important development this week for the US
economy or US markets. If nothing relevant happened, write "Quiet week for US-relevant news."
Use only the headlines given.

{blocks}

Return ONLY a JSON object (no markdown) keyed by country code:
{{"cn": {{"line": "...", "ids": [up to 2 headline numbers used]}}, ...}}"""


def headlines(queries: list[str], n=14) -> list[dict]:
    build.NEWS_MAX_AGE_DAYS = 7
    items = build.collect_news(queries, max_items=n, describe=False)
    return sorted(items, key=lambda i: -i["ts"])


def fmt_numbers(nums: dict) -> str:
    def b(v, unit="B"):
        return "n/a" if v is None else (f"${v / 1000:.1f}T" if unit == "B" and abs(v) >= 1000 else f"${v:.0f}B")
    return (f"US goods trade YTD {b(nums.get('trade_b'))} (US exports {b(nums.get('us_exports_b'))}, "
            f"US imports {b(nums.get('us_imports_b'))}, balance {b(nums.get('balance_b'))}); "
            f"holds US Treasuries {b(nums.get('treasuries_b'))}; GDP {b(nums.get('gdp_b'))}")


def write_profiles(gem: Gemini, entries: list[dict], news: dict, today: str) -> dict:
    out = {}
    for i in range(0, len(entries), BATCH):
        chunk = entries[i:i + BATCH]
        blocks = []
        for e in chunk:
            hl = "\n".join(f"  - {h['title']} ({h['publisher']})" for h in news.get(e["code"], [])[:10])
            nums = fmt_numbers(e.get("numbers") or {}) if e["code"] != "us" else "home country"
            blocks.append(f"## {e['code']} - {e['name']}\nData: {nums}\nHeadlines:\n{hl or '  (none)'}")
        prompt = PROFILE_PROMPT.format(today=today, blocks="\n\n".join(blocks),
                                       sectors=", ".join(SECTOR_CODES))
        for attempt in range(2):
            try:
                text, _ = gem.generate(prompt)
                j = parse_json(text)
                if not isinstance(j, dict):
                    raise ValueError("not an object")
                for e in chunk:
                    p = j.get(e["code"]) or j.get(e["code"].upper())
                    if isinstance(p, dict) and p.get("strengths") and p.get("weaknesses"):
                        out[e["code"]] = clean_profile(p, e["code"] == "us")
                break
            except (ValueError, json.JSONDecodeError) as ex:
                log.warning("profile parse failed (%s)", ex)
                prompt += "\n\nIMPORTANT: output ONLY the JSON object."
    return out


def clean_profile(p: dict, is_us: bool) -> dict:
    s = lambda v, n=220: str(v or "").strip()[:n]  # noqa: E731
    prof = {
        "self_view": s(p.get("self_view"), 300),
        "strengths": [s(x, 140) for x in (p.get("strengths") or [])][:4],
        "weaknesses": [s(x, 140) for x in (p.get("weaknesses") or [])][:4],
    }
    if not is_us:
        rel = s(p.get("relation"), 20).title()
        prof["relation"] = rel if rel in ("Ally", "Partner", "Competitor", "Rival") else "Partner"
        prof["lever"] = s(p.get("lever"), 60)
        impacts = []
        for im in (p.get("impacts") or [])[:3]:
            if not isinstance(im, dict):
                continue
            d = str(im.get("direction") or "mixed").lower()
            impacts.append({"title": s(im.get("title"), 80), "detail": s(im.get("detail"), 300),
                            "direction": d if d in ("positive", "negative", "mixed") else "mixed",
                            "sectors": [x for x in (im.get("sectors") or []) if x in SECTOR_CODES][:4]})
        prof["impacts"] = impacts
    return prof


def write_week(gem: Gemini, news: dict, names: dict, today: str) -> dict:
    blocks = []
    for code, items in news.items():
        lines = "\n".join(f"  [{i}] {h['title']} ({h['publisher']})" for i, h in enumerate(items[:10], 1))
        blocks.append(f"## {code} - {names[code]}\n{lines or '  (no headlines)'}")
    prompt = WEEK_PROMPT.format(today=today, blocks="\n\n".join(blocks))
    text, _ = gem.generate(prompt)
    j = parse_json(text)
    out = {}
    for code, items in news.items():
        w = j.get(code) if isinstance(j, dict) else None
        if not isinstance(w, dict) or not w.get("line"):
            continue
        used = []
        for i in (w.get("ids") or [])[:2]:
            try:
                h = items[int(i) - 1]
                used.append({"title": h["title"], "url": h["url"], "publisher": h["publisher"]})
            except (ValueError, IndexError, TypeError):
                pass
        out[code] = {"line": str(w["line"]).strip()[:260], "links": used}
    return out


# --------------------------------------------------------------------------
def main() -> int:
    t0 = time.time()
    try:
        prev = json.loads(GEO_PATH.read_text())
    except Exception:  # noqa: BLE001
        prev = {}
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key:
        log.warning("no GEMINI_API_KEY; geopolitics not refreshed")
        return 0
    msg = os.environ.get("COMMIT_MSG", "")
    m = re.search(r"\[geo(?::([a-z,]+))?\]", msg)
    only = {c for c in (os.environ.get("GEO_ONLY") or (m.group(1) if m and m.group(1) else "")).split(",") if c}
    force = bool(os.environ.get("GEO_FORCE") or m or only)

    out = {**prev, "notices": []}
    names = {p["code"]: p["name"] for p in PLAYERS} | {"us": US["name"]}
    today = now_utc().astimezone(MADRID).strftime("%A %d %B %Y")
    gem = Gemini(key, os.environ.get("GEMINI_MODEL") or None,
                 float(os.environ.get("GEMINI_MIN_INTERVAL", "12")))
    gem.deadline = t0 + GEO_BUDGET

    # News for every player (+US): used by both parts.
    news = {}
    for p in [US] + PLAYERS:
        news[p["code"]] = headlines(p["queries"])
    log.info("geo headlines: %s", {k: len(v) for k, v in news.items()})

    # 1) Weekly profiles (+ ranking data)
    last = prev.get("profiles_updated_at")
    age_days = ((now_utc() - dt.datetime.fromisoformat(last.replace("Z", "+00:00"))).total_seconds()
                / 86400) if last else 1e9
    if force or age_days >= PROFILE_MAX_AGE_DAYS:
        log.info("rebuilding geopolitics profiles (force=%s only=%s age=%.1fd)", force, only, age_days)
        trade, dbg_t = census_trade()
        tic, dbg_h = tic_holdings()
        gdp, dbg_g = imf_gdp()
        out["debug"] = {"census": dbg_t, "tic": dbg_h, "imf": dbg_g}
        ranked = rank_players(trade, tic, gdp)
        prev_players = {p["code"]: p for p in prev.get("players", [])}
        entries = ([{"code": "us", "name": US["name"]}] if (not only or "us" in only) else []) + [
            r for r in ranked if not only or r["code"] in only]
        try:
            profiles = write_profiles(gem, entries, news, today)
        except Exception as e:  # noqa: BLE001
            log.exception("profiles failed")
            profiles = {}
            out["notices"].append(f"Geopolitics profiles not refreshed ({str(e)[:100]}).")
        players = []
        for r in ranked:
            old = prev_players.get(r["code"], {})
            prof = profiles.get(r["code"]) or old.get("profile")
            players.append({**r, "profile": prof,
                            "profile_as_of": (iso(now_utc()) if r["code"] in profiles
                                              else old.get("profile_as_of")),
                            "week": old.get("week")})
        out["players"] = players
        if "us" in profiles:
            out["us"] = {"code": "us", "name": US["name"], "profile": profiles["us"],
                         "gdp_b": num(gdp.get("values", {}).get("us"), 0), "week": (prev.get("us") or {}).get("week")}
        out["data_as_of"] = {"trade": trade.get("as_of"), "treasuries": tic.get("as_of"), "gdp": gdp.get("as_of")}
        out["weights"] = WEIGHTS
        missing = [k for k, v in (("US Census trade", trade), ("US Treasury holdings", tic), ("IMF GDP", gdp)) if not v]
        if missing:
            out["notices"].append("Ranking data unavailable this week: " + ", ".join(missing) + ".")
        if profiles and not only:
            out["profiles_updated_at"] = iso(now_utc())
        elif profiles and not prev.get("profiles_updated_at"):
            out["profiles_updated_at"] = iso(now_utc())
        failed = [names[e["code"]] for e in entries if e["code"] not in profiles]
        if failed:
            out["notices"].append("Profiles not refreshed for: " + ", ".join(failed) + ".")

    # 2) Daily "this week" lines
    if out.get("players"):
        try:
            week = write_week(gem, news, names, today)
            for p in out["players"]:
                if p["code"] in week:
                    p["week"] = week[p["code"]]
            if out.get("us") and "us" in week:
                out["us"]["week"] = week["us"]
            out["week_updated_at"] = iso(now_utc())
        except Exception as e:  # noqa: BLE001
            log.exception("week lines failed")
            out["notices"].append(f"'This week' lines not refreshed ({str(e)[:100]}).")

    out["generated_at"] = iso(now_utc())
    out["gemini"] = {"ok_calls": gem.ok, "errors": gem.errors}
    GEO_PATH.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    log.info("wrote %s in %.0fs; notices: %s", GEO_PATH, time.time() - t0, out["notices"] or "none")
    return 0


if __name__ == "__main__":
    sys.exit(main())
