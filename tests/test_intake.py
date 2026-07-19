"""Coverage for smart multi-file intake and FC-specific targets."""
from __future__ import annotations

import sys
import tempfile
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from core.config import load_config
from core.engine import compute_pnl, detect_anomalies
from core.intake import inspect_inputs


P_AND_L_COLUMNS = [
    "Date", "FC", "Revenue", "Manpower", "Packaging", "Power and Fuel",
    "FC Rent", "Equipment Rentals", "Overheads",
]


def _row(fc: str, date: str = "2026-07-17", manpower: int = 26000) -> dict:
    return {
        "Date": date, "FC": fc, "Revenue": 100000, "Manpower": manpower,
        "Packaging": 6000, "Power and Fuel": 11000, "FC Rent": 16500,
        "Equipment Rentals": 6500, "Overheads": 11000,
    }


def test_combined_three_fc_and_long_targets():
    cfg = load_config()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        data = root / "daily.csv"
        targets = root / "targets.csv"
        pd.DataFrame([_row("FC-A", manpower=28000), _row("FC-B", manpower=28000),
                      _row("FC-C", manpower=28000)])[P_AND_L_COLUMNS].to_csv(data, index=False)
        targets.write_text(
            "FC,Line Item,Min,Max\n"
            "FC-A,Manpower,25,27\n"
            "FC-B,Manpower,27,29\n"
            "FC-C,Manpower,28,30\n",
            encoding="utf-8",
        )
        intake = inspect_inputs([str(data), str(targets)], cfg.targets)
        assert intake.errors == [], intake.errors
        assert intake.fcs == ["FC-A", "FC-B", "FC-C"], intake.fcs
        assert intake.targets_by_fc["FC-B"]["Manpower"] == {"min": 27.0, "max": 29.0}

        runtime_cfg = cfg
        runtime_cfg.targets_by_fc = intake.targets_by_fc
        pnl = compute_pnl(intake.data, runtime_cfg)
        anomalies = detect_anomalies(pnl, runtime_cfg)
        manpower_fcs = set(anomalies.loc[anomalies["Line Item"] == "Manpower", "FC"])
        assert manpower_fcs == {"FC-A"}, manpower_fcs
    print("PASS test_combined_three_fc_and_long_targets")


def test_multiple_missing_fc_files_can_be_confirmed():
    cfg = load_config()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        paths = []
        for name in ("North PnL.csv", "South PnL.csv"):
            path = root / name
            pd.DataFrame([_row("unused")]).drop(columns=["FC"]).to_csv(path, index=False)
            paths.append(str(path))
        probe = inspect_inputs(paths, cfg.targets)
        assignments = {
            table.id: ("FC-North" if "North" in table.source else "FC-South")
            for table in probe.tables if table.needs_fc
        }
        intake = inspect_inputs(paths, cfg.targets, assignments)
        assert intake.errors == [], intake.errors
        assert intake.fcs == ["FC-North", "FC-South"], intake.fcs
    print("PASS test_multiple_missing_fc_files_can_be_confirmed")


def test_matrix_inline_and_global_fallback():
    cfg = load_config()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        data = root / "inline.csv"
        pd.DataFrame([
            {**_row("FC-A", manpower=28000), "Manpower Target": "27-29%"},
            {**_row("FC-B", manpower=28000), "Manpower Target": "25-27%"},
        ]).to_csv(data, index=False)
        matrix = root / "matrix.csv"
        matrix.write_text(
            "Line Item,FC-A,FC-B\nPackaging,5-6%,6-7%\nPower and Fuel,10-11%,11-12%\n",
            encoding="utf-8",
        )
        intake = inspect_inputs([str(data), str(matrix)], cfg.targets)
        assert intake.errors == [], intake.errors
        assert intake.targets_by_fc["FC-A"]["Manpower"] == {"min": 27.0, "max": 29.0}
        assert intake.targets_by_fc["FC-B"]["Packaging"] == {"min": 6.0, "max": 7.0}
        assert intake.resolved_targets_by_fc["FC-A"]["Overheads"] == cfg.targets["Overheads"]
        assert intake.target_sources_by_fc["FC-A"]["Overheads"] == "config.yaml default"
    print("PASS test_matrix_inline_and_global_fallback")


def test_appended_targets_and_conflicts_are_explicit():
    cfg = load_config()
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp)
        mixed = root / "mixed.csv"
        mixed.write_text(
            ",".join(P_AND_L_COLUMNS) + "\n"
            "2026-07-17,FC-A,100000,26000,6000,11000,16500,6500,11000\n"
            "\nLine Item,Range\nManpower,24-26%\n",
            encoding="utf-8",
        )
        first = root / "first.csv"
        second = root / "second.csv"
        first.write_text("Line Item,Range\nManpower,25-27%\n", encoding="utf-8")
        second.write_text("Line Item,Range\nManpower,26-28%\n", encoding="utf-8")

        intake = inspect_inputs([str(mixed)], cfg.targets)
        assert intake.errors == [], intake.errors
        assert intake.targets_by_fc["FC-A"]["Manpower"] == {"min": 24.0, "max": 26.0}

        conflict = inspect_inputs([str(mixed), str(first), str(second)], cfg.targets)
        assert any("Conflicting uploaded Manpower targets" in error for error in conflict.errors), conflict.errors
    print("PASS test_appended_targets_and_conflicts_are_explicit")


def test_duplicate_fc_date_is_blocking():
    cfg = load_config()
    with tempfile.TemporaryDirectory() as tmp:
        path = Path(tmp) / "duplicate.csv"
        pd.DataFrame([_row("FC-A"), _row("FC-A")]).to_csv(path, index=False)
        intake = inspect_inputs([str(path)], cfg.targets)
        assert any("Duplicate FC/date" in error for error in intake.errors), intake.errors
    print("PASS test_duplicate_fc_date_is_blocking")


def run_all():
    test_combined_three_fc_and_long_targets()
    test_multiple_missing_fc_files_can_be_confirmed()
    test_matrix_inline_and_global_fallback()
    test_appended_targets_and_conflicts_are_explicit()
    test_duplicate_fc_date_is_blocking()
    print("\nALL INTAKE TESTS PASSED")


if __name__ == "__main__":
    run_all()
