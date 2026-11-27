# Contributing

Fixes and new brokers are welcome. Open an issue first for anything large.

## Run it

```bash
./run.sh                                               # the app, with sample data
.venv/bin/python -m unittest discover -s tests -t .    # the tests (no network needed)
```

## Rules

- **No personal data, ever.** Not in code, tests, docs, screenshots or issues.
  Use made-up names and numbers. The `data` folder is git-ignored: keep it so.
- **Read-only.** The app never trades or moves money. Do not add a connector
  that needs trading permissions.
- **Say only what is checked.** Steps in `backend/broker_catalog.json` come
  from the broker's own help pages. Mark anything else `"verified": false`
  and give the reason in `notes`.
- **Tests for every change**, using small made-up files. Tests must not call
  the internet.
- No build step in `frontend/` and no new dependency without a good reason.

## Add a broker

| You have | Do this |
|---|---|
| New or better steps for a broker | Edit its entry in `backend/broker_catalog.json`. No code needed |
| A broker with an API | Write `backend/connectors/<name>.py` with `fetch(creds, conn)`, then add it to `CONNECTORS` and `LINKS` in `backend/connectors/registry.py` |
| A statement file to read | Write `backend/importers/<name>.py` and link it in `registry.py` |

The top of `backend/connectors/registry.py` explains how the catalog is linked
to code.

## Add your country

| You have | Do this |
|---|---|
| How tax on investments works in your country, from official sources, or good news feeds | Write `backend/countries/<CC>.json` as `backend/countries/SCHEMA.md` describes. No code needed. Try it first with `COUNTRIES_EXTRA_DIR=<folder with your file> ./run.sh`; the tests check every file |

## Report a security problem

Do not open a public issue. Use GitHub's **Security → Report a vulnerability**
on this repo. See [SECURITY.md](SECURITY.md).

## Licence of your contribution

Stock Overview is licensed under [AGPL-3.0](LICENSE). By opening a pull
request you confirm that:

- the contribution is your own work, or you have the right to submit it;
- it is licensed under AGPL-3.0; and
- you also allow Gidon Peeper to license it under other terms, for example
  for a paid hosted version that helps pay for the project.

If you do not agree, say so in the pull request before it is merged.
