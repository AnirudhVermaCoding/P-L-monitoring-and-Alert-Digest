"""Deterministic P&L computation and anomaly detection. No I/O, no LLM, no Streamlit.

All the numbers the business cares about are computed here so they are auditable and
reproducible. The LLM layer (agent/) only *phrases* these facts, never computes them.
"""
from __future__ import annotations

import pandas as pd

from core.config import LINE_ITEMS, Config

# Costs that sit "below the CM1 line".
CM1_COSTS = ["Manpower", "Packaging"]
# Costs subtracted between CM1 and CM2.
CM2_COSTS = ["Power and Fuel", "FC Rent", "Equipment Rentals", "Overheads"]


def colour_for_cm2(cm2_pct: float, colors: dict) -> str:
    """Map CM2% to the daily colour code (see brief's rules)."""
    if cm2_pct < colors["red_below"]:
        return "Red"
    if cm2_pct < colors["yellow_low"]:
        return "Yellow"
    if cm2_pct <= colors["green_high"]:
        return "Green"
    if cm2_pct <= colors["yellow_high"]:
        return "Yellow"
    return "Blue"


def compute_pnl(df: pd.DataFrame, config: Config) -> pd.DataFrame:
    """Return a copy of df with each cost as % of revenue, CM1, CM1%, CM2, CM2%, Colour."""
    out = df.copy()
    rev = out["Revenue"]

    for item in LINE_ITEMS:
        out[f"{item} %"] = (out[item] / rev * 100).round(2)

    out["CM1"] = (rev - out[CM1_COSTS].sum(axis=1)).round(2)
    out["CM1 %"] = (out["CM1"] / rev * 100).round(2)
    out["CM2"] = (out["CM1"] - out[CM2_COSTS].sum(axis=1)).round(2)
    out["CM2 %"] = (out["CM2"] / rev * 100).round(2)

    out["Colour"] = out["CM2 %"].apply(lambda v: colour_for_cm2(v, config.colors))
    return out


def _margin_bounds(config: Config, key: str) -> tuple[float, float]:
    m = config.margins[key]
    tol = m.get("tolerance_pp", 3)
    return m["min"] - tol, m["max"] + tol


def detect_anomalies(pnl_df: pd.DataFrame, config: Config) -> pd.DataFrame:
    """Return the flagged-anomalies log.

    Columns: Date | FC | Line Item | % of Revenue | Target Range | Status.
    Status is one of BELOW_MIN, ABOVE_MAX, CM1_BREACH, CM2_BREACH, SUSPICIOUS_HIGH.
    """
    rows: list[dict] = []
    cm1_lo, cm1_hi = _margin_bounds(config, "cm1")
    cm2_lo, cm2_hi = _margin_bounds(config, "cm2")

    for _, r in pnl_df.iterrows():
        date, fc = r["Date"], r["FC"]

        for item in LINE_ITEMS:
            pct = r[f"{item} %"]
            t = config.targets[item]
            status = None
            if pct < t["min"]:
                status = "BELOW_MIN"
            elif pct > t["max"]:
                status = "ABOVE_MAX"
            if status:
                rows.append({
                    "Date": date, "FC": fc, "Line Item": item,
                    "% of Revenue": round(pct, 2),
                    "Target Range": config.target_range_str(item),
                    "Status": status,
                })

        cm1 = r["CM1 %"]
        if cm1 < cm1_lo or cm1 > cm1_hi:
            rows.append({
                "Date": date, "FC": fc, "Line Item": "CM1%",
                "% of Revenue": round(cm1, 2),
                "Target Range": f"{config.margins['cm1']['min']:g}-{config.margins['cm1']['max']:g}%",
                "Status": "CM1_BREACH",
            })

        cm2 = r["CM2 %"]
        if cm2 < cm2_lo:
            rows.append({
                "Date": date, "FC": fc, "Line Item": "CM2%",
                "% of Revenue": round(cm2, 2),
                "Target Range": f"{config.margins['cm2']['min']:g}-{config.margins['cm2']['max']:g}%",
                "Status": "CM2_BREACH",
            })
        elif cm2 > cm2_hi:
            rows.append({
                "Date": date, "FC": fc, "Line Item": "CM2%",
                "% of Revenue": round(cm2, 2),
                "Target Range": f"{config.margins['cm2']['min']:g}-{config.margins['cm2']['max']:g}%",
                "Status": "SUSPICIOUS_HIGH",
            })

    cols = ["Date", "FC", "Line Item", "% of Revenue", "Target Range", "Status"]
    return pd.DataFrame(rows, columns=cols)


def contributors_for_day(pnl_row: pd.Series, config: Config) -> list[dict]:
    """Rank line items by how far outside their band they are, weighted by revenue.

    Impact (in currency) = deviation in percentage points * revenue / 100. Sorted desc.
    Used to name the "top contributors" in insights.
    """
    rev = pnl_row["Revenue"]
    out = []
    for item in LINE_ITEMS:
        pct = pnl_row[f"{item} %"]
        t = config.targets[item]
        if pct > t["max"]:
            dev = pct - t["max"]
            direction = "above max"
        elif pct < t["min"]:
            dev = t["min"] - pct
            direction = "below min"
        else:
            continue
        out.append({
            "line_item": item,
            "pct": round(pct, 2),
            "target": config.target_range_str(item),
            "deviation_pp": round(dev, 2),
            "direction": direction,
            "impact_value": round(dev * rev / 100, 0),
        })
    out.sort(key=lambda d: d["impact_value"], reverse=True)
    return out
