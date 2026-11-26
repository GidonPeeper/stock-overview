"""Country files: how tax on investments works in a country, the currency and
time zone it suggests, and its news feeds (backend/countries/<CC>.json).

The format is in backend/countries/SCHEMA.md. Every file is checked strictly
when it is read (validate()): an unknown key, a missing key, a bad topic id or
order, a bad date, an address that is not https or a `code` that differs from
the file name is an error that names the file and the field. A file with
errors is left out, with a warning in the log, so one bad file never breaks
the app; tests/test_countries.py fails on it instead.

COUNTRIES_EXTRA_DIR (optional): a folder with more country files, read after
backend/countries/ (a file there replaces the built-in one with the same
code). For trying out a new or changed file without copying it into the app.

  all()           every country for the picker, with what the app has for it
  get(code)       one country's checked file (a copy), or None
  overview(code)  what /api/country and the MCP tool answer
  news_feeds(code)  the country's news feeds ([] when it has none)
"""

from __future__ import annotations

import copy
import json
import logging
import os
import re
import threading
import unicodedata
from datetime import date, timedelta
from pathlib import Path
from urllib.parse import urlsplit

log = logging.getLogger("stock_overview.countries")

DIR = Path(__file__).with_name("countries")
EXTRA_ENV = "COUNTRIES_EXTRA_DIR"

TOPIC_IDS = ("gains", "dividends", "interest", "funds", "wealth", "allowances",
             "accounts", "losses", "foreign", "reporting", "crypto")
CURRENCIES = ("EUR", "GBP", "USD")
# Countries the app's own tax calculations cover (harvest.RESIDENCES, as ISO codes).
TAX_CARDS = ("GB", "US", "NL")
CALCULATIONS_NOTE = "The app's own tax calculations cover the UK, the US and the Netherlands only."
DISCLAIMER = ("General information, not tax advice. Rules change: check the official pages "
              "linked here, or ask a tax adviser, before you act.")

_REQUIRED = ("code", "name", "currency", "currency_note", "timezone", "languages", "checked_on",
             "tax_year", "summary", "topics", "official", "upcoming", "not_covered", "news")
_OPTIONAL = ("dates", "unverified")
_TAX_YEAR = ("label", "starts", "ends", "return_due", "source")
_TOPIC_REQUIRED = ("id", "title", "points", "sources")
_TOPIC_OPTIONAL = ("figures",)

_CODE = re.compile(r"^[A-Z]{2}$")
_LANG = re.compile(r"^[a-z]{2,3}$")
_ISO_DAY = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_MONTH_DAY = re.compile(r"^\d{2}-\d{2}$")
_ZONE = re.compile(r"^[A-Za-z_]+(/[A-Za-z0-9_+-]+)+$")


class CountryFileError(ValueError):
    """A country file that does not follow SCHEMA.md; `errors` lists why."""

    def __init__(self, errors: list[str]):
        super().__init__("; ".join(errors))
        self.errors = errors


