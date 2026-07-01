"""Common shape every broker connector returns.

A connector's only job is to answer "what do I own?" -- ticker, quantity and
the price you paid. The current market price is optional: a broker like
Trading 212 already knows it (so it fills it in), while manually-imported
holdings leave it as None and let the price layer fetch it from yfinance.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass
class Holding:
    broker: str            # "Trading212", "DeGiro", "Trade Republic"
    ticker: str            # market-data ticker, e.g. "ASML.AS", "AAPL"
    name: str
    quantity: float
    buy_price: float       # average price paid per share, in `buy_currency`
    currency: str = "EUR"  # currency the live price is quoted in
    isin: str = ""         # stable cross-broker identity, for merging duplicates
    price: float | None = None   # live price if the connector already knows it
    buy_currency: str | None = None  # currency of buy_price; defaults to `currency`
    # Exact unrealized P/L in EUR if the broker reports it (Trading 212 does, via
    # ppl+fxPpl). When set, it's used verbatim instead of value-minus-cost, which
    # avoids losing the FX-at-purchase component.
    broker_profit_eur: float | None = None

    def cost_currency(self) -> str:
        return self.buy_currency or self.currency
