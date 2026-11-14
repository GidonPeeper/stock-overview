"""Interactive Brokers -- Flex Web Service (read-only reports).

A Flex token can only download reports the account holder defined; it can
never trade or move money. Setup (IBKR Portal, Performance & Reports -> Flex
Queries): make an Activity Flex Query with Open Positions, Trades, Cash
Transactions and Cash Report, format XML, period "Last 365 Calendar Days";
then switch on Flex Web Service and generate a token.

Two calls, both GET, both need a User-Agent header:

    {BASE}/SendRequest?t=TOKEN&q=QUERY_ID&v=3   -> <FlexStatementResponse>
        <Status>Success</Status><ReferenceCode>...</ReferenceCode>
    {BASE}/GetStatement?t=TOKEN&q=REFERENCE&v=3 -> <FlexQueryResponse> (the report),
        or <FlexStatementResponse> with ErrorCode 1019 "Statement generation in
        progress" while it is being built: wait and ask again.

Limits: 1 request per second, 10 per minute per token (error 1018). The report
is end-of-day data, so it is fetched at most every few hours.

Docs: https://www.ibkrguides.com/clientportal/performanceandstatements/flex3.htm
      https://www.ibkrguides.com/clientportal/performanceandstatements/flex3error.htm

Mapping (amounts stay in each row's own currency; the app converts to EUR):
  OpenPosition  -> Holding: position, positionValue, costBasisMoney, currency
  Trade         -> Trade:   buySell, quantity, |netCash| (fees included)
  CashTransaction "Dividends" / "Payment In Lieu Of Dividends" / "Withholding
                  Tax" -> dividends (net); "Broker/Bond Interest Received" -> interest
  CashReportCurrency endingCash per currency -> cash
"""

from __future__ import annotations

import logging
import os
import re
import time
import xml.etree.ElementTree as ET

from .base import Holding
from .live import ConnectorError, Snapshot, num, request
from ..instruments import TICKERS
from ..positions import Trade

NAME = "Interactive Brokers"
BASE_URL = os.getenv("IBKR_FLEX_URL",
                     "https://ndcdyn.interactivebrokers.com/AccountManagement/FlexWebService")
_HEADERS = {"User-Agent": "stock-overview/1.0 Python/3"}

# Codes that mean "not ready yet, ask again shortly".
RETRY_CODES = {"1001", "1004", "1005", "1006", "1007", "1008", "1009", "1018", "1019", "1021"}
MESSAGES = {
    "1003": "IBKR says the statement is not available.",
    "1010": "That is a legacy Flex Query. Make a new Activity Flex Query.",
    "1011": "Flex Web Service is switched off. Turn it on under Flex Web Service Configuration.",
    "1012": "The token has expired. Generate a new one under Flex Web Service Configuration.",
    "1013": "The token is limited to another IP address. Generate one with the IP field empty.",
    "1014": "IBKR does not recognise that Query ID. Check the number next to your Flex Query.",
    "1015": "IBKR says the token is not valid. Copy it again or generate a new one.",
    "1016": "IBKR says the account is not valid for this query.",
    "1017": "IBKR lost the report reference. It will try again.",
    "1018": "Too many requests for this token. It will try again in a minute.",
    "1019": "IBKR is still preparing the report. It will be fetched shortly.",
    "1020": "IBKR could not check the request. Check the token and the Query ID.",
}

# Waits between GetStatement attempts while IBKR builds the report.
_DELAYS = (2, 3, 5, 5, 8, 10, 12)

# Derivatives and FX are left out: their value needs multipliers and margin.
_SKIP_ASSETS = {"OPT", "FUT", "FOP", "WAR", "CFD", "FXCFD", "CASH", "FSFOP", "FSOPT", "IOPT"}

# IBKR listing exchange -> Yahoo Finance suffix (live prices come from yfinance).
_US = {"NASDAQ", "NYSE", "ARCA", "AMEX", "BATS", "NYSEARCA", "PINK", "IEX", "OTC", "BYX", "BEX"}
_SUFFIX = {
    "LSE": ".L", "LSEETF": ".L", "LSEIOB1": ".L", "AEB": ".AS", "IBIS": ".DE", "IBIS2": ".DE",
    "XETRA": ".DE", "FWB": ".F", "FWB2": ".F", "SBF": ".PA", "BVME": ".MI", "BVME.ETF": ".MI",
    "EBS": ".SW", "SWX": ".SW", "ENEXT.BE": ".BR", "BM": ".MC", "TSE": ".TO", "VENTURE": ".V",
    "SEHK": ".HK", "ASX": ".AX", "TSEJ": ".T", "SFB": ".ST", "OSE": ".OL", "KFB": ".CO",
    "CPH": ".CO", "WSE": ".WA", "VSE": ".VI", "BVL": ".LS", "ISED": ".IR", "HEX": ".HE",
}


