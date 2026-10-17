from __future__ import annotations

import threading
import time
from pathlib import Path
from typing import Callable

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from .models import PipelineOptions, Selection

# -- Constants mirroring test_playwright.py ---------------------------------

FRAME_HOST = "dataflow.productsup.com"
IFRAME_SELECTOR = "iframe#frame"
CONTAINER_SELECTOR = "#dataflow-container"
MARKERS = (
    "source-columns",
    "intermediate-columns",
    "export-columns",
)

NAV_TIMEOUT_MS = 60_000
CONTAINER_TIMEOUT_MS = 180_000
MARKER_TIMEOUT_MS = 180_000
CONTENT_STABLE_TIMEOUT_S = 120
CONTENT_STABLE_POLL_S = 1.5
CONTENT_READY_TIMEOUT_MS = 20_000
FRAME_DISCOVERY_TIMEOUT_MS = 300_000


class FetchError(Exception):
    """Raised when a dataflow HTML cannot be retrieved."""


# Signature: (done, total, selection, status)
#   status in {"cached", "ok", "ok (retry N)", "failed", "cancelled"}
ProgressFn = Callable[[int, int, Selection, str], None]


def html_path_for(opts: PipelineOptions, channel_id: str) -> Path:
    """Where the slimedit HTML for a channel is stored."""
    return opts.html_dir / f"slimedit_{channel_id}.txt"


# ---------------------------------------------------------------------------
# Frame discovery (mirrors test_playwright.wait_for_dataflow_frame)
# ---------------------------------------------------------------------------


def _wait_for_dataflow_frame(
    page,
    timeout_ms: int = FRAME_DISCOVERY_TIMEOUT_MS,
    cancel_event: threading.Event | None = None,
):
    """
    Poll until a frame whose URL contains FRAME_HOST appears.

    Strategy mirrors test_playwright.py exactly:
      1. Scan page.frames first.
      2. Fall back to iframe#frame's content_frame.
    last_seen is reset on every iteration, same as the reference script.
    """
    deadline = time.time() + timeout_ms / 1000.0
    last_seen: list[str] = []

    while time.time() < deadline:
        if _is_cancelled(cancel_event):
            raise FetchError("cancelled while discovering frame")

        last_seen = []

        # --- 1. scan page.frames --------------------------------------
        for frame in page.frames:
            try:
                url = frame.url or ""
            except Exception:  # noqa: BLE001
                continue
            if url and url not in last_seen:
                last_seen.append(url)
            if FRAME_HOST in url:
                return frame

        # --- 2. fallback: iframe#frame's content_frame ----------------
        try:
            el = page.query_selector(IFRAME_SELECTOR)
            cf = el.content_frame() if el else None
        except Exception:  # noqa: BLE001
            cf = None
        if cf is not None:
            try:
                url = cf.url or ""
            except Exception:  # noqa: BLE001
                url = ""
            if url and url not in last_seen:
                last_seen.append(url)
            if FRAME_HOST in url:
                return cf

        time.sleep(1)

    raise FetchError(
        "dataflow frame not found. URLs seen:\n  " + "\n  ".join(last_seen)
    )


def _wait_content_stable(
    frame,
    cancel_event: threading.Event | None,
) -> None:
    """
    Block until the three dataflow sections stop growing.

    The page renders attribute lists asynchronously and can recycle
    already-rendered DOM nodes after roughly 30 seconds. A fixed settle
    delay cannot fit both cases: short pages waste time, long pages get
    truncated. Instead, poll the <li> count of each section until three
    consecutive samples agree, then return.

    Returns early on cancellation or on the overall timeout; in either
    case the caller proceeds with whatever is currently on the page.
    """
    js = """() => {
        const keys = ['source', 'intermediate', 'export'];
        return keys.map(k => {
            const td = document.querySelector(
                'td[class*="' + k + '-columns"]'
            );
            return td ? td.querySelectorAll('li').length : 0;
        });
    }"""
    deadline = time.time() + CONTENT_STABLE_TIMEOUT_S
    prev: list[int] | None = None
    stable_rounds = 0

    while time.time() < deadline:
        if _is_cancelled(cancel_event):
            return

        try:
            counts = frame.evaluate(js)
        except Exception:  # noqa: BLE001
            return

        if not isinstance(counts, list) or len(counts) != 3:
            return

        # The source section is required. While it is still empty the
        # page is in its initial loading state; samples of [0, 0, 0]
        # are not stable content and must not be captured.
        if counts[0] == 0:
            prev = None
            stable_rounds = 0
            _sleep_interruptibly(CONTENT_STABLE_POLL_S, cancel_event)
            continue

        if prev is not None and counts == prev:
            stable_rounds += 1
            if stable_rounds >= 2:
                return
        else:
            stable_rounds = 0

        prev = counts
        _sleep_interruptibly(CONTENT_STABLE_POLL_S, cancel_event)


