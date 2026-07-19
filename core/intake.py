"""Smart, auditable intake for mixed multi-FC CSV and Excel uploads.

Every table is classified independently.  P&L tables are normalized and concatenated;
target tables are parsed into global and per-FC profiles.  Ambiguity is surfaced through
editable assignments in the UI rather than hidden behind a silent guess.
"""

import csv
import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path

import pandas as pd

from core.config import LINE_ITEMS
from core.criteria import _canon_item, _parse_range
from core.loader import COLUMN_ALIASES, LoaderError, REQUIRED_NUMERIC

RUNTIME_API_VERSION = 1

GLOBAL_SCOPE = "__global__"
_GENERIC_NAMES = {
    "", "sheet1", "data", "pnl", "day wise pnl", "daily pnl", "targets", "target",
    "criteria", "ideal fc criteria", "ranges", "case study 3",
}


def _text(value) -> str:
    if pd.isna(value):
        return ""
    return " ".join(str(value).strip().split())


def _norm(value) -> str:
    return _text(value).lower().replace("_", " ")


def _canonical_column(value) -> str:
    value = _text(value)
    return COLUMN_ALIASES.get(_norm(value), value)


def _display_name(path: Path) -> str:
    # Uploaded files are stored as <content-hash>__<original-name> to avoid collisions.
    return re.sub(r"^[0-9a-f]{12}__", "", path.name, flags=re.IGNORECASE)


def _table_id(path: Path, sheet: str) -> str:
    raw = f"{path.resolve()}::{sheet}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:12]


def _read_tables(path: Path) -> list[tuple[str, pd.DataFrame]]:
    try:
        if path.suffix.lower() in (".xlsx", ".xls"):
            xl = pd.ExcelFile(path)
            return [(name, xl.parse(name, header=None)) for name in xl.sheet_names]
        if path.suffix.lower() in (".csv", ".txt"):
            # csv.reader tolerates an appended target section with a different width.
            with path.open("r", encoding="utf-8-sig", newline="") as handle:
                rows = list(csv.reader(handle))
            width = max((len(row) for row in rows), default=0)
            return [("", pd.DataFrame([row + [None] * (width - len(row)) for row in rows]))]
        raise LoaderError(
            f"Unsupported file type '{path.suffix}'. Please upload CSV or XLSX files."
        )
    except LoaderError:
        raise
    except Exception as exc:  # noqa: BLE001 - converted to a user-facing intake error
        raise LoaderError(f"Could not read '{_display_name(path)}': {exc}") from exc


def _find_pnl_header(raw: pd.DataFrame) -> int | None:
    for idx in range(min(25, len(raw))):
        columns = {_canonical_column(v) for v in raw.iloc[idx].tolist()}
        if all(name in columns for name in REQUIRED_NUMERIC):
            return idx
    return None


def _suggest_fc(path: Path, sheet: str) -> str:
    candidates = [sheet, Path(_display_name(path)).stem]
    for candidate in candidates:
        value = _text(candidate)
        if _norm(value) in _GENERIC_NAMES:
            continue
        value = re.sub(
            r"(?i)\b(daily|day[ -]?wise|p\s*&?\s*l|pnl|data|report|targets?|criteria|ranges?)\b",
            " ", value,
        )
        value = re.sub(r"[-_ ]+", " ", value).strip(" -_")
        if value and _norm(value) not in _GENERIC_NAMES:
            return value
    return "FC-1"