class _RedactToken(logging.Filter):
    """urllib3 logs request lines (with the query string) at DEBUG level; the
    Flex token travels in the query string, so blank it out of any such line."""
    _RE = re.compile(r"([?&]t=)[^&\s\"']+")

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.args, tuple):
            record.args = tuple(self._RE.sub(r"\1***", a) if isinstance(a, str) else a
                                for a in record.args)
        if isinstance(record.msg, str):
            record.msg = self._RE.sub(r"\1***", record.msg)
        return True


logging.getLogger("urllib3.connectionpool").addFilter(_RedactToken())


# ------------------------------------------------------------------ HTTP
def _call(endpoint: str, token: str, q: str) -> ET.Element:
    resp = request(NAME, f"{BASE_URL}/{endpoint}", headers=_HEADERS,
                   params={"t": token, "q": q, "v": "3"}, timeout=(5, 60))
    try:
        return ET.fromstring(resp.content)
    except ET.ParseError:
        raise ConnectorError("IBKR sent a report this app could not read. "
                             "Check the Flex Query format is XML.") from None


def _fail(root: ET.Element) -> ConnectorError:
    code = (root.findtext("ErrorCode") or "").strip()
    msg = MESSAGES.get(code) or f"IBKR error {code or '?'}: {(root.findtext('ErrorMessage') or '').strip()}"
    return ConnectorError(msg, status=code, pending=code in RETRY_CODES)


def download(token: str, query_id: str, *, max_wait: float = 45.0, sleep=time.sleep) -> ET.Element:
    """The FlexQueryResponse element for `query_id` (two-step fetch)."""
    root = _call("SendRequest", token, query_id)
    if root.tag != "FlexStatementResponse":
        raise ConnectorError("IBKR answered in an unexpected way. Check the token and Query ID.")
    if (root.findtext("Status") or "").strip() != "Success":
        raise _fail(root)
    ref = (root.findtext("ReferenceCode") or "").strip()
    if not ref:
        raise ConnectorError("IBKR did not return a report reference. Try again later.")
    waited = 0.0
    for delay in (0,) + _DELAYS:
        if delay:
            if waited + delay > max_wait:
                break
            sleep(delay)
            waited += delay
        root = _call("GetStatement", token, ref)
        if root.tag == "FlexQueryResponse":
            return root
        err = _fail(root)
        if not err.pending:
            raise err
    raise ConnectorError(MESSAGES["1019"], status="1019", pending=True)


# ---------------------------------------------------------------- parsing
def _date(s: str | None) -> str:
    """IBKR dates -> ISO. Handles the default yyyyMMdd;HHmmss and the other
    common Flex date settings (yyyy-MM-dd, MM/dd/yyyy, with or without time)."""
    s = (s or "").strip()
    if not s:
        return ""
    m = re.match(r"(\d{4})-?(\d{2})-?(\d{2})", s)
    if m:
        day = f"{m.group(1)}-{m.group(2)}-{m.group(3)}"
        rest = s[m.end():]
    else:
        m = re.match(r"(\d{2})/(\d{2})/(\d{4})", s)
        if not m:
            return ""
        day = f"{m.group(3)}-{m.group(1)}-{m.group(2)}"
        rest = s[m.end():]
    t = re.search(r"(\d{2}):?(\d{2}):?(\d{2})", rest)
    return f"{day} {t.group(1)}:{t.group(2)}:{t.group(3)}" if t else day


def yf_ticker(symbol: str, exchange: str, currency: str, isin: str = "") -> str:
    """Best-effort Yahoo Finance ticker; "" if unknown (then the report's own
    price is used instead of a live one)."""
    if isin and TICKERS.get(isin):
        return TICKERS[isin]
    sym = (symbol or "").strip().rstrip(".")
    if not sym:
        return ""
    exch = (exchange or "").upper()
    if exch in _US or (not exch and currency == "USD"):
        return sym.replace(" ", "-").replace(".", "-")
    suffix = _SUFFIX.get(exch)
    return sym.replace(" ", "-") + suffix if suffix else ""


def _rows(parent: ET.Element | None, tag: str, prefer: tuple[str, ...]) -> list[ET.Element]:
    """Rows of one section at a single level of detail (summary rows if the
    query also has lot rows, so nothing is counted twice)."""
    if parent is None:
        return []
    rows = parent.findall(tag)
    for level in prefer:
        chosen = [r for r in rows if (r.get("levelOfDetail") or "").upper() == level]
        if chosen:
            return chosen
    return [r for r in rows if not r.get("levelOfDetail")]


