"""Country files (backend/countries.py, backend/countries/SCHEMA.md): every
file in backend/countries/ is valid; the validator catches each kind of
mistake; a bad file is left out at run time; the API, the news per country
and the feed parser.

The fixture tests/countries/ZZ.json ("Exampleland") is made up. No network:
feeds are parsed from strings and fetching is replaced by fakes.
"""

import copy
import io
import json
import os
import tempfile
import unittest
import xml.etree.ElementTree as ET
from datetime import date
from pathlib import Path
from unittest import mock

from backend import cache, countries, insights, prefs

ROOT = Path(__file__).resolve().parents[1]
FIXTURES = ROOT / "tests" / "countries"
GOOD = json.loads((FIXTURES / "ZZ.json").read_text(encoding="utf-8"))
TODAY = date(2026, 11, 26)


def errors(data, fname="ZZ.json"):
    return countries.validate(data, fname, today=TODAY)


def changed(**top):
    """The good fixture with top-level keys replaced (None removes one)."""
    d = copy.deepcopy(GOOD)
    for k, v in top.items():
        if v is None:
            d.pop(k, None)
        else:
            d[k] = v
    return d


class Isolated(unittest.TestCase):
    """Country files from a folder of the test's own; prefs in a temp folder."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "countries"
        self.dir.mkdir()
        self.data = Path(tmp.name) / "data"
        self.data.mkdir()
        for p in (mock.patch.object(countries, "DIR", self.dir),
                  mock.patch.object(countries, "_state", {"sig": None, "files": {}, "problems": []}),
                  mock.patch.object(prefs, "DATA_DIR", self.data),
                  mock.patch.object(prefs, "_cache", [None, None]),
                  mock.patch.dict(os.environ, {countries.EXTRA_ENV: "", "TAX_RESIDENCE": ""})):
            p.start()
            self.addCleanup(p.stop)

    def put(self, data, name=None, folder=None):
        path = (folder or self.dir) / (name or f"{data['code']}.json")
        path.write_text(json.dumps(data) if not isinstance(data, str) else data, encoding="utf-8")
        st = path.stat()
        os.utime(path, ns=(st.st_atime_ns, st.st_mtime_ns + 1_000_000))   # seen as changed
        return path


# ------------------------------------------------------- the real files
class CountryFilesTest(unittest.TestCase):
    def test_every_file_in_backend_countries_is_valid(self):
        """The research files: each must follow SCHEMA.md exactly."""
        folder = ROOT / "backend" / "countries"
        files = sorted(folder.glob("*.json")) if folder.is_dir() else []
        for path in files:
            with self.subTest(file=path.name):
                try:
                    countries.read_file(path)
                except countries.CountryFileError as e:
                    self.fail("\n".join(e.errors))

    def test_only_country_files_and_the_schema_in_the_folder(self):
        folder = ROOT / "backend" / "countries"
        if not folder.is_dir():
            self.skipTest("no backend/countries/ yet")
        stray = [p.name for p in folder.iterdir() if p.suffix not in (".json", ".md")]
        self.assertEqual(stray, [])

    def test_the_fixture_is_valid(self):
        self.assertEqual(countries.read_file(FIXTURES / "ZZ.json")["name"], "Exampleland")


# -------------------------------------------------------------- validator
class ValidatorTest(unittest.TestCase):
    def assertError(self, data, field, words="", fname="ZZ.json"):
        errs = errors(data, fname)
        hits = [e for e in errs if e.startswith(f"{fname}: {field}:")]
        self.assertTrue(hits, f"no error for {field}; got {errs}")
        if words:
            self.assertTrue(any(words in e for e in hits), f"{words!r} not in {hits}")

    def test_good_file(self):
        self.assertEqual(errors(GOOD), [])
        minimal = changed(dates=None, unverified=None)
        for t in minimal["topics"]:
            t.pop("figures", None)
        self.assertEqual(errors(minimal), [], "dates, unverified and figures are optional")

    def test_unknown_keys(self):
        self.assertError(changed(website="https://x.example.org"), "website", "unknown key")
        d = changed()
        d["topics"][0]["notes"] = "x"
        self.assertError(d, "topics[0].notes", "unknown key")
        d = changed()
        d["tax_year"]["month"] = 4
        self.assertError(d, "tax_year.month", "unknown key")
        d = changed()
        d["topics"][0]["figures"][0]["unit"] = "%"
        self.assertError(d, "topics[0].figures[0].unit", "unknown key")
        d = changed()
        d["news"][0]["paywall"] = False
        self.assertError(d, "news[0].paywall", "unknown key")

    def test_missing_required_keys(self):
        for key in countries._REQUIRED:
            with self.subTest(key=key):
                self.assertError(changed(**{key: None}), key, "missing")
        d = changed()
        del d["topics"][1]["sources"]
        self.assertError(d, "topics[1].sources", "missing")
        d = changed()
        del d["tax_year"]["source"]
        self.assertError(d, "tax_year.source", "missing")
        d = changed()
        del d["official"][0]["url"]
        self.assertError(d, "official[0].url", "missing")

    def test_wrong_types_and_empty_text(self):
        self.assertError(changed(summary="  "), "summary", "empty")
        self.assertError(changed(upcoming="none"), "upcoming", "list")
        self.assertError(changed(not_covered=[""]), "not_covered[0]", "empty")
        d = changed()
        d["topics"][0]["points"] = []
        self.assertError(d, "topics[0].points", "at least 1")
        self.assertEqual(errors([1, 2]), ["ZZ.json: must be an object {…}"])

    def test_topic_ids_order_and_duplicates(self):
        d = changed()
        d["topics"][0]["id"] = "capital_gains"
        self.assertError(d, "topics[0].id", "not a topic id")
        d = changed()
        d["topics"][0], d["topics"][1] = d["topics"][1], d["topics"][0]     # dividends before gains
        self.assertError(d, "topics[1].id", "must come before")
        d = changed()
        d["topics"].append(copy.deepcopy(d["topics"][-1]))
        self.assertError(d, f"topics[{len(d['topics']) - 1}].id", "twice")
        self.assertError(changed(topics=[]), "topics", "at least 1")

    def test_dates(self):
        self.assertError(changed(checked_on="26-11-2026"), "checked_on", "YYYY-MM-DD")
        self.assertError(changed(checked_on="2026-02-30"), "checked_on", "YYYY-MM-DD")
        self.assertError(changed(checked_on="2027-01-05"), "checked_on", "future")
        d = changed()
        d["tax_year"]["starts"] = "4-6"
        self.assertError(d, "tax_year.starts", "MM-DD")
        d = changed()
        d["tax_year"]["ends"] = "04-04"
        self.assertError(d, "tax_year.ends", "day before")
        d = changed()
        d["dates"][0]["when"] = "13-01"
        self.assertError(d, "dates[0].when", "MM-DD")
        d = changed()
        d["tax_year"].update(starts="01-01", ends="12-31")
        d["dates"][0]["when"] = "02-29"
        self.assertEqual(errors(d), [])

    def test_addresses_must_be_https(self):
        for bad in ("http://tax.example.org/x", "javascript:alert(1)", "https://", "tax.example.org",
                    "https://tax example.org", "https://user:pw@tax.example.org/", "https://[bad"):
            with self.subTest(url=bad):
                d = changed()
                d["topics"][0]["sources"][0] = bad
                self.assertError(d, "topics[0].sources[0]", "https://")
        d = changed()
        d["tax_year"]["source"] = "http://tax.example.org"
        self.assertError(d, "tax_year.source", "https://")
        d = changed()
        d["official"][1]["url"] = "ftp://tax.example.org"
        self.assertError(d, "official[1].url", "https://")
        d = changed()
        d["unverified"][0]["source"] = "http://bank.example.com"
        self.assertError(d, "unverified[0].source", "https://")
        d = changed()
        d["news"][1]["url"] = "http://journal.example.net/feed"
        self.assertError(d, "news[1].url", "https://")
        d = changed()
        d["topics"][2]["sources"] = []
        self.assertError(d, "topics[2].sources", "at least 1")

    def test_code_matches_the_file_name(self):
        self.assertError(GOOD, "code", "YY.json", fname="YY.json")
        self.assertError(changed(code="zz"), "code", "capitals", fname="zz.json")
        self.assertError(changed(code="UK"), "code", "GB", fname="UK.json")

    def test_settings_fields(self):
        self.assertError(changed(currency="CHF"), "currency", "EUR, GBP, USD")
        self.assertError(changed(timezone="Mars/Base"), "timezone", "IANA")
        self.assertError(changed(timezone="UTC"), "timezone", "IANA")
        self.assertError(changed(languages=["NL"]), "languages[0]", "small letters")
        self.assertError(changed(languages=[]), "languages", "at least 1")
        d = changed()
        d["news"][0]["language"] = "English"
        self.assertError(d, "news[0].language", "small letters")

    def test_list_lengths(self):
        self.assertError(changed(official=GOOD["official"][:1]), "official", "2 to 6")
        self.assertError(changed(official=GOOD["official"] * 4), "official", "2 to 6")
        self.assertError(changed(news=GOOD["news"][:1]), "news", "2 to 5")
        self.assertError(changed(news=GOOD["news"] * 3), "news", "2 to 5")

    def test_every_error_names_the_file(self):
        d = changed(code="NL", summary="", currency="JPY")
        errs = errors(d)
        self.assertGreaterEqual(len(errs), 3)
        self.assertTrue(all(e.startswith("ZZ.json: ") for e in errs))

    def test_read_file_reports_bad_json_and_duplicate_keys(self):
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "ZZ.json"
            p.write_text('{"code": "ZZ",', encoding="utf-8")
            with self.assertRaises(countries.CountryFileError) as cm:
                countries.read_file(p)
            self.assertIn("ZZ.json: is not valid JSON (line 1", cm.exception.errors[0])
            p.write_text('{"code": "ZZ", "code": "ZZ"}', encoding="utf-8")
            with self.assertRaises(countries.CountryFileError) as cm:
                countries.read_file(p)
            self.assertIn("'code' appears twice", cm.exception.errors[0])


# ---------------------------------------------------------------- loading
class LoadingTest(Isolated):
    def test_a_bad_file_is_left_out_with_a_warning(self):
        self.put(GOOD)
        self.put(changed(code="YY", currency="JPY"))
        with self.assertLogs("stock_overview.countries", "WARNING") as logs:
            self.assertIsNotNone(countries.get("ZZ"))
        self.assertIsNone(countries.get("YY"))
        self.assertIn("YY.json", logs.output[0])
        self.assertTrue(any("YY.json: currency" in p for p in countries.problems()))
        self.assertTrue(next(c for c in countries.all() if c["code"] == "ZZ")["has_overview"])

    def test_changes_are_picked_up(self):
        self.assertIsNone(countries.get("ZZ"))
        self.put(GOOD)
        self.assertEqual(countries.get("ZZ")["name"], "Exampleland")
        self.put(changed(name="Exampleland Republic"))
        self.assertEqual(countries.name("ZZ"), "Exampleland Republic")
        (self.dir / "ZZ.json").unlink()
        self.assertIsNone(countries.get("ZZ"))
        self.assertEqual(countries.name("ZZ"), "ZZ")

    def test_get_returns_a_copy(self):
        self.put(GOOD)
        countries.get("ZZ")["topics"].clear()
        self.assertTrue(countries.get("ZZ")["topics"])

    def test_the_extra_folder_adds_and_replaces(self):
        self.put(GOOD)
        extra = self.dir.parent / "extra"
        extra.mkdir()
        self.put(changed(name="Exampleland (draft)"), folder=extra)
        with mock.patch.dict(os.environ, {countries.EXTRA_ENV: str(extra)}):
            self.assertEqual(countries.name("ZZ"), "Exampleland (draft)")
        self.assertEqual(countries.name("ZZ"), "Exampleland")
        with mock.patch.dict(os.environ, {countries.EXTRA_ENV: str(self.dir.parent / "missing")}):
            self.assertEqual(countries.name("ZZ"), "Exampleland")      # a missing folder is fine

    def test_all_lists_every_country_by_name(self):
        self.put(GOOD)
        rows = countries.all()
        codes = [r["code"] for r in rows]
        self.assertEqual(len(codes), len(set(codes)))
        self.assertGreater(len(rows), 240)
        for code in ("GB", "US", "NL", "DE", "JP", "ZZ"):
            self.assertIn(code, codes)
        names = [countries._sort_key(r["name"]) for r in rows]
        self.assertEqual(names, sorted(names))
        gb, zz, jp = (next(r for r in rows if r["code"] == c) for c in ("GB", "ZZ", "JP"))
        self.assertTrue(gb["tax_cards"] and not zz["tax_cards"])
        self.assertEqual((zz["currency"], zz["timezone"], zz["has_news"]), ("USD", "Atlantic/Reykjavik", True))
        self.assertEqual((jp["has_overview"], jp["currency"]), (False, None))
        self.assertIn("UK", gb["aka"])

    def test_names(self):
        self.assertEqual(countries.in_name("NL"), "the Netherlands")
        self.assertEqual(countries.in_name("GB"), "the United Kingdom")
        self.assertEqual(countries.in_name("KY"), "the Cayman Islands")
        self.assertEqual(countries.in_name("JP"), "Japan")

    def test_overview_answers_every_case(self):
        self.put(GOOD)
        prefs.update({"country": ""})
        none = countries.overview()
        self.assertEqual((none["code"], none["available"], none["source"]), ("", False, "settings"))
        self.assertIn("Settings", none["message"])
        prefs.update({"country": "JP"})
        jp = countries.overview()
        self.assertEqual((jp["available"], jp["tax_cards"]), (False, False))
        self.assertIn("no tax overview for Japan", jp["message"])
        self.assertIn("UK, the US and the Netherlands", jp["calculations_note"])
        prefs.update({"country": "ZZ"})
        zz = countries.overview()
        self.assertIs(zz["available"], True)
        self.assertEqual(zz["overview"]["summary"], GOOD["summary"])
        self.assertIn("not tax advice", zz["disclaimer"])
        gb = countries.overview("uk")
        self.assertEqual((gb["code"], gb["tax_cards"], gb["source"]), ("GB", True, "asked"))
        self.assertNotIn("calculations_note", gb)
        with self.assertRaises(ValueError):
            countries.overview("Netherlands")

    def test_where_the_country_comes_from(self):
        self.assertEqual(countries.overview()["source"], "default")              # the UK
        with mock.patch.dict(os.environ, {"TAX_RESIDENCE": "US"}):
            self.assertEqual(countries.overview()["code"], "US")
            self.assertEqual(countries.overview()["source"], "environment")
            prefs.update({"country": "NL"})
            self.assertEqual((countries.overview()["code"], countries.overview()["source"]), ("NL", "settings"))


# ------------------------------------------------------------------- news
RSS = b"""<?xml version="1.0"?><rss version="2.0"><channel><title>Feed</title>
<item><title>Rates &lt;b&gt;held&lt;/b&gt; &amp;amp; markets calm</title><link>https://news.example.org/1</link>
  <pubDate>Thu, 26 Nov 2026 09:30:00 +0100</pubDate></item>
