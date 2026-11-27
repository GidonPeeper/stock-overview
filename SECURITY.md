# Security

Found a security problem? Please do not open a public issue. Report it
privately on GitHub: **Security → Report a vulnerability** on this
repository. We aim to reply within 7 days.

Examples of what to report:

- someone can read data, keys or statements they should not see;
- someone can sign in without the password, or get past the login limit;
- the app can be made to trade, move money or write to a broker account;
- another website can make changes in a running copy.

Please include the steps to reproduce it and the version (Settings → App
and data, or `/healthz`). Never send real account data or keys: use made-up
ones.

Fixes go into the newest version only. Update with `./run.sh update`.
