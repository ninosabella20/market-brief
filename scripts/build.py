#!/usr/bin/env python3
"""Market Brief builder.

Fetches sector ETF performance + company data from Yahoo Finance (yfinance),
asks Google Gemini (free tier, Google Search grounding) for the top stories per
GICS sector and for short "why it moved" explanations, and writes
docs/data.json for the static site in docs/.

Every part is independent: if one part fails, the last good copy of that part
is kept from the previous docs/data.json and a notice is shown on the site.

Usage:
  python scripts/build.py            # only runs in the 06:00-08:00 Europe/Madrid window
  python scripts/build.py --force    # run now regardless of time
  python scripts/build.py --force --sectors XLK,XLE   # quick partial test
  python scripts/build.py --force --skip-stories      # numbers + reasons only
"""
from __future__ import annotations

import argparse
import datetime as dt
import html
import json
import logging
import os
import re
import sys
import time
from pathlib import Path
from urllib.parse import urljoin, urlparse
from zoneinfo import ZoneInfo

import pandas as pd
import requests
import yfinance as yf

ROOT = Path(__file__).resolve().parent.parent
DATA_PATH = ROOT / "docs" / "data.json"
MADRID = ZoneInfo("Europe/Madrid")
NEW_YORK = ZoneInfo("America/New_York")
SLOTS = (7,)                 # Madrid hour(s) by which fresh data should be READY
LEAD_MINUTES = 60            # a slot's window opens this long before it (06:00)
WINDOW_MINUTES = 120         # ...and stays open this long (until 08:00) to catch late cron runs
STORY_MAX_AGE_DAYS = 10      # drop stories older than this
STORIES_PER_SECTOR = 5
START = time.time()
# Hard stop well before the workflow's 45-minute timeout so data.json is always written.
DEADLINE = START + float(os.environ.get("BUILD_BUDGET_SECONDS", str(36 * 60)))


def out_of_time(margin: float = 0) -> bool:
    return time.time() + margin > DEADLINE
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/130.0 Safari/537.36")

SECTORS = [  # (ETF, GICS sector)
    ("XLK", "Information Technology"),
    ("XLC", "Communication Services"),
    ("XLY", "Consumer Discretionary"),
    ("XLP", "Consumer Staples"),
    ("XLF", "Financials"),
    ("XLV", "Health Care"),
    ("XLI", "Industrials"),
    ("XLB", "Materials"),
    ("XLE", "Energy"),
    ("XLU", "Utilities"),
    ("XLRE", "Real Estate"),
]
INDEX = ("SPY", "S&P 500")

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s",
                    datefmt="%H:%M:%S")
log = logging.getLogger("build")
logging.getLogger("yfinance").setLevel(logging.CRITICAL)

