"""Global market snapshot from Yahoo Finance (via yfinance)."""
from __future__ import annotations

import logging
from dataclasses import dataclass

logging.getLogger("yfinance").setLevel(logging.CRITICAL)


@dataclass
class Quote:
    symbol: str
    name: str
    group: str
    price: float | None
    change_pct: float | None
    as_of: str = ""
    prev: float | None = None

    @property
    def ok(self) -> bool:
        return self.price is not None and self.change_pct is not None


def fetch_quotes(instruments: list[dict]) -> list[Quote]:
    import yfinance as yf  # imported lazily: it is slow to import

    symbols = [i["symbol"] for i in instruments]
    quotes: list[Quote] = []
    try:
        data = yf.download(
            symbols, period="7d", interval="1d", group_by="ticker",
            auto_adjust=False, progress=False, threads=True,
        )
    except Exception:
        data = None

    for inst in instruments:
        sym = inst["symbol"]
        price = change = prev = None
        as_of = ""
        try:
            closes = data[sym]["Close"].dropna() if data is not None else []
            if len(closes) >= 2:
                price = float(closes.iloc[-1])
                prev = float(closes.iloc[-2])
                change = (price / prev - 1) * 100 if prev else None
                as_of = str(closes.index[-1].date())
        except Exception:
            pass
        quotes.append(Quote(sym, inst["name"], inst.get("group", ""), price, change, as_of, prev))
    return quotes


def biggest_movers(quotes: list[Quote], n: int = 3) -> list[Quote]:
    ok = [q for q in quotes if q.ok and q.symbol != "^TNX"]
    return sorted(ok, key=lambda q: abs(q.change_pct or 0), reverse=True)[:n]


def format_quote(q: Quote) -> str:
    if not q.ok:
        return f"{q.name}: unavailable"
    if q.symbol == "^TNX":
        bps = (q.price - q.prev) * 100 if q.prev is not None else 0.0
        return f"{q.name}: {q.price:.2f}% ({bps:+.0f} bp)"
    if q.price >= 1000:
        price = f"{q.price:,.0f}"
    elif q.price >= 10:
        price = f"{q.price:,.2f}"
    else:
        price = f"{q.price:.4f}"
    return f"{q.name}: {price} ({q.change_pct:+.2f}%)"