def _load_pnl_table(raw: pd.DataFrame, header_row: int, source: str,
                    forced_fc: str | None) -> tuple[pd.DataFrame, list[str]]:
    header = [_canonical_column(c) for c in raw.iloc[header_row].tolist()]
    df = raw.iloc[header_row + 1:].copy()
    df.columns = header
    df = df.loc[:, [c for c in df.columns if c and c.lower() != "nan"]]
    df = df.dropna(axis=1, how="all").dropna(axis=0, how="all")

    missing = [name for name in REQUIRED_NUMERIC if name not in df.columns]
    if missing:
        raise LoaderError(f"{source} is missing: {', '.join(missing)}.")

    for column in REQUIRED_NUMERIC:
        df[column] = pd.to_numeric(df[column], errors="coerce")
    df = df.dropna(subset=REQUIRED_NUMERIC, how="all")
    if df.empty:
        raise LoaderError(f"{source} contains no complete numeric P&L rows.")

    warnings: list[str] = []
    incomplete = df[REQUIRED_NUMERIC].isna().any(axis=1)
    if incomplete.any():
        warnings.append(f"{source}: ignored {int(incomplete.sum())} incomplete row(s).")
        df = df[~incomplete].copy()
    bad_revenue = df["Revenue"] <= 0
    if bad_revenue.any():
        warnings.append(f"{source}: ignored {int(bad_revenue.sum())} non-positive revenue row(s).")
        df = df[~bad_revenue].copy()
    if df.empty:
        raise LoaderError(f"{source} has no usable rows after validation.")

    if "FC" not in df.columns:
        df["FC"] = forced_fc or "FC-1"
    else:
        df["FC"] = df["FC"].where(df["FC"].notna(), forced_fc or "")
    df["FC"] = df["FC"].astype(str).str.strip()
    if (df["FC"] == "").any():
        raise LoaderError(f"{source} has blank FC values. Fill them or assign one FC to the table.")

    if "Date" not in df.columns:
        if "Day" in df.columns:
            days = pd.to_numeric(df["Day"], errors="coerce").astype("Int64")
            width = max(2, len(str(int(days.max())))) if days.notna().any() else 2
            df["Date"] = [f"Day {int(day):0{width}d}" if pd.notna(day) else "Day ?"
                          for day in days]
        else:
            width = max(2, len(str(len(df))))
            df["Date"] = [f"Day {i + 1:0{width}d}" for i in range(len(df))]
    else:
        parsed = pd.to_datetime(df["Date"], errors="coerce")
        df["Date"] = parsed.dt.strftime("%Y-%m-%d").where(
            parsed.notna(), df["Date"].astype(str)
        )

    keep = ["Date", "FC", "Revenue"] + LINE_ITEMS
    return df[keep].reset_index(drop=True), warnings


@dataclass
class _TargetExtraction:
    global_targets: dict[str, dict[str, float]] = field(default_factory=dict)
    by_fc: dict[str, dict[str, dict[str, float]]] = field(default_factory=dict)
    errors: list[str] = field(default_factory=list)
    found: bool = False
    has_explicit_fc: bool = False


def _number(value) -> float | None:
    try:
        return float(str(value).strip().replace("%", ""))
    except (TypeError, ValueError):
        return None


def _valid_range(lo: float | None, hi: float | None) -> bool:
    return lo is not None and hi is not None and 0 <= lo <= hi <= 100


def _add_target(result: _TargetExtraction, fc: str | None, item: str,
                target: dict[str, float], source: str) -> None:
    result.found = True
    bucket = result.global_targets if not fc else result.by_fc.setdefault(str(fc).strip(), {})
    previous = bucket.get(item)
    if previous and previous != target:
        scope = str(fc).strip() if fc else "global"
        result.errors.append(
            f"{source}: conflicting {item} targets for {scope}: "
            f"{previous['min']:g}-{previous['max']:g}% and {target['min']:g}-{target['max']:g}%."
        )
    else:
        bucket[item] = target


def _target_from_cells(range_value=None, min_value=None, max_value=None):
    parsed = _parse_range(range_value) if range_value is not None else None
    if parsed:
        return {"min": parsed[0], "max": parsed[1]}
    lo, hi = _number(min_value), _number(max_value)
    if _valid_range(lo, hi):
        return {"min": lo, "max": hi}
    return None


