# Country files

One file per country: `backend/countries/<CC>.json`, where `<CC>` is the
two-letter ISO code in capitals (`GB` for the United Kingdom, not `UK`). The
app shows a file's content as "How tax on investments works in <country>" and
uses its settings and news feeds when the user picks that country.

These files are data. No code change is needed to add a country.

## Rules for the content

- **Private investors only**: a person who lives in the country and holds
  shares, funds (ETFs), bonds, savings and perhaps crypto in their own name.
  Not companies, not professional traders.
- **Every figure has an official source.** A rate, an allowance, a threshold
  or a date is only written down when it was read on the tax authority's or
  government's own site, or in the law, on the day in `checked_on`. Put that
  page in the topic's `sources`. If only a bank, broker, newspaper or advice
  site says it, either leave it out or put the point in `unverified` with
  that source.
- **Say which year.** Use the current tax year on `checked_on`. Changes that
  are announced but not yet in force go in `upcoming`, never in `topics`.
- **Plain words.** Short sentences a non-specialist understands. Write in
  English. Give the local name of a thing once, in brackets: "the tax-free
  amount (heffingsvrij vermogen)". No advice ("you should"): describe the rule.
- **Amounts** in the country's own currency, written as the country writes
  them is not needed: use `€57,684`, `£3,000`, `$3,000`, `CHF 2,000`.
- **Leave out what you could not confirm.** A shorter, correct file is better
  than a complete, doubtful one. List gaps in `not_covered`.

## Fields

```jsonc
{
  "code": "NL",                       // same as the file name
  "name": "Netherlands",              // English name
  "currency": "EUR",                  // suggested display currency: EUR, GBP or USD
                                      // (the app shows amounts in these three only;
                                      // for another currency pick the closest and say so in currency_note)
  "currency_note": "",                // e.g. "Switzerland uses francs; the app shows euros."
  "timezone": "Europe/Amsterdam",     // IANA zone of the capital or main financial centre
  "languages": ["nl"],                // main language codes, most used first
  "checked_on": "2026-11-26",         // the day the sources were read
  "tax_year": {
    "label": "2026",                  // the year the figures below are for ("2026/27" for the UK)
    "starts": "01-01",                // MM-DD
    "ends": "12-31",                  // MM-DD
    "return_due": "File between 1 March and 1 May of the next year.",   // one sentence
    "source": "https://…"
  },
  "summary": "Two or three sentences: the system in a nutshell.",
  "topics": [                         // only topics that apply; use these ids, in this order
    {
      "id": "gains",                  // gains | dividends | interest | funds | wealth | allowances |
                                      // accounts | losses | foreign | reporting | crypto
      "title": "Profit when you sell",
      "points": ["One rule per item, a sentence or two."],
      "figures": [                    // optional: the numbers a reader looks for
        {"label": "Tax rate", "value": "26.375%"}
      ],
      "sources": ["https://…"]        // at least one official page per topic
    }
  ],
  "dates": [                          // optional: key dates in a year
    {"when": "05-01", "what": "Last day to file the return for the year before."}
  ],
  "official": [                       // 2 to 6 links to start from
    {"title": "Belastingdienst: box 3", "url": "https://…"}
  ],
  "upcoming": ["Announced, not yet law or not yet in force: one sentence each, with the date it would start."],
  "unverified": [                     // optional: useful, but not confirmed on an official page
    {"point": "…", "source": "https://…"}
  ],
  "not_covered": ["What this page leaves out, e.g. real estate, pensions, moving abroad."],
  "news": [                           // 2 to 5 RSS or Atom feeds on markets and personal finance
    {"name": "NOS Economie", "url": "https://…", "language": "nl"}
  ]
}
```

Topic ids, in order, and what belongs in each:

| id | Title to use | Content |
|---|---|---|
| `gains` | Profit when you sell | How a gain on shares and funds is taxed; holding periods |
| `dividends` | Dividends | Rate, who withholds it, how it is settled |
| `interest` | Interest on savings | Rate, allowance, who withholds |
| `funds` | Funds and ETFs | Special rules: accumulating funds, deemed income, fund status |
| `wealth` | Tax on what you own | A wealth tax or a deemed-return system |
| `allowances` | Tax-free amounts | Yearly exemptions and how to use them |
| `accounts` | Tax-free and tax-deferred accounts | ISA, PEA, 401(k), pension accounts: limits and rules |
| `losses` | Losses | Offsetting, carrying forward, wash-sale style rules |
| `foreign` | Foreign shares | Withholding tax abroad and how to get credit for it |
| `reporting` | What your broker does and what you do | What a local or a foreign broker reports and withholds; what goes in the return |
| `crypto` | Crypto | Only if the authority has published its treatment |

## News feeds

Each feed must be an RSS or Atom address that answers without a login and had
items from the last week when checked. Prefer the public broadcaster's
economy feed, the main financial daily and one investor site. No paywalled
feeds that show only headlines behind a login wall, and no feeds of a broker
or bank selling products.
