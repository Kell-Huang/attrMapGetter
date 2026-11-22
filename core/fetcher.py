from __future__ import annotations

import queue
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Callable

from playwright.sync_api import TimeoutError as PlaywrightTimeout
from playwright.sync_api import sync_playwright

from .models import PipelineOptions, Selection

# -- Constants --------------------------------------------------------------

FRAME_HOST = "dataflow.productsup.com"
IFRAME_SELECTOR = "iframe#frame"
CONTAINER_SELECTOR = "#dataflow-container"
MARKERS = (
    "source-columns",
    "intermediate-columns",
    "export-columns",
)

NAV_TIMEOUT_MS = 60_000
CONTAINER_TIMEOUT_MS = 60_000
MARKER_TIMEOUT_MS = 60_000
FRAME_DISCOVERY_TIMEOUT_MS = 300_000
CONTENT_READY_TIMEOUT_MS = 20_000
CONTENT_STABLE_TIMEOUT_S = 120
CONTENT_STABLE_POLL_S = 1.5

# Control concurrency (2-4 is recommended to avoid rate limits)
MAX_CONCURRENT_WORKERS = 3


class FetchError(Exception):
    """Raised when a dataflow HTML cannot be retrieved."""


# Signature: (done, total, selection, status)
#   status in {"cached", "ok", "ok (retry N)", "failed", "cancelled"}
ProgressFn = Callable[[int, int, Selection, str], None]


def html_path_for(opts: PipelineOptions, channel_id: str) -> Path:
    """Where the slimedit HTML for a channel is stored."""
    return opts.html_dir / f"slimedit_{channel_id}.txt"


# ---------------------------------------------------------------------------
# Frame discovery
# ---------------------------------------------------------------------------


def _wait_for_dataflow_frame(
    page,
    timeout_ms: int = FRAME_DISCOVERY_TIMEOUT_MS,
    cancel_event: threading.Event | None = None,
):
    """
    Poll until a frame whose URL contains FRAME_HOST appears.
    """
    deadline = time.time() + timeout_ms / 1000.0
    last_seen: list[str] = []

    while time.time() < deadline:
        if _is_cancelled(cancel_event):
            raise FetchError("cancelled while discovering frame")

        last_seen = []

        for frame in page.frames:
            try:
                url = frame.url or ""
            except Exception:  # noqa: BLE001
                continue
            if url and url not in last_seen:
                last_seen.append(url)
            if FRAME_HOST in url:
                return frame

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


# ---------------------------------------------------------------------------
# Content stability
# ---------------------------------------------------------------------------


