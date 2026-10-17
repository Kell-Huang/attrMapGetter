"""
UI layer for the attrMapGetter pipeline.

Two entry points are provided:

    * ui.gui  -- PySide6 desktop app (default)
    * ui.cli  -- terminal menu, no Qt dependency

Both consume the same core.pipeline API and never touch Playwright or
openpyxl directly. UI code is the only place allowed to call input(),
print(), or instantiate Qt widgets.
"""

__all__ = ["cli", "gui"]