def _find_column(values: list[str], choices: set[str]) -> int | None:
    return next((idx for idx, value in enumerate(values) if value in choices), None)


def _extract_targets(raw: pd.DataFrame, source: str,
                     default_fc: str | None = None) -> _TargetExtraction:
    """Parse long, matrix, inline, and loose target layouts from one raw table."""
    out = _TargetExtraction()
    if raw is None or raw.empty:
        return out

    # Long layout: [FC] | Line Item | Range, or [FC] | Line Item | Min | Max.
    for header_idx in range(min(30, len(raw))):
        headers = [_norm(v) for v in raw.iloc[header_idx].tolist()]
        item_col = next(
            (i for i, h in enumerate(headers)
             if h in {"item", "cost item"} or "line item" in h),
            None,
        )
        range_col = next(
            (i for i, h in enumerate(headers) if "range" in h or h in {"target", "target %"}),
            None,
        )
        min_col = next((i for i, h in enumerate(headers) if h in {"min", "minimum", "target min", "min %"} or h.endswith(" minimum")), None)
        max_col = next((i for i, h in enumerate(headers) if h in {"max", "maximum", "target max", "max %"} or h.endswith(" maximum")), None)
        fc_col = _find_column(headers, {"fc", "fulfilment centre", "fulfillment center", "warehouse"})
        if item_col is None or (range_col is None and (min_col is None or max_col is None)):
            continue
        out.found = True
        out.has_explicit_fc = fc_col is not None
        for row_idx in range(header_idx + 1, len(raw)):
            row = raw.iloc[row_idx].tolist()
            item = _canon_item(row[item_col] if item_col < len(row) else None)
            if not item:
                continue
            target = _target_from_cells(
                row[range_col] if range_col is not None and range_col < len(row) else None,
                row[min_col] if min_col is not None and min_col < len(row) else None,
                row[max_col] if max_col is not None and max_col < len(row) else None,
            )
            fc = _text(row[fc_col]) if fc_col is not None and fc_col < len(row) else default_fc
            if not target:
                out.errors.append(f"{source}: invalid target range for {item} on row {row_idx + 1}.")
                continue
            _add_target(out, fc or None, item, target, source)
        return out

    # Matrix layout: Line Item | FC-Delhi | FC-Mumbai, with a range in each FC cell.
    for header_idx in range(min(30, len(raw))):
        headers = [_text(v) for v in raw.iloc[header_idx].tolist()]
        normalized = [_norm(v) for v in headers]
        item_col = next(
            (i for i, h in enumerate(normalized)
             if h in {"item", "cost item"} or "line item" in h),
            None,
        )
        if item_col is None:
            continue
        matrix_hits = 0
        for row_idx in range(header_idx + 1, len(raw)):
            row = raw.iloc[row_idx].tolist()
            item = _canon_item(row[item_col] if item_col < len(row) else None)
            if not item:
                continue
            for col_idx, fc in enumerate(headers):
                if col_idx == item_col or not fc:
                    continue
                target = _target_from_cells(row[col_idx] if col_idx < len(row) else None)
                if target:
                    matrix_hits += 1
                    _add_target(out, fc, item, target, source)
        if matrix_hits:
            out.found = True
            out.has_explicit_fc = True
            return out

    # Inline targets beside daily rows: Manpower Target / Min / Max etc.
    pnl_header = _find_pnl_header(raw)
    if pnl_header is not None:
        headers = [_norm(v) for v in raw.iloc[pnl_header].tolist()]
        fc_col = _find_column(headers, {"fc", "fulfilment centre", "fulfillment center"})
        target_cols: dict[str, dict[str, int]] = {}
        for col_idx, header in enumerate(headers):
            if not any(token in header for token in ("target", "range", " min", " max")):
                continue
            item = next((item for item in LINE_ITEMS if _norm(item) in header), None)
            if not item:
                continue
            kind = "min" if "min" in header else "max" if "max" in header else "range"
            target_cols.setdefault(item, {})[kind] = col_idx
        if target_cols:
            out.found = True
            out.has_explicit_fc = fc_col is not None
            for row_idx in range(pnl_header + 1, len(raw)):
                row = raw.iloc[row_idx].tolist()
                fc = _text(row[fc_col]) if fc_col is not None and fc_col < len(row) else default_fc
                for item, cols in target_cols.items():
                    target = _target_from_cells(
                        row[cols["range"]] if "range" in cols and cols["range"] < len(row) else None,
                        row[cols["min"]] if "min" in cols and cols["min"] < len(row) else None,
                        row[cols["max"]] if "max" in cols and cols["max"] < len(row) else None,
                    )
                    if target:
                        _add_target(out, fc or None, item, target, source)

    # Loose/appended section: find a known item and one textual range anywhere in its row.
    if not out.found:
        for row_idx, row_series in raw.iterrows():
            cells = row_series.tolist()
            item = next((_canon_item(cell) for cell in cells if _canon_item(cell)), None)
            if not item:
                continue
            target = next((_target_from_cells(cell) for cell in cells
                           if not _canon_item(cell) and _target_from_cells(cell)), None)
            if target:
                _add_target(out, default_fc, item, target, source)
    return out


