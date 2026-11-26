"""The Insights engine: Daily Brief newsletter, market headlines from renowned
sources, an earnings calendar for your holdings, and a transparent small-cap
"Rising Stars" screener.

Sources (all free, no key required):
  * Headlines — RSS from CNBC, MarketWatch (Dow Jones) and Yahoo Finance,
    de-duplicated and interleaved. If a NEWSAPI_KEY env var is set, NewsAPI
    top business headlines are blended in as a fourth source (optional).
  * Earnings — upcoming earnings dates per holding via yfinance.
  * Rising Stars — Yahoo's small-cap screeners (small_cap_gainers +
    aggressive_small_caps + growth_technology_stocks), enriched and re-scored.
    The scoring method is returned alongside the results so the UI can show
    exactly how picks are ranked — screeners are a starting point for research,
    NOT advice, and the UI says so.

Everything is cached and fails soft: a dead feed or endpoint never breaks the app.
"""

from __future__ import annotations

import os
import re
import urllib.request
from concurrent.futures import ThreadPoolExecutor
import xml.etree.ElementTree as ET
from datetime import date, datetime, timezone

from . import cache

_UA = {"User-Agent": "Mozilla/5.0 (StockOverview dashboard)"}

RSS_FEEDS = [
    ("CNBC", "https://www.cnbc.com/id/100003114/device/rss/rss.html"),
    ("MarketWatch", "https://feeds.content.dowjones.io/public/rss/mw_topstories"),
    ("Yahoo Finance", "https://finance.yahoo.com/news/rssindex"),
]

DATA = False  # market data only: uploads don't invalidate it


def _cached(key: str, ttl: int, fn, data: bool = DATA):
    """Fresh for `ttl`, then served while one background refresh runs; a failed
    refresh keeps the last value (backend/cache.py). None if never computed."""
    def build():
        val = fn()
        if val is None:
            raise ValueError("no value")
        return val
    try:
        return cache.get(f"{__name__}.{key}", build, ttl=ttl, stale=4 * ttl, data=data)
    except Exception:
        return None


# ---------------------------------------------------------------- headlines
def _fetch_rss(name: str, url: str, limit: int = 10) -> list[dict]:
    req = urllib.request.Request(url, headers=_UA)
    root = ET.fromstring(urllib.request.urlopen(req, timeout=10).read())
    items = []
    for it in root.findall(".//item")[:limit]:
        title = (it.findtext("title") or "").strip()
        if not title:
            continue
        items.append({
            "title": title,
            "url": (it.findtext("link") or "").strip(),
            "source": name,
            "published": (it.findtext("pubDate") or "").strip(),
        })
    return items


def _newsapi(limit: int = 8) -> list[dict]:
    key = os.getenv("NEWSAPI_KEY")
    if not key:
        return []
    import json
    url = ("https://newsapi.org/v2/top-headlines?category=business&language=en"
           f"&pageSize={limit}&apiKey={key}")
    data = json.load(urllib.request.urlopen(
        urllib.request.Request(url, headers=_UA), timeout=10))
    return [{"title": a["title"], "url": a.get("url", ""),
             "source": a.get("source", {}).get("name", "NewsAPI"), "published": a.get("publishedAt", "")}
            for a in data.get("articles", []) if a.get("title")]


def _try_rss(feed: tuple[str, str]) -> list[dict]:
    try:
        return _fetch_rss(*feed)
    except Exception:
        return []


def headlines() -> list[dict]:
    def build():
        # the feeds at the same time: a slow one costs its timeout once, not per feed
        with ThreadPoolExecutor(max_workers=len(RSS_FEEDS)) as pool:
            per_source = [items for items in pool.map(_try_rss, RSS_FEEDS) if items]
        extra = []
        try:
            extra = _newsapi()
        except Exception:
            pass
        if extra:
            per_source.append(extra)
        # interleave sources, de-duplicate near-identical titles
        out, seen = [], set()
        for i in range(10):
            for src in per_source:
                if i < len(src):
                    key = re.sub(r"\W+", "", src[i]["title"].lower())[:60]
                    if key not in seen:
                        seen.add(key)
                        out.append(src[i])
        if not out and cache.peek(f"{__name__}.headlines")[0]:
            return None            # every feed down: keep the last headlines
        return out[:18]
    return _cached("headlines", 900, build) or []


# ---------------------------------------------------------------- earnings
def earnings_calendar(tickers: list[str]) -> list[dict]:
    def build():
        import yfinance as yf
        today = datetime.now(timezone.utc)

        def one(t: str) -> dict | None:
            try:
                ed = yf.Ticker(t).get_earnings_dates(limit=8)
                future = [i for i in ed.index if i.to_pydatetime() >= today]
                if future:
                    nxt = min(future)
                    return {"ticker": t, "date": str(nxt.date()),
                            "days_away": (nxt.to_pydatetime() - today).days}
            except Exception:
                pass
            return None

        symbols = sorted(set(x for x in tickers if x))
        with ThreadPoolExecutor(max_workers=4) as pool:  # a few at a time: this endpoint needs a crumb
            out = [r for r in pool.map(one, symbols) if r]
        out.sort(key=lambda x: x["date"])
        return out
    return _cached("earnings:" + ",".join(sorted(set(tickers)))[:120], 6 * 3600, build) or []