# ---------------------------------------------------------------- checking
class _Check:
    """Collects 'FILE: field: problem' lines."""

    def __init__(self, fname: str):
        self.fname = fname
        self.errors: list[str] = []

    def err(self, path: str, problem: str) -> None:
        self.errors.append(f"{self.fname}: {path}: {problem}" if path else f"{self.fname}: {problem}")

    def obj(self, v, path: str, required: tuple, optional: tuple = ()) -> bool:
        """An object with exactly these keys (optional ones may be left out)."""
        if not isinstance(v, dict):
            self.err(path, "must be an object {…}")
            return False
        for k in sorted(set(v) - set(required) - set(optional)):
            self.err(f"{path}.{k}" if path else k, "unknown key (see SCHEMA.md)")
        for k in required:
            if k not in v:
                self.err(f"{path}.{k}" if path else k, "missing")
        return True

    def text(self, v, path: str, empty: bool = False) -> bool:
        if not isinstance(v, str):
            self.err(path, "must be text")
            return False
        if not empty and not v.strip():
            self.err(path, "must not be empty")
            return False
        return True

    def texts(self, v, path: str, at_least: int = 0) -> None:
        if not isinstance(v, list):
            self.err(path, "must be a list […]")
            return
        if len(v) < at_least:
            self.err(path, f"needs at least {at_least} item{'s' if at_least > 1 else ''}")
        for i, x in enumerate(v):
            self.text(x, f"{path}[{i}]")

    def https(self, v, path: str) -> None:
        if not self.text(v, path):
            return
        try:
            p = urlsplit(v)
            ok = (p.scheme == "https" and p.hostname and "." in p.hostname and not p.username
                  and not p.password and not any(c.isspace() for c in v))
        except ValueError:              # e.g. "https://[bad"
            ok = False
        if not ok:
            self.err(path, f"must be an https:// address, got {v[:80]!r}")

    def items(self, v, path: str, low: int, high: int | None) -> list:
        """A list with `low` to `high` entries; returns it (or [])."""
        if not isinstance(v, list):
            self.err(path, "must be a list […]")
            return []
        if len(v) < low or (high is not None and len(v) > high):
            self.err(path, f"needs {low} to {high} items, has {len(v)}" if high is not None
                     else f"needs at least {low} item{'s' if low > 1 else ''}")
        return v

    def month_day(self, v, path: str) -> date | None:
        if not self.text(v, path):
            return None
        if _MONTH_DAY.match(v):
            try:
                return date(2000, int(v[:2]), int(v[3:]))     # a leap year: 02-29 is a real day
            except ValueError:
                pass
        self.err(path, f"must be a day of the year as MM-DD, e.g. 04-06, got {v!r}")
        return None


