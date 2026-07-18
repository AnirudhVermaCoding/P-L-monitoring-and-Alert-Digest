"""Parse an 'Ideal FC Criteria' table into target ranges — from a sheet or a separate file.

This makes the system adaptive to the input: if the user supplies target ranges (as a second
sheet in the same workbook, or as a separate CSV/Excel), we use THOSE ranges instead of the
config.yaml defaults. If no criteria are found anywhere, we silently fall back to config.yaml.

Robust to junk rows/columns and different spellings: it scans every cell for a known line-item
name and a range like "25 - 27%", "5-7", "10 to 12", "10–12%" (en/em dashes), in any position.
"""
from __future__ import annotations

import re
from pathlib import Path

import pandas as pd

# Normalise many spellings of a line item to its canonical name.
_ITEM_ALIASES = {
    "manpower": "Manpower",
    "manpower cost": "Manpower",
    "packaging": "Packaging",
    "packaging cost": "Packaging",
    "power and fuel": "Power and Fuel",
    "power & fuel": "Power and Fuel",
    "power fuel": "Power and Fuel",
    "fc rent": "FC Rent",
    "rent": "FC Rent",
    "equipment rentals": "Equipment Rentals",
    "equipment rental": "Equipment Rentals",
    "equipment": "Equipment Rentals",
    "overheads": "Overheads",
    "overhead": "Overheads",
}

# Matches "25 - 27", "25-27%", "25% - 27%", "5 to 7 %", "10–12" (en dash), "10—12" (em dash).
# An optional '%' is allowed right after the first number ("25% - 27%").
_RANGE_RE = re.compile(
    r"(\d+(?:\.\d+)?)\s*%?\s*(?:-|–|—|to)\s*(\d+(?:\.\d+)?)", re.IGNORECASE
)


def _canon_item(text) -> str | None:
    key = " ".join(str(text).strip().lower().split())
    return _ITEM_ALIASES.get(key)


def _parse_range(text) -> tuple[float, float] | None:
    """Parse a '% of revenue' range. Rejects values >100 so dates ('2026-07') aren't mistaken."""
    m = _RANGE_RE.search(str(text))
    if not m:
        return None
    lo, hi = float(m.group(1)), float(m.group(2))
    if lo > hi:
        lo, hi = hi, lo
    if hi > 100 or lo < 0:
        return None
    return lo, hi


def parse_criteria_frame(raw: pd.DataFrame | None) -> dict[str, dict[str, float]] | None:
    """Find (line item, range) pairs anywhere in a raw (header-less) sheet. None if not found."""
    if raw is None or raw.empty:
        return None
    targets: dict[str, dict[str, float]] = {}
    for _, row in raw.iterrows():
        cells = row.tolist()
        item = next((_canon_item(c) for c in cells if _canon_item(c)), None)
        if not item or item in targets:
            continue
        rng = None
        for c in cells:
            if _canon_item(c):  # don't parse the item-name cell itself as a range
                continue
            rng = _parse_range(c)
            if rng:
                break
        if rng:
            targets[item] = {"min": rng[0], "max": rng[1]}
    return targets or None


def looks_like_criteria_sheet(name: str) -> bool:
    n = str(name).lower()
    return any(k in n for k in ("criteria", "ideal", "target", "range"))


def extract_criteria(path: str | Path) -> dict[str, dict[str, float]] | None:
    """Scan a CSV/Excel for a criteria table.

    For Excel, checks criteria-named sheets first (e.g. 'Ideal FC Criteria'), then any sheet.
    Returns a {line_item: {min, max}} dict, or None if nothing parseable is found.
    """
    path = Path(path)
    if not path.exists():
        return None
    suffix = path.suffix.lower()
    try:
        if suffix in (".xlsx", ".xls"):
            xl = pd.ExcelFile(path)
            # Criteria-named sheets first; stable order otherwise.
            names = sorted(xl.sheet_names, key=lambda n: not looks_like_criteria_sheet(n))
            for name in names:
                got = parse_criteria_frame(xl.parse(name, header=None))
                if got:
                    return got
        elif suffix in (".csv", ".txt"):
            return parse_criteria_frame(pd.read_csv(path, header=None))
    except Exception:  # noqa: BLE001 - criteria is optional; never break the run over it
        return None
    return None