@dataclass
class TableInfo:
    id: str
    source: str
    sheet: str
    role: str
    row_count: int
    fcs: list[str]
    needs_fc: bool = False
    suggested_fc: str = ""
    target_scope: str = ""
    target_scope_editable: bool = False
    target_count: int = 0
    warnings: list[str] = field(default_factory=list)

    def as_dict(self) -> dict:
        return {
            "Source": self.source,
            "Sheet": self.sheet or "(CSV)",
            "Detected role": self.role,
            "Rows": self.row_count,
            "FCs": ", ".join(self.fcs) or "â€”",
            "Target scope": self.target_scope or "â€”",
            "Targets": self.target_count,
            "Warnings": " ".join(self.warnings),
        }


@dataclass
class IntakeResult:
    data: pd.DataFrame
    tables: list[TableInfo]
    global_targets: dict[str, dict[str, float]]
    targets_by_fc: dict[str, dict[str, dict[str, float]]]
    resolved_targets_by_fc: dict[str, dict[str, dict[str, float]]]
    target_sources_by_fc: dict[str, dict[str, str]]
    warnings: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)

    @property
    def fcs(self) -> list[str]:
        if self.data.empty or "FC" not in self.data:
            return []
        return sorted(self.data["FC"].astype(str).unique().tolist())

    def target_coverage(self) -> pd.DataFrame:
        rows = []
        for fc in self.fcs:
            for item in LINE_ITEMS:
                target = self.resolved_targets_by_fc.get(fc, {}).get(item, {})
                rows.append({
                    "FC": fc,
                    "Line Item": item,
                    "Target Range": (
                        f"{target.get('min', 0):g}-{target.get('max', 0):g}%" if target else "â€”"
                    ),
                    "Source": self.target_sources_by_fc.get(fc, {}).get(item, ""),
                })
        return pd.DataFrame(rows)


