(() => {
  "use strict";
  const DAY_MS = 26 * 3600 * 1000; // daily refresh + slack for late GitHub runs
  const SHORT = { SPY: "S&P 500", XLK: "Tech", XLC: "Comm.", XLY: "Discr.", XLP: "Staples",
    XLF: "Financials", XLV: "Health", XLI: "Industr.", XLB: "Materials", XLE: "Energy",
    XLU: "Utilities", XLRE: "Real Est." };
  // colour saturates at these magnitudes (%), per column
  const SCALE = { day: 2, week: 5, month: 8, ytd: 25, y1: 30, y5: 100 };
  const COLS = ["day", "week", "month", "ytd", "y1", "y5"];

  const $ = (s, r = document) => r.querySelector(s);
  function el(tag, attrs, ...kids) {
    const n = document.createElement(tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) {
      if (v == null || v === false) continue;
      if (k === "class") n.className = v;
      else if (k === "text") n.textContent = v;
      else if (k.startsWith("on")) n.addEventListener(k.slice(2), v);
      else n.setAttribute(k, v === true ? "" : v);
    }
    for (const k of kids.flat(Infinity)) if (k != null && k !== false) n.append(k.nodeType ? k : String(k));
    return n;
  }
  const store = {
    get(k) { try { return localStorage.getItem(k); } catch { return null; } },
    set(k, v) { try { localStorage.setItem(k, v); } catch { /* ignore */ } },
  };
  const pct = (v, d = 1) => v == null ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(d) + "%";
  const cls = v => v == null ? "" : v > 0 ? "up" : v < 0 ? "down" : "";
  const fmtDate = s => {
    if (!s) return "";
    const d = new Date(s.length === 10 ? s + "T12:00:00Z" : s);
    return isNaN(d) ? s : d.toLocaleDateString(undefined, { day: "numeric", month: "short", year: "numeric" });
  };
  const fmtStamp = s => {
    const d = new Date(s);
    return d.toLocaleString(undefined, { weekday: "short", day: "numeric", month: "short", hour: "2-digit", minute: "2-digit" });
  };
  const ago = s => { const h = (Date.now() - new Date(s)) / 3600000; return h < 1 ? "just now" : h < 48 ? Math.round(h) + "h ago" : Math.round(h / 24) + " days ago"; };
  const money = (v, cur) => {
    if (v == null) return "";
    try { return new Intl.NumberFormat(undefined, { style: "currency", currency: cur || "USD", maximumFractionDigits: v >= 1000 ? 0 : 2 }).format(v); }
    catch { return v.toFixed(2) + " " + (cur || ""); }
  };
  const bigMoney = (v, cur) => {
    if (!v) return "";
    const u = [[1e12, "T"], [1e9, "B"], [1e6, "M"]].find(([x]) => v >= x) || [1, ""];
    return (cur && cur !== "USD" ? cur + " " : "$") + (v / u[0]).toFixed(v / u[0] >= 100 ? 0 : 1) + u[1];
  };

  /* ---------- theme ---------- */
  const ICONS = {
    auto: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2"><circle cx="12" cy="12" r="9"/><path d="M12 3a9 9 0 0 0 0 18z" fill="currentColor"/></svg>',
    light: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round"><circle cx="12" cy="12" r="4"/><path d="M12 2v2M12 20v2M4.9 4.9l1.4 1.4M17.7 17.7l1.4 1.4M2 12h2M20 12h2M4.9 19.1l1.4-1.4M17.7 6.3l1.4-1.4"/></svg>',
    dark: '<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linejoin="round"><path d="M21 12.8A9 9 0 1 1 11.2 3a7 7 0 0 0 9.8 9.8z"/></svg>',
  };
  function applyTheme(t) {
    if (t === "light" || t === "dark") document.documentElement.dataset.theme = t;
    else delete document.documentElement.dataset.theme;
    const b = $("#theme"); b.innerHTML = ICONS[t] || ICONS.auto; b.title = "Theme: " + (t || "auto");
  }
  let theme = store.get("theme") || "auto";
  applyTheme(theme);
  $("#theme").addEventListener("click", () => {
    theme = { auto: "light", light: "dark", dark: "auto" }[theme] || "auto";
    store.set("theme", theme); applyTheme(theme);
  });

  /* ---------- tabs ---------- */
  function showTab(id) {
    for (const t of document.querySelectorAll('[role="tab"]')) {
      const on = t.id === id; t.setAttribute("aria-selected", on);
      $("#" + t.getAttribute("aria-controls")).hidden = !on;
    }
    store.set("tab", id);
  }
  for (const t of document.querySelectorAll('[role="tab"]')) t.addEventListener("click", () => showTab(t.id));
  const hashTab = { "#stories": "tab-stories", "#sectors": "tab-perf", "#geo": "tab-geo" }[location.hash] || null;
  showTab(hashTab || store.get("tab") || "tab-perf");

  /* ---------- alerts ---------- */
  function renderAlerts(d) {
    const box = $("#alerts"); box.replaceChildren();
    const stale = [];
    const mAt = d.market && d.market.updated_at;
    const sAt = d.stories && d.stories.updated_at;
    if (!mAt || Date.now() - new Date(mAt) > DAY_MS) stale.push("sector numbers" + (mAt ? " (" + ago(mAt) + ")" : ""));
    if (!sAt || Date.now() - new Date(sAt) > DAY_MS) stale.push("top stories" + (sAt ? " (" + ago(sAt) + ")" : ""));
    if (stale.length) box.append(el("div", { class: "alert", role: "status" },
      el("strong", { text: "Data is over a day old: " }), stale.join(", ") +
      ". The scheduled refresh may have failed — check the repo's Actions tab."));
    if (d.notices && d.notices.length) box.append(el("div", { class: "alert", role: "status" },
      el("strong", { text: "Last refresh was partial." }),
      el("ul", null, d.notices.map(n => el("li", { text: n })))));
  }

  const mq = window.matchMedia("(max-width: 560px)");
  let lastData = null;

  /* ---------- AI research picks (inside a tapped sector row) ---------- */
  function picksBlock(list) {
    if (!list || !list.length) return null;
    const stat = (label, v, fmt) => v == null ? null : el("span", { class: "pill" }, el("b", { text: label }), fmt(v));
    return el("div", { class: "picks" },
      el("div", { class: "picks-h" }, el("span", { class: "lbl", text: "Long-term picks" }),
        el("span", { class: "muted small", text: "quality at a good price · 5+ year holds · ideas to research, not advice" })),
      list.map(p => el("div", { class: "pick" },
        el("div", { class: "pick-top" },
          el("a", { class: "pick-tk", href: "https://finance.yahoo.com/quote/" + encodeURIComponent(p.ticker),
            target: "_blank", rel: "noopener noreferrer", text: p.ticker, onclick: e => e.stopPropagation() }),
          el("span", { class: "pick-nm", text: p.name || "" }),
          el("span", { class: "badge hz", text: "Hold " + (p.horizon || "5+ years").replace("years", "yrs") }),
          p.angle ? el("span", { class: "badge angle", text: p.angle }) : null),
        el("div", { class: "pick-why", text: p.reason }),
        el("div", { class: "pick-stats" },
          p.y5 != null ? el("span", { class: "pill " + cls(p.y5) }, el("b", { text: "5Y" }), pct(p.y5, 0)) : null,
          stat("ROE", p.roe, v => v.toFixed(0) + "%"),
          stat("P/E", p.fwd_pe || p.pe, v => v.toFixed(0)),
          p.div_yield ? stat("Div", p.div_yield, v => v.toFixed(1) + "%") : null,
          p.off_high != null && p.off_high < -5 ? stat("vs 52w high", p.off_high, v => pct(v, 0)) : null,
          p.consensus ? el("span", { class: "badge " + ratingClass(p.consensus), text: p.consensus }) : null))));
  }

  /* ---------- performance tab ---------- */
  function heat(v, col) {
    if (v == null) return "";
    const a = Math.min(Math.abs(v) / SCALE[col], 1) * 0.42 + 0.05;
    return `background: rgba(var(${v >= 0 ? "--up" : "--down"}), ${a.toFixed(3)})`;
  }
  function renderPerf(d) {
    const m = d.market || { rows: [] };
    const reasons = (d.reasons && d.reasons.items) || {};
    const picks = (d.picks && d.picks.items) || {};
    const spy = m.rows.find(r => r.is_index) || {};
    const recap = $("#recap"); recap.replaceChildren(
      el("h2", { text: "Market recap" }),
      el("div", { class: "spx num" },
        el("span", null, el("b", { text: "S&P 500" }), " "),
        COLS.slice(0, 4).map(c => el("span", { class: "pill " + cls(spy[c]) },
          el("b", { text: { day: "Day", week: "Wk", month: "Mo", ytd: "YTD" }[c] }), pct(spy[c], 2)))),
      el("p", { text: (d.recap && d.recap.text) || "Recap not available yet." }));
    $("#asof").textContent = m.as_of_label ? m.as_of_label + (m.updated_at ? " · fetched " + fmtStamp(m.updated_at) : "") : "";
    const tb = $("#perf tbody"); tb.replaceChildren();
    for (const r of m.rows) {
      const reasonText = reasons[r.ticker] || "No explanation available for this refresh.";
      const reasonRow = el("tr", { class: "reason", hidden: true },
        el("td", { colspan: 7 }, el("p", { class: "why", text: reasonText }), picksBlock(picks[r.ticker])));
      const row = el("tr", {
        class: "row" + (r.is_index ? " index" : ""), tabindex: 0, "aria-expanded": "false",
        onclick: () => toggle(), onkeydown: e => { if (e.key === "Enter" || e.key === " ") { e.preventDefault(); toggle(); } },
      },
        el("td", { class: "name" }, el("span", { class: "chev", text: "▶" }),
          el("span", { class: "tk", "data-ticker": r.ticker, "data-short": SHORT[r.ticker] || r.ticker, text: r.ticker }), " ",
          el("span", { class: "nm", text: r.name })),
        COLS.map(c => el("td", { class: "cell", style: heat(r[c], c), title: pct(r[c], 2),
          text: mq.matches ? compact(r[c]) : pct(r[c], 1) })));
      function toggle() {
        const open = reasonRow.hidden; reasonRow.hidden = !open;
        row.classList.toggle("open", open); row.setAttribute("aria-expanded", open);
      }
      tb.append(row, reasonRow);
    }
    if (!m.rows.length) tb.append(el("tr", null, el("td", { colspan: 7, class: "empty", text: "No data yet." })));
    applyShortNames();
  }
  // phone: "+105" / "−4.3" without the % sign so all seven columns fit
  const compact = v => v == null ? "–" : (v > 0 ? "+" : v < 0 ? "−" : "") + Math.abs(v).toFixed(Math.abs(v) >= 9.95 ? 0 : 1);
  function applyShortNames() {
    for (const tk of document.querySelectorAll("#perf .tk")) {
      tk.textContent = mq.matches ? tk.dataset.short : tk.dataset.ticker;
    }
  }

  /* ---------- stories tab ---------- */
  let current = store.get("sector") || "ALL";
  function picture(s) {
    const box = el("div", { class: "pic" });
    const f = s.facts || {};
    const initials = () => {
      box.className = "pic"; box.replaceChildren(el("span", { class: "initials", text: (s.company || "?").replace(/[^A-Za-z0-9 ]/g, "").split(/\s+/).filter(w => w && !/^(inc|corp|corporation|co|company|plc|ltd|limited|sa|spa|ag|nv|se|group|holdings?|the)$/i.test(w)).slice(0, 2).map(w => w[0]).join("").toUpperCase() }));
    };
    const logo = () => {
      if (!f.logo) return initials();
      box.className = "pic logo";
      const img = el("img", { src: f.logo, alt: "", loading: "lazy", referrerpolicy: "no-referrer" });
      img.onerror = initials; box.replaceChildren(img);
    };
    if (s.image) {
      const img = el("img", { src: s.image, alt: "", loading: "lazy", referrerpolicy: "no-referrer" });
      img.onerror = logo;
      // treat tiny images (tracking pixels / favicons) as failures
      img.onload = () => { if (img.naturalWidth && img.naturalWidth < 120) logo(); };
      box.append(img);
    } else logo();
    return box;
  }
  function storyCard(s, i) {
    const f = s.facts || {}, g = s.growth || {}, c = f.consensus || {};
    const sym = s.symbol || s.ticker;
    const ratings = f.ratings || [];
    const pic = picture(s); pic.append(el("span", { class: "rank", text: "#" + (i + 1) }));
    const metaBits = [f.country || s.country, f.industry, bigMoney(f.market_cap, f.currency) && "Mkt cap " + bigMoney(f.market_cap, f.currency)].filter(Boolean);
    return el("article", { class: "card story" }, pic,
      el("div", { class: "body" },
        el("div", { class: "meta" }, el("span", { class: "co", text: s.company }), sym ? el("span", { text: sym }) : null,
        ),
        el("h3", { text: s.headline }),
        s.profile ? el("p", { class: "profile", text: s.profile }) : null,
        metaBits.length ? el("div", { class: "meta", text: metaBits.join(" · ") }) : null,
        el("p", null, el("span", { class: "lbl", text: "What happened" }), s.what_happened),
        el("p", null, el("span", { class: "lbl", text: "Why it matters" }), s.why_it_matters),
        g.y1 != null || g.w1 != null ? el("div", null, el("span", { class: "lbl", text: "Share price" + (sym ? " · " + sym : "") }),
          el("div", { class: "pills" }, [["1W", g.w1], ["1Y", g.y1], ["5Y", g.y5]].map(([k, v]) =>
            el("span", { class: "pill " + cls(v) }, el("b", { text: k }), pct(v, 1))))) : null,
        el("div", { class: "analysts" },
          el("div", { class: "cons" },
            el("span", null, el("span", { class: "lbl", text: "Analysts" })),
            el("span", null, c.rating ? el("span", { class: "badge " + ratingClass(c.rating), text: c.rating }) : "No consensus",
              c.analysts ? ` ${c.analysts} analysts` : "",
              c.target_mean ? ` · PT ${money(c.target_mean, f.currency)}` : "",
              c.upside != null ? ` (${pct(c.upside, 0)})` : "")),
          ratings.length ? el("div", { class: "rt" }, ratings.map(r => [
            el("span", { class: "firm", title: r.grade, text: r.firm }),
            el("span", { class: "badge " + r.rating, title: r.grade, text: r.rating }),
            el("span", { class: "pt" }, fmtDate(r.date) + (r.target ? " · " + money(r.target, f.ratings_ticker && f.ratings_ticker !== sym ? "USD" : f.currency) : "")),
          ])) : el("div", { class: "muted small", text: "No recent firm-level ratings on Yahoo Finance for this listing." }),
          ratings.length && f.ratings_ticker && f.ratings_ticker !== sym ? el("div", { class: "muted small", text: `Ratings from US listing ${f.ratings_ticker}.` }) : null),
        el("div", { class: "src" },
          s.url ? el("a", { href: s.url, target: "_blank", rel: "noopener noreferrer", text: (s.source || "Source") + " ↗" }) : el("span", { text: s.source || "" }),
          el("span", { class: "muted", text: fmtDate(s.published) }))));
  }
  function ratingClass(r) { return /sell|under/i.test(r) ? "sell" : /buy/i.test(r) ? "buy" : "hold"; }

  function renderStories(d) {
    const secs = (d.stories && d.stories.sectors) || {};
    const keys = Object.keys(secs);
    if (current !== "ALL" && !secs[current]) current = "ALL";
    const chips = $("#chips"); chips.replaceChildren();
    const chip = (k, label) => el("button", { class: "chip", type: "button", "aria-pressed": String(current === k),
      onclick: () => { current = k; store.set("sector", k); renderStories(d); } , text: label });
    chips.append(chip("ALL", "All sectors"), ...keys.map(k => chip(k, secs[k].name)));
    const out = $("#stories"); out.replaceChildren();
    const show = current === "ALL" ? keys : [current];
    for (const k of show) {
      const sec = secs[k];
      out.append(el("div", { class: "sector-head" }, el("h2", { text: sec.name }),
        el("span", { class: "muted small", text: (sec.stale ? "not refreshed · " : "") + (sec.updated_at ? "updated " + ago(sec.updated_at) : "") })));
      out.append(sec.items.length ? el("div", { class: "grid" }, sec.items.map((s, i) => storyCard(s, i)))
        : el("div", { class: "card empty", text: "No major verified stories for this sector right now." }));
    }
    if (!show.length) out.append(el("div", { class: "card empty", text: "No stories yet — they appear after the first refresh with a Gemini key." }));
  }

  /* ---------- geopolitics tab ---------- */
  const REL_CLASS = { Ally: "buy", Partner: "hold", Competitor: "warn", Rival: "sell" };
  const DIR = { positive: ["▲", "up", "Upside"], negative: ["▼", "down", "Risk"], mixed: ["◆", "mixed", "Could go either way"] };
  const flag = (code, big) => el("img", { class: "flag" + (big ? " big" : ""), src: "flags/" + code + ".svg", alt: "", width: big ? 40 : 28, height: big ? 30 : 21 });
  const usd = v => v == null ? null : (Math.abs(v) >= 1000 ? "$" + (v / 1000).toFixed(1) + "T" : "$" + Math.round(v) + "B");

  function swCols(prof, name) {
    return el("div", { class: "sw" },
      el("div", { class: "sw-col good" }, el("span", { class: "lbl", text: "Strengths" }),
        el("ul", null, (prof.strengths || []).map(x => el("li", { text: x })))),
      el("div", { class: "sw-col bad" }, el("span", { class: "lbl", text: "Weaknesses" }),
        el("ul", null, (prof.weaknesses || []).map(x => el("li", { text: x })))));
  }
  function weekLine(w) {
    if (!w || !w.line) return null;
    return el("div", { class: "geo-week" }, el("span", { class: "lbl", text: "This week" }), " ", w.line,
      (w.links || []).length ? el("span", { class: "geo-links" }, w.links.map(l =>
        el("a", { href: l.url, target: "_blank", rel: "noopener noreferrer", title: l.title, text: l.publisher + " ↗",
          onclick: e => e.stopPropagation() }))) : null);
  }
  function renderGeo(g) {
    const meta = $("#geo-meta");
    if (!g || !g.players) { meta.textContent = "Geopolitics data not available yet."; return; }
    const da = g.data_as_of || {};
    meta.textContent = [g.profiles_updated_at && "Profiles updated " + fmtDate(g.profiles_updated_at),
      g.week_updated_at && "this-week lines " + ago(g.week_updated_at),
      da.trade && "trade " + da.trade, da.treasuries && "Treasuries " + da.treasuries, da.gdp && "GDP " + da.gdp]
      .filter(Boolean).join(" · ");
    const us = g.us;
    $("#geo-us").replaceChildren(us && us.profile ? el("article", { class: "card geo-card geo-home" },
      el("div", { class: "geo-head" }, flag("us", true),
        el("div", null, el("h3", null, "United States ", el("span", { class: "badge hz", text: "Home base" })),
          el("p", { class: "muted small", text: us.gdp_b ? "GDP " + usd(us.gdp_b) : "" }))),
      el("p", { class: "self", text: us.profile.self_view }),
      swCols(us.profile), weekLine(us.week)) : null);
    const list = $("#geo-list"); list.replaceChildren();
    for (const p of g.players) {
      const pr = p.profile || {}, n = p.numbers || {};
      const body = el("div", { class: "geo-body", hidden: true },
        el("div", { class: "pills" }, [
          ["US trade", usd(n.trade_b)], ["US balance", n.balance_b == null ? null : (n.balance_b >= 0 ? "+" : "−") + usd(Math.abs(n.balance_b))],
          ["Holds Treasuries", usd(n.treasuries_b)], ["GDP", usd(n.gdp_b)]].filter(x => x[1]).map(([k, v]) =>
          el("span", { class: "pill" }, el("b", { text: k }), v))),
        pr.self_view ? el("div", null, el("span", { class: "lbl", text: "How " + p.name + " sees itself" }),
          el("p", { class: "self", text: pr.self_view })) : null,
        pr.strengths ? swCols(pr) : el("p", { class: "muted", text: "Profile not available yet." }),
        (pr.impacts || []).length ? el("div", { class: "impacts" },
          el("span", { class: "lbl", text: "What it means for the US · next 5–10 years" }),
          pr.impacts.map(im => { const d = DIR[im.direction] || DIR.mixed; return el("div", { class: "impact " + d[1] },
            el("span", { class: "imp-ico", title: d[2], text: d[0] }),
            el("div", null, el("b", { text: im.title }), el("p", { text: im.detail }),
              (im.sectors || []).length ? el("div", { class: "imp-sec" }, im.sectors.map(sx =>
                el("span", { class: "chip-s", title: SHORT[sx] || sx, text: sx + " · " + (SHORT[sx] || "") }))) : null)); })) : null,
        p.profile_as_of ? el("p", { class: "muted small", text: "Profile as of " + fmtDate(p.profile_as_of) }) : null);
      const head = el("button", { class: "geo-row", type: "button", "aria-expanded": "false",
        onclick: () => { const open = body.hidden; body.hidden = !open; head.setAttribute("aria-expanded", open); card.classList.toggle("open", open); } },
        el("span", { class: "geo-rank", text: "#" + p.rank }), flag(p.code),
        el("span", { class: "geo-name" }, el("b", { text: p.name }), pr.lever ? el("span", { class: "muted small", text: pr.lever }) : null),
        pr.relation ? el("span", { class: "badge " + (REL_CLASS[pr.relation] || "hold"), text: pr.relation }) : null,
        el("span", { class: "chev", text: "▶" }));
      const card = el("article", { class: "card geo-card" }, head, weekLine(p.week), body);
      list.append(card);
    }
    if ((g.notices || []).length) $("#alerts").append(el("div", { class: "alert", role: "status" },
      el("strong", { text: "Geopolitics: " }), g.notices.join(" ")));
  }

  /* ---------- load ---------- */
  async function load() {
    try {
      const r = await fetch("data.json?t=" + Date.now(), { cache: "no-store" });
      if (!r.ok) throw new Error("HTTP " + r.status);
      const d = await r.json();
      $("#stamp").textContent = "Updated " + fmtStamp(d.generated_at);
      $("#stamp").title = d.generated_at + (d.model ? " · " + d.model : "");
      renderAlerts(d); renderPerf(d); renderStories(d);
      fetch("geo.json?t=" + Date.now(), { cache: "no-store" }).then(x => x.ok ? x.json() : null)
        .then(renderGeo).catch(() => renderGeo(null));
      lastData = d;
      mq.addEventListener("change", () => renderPerf(lastData));
    } catch (e) {
      $("#stamp").textContent = "Could not load data";
      $("#alerts").replaceChildren(el("div", { class: "alert", text: "Could not load data.json (" + e.message + ")." }));
    }
  }
  load();
})();