def _wait_content_stable(
    frame,
    cancel_event: threading.Event | None,
) -> None:
    """
    Block until the three dataflow sections stop growing.
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
# Single fetch
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
# Concurrency & Batch Fetch
# ---------------------------------------------------------------------------


def _thread_worker(
    task_queue: queue.Queue,
    total: int,
    opts: PipelineOptions,
    auth_file: Path,
    results: dict[str, Path | str],
    results_lock: threading.Lock,
    progress_state: dict[str, int],
    progress_lock: threading.Lock,
    worker_errors: list[str],
    worker_errors_lock: threading.Lock,
    on_progress: ProgressFn | None,
    cancel_event: threading.Event | None,
) -> None:
    """Worker thread that runs its own isolated Playwright instance."""
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(channel="msedge", headless=opts.headless)
            try:
                context = browser.new_context(
                    storage_state=str(auth_file),
                    ignore_https_errors=True,
                    )
                try:
                    while True:
                        if _is_cancelled(cancel_event):
                            break

                        try:
                            sel = task_queue.get_nowait()
                        except queue.Empty:
                            break

                        status = "failed"
                        try:
                            status = _process_one(
                                context,
                                sel,
                                opts,
                                results,
                                results_lock,
                                cancel_event,
                            )
                        except Exception as exc:  # noqa: BLE001
                            with results_lock:
                                if sel.channel_id not in results:
                                    results[sel.channel_id] = (
                                        f"ERROR: {type(exc).__name__}: {exc}"
                                    )
                        finally:
                            with progress_lock:
                                progress_state["completed"] += 1
                                done = progress_state["completed"]
                            if on_progress:
                                try:
                                    on_progress(done, total, sel, status)
                                except Exception:  # noqa: BLE001
                                    pass
                            task_queue.task_done()
                finally:
                    _safe_close(context)
            finally:
                _safe_close(browser)
    except Exception as exc:  # noqa: BLE001
        with worker_errors_lock:
            worker_errors.append(f"{type(exc).__name__}: {exc}")


def fetch_many(
    selections: list[Selection],
    opts: PipelineOptions,
    *,
    auth_file: Path = Path("pu_auth.json"),
    on_progress: ProgressFn | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Path | str]:
    """
    Fetch all selections concurrently, using isolated browsers per thread.
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
    todo: list[Selection] = []
    total = len(selections)

    progress_state = {"completed": 0}
    progress_lock = threading.Lock()

    worker_errors = []
    worker_errors_lock = threading.Lock()

    for sel in selections:
        out_file = html_path_for(opts, sel.channel_id)
        if out_file.exists() and not opts.refresh and _is_non_empty_file(out_file):
            results[sel.channel_id] = out_file
            with progress_lock:
                progress_state["completed"] += 1
                done = progress_state["completed"]
            if on_progress:
                try:
                    on_progress(done, total, sel, "cached")
                except Exception:  # noqa: BLE001
                    pass
        else:
            todo.append(sel)

    if not todo:
        return results

    task_queue = queue.Queue()
    for sel in todo:
        task_queue.put(sel)

    results_lock = threading.Lock()

    with ThreadPoolExecutor(max_workers=MAX_CONCURRENT_WORKERS) as executor:
        for _ in range(MAX_CONCURRENT_WORKERS):
            executor.submit(
                _thread_worker,
                task_queue,
                total,
                opts,
                auth_file,
                results,
                results_lock,
                progress_state,
                progress_lock,
                worker_errors,
                worker_errors_lock,
                on_progress,
                cancel_event,
            )

    worker_error_msg = ""
    if worker_errors:
        worker_error_msg = " | ".join(worker_errors)

    # Drain remaining tasks if cancelled or if all worker threads crashed early
    while True:
        try:
            sel = task_queue.get_nowait()
        except queue.Empty:
            break

        err_text = "ERROR: cancelled"
        if worker_error_msg:
            err_text = f"ERROR: worker failed ({worker_error_msg})"

        with results_lock:
            if sel.channel_id not in results:
                results[sel.channel_id] = err_text

        with progress_lock:
            progress_state["completed"] += 1
            done = progress_state["completed"]

        if on_progress:
            try:
                status = "failed" if worker_error_msg else "cancelled"
                on_progress(done, total, sel, status)
            except Exception:  # noqa: BLE001
                pass

    return results


def _process_one(
    context,
    sel: Selection,
    opts: PipelineOptions,
    results: dict[str, Path | str],
    results_lock: threading.Lock,
    cancel_event: threading.Event | None,
) -> str:
    """Fetch one channel with retry / cancellation. Thread-safe.
    Returns final status string for the progress callback.
    """
    if _is_cancelled(cancel_event):
        with results_lock:
            results[sel.channel_id] = "ERROR: cancelled"
        return "cancelled"

    out_file = html_path_for(opts, sel.channel_id)
    last_err = ""
    succeeded = False
    attempt = 0

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

        with results_lock:
            results[sel.channel_id] = out_file
        succeeded = True
        break

    if succeeded:
        return "ok" if attempt == 0 else f"ok (retry {attempt})"

    if _is_cancelled(cancel_event):
        with results_lock:
            results[sel.channel_id] = "ERROR: cancelled"
        return "cancelled"

    with results_lock:
        results[sel.channel_id] = f"ERROR: {last_err or 'unknown error'}"
    return "failed"


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