def validate(data, fname: str, today: date | None = None) -> list[str]:
    """Every way `data` (a parsed file named `fname`, e.g. "NL.json") breaks
    SCHEMA.md, as 'NL.json: topics[2].sources[0]: problem' lines; [] when it
    is fine."""
    c = _Check(fname)
    today = today or date.today()
    if not c.obj(data, "", _REQUIRED, _OPTIONAL):
        return c.errors

    stem = fname[:-5] if fname.endswith(".json") else fname
    code = data.get("code")
    if c.text(code, "code"):
        if code == "UK":
            c.err("code", "the ISO code of the United Kingdom is GB: name the file GB.json")
        elif not _CODE.match(code):
            c.err("code", f"must be a two-letter code in capitals, e.g. NL, got {code!r}")
        elif code != stem:
            c.err("code", f"is {code!r} but the file is called {fname}: they must match")
    if "name" in data:
        c.text(data["name"], "name")
    if "currency" in data and data["currency"] not in CURRENCIES:
        c.err("currency", f"must be one of {', '.join(CURRENCIES)} (the app shows only these), "
                          f"got {data['currency']!r}")
    if "currency_note" in data:
        c.text(data["currency_note"], "currency_note", empty=True)
    if "timezone" in data and c.text(data["timezone"], "timezone"):
        tz = data["timezone"]
        ok = bool(_ZONE.match(tz))
        if ok:
            from zoneinfo import ZoneInfo
            try:
                ZoneInfo(tz)
            except Exception:
                ok = False
        if not ok:
            c.err("timezone", f"must be an IANA time zone such as Europe/Amsterdam, got {tz!r}")
    if "languages" in data:
        langs = c.items(data["languages"], "languages", 1, None)
        for i, x in enumerate(langs):
            if not (isinstance(x, str) and _LANG.match(x)):
                c.err(f"languages[{i}]", f"must be a language code in small letters, e.g. nl, got {x!r}")
    if "checked_on" in data and c.text(data["checked_on"], "checked_on"):
        v = data["checked_on"]
        try:
            day = date.fromisoformat(v) if _ISO_DAY.match(v) else None
        except ValueError:
            day = None
        if day is None:
            c.err("checked_on", f"must be a date as YYYY-MM-DD, got {v!r}")
        elif day > today + timedelta(days=1):
            c.err("checked_on", f"{v} is in the future")

    if "tax_year" in data and c.obj(data["tax_year"], "tax_year", _TAX_YEAR):
        ty = data["tax_year"]
        for k in ("label", "return_due"):
            if k in ty:
                c.text(ty[k], f"tax_year.{k}")
        starts = c.month_day(ty["starts"], "tax_year.starts") if "starts" in ty else None
        ends = c.month_day(ty["ends"], "tax_year.ends") if "ends" in ty else None
        before = starts - timedelta(days=1) if starts else None
        if before and ends and (before.month, before.day) != (ends.month, ends.day):
            c.err("tax_year.ends", "must be the day before tax_year.starts (a tax year runs 12 months)")
        if "source" in ty:
            c.https(ty["source"], "tax_year.source")

    if "summary" in data:
        c.text(data["summary"], "summary")

    if "topics" in data:
        seen: list[str] = []
        for i, t in enumerate(c.items(data["topics"], "topics", 1, None)):
            p = f"topics[{i}]"
            if not c.obj(t, p, _TOPIC_REQUIRED, _TOPIC_OPTIONAL):
                continue
            tid = t.get("id")
            if "id" in t:
                if tid not in TOPIC_IDS:
                    c.err(f"{p}.id", f"{tid!r} is not a topic id; use one of {', '.join(TOPIC_IDS)}")
                elif tid in seen:
                    c.err(f"{p}.id", f"{tid!r} appears twice")
                elif seen and TOPIC_IDS.index(tid) < TOPIC_IDS.index(seen[-1]):
                    c.err(f"{p}.id", f"{tid!r} must come before {seen[-1]!r} "
                                     f"(order: {', '.join(TOPIC_IDS)})")
                if tid in TOPIC_IDS and tid not in seen:
                    seen.append(tid)
            if "title" in t:
                c.text(t["title"], f"{p}.title")
            if "points" in t:
                c.texts(t["points"], f"{p}.points", at_least=1)
            for j, f in enumerate(c.items(t.get("figures", []), f"{p}.figures", 0, None)):
                if c.obj(f, f"{p}.figures[{j}]", ("label", "value")):
                    for k in ("label", "value"):
                        if k in f:
                            c.text(f[k], f"{p}.figures[{j}].{k}")
            if "sources" in t:
                for j, s in enumerate(c.items(t["sources"], f"{p}.sources", 1, None)):
                    c.https(s, f"{p}.sources[{j}]")

    for i, d in enumerate(c.items(data.get("dates", []), "dates", 0, None)):
        if c.obj(d, f"dates[{i}]", ("when", "what")):
            if "when" in d:
                c.month_day(d["when"], f"dates[{i}].when")
            if "what" in d:
                c.text(d["what"], f"dates[{i}].what")

    if "official" in data:
        for i, o in enumerate(c.items(data["official"], "official", 2, 6)):
            if c.obj(o, f"official[{i}]", ("title", "url")):
                if "title" in o:
                    c.text(o["title"], f"official[{i}].title")
                if "url" in o:
                    c.https(o["url"], f"official[{i}].url")

    for k in ("upcoming", "not_covered"):
        if k in data:
            c.texts(data[k], k)

    for i, u in enumerate(c.items(data.get("unverified", []), "unverified", 0, None)):
        if c.obj(u, f"unverified[{i}]", ("point", "source")):
            if "point" in u:
                c.text(u["point"], f"unverified[{i}].point")
            if "source" in u:
                c.https(u["source"], f"unverified[{i}].source")

    if "news" in data:
        for i, n in enumerate(c.items(data["news"], "news", 2, 5)):
            if c.obj(n, f"news[{i}]", ("name", "url", "language")):
                if "name" in n:
                    c.text(n["name"], f"news[{i}].name")
                if "url" in n:
                    c.https(n["url"], f"news[{i}].url")
                if "language" in n and not (isinstance(n["language"], str) and _LANG.match(n["language"])):
                    c.err(f"news[{i}].language", f"must be a language code in small letters, e.g. nl, "
                                                 f"got {n['language']!r}")
    return c.errors


def _no_duplicates(pairs):
    keys = [k for k, _ in pairs]
    dup = sorted({k for k in keys if keys.count(k) > 1})
    if dup:
        raise ValueError(f"key {dup[0]!r} appears twice in one object")
    return dict(pairs)


