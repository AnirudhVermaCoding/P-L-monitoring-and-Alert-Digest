"""SQLite-backed memory: run history, daily results, manual overrides, and trends.

This is the "gets smarter over time" layer:
  - runs / daily_results   -> remember every past day so we can compute trends & streaks
  - overrides              -> human feedback (acknowledge, false-positive, per-FC target tweak)
  - trend helpers          -> rolling CM2%, consecutive-breach streaks, drift early-warning

Single access module with a lock so it's safe to call from Streamlit worker threads.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from pathlib import Path

import pandas as pd

REPO_ROOT = Path(__file__).resolve().parent.parent
DB_PATH = REPO_ROOT / "pnl_monitor.db"

_LOCK = threading.Lock()


def _conn(db_path: Path | str = DB_PATH) -> sqlite3.Connection:
    conn = sqlite3.connect(str(db_path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: Path | str = DB_PATH) -> None:
    with _LOCK, _conn(db_path) as conn:
        conn.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                run_id TEXT PRIMARY KEY,
                ts TEXT,
                source_file TEXT,
                status TEXT,
                summary_json TEXT
            );
            CREATE TABLE IF NOT EXISTS daily_results (
                run_id TEXT,
                fc TEXT,
                date TEXT,
                cm1_pct REAL,
                cm2_pct REAL,
                colour TEXT,
                anomalies_json TEXT,
                PRIMARY KEY (fc, date)
            );
            CREATE TABLE IF NOT EXISTS overrides (
                fc TEXT,
                date TEXT,
                line_item TEXT,
                action TEXT,
                note TEXT,
                min_val REAL,
                max_val REAL,
                ts TEXT
            );
            """
        )


def record_run(run_id: str, ts: str, source_file: str, status: str, summary: dict,
               db_path: Path | str = DB_PATH) -> None:
    with _LOCK, _conn(db_path) as conn:
        conn.execute(
            "INSERT OR REPLACE INTO runs VALUES (?,?,?,?,?)",
            (run_id, ts, source_file, status, json.dumps(summary)),
        )


def record_daily_results(run_id: str, pnl_df: pd.DataFrame, anomalies_df: pd.DataFrame,
                         db_path: Path | str = DB_PATH) -> None:
    """Upsert each day's headline metrics + its anomalies (keyed by FC+date)."""
    with _LOCK, _conn(db_path) as conn:
        for _, r in pnl_df.iterrows():
            day_anoms = anomalies_df[
                (anomalies_df["FC"] == r["FC"]) & (anomalies_df["Date"] == r["Date"])
            ]
            conn.execute(
                "INSERT OR REPLACE INTO daily_results VALUES (?,?,?,?,?,?,?)",
                (run_id, r["FC"], str(r["Date"]), float(r["CM1 %"]), float(r["CM2 %"]),
                 r["Colour"], day_anoms.to_json(orient="records")),
            )


def add_override(fc: str, date: str, line_item: str, action: str, ts: str,
                 note: str = "", min_val: float | None = None, max_val: float | None = None,
                 db_path: Path | str = DB_PATH) -> None:
    """Record a manual override. action in {acknowledge, false_positive, adjust_target}."""
    with _LOCK, _conn(db_path) as conn:
        conn.execute(
            "INSERT INTO overrides VALUES (?,?,?,?,?,?,?,?)",
            (fc, date, line_item, action, note, min_val, max_val, ts),
        )


def get_overrides(db_path: Path | str = DB_PATH) -> pd.DataFrame:
    with _LOCK, _conn(db_path) as conn:
        return pd.read_sql_query("SELECT * FROM overrides ORDER BY ts DESC", conn)


def get_history(db_path: Path | str = DB_PATH) -> pd.DataFrame:
    """All daily results ever recorded, oldest first."""
    with _LOCK, _conn(db_path) as conn:
        try:
            return pd.read_sql_query(
                "SELECT fc, date, cm1_pct, cm2_pct, colour FROM daily_results ORDER BY date",
                conn,
            )
        except Exception:  # noqa: BLE001 - table may not exist on first run
            return pd.DataFrame(columns=["fc", "date", "cm1_pct", "cm2_pct", "colour"])


def list_runs(db_path: Path | str = DB_PATH) -> pd.DataFrame:
    with _LOCK, _conn(db_path) as conn:
        try:
            return pd.read_sql_query("SELECT * FROM runs ORDER BY ts DESC", conn)
        except Exception:  # noqa: BLE001
            return pd.DataFrame()


# ---------------------------------------------------------------------------
# Trend helpers — computed from history so digests can say more than "today".
# ---------------------------------------------------------------------------

def rolling_cm2(history: pd.DataFrame, fc: str, window: int = 7) -> float | None:
    """Mean CM2% over the last `window` recorded days for an FC."""
    h = history[history["fc"] == fc].sort_values("date")
    if h.empty:
        return None
    return round(h["cm2_pct"].tail(window).mean(), 2)


def breach_streak(history: pd.DataFrame, fc: str) -> int:
    """How many consecutive most-recent days this FC has been Red or Yellow."""
    h = history[history["fc"] == fc].sort_values("date")
    streak = 0
    for colour in reversed(h["colour"].tolist()):
        if colour in ("Red", "Yellow"):
            streak += 1
        else:
            break
    return streak


def trend_note(history: pd.DataFrame, fc: str) -> str:
    """A short human sentence about this FC's recent trajectory (empty if no history)."""
    if history.empty or fc not in set(history["fc"]):
        return ""
    avg = rolling_cm2(history, fc)
    streak = breach_streak(history, fc)
    parts = []
    if avg is not None:
        parts.append(f"7-day avg CM2 {avg:.1f}%")
    if streak >= 2:
        parts.append(f"{streak} consecutive non-green days — consider escalation")
    return "; ".join(parts)
