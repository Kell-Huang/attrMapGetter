from __future__ import annotations

from pathlib import Path

from attInvestigator import AttrInvestigator

# Markers required by AttrInvestigator.PageSplitter to succeed.
# export-columns is optional and controls has_export.
_REQUIRED_MARKERS = (
    "source-columns",
    "intermediate-columns",
)


class ParseError(Exception):
    """Raised when an HTML file cannot be parsed into attribute rows."""


def parse_html(html_path: Path | str) -> tuple[list[list[str]], bool]:
    """
    Parse a slimedit HTML file produced by core.fetcher.

    Returns (rows, has_export):
        rows       -- AttrInvestigator.complete_attrs after merge/filter/clear
        has_export -- True when the export-columns section was present

    Raises ParseError on any failure: bad input type, missing file,
    unreadable file, missing markers (ValueError from PageSplitter), or
    any unexpected exception raised by the upstream parsers.
    """
    # -- normalize input --------------------------------------------------
    try:
        path = html_path if isinstance(html_path, Path) else Path(html_path)
    except TypeError as exc:
        raise ParseError(
            f"html_path must be a str or Path, got " f"{type(html_path).__name__}"
        ) from exc

    if not path.exists():
        raise ParseError(f"HTML file not found: {path}")
    if not path.is_file():
        raise ParseError(f"HTML path is not a file: {path}")

    inv = AttrInvestigator()
    inv.page_file_name = str(path)

    # -- load + split -----------------------------------------------------
    try:
        inv.load_page()
    except ValueError as exc:
        # PageSplitter raises ValueError for missing source / intermediate
        # markers. Add a diagnostic listing which markers are absent.
        detail = str(exc) or "page split failed"
        detail = _append_missing_markers(detail, path)
        raise ParseError(detail) from exc
    except OSError as exc:
        raise ParseError(f"Cannot read {path}: {exc}") from exc
    except Exception as exc:  # noqa: BLE001
        raise ParseError(f"load_page failed: {type(exc).__name__}: {exc}") from exc

    # -- merge ------------------------------------------------------------
    try:
        inv.complete_attrs = inv.merge_all()
    except Exception as exc:  # noqa: BLE001
        raise ParseError(f"merge_all failed: {type(exc).__name__}: {exc}") from exc

    # -- enrich (two distinct stages, both best-effort) -------------------
    try:
        inv.filter_source()
    except Exception as exc:  # noqa: BLE001
        raise ParseError(f"filter_source failed: {type(exc).__name__}: {exc}") from exc

    try:
        inv.clear_data()
    except Exception as exc:  # noqa: BLE001
        raise ParseError(f"clear_data failed: {type(exc).__name__}: {exc}") from exc

    return inv.complete_attrs, inv.has_export


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _append_missing_markers(detail: str, path: Path) -> str:
    """
    Re-read the HTML file (best effort) and append the names of any
    required marker that is absent. Never raises.
    """
    try:
        raw = path.read_text(encoding="utf-8")
    except Exception:  # noqa: BLE001
        return detail

    missing = [m for m in _REQUIRED_MARKERS if m not in raw]
    if not missing:
        return detail
    return f"{detail} (missing markers: {', '.join(missing)})"