def read_file(path: Path) -> dict:
    """The parsed, checked file; raises CountryFileError naming the problems."""
    fname = path.name
    try:
        data = json.loads(path.read_text(encoding="utf-8"), object_pairs_hook=_no_duplicates)
    except UnicodeDecodeError:
        raise CountryFileError([f"{fname}: is not UTF-8 text"]) from None
    except json.JSONDecodeError as e:
        raise CountryFileError([f"{fname}: is not valid JSON (line {e.lineno}, column {e.colno}: {e.msg})"]) from None
    except ValueError as e:
        raise CountryFileError([f"{fname}: {e}"]) from None
    except OSError as e:
        raise CountryFileError([f"{fname}: cannot be read ({e.strerror or e})"]) from None
    errors = validate(data, fname)
    if errors:
        raise CountryFileError(errors)
    return data


# ---------------------------------------------------------------- loading
_lock = threading.Lock()
_state: dict = {"sig": None, "files": {}, "problems": []}


def _paths() -> list[Path]:
    dirs = [DIR]
    extra = (os.getenv(EXTRA_ENV) or "").strip()
    if extra:
        dirs.append(Path(extra).expanduser())
    out = []
    for d in dirs:
        try:
            out += sorted(p for p in d.glob("*.json") if p.is_file())
        except OSError:
            continue
    return out


def _load() -> tuple[dict, list[str]]:
    """{code: data} of the good files and the problems of the others. Read
    again only when a file was added, changed or removed."""
    paths = _paths()
    sig = []
    for p in paths:
        try:
            st = p.stat()
            sig.append((str(p), st.st_mtime_ns, st.st_size))
        except OSError:
            continue
    sig = tuple(sig)
    with _lock:
        if _state["sig"] == sig:
            return _state["files"], _state["problems"]
        files, problems = {}, []
        for p in paths:
            try:
                data = read_file(p)
            except CountryFileError as e:
                problems += e.errors
                log.warning("Country file %s left out: %s", p.name, "; ".join(e.errors))
                continue
            files[data["code"]] = data            # the extra folder comes last: it wins
        _state.update(sig=sig, files=files, problems=problems)
        return files, problems


def problems() -> list[str]:
    """Why files were left out (shown to contributors; no paths, only names)."""
    return list(_load()[1])


def get(code: str) -> dict | None:
    data = _load()[0].get((code or "").upper())
    return copy.deepcopy(data) if data else None


def news_feeds(code: str) -> list[dict]:
    data = _load()[0].get((code or "").upper())
    return [dict(n) for n in (data or {}).get("news", [])]


def clean_code(code) -> str:
    """A two-letter code in capitals; "" for none. Raises ValueError."""
    v = str(code or "").strip().upper()
    v = {"UK": "GB", "USA": "US"}.get(v, v)
    if v in ("", "NONE", "OTHER"):
        return ""
    if not _CODE.match(v):
        raise ValueError("A country is a two-letter code, for example NL")
    return v


def name(code: str) -> str:
    code = (code or "").upper()
    data = _load()[0].get(code)
    return data["name"] if data else NAMES.get(code, code)


# "in the Netherlands", "in Japan"
_THE = {"AE", "BQ", "BS", "CD", "CF", "CG", "DO", "GB", "GM", "KM", "MV", "NL", "PH", "SC", "US"}


def in_name(code: str) -> str:
    n = name(code)
    return f"the {n}" if (code or "").upper() in _THE or n.endswith(" Islands") else n


def _sort_key(text: str) -> str:
    return "".join(ch for ch in unicodedata.normalize("NFKD", text) if not unicodedata.combining(ch)).casefold()


