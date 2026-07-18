"""Build a polished, colour-coded Excel workbook of the computed P&L and anomalies log.

CSV is raw text and can't be styled, so the "beautiful" download is an .xlsx: a dark bold
header row (frozen), sensible column widths, and the Colour column filled with its actual
Red/Yellow/Green/Blue so a non-technical reader sees the day's status at a glance.
"""
from __future__ import annotations

from io import BytesIO

import pandas as pd
from openpyxl.styles import Alignment, Font, PatternFill
from openpyxl.utils import get_column_letter

# ARGB fills (no leading '#') matching the app/email palette.
COLOUR_FILL = {"Red": "E74C3C", "Yellow": "F1C40F", "Green": "2ECC71", "Blue": "3498DB"}
_HEADER_FILL = PatternFill("solid", fgColor="1F2933")
_HEADER_FONT = Font(bold=True, color="FFFFFF")


def _style_sheet(ws, df: pd.DataFrame) -> None:
    """Header styling, frozen top row, column widths, and Colour-column fills."""
    for col_idx, col in enumerate(df.columns, start=1):
        cell = ws.cell(row=1, column=col_idx)
        cell.fill = _HEADER_FILL
        cell.font = _HEADER_FONT
        cell.alignment = Alignment(horizontal="center", vertical="center")
    ws.freeze_panes = "A2"

    for col_idx, col in enumerate(df.columns, start=1):
        body = [len(str(v)) for v in df[col].tolist()] if len(df) else [0]
        width = min(max(len(str(col)), max(body)) + 2, 42)
        ws.column_dimensions[get_column_letter(col_idx)].width = max(width, 10)

    if "Colour" in df.columns:
        ci = list(df.columns).index("Colour") + 1
        for row_idx, val in enumerate(df["Colour"].tolist(), start=2):
            fill = COLOUR_FILL.get(val)
            if fill:
                cell = ws.cell(row=row_idx, column=ci)
                cell.fill = PatternFill("solid", fgColor=fill)
                cell.font = Font(bold=True, color="FFFFFF")
                cell.alignment = Alignment(horizontal="center")


def build_report_xlsx(pnl_df: pd.DataFrame, anomalies_df: pd.DataFrame) -> bytes:
    """Return a styled two-sheet workbook (P&L + Anomalies) as bytes, ready to download."""
    buf = BytesIO()
    with pd.ExcelWriter(buf, engine="openpyxl") as xl:
        pnl_df.to_excel(xl, sheet_name="P&L", index=False)
        (anomalies_df if not anomalies_df.empty else
         pd.DataFrame(columns=list(anomalies_df.columns) or ["Date"])).to_excel(
            xl, sheet_name="Anomalies", index=False)
        _style_sheet(xl.sheets["P&L"], pnl_df)
        _style_sheet(xl.sheets["Anomalies"], anomalies_df)
    return buf.getvalue()
