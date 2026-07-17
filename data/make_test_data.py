"""Generate extended multi-FC test data covering all four required scenarios.

Run: `python -m data.make_test_data`
Produces: data/extended_test_data.csv

The brief requires a run that demonstrates at least: one clean day, one CM2 breach,
one line-item-only breach, and one suspiciously-high CM2 (Blue). The sample workbook has
no Blue day, so we build a second FC (FC-Mumbai) with hand-designed days and VERIFY each
one with the real engine before writing — the generator fails loudly if a scenario drifts.
FC-Delhi replays the 100 sample days against real calendar dates ending today.
"""
from __future__ import annotations

import sys
from datetime import date, timedelta
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import LINE_ITEMS, load_config
from core.engine import compute_pnl, detect_anomalies
from core.loader import load_pnl

REPO_ROOT = Path(__file__).resolve().parent.parent
SAMPLE = REPO_ROOT / "data" / "Case Study 3.xlsx"
OUT = REPO_ROOT / "data" / "extended_test_data.csv"
RUN_DATE = date(2026, 7, 17)  # fixed for reproducibility (no Date.now in this codebase)


def _row(fc: str, d: date, rev: float, pcts: dict[str, float]) -> dict:
    """Build a raw data row from revenue and per-line-item % of revenue."""
    row = {"Date": d.isoformat(), "FC": fc, "Revenue": int(rev)}
    for item in LINE_ITEMS:
        row[item] = int(round(pcts[item] / 100 * rev))
    return row


def _delhi_rows() -> list[dict]:
    """Replay the 100 sample days as FC-Delhi ending on RUN_DATE."""
    df = load_pnl(SAMPLE)
    n = len(df)
    rows = []
    for i, (_, r) in enumerate(df.iterrows()):
        d = RUN_DATE - timedelta(days=(n - 1 - i))
        row = {"Date": d.isoformat(), "FC": "FC-Delhi", "Revenue": int(r["Revenue"])}
        for item in LINE_ITEMS:
            row[item] = int(r[item])
        rows.append(row)
    return rows


def _mumbai_rows() -> list[dict]:
    """Hand-designed FC-Mumbai days, one per required scenario (plus filler)."""
    rev = 100000
    start = RUN_DATE - timedelta(days=13)

    # Scenario percentages (of revenue). Verified below by the engine.
    scenarios = {
        # CLEAN: every line mid-range, CM2 = 100-26-6-11-16.5-6.5-11 = 23% (Green).
        "clean": {"Manpower": 26, "Packaging": 6, "Power and Fuel": 11,
                  "FC Rent": 16.5, "Equipment Rentals": 6.5, "Overheads": 11},
        # LINE-ITEM ONLY: Packaging 8.5% (above 7 max) but everything else in range and
        # CM2 = 100-26-8.5-11-16-6-11 = 21.5% (Green) -> a breach with a healthy margin.
        "line_only": {"Manpower": 26, "Packaging": 8.5, "Power and Fuel": 11,
                      "FC Rent": 16, "Equipment Rentals": 6, "Overheads": 11},
        # CM2 BREACH (Red): costs bloated so CM2 = 100-31-9-14-19-9-14 = 4% (Red, <12).
        "cm2_breach": {"Manpower": 31, "Packaging": 9, "Power and Fuel": 14,
                       "FC Rent": 19, "Equipment Rentals": 9, "Overheads": 14},
        # BLUE suspicious: costs understated so CM2 = 100-24-4-8-13-4-8 = 39% (Blue, >33).
        "blue": {"Manpower": 24, "Packaging": 4, "Power and Fuel": 8,
                 "FC Rent": 13, "Equipment Rentals": 4, "Overheads": 8},
    }

    rows = []
    # Interleave scenario days with a few extra clean-ish filler days for realism.
    day_plan = ["clean", "line_only", "clean", "cm2_breach", "clean", "blue", "clean"]
    for i, key in enumerate(day_plan):
        d = start + timedelta(days=i * 2)
        rows.append(_row("FC-Mumbai", d, rev, scenarios[key]))
    return rows, scenarios


def _verify_scenarios(all_rows: list[dict]) -> None:
    """Assert the four required scenarios are actually present in FC-Mumbai output."""
    cfg = load_config()
    df = pd.DataFrame(all_rows)
    mum = df[df["FC"] == "FC-Mumbai"].reset_index(drop=True)
    pnl = compute_pnl(mum, cfg)
    anoms = detect_anomalies(pnl, cfg)

    colours = set(pnl["Colour"])
    statuses = set(anoms["Status"])

    # 1 clean day: at least one Mumbai day with zero anomalies.
    anom_dates = set(anoms["Date"])
    clean_days = [r["Date"] for _, r in pnl.iterrows() if r["Date"] not in anom_dates]
    assert clean_days, "No clean day generated!"

    # CM2 breach (Red).
    assert "Red" in colours, f"No Red/CM2-breach day. Colours={colours}"
    assert "CM2_BREACH" in statuses, f"No CM2_BREACH status. Statuses={statuses}"

    # Line-item-only breach: a day flagged for a line item but NOT for CM2, and Green.
    line_only_ok = False
    for d in anom_dates:
        day_anoms = anoms[anoms["Date"] == d]
        day_colour = pnl[pnl["Date"] == d]["Colour"].iloc[0]
        items = set(day_anoms["Line Item"])
        if day_colour == "Green" and "CM2%" not in items and "CM1%" not in items:
            line_only_ok = True
            break
    assert line_only_ok, "No line-item-only breach day (breach with healthy Green CM2)."

    # Blue suspicious.
    assert "Blue" in colours, f"No Blue day. Colours={colours}"
    assert "SUSPICIOUS_HIGH" in statuses, f"No SUSPICIOUS_HIGH status. Statuses={statuses}"

    print("Scenario verification passed:")
    print(f"  clean day(s):      {clean_days}")
    print(f"  Red / CM2 breach:  {sorted(pnl[pnl['Colour']=='Red']['Date'])}")
    print(f"  Blue / suspicious: {sorted(pnl[pnl['Colour']=='Blue']['Date'])}")


def main() -> None:
    delhi = _delhi_rows()
    mumbai, _ = _mumbai_rows()
    all_rows = delhi + mumbai
    _verify_scenarios(all_rows)

    df = pd.DataFrame(all_rows)[["Date", "FC", "Revenue"] + LINE_ITEMS]
    df = df.sort_values(["FC", "Date"]).reset_index(drop=True)
    df.to_csv(OUT, index=False)
    print(f"\nWrote {len(df)} rows across {df['FC'].nunique()} FCs -> {OUT}")


if __name__ == "__main__":
    main()
