"""Load daily FC P&L data from CSV/Excel with tolerant header + column handling.

The sample workbook (Case Study 3.xlsx) is awkward: on the "Day Wise PnL" sheet the
real header is on the second row and the first column is blank. Uploaded files may also
use slightly different column names ("Manpower Cost" vs "Manpower", "Power & Fuel" vs
"Power and Fuel") and may or may not carry FC / Date columns. This module normalises all
of that into one tidy frame, and raises LoaderError with a human message on bad input.
"""
from __future__ import annotations

from pathlib import Path

import pandas as pd

from core.config import LINE_ITEMS

REQUIRED_NUMERIC = ["Revenue"] + LINE_ITEMS

# Map many possible spellings -> canonical column name.
COLUMN_ALIASES = {
    "day": "Day",
    "date": "Date",
    "fc": "FC",
    "fulfilment centre": "FC",
    "fulfillment center": "FC",
    "revenue": "Revenue",
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


class LoaderError(Exception):
    """Raised with a user-facing message when input data can't be used."""


def _canonical(col: str) -> str:
    key = " ".join(str(col).strip().lower().split())
    return COLUMN_ALIASES.get(key, str(col).strip())


def _find_header_row(raw: pd.DataFrame) -> int:
    """Return the row index whose cells look like the P&L header (contain 'Revenue')."""
    for i in range(min(6, len(raw))):
        values = {" ".join(str(v).strip().lower().split()) for v in raw.iloc[i].tolist()}
        if "revenue" in values:
            return i
    return 0


def _read_any(path: Path) -> pd.DataFrame:
    """Read a CSV or Excel file into a raw (header-less) DataFrame, sniffing the header row."""
    suffix = path.suffix.lower()
    try:
        if suffix in (".xlsx", ".xls"):
            xl = pd.ExcelFile(path)
            # Prefer a sheet that looks like day-wise data.
            sheet = xl.sheet_names[0]
            for name in xl.sheet_names:
                if "pnl" in name.lower() or "day" in name.lower():
                    sheet = name
                    break
            raw = xl.parse(sheet, header=None)
        elif suffix in (".csv", ".txt"):
            raw = pd.read_csv(path, header=None)
        else:
            raise LoaderError(
                f"Unsupported file type '{suffix}'. Please upload a .csv or .xlsx file."
            )
    except LoaderError:
        raise
    except Exception as exc:  # noqa: BLE001 - surface a clean message for any parse failure
        raise LoaderError(
            f"Could not read '{path.name}'. The file may be corrupt or not a real "
            f"spreadsheet/CSV. (details: {exc})"
        ) from exc

    if raw is None or raw.empty:
        raise LoaderError(f"'{path.name}' is empty — there is no data to analyse.")
    return raw


def load_pnl(path: str | Path) -> pd.DataFrame:
    """Load a P&L file into a tidy DataFrame with canonical columns.

    Guarantees columns: Date, FC, Revenue + the six line items (all numeric).
    Raises LoaderError (with a readable message) on any unusable input.
    """
    path = Path(path)
    if not path.exists():
        raise LoaderError(f"File not found: {path}")

    raw = _read_any(path)

    header_row = _find_header_row(raw)
    header = [_canonical(c) for c in raw.iloc[header_row].tolist()]
    df = raw.iloc[header_row + 1 :].copy()
    df.columns = header
    df = df.loc[:, [c for c in df.columns if str(c) != "nan"]]
    df = df.dropna(axis=1, how="all").dropna(axis=0, how="all")

    missing = [c for c in REQUIRED_NUMERIC if c not in df.columns]
    if missing:
        raise LoaderError(
            "The file is missing required column(s): "
            + ", ".join(missing)
            + ". Expected Revenue plus: "
            + ", ".join(LINE_ITEMS)
            + "."
        )

    # Coerce numerics; non-numeric cells become NaN so we can report them.
    for col in REQUIRED_NUMERIC:
        df[col] = pd.to_numeric(df[col], errors="coerce")

    # Drop entirely-non-numeric junk rows (stray header/blank lines).
    df = df.dropna(subset=REQUIRED_NUMERIC, how="all")
    if df.empty:
        raise LoaderError(
            "No numeric rows found. Check that Revenue and cost columns contain numbers."
        )

    # A row missing ANY revenue/cost value would compute a silently-wrong (too-healthy)
    # margin, because pandas' sum() skips NaN. Drop such incomplete rows rather than mislead.
    incomplete = df[REQUIRED_NUMERIC].isna().any(axis=1)
    if incomplete.all():
        raise LoaderError(
            "Every row is missing at least one Revenue or cost value — cannot compute margins."
        )
    df = df[~incomplete].copy()

    # Drop rows with non-positive revenue (division base).
    bad_rev = df["Revenue"] <= 0
    if bad_rev.all():
        raise LoaderError("Every row has zero or negative Revenue — cannot compute margins.")
    df = df[~bad_rev].copy()

    # Fill defaults for optional dimensions.
    if "FC" not in df.columns:
        df["FC"] = "FC-1"
    df["FC"] = df["FC"].fillna("FC-1").astype(str).str.strip()

    if "Date" not in df.columns:
        if "Day" in df.columns:
            # Zero-pad so lexical sort (used for "latest day" / trends) matches numeric order.
            days = pd.to_numeric(df["Day"], errors="coerce").astype("Int64")
            width = max(2, len(str(int(days.max())))) if days.notna().any() else 2
            df["Date"] = [f"Day {int(d):0{width}d}" if pd.notna(d) else "Day ?" for d in days]
        else:
            width = max(2, len(str(len(df))))
            df["Date"] = [f"Day {i + 1:0{width}d}" for i in range(len(df))]
    else:
        # Keep dates as readable ISO strings where possible.
        parsed = pd.to_datetime(df["Date"], errors="coerce")
        df["Date"] = parsed.dt.strftime("%Y-%m-%d").where(parsed.notna(), df["Date"].astype(str))

    keep = ["Date", "FC", "Revenue"] + LINE_ITEMS
    df = df[keep].reset_index(drop=True)
    return df
