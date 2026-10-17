from __future__ import annotations

import threading
from pathlib import Path
from typing import Callable

from . import fetcher, parser, reporter
from .models import ChannelResult, PipelineOptions, Selection

STAGE_FETCHING = "fetching"
STAGE_PARSING = "parsing"
STAGE_WRITING = "writing"
STAGE_DONE = "done"

StageFn = Callable[[str], None]
ProgressFn = Callable[[int, int, Selection, str], None]
ResultFn = Callable[[ChannelResult], None]


class PipelineError(Exception):
    """Raised when the pipeline aborts before producing reports."""


def run(
    selections: list[Selection],
    opts: PipelineOptions,
    *,
    on_stage: StageFn | None = None,
    on_progress: ProgressFn | None = None,
    on_result: ResultFn | None = None,
    cancel_event: threading.Event | None = None,
) -> tuple[list[ChannelResult], list[Path]]:
    """
    Execute the full pipeline.

    Returns (results, report_paths):
      * results       -- one ChannelResult per Selection, in input order
      * report_paths  -- list of .xlsx paths for successful channels;
                         empty when nothing was written (no successful
                         results, or an empty run). _errors.xlsx is not
                         included; locate it via opts.report_dir.

    Raises PipelineError only when the pipeline cannot start (for
    example pu_auth.json missing, duplicate channel ids, output
    directory not creatable) or when report writing fails. Per-channel
    failures are captured in ChannelResult.error.
    """
    if not selections:
        if on_stage:
            on_stage(STAGE_DONE)
        return [], []

    try:
        opts.out_dir.mkdir(parents=True, exist_ok=True)
        opts.html_dir.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise PipelineError(
            f"cannot create output directory {opts.out_dir}: {exc}"
        ) from exc

    results_by_cid = _build_results(selections)

    # -- fetch -----------------------------------------------------------
    if on_stage:
        on_stage(STAGE_FETCHING)
    _run_fetch(selections, opts, results_by_cid, on_progress, cancel_event)

    # -- parse -----------------------------------------------------------
    if on_stage:
        on_stage(STAGE_PARSING)
    _run_parse(selections, results_by_cid, on_result, cancel_event)

    # -- write -----------------------------------------------------------
    if on_stage:
        on_stage(STAGE_WRITING)

    ordered = [results_by_cid[sel.channel_id] for sel in selections]
    try:
        report_paths = reporter.write_reports(ordered, opts.report_dir)
    except reporter.ReportError as exc:
        raise PipelineError(f"report write failed: {exc}") from exc

    if on_stage:
        on_stage(STAGE_DONE)

    return ordered, report_paths


# ---------------------------------------------------------------------------
# Internals
# ---------------------------------------------------------------------------


def _build_results(
    selections: list[Selection],
) -> dict[str, ChannelResult]:
    """
    One ChannelResult per Selection, keyed by channel_id.

    fetch_many enforces unique channel ids, so the dict cannot silently
    collapse two selections into one entry here.
    """
    return {sel.channel_id: ChannelResult(selection=sel) for sel in selections}


def _run_fetch(
    selections: list[Selection],
    opts: PipelineOptions,
    results_by_cid: dict[str, ChannelResult],
    on_progress: ProgressFn | None,
    cancel_event: threading.Event | None,
) -> None:
    """
    Call fetcher.fetch_many and copy its outcome into each ChannelResult.

    A fetcher.FetchError raised here means the fetch stage could not
    even start (missing pu_auth.json, duplicate channel ids, ...); it is
    re-raised as PipelineError. Per-channel failures are stored in
    ChannelResult.error instead.
    """
    try:
        outcome = fetcher.fetch_many(
            selections,
            opts,
            on_progress=on_progress,
            cancel_event=cancel_event,
        )
    except fetcher.FetchError as exc:
        raise PipelineError(f"fetch setup failed: {exc}") from exc

    for sel in selections:
        res = results_by_cid[sel.channel_id]
        value = outcome.get(sel.channel_id)
        if isinstance(value, Path):
            res.html_path = value
        elif value is None:
            res.error = "ERROR: internal: no fetch outcome"
        else:
            # fetcher already returns strings prefixed with "ERROR: ".
            res.error = str(value)


def _run_parse(
    selections: list[Selection],
    results_by_cid: dict[str, ChannelResult],
    on_result: ResultFn | None,
    cancel_event: threading.Event | None,
) -> None:
    """
    Parse each fetched HTML and call on_result exactly once per selection.

    A channel already marked as failed (fetch error or cancelled) is
    reported through on_result without re-parsing. Cancellation is
    checked before each parse; a cancelled channel is marked with
    error='ERROR: cancelled' (same string fetcher uses).
    """
    for sel in selections:
        res = results_by_cid[sel.channel_id]

        if res.error is not None:
            if on_result:
                on_result(res)
            continue

        if _is_cancelled(cancel_event):
            res.error = "ERROR: cancelled"
            if on_result:
                on_result(res)
            continue

        if res.html_path is None:
            res.error = "ERROR: internal: no HTML produced"
            if on_result:
                on_result(res)
            continue

        try:
            rows, has_export = parser.parse_html(res.html_path)
            res.rows = rows
            res.has_export = has_export
        except parser.ParseError as exc:
            res.error = f"ERROR: parse failed: {exc}"
        except Exception as exc:  # noqa: BLE001
            res.error = f"ERROR: parse failed: {type(exc).__name__}: {exc}"

        if on_result:
            on_result(res)


def _is_cancelled(cancel_event: threading.Event | None) -> bool:
    return cancel_event is not None and cancel_event.is_set()
