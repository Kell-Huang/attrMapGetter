from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from playwright.sync_api import sync_playwright

LOGIN_URL = "https://platform.productsup.com"
DEFAULT_AUTH_FILE = Path("pu_auth.json")


def session_exists(auth_file: Path = DEFAULT_AUTH_FILE) -> bool:
    """True when the auth file exists and is non-empty."""
    try:
        return auth_file.exists() and auth_file.stat().st_size > 0
    except OSError:
        return False


def remove_session(auth_file: Path = DEFAULT_AUTH_FILE) -> None:
    """Delete the auth file if present. Best-effort."""
    try:
        if auth_file.exists():
            auth_file.unlink()
    except OSError:
        pass


def capture_session(
    auth_file: Path = DEFAULT_AUTH_FILE,
    *,
    on_browser_opened: Callable[[], None] | None = None,
    confirmed: threading.Event,
    cancelled: threading.Event | None = None,
    timeout_seconds: int = 600,
) -> Path:
    """
    Open a headed browser to LOGIN_URL, wait for `confirmed` to be set,
    then save storage_state to auth_file.

    The caller is expected to:
      * start this function on a worker thread
      * set `confirmed` from the UI thread once the user says they
        are done logging in
      * optionally set `cancelled` (must also set `confirmed`) to
        abort early

    Raises:
        RuntimeError: cancelled or timed out.
        Exception:    any Playwright or I/O error.
    """
    auth_file = auth_file if isinstance(auth_file, Path) else Path(auth_file)

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=False)
        try:
            context = browser.new_context()
            page = context.new_page()
            page.goto(LOGIN_URL, wait_until="commit", timeout=60_000)
            if on_browser_opened is not None:
                on_browser_opened()

            confirmed_set = confirmed.wait(timeout=timeout_seconds)
            if cancelled is not None and cancelled.is_set():
                raise RuntimeError("Login cancelled.")
            if not confirmed_set:
                raise RuntimeError(f"Login timed out after {timeout_seconds} seconds.")

            context.storage_state(path=str(auth_file))
        finally:
            try:
                browser.close()
            except Exception:  # noqa: BLE001
                pass

    return auth_file
