from __future__ import annotations

from datetime import datetime
from pathlib import Path

from openpyxl import Workbook
from openpyxl.styles import Alignment, Border, Font, PatternFill, Side
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.worksheet import Worksheet

from .models import ChannelResult

HEADER_6 = [
    "Import",
    "Rule",
    "Intermediate",
    "Export Rule",
    "Export",
    "Data Source",
]
HEADER_4 = [
    "Import",
    "Rule",
    "Intermediate",
    "Data Source",
]

MAX_COLUMNS = 6
COLUMN_WIDTH = 36
ROW_HEIGHT_DATA = 18
ROW_HEIGHT_HEADER = 22
ROW_HEIGHT_TITLE = 22

FILENAME_MAX_LEN = 200
SHEET_NAME_MAX_LEN = 31
TIMESTAMP_FORMAT = "%Y%m%d_%H%M%S"

TITLE_FILL = PatternFill(start_color="1D1D1F", end_color="1D1D1F", fill_type="solid")
GREEN_FILL = PatternFill(start_color="92D050", end_color="92D050", fill_type="solid")
BLUE_FILL = PatternFill(start_color="00B0F0", end_color="00B0F0", fill_type="solid")
PURPLE_FILL = PatternFill(start_color="7030A0", end_color="7030A0", fill_type="solid")
ERROR_HEADER_FILL = PatternFill(
    start_color="FAFAFA", end_color="FAFAFA", fill_type="solid"
)

_HEADER_COLUMN_FILLS: dict[str, PatternFill] = {
    "Import": GREEN_FILL,
    "Intermediate": BLUE_FILL,
    "Export": GREEN_FILL,
}

TITLE_FONT = Font(color="FFFFFF", bold=True, size=11)
HEADER_WHITE_FONT = Font(color="FFFFFF", bold=True, size=11)
HEADER_DEFAULT_FONT = Font(color="1D1D1F", bold=True, size=11)
DATA_FONT = Font(color="1D1D1F", size=11)
DATA_WHITE_FONT = Font(color="FFFFFF", size=11)

BORDER_SIDE = Side(style="thin", color="D1D1D6")
CELL_BORDER = Border(
    left=BORDER_SIDE,
    right=BORDER_SIDE,
    top=BORDER_SIDE,
    bottom=BORDER_SIDE,
)

TITLE_ALIGN = Alignment(horizontal="left", vertical="center", wrap_text=False)
HEADER_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=True)
DATA_ALIGN = Alignment(horizontal="center", vertical="center", wrap_text=False)

_INVALID_FILENAME_CHARS = str.maketrans({c: "_" for c in r':\/?*[]<>|"'})
_INVALID_SHEET_CHARS = str.maketrans({c: "_" for c in r":\/?*[]"})

ERRORS_FILENAME = "_errors.xlsx"


class ReportError(Exception):
    """Raised when a workbook cannot be written."""


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def write_reports(
    results: list[ChannelResult],
    out_dir: Path | str,
) -> list[Path]:
    """
    Write one .xlsx per successful channel into out_dir.

    - Filename: {sanitized channel_name}__{timestamp}.xlsx. The
      timestamp suffix guarantees that repeated runs never overwrite
      each other. Within a single run, name collisions get an extra
      '__{channel_id}' before the timestamp.
    - Sheet name: sanitized channel_name (<= 31 chars).
    - Failed channels do not produce their own file; they are collected
      in _errors.xlsx. _errors.xlsx has no timestamp and is overwritten
      on every run that has failures, so it always reflects the latest
      batch.

    Returns the list of written report paths (excluding _errors.xlsx).

    Raises ReportError when a workbook cannot be written. Removing a
    stale _errors.xlsx on the success path is best-effort: a deletion
    failure (for example, the file is open in Excel) is silently
    ignored, because the batch itself succeeded and the leftover file
    does not affect the new reports.
    """
    try:
        directory = out_dir if isinstance(out_dir, Path) else Path(out_dir)
    except TypeError as exc:
        raise ReportError(
            f"out_dir must be a str or Path, got {type(out_dir).__name__}"
        ) from exc

    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise ReportError(f"cannot create output directory {directory}: {exc}") from exc

    timestamp = datetime.now().strftime(TIMESTAMP_FORMAT)

    succeeded = [r for r in results if r.succeeded]
    failed = [r for r in results if r.error is not None]

    written: list[Path] = []
    used_filenames: set[str] = set()

    for result in succeeded:
        base = _sanitize_filename(
            result.selection.channel_name,
            result.selection.channel_id,
        )
        unique = _unique_filename(base, result.selection.channel_id, used_filenames)
        out_path = directory / f"{unique}__{timestamp}.xlsx"
        _write_single_channel(out_path, result)
        written.append(out_path)

    if failed:
        _write_errors_file(directory / ERRORS_FILENAME, failed)

    return written


