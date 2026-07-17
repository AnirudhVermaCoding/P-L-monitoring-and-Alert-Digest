"""Hand-checked tests for the deterministic engine. Run: `python -m tests.test_engine`."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import load_config
from core.engine import colour_for_cm2, compute_pnl, contributors_for_day, detect_anomalies


def approx(a: float, b: float, tol: float = 0.05) -> bool:
    return abs(a - b) <= tol


def test_day1_sample():
    """Sample Day 1: hand-computed CM1/CM2 and line-item breaches."""
    cfg = load_config()
    df = pd.DataFrame([{
        "Date": "Day 1", "FC": "FC-1", "Revenue": 112453,
        "Manpower": 34221, "Packaging": 8769, "Power and Fuel": 14467,
        "FC Rent": 16846, "Equipment Rentals": 5034, "Overheads": 13134,
    }])
    pnl = compute_pnl(df, cfg)
    r = pnl.iloc[0]
    assert r["CM1"] == 69463, r["CM1"]
    assert approx(r["CM1 %"], 61.77), r["CM1 %"]
    assert r["CM2"] == 19982, r["CM2"]
    assert approx(r["CM2 %"], 17.77), r["CM2 %"]
    assert r["Colour"] == "Green", r["Colour"]

    anoms = detect_anomalies(pnl, cfg)
    got = dict(zip(anoms["Line Item"], anoms["Status"]))
    assert got.get("Manpower") == "ABOVE_MAX", got
    assert got.get("Packaging") == "ABOVE_MAX", got
    assert got.get("Power and Fuel") == "ABOVE_MAX", got
    assert got.get("FC Rent") == "BELOW_MIN", got
    assert got.get("Equipment Rentals") == "BELOW_MIN", got
    assert got.get("CM1%") == "CM1_BREACH", got  # 61.77 < 62
    assert "Overheads" not in got, got  # 11.68% is in range
    print("PASS test_day1_sample")


def test_clean_day():
    """A day with every line mid-range and CM2 healthy -> no anomalies, Green."""
    cfg = load_config()
    rev = 100000
    df = pd.DataFrame([{
        "Date": "clean", "FC": "FC-x", "Revenue": rev,
        "Manpower": 0.26 * rev, "Packaging": 0.06 * rev, "Power and Fuel": 0.11 * rev,
        "FC Rent": 0.165 * rev, "Equipment Rentals": 0.065 * rev, "Overheads": 0.11 * rev,
    }])
    pnl = compute_pnl(df, cfg)
    assert pnl.iloc[0]["Colour"] == "Green"
    assert approx(pnl.iloc[0]["CM2 %"], 23.0)
    assert len(detect_anomalies(pnl, cfg)) == 0
    print("PASS test_clean_day")


def test_blue_suspicious():
    """Understated costs -> CM2 > 33 -> Blue + SUSPICIOUS_HIGH."""
    cfg = load_config()
    rev = 100000
    df = pd.DataFrame([{
        "Date": "blue", "FC": "FC-x", "Revenue": rev,
        "Manpower": 0.24 * rev, "Packaging": 0.04 * rev, "Power and Fuel": 0.08 * rev,
        "FC Rent": 0.13 * rev, "Equipment Rentals": 0.04 * rev, "Overheads": 0.08 * rev,
    }])
    pnl = compute_pnl(df, cfg)
    assert pnl.iloc[0]["Colour"] == "Blue", pnl.iloc[0]["CM2 %"]
    statuses = set(detect_anomalies(pnl, cfg)["Status"])
    assert "SUSPICIOUS_HIGH" in statuses, statuses
    print("PASS test_blue_suspicious")


def test_colour_bands():
    colors = load_config().colors
    assert colour_for_cm2(9.6, colors) == "Red"
    assert colour_for_cm2(13.0, colors) == "Yellow"
    assert colour_for_cm2(20.0, colors) == "Green"
    assert colour_for_cm2(31.5, colors) == "Yellow"
    assert colour_for_cm2(36.0, colors) == "Blue"
    print("PASS test_colour_bands")


def test_contributors_sorted():
    cfg = load_config()
    df = pd.DataFrame([{
        "Date": "d", "FC": "f", "Revenue": 100000,
        "Manpower": 33000, "Packaging": 6000, "Power and Fuel": 15000,
        "FC Rent": 16000, "Equipment Rentals": 6000, "Overheads": 11000,
    }])
    pnl = compute_pnl(df, cfg)
    contribs = contributors_for_day(pnl.iloc[0], cfg)
    # Manpower (33% vs 27 max = 6pp) should outrank Power&Fuel (15% vs 12 = 3pp).
    assert contribs[0]["line_item"] == "Manpower", contribs
    assert contribs[0]["impact_value"] >= contribs[1]["impact_value"]
    print("PASS test_contributors_sorted")


def run_all():
    test_day1_sample()
    test_clean_day()
    test_blue_suspicious()
    test_colour_bands()
    test_contributors_sorted()
    print("\nALL ENGINE TESTS PASSED")


if __name__ == "__main__":
    run_all()