HTTP = requests.Session()
HTTP.headers.update({"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
HTTP.mount("https://", requests.adapters.HTTPAdapter(pool_connections=20, pool_maxsize=20))


def now_utc() -> dt.datetime:
    return dt.datetime.now(dt.timezone.utc)


def iso(t: dt.datetime) -> str:
    return t.astimezone(dt.timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def num(x, nd=2):
    try:
        if x is None or pd.isna(x):
            return None
        return round(float(x), nd)
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------
# Schedule gate
# --------------------------------------------------------------------------
def current_slot(prev: dict) -> tuple[bool, str]:
    """Return (should_run, reason). A slot (e.g. 07:00) is due between 06:00 and
    08:00 Madrid time, once: skip if a build already happened inside the window."""
    m = now_utc().astimezone(MADRID)
    for slot in SLOTS:
        start = m.replace(hour=slot, minute=0, second=0, microsecond=0) - dt.timedelta(minutes=LEAD_MINUTES)
        if start <= m < start + dt.timedelta(minutes=WINDOW_MINUTES):
            last = prev.get("generated_at")
            if last:
                last_t = dt.datetime.fromisoformat(last.replace("Z", "+00:00"))
                if last_t >= start:
                    return False, f"slot {slot:02d}:00 already built at {last}"
            return True, f"slot {slot:02d}:00 due (Madrid time {m:%H:%M})"
    return False, f"Madrid time {m:%H:%M} is not a refresh slot"


# --------------------------------------------------------------------------
# Prices (yfinance -> Yahoo chart API -> Stooq)
# --------------------------------------------------------------------------
def _closes_from_download(df: pd.DataFrame, tickers: list[str]) -> dict[str, pd.Series]:
    out = {}
    if df is None or df.empty:
        return out
    for t in tickers:
        try:
            if isinstance(df.columns, pd.MultiIndex):
                if t not in df.columns.get_level_values(0):
                    continue
                s = df[t]["Close"]
            else:
                s = df["Close"]
            s = s.dropna()
            if len(s) >= 2:
                s.index = pd.to_datetime(s.index).tz_localize(None)
                out[t] = s.astype(float)
        except Exception:  # noqa: BLE001
            continue
    return out


def _yahoo_chart(ticker: str) -> pd.Series | None:
    end = int(time.time()) + 86400
    start = end - int(6.2 * 365 * 86400)
    for host in ("query2.finance.yahoo.com", "query1.finance.yahoo.com"):
        try:
            r = HTTP.get(f"https://{host}/v8/finance/chart/{ticker}",
                         params={"period1": start, "period2": end, "interval": "1d",
                                 "events": "split"}, timeout=20)
            if r.status_code != 200:
                continue
            res = r.json()["chart"]["result"][0]
            ts = pd.to_datetime(res["timestamp"], unit="s")
            close = res["indicators"]["quote"][0]["close"]
            s = pd.Series(close, index=ts).dropna()
            s.index = s.index.normalize()
            if len(s) >= 2:
                return s.astype(float)
        except Exception as e:  # noqa: BLE001
            log.debug("chart api %s %s: %s", host, ticker, e)
    return None


def _stooq(ticker: str) -> pd.Series | None:
    if "." in ticker or "^" in ticker:
        return None  # only used for the US sector ETFs
    try:
        r = HTTP.get("https://stooq.com/q/d/l/", params={"s": f"{ticker.lower()}.us", "i": "d"},
                     timeout=20)
        if r.status_code != 200 or not r.text.startswith("Date"):
            return None
        from io import StringIO
        df = pd.read_csv(StringIO(r.text), parse_dates=["Date"]).set_index("Date")
        s = df["Close"].dropna()
        return s.astype(float) if len(s) >= 2 else None
    except Exception as e:  # noqa: BLE001
        log.debug("stooq %s: %s", ticker, e)
        return None


def fetch_closes(tickers: list[str], period: str = "6y") -> dict[str, pd.Series]:
    """Daily (split-adjusted, dividend-excluded) closes for ~6 years."""
    tickers = list(dict.fromkeys(tickers))
    out: dict[str, pd.Series] = {}
    for attempt in range(3):
        missing = [t for t in tickers if t not in out]
        if not missing:
            break
        try:
            df = yf.download(missing, period=period, interval="1d", auto_adjust=False,
                             group_by="ticker", threads=False, progress=False)
            out.update(_closes_from_download(df, missing))
        except Exception as e:  # noqa: BLE001
            log.warning("yf.download attempt %d failed: %s", attempt + 1, e)
        if any(t not in out for t in tickers):
            time.sleep(5 * (3 ** attempt))
    for t in [t for t in tickers if t not in out]:
        s = _yahoo_chart(t)
        src = "yahoo-chart"
        if s is None:
            s, src = _stooq(t), "stooq"
        if s is not None:
            log.info("price fallback used for %s (%s)", t, src)
            out[t] = s
        else:
            log.warning("no price history for %s", t)
    return out


def returns(s: pd.Series) -> dict:
    s = s.sort_index()
    last_d, last = s.index[-1], s.iloc[-1]

    def back(offset) -> float | None:
        prior = s[s.index <= last_d - offset]
        if prior.empty:
            return None
        return (last / prior.iloc[-1] - 1) * 100

    prev_year = s[s.index < pd.Timestamp(year=last_d.year, month=1, day=1)]
    return {
        "last": num(last),
        "day": num((last / s.iloc[-2] - 1) * 100) if len(s) > 1 else None,
        "week": num(back(pd.DateOffset(days=7))),
        "month": num(back(pd.DateOffset(months=1))),
        "ytd": num((last / prev_year.iloc[-1] - 1) * 100) if not prev_year.empty else None,
        "y1": num(back(pd.DateOffset(years=1))),
        "y5": num(back(pd.DateOffset(years=5))),
        "as_of": last_d.strftime("%Y-%m-%d"),
    }


def build_market() -> dict:
    tickers = [INDEX[0]] + [t for t, _ in SECTORS]
    closes = fetch_closes(tickers)
    missing = [t for t in tickers if t not in closes]
    if len(missing) > 3:
        raise RuntimeError(f"price data missing for {', '.join(missing)}")
    rows = []
    for t, name in [INDEX] + SECTORS:
        r = {"ticker": t, "name": name, "is_index": t == INDEX[0]}
        if t in closes:
            r.update(returns(closes[t]))
        rows.append(r)
    ny = now_utc().astimezone(NEW_YORK)
    as_of = max((r.get("as_of") or "") for r in rows)
    intraday = (as_of == ny.strftime("%Y-%m-%d") and ny.weekday() < 5
                and (9, 30) <= (ny.hour, ny.minute) < (16, 0))
    return {"rows": rows, "as_of": as_of, "intraday": intraday,
            "as_of_label": (f"Intraday, {ny:%H:%M} ET" if intraday
                            else f"Close {dt.date.fromisoformat(as_of):%a %d %b %Y}"),
            "missing": missing}


# --------------------------------------------------------------------------
# Gemini
# --------------------------------------------------------------------------
class GeminiError(RuntimeError):
    pass


class Gemini:
    BASE = "https://generativelanguage.googleapis.com/v1beta"

    def __init__(self, key: str, override: str | None, min_interval: float):
        self.key = key
        self.available: list[str] = []
        self.errors: dict[str, str] = {}      # model -> why it was abandoned
        self.last_429: dict[str, str] = {}
        self.ok: dict[str, int] = {}          # model -> successful calls
        self.exhausted = False
        self.timeout = float(os.environ.get("GEMINI_TIMEOUT", "150"))
        self.timeouts: dict[str, int] = {}
        self.deadline = time.time() + float(os.environ.get("GEMINI_BUDGET_SECONDS", "1500"))
        self.min_interval = min_interval
        self._last_call = 0.0
        self.candidates = [override] if override else self._discover()
        self.model = self.candidates[0]
        log.info("Gemini model candidates: %s", ", ".join(self.candidates))

    def _headers(self):
        return {"x-goog-api-key": self.key, "Content-Type": "application/json"}

    def _discover(self) -> list[str]:
        names, token = [], None
        try:
            while True:
                r = HTTP.get(f"{self.BASE}/models", headers=self._headers(),
                             params={"pageSize": 1000, **({"pageToken": token} if token else {})},
                             timeout=30)
                r.raise_for_status()
                j = r.json()
                names += [m["name"].split("/", 1)[1] for m in j.get("models", [])
                          if "generateContent" in m.get("supportedGenerationMethods", [])]
                token = j.get("nextPageToken")
                if not token:
                    break
        except Exception as e:  # noqa: BLE001
            log.warning("model list failed (%s); using defaults", e)
        self.available = names
        bad = re.compile(r"image|tts|audio|live|embedding|thinking|vision|robotics|computer", re.I)

        def ver(n):
            m = re.match(r"gemini-(\d+(?:\.\d+)?)", n)
            return float(m.group(1)) if m else 0.0

        stable = [n for n in names if re.fullmatch(r"gemini-\d+(?:\.\d+)?-flash", n)]
        preview = [n for n in names if re.fullmatch(r"gemini-\d+(?:\.\d+)?-flash-preview[\w.-]*", n)
                   and not bad.search(n)]
        lite = [n for n in names if re.fullmatch(r"gemini-\d+(?:\.\d+)?-flash-lite", n)]
        ordered = sorted(stable, key=ver, reverse=True)
        ordered += [n for n in ("gemini-flash-latest",) if not names or n in names]
        ordered += sorted(preview, key=ver, reverse=True)
        ordered += sorted(lite, key=ver, reverse=True)
        ordered += [n for n in ("gemini-flash-lite-latest",) if n in names]
        if not ordered:
            ordered = ["gemini-flash-latest", "gemini-2.5-flash"]
        return list(dict.fromkeys(ordered))

    def _next_model(self, why: str) -> bool:
        """Mark the current model unusable for the rest of this run and move on."""
        self.errors[self.model] = why[:300]
        log.warning("model %s unusable: %s", self.model, why[:300])
        rest = [m for m in self.candidates if m not in self.errors]
        if rest:
            self.model = rest[0]
            log.info("switching to %s", self.model)
            return True
        self.exhausted = True
        return False

    def generate(self, prompt: str, grounded: bool = False) -> tuple[str, list[dict]]:
        if self.exhausted:
            raise GeminiError("no usable Gemini model left this run: " +
                              "; ".join(f"{k}: {v[:90]}" for k, v in self.errors.items()))
        if time.time() > self.deadline:
            raise GeminiError("Gemini time budget for this run used up")
        body = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                "generationConfig": {"temperature": 0.2}}
        if grounded:
            body["tools"] = [{"google_search": {}}]
        attempts = 0
        while True:
            if time.time() > self.deadline:
                raise GeminiError("Gemini time budget for this run used up")
            wait = self.min_interval - (time.time() - self._last_call)
            if wait > 0:
                time.sleep(wait)
            self._last_call = time.time()
            attempts += 1
            try:
                r = HTTP.post(f"{self.BASE}/models/{self.model}:generateContent",
                              headers=self._headers(), json=body, timeout=self.timeout)
            except requests.Timeout:
                # A model that already answered this run gets one more try; one that
                # never answered (or times out twice) is dropped for the run.
                self.timeouts[self.model] = self.timeouts.get(self.model, 0) + 1
                if self.ok.get(self.model) and self.timeouts[self.model] < 2:
                    log.info("%s slow (timeout), retrying once", self.model)
                    continue
                if self._next_model(f"no answer within {self.timeout:.0f}s"):
                    attempts = 0
                    continue
                raise GeminiError("every model timed out")
            except requests.RequestException as e:
                if attempts >= 3:
                    raise GeminiError(f"network error: {e}") from e
                time.sleep(10 * attempts)
                continue
            if r.status_code == 200:
                self.ok[self.model] = self.ok.get(self.model, 0) + 1
                return self._parse(r.json())
            full = r.text
            if r.status_code == 429:
                quota = ",".join(sorted(set(re.findall(r'"quotaId":\s*"([^"]+)"', full))))
                limit = ",".join(sorted(set(re.findall(r'"quotaValue":\s*"?(\d+)', full))))
                m = re.search(r'"retryDelay":\s*"(\d+(?:\.\d+)?)s"', full)
                delay = float(m.group(1)) if m else 30.0
                daily = "PerDay" in full or "per day" in full.lower()
                try:
                    gmsg = r.json()["error"].get("message", "")
                except Exception:  # noqa: BLE001
                    gmsg = full
                why = (f"429 quota={quota or '?'} limit={limit or '?'} retry={delay:.0f}s "
                       f"msg={' '.join(gmsg.split())[:260]}")
                self.last_429[self.model] = why
                if (daily or limit == "0" or "limit: 0" in full or delay > 65 or not m
                        or attempts >= 2):
                    if self._next_model(why):
                        attempts = 0
                        continue
                    raise GeminiError("rate limited on every model: " + why)
                log.info("429 on %s (%s), sleeping %.0fs", self.model, why, delay + 2)
                time.sleep(delay + 2)
                continue
            if r.status_code == 404 or (r.status_code in (400, 403) and re.search(
                    r"model|not supported|not available|no longer", full, re.I)):
                if self._next_model(f"{r.status_code}: {full[:200]}"):
                    attempts = 0
                    continue
                raise GeminiError(f"HTTP {r.status_code}: {full[:300]}")
            if r.status_code >= 500 and attempts < 3:
                time.sleep(10 * attempts)
                continue
            if r.status_code >= 500 and self._next_model(f"{r.status_code}: {full[:200]}"):
                attempts = 0
                continue
            raise GeminiError(f"HTTP {r.status_code}: {full[:300]}")

    def probe(self) -> dict:
        """Diagnostics when every model failed: tiny calls with/without Google
        Search grounding, on the preferred model and on a lite model."""
        res = {}
        lite = next((m for m in self.candidates if "lite" in m), self.candidates[-1])
        for model in dict.fromkeys([self.candidates[0], lite]):
            for grounded in (False, True):
                body = {"contents": [{"role": "user", "parts": [{"text": "Reply with the word OK."}]}]}
                if grounded:
                    body["tools"] = [{"google_search": {}}]
                key = f"{model} {'grounded' if grounded else 'plain'}"
                t = time.time()
                try:
                    r = HTTP.post(f"{self.BASE}/models/{model}:generateContent",
                                  headers=self._headers(), json=body, timeout=200)
                    res[key] = f"{r.status_code} in {time.time()-t:.0f}s: {' '.join(r.text.split())[:300]}"
                except Exception as e:  # noqa: BLE001
                    res[key] = f"error after {time.time()-t:.0f}s: {e}"
                time.sleep(5)
        return res

    @staticmethod
    def _parse(j: dict) -> tuple[str, list[dict]]:
        cands = j.get("candidates") or []
        if not cands:
            raise GeminiError(f"no candidates: {json.dumps(j)[:300]}")
        c = cands[0]
        text = "".join(p.get("text", "") for p in c.get("content", {}).get("parts", [])
                       if not p.get("thought"))
        chunks = [ch["web"] for ch in (c.get("groundingMetadata") or {}).get("groundingChunks", [])
                  if "web" in ch]
        if not text.strip():
            raise GeminiError(f"empty answer (finishReason={c.get('finishReason')})")
        return text, chunks


def parse_json(text: str):
    """Extract a JSON array/object from an LLM answer (fences, prose, trailing commas)."""
    t = text.strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.I | re.M).strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    starts = [i for i in (t.find("["), t.find("{")) if i >= 0]
    if not starts:
        raise ValueError("no JSON found")
    start = min(starts)
    opener = t[start]
    closer = "]" if opener == "[" else "}"
    depth, in_str, esc = 0, False, False
    end = None
    for i in range(start, len(t)):
        ch = t[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        elif ch == '"':
            in_str = True
        elif ch in "[{":
            depth += 1
        elif ch in "]}":
            depth -= 1
            if depth == 0 and ch == closer:
                end = i
                break
    chunk = t[start:(end + 1 if end is not None else len(t))]
    chunk = chunk.replace("“", '"').replace("”", '"')
    chunk = re.sub(r",\s*([\]}])", r"\1", chunk)
    return json.loads(chunk)


# --------------------------------------------------------------------------
# Web pages: link check + og:image
# --------------------------------------------------------------------------
_page_cache: dict[str, dict] = {}
META_RE = re.compile(r"<meta\b[^>]*>", re.I)
GENERIC_IMG = re.compile(r"default|placeholder|fallback|favicon|sprite|/logo[^/]*$|-logo\.", re.I)


def _meta(page: str, base: str) -> dict:
    found = {}
    for tag in META_RE.findall(page):
        key = re.search(r'(?:property|name|itemprop)\s*=\s*["\']([^"\']+)["\']', tag, re.I)
        val = re.search(r'content\s*=\s*["\']([^"\']*)["\']', tag, re.I)
        if key and val:
            found.setdefault(key.group(1).lower(), html.unescape(val.group(1)).strip())
    img = None
    for k in ("og:image:secure_url", "og:image", "og:image:url", "twitter:image",
              "twitter:image:src"):
        if found.get(k):
            u = urljoin(base, found[k])
            # skip publishers' generic fallback images; the company logo is better
            if u.startswith("http") and not GENERIC_IMG.search(urlparse(u).path):
                img = u
                break
    desc = found.get("og:description") or found.get("description") or found.get("twitter:description")
    return {"image": img, "description": (desc or "")[:600]}


def fetch_page(url: str) -> dict:
    """{'ok': True/False/None, 'final', 'image': og:image, 'description'}. ok=None means
    "couldn't check" (bot wall / blocked network) — the link is kept."""
    if url in _page_cache:
        return _page_cache[url]
    res = {"ok": None, "final": url, "image": None, "description": ""}
    try:
        r = HTTP.get(url, timeout=10, allow_redirects=True, stream=True,
                     headers={"Accept": "text/html,application/xhtml+xml"})
        res["final"] = r.url
        if r.status_code in (404, 410):
            res["ok"] = False
        elif r.status_code < 400:
            res["ok"] = True
            body = b""
            for part in r.iter_content(65536):
                body += part
                if len(body) > 600_000 or b"</head>" in body.lower():
                    break
            res.update(_meta(body.decode(r.encoding or "utf-8", "replace"), r.url))
        r.close()
    except requests.exceptions.ProxyError:
        pass  # local sandbox proxy refused the host: unknown
    except requests.RequestException:
        res["ok"] = False
    _page_cache[url] = res
    return res


# --------------------------------------------------------------------------
# Company data (yfinance)
# --------------------------------------------------------------------------
COUNTRY_SUFFIX = {
    "united kingdom": ".L", "uk": ".L", "germany": ".DE", "france": ".PA", "italy": ".MI",
    "spain": ".MC", "netherlands": ".AS", "switzerland": ".SW", "japan": ".T",
    "hong kong": ".HK", "china": ".HK", "canada": ".TO", "australia": ".AX", "sweden": ".ST",
    "denmark": ".CO", "norway": ".OL", "finland": ".HE", "belgium": ".BR", "ireland": ".IR",
    "portugal": ".LS", "south korea": ".KS", "korea": ".KS", "taiwan": ".TW", "india": ".NS",
    "brazil": ".SA", "singapore": ".SI", "austria": ".VI", "mexico": ".MX",
}
OTC_EXCHANGES = {"PNK", "OQB", "OQX", "OEM", "OTC"}
US_EXCHANGES = {"NYQ", "NMS", "NGM", "NCM", "ASE", "PCX", "BTS", "NYS"}
STOP = {"inc", "corp", "corporation", "co", "company", "plc", "sa", "s", "a", "spa", "ag",
        "nv", "se", "ltd", "limited", "group", "holdings", "holding", "the", "and", "of",
        "class", "ord", "shares", "adr", "p", "l", "lp", "llc", "asa", "ab", "oyj", "kk"}


def _tokens(s: str) -> set[str]:
    return {w for w in re.findall(r"[a-z0-9]+", (s or "").lower()) if w not in STOP}


def name_sim(a: str, b: str) -> float:
    ta, tb = _tokens(a), _tokens(b)
    if not ta or not tb:
        return 0.0
    return len(ta & tb) / min(len(ta), len(tb))


_search_cache: dict[str, list[dict]] = {}


def yf_search(q: str) -> list[dict]:
    if q not in _search_cache:
        quotes = []
        for attempt in range(3):
            try:
                quotes = yf.Search(q, max_results=10, news_count=0).quotes or []
                break
            except Exception as e:  # noqa: BLE001
                log.debug("search %s: %s", q, e)
                time.sleep(2 + 3 * attempt)
        _search_cache[q] = [x for x in quotes if x.get("quoteType") == "EQUITY"]
    return _search_cache[q]


def resolve_ticker(company: str, hint: str | None, country: str | None) -> tuple[str | None, list[dict]]:
    """Return (best Yahoo symbol, search results). Validates the LLM's hint
    against Yahoo search so 'UCG' becomes 'UCG.MI', 'Toyota' -> '7203.T'."""
    hint = (hint or "").strip().upper().replace(" ", "")
    hint = re.sub(r"^[A-Z]+:", "", hint)  # "NYSE:XOM" -> "XOM"
    results = yf_search(company) if company else []
    syms = {q["symbol"].upper(): q for q in results}
    variants = [hint] if hint else []
    if hint and "." not in hint and country:
        suf = COUNTRY_SUFFIX.get(country.lower().strip())
        if suf:
            variants.insert(0, hint + suf)
    country_suf = COUNTRY_SUFFIX.get((country or "").lower().strip())
    for v in variants:
        if v in syms:
            q = syms[v]
            # A US ADR/OTC symbol was given for a foreign company (e.g. "TM" for
            # Toyota): prefer the home listing (7203.T) when search has it.
            if country_suf and "." not in v:
                home = [x for x in results if x["symbol"].endswith(country_suf) and name_sim(
                    company, x.get("longname") or x.get("shortname") or "") >= 0.5]
                if home:
                    return home[0]["symbol"], results
            return q["symbol"], results
    if results:
        is_us = (country or "").lower() in ("united states", "usa", "us", "u.s.")

        def score(q):
            nm = q.get("longname") or q.get("shortname") or ""
            s = name_sim(company, nm) * 3
            sym = q["symbol"]
            if q.get("exchange") in OTC_EXCHANGES:
                s -= 2
            if country_suf and sym.endswith(country_suf):
                s += 1.5
            if is_us and q.get("exchange") in US_EXCHANGES:
                s += 1.5
            if not country_suf and not is_us and "." not in sym:
                s += 0.5
            return s

        best = max(results, key=score)
        if name_sim(company, best.get("longname") or best.get("shortname") or "") >= 0.5:
            return best["symbol"], results
    # Search gave nothing usable: trust the hint if Yahoo knows it.
    for v in variants:
        try:
            if not yf.Ticker(v).history(period="5d").empty:
                return v, results
        except Exception:  # noqa: BLE001
            pass
    return None, results


def reset_yahoo_session() -> None:
    """Force yfinance to fetch a fresh crumb on the next call (stale crumbs give
    "401 Invalid Crumb"); yfinance itself toggles cookie strategy as needed."""
    try:
        from yfinance.data import YfData
        yd = YfData()
        with yd._cookie_lock:
            yd._crumb = None
    except Exception as e:  # noqa: BLE001
        log.debug("could not reset yahoo session: %s", e)


def _with_retry(fn, tries=3, default=None):
    for a in range(tries):
        try:
            v = fn()
            if v is not None:
                return v
        except Exception as e:  # noqa: BLE001
            log.debug("retry %d: %s", a + 1, e)
            if "Too Many Requests" in str(e) or "RateLimit" in type(e).__name__:
                time.sleep(20 * (a + 1))
        if a + 1 < tries:
            reset_yahoo_session()
            time.sleep(1.5 * (a + 1))
    return default


BUY = re.compile(r"buy|outperform|overweight|accumulate|positive|add\b|top pick|conviction", re.I)
SELL = re.compile(r"sell|underperform|underweight|reduce|negative", re.I)


def rating_bucket(grade: str) -> str:
    g = grade or ""
    if SELL.search(g):
        return "sell"
    if BUY.search(g):
        return "buy"
    return "hold"


_ratings_cache: dict[str, list[dict]] = {}


def firm_ratings(ticker: str, n=3) -> list[dict]:
    if not _ratings_cache.get(ticker):  # only successful lookups are cached
        _ratings_cache[ticker] = _firm_ratings(ticker, n)
    return _ratings_cache[ticker]


def _firm_ratings(ticker: str, n=3) -> list[dict]:
    if "." in ticker:
        # Yahoo has no firm-level actions for non-US listings and answers with
        # "401 Invalid Crumb", which poisons the session. Use the US line instead.
        return []

    def get():
        df = yf.Ticker(ticker).upgrades_downgrades
        return df if df is not None and not df.empty else None

    df = _with_retry(get, tries=3)
    if df is None:
        return []
    df = df.sort_index(ascending=False)
    out, seen = [], set()
    cutoff = pd.Timestamp.now() - pd.DateOffset(years=2)
    for d, row in df.iterrows():
        firm = str(row.get("Firm") or "").strip()
        grade = str(row.get("ToGrade") or "").strip()
        when = pd.Timestamp(d).tz_localize(None) if pd.Timestamp(d).tzinfo else pd.Timestamp(d)
        if not firm or not grade or firm.lower() in seen or when < cutoff:
            continue
        seen.add(firm.lower())
        pt = num(row.get("currentPriceTarget"))
        out.append({"firm": firm, "grade": grade, "rating": rating_bucket(grade),
                    "action": str(row.get("Action") or ""), "date": when.strftime("%Y-%m-%d"),
                    "target": pt if pt else None})
        if len(out) >= n:
            break
    return out


REC_LABEL = {"strong_buy": "Strong Buy", "buy": "Buy", "hold": "Hold",
             "underperform": "Underperform", "sell": "Sell", "none": None}


def company_facts(ticker: str, search_results: list[dict]) -> dict:
    def get_info():
        i = yf.Ticker(ticker).info or {}
        return i if (i.get("longName") or i.get("shortName")) else None

    info = _with_retry(get_info, tries=3, default={}) or {}
    if not info:
        # Fallback without quoteSummary/crumb: search result + fast_info.
        q = next((x for x in search_results if x["symbol"] == ticker), {})
        info = {"longName": q.get("longname") or q.get("shortname"),
                "industry": q.get("industryDisp") or q.get("industry"),
                "fullExchangeName": q.get("exchDisp")}
        try:
            fi = yf.Ticker(ticker).fast_info
            info.update(currency=fi.currency, marketCap=fi.market_cap, currentPrice=fi.last_price)
        except Exception:  # noqa: BLE001
            pass
        info["_partial"] = True
    website = info.get("website") or ""
    domain = urlparse(website).netloc.replace("www.", "") if website else ""
    facts = {
        "name": info.get("longName") or info.get("shortName"),
        "currency": info.get("currency"),
        "price": num(info.get("currentPrice") or info.get("regularMarketPrice")),
        "market_cap": info.get("marketCap"),
        "country": info.get("country"),
        "industry": info.get("industry"),
        "website": website or None,
        "logo": f"https://www.google.com/s2/favicons?domain={domain}&sz=128" if domain else None,
        "exchange": info.get("fullExchangeName") or info.get("exchange"),
        "partial": bool(info.get("_partial")),
    }
    rec = (info.get("recommendationKey") or "none").lower()
    facts["consensus"] = {
        "rating": REC_LABEL.get(rec, rec.replace("_", " ").title() or None),
        "mean": num(info.get("recommendationMean")),
        "analysts": info.get("numberOfAnalystOpinions"),
        "target_mean": num(info.get("targetMeanPrice")),
        "upside": (num((info["targetMeanPrice"] / facts["price"] - 1) * 100, 1)
                   if info.get("targetMeanPrice") and facts["price"] else None),
    }
    try:
        rec_df = yf.Ticker(ticker).recommendations
        if rec_df is not None and not rec_df.empty:
            r0 = rec_df.iloc[0]
            facts["consensus"]["breakdown"] = {
                "buy": int(r0.get("strongBuy", 0) + r0.get("buy", 0)),
                "hold": int(r0.get("hold", 0)),
                "sell": int(r0.get("sell", 0) + r0.get("strongSell", 0))}
    except Exception:  # noqa: BLE001
        pass
    ratings = firm_ratings(ticker)
    facts["ratings_ticker"] = ticker
    if len(ratings) < 3:
        # Non-US listings rarely carry firm-level actions on Yahoo; try the
        # company's US line (ADR / ordinary on NYSE/Nasdaq/OTC).
        name = facts["name"] or ""
        for q in search_results:
            sym = q["symbol"]
            if sym == ticker or "." in sym or q.get("exchange") not in US_EXCHANGES | OTC_EXCHANGES:
                continue
            if name and name_sim(name, q.get("longname") or q.get("shortname") or "") < 0.5:
                continue
            alt = firm_ratings(sym)
            if len(alt) > len(ratings):
                ratings, facts["ratings_ticker"] = alt, sym
            if len(ratings) >= 3:
                break
    facts["ratings"] = ratings
    return facts


def growth(s: pd.Series | None) -> dict:
    if s is None:
        return {}
    r = returns(s)
    return {"w1": r["week"], "y1": r["y1"], "y5": r["y5"], "as_of": r["as_of"]}


# --------------------------------------------------------------------------
# Stories
# --------------------------------------------------------------------------
# Yahoo news-search queries per sector, in addition to the ETF's top holdings.
# Thematic queries pull in non-US companies too.
SECTOR_QUERIES = {
    "XLK": ["semiconductor stocks", "software earnings", "chipmaker", "tech acquisition"],
    "XLC": ["media stocks", "telecom earnings", "streaming subscribers", "advertising revenue"],
    "XLY": ["retail earnings", "automaker", "consumer discretionary stocks", "luxury goods"],
    "XLP": ["consumer staples stocks", "food company earnings", "beverage maker", "grocery retailer"],
    "XLF": ["bank earnings", "European banks", "insurer earnings", "bank merger"],
    "XLV": ["pharma earnings", "FDA approval", "biotech acquisition", "medtech"],
    "XLI": ["industrial stocks", "aerospace defense earnings", "railroad", "airline earnings"],
    "XLB": ["mining stocks", "chemical maker earnings", "copper miner", "steel maker"],
    "XLE": ["oil major earnings", "oil and gas stocks", "LNG deal", "refiner earnings"],
    "XLU": ["utility stocks", "power demand data centers utility", "nuclear power deal", "electric utility earnings"],
    "XLRE": ["REIT earnings", "real estate investment trust", "data center REIT", "commercial real estate"],
}
NEWS_MAX_AGE_DAYS = 4
JUNK_TITLE = re.compile(r"\b(stocks? to (buy|watch|sell)|top \d+|\d+ (stocks|reasons)|should you buy|"
                        r"is it time to|buy now|sector update|market (open|close|wrap)|premarket|"
                        r"stock market today|dividend stocks|etfs? to|better buy|what to watch)\b", re.I)


_holdings_cache: dict[str, list[str]] = {}


def etf_holdings(etf: str, n=10) -> list[str]:
    if etf not in _holdings_cache:
        try:
            h = yf.Ticker(etf).funds_data.top_holdings
            _holdings_cache[etf] = [str(x) for x in list(h.index)]
        except Exception as e:  # noqa: BLE001
            log.warning("holdings %s: %s", etf, e)
            return []
    return _holdings_cache[etf][:n]


def news_search(q: str, n=12) -> list[dict]:
    for attempt in range(2):
        try:
            return yf.Search(q, news_count=n, max_results=0).news or []
        except Exception as e:  # noqa: BLE001
            log.debug("news %s: %s", q, e)
            time.sleep(2)
    return []


def collect_news(queries: list[str], max_items=45, describe=True) -> list[dict]:
    """Recent news from Yahoo Finance search: real links, publisher, time."""
    from concurrent.futures import ThreadPoolExecutor
    cutoff = time.time() - NEWS_MAX_AGE_DAYS * 86400
    items, seen = [], set()
    for q in queries:
        for x in news_search(q):
            title = (x.get("title") or "").strip()
            link = x.get("link") or ""
            ts = x.get("providerPublishTime") or 0
            key = re.sub(r"\W+", "", title.lower())[:80]
            if (not title or not link or ts < cutoff or key in seen
                    or x.get("type", "STORY") not in ("STORY", "VIDEO")):
                continue
            seen.add(key)
            thumb = None
            for res in ((x.get("thumbnail") or {}).get("resolutions") or []):
                if res.get("width", 0) >= 300:
                    thumb = res.get("url")
            items.append({"title": title, "url": link, "publisher": x.get("publisher") or "",
                          "ts": ts, "tickers": x.get("relatedTickers") or [], "image": thumb,
                          "junk": bool(JUNK_TITLE.search(title))})
        time.sleep(0.25)
    items.sort(key=lambda i: (i["junk"], -i["ts"]))
    items = items[:max_items]
    if describe:
        with ThreadPoolExecutor(12) as ex:
            pages = list(ex.map(lambda i: fetch_page(i["url"]), items))
        for i, pg in zip(items, pages):
            i["description"] = pg.get("description") or ""
            i["image"] = i["image"] or pg.get("image")
            i["url"] = pg.get("final") or i["url"]
    return items


STORY_PROMPT = """Today is {today}. You are a sell-side equity research editor.
Below are recent news items (last {days} days) relevant to the GICS sector "{sector}".
Pick the {n} most important stories for investors in this sector: company-specific, material
events such as earnings and guidance, demand, pricing, M&A, regulation, technology, financing.
Rank by importance. One story per event (merge duplicates), no generic market commentary,
no "stocks to buy" lists, no stories about ETFs. Fewer than {n} is fine if fewer qualify.
Companies may be listed anywhere in the world but must belong to this sector.

NEWS ITEMS:
{items}

Return ONLY a JSON array (no prose, no markdown). Each element:
{{
 "id": <number of the news item used>,
 "headline": "concise factual headline",
 "company": "company name",
 "ticker": "Yahoo Finance symbol of the primary listing incl. exchange suffix (e.g. AAPL, UCG.MI, 7203.T)",
 "country": "country of headquarters",
 "profile": "ONE sentence: what the company does, its size/rank in its industry, where it is from",
 "what_happened": "2-3 sentences with the key figures - use ONLY facts and numbers stated in the item",
 "why_it_matters": "1-2 sentences on why it matters to the sector and investors"
}}"""


def clean_story(raw: dict) -> dict | None:
    if not isinstance(raw, dict):
        return None
    s = {k: (str(raw.get(k)).strip() if raw.get(k) is not None else "")
         for k in ("headline", "company", "ticker", "country", "profile", "what_happened",
                   "why_it_matters")}
    if not s["headline"] or not s["company"]:
        return None
    try:
        s["id"] = int(str(raw.get("id")).strip("[] "))
    except (TypeError, ValueError):
        return None
    return s


def sector_stories(gem: Gemini, etf: str, sector: str, today: str) -> list[dict]:
    queries = etf_holdings(etf) + SECTOR_QUERIES.get(etf, [f"{sector} stocks"])
    news = collect_news(queries)
    log.info("%s: %d candidate news items", etf, len(news))
    if len(news) < 3:
        raise GeminiError("too few news items found")
    lines = []
    for i, n in enumerate(news, 1):
        d = dt.datetime.fromtimestamp(n["ts"], dt.timezone.utc).strftime("%Y-%m-%d")
        desc = f" | {n['description'][:350]}" if n.get("description") else ""
        tick = f" | tickers: {','.join(n['tickers'][:5])}" if n["tickers"] else ""
        lines.append(f"[{i}] {d} | {n['publisher']} | {n['title']}{tick}{desc}")
    prompt = STORY_PROMPT.format(today=today, days=NEWS_MAX_AGE_DAYS, n=STORIES_PER_SECTOR,
                                 sector=sector, items="\n".join(lines))
    items = None
    for attempt in range(2):
        text, _ = gem.generate(prompt, grounded=False)
        try:
            items = parse_json(text)
            if isinstance(items, dict):
                items = items.get("stories") or items.get("items") or [items]
            break
        except (ValueError, json.JSONDecodeError) as e:
            log.warning("%s: JSON parse failed (%s); retrying", etf, e)
            prompt += "\n\nIMPORTANT: your previous answer was not valid JSON. Output ONLY the JSON array."
    if items is None:
        raise GeminiError("could not parse stories JSON")
    stories, used = [], set()
    for raw in items:
        s = clean_story(raw)
        if not s or not (1 <= s["id"] <= len(news)) or s["id"] in used:
            continue
        used.add(s["id"])
        n = news[s.pop("id") - 1]
        s.update(url=n["url"], source=n["publisher"], image=n.get("image"),
                 published=dt.datetime.fromtimestamp(n["ts"], dt.timezone.utc).strftime("%Y-%m-%d"),
                 related=n["tickers"], link_verified=True)
        # The feed's related tickers are a better hint than the model's guess.
        if not s["ticker"] and n["tickers"]:
            s["ticker"] = n["tickers"][0]
        stories.append(s)
    return stories[:STORIES_PER_SECTOR]


def enrich_stories(sectors: dict) -> None:
    """Resolve tickers and attach yfinance growth, facts and analyst ratings."""
    all_items = [s for sec in sectors.values() for s in sec["items"]]
    resolved = {}
    for s in all_items:
        key = (s["company"], s.get("ticker"))
        if key not in resolved and out_of_time(180):
            resolved[key] = (None, [])
        if key not in resolved:
            resolved[key] = resolve_ticker(s["company"], s.get("ticker"), s.get("country"))
            time.sleep(0.3)
        sym, results = resolved[key]
        s["symbol"], s["_search"] = sym, results
    syms = sorted({s["symbol"] for s in all_items if s["symbol"]})
    closes = fetch_closes(syms) if syms else {}
    facts_cache = {}
    for s in all_items:
        sym = s["symbol"]
        if sym and sym not in facts_cache and not out_of_time(90):
            facts_cache[sym] = company_facts(sym, s["_search"])
            time.sleep(0.4)
    # Yahoo sometimes rejects the first burst of quoteSummary calls; retry those
    # tickers once after a pause.
    retry = [k for k, f in facts_cache.items() if f.get("partial")] if not out_of_time(240) else []
    if retry:
        log.info("retrying company facts for %s", ", ".join(retry))
        time.sleep(8)
        reset_yahoo_session()
        for sym in retry:
            res = next((s["_search"] for s in all_items if s["symbol"] == sym), [])
            f = company_facts(sym, res)
            if not f.get("partial") or not facts_cache[sym].get("ratings"):
                facts_cache[sym] = f
            time.sleep(1)
    for s in all_items:
        sym = s["symbol"]
        s["growth"] = growth(closes.get(sym)) if sym else {}
        s["facts"] = facts_cache.get(sym, {}) if sym else {}
        if not s.get("image"):
            s["image"] = None
        s.pop("_search", None)


# --------------------------------------------------------------------------
# AI research picks: Gemini chooses from real candidates with real numbers
# --------------------------------------------------------------------------
PICKS_PER_SECTOR = 3
PICKS_PROMPT = """Today is {today}. You are a long-term, fundamentals-driven equity analyst helping an
investor who NEVER trades short term. Their philosophy: buy shares of high-quality companies at good
prices and hold them for a MINIMUM of 5 years, ideally 10+. No day trading, no medium-term trades.

For each GICS sector below you get candidate companies with REAL data: 5-year / 1-year / 1-week price
return, return on equity (ROE), profit margin, revenue growth (yoy), debt/equity, free-cash-flow
yield, dividend yield, trailing and forward P/E, distance from the 52-week high, analyst consensus,
market cap, and today's news headline if any.

For EACH sector pick exactly {n} LONG-TERM holdings to research. Favour:
- durable quality: high/consistent ROE and margins, healthy balance sheet, real free cash flow,
  a lasting competitive advantage (scale, brand, network, switching costs, regulated assets...)
- a sensible price today: reasonable P/E vs. the company's quality/growth, or a good entry point
  (e.g. well below its 52-week high) - but never a broken business just because it's cheap
Avoid speculative, unprofitable or highly indebted names and pure momentum stories. Short-term
news is only relevant if it makes the long-term entry price more attractive.
Only choose from the listed candidates. Never invent numbers or facts.

For each pick give:
- "horizon": "5+ years" or "10+ years" (your intended minimum holding period; never shorter)
- "angle": one of "Compounder", "Quality at a discount", "Dividend grower", "Long-term growth"
- "reason": ONE or two sentences (max 40 words): the durable advantage AND why today's price is
  reasonable, citing at least one concrete number from the data.

{blocks}

Return ONLY a JSON object (no markdown) mapping each sector ETF code to a list of {n}:
{{"XLK": [{{"ticker": "...", "horizon": "10+ years", "angle": "Compounder", "reason": "..."}}, ...], ...}}"""
PICK_ANGLES = ("Compounder", "Quality at a discount", "Dividend grower", "Long-term growth")


def quick_facts(sym: str) -> dict:
    def get_info():
        i = yf.Ticker(sym).info or {}
        return i if (i.get("longName") or i.get("shortName")) else None

    i = _with_retry(get_info, tries=2, default={}) or {}
    price = i.get("currentPrice") or i.get("regularMarketPrice")
    tgt = i.get("targetMeanPrice")
    hi = i.get("fiftyTwoWeekHigh")
    mcap = i.get("marketCap")
    fcf = i.get("freeCashflow")
    rec = (i.get("recommendationKey") or "none").lower()
    pct_ = lambda v: num(v * 100, 1) if isinstance(v, (int, float)) else None  # noqa: E731
    return {
        "name": i.get("shortName") or i.get("longName") or sym,
        "currency": i.get("currency"),
        "market_cap": mcap,
        "fwd_pe": num(i.get("forwardPE"), 1),
        "pe": num(i.get("trailingPE"), 1),
        "roe": pct_(i.get("returnOnEquity")),
        "margin": pct_(i.get("profitMargins")),
        "rev_growth": pct_(i.get("revenueGrowth")),
        "debt_eq": num(i.get("debtToEquity"), 0),
        "fcf_yield": num(fcf / mcap * 100, 1) if fcf and mcap else None,
        "div_yield": num(i.get("dividendYield"), 2),      # yfinance gives this in %
        "off_high": num((price / hi - 1) * 100, 1) if price and hi else None,
        "consensus": REC_LABEL.get(rec, rec.replace("_", " ").title() or None),
        "analysts": i.get("numberOfAnalystOpinions"),
        "upside": num((tgt / price - 1) * 100, 1) if tgt and price else None,
    }


def build_picks(gem: Gemini, story_secs: dict, today: str) -> dict:
    cands: dict[str, list[str]] = {}
    news: dict[str, str] = {}
    for etf, _ in SECTORS:
        syms = list(etf_holdings(etf, 15))
        for st in (story_secs.get(etf) or {}).get("items", []):
            if st.get("symbol"):
                syms.append(st["symbol"])
                news.setdefault(st["symbol"], st["headline"])
        cands[etf] = list(dict.fromkeys(syms))
    all_syms = sorted({x for v in cands.values() for x in v})
    closes = fetch_closes(all_syms, period="6y")
    facts = {}
    for sym in all_syms:
        if out_of_time(150):
            break
        facts[sym] = quick_facts(sym)
        g = returns(closes[sym]) if sym in closes else {}
        facts[sym].update(w1=g.get("week"), y1=g.get("y1"), y5=g.get("y5"))
        time.sleep(0.2)

    def f(v, suf="%"):
        return "n/a" if v is None else (f"{v:+.1f}{suf}" if suf == "%" else f"{v}{suf}")

    blocks = []
    for etf, sector in SECTORS:
        lines = [f"## {etf} - {sector}"]
        for sym in cands[etf]:
            x = facts.get(sym)
            if not x:
                continue
            mc = (f"${x['market_cap'] / 1e9:.0f}B" if x.get("market_cap") else "n/a")
            line = (f"- {sym} ({x['name']}): 5Y {f(x['y5'])}, 1Y {f(x['y1'])}, 1W {f(x['w1'])}, "
                    f"ROE {f(x['roe'])}, margin {f(x['margin'])}, rev growth {f(x['rev_growth'])}, "
                    f"debt/equity {x['debt_eq'] if x['debt_eq'] is not None else 'n/a'}, "
                    f"FCF yield {f(x['fcf_yield'])}, div yield {f(x['div_yield'])}, "
                    f"P/E {x['pe'] or 'n/a'} (fwd {x['fwd_pe'] or 'n/a'}), vs 52w high {f(x['off_high'])}, "
                    f"consensus {x['consensus'] or 'n/a'} ({x['analysts'] or 0} analysts), mkt cap {mc}")
            if sym in news:
                line += f", news today: {news[sym]}"
            lines.append(line)
        blocks.append("\n".join(lines))
    prompt = PICKS_PROMPT.format(today=today, n=PICKS_PER_SECTOR, blocks="\n\n".join(blocks))
    j = None
    for attempt in range(2):
        text, _ = gem.generate(prompt)
        try:
            j = parse_json(text)
            if isinstance(j, dict):
                break
            raise ValueError("not an object")
        except (ValueError, json.JSONDecodeError) as e:
            log.warning("picks parse failed (%s)", e)
            prompt += "\n\nIMPORTANT: output ONLY the JSON object."
            j = None
    if j is None:
        raise GeminiError("could not parse picks JSON")
    out = {}
    for etf, _ in SECTORS:
        picks, seen = [], set()
        for p in (j.get(etf) or j.get(etf.lower()) or []):
            if not isinstance(p, dict):
                continue
            sym = str(p.get("ticker") or "").strip().upper()
            match = next((c for c in cands[etf] if c.upper() == sym), None)
            if not match or match in seen or not p.get("reason"):
                continue   # only real candidates; numbers come from Yahoo, not the model
            seen.add(match)
            x = facts.get(match, {})
            angle = str(p.get("angle") or "").strip()
            angle = next((a for a in PICK_ANGLES if a.lower() == angle.lower()), angle.title()[:24])
            horizon = "10+ years" if "10" in str(p.get("horizon") or "") else "5+ years"
            picks.append({"ticker": match, "name": x.get("name"), "angle": angle, "horizon": horizon,
                          "reason": str(p["reason"]).strip(),
                          **{k: x.get(k) for k in ("y5", "y1", "roe", "margin", "fwd_pe", "pe",
                                                   "div_yield", "off_high", "consensus", "analysts")}})
            if len(picks) >= PICKS_PER_SECTOR:
                break
        if picks:
            out[etf] = picks
    if len(out) < 6:
        raise GeminiError(f"picks returned for only {len(out)} sectors")
    return out


# --------------------------------------------------------------------------
# Reasons + recap
# --------------------------------------------------------------------------
REASON_PROMPT = """Today is {today} (Madrid time {time}). Below is REAL performance data (price
returns in %) for the S&P 500 (SPY) and the 11 SPDR sector ETFs, {as_of}.

{table}

Recent market headlines (newest first):
{news}

Explain the moves using the headlines above and general market knowledge. Use the numbers
above exactly as given; do not invent other performance figures or events. If the headlines
don't explain a move, say what the sector is exposed to (rates, oil, consumer demand...).
Return ONLY a JSON object (no markdown):
{{
 "recap": "3-4 sentence overall market recap: what drove the S&P 500 today and this week, leaders vs laggards, key macro (rates, Fed, data, oil, earnings)",
 "reasons": {{ "SPY": "1-2 sentences why it moved", "XLK": "...", ... one entry for each ticker above }}
}}"""


def build_reasons(gem: Gemini, market: dict, today: str) -> dict:
    lines = ["ticker | name | day | week | month | YTD | 1y | 5y"]
    for r in market["rows"]:
        f = lambda k: "n/a" if r.get(k) is None else f"{r[k]:+.2f}"  # noqa: E731
        lines.append(f"{r['ticker']} | {r['name']} | {f('day')} | {f('week')} | {f('month')} | "
                     f"{f('ytd')} | {f('y1')} | {f('y5')}")
    m = now_utc().astimezone(MADRID)
    queries = ["stock market today", "S&P 500", "Wall Street stocks", "Treasury yields Fed",
               "oil prices"] + [f"{name} stocks" for _, name in SECTORS]
    news = collect_news(queries, max_items=70, describe=False)
    hl = "\n".join(f"- {dt.datetime.fromtimestamp(n['ts'], dt.timezone.utc):%a %H:%M} UTC | "
                   f"{n['publisher']} | {n['title']}" for n in sorted(news, key=lambda n: -n["ts"]))
    prompt = REASON_PROMPT.format(today=today, time=f"{m:%H:%M}", as_of=market["as_of_label"],
                                  table="\n".join(lines), news=hl or "(none available)")
    for attempt in range(2):
        text, _ = gem.generate(prompt, grounded=False)
        try:
            j = parse_json(text)
            reasons = {k.upper(): str(v).strip() for k, v in (j.get("reasons") or {}).items()}
            if j.get("recap") and len(reasons) >= 6:
                return {"recap": str(j["recap"]).strip(), "reasons": reasons}
            raise ValueError("incomplete reasons")
        except (ValueError, AttributeError, json.JSONDecodeError) as e:
            log.warning("reasons parse failed (%s)", e)
            prompt += "\n\nIMPORTANT: output ONLY the JSON object."
    raise GeminiError("could not parse reasons JSON")


# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
def load_prev() -> dict:
    try:
        return json.loads(DATA_PATH.read_text())
    except Exception:  # noqa: BLE001
        return {}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="run regardless of Madrid time")
    ap.add_argument("--sectors", help="comma list of ETFs for stories (testing)")
    ap.add_argument("--skip-stories", action="store_true")
    args = ap.parse_args()
    force = args.force or os.environ.get("FORCE_RUN", "").lower() in ("1", "true", "yes")

    prev = load_prev()
    due, why = current_slot(prev)
    if not force and not due:
        log.info("Skipping: %s", why)
        return 0
    log.info("Building (%s)", "forced" if force else why)

    t0 = now_utc()
    today = t0.astimezone(MADRID).strftime("%A %d %B %Y")
    notices: list[str] = []
    out = {"generated_at": iso(t0), "notices": notices, "model": None,
           "market": prev.get("market"), "recap": prev.get("recap"),
           "reasons": prev.get("reasons") or {}, "stories": prev.get("stories") or {},
           "picks": prev.get("picks") or {}}

    # 1) Numbers
    market = None
    try:
        market = build_market()
        market["updated_at"] = iso(now_utc())
        out["market"] = market
        if market["missing"]:
            notices.append("No price data this run for " + ", ".join(market["missing"]) + ".")
        log.info("market ok: %s", market["as_of_label"])
    except Exception as e:  # noqa: BLE001
        log.exception("market failed")
        notices.append(f"Sector performance could not be refreshed ({type(e).__name__}); "
                       "showing the last good data.")

    # 2) Gemini parts
    key = os.environ.get("GEMINI_API_KEY", "").strip()
    log.info("GEMINI_API_KEY %s", "present" if key else "MISSING (set the repo secret)")
    gem = None
    if not key:
        notices.append("GEMINI_API_KEY is not set; stories and explanations were not refreshed.")
    else:
        try:
            gem = Gemini(key, os.environ.get("GEMINI_MODEL") or None,
                         float(os.environ.get("GEMINI_MIN_INTERVAL", "12")))
        except Exception as e:  # noqa: BLE001
            notices.append(f"Gemini unavailable ({e}); stories and explanations not refreshed.")

    if gem and market:
        try:
            rr = build_reasons(gem, market, today)
            out["recap"] = {"text": rr["recap"], "updated_at": iso(now_utc())}
            out["reasons"] = {"updated_at": iso(now_utc()), "items": rr["reasons"]}
        except Exception as e:  # noqa: BLE001
            log.exception("reasons failed")
            notices.append(f"Market recap / 'why it moved' not refreshed ({str(e)[:120]}); "
                           "showing the previous text.")
    elif gem and not market:
        notices.append("Explanations skipped because prices failed.")

    if gem and not args.skip_stories:
        only = args.sectors or os.environ.get("STORY_SECTORS", "")
        want = {x.strip() for x in only.upper().split(",") if x.strip()} or None
        if want:
            log.info("stories limited to %s", ", ".join(sorted(want)))
        prev_secs = (prev.get("stories") or {}).get("sectors") or {}
        sectors_out, fresh, failed = {}, {}, []
        for etf, sector in SECTORS:
            if want and etf not in want:
                if etf in prev_secs:
                    sectors_out[etf] = prev_secs[etf]
                continue
            if out_of_time(12 * 60):  # leave time for Yahoo enrichment, picks + writing
                failed.append(sector)
                if etf in prev_secs:
                    sectors_out[etf] = {**prev_secs[etf], "stale": True}
                continue
            try:
                log.info("stories: %s", sector)
                items = sector_stories(gem, etf, sector, today)
                if not items:
                    raise GeminiError("no usable stories returned")
                fresh[etf] = {"name": sector, "updated_at": iso(now_utc()), "items": items}
            except Exception as e:  # noqa: BLE001
                log.error("stories %s failed: %s", etf, e)
                failed.append(sector)
                if etf in prev_secs:
                    sectors_out[etf] = {**prev_secs[etf], "stale": True}
        try:
            enrich_stories(fresh)
        except Exception as e:  # noqa: BLE001
            log.exception("enrich failed")
            notices.append(f"Company prices/ratings partly unavailable ({type(e).__name__}).")
        sectors_out.update(fresh)
        # cross-sector de-duplication (same article in two sectors)
        seen = set()
        for etf, _ in SECTORS:
            if etf in sectors_out:
                keep = []
                for s in sectors_out[etf]["items"]:
                    k = (s.get("url") or s["headline"]).lower()
                    if k not in seen:
                        seen.add(k)
                        keep.append(s)
                sectors_out[etf]["items"] = keep
        out["stories"] = {"updated_at": iso(now_utc()) if fresh else
                          (prev.get("stories") or {}).get("updated_at"),
                          "sectors": {e: sectors_out[e] for e, _ in SECTORS if e in sectors_out}}
        if failed:
            notices.append("Stories not refreshed for: " + ", ".join(failed) +
                           " (showing previous stories where available).")
    if gem and not os.environ.get("SKIP_PICKS"):
        try:
            picks = build_picks(gem, (out.get("stories") or {}).get("sectors") or {}, today)
            out["picks"] = {"updated_at": iso(now_utc()), "items": picks}
            log.info("picks ok for %d sectors", len(picks))
        except Exception as e:  # noqa: BLE001
            log.exception("picks failed")
            notices.append(f"AI research picks not refreshed ({str(e)[:120]}); showing previous picks.")
    out["model"] = gem.model if gem else (prev.get("model"))
    if gem:  # small diagnostics block, handy when Google renames/retires models
        out["gemini_debug"] = {"candidates": gem.candidates, "errors": gem.errors,
                               "ok_calls": gem.ok, "last_429": gem.last_429,
                               "probe": gem.probe() if not gem.ok else None,
                               "flash_models_listed": [n for n in gem.available if "flash" in n]}

    DATA_PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = DATA_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(out, ensure_ascii=False, indent=1, default=str))
    tmp.replace(DATA_PATH)
    log.info("wrote %s in %.0fs; notices: %s", DATA_PATH, (now_utc() - t0).total_seconds(),
             notices or "none")
    # Fail the workflow (so GitHub emails you) only if nothing at all refreshed.
    return 1 if (market is None and not (out.get("stories") or {}).get("sectors")) else 0


if __name__ == "__main__":
    sys.exit(main())
