#!/usr/bin/env python3
"""Holdings + picks tracker -> docs/holdings.json, docs/picks_history.json

No Gemini calls: Yahoo Finance prices only. Runs as a separate workflow step
after the market build.

* Holdings: tickers listed in holdings.txt, with 1-year (daily) and 5-year
  (weekly) chart series and key numbers.
* Picks tracker: every long-term pick is logged the first day it appears
  (date + closing price). Each run measures its return since then against the
  S&P 500 (SPY) over the same period.
"""
from __future__ import annotations

import datetime as dt
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402
from build import MADRID, iso, log, now_utc, num  # noqa: E402

import pandas as pd  # noqa: E402

ROOT = build.ROOT
HOLDINGS_TXT = ROOT / "holdings.txt"
HOLDINGS_OUT = ROOT / "docs" / "holdings.json"
PICKS_HIST = ROOT / "docs" / "picks_history.json"
BENCH = "SPY"
SECTOR_NAMES = dict(build.SECTORS)


def read_holdings() -> list[str]:
    out = []
    for line in HOLDINGS_TXT.read_text().splitlines():
        t = line.split("#", 1)[0].strip().upper()
        if t and t not in out:
            out.append(t)
    return out


def series_out(s: pd.Series, n_points: int | None = None) -> dict:
    """Compact chart series: {"start", "end", "v": [closes...]}, optionally thinned."""
    if n_points and len(s) > n_points:
        step = len(s) / n_points
        idx = sorted({int(i * step) for i in range(n_points)} | {len(s) - 1})
        s = s.iloc[idx]
    return {"start": s.index[0].strftime("%Y-%m-%d"), "end": s.index[-1].strftime("%Y-%m-%d"),
            "v": [round(float(v), 2) for v in s.values]}


def close_on(s: pd.Series, day: str) -> tuple[str, float] | None:
    """Last close on or before `day` (YYYY-MM-DD)."""
    sub = s[s.index <= pd.Timestamp(day)]
    if sub.empty:
        return None
    return sub.index[-1].strftime("%Y-%m-%d"), float(sub.iloc[-1])


# --------------------------------------------------------------------------
def build_holdings(tickers: list[str], closes: dict) -> dict:
    items = []
    for t in tickers:
        s = closes.get(t)
        if s is None or len(s) < 2:
            items.append({"ticker": t, "error": "no price data"})
            continue
        s = s.sort_index()
        r = build.returns(s)
        last_d = s.index[-1]
        y1 = s[s.index > last_d - pd.DateOffset(years=1)]
        y5 = s[s.index > last_d - pd.DateOffset(years=5)].resample("W-FRI").last().dropna()
        hi = float(y1.max()) if len(y1) else None
        f = build.quick_facts(t)
        time.sleep(0.2)
        items.append({
            "ticker": t, "name": f.get("name") or t, "currency": f.get("currency") or "USD",
            "last": r["last"], "as_of": r["as_of"],
            "day": r["day"], "w1": r["week"], "ytd": r["ytd"], "y1": r["y1"], "y5": r["y5"],
            "off_high": num((float(s.iloc[-1]) / hi - 1) * 100, 1) if hi else None,
            "fwd_pe": f.get("fwd_pe") or f.get("pe"), "consensus": f.get("consensus"),
            "s1y": series_out(y1, 130), "s5y": series_out(y5),
        })
    return {"generated_at": iso(now_utc()), "items": items}