<item><title>Bad link</title><link>javascript:alert(1)</link></item>
<item><title>  </title><link>https://news.example.org/empty</link></item>
<item><title>Koersen stijgen</title><link> http://news.example.org/3 </link></item>
</channel></rss>"""

ATOM = b"""<?xml version="1.0" encoding="utf-8"?><feed xmlns="http://www.w3.org/2005/Atom"><title>A</title>
<entry><title type="html">Les march\xc3\xa9s montent</title>
  <link rel="enclosure" href="https://journal.example.net/img.jpg"/>
  <link rel="alternate" href="https://journal.example.net/a"/>
  <updated>2026-11-26T08:00:00Z</updated></entry>
<entry><title>No link</title></entry>
</feed>"""

RDF = b"""<?xml version="1.0"?><rdf:RDF xmlns:rdf="http://www.w3.org/1999/02/22-rdf-syntax-ns#"
  xmlns="http://purl.org/rss/1.0/" xmlns:dc="http://purl.org/dc/elements/1.1/">
<item><title>RDF story</title><link>https://rdf.example.org/1</link><dc:date>2026-11-25T10:00:00+00:00</dc:date></item>
</rdf:RDF>"""


class FakeResponse(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *a):
        self.close()


class FeedParsingTest(unittest.TestCase):
    def parse(self, body):
        return insights._parse_feed(ET.fromstring(body), "Src", 10)

    def test_rss(self):
        items = self.parse(RSS)
        self.assertEqual([i["title"] for i in items], ["Rates held & markets calm", "Koersen stijgen"])
        self.assertEqual(items[0]["url"], "https://news.example.org/1")
        self.assertEqual(items[0]["published_at"], "2026-11-26T08:30:00+00:00")
        self.assertEqual(items[1]["url"], "http://news.example.org/3")
        self.assertIsNone(items[1]["published_at"])

    def test_atom_and_rdf(self):
        a = self.parse(ATOM)
        self.assertEqual([(i["title"], i["url"]) for i in a], [("Les marchés montent", "https://journal.example.net/a")])
        self.assertEqual(a[0]["published_at"], "2026-11-26T08:00:00+00:00")
        r = self.parse(RDF)
        self.assertEqual((r[0]["title"], r[0]["published_at"]), ("RDF story", "2026-11-25T10:00:00+00:00"))

    def test_only_http_links(self):
        for bad in ("javascript:alert(1)", "data:text/html,x", "file:///etc/passwd", "//x.example.org", "", None,
                    "http://[::1"):
            self.assertEqual(insights._http_url(bad), "", bad)
        self.assertEqual(insights._http_url("https://x.example.org/a?b=1"), "https://x.example.org/a?b=1")

    def test_fetch_refuses_bad_addresses_entities_and_huge_feeds(self):
        with self.assertRaises(ValueError):
            insights._fetch_rss("X", "file:///etc/passwd")
        evil = b'<?xml version="1.0"?><!DOCTYPE r [<!ENTITY a "aaaa">]><rss><channel><item><title>&a;</title></item></channel></rss>'
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(evil)):
            with self.assertRaises(ValueError):
                insights._fetch_rss("X", "https://x.example.org/feed")
        with mock.patch.object(insights, "_MAX_FEED_BYTES", 100), \
                mock.patch("urllib.request.urlopen", return_value=FakeResponse(RSS)):
            with self.assertRaises(ValueError):
                insights._fetch_rss("X", "https://x.example.org/feed")
        with mock.patch("urllib.request.urlopen", return_value=FakeResponse(RSS)):
            self.assertEqual(len(insights._fetch_rss("X", "https://x.example.org/feed")), 2)


class NewsPerCountryTest(Isolated):
    def setUp(self):
        super().setUp()
        cache.clear()
        self.addCleanup(cache.clear)
        self.put(GOOD)

    def fake(self, name, url, limit=10):
        if "journal" in url:
            raise OSError("feed down")
        return [{"title": f"{name} story {n}", "url": f"https://x.example.org/{n}", "source": name,
                 "published": "", "published_at": None} for n in range(2)]

    def test_feeds_country_first_then_international(self):
        f = insights.feeds("ZZ")
        self.assertEqual([x[0] for x in f[:2]], ["Example Times: markets", "Le Journal d'Exemple"])
        self.assertEqual(f[1][2:], ("fr", "ZZ"))
        self.assertEqual(len(f), 2 + len(insights.RSS_FEEDS))
        self.assertEqual(insights.feeds(""), insights.feeds("JP"))          # no file: international only

    def test_news_country_follows_the_settings(self):
        prefs.update({"country": "JP"})
        self.assertEqual(insights.news_country(), "JP")
        prefs.update({"news_country": "ZZ"})
        self.assertEqual(insights.news_country(), "ZZ")
        prefs.update({"country": "", "news_country": None})
        self.assertEqual(insights.news_country(), "")

    def test_one_failing_feed_never_empties_the_list(self):
        with mock.patch.object(insights, "_fetch_rss", self.fake):
            items = insights.headlines("ZZ")
        sources = [i["source"] for i in items]
        self.assertEqual(sources[0], "Example Times: markets")              # the country's feed first
        self.assertNotIn("Le Journal d'Exemple", sources)                   # the one that failed
        self.assertEqual(len(items), 2 * (1 + len(insights.RSS_FEEDS)))
        zz = next(i for i in items if i["source"] == "Example Times: markets")
        self.assertEqual((zz["language"], zz["country"]), ("en", "ZZ"))
        cnbc = next(i for i in items if i["source"] == "CNBC")
        self.assertEqual((cnbc["language"], cnbc["country"]), ("en", ""))

    def test_each_country_has_its_own_cache(self):
        with mock.patch.object(insights, "_fetch_rss", self.fake):
            zz = insights.headlines("ZZ")
            intl = insights.headlines("")
        self.assertNotEqual(len(zz), len(intl))
        self.assertFalse(any(i["country"] for i in intl))


# -------------------------------------------------------------------- API
try:
    from starlette.testclient import TestClient
except ImportError:      # pragma: no cover
    TestClient = None


@unittest.skipIf(TestClient is None, "starlette's test client is not available")
class ApiTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        from tests.test_mcp import sample_world
        sample_world(cls)
        from backend import main
        cls.client = TestClient(main.app, base_url="http://localhost:8000")

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        cache.clear()
        for p in (mock.patch.object(prefs, "DATA_DIR", Path(tmp.name)),
                  mock.patch.object(prefs, "_cache", [None, None]),
                  mock.patch.object(countries, "_state", {"sig": None, "files": {}, "problems": []}),
                  mock.patch.dict(os.environ, {countries.EXTRA_ENV: str(FIXTURES)})):
            p.start()
            self.addCleanup(p.stop)

    def test_countries_list(self):
        body = self.client.get("/api/countries").json()
        zz = next(c for c in body["countries"] if c["code"] == "ZZ")
        self.assertTrue(zz["has_overview"] and zz["has_news"])
        self.assertEqual(body["tax_cards"], ["GB", "US", "NL"])
        self.assertEqual(body["problems"], [])

    def test_country_overview(self):
        self.assertEqual(self.client.post("/api/prefs", json={"country": "ZZ"}).status_code, 200)
        ov = self.client.get("/api/country").json()
        self.assertEqual((ov["code"], ov["available"], ov["source"]), ("ZZ", True, "settings"))
        self.client.post("/api/prefs", json={"country": ""})
        self.assertIs(self.client.get("/api/country").json()["available"], False)
        jp = self.client.get("/api/country", params={"code": "JP"}).json()
        self.assertIn("Japan", jp["message"])
        self.assertEqual(self.client.get("/api/country", params={"code": "Japan"}).status_code, 400)

    def test_headlines_for_a_country(self):
        def fake(name, url, limit=10):
            return [{"title": f"{name} story", "url": "https://x.example.org/" + name.replace(" ", ""),
                     "source": name, "published": "", "published_at": None}]
        with mock.patch.object(insights, "_fetch_rss", fake):
            items = self.client.get("/api/headlines", params={"country": "ZZ"}).json()
            self.assertEqual(items[0]["country"], "ZZ")
            self.client.post("/api/prefs", json={"country": "ZZ"})
            self.assertEqual(self.client.get("/api/headlines").json()[0]["country"], "ZZ")
            intl = self.client.get("/api/headlines", params={"country": ""}).json()
        self.assertEqual({i["country"] for i in intl}, {""})
        self.assertEqual(self.client.get("/api/headlines", params={"country": "xyz"}).status_code, 400)


if __name__ == "__main__":
    unittest.main()