def all() -> list[dict]:  # noqa: A001 (the module's own name for "every country")
    """Every country for the picker, by name: what the app has for each."""
    files = _load()[0]
    rows = []
    for code in sorted(set(NAMES) | set(files)):
        data = files.get(code)
        rows.append({
            "code": code,
            "name": data["name"] if data else NAMES[code],
            "in_name": in_name(code),
            "aka": ALIASES.get(code, ""),
            "has_overview": bool(data),
            "has_news": bool(data and data.get("news")),
            "tax_cards": code in TAX_CARDS,
            "currency": data["currency"] if data else None,
            "currency_note": data["currency_note"] if data else "",
            "timezone": data["timezone"] if data else None,
            "checked_on": data["checked_on"] if data else None,
        })
    rows.sort(key=lambda r: _sort_key(r["name"]))
    return rows


def _source() -> str:
    """Where the country comes from: chosen in Settings, the TAX_RESIDENCE
    environment variable, or the built-in default (the UK)."""
    from . import prefs
    if "country" in prefs.get()["chosen"]:
        return "settings"
    return "environment" if (os.getenv("TAX_RESIDENCE") or "").strip() else "default"


def overview(code: str | None = None) -> dict:
    """The tax overview of `code`, or of the country chosen in Settings. Always
    answers: `available` says whether there is an overview, `message` why not."""
    from . import prefs
    asked = code is not None
    code = clean_code(code if asked else prefs.value("country"))
    out: dict = {"code": code, "source": "asked" if asked else _source(),
                 "tax_cards": code in TAX_CARDS, "disclaimer": DISCLAIMER}
    if not code:
        out.update(available=False, name="", in_name="",
                   message="No country chosen. Choose the country you live in under Settings to see "
                           "how tax on investments works there.")
        return out
    out.update(name=name(code), in_name=in_name(code))
    data = get(code)
    if not data:
        out.update(available=False,
                   message=f"There is no tax overview for {out['in_name']} yet.")
    else:
        out.update(available=True, overview=data)
    if code not in TAX_CARDS:
        out["calculations_note"] = CALCULATIONS_NOTE
    return out


