"""History store for the profit/value trendline.

A tiny SQLite database holding one row per day: total value, total cost and
profit. We record a snapshot whenever the portfolio is computed (deduped per
day, last write wins), and the dashboard reads the series back for the chart.

In demo mode we seed ~60 days of synthetic history on first run so the trendline
isn't empty before you've used the app for a while.
"""

from __future__ import annotations

import math
import sqlite3
from datetime import date, timedelta
from pathlib import Path

DATA_DIR = Path(__file__).resolve().parents[1] / "data"
DB_PATH = DATA_DIR / "history.db"


def _conn() -> sqlite3.Connection:
    DATA_DIR.mkdir(exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS snapshots (
            day         TEXT PRIMARY KEY,
            total_value REAL NOT NULL,
            total_cost  REAL NOT NULL,
            profit      REAL NOT NULL,
            realized    REAL NOT NULL DEFAULT 0
        )
        """
    )
    return conn


def record_snapshot(total_value: float, total_cost: float,
                    realized: float = 0.0, day: str | None = None) -> None:
    day = day or date.today().isoformat()
    unrealized = total_value - total_cost
    conn = _conn()
    with conn:
        conn.execute(
            "INSERT INTO snapshots (day, total_value, total_cost, profit, realized) "
            "VALUES (?, ?, ?, ?, ?) "
            "ON CONFLICT(day) DO UPDATE SET "
            "total_value=excluded.total_value, "
            "total_cost=excluded.total_cost, "
            "profit=excluded.profit, "
            "realized=excluded.realized",
            (day, total_value, total_cost, unrealized, realized),
        )
    conn.close()


def get_history() -> list[dict]:
    conn = _conn()
    rows = conn.execute(
        "SELECT day, total_value, total_cost, profit, realized FROM snapshots ORDER BY day"
    ).fetchall()
    conn.close()
    return [
        # profit = unrealized (value-cost); total_profit = unrealized + realized
        {"day": d, "total_value": v, "total_cost": c,
         "unrealized": p, "realized": r, "total_profit": round(p + r, 2)}
        for (d, v, c, p, r) in rows
    ]


def seed_demo_history(total_value: float, total_cost: float, days: int = 60) -> None:
    """Synthesize a plausible past so the demo trendline looks real. No-op if
    any history already exists."""
    conn = _conn()
    existing = conn.execute("SELECT COUNT(*) FROM snapshots").fetchone()[0]
    conn.close()
    if existing:
        return

    today = date.today()
    for i in range(days, -1, -1):
        d = today - timedelta(days=i)
        progress = (days - i) / days  # 0 -> 1 toward today
        # Gentle upward drift from cost basis to today's value, plus a wave.
        base = total_cost + (total_value - total_cost) * progress
        wobble = math.sin(i / 5.0) * (total_value * 0.01)
        value = base + wobble
        record_snapshot(round(value, 2), round(total_cost, 2), day=d.isoformat())
