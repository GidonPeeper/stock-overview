import os

# No network in tests: unknown ISINs are not looked up on Yahoo (instruments.py).
os.environ.setdefault("TICKER_LOOKUP", "0")
