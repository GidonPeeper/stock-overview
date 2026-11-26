import os

# No network in tests: unknown ISINs are not looked up on Yahoo (instruments.py).
os.environ.setdefault("TICKER_LOOKUP", "0")
os.environ.setdefault("PRICE_GAPS", "0")      # no hourly-price requests (backend/closes.py)