# ---------------------------------------------------------------------------
# Single-channel workbook
# ---------------------------------------------------------------------------


def _write_single_channel(out_path: Path, result: ChannelResult) -> None:
    wb = Workbook()
    sheet = wb.active
    sheet.title = _sanitize_sheet_name(
        result.selection.channel_name,
        result.selection.channel_id,
    )
    _apply_column_widths(sheet)
    sheet.freeze_panes = "A3"

    _write_channel_block(sheet, 1, result)

    try:
        wb.save(out_path)
    except OSError as exc:
        raise ReportError(f"cannot write {out_path}: {exc}") from exc


def _write_channel_block(sheet: Worksheet, row: int, result: ChannelResult) -> int:
    """Write one channel block; return the row index after the block."""
    sel = result.selection
    header = HEADER_6 if result.has_export else HEADER_4
    last_col = len(header)

    title_text = f"{sel.channel_id}  |  {sel.channel_name}  |  {sel.dataflow_url}"
    anchor = sheet.cell(row=row, column=1)
    anchor.value = title_text
    anchor.font = TITLE_FONT
    anchor.alignment = TITLE_ALIGN
    anchor.fill = TITLE_FILL
    anchor.border = CELL_BORDER
    sheet.merge_cells(
        start_row=row,
        start_column=1,
        end_row=row,
        end_column=last_col,
    )
    sheet.row_dimensions[row].height = ROW_HEIGHT_TITLE
    row += 1

    for col, text in enumerate(header, start=1):
        cell = sheet.cell(row=row, column=col, value=text)
        cell.alignment = HEADER_ALIGN
        cell.border = CELL_BORDER
        fill = _HEADER_COLUMN_FILLS.get(text)
        if fill is not None:
            cell.fill = fill
            cell.font = HEADER_WHITE_FONT
        else:
            cell.font = HEADER_DEFAULT_FONT
    sheet.row_dimensions[row].height = ROW_HEIGHT_HEADER
    row += 1

    for attr_row in result.rows or []:
        highlight_import, highlight_inter = _orphan_flags(attr_row)
        for col, value in enumerate(attr_row, start=1):
            cell_value = None if value in ("", " ") else value
            cell = sheet.cell(row=row, column=col, value=cell_value)
            cell.alignment = DATA_ALIGN
            cell.border = CELL_BORDER
            if col == 1 and highlight_import:
                cell.fill = PURPLE_FILL
                cell.font = DATA_WHITE_FONT
            elif col == 3 and highlight_inter:
                cell.fill = PURPLE_FILL
                cell.font = DATA_WHITE_FONT
            else:
                cell.font = DATA_FONT
        sheet.row_dimensions[row].height = ROW_HEIGHT_DATA
        row += 1

    return row


def _orphan_flags(attr_row: list[str]) -> tuple[bool, bool]:
    if len(attr_row) < 3:
        return False, False
    import_val = attr_row[0]
    inter_val = attr_row[2]
    if import_val == " " and inter_val != " ":
        return False, True
    if import_val != " " and inter_val == " ":
        return True, False
    return False, False


# ---------------------------------------------------------------------------
# Errors workbook
# ---------------------------------------------------------------------------