# --------------------------------------------------------------------------
def update_picks_history(hist: dict, data: dict, closes: dict) -> dict:
    """Log new picks (first appearance) from today's data.json picks."""
    picks = (data.get("picks") or {}).get("items") or {}
    updated = (data.get("picks") or {}).get("updated_at") or iso(now_utc())
    day = dt.datetime.fromisoformat(updated.replace("Z", "+00:00")).astimezone(MADRID).strftime("%Y-%m-%d")
    pos = {p["ticker"]: p for p in hist.get("positions", [])}
    for etf, plist in picks.items():
        for p in plist:
            if "horizon" not in p:      # only long-term picks are tracked
                continue
            t = p["ticker"]
            if t in pos:
                q = pos[t]
                if q.get("last_picked") != day:
                    q["last_picked"] = day
                    q["times_picked"] = q.get("times_picked", 1) + 1
                q.update(horizon=p.get("horizon"), angle=p.get("angle"), reason=p.get("reason"))
                continue
            s = closes.get(t)
            # Picks are made ~06:00 Madrid, before the US open: the fair entry is the last
            # close BEFORE the pick day (the price known when the pick was made).
            prev_day = (dt.date.fromisoformat(day) - dt.timedelta(days=1)).isoformat()
            entry = close_on(s, prev_day) if s is not None else None
            if not entry:
                log.warning("tracker: no price for new pick %s", t)
                continue
            pos[t] = {"ticker": t, "name": p.get("name") or t, "sector": etf,
                      "horizon": p.get("horizon"), "angle": p.get("angle"), "reason": p.get("reason"),
                      "first_picked": day, "entry_date": entry[0], "entry_price": round(entry[1], 2),
                      "last_picked": day, "times_picked": 1}
    hist["positions"] = sorted(pos.values(), key=lambda p: (p["first_picked"], p["ticker"]))
    hist["picks_day"] = day
    return hist


def performance(hist: dict, closes: dict, current: set[str]) -> dict:
    bench = closes.get(BENCH)
    rows = []
    for p in hist.get("positions", []):
        s = closes.get(p["ticker"])
        if s is None or bench is None:
            continue
        s = s.sort_index()
        since = s[s.index >= pd.Timestamp(p["entry_date"])]
        b_entry = close_on(bench, p["entry_date"])
        if since.empty or not b_entry:
            continue
        b_since = bench[bench.index >= pd.Timestamp(b_entry[0])]
        ret = (float(since.iloc[-1]) / p["entry_price"] - 1) * 100
        bret = (float(bench.iloc[-1]) / b_entry[1] - 1) * 100
        rows.append({**p, "price": round(float(since.iloc[-1]), 2), "price_date": since.index[-1].strftime("%Y-%m-%d"),
                     "ret": num(ret, 2), "bench_ret": num(bret, 2), "excess": num(ret - bret, 2),
                     "active": p["ticker"] in current,   # in the picks shown on the site right now
                     # both rebased to 100 at entry, for the comparison chart
                     "chart": series_out(since / p["entry_price"] * 100, 260),
                     "bench_chart": series_out(b_since / b_entry[1] * 100, 260)})
    n = len(rows)
    summary = {}
    if n:
        summary = {"count": n, "active": sum(r["active"] for r in rows),
                   "avg_ret": num(sum(r["ret"] for r in rows) / n, 2),
                   "avg_bench": num(sum(r["bench_ret"] for r in rows) / n, 2),
                   "beating": sum(1 for r in rows if r["excess"] > 0),
                   "since": min(r["first_picked"] for r in rows)}
        summary["avg_excess"] = num(summary["avg_ret"] - summary["avg_bench"], 2)
    return {"summary": summary, "rows": rows}


# --------------------------------------------------------------------------
def main() -> int:
    t0 = time.time()
    data = json.loads((ROOT / "docs" / "data.json").read_text())
    try:
        hist = json.loads(PICKS_HIST.read_text())
    except Exception:  # noqa: BLE001
        hist = {"positions": []}
    hold = read_holdings()
    pick_syms = [p["ticker"] for p in hist.get("positions", [])] + [
        p["ticker"] for v in ((data.get("picks") or {}).get("items") or {}).values() for p in v]
    closes = build.fetch_closes(sorted(set(hold + pick_syms + [BENCH])), period="6y")

    try:
        HOLDINGS_OUT.write_text(json.dumps(build_holdings(hold, closes), separators=(",", ":")))
    except Exception:  # noqa: BLE001
        log.exception("holdings failed")
    hist = update_picks_history(hist, data, closes)
    current = {p["ticker"] for v in ((data.get("picks") or {}).get("items") or {}).values() for p in v}
    hist["performance"] = performance(hist, closes, current)
    hist["generated_at"] = iso(now_utc())
    PICKS_HIST.write_text(json.dumps(hist, separators=(",", ":")))
    log.info("tracker: %d holdings, %d tracked picks in %.0fs", len(hold),
             len(hist.get("positions", [])), time.time() - t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