# ------------------------------------------------- ISO 3166-1 country names
# Short English names; places where nobody lives (Antarctica, Bouvet Island…)
# are left out. XK (Kosovo) is a code in common use, not an ISO one.
_NAMES = """
AD Andorra|AE United Arab Emirates|AF Afghanistan|AG Antigua and Barbuda|AI Anguilla|AL Albania
AM Armenia|AO Angola|AR Argentina|AS American Samoa|AT Austria|AU Australia|AW Aruba
AX Åland Islands|AZ Azerbaijan|BA Bosnia and Herzegovina|BB Barbados|BD Bangladesh|BE Belgium
BF Burkina Faso|BG Bulgaria|BH Bahrain|BI Burundi|BJ Benin|BL Saint Barthélemy|BM Bermuda
BN Brunei|BO Bolivia|BQ Caribbean Netherlands|BR Brazil|BS Bahamas|BT Bhutan|BW Botswana
BY Belarus|BZ Belize|CA Canada|CC Cocos (Keeling) Islands|CD Democratic Republic of the Congo
CF Central African Republic|CG Republic of the Congo|CH Switzerland|CI Côte d'Ivoire
CK Cook Islands|CL Chile|CM Cameroon|CN China|CO Colombia|CR Costa Rica|CU Cuba|CV Cape Verde
CW Curaçao|CX Christmas Island|CY Cyprus|CZ Czechia|DE Germany|DJ Djibouti|DK Denmark
DM Dominica|DO Dominican Republic|DZ Algeria|EC Ecuador|EE Estonia|EG Egypt|EH Western Sahara
ER Eritrea|ES Spain|ET Ethiopia|FI Finland|FJ Fiji|FK Falkland Islands|FM Micronesia
FO Faroe Islands|FR France|GA Gabon|GB United Kingdom|GD Grenada|GE Georgia|GF French Guiana
GG Guernsey|GH Ghana|GI Gibraltar|GL Greenland|GM Gambia|GN Guinea|GP Guadeloupe
GQ Equatorial Guinea|GR Greece|GT Guatemala|GU Guam|GW Guinea-Bissau|GY Guyana|HK Hong Kong
HN Honduras|HR Croatia|HT Haiti|HU Hungary|ID Indonesia|IE Ireland|IL Israel|IM Isle of Man
IN India|IQ Iraq|IR Iran|IS Iceland|IT Italy|JE Jersey|JM Jamaica|JO Jordan|JP Japan|KE Kenya
KG Kyrgyzstan|KH Cambodia|KI Kiribati|KM Comoros|KN Saint Kitts and Nevis|KP North Korea
KR South Korea|KW Kuwait|KY Cayman Islands|KZ Kazakhstan|LA Laos|LB Lebanon|LC Saint Lucia
LI Liechtenstein|LK Sri Lanka|LR Liberia|LS Lesotho|LT Lithuania|LU Luxembourg|LV Latvia
LY Libya|MA Morocco|MC Monaco|MD Moldova|ME Montenegro|MF Saint Martin|MG Madagascar
MH Marshall Islands|MK North Macedonia|ML Mali|MM Myanmar|MN Mongolia|MO Macao
MP Northern Mariana Islands|MQ Martinique|MR Mauritania|MS Montserrat|MT Malta|MU Mauritius
MV Maldives|MW Malawi|MX Mexico|MY Malaysia|MZ Mozambique|NA Namibia|NC New Caledonia|NE Niger
NF Norfolk Island|NG Nigeria|NI Nicaragua|NL Netherlands|NO Norway|NP Nepal|NR Nauru|NU Niue
NZ New Zealand|OM Oman|PA Panama|PE Peru|PF French Polynesia|PG Papua New Guinea|PH Philippines
PK Pakistan|PL Poland|PM Saint Pierre and Miquelon|PN Pitcairn Islands|PR Puerto Rico
PS Palestine|PT Portugal|PW Palau|PY Paraguay|QA Qatar|RE Réunion|RO Romania|RS Serbia
RU Russia|RW Rwanda|SA Saudi Arabia|SB Solomon Islands|SC Seychelles|SD Sudan|SE Sweden
SG Singapore|SH Saint Helena|SI Slovenia|SJ Svalbard and Jan Mayen|SK Slovakia|SL Sierra Leone
SM San Marino|SN Senegal|SO Somalia|SR Suriname|SS South Sudan|ST São Tomé and Príncipe
SV El Salvador|SX Sint Maarten|SY Syria|SZ Eswatini|TC Turks and Caicos Islands|TD Chad|TG Togo
TH Thailand|TJ Tajikistan|TK Tokelau|TL Timor-Leste|TM Turkmenistan|TN Tunisia|TO Tonga
TR Türkiye|TT Trinidad and Tobago|TV Tuvalu|TW Taiwan|TZ Tanzania|UA Ukraine|UG Uganda
US United States|UY Uruguay|UZ Uzbekistan|VA Vatican City|VC Saint Vincent and the Grenadines
VE Venezuela|VG British Virgin Islands|VI U.S. Virgin Islands|VN Vietnam|VU Vanuatu
WF Wallis and Futuna|WS Samoa|XK Kosovo|YE Yemen|YT Mayotte|ZA South Africa|ZM Zambia
ZW Zimbabwe
"""
NAMES = dict(item.strip().split(" ", 1) for item in _NAMES.replace("\n", "|").split("|") if item.strip())

# Other names people search for.
ALIASES = {
    "GB": "UK Britain Great Britain England Scotland Wales Northern Ireland",
    "US": "USA America United States of America", "NL": "Holland Nederland", "DE": "Deutschland",
    "CH": "Schweiz Suisse Svizzera", "AT": "Österreich", "ES": "España", "IT": "Italia",
    "BE": "België Belgique", "SE": "Sverige", "NO": "Norge", "DK": "Danmark", "FI": "Suomi",
    "PL": "Polska", "IE": "Éire", "GR": "Hellas Ellada", "CZ": "Czech Republic",
    "AE": "UAE Emirates Dubai", "TR": "Turkey", "CI": "Ivory Coast", "SZ": "Swaziland",
    "MK": "Macedonia", "MM": "Burma", "CV": "Cabo Verde", "KR": "Korea", "CD": "DRC Congo-Kinshasa",
    "CG": "Congo-Brazzaville", "VA": "Holy See", "TL": "East Timor", "HK": "Hong Kong SAR",
    "MO": "Macau",
}