def _wait_content_stable(
    frame,
    cancel_event: threading.Event | None,
) -> None:
    """
    Block until the three dataflow sections stop growing.

    The page renders attribute lists asynchronously and can recycle
    already-rendered DOM nodes after roughly 30 seconds. A fixed settle
    delay cannot fit both cases: short pages waste time, long pages get
    truncated. Instead, poll the <li> count of each section until three
    consecutive samples agree, then return.

    Returns early on cancellation or on the overall timeout; in either
    case the caller proceeds with whatever is currently on the page.
    """
    js = """() => {
        const keys = ['source', 'intermediate', 'export'];
        return keys.map(k => {
            const td = document.querySelector(
                'td[class*="' + k + '-columns"]'
            );
            return td ? td.querySelectorAll('li').length : 0;
        });
    }"""
    deadline = time.time() + CONTENT_STABLE_TIMEOUT_S
    prev: list[int] | None = None
    stable_rounds = 0

    while time.time() < deadline:
        if _is_cancelled(cancel_event):
            return

        try:
            counts = frame.evaluate(js)
        except Exception:  # noqa: BLE001
            return

        if not isinstance(counts, list) or len(counts) != 3:
            return

        # The source section is required. While it is still empty the
        # page is in its initial loading state; samples of [0, 0, 0]
        # are not stable content and must not be captured.
        if counts[0] == 0:
            prev = None
            stable_rounds = 0
            _sleep_interruptibly(CONTENT_STABLE_POLL_S, cancel_event)
            continue

        if prev is not None and counts == prev:
            stable_rounds += 1
            if stable_rounds >= 2:
                return
        else:
            stable_rounds = 0

        prev = counts
        _sleep_interruptibly(CONTENT_STABLE_POLL_S, cancel_event)


# ---------------------------------------------------------------------------
# Single fetch (uses an already-open context)
# ---------------------------------------------------------------------------


def _fetch_one(
    context,
    sel: Selection,
    out_file: Path,
    settle_seconds: int,
    cancel_event: threading.Event | None = None,
) -> Path:
    """
    Fetch one channel's dataflow page and write its HTML to out_file.

    Steps:
      1. Navigate to the dataflow URL.
      2. Locate the dataflow iframe.
      3. Wait for the container and all three section shells.
      4. Poll the DOM until attribute counts stop changing.
      5. Sleep the caller-configured settle as a final cushion.
      6. Serialize the iframe HTML and write it to disk.
      7. Verify every expected marker is present in the output.

    Any failure raises FetchError so the caller can retry.
    """
    if not sel.channel_id:
        raise FetchError("selection has empty channel_id")
    if not sel.dataflow_url:
        raise FetchError("selection has empty dataflow_url")

    page = context.new_page()
    try:
        try:
            page.goto(
                sel.dataflow_url,
                wait_until="commit",
                timeout=NAV_TIMEOUT_MS,
            )
        except Exception as exc:  # noqa: BLE001
            raise FetchError(f"navigation failed: {type(exc).__name__}: {exc}") from exc

        frame = _wait_for_dataflow_frame(page, cancel_event=cancel_event)

        try:
            frame.wait_for_selector(
                CONTAINER_SELECTOR,
                timeout=CONTAINER_TIMEOUT_MS,
            )
        except Exception as exc:  # noqa: BLE001
            raise FetchError(
                f"container selector not found: " f"{type(exc).__name__}: {exc}"
            ) from exc

        for marker in MARKERS:
            try:
                frame.wait_for_selector(
                    f'td[class*="{marker}"]',
                    timeout=MARKER_TIMEOUT_MS,
                )
            except Exception as exc:  # noqa: BLE001
                raise FetchError(
                    f"marker '{marker}' not found: " f"{type(exc).__name__}: {exc}"
                ) from exc

        if _is_cancelled(cancel_event):
            raise FetchError("cancelled before content wait")

        _wait_content_stable(frame, cancel_event)

        if _is_cancelled(cancel_event):
            raise FetchError("cancelled after content wait")

        if settle_seconds > 0:
            _sleep_interruptibly(settle_seconds, cancel_event)
            if _is_cancelled(cancel_event):
                raise FetchError("cancelled during settle")

        try:
            html = frame.content()
        except Exception as exc:  # noqa: BLE001
            raise FetchError(f"content() failed: {type(exc).__name__}: {exc}") from exc
    finally:
        try:
            page.close()
        except Exception:  # noqa: BLE001
            pass

    if not html.strip():
        raise FetchError("fetched HTML is empty")

    out_file.parent.mkdir(parents=True, exist_ok=True)
    out_file.write_text(html, encoding="utf-8")

    missing = [m for m in MARKERS if m not in html]
    if missing:
        raise FetchError(f"marker(s) missing in fetched HTML: {', '.join(missing)}")

    return out_file


# ---------------------------------------------------------------------------
# Batch fetch
# ---------------------------------------------------------------------------