def _write_errors_file(out_path: Path, failed: list[ChannelResult]) -> None:
    wb = Workbook()
    sheet = wb.active
    sheet.title = "Errors"
    sheet.freeze_panes = "A2"

    headers = [
        "Project",
        "Site",
        "Channel",
        "Channel ID",
        "URL",
        "Error",
    ]
    for i in range(1, len(headers) + 1):
        sheet.column_dimensions[get_column_letter(i)].width = 30

    for col, text in enumerate(headers, start=1):
        cell = sheet.cell(row=1, column=col, value=text)
        cell.font = HEADER_DEFAULT_FONT
        cell.fill = ERROR_HEADER_FILL
        cell.alignment = HEADER_ALIGN
        cell.border = CELL_BORDER
    sheet.row_dimensions[1].height = ROW_HEIGHT_HEADER

    for i, result in enumerate(failed, start=2):
        sel = result.selection
        values = [
            sel.project_name,
            sel.site_name,
            sel.channel_name,
            sel.channel_id,
            sel.dataflow_url,
            result.error,
        ]
        for col, value in enumerate(values, start=1):
            cell = sheet.cell(row=i, column=col, value=value)
            cell.font = DATA_FONT
            cell.alignment = DATA_ALIGN
            cell.border = CELL_BORDER
        sheet.row_dimensions[i].height = ROW_HEIGHT_DATA

    try:
        wb.save(out_path)
    except OSError as exc:
        raise ReportError(f"cannot write {out_path}: {exc}") from exc


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _apply_column_widths(sheet: Worksheet) -> None:
    for i in range(1, MAX_COLUMNS + 1):
        sheet.column_dimensions[get_column_letter(i)].width = COLUMN_WIDTH


def _sanitize_filename(name: str, fallback_id: str) -> str:
    """
    Turn a channel name into a valid Windows filename (no extension).

    - empty / all-invalid -> fallback_id
    - replace invalid chars with '_'
    - truncate to FILENAME_MAX_LEN, then strip trailing '.' and ' '
    - if the result is empty (pathological), fall back to 'unnamed'
    """
    cleaned = (name or "").strip().translate(_INVALID_FILENAME_CHARS)
    cleaned = cleaned.rstrip(". ")

    if not cleaned:
        cleaned = (fallback_id or "").strip().translate(_INVALID_FILENAME_CHARS)
        cleaned = cleaned.rstrip(". ")

    cleaned = cleaned[:FILENAME_MAX_LEN].rstrip(". ")
    return cleaned or "unnamed"


def _unique_filename(base: str, channel_id: str, used: set[str]) -> str:
    """
    Return a unique filename stem, reserving it in `used`.

    Order of preference:
        1. base
        2. base__{channel_id}
        3. base__{channel_id}_2, _3, ...

    Filenames longer than FILENAME_MAX_LEN are truncated from the base,
    preserving the suffix.
    """
    if base not in used:
        used.add(base)
        return base

    suffix = f"__{channel_id}"
    max_base = max(1, FILENAME_MAX_LEN - len(suffix))
    candidate_base = base[:max_base]

    with_id = f"{candidate_base}{suffix}"
    if with_id not in used:
        used.add(with_id)
        return with_id

    counter = 2
    while True:
        counter_suffix = f"{suffix}_{counter}"
        max_base = max(1, FILENAME_MAX_LEN - len(counter_suffix))
        candidate = f"{base[:max_base]}{counter_suffix}"
        if candidate not in used:
            used.add(candidate)
            return candidate
        counter += 1


def _sanitize_sheet_name(name: str, fallback_id: str) -> str:
    """
    Turn a channel name into a valid Excel sheet name.

    - empty / all-invalid -> fallback_id
    - replace Excel-invalid chars with '_'
    - truncate to SHEET_NAME_MAX_LEN, then strip leading/trailing "'"
    - if the result is empty (pathological), fall back to 'unnamed'
    """
    cleaned = (name or "").strip().translate(_INVALID_SHEET_CHARS)
    cleaned = cleaned.strip("'")

    if not cleaned:
        cleaned = (fallback_id or "").strip().translate(_INVALID_SHEET_CHARS)
        cleaned = cleaned.strip("'")

    cleaned = cleaned[:SHEET_NAME_MAX_LEN].strip("'")
    return cleaned or "unnamed"