def parse(root: ET.Element, label: str = "IBKR") -> Snapshot:
    snap = Snapshot()
    to_date = ""
    for st in root.iter("FlexStatement"):
        to_date = max(to_date, _date(st.get("toDate")))
        info = st.find("AccountInformation")
        base_ccy = (info.get("currency") if info is not None else "") or ""

        for p in _rows(st.find("OpenPositions"), "OpenPosition", ("SUMMARY", "LOT")):
            if (p.get("assetCategory") or "STK").upper() in _SKIP_ASSETS:
                continue
            qty = num(p.get("position"))
            if qty <= 0:
                continue  # short positions are left out
            ccy = (p.get("currency") or base_ccy or "USD").upper()
            isin = p.get("isin") or ""
            ticker = yf_ticker(p.get("symbol"), p.get("listingExchange"), ccy, isin)
            value = num(p.get("positionValue"), qty * num(p.get("markPrice")))
            cost = num(p.get("costBasisMoney"), qty * num(p.get("costBasisPrice")))
            snap.holdings.append(Holding(
                broker=label, ticker=ticker, name=p.get("description") or p.get("symbol") or ticker,
                quantity=qty, buy_price=round(cost / qty, 6), currency=ccy, isin=isin,
                # with a ticker the live price is fetched; otherwise use the report's
                price=None if ticker else round(value / qty, 6), buy_currency=ccy,
            ))

        trades = st.find("Trades")
        rows = _rows(trades, "Trade", ("EXECUTION",)) or _rows(trades, "Order", ("ORDER",))
        for t in rows:
            if (t.get("assetCategory") or "STK").upper() in _SKIP_ASSETS:
                continue
            side = (t.get("buySell") or "").upper()
            if side not in ("BUY", "SELL"):
                continue  # cancellations ("BUY (Ca.)") and other bookings
            qty = abs(num(t.get("quantity")))
            net = num(t.get("netCash"), num(t.get("proceeds")) + num(t.get("ibCommission")))
            if not qty:
                continue
            isin = t.get("isin") or ""
            ccy = (t.get("currency") or base_ccy or "USD").upper()
            snap.trades.append(Trade(
                when=_date(t.get("dateTime")) or _date(t.get("tradeDate")),
                key=isin or t.get("conid") or t.get("symbol") or "",
                name=t.get("description") or t.get("symbol") or "", side=side, quantity=qty,
                amount=round(abs(net), 2), currency=ccy,
                ticker=yf_ticker(t.get("symbol"), t.get("listingExchange"), ccy, isin),
            ))

        for c in _rows(st.find("CashTransactions"), "CashTransaction", ("DETAIL",)):
            kind = (c.get("type") or "").lower()
            row = {"date": _date(c.get("dateTime") or c.get("settleDate") or c.get("reportDate"))[:10],
                   "amount": num(c.get("amount")),
                   "currency": (c.get("currency") or base_ccy or "USD").upper()}
            if kind in ("dividends", "payment in lieu of dividends", "withholding tax"):
                snap.dividends.append({**row, "ticker": c.get("symbol") or "", "isin": c.get("isin") or ""})
            elif kind in ("broker interest received", "bond interest received"):
                snap.interest.append(row)

        report = st.find("CashReport")
        per_ccy = [r for r in (report.findall("CashReportCurrency") if report is not None else [])
                   if (r.get("currency") or "").upper() not in ("", "BASE_SUMMARY")]
        if not per_ccy and report is not None and base_ccy:
            per_ccy = [r for r in report.findall("CashReportCurrency")
                       if (r.get("currency") or "").upper() == "BASE_SUMMARY"]
        for r in per_ccy:
            ccy = (r.get("currency") or "").upper()
            ccy = base_ccy.upper() if ccy == "BASE_SUMMARY" else ccy
            snap.cash.append({"name": f"cash {ccy}", "currency": ccy,
                              "balance": round(num(r.get("endingCash")), 2)})

    if root.find(".//FlexStatement") is None:
        raise ConnectorError("The IBKR report is empty. Check the Flex Query has an account selected.")
    parts = [f"{len(snap.holdings)} positions", f"{len(snap.trades)} trades"]
    if to_date:
        parts.append(f"report of {to_date[:10]}")
    snap.summary = " · ".join(parts)
    return snap


def fetch(creds: dict, conn: dict | None = None, *, max_wait: float = 45.0) -> Snapshot:
    token = (creds.get("token") or "").strip()
    query_id = (creds.get("query_id") or "").strip()
    if not token or not query_id:
        raise ConnectorError("Fill in both the Flex token and the Query ID.")
    if not query_id.isdigit():
        raise ConnectorError("The Query ID is a number, shown next to your Flex Query.")
    label = (conn or {}).get("label") or "IBKR"
    return parse(download(token, query_id, max_wait=max_wait), label)