def inspect_inputs(sources: list[str] | tuple[str, ...], default_targets: dict,
                   fc_assignments: dict[str, str] | None = None,
                   target_assignments: dict[str, str] | None = None) -> IntakeResult:
    """Classify, validate, merge, and resolve a collection of input files."""
    fc_assignments = fc_assignments or {}
    target_assignments = target_assignments or {}
    errors: list[str] = []
    warnings: list[str] = []
    table_rows: list[dict] = []
    recognized_by_file: dict[str, bool] = {}

    for source_path in sources:
        path = Path(source_path)
        recognized_by_file[str(path)] = False
        if not path.exists():
            errors.append(f"File not found: {path}")
            continue
        try:
            for sheet, raw in _read_tables(path):
                table_rows.append({"path": path, "sheet": sheet, "raw": raw,
                                   "id": _table_id(path, sheet)})
        except LoaderError as exc:
            errors.append(str(exc))

    # First pass: P&L tables establish the authoritative FC list.
    pnl_frames: list[pd.DataFrame] = []
    pnl_meta: dict[str, dict] = {}
    for entry in table_rows:
        raw, path, sheet, table_id = entry["raw"], entry["path"], entry["sheet"], entry["id"]
        header_row = _find_pnl_header(raw)
        if header_row is None:
            continue
        source_label = f"{_display_name(path)} / {sheet}" if sheet else _display_name(path)
        header = [_canonical_column(c) for c in raw.iloc[header_row].tolist()]
        needs_fc = "FC" not in header
        suggested = _suggest_fc(path, sheet) if needs_fc else ""
        forced_fc = _text(fc_assignments.get(table_id)) or suggested or None
        try:
            frame, table_warnings = _load_pnl_table(raw, header_row, source_label, forced_fc)
            pnl_frames.append(frame)
            recognized_by_file[str(path)] = True
            pnl_meta[table_id] = {
                "frame": frame, "needs_fc": needs_fc, "suggested": suggested,
                "warnings": table_warnings,
            }
            warnings.extend(table_warnings)
        except LoaderError as exc:
            errors.append(str(exc))

    data = pd.concat(pnl_frames, ignore_index=True) if pnl_frames else pd.DataFrame()
    detected_fcs = sorted(data["FC"].unique().tolist()) if not data.empty else []

    global_targets: dict[str, dict[str, float]] = {}
    by_fc: dict[str, dict[str, dict[str, float]]] = {}
    global_sources: dict[str, str] = {}
    fc_sources: dict[str, dict[str, str]] = {}
    target_meta: dict[str, dict] = {}

    def merge_target(fc: str | None, item: str, target: dict, source_label: str) -> None:
        bucket = global_targets if not fc else by_fc.setdefault(fc, {})
        source_bucket = global_sources if not fc else fc_sources.setdefault(fc, {})
        previous = bucket.get(item)
        if previous and previous != target:
            scope = fc or "global"
            errors.append(
                f"Conflicting uploaded {item} targets for {scope}: "
                f"{previous['min']:g}-{previous['max']:g}% ({source_bucket[item]}) versus "
                f"{target['min']:g}-{target['max']:g}% ({source_label})."
            )
            return
        bucket[item] = target
        source_bucket[item] = source_label

    # Second pass: target scope can now be inferred against detected FC names.
    for entry in table_rows:
        raw, path, sheet, table_id = entry["raw"], entry["path"], entry["sheet"], entry["id"]
        source_label = f"{_display_name(path)} / {sheet}" if sheet else _display_name(path)
        probe = _extract_targets(raw, source_label)
        if not probe.found and not probe.errors:
            continue

        scope = target_assignments.get(table_id, "")
        if not scope and not probe.has_explicit_fc:
            pnl_info = pnl_meta.get(table_id)
            if pnl_info and len(pnl_info["frame"]["FC"].unique()) == 1:
                scope = str(pnl_info["frame"]["FC"].iloc[0])
            else:
                searchable = _norm(f"{Path(_display_name(path)).stem} {sheet}")
                matches = [fc for fc in detected_fcs if _norm(fc) in searchable]
                scope = matches[0] if len(matches) == 1 else GLOBAL_SCOPE
        parsed = _extract_targets(raw, source_label,
                                  None if scope == GLOBAL_SCOPE else scope or None)
        errors.extend(parsed.errors)
        if parsed.found:
            recognized_by_file[str(path)] = True
        for item, target in parsed.global_targets.items():
            merge_target(None, item, target, source_label)
        for fc, targets in parsed.by_fc.items():
            resolved_fc = next(
                (known_fc for known_fc in detected_fcs if _norm(known_fc) == _norm(fc)),
                fc,
            )
            for item, target in targets.items():
                merge_target(resolved_fc, item, target, source_label)
        target_meta[table_id] = {
            "scope": "Per-FC" if parsed.has_explicit_fc else ("Global" if scope == GLOBAL_SCOPE else scope),
            "editable": not parsed.has_explicit_fc,
            "count": len(parsed.global_targets) + sum(len(v) for v in parsed.by_fc.values()),
        }

    tables: list[TableInfo] = []
    for entry in table_rows:
        path, sheet, table_id = entry["path"], entry["sheet"], entry["id"]
        pmeta, tmeta = pnl_meta.get(table_id), target_meta.get(table_id)
        roles = (["P&L"] if pmeta else []) + (["Targets"] if tmeta else [])
        if not roles:
            warnings.append(
                f"Ignored unrecognized table: {_display_name(path)}"
                + (f" / {sheet}" if sheet else "") + "."
            )
        tables.append(TableInfo(
            id=table_id,
            source=_display_name(path),
            sheet=sheet,
            role=" + ".join(roles) if roles else "Unrecognized",
            row_count=len(pmeta["frame"]) if pmeta else 0,
            fcs=sorted(pmeta["frame"]["FC"].unique().tolist()) if pmeta else [],
            needs_fc=bool(pmeta and pmeta["needs_fc"]),
            suggested_fc=pmeta["suggested"] if pmeta else "",
            target_scope=tmeta["scope"] if tmeta else "",
            target_scope_editable=bool(tmeta and tmeta["editable"]),
            target_count=tmeta["count"] if tmeta else 0,
            warnings=pmeta["warnings"] if pmeta else [],
        ))

    for path_str, recognized in recognized_by_file.items():
        if not recognized:
            errors.append(f"'{_display_name(Path(path_str))}' contains no recognized P&L or target table.")
    if data.empty:
        errors.append(
            "No P&L table found. Expected Revenue plus Manpower, Packaging, Power and Fuel, "
            "FC Rent, Equipment Rentals, and Overheads."
        )
    else:
        duplicates = data[data.duplicated(["FC", "Date"], keep=False)]
        if not duplicates.empty:
            pairs = duplicates[["FC", "Date"]].drop_duplicates().head(5)
            rendered = ", ".join(f"{r.FC} / {r.Date}" for r in pairs.itertuples())
            errors.append(
                f"Duplicate FC/date rows would be double-counted: {rendered}. "
                "Remove duplicates or correct the FC assignments."
            )

    unused_target_fcs = sorted(set(by_fc) - set(detected_fcs))
    if unused_target_fcs:
        warnings.append(
            "Uploaded targets were ignored because no matching P&L data was found for: "
            + ", ".join(unused_target_fcs) + "."
        )

    resolved: dict[str, dict[str, dict[str, float]]] = {}
    resolved_sources: dict[str, dict[str, str]] = {}
    for fc in detected_fcs:
        resolved[fc], resolved_sources[fc] = {}, {}
        for item in LINE_ITEMS:
            if item in by_fc.get(fc, {}):
                resolved[fc][item] = by_fc[fc][item]
                resolved_sources[fc][item] = f"Uploaded per-FC: {fc_sources[fc][item]}"
            elif item in global_targets:
                resolved[fc][item] = global_targets[item]
                resolved_sources[fc][item] = f"Uploaded global: {global_sources[item]}"
            else:
                resolved[fc][item] = default_targets[item]
                resolved_sources[fc][item] = "config.yaml default"

    # Stable de-duplication keeps the UI readable when one issue is detected twice.
    warnings = list(dict.fromkeys(warnings))
    errors = list(dict.fromkeys(errors))
    return IntakeResult(
        data=data,
        tables=tables,
        global_targets=global_targets,
        targets_by_fc=by_fc,
        resolved_targets_by_fc=resolved,
        target_sources_by_fc=resolved_sources,
        warnings=warnings,
        errors=errors,
    )
