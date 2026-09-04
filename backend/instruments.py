"""ISIN -> market-data ticker, shared by every connector and importer.

One table instead of three: the DeGiro connector (open positions), the backfill
(every instrument ever traded) and the Trade Republic importer all need the same
answer to "which yfinance symbol prices this ISIN?". "" means no usable quote
(delisted SPACs, penny stocks) — such positions are shown but not priced.

Convention: US listings price in USD; ".AS" (Euronext Amsterdam), ".DE" (Xetra),
".PA", ".MI" in EUR; ".L" in GBP (see `price_currency`).
"""

from __future__ import annotations

TICKERS: dict[str, str] = {
    # --- equities ---
    "US0378331005": "AAPL",     "US5949181045": "MSFT",     "US02079K3059": "GOOGL",
    "US0231351067": "AMZN",     "US30303M1027": "META",     "US67066G1040": "NVDA",
    "US88160R1014": "TSLA",     "US64110L1061": "NFLX",     "US00724F1012": "ADBE",
    "US68389X1054": "ORCL",     "US79466L3024": "CRM",      "US6541061031": "NKE",
    "US86800U3023": "SMCI",     "US0079031078": "AMD",      "US4581401001": "INTC",
    "US69608A1088": "PLTR",     "US6311031081": "NDAQ",     "US5801351017": "MCD",
    "US91324P1021": "UNH",      "US70450Y1038": "PYPL",     "US60770K1079": "MRNA",
    "US09075V1026": "BNTX",     "US62914V1061": "NIO",      "US98422D1054": "XPEV",
    "US76954A1034": "RIVN",     "US83406F1021": "SOFI",     "US22041X1028": "CRSR",
    "US83304A1060": "SNAP",     "US35953D1046": "FUBO",     "US46222L1089": "IONQ",
    "US4884452065": "ZVRA",     "US21077C1071": "LOGC",     "US21077C3051": "LOGC",
    "US36315X1019": "LKFT",     # Lakefront Biotherapeutics (ex-Galapagos, renamed 2026)
    "US30231G1022": "XOM",      # Exxon Mobil Corp (old ISIN, until 2026-07)
    "US30233Q1085": "XOM",      # ExxonMobil Holdings Corp (new ISIN after 2026-07 reorganisation)
    "US6745991058": "OXY",
    "NL0010273215": "ASML.AS",  "USN070592100": "ASML",     "NL0011585146": "RACE",
    "NL0012969182": "ADYEN.AS", "NL0011872650": "BFIT.AS",  "NL0000888691": "AMG.AS",
    "NL0000334118": "ASM.AS",   "NL0012866412": "BESI.AS",  "NL0000009165": "HEIA.AS",
    "GB00B03MLX29": "SHELL.AS", "GB00BP6MXD84": "SHEL",     "FR001400J770": "AF.PA",
    # --- ETFs / ETCs (priced on the EUR listing Trade Republic / DeGiro trade) ---
    "IE00BZCQB185": "QDV5.DE",  # iShares MSCI India UCITS ETF USD (Acc)
    "IE00BJ38QD84": "ZPRR.DE",  # SPDR Russell 2000 US Small Cap UCITS ETF
    "IE00B5BMR087": "SXR8.DE",  # iShares Core S&P 500 UCITS ETF USD (Acc)
    "IE00B4ND3602": "PPFB.DE",  # iShares Physical Gold ETC
    # --- no usable market data (delisted / worthless) ---
    "US34619R1023": "", "US33813J1060": "", "US84677R1068": "", "US74374T1097": "",
    "US44951Y1029": "", "US44951Y2019": "", "US01671P1003": "",
}

# Human-readable names where the broker's own label is truncated or cryptic
# (Trading 212 statements only carry the ticker symbol).
NAMES: dict[str, str] = {
    "US0231351067": "Amazon.com Inc", "USN070592100": "ASML Holding (ADR)",
    "NL0010273215": "ASML Holding", "NL0012866412": "BE Semiconductor Industries",
    "US02079K3059": "Alphabet Inc Class A", "US30303M1027": "Meta Platforms Inc",
    "US6311031081": "Nasdaq Inc", "NL0000009165": "Heineken", "US60770K1079": "Moderna Inc",
    "US70450Y1038": "PayPal Holdings", "US0378331005": "Apple Inc", "US5949181045": "Microsoft Corp",
    "US68389X1054": "Oracle Corp", "US79466L3024": "Salesforce Inc", "US6541061031": "Nike Inc Class B",
    "US00724F1012": "Adobe Inc", "US86800U3023": "Super Micro Computer",
    "IE00B4ND3602": "iShares Physical Gold ETC",
    "IE00B5BMR087": "iShares Core S&P 500 UCITS ETF (Acc)",
    "IE00BZCQB185": "iShares MSCI India UCITS ETF (Acc)",
    "IE00BJ38QD84": "SPDR Russell 2000 US Small Cap UCITS ETF",
    "US30233Q1085": "ExxonMobil Holdings Corp",
}


def ticker_for(isin: str) -> str:
    return TICKERS.get(isin, "")


def price_currency(ticker: str) -> str:
    """Currency a yfinance ticker is quoted in, from its exchange suffix."""
    if not ticker:
        return "EUR"
    if ticker.endswith((".AS", ".DE", ".F", ".PA", ".MI", ".BR")):
        return "EUR"
    if ticker.endswith(".L"):
        return "GBP"
    if ticker.endswith(".SW"):
        return "CHF"
    return "USD"


def is_valid_isin(isin: str) -> bool:
    """ISO 6166 check digit (Luhn over the digit expansion)."""
    if len(isin) != 12 or not isin[:2].isalpha() or not isin[-1].isdigit():
        return False
    digits = "".join(str(int(c, 36)) for c in isin[:-1])
    total, double = 0, True
    for ch in reversed(digits):
        d = int(ch) * (2 if double else 1)
        total += d // 10 + d % 10
        double = not double
    return (10 - total % 10) % 10 == int(isin[-1])
