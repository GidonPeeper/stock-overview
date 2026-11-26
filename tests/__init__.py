import logging
import os

# No network in tests: unknown ISINs are not looked up on Yahoo (instruments.py).
os.environ.setdefault("TICKER_LOOKUP", "0")
os.environ.setdefault("PRICE_GAPS", "0")      # no hourly-price requests (backend/closes.py)

# The app logs a warning whenever a source fails (the signal on a real server).
# Tests make sources fail on purpose; keep their output readable.
logging.getLogger("stock_overview").setLevel(logging.ERROR)
