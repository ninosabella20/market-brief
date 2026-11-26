#!/usr/bin/env python3
"""Holdings tab -> docs/holdings.json

No Gemini calls: Yahoo Finance prices only. Runs as a separate workflow step
after the market build.

* Holdings: tickers listed in holdings.txt, with 1-year (daily) and 5-year
  (weekly) chart series and key numbers.
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import build  # noqa: E402
from build import iso, log, now_utc, num  # noqa: E402

import pandas as pd  # noqa: E402

ROOT = build.ROOT
HOLDINGS_TXT = ROOT / "holdings.txt"
HOLDINGS_OUT = ROOT / "docs" / "holdings.json"


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
def main() -> int:
    t0 = time.time()
    hold = read_holdings()
    closes = build.fetch_closes(hold, period="6y")
    HOLDINGS_OUT.write_text(json.dumps(build_holdings(hold, closes), separators=(",", ":")))
    log.info("holdings: %d tickers in %.0fs", len(hold), time.time() - t0)
    return 0


if __name__ == "__main__":
    sys.exit(main())