def fetch_many(
    selections: list[Selection],
    opts: PipelineOptions,
    *,
    auth_file: Path = Path("pu_auth.json"),
    on_progress: ProgressFn | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Path | str]:
    """
    Fetch all selections serially, reusing one Playwright context.

    Returns {channel_id: Path | "ERROR: <message>"}.
    Cache is reused unless opts.refresh is True. Empty cache files are
    treated as absent.

    Cancellation semantics: cancel_event is checked between channels,
    between retries, during frame discovery, and during settle. An
    in-flight Playwright navigation is not aborted mid-call; the next
    cancellation check happens as soon as control returns to this
    module. Cancelled channels are reported as "ERROR: cancelled".

    Raises FetchError if auth_file is missing or channel_ids are not
    unique across the input list.
    """
    if not isinstance(auth_file, Path):
        auth_file = Path(auth_file)
    if not auth_file.exists():
        raise FetchError(f"{auth_file} not found. Run 'python pu_login.py' first.")

    seen: set[str] = set()
    for sel in selections:
        if not sel.channel_id:
            raise FetchError(
                "selection has empty channel_id: "
                f"site={sel.site_id} url={sel.dataflow_url}"
            )
        if sel.channel_id in seen:
            raise FetchError(f"duplicate channel_id in selections: {sel.channel_id}")
        seen.add(sel.channel_id)

    opts.html_dir.mkdir(parents=True, exist_ok=True)

    results: dict[str, Path | str] = {}
    todo: list[tuple[int, Selection]] = []
    total = len(selections)

    for index, sel in enumerate(selections, start=1):
        out_file = html_path_for(opts, sel.channel_id)
        if out_file.exists() and not opts.refresh and _is_non_empty_file(out_file):
            results[sel.channel_id] = out_file
            if on_progress:
                on_progress(index, total, sel, "cached")
        else:
            todo.append((index, sel))

    if not todo:
        return results

    with sync_playwright() as pw:
        browser = pw.chromium.launch(channel="msedge", headless=opts.headless)
        try:
            context = browser.new_context(storage_state=str(auth_file))
            try:
                for index, sel in todo:
                    _process_one(
                        context,
                        index,
                        total,
                        sel,
                        opts,
                        results,
                        on_progress,
                        cancel_event,
                    )
            finally:
                _safe_close(context)
        finally:
            _safe_close(browser)

    return results


# ---------------------------------------------------------------------------
# Per-channel worker
# ---------------------------------------------------------------------------


def _process_one(
    context,
    index: int,
    total: int,
    sel: Selection,
    opts: PipelineOptions,
    results: dict[str, Path | str],
    on_progress: ProgressFn | None,
    cancel_event: threading.Event | None,
) -> None:
    """Fetch one channel with retry / cancellation. Mutates `results`."""
    if _is_cancelled(cancel_event):
        results[sel.channel_id] = "ERROR: cancelled"
        if on_progress:
            on_progress(index, total, sel, "cancelled")
        return

    out_file = html_path_for(opts, sel.channel_id)
    last_err = ""
    succeeded = False

    for attempt in range(opts.max_retries + 1):
        if _is_cancelled(cancel_event):
            break
        try:
            _fetch_one(
                context,
                sel,
                out_file,
                opts.settle_seconds,
                cancel_event,
            )
        except FetchError as exc:
            last_err = str(exc)
            if attempt < opts.max_retries:
                _sleep_interruptibly(opts.retry_wait, cancel_event)
            continue
        except Exception as exc:  # noqa: BLE001
            last_err = f"{type(exc).__name__}: {exc}"
            break

        results[sel.channel_id] = out_file
        if on_progress:
            status = "ok" if attempt == 0 else f"ok (retry {attempt})"
            on_progress(index, total, sel, status)
        succeeded = True
        break

    if succeeded:
        return

    if _is_cancelled(cancel_event):
        results[sel.channel_id] = "ERROR: cancelled"
        if on_progress:
            on_progress(index, total, sel, "cancelled")
        return

    results[sel.channel_id] = f"ERROR: {last_err or 'unknown error'}"
    if on_progress:
        on_progress(index, total, sel, "failed")


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _is_non_empty_file(path: Path) -> bool:
    try:
        return path.stat().st_size > 0
    except OSError:
        return False


def _is_cancelled(cancel_event: threading.Event | None) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def _sleep_interruptibly(seconds: float, cancel_event: threading.Event | None) -> None:
    """Sleep in small slices so cancellation is noticed quickly."""
    if seconds <= 0:
        return
    slice_ = 0.2
    elapsed = 0.0
    while elapsed < seconds:
        if _is_cancelled(cancel_event):
            return
        time.sleep(min(slice_, seconds - elapsed))
        elapsed += slice_


def _safe_close(obj) -> None:
    try:
        obj.close()
    except Exception:  # noqa: BLE001
        pass
