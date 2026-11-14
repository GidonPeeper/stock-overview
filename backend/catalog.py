"""Broker and bank catalog for the "Add an account" flow.

`broker_catalog.json` is research data (institutions, how to track each, steps,
official links); it can be replaced by a bigger catalog without code changes.
This module adds what the app knows on top (via connectors/registry.py, the
one place that links catalog entries to code) so the Settings page can render
everything from data:

  * support: live / import / csv / manual / info (see registry.link)
  * fields:  the form fields a live connector needs, named as the connector
             expects, labelled as the catalog says
  * updates: how often the data refreshes, in plain words
  * aggregator: the top-level aggregator entry an aggregator method refers to
  * a generic "positions file" upload for brokers without a readable export,
    and a manual balance for every institution
  * default: which method to open first (the recommended one if the app can
    use it, else the first one it can)
"""

from __future__ import annotations

import copy
import functools
import json
from pathlib import Path

from .connectors import registry

CATALOG = Path(__file__).with_name("broker_catalog.json")

BROKERISH = ("broker", "both", "robo", "crypto")
_FEED_COVERS = {"holdings": "positions", "trades": "transactions"}
ACTIONABLE = ("live", "import", "csv", "manual")

GENERIC_CSV_METHOD = {
    "type": "upload", "file_format": "csv", "report_name": "Positions file (any broker)",
    "importer": registry.GENERIC_CSV,
    "steps": [
        "In your broker's website or app, find Portfolio, Positions or Holdings.",
        "Look for Export or Download and choose CSV. (Excel? Save it as CSV first.)",
        "Upload the file below. You can check which column is which before saving.",
    ],
    "covers": ["positions"],
    "notes": "Works with most positions exports: it looks for columns such as Symbol or ISIN, "
             "Quantity and Average cost. Prices then update live where the symbol is known.",
    "synthetic": True,
}
MANUAL_METHOD = {"type": "manual", "covers": ["balance"], "synthetic": True}

OTHER = [
    {"id": "other_broker", "name": "Another broker or app (positions file)", "kind": "broker",
     "countries": [], "recommended": "upload", "methods": [], "sources": [], "synthetic": True},
    {"id": "other_bank", "name": "Another bank, savings or pension account", "kind": "bank",
     "countries": [], "recommended": "manual", "methods": [], "sources": [], "synthetic": True},
]


@functools.lru_cache(maxsize=2)
def _load(path: str, mtime: float) -> dict:
    return json.loads(Path(path).read_text())


def raw() -> dict:
    try:
        return _load(str(CATALOG), CATALOG.stat().st_mtime)
    except (OSError, ValueError):
        return {"version": 1, "aggregators": [], "institutions": []}


def _fields(spec: registry.Spec, method: dict) -> list[dict]:
    """The connector's fields, labelled as the catalog labels them."""
    given = {f.get("name"): f for f in (method.get("fields") or []) if isinstance(f, dict)}
    out = []
    for f in spec.fields:
        cat = next((given[n] for n in (f["name"], *f.get("aliases", ())) if n in given), {})
        out.append({"name": f["name"], "label": cat.get("label") or f.get("label") or f["name"],
                    "secret": f.get("secret", True) is not False})
    return out


def _method(inst: dict, method: dict, key: str, aggregators: dict) -> dict:
    m = copy.deepcopy(method)
    m["key"] = key
    m.update(registry.link(inst, method))
    support = m["support"]
    if support == "live":
        spec = registry.connector(m["connector"])
        m["fields"] = _fields(spec, method)
        m["updates"] = spec.updates
        # what this app reads (the API may offer more)
        m["api_covers"] = m.get("covers") or []
        m["covers"] = [_FEED_COVERS.get(f, f) for f in spec.feeds]
    elif support in ("import", "csv"):
        m["updates"] = "When you upload a new file"
        m.pop("fields", None)
    elif support == "manual":
        m["updates"] = "When you change the balance"
    if m.get("type") == "aggregator":
        m["aggregator"] = aggregators.get(registry.norm(m.get("provider", "")), None)
    return m


def institution(inst: dict, aggregators: dict) -> dict:
    out = {k: copy.deepcopy(v) for k, v in inst.items() if k != "methods"}
    methods = [_method(inst, m, str(i), aggregators)
               for i, m in enumerate(inst.get("methods") or []) if isinstance(m, dict)]
    if inst.get("kind") in BROKERISH and not any(m["support"] == "csv" for m in methods):
        methods.append(_method(inst, GENERIC_CSV_METHOD, "csv", aggregators))
    if not any(m["type"] == "manual" for m in methods):
        methods.append(_method(inst, MANUAL_METHOD, "manual", aggregators))
    rec = inst.get("recommended")
    # of several methods of the recommended type (e.g. bank-link services),
    # recommend one the app can use
    first = next((m for m in methods if m["type"] == rec and m["support"] in ACTIONABLE), None) \
        or next((m for m in methods if m["type"] == rec), None)
    if first is not None:
        first["recommended"] = True
        methods.remove(first)
        methods.insert(0, first)
    # open the recommended method if the app can use it, else the most automatic one it can
    # (for a bank that also invests, a balance beats the generic positions file)
    def rank(m: dict) -> int:
        if m["support"] not in ACTIONABLE:
            return 9
        if m["support"] == "csv" and inst.get("kind") in ("bank", "both"):
            return 4
        return ACTIONABLE.index(m["support"])

    default = next((m for m in methods if m.get("recommended") and m["support"] in ACTIONABLE),
                   None) or min(methods, key=rank)
    out["methods"] = methods
    out["default"] = default["key"]
    countries = inst.get("countries") or []
    out["wrappers"] = inst.get("wrappers") or (
        ["ISA"] if "UK" in countries and inst.get("kind") in ("broker", "both", "robo") else [])
    return out


def view() -> dict:
    data = raw()
    aggregators = {registry.norm(a.get("id", "")): a for a in data.get("aggregators", [])
                   if isinstance(a, dict)}
    insts = [institution(i, aggregators) for i in data.get("institutions", [])
             if isinstance(i, dict) and i.get("id")]
    insts.sort(key=lambda i: (i.get("name") or i["id"]).lower())
    insts += [institution(i, aggregators) for i in OTHER]
    return {"version": data.get("version", 1), "researched_on": data.get("researched_on"),
            "aggregators": list(aggregators.values()), "institutions": insts}


def find(institution_id: str) -> dict | None:
    want = registry.norm(institution_id)
    return next((i for i in view()["institutions"] if registry.norm(i["id"]) == want), None)


def method(inst: dict, key: str) -> dict | None:
    return next((m for m in inst.get("methods", []) if m["key"] == str(key)), None)