# ---------------------------------------------------------------- rising stars
SCREENER_METHOD = (
    "Candidates come from Yahoo Finance's public screeners (small-cap gainers, "
    "aggressive small caps, growth technology). Each is re-scored 0–100 here: "
    "40% price momentum (day's gain, capped), 30% valuation sanity (P/E between "
    "2 and 40 scores higher; missing P/E scores neutral), 30% liquidity/size "
    "(market cap €300M–€10B preferred — small but not micro-cap). "
    "This is a research starting point, not investment advice."
)


def _score(q: dict) -> float:
    pct = q.get("regularMarketChangePercent") or 0.0
    momentum = max(0.0, min(pct, 10.0)) / 10.0          # 0..1, capped at +10%
    pe = q.get("trailingPE")
    if pe is None:
        valuation = 0.5
    elif 2 <= pe <= 40:
        valuation = 1.0 - abs(pe - 18) / 40              # best around ~18
    else:
        valuation = 0.15
    mc = q.get("marketCap") or 0
    if 3e8 <= mc <= 1e10:
        size = 1.0
    elif mc > 1e10:
        size = 0.4
    else:
        size = 0.25
    return round((0.4 * momentum + 0.3 * valuation + 0.3 * size) * 100, 1)


def rising_stars() -> dict:
    def build():
        import yfinance as yf
        quotes: dict[str, dict] = {}
        for screener in ("small_cap_gainers", "aggressive_small_caps",
                         "growth_technology_stocks"):
            try:
                for q in yf.screen(screener, count=15).get("quotes", []):
                    sym = q.get("symbol")
                    if sym and sym not in quotes:
                        q["_screener"] = screener
                        quotes[sym] = q
            except Exception:
                continue
        rows = []
        for q in quotes.values():
            mc = q.get("marketCap") or 0
            if mc > 5e10:  # keep it about *smaller* companies
                continue
            rows.append({
                "symbol": q.get("symbol"),
                "name": (q.get("shortName") or q.get("longName") or "")[:40],
                "price": q.get("regularMarketPrice"),
                "change_pct": round(q.get("regularMarketChangePercent") or 0, 2),
                "market_cap": mc,
                "pe": round(q["trailingPE"], 1) if q.get("trailingPE") else None,
                "from_screener": q["_screener"].replace("_", " "),
                "score": _score(q),
            })
        rows.sort(key=lambda r: -r["score"])
        return {"as_of": date.today().isoformat(), "method": SCREENER_METHOD,
                "picks": rows[:10]}
    return _cached("rising_stars", 3600, build) or {"as_of": None, "method": SCREENER_METHOD, "picks": []}


# ---------------------------------------------------------------- daily brief
def daily_brief(portfolio: dict, analytics_data: dict, ccy: str = "EUR") -> dict:
    """A generated 'newsletter' combining your portfolio with market context.
    Amounts are written in `ccy` (EUR, GBP or USD)."""
    from .fx import from_eur

    sym = {"EUR": "€", "GBP": "£", "USD": "$"}.get(ccy, "€")

    def m(v: float) -> str:
        return f"{sym}{abs(from_eur(v, ccy)):,.0f}"

    t = portfolio["totals"]
    holdings = portfolio["holdings"]
    movers = [h for h in holdings if h.get("daily_eur") is not None]
    movers.sort(key=lambda h: h["daily_eur"], reverse=True)

    paras = []
    d = t.get("daily")
    if d is not None:
        word = "up" if d >= 0 else "down"
        paras.append(
            f"Your portfolio is {word} {abs(t['daily_pct']):.2f}% today "
            f"({'+' if d >= 0 else '−'}{m(d)}), at {m(t['value'])}.")
        if movers:
            best, worst = movers[0], movers[-1]
            if best["daily_eur"] > 0:
                paras.append(f"Biggest driver: {best['name']} "
                             f"({'+' if best['daily_pct']>=0 else ''}{best['daily_pct']:.1f}%).")
            if worst["daily_eur"] < 0:
                paras.append(f"Biggest drag: {worst['name']} ({worst['daily_pct']:.1f}%).")
    if t.get("total_return") is not None:   # None: the income could not be read just now
        # the parts add up to the total: total_return = all_time + income + fees
        fees = t.get("fees") or 0.0
        parts = (f"{'−' if t['all_time'] < 0 else ''}{m(t['all_time'])} capital gains "
                 f"{'−' if t['income'] < 0 else '+'} {m(t['income'])} dividends & interest")
        if round(from_eur(fees, ccy)):
            parts += f" {'−' if fees < 0 else '+'} {m(fees)} fees"
        paras.append(
            f"All-time total return stands at {'−' if t['total_return'] < 0 else ''}{m(t['total_return'])} "
            f"({parts}).")
    beta = analytics_data.get("beta")
    vol = analytics_data.get("volatility_pct")
    if beta is not None and vol is not None:
        stance = "more volatile than" if beta > 1.1 else ("less volatile than" if beta < 0.9 else "moving broadly with")
        paras.append(f"Risk check: beta {beta} — your portfolio is {stance} the market "
                     f"— with {vol}% annualized volatility.")
    top3 = analytics_data.get("top3_pct")
    if top3 and top3 > 50:
        paras.append(f"Concentration note: your top 3 positions are {top3}% of the portfolio.")

    return {
        "date": date.today().strftime("%A %d %B %Y"),
        "summary": paras,
        "headlines": _headlines_now()[:8],
    }


def _headlines_now() -> list[dict]:
    """The headlines when they were fetched before; else none for now (and
    they are fetched in the background), so the brief never waits on a feed."""
    if cache.peek(f"{__name__}.headlines")[0]:
        return headlines()
    import threading
    threading.Thread(target=headlines, name="headlines", daemon=True).start()
    return []
