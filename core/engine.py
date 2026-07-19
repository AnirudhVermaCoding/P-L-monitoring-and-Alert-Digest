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

    Columns: Date | FC | Line Item | % of Revenue | Target Range | Status | Colour.
    Status is one of BELOW_MIN, ABOVE_MAX, CM1_BREACH, CM2_BREACH, SUSPICIOUS_HIGH.
    Colour is the FC's daily CM2 colour for that date, so each breach carries the day's
    overall severity (Red/Yellow/Green/Blue) alongside it.
    """
    rows: list[dict] = []
    cm1_lo, cm1_hi = _margin_bounds(config, "cm1")
    cm2_lo, cm2_hi = _margin_bounds(config, "cm2")

    for _, r in pnl_df.iterrows():
        date, fc, colour = r["Date"], r["FC"], r["Colour"]

        for item in LINE_ITEMS:
            pct = r[f"{item} %"]
            t = config.target_for(fc, item)
            status = None
            if pct < t["min"]:
                status = "BELOW_MIN"
            elif pct > t["max"]:
                status = "ABOVE_MAX"
            if status:
                rows.append({
                    "Date": date, "FC": fc, "Line Item": item,
                    "% of Revenue": round(pct, 2),
                    "Target Range": config.target_range_str(item, fc),
                    "Status": status, "Colour": colour,
                })

        cm1 = r["CM1 %"]
        if cm1 < cm1_lo or cm1 > cm1_hi:
            rows.append({
                "Date": date, "FC": fc, "Line Item": "CM1%",
                "% of Revenue": round(cm1, 2),
                "Target Range": f"{config.margins['cm1']['min']:g}-{config.margins['cm1']['max']:g}%",
                "Status": "CM1_BREACH", "Colour": colour,
            })

        cm2 = r["CM2 %"]
        if cm2 < cm2_lo:
            rows.append({
                "Date": date, "FC": fc, "Line Item": "CM2%",
                "% of Revenue": round(cm2, 2),
                "Target Range": f"{config.margins['cm2']['min']:g}-{config.margins['cm2']['max']:g}%",
                "Status": "CM2_BREACH", "Colour": colour,
            })
        elif cm2 > cm2_hi:
            rows.append({
                "Date": date, "FC": fc, "Line Item": "CM2%",
                "% of Revenue": round(cm2, 2),
                "Target Range": f"{config.margins['cm2']['min']:g}-{config.margins['cm2']['max']:g}%",
                "Status": "SUSPICIOUS_HIGH", "Colour": colour,
            })

    cols = ["Date", "FC", "Line Item", "% of Revenue", "Target Range", "Status", "Colour"]
    return pd.DataFrame(rows, columns=cols)


def contributors_for_day(pnl_row: pd.Series, config: Config) -> list[dict]:
    """Rank line items by how far outside their band they are, weighted by revenue.

    Impact (in currency) = deviation in percentage points * revenue / 100. Sorted desc.
    Used to name the "top contributors" in insights.
    """
    rev = pnl_row["Revenue"]
    fc = str(pnl_row["FC"])
    out = []
    for item in LINE_ITEMS:
        pct = pnl_row[f"{item} %"]
        t = config.target_for(fc, item)
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
            "target": config.target_range_str(item, fc),
            "deviation_pp": round(dev, 2),
            "direction": direction,
            "impact_value": round(dev * rev / 100, 0),
        })
    out.sort(key=lambda d: d["impact_value"], reverse=True)
    return out


def recommended_action(colour: str, has_breach: bool = False) -> str:
    """One crisp next-step. Single source of truth for the phrasing used by both the
    plain-language insight (agent/insights.py) and the Simple view (app.py).

    A Green day can still carry a line-item breach (healthy CM2, one cost over its band);
    has_breach keeps that case from reading "No action needed" while a breach is on screen.
    """
    if colour == "Red":
        return "Urgent cost audit of the top contributor."
    if colour == "Blue":
        return "Cost-completeness audit — check for missing invoices."
    if colour == "Green" and not has_breach:
        return "No action needed."
    return "Line-item owner to review the flagged cost."


def latest_status_by_fc(pnl_df: pd.DataFrame, insights: list[dict]) -> list[dict]:
    """One plain-English status row per FC, for that FC's latest date. Pure; no I/O, no Streamlit.

    Built for the non-technical Simple view: every FC is shown (healthy ones included), so the
    healthy days — which have no insight entry — are derived from pnl_df directly.

    Returns list of dicts: FC, Date, Colour, cm2_pct, main_reason, largest_breach,
    recommended_action.
    """
    # Latest row per FC. ISO date strings sort correctly; fall back to as-is order otherwise.
    latest = pnl_df.sort_values("Date").groupby("FC", sort=False).tail(1)

    # Index the anomaly-only insights by (FC, Date) so healthy FCs simply miss the lookup.
    facts_by_key = {(i["FC"], i["Date"]): i["facts"] for i in insights}

    rows: list[dict] = []
    for _, r in latest.iterrows():
        fc, date, colour = r["FC"], r["Date"], r["Colour"]
        cm2 = float(r["CM2 %"])
        facts = facts_by_key.get((fc, date))

        if colour == "Red":
            main_reason = f"Margin compressed: CM2 {cm2:.1f}% is below the 15% target."
        elif colour == "Blue":
            main_reason = f"Suspiciously high CM2 {cm2:.1f}% — possible under-reported costs."
        elif colour == "Yellow":
            main_reason = f"Borderline: CM2 {cm2:.1f}% is near the edge of the healthy band."
        else:  # Green
            main_reason = f"Healthy: CM2 {cm2:.1f}%, all costs within range."

        top = facts["top_contributors"] if facts and facts["top_contributors"] else []
        if top:
            c = top[0]
            largest_breach = (f"{c['line_item']} {c['pct']:.1f}% "
                              f"(target {c['target']}, {c['direction']} {c['deviation_pp']:.1f}pp)")
        else:
            largest_breach = "None"

        rows.append({
            "FC": fc,
            "Date": date,
            "Colour": colour,
            "cm2_pct": round(cm2, 2),
            "main_reason": main_reason,
            "largest_breach": largest_breach,
            "recommended_action": recommended_action(colour, has_breach=bool(top)),
        })
    return rows
