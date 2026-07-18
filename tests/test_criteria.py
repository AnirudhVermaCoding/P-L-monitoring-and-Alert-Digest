"""Tests for adaptive criteria parsing. Run: `python -m tests.test_criteria`."""
from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.criteria import extract_criteria, parse_criteria_frame

REPO = Path(__file__).resolve().parent.parent
SAMPLE = REPO / "data" / "Case Study 3.xlsx"


def test_extract_from_two_sheet_workbook():
    got = extract_criteria(SAMPLE)
    assert got is not None, "criteria not found in the sample workbook"
    assert got["Manpower"] == {"min": 25.0, "max": 27.0}, got["Manpower"]
    assert got["Packaging"] == {"min": 5.0, "max": 7.0}, got["Packaging"]
    assert len(got) == 6, got
    print("PASS test_extract_from_two_sheet_workbook")


def test_data_sheet_not_mistaken_for_criteria():
    xl = pd.ExcelFile(SAMPLE)
    data = parse_criteria_frame(xl.parse("Day Wise PnL", header=None))
    assert data is None, f"data sheet wrongly parsed as criteria: {data}"
    print("PASS test_data_sheet_not_mistaken_for_criteria")


def test_alt_spellings_and_percent_between():
    csv = ("Item,Range\n"
           "Manpower Cost,25% - 27%\n"      # % between numbers
           "Power & Fuel,10 to 12\n"        # 'to' + ampersand spelling
           "Packaging,5-7%\n"
           "FC Rent,15 – 18\n"              # en dash
           "Equipment,5-8\n"
           "Overhead,10-12\n")
    p = REPO / "data" / "bad_inputs" / "_criteria_test.csv"
    p.write_text(csv, encoding="utf-8")
    got = extract_criteria(p)
    assert got is not None and len(got) == 6, got
    assert got["Manpower"] == {"min": 25.0, "max": 27.0}, got["Manpower"]
    assert got["Power and Fuel"] == {"min": 10.0, "max": 12.0}, got["Power and Fuel"]
    print("PASS test_alt_spellings_and_percent_between")


def test_no_criteria_returns_none():
    # A plain day-wise CSV has no ranges -> None (so the app falls back to config.yaml).
    csv = "Date,Revenue,Manpower,Packaging,Power and Fuel,FC Rent,Equipment Rentals,Overheads\n" \
          "2026-01-01,100000,26000,6000,11000,16000,6000,11000\n"
    p = REPO / "data" / "bad_inputs" / "_daywise_only.csv"
    p.write_text(csv, encoding="utf-8")
    assert extract_criteria(p) is None
    print("PASS test_no_criteria_returns_none")


def run_all():
    test_extract_from_two_sheet_workbook()
    test_data_sheet_not_mistaken_for_criteria()
    test_alt_spellings_and_percent_between()
    test_no_criteria_returns_none()
    print("\nALL CRITERIA TESTS PASSED")


if __name__ == "__main__":
    run_all()
