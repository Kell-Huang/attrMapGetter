from __future__ import annotations

import argparse
import sys
from pathlib import Path

APP_NAME = "attrMapGetter"
APP_VERSION = "v0.1.0"


# ---------------------------------------------------------------------------
# Argument parsing
# ---------------------------------------------------------------------------


def _build_parser(settle_min: int, settle_max: int) -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="attrMapGetter",
        description=(
            "Batch fetch and parse Productsup dataflow attributes. "
            "Prompts for project / site / channel selections, downloads "
            "the slimedit HTML for each channel, parses it, and writes "
            "one Excel report (one sheet per project)."
        ),
    )
    parser.add_argument(
        "--ui",
        choices=("gui", "cli"),
        default="gui",
        help="User interface to use (default: gui).",
    )
    parser.add_argument(
        "--out",
        type=Path,
        default=Path("output"),
        metavar="DIR",
        help="Output directory for the HTML cache and report " "(default: ./output).",
    )
    parser.add_argument(
        "--refresh",
        action="store_true",
        help="Ignore cached HTML and re-fetch from Productsup.",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="Run the browser headless.",
    )
    parser.add_argument(
        "--settle",
        type=int,
        default=15,
        metavar="SECONDS",
        help=(
            f"Seconds to wait after the three markers appear "
            f"(default: 15, range {settle_min}..{settle_max})."
        ),
    )
    parser.add_argument(
        "--version",
        action="version",
        version=f"{APP_NAME} {APP_VERSION}",
    )
    return parser


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    # Import core.models early to read the shared settle bounds, but
    # defer heavier modules (openpyxl, requests, playwright) by only
    # importing PipelineOptions now.
    from core.models import SETTLE_MAX, SETTLE_MIN, PipelineOptions

    parser = _build_parser(SETTLE_MIN, SETTLE_MAX)
    args = parser.parse_args(argv)

    if not (SETTLE_MIN <= args.settle <= SETTLE_MAX):
        print(
            f"[ERROR] --settle must be between {SETTLE_MIN} and "
            f"{SETTLE_MAX} seconds.",
            file=sys.stderr,
        )
        return 1

    opts = PipelineOptions(
        out_dir=args.out,
        refresh=args.refresh,
        headless=args.headless,
        settle_seconds=args.settle,
    )

    if args.ui == "gui":
        try:
            from ui import gui
        except ImportError as exc:
            _report_gui_import_error(exc)
            return 1
        return int(gui.run(opts))

    from ui import cli

    return int(cli.run(opts))


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _report_gui_import_error(exc: ImportError) -> None:
    """
    Print a message that distinguishes 'PySide6 missing' from
    'some other import inside ui/gui failed'.

    PySide6 raises ImportError with exc.name set to 'PySide6' or one of
    its submodules (e.g. 'PySide6.QtWidgets'). Any other ImportError is
    surfaced verbatim so real bugs are not hidden behind a misleading
    'install PySide6' hint.
    """
    name = exc.name or ""
    message = str(exc)

    is_pyside = name.startswith("PySide6") or "PySide6" in message
    if is_pyside:
        print(
            "[ERROR] PySide6 is required for the GUI. "
            "Install it with 'pip install PySide6', "
            "or run with '--ui cli'.",
            file=sys.stderr,
        )
    else:
        print(
            f"[ERROR] Failed to load the GUI: {message}",
            file=sys.stderr,
        )


if __name__ == "__main__":
    sys.exit(main())
