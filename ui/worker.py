from __future__ import annotations

import threading
from pathlib import Path

from PySide6.QtCore import QObject, QThread, Signal

from core import pipeline
from core.models import ChannelResult, PipelineOptions, Selection


class PipelineWorker(QThread):
    """
    Run core.pipeline.run in a dedicated thread (single-use).

    Signals:
        stage_changed(str)          one of "fetching", "parsing",
                                    "writing", "done"
        progress(int, int, object, str)
                                    (done, total, Selection, status)
        result_ready(object)        a ChannelResult, once per selection
        succeeded(object, object)   (list[ChannelResult], list[Path])
                                    list[Path] is the per-channel report
                                    paths; _errors.xlsx is NOT included
                                    (locate it via opts.report_dir).
        failed(str)                 fatal PipelineError message

    Usage (GUI side):
        self._worker = PipelineWorker(selections, opts)
        self._worker.stage_changed.connect(on_stage)
        self._worker.progress.connect(on_progress)
        self._worker.result_ready.connect(on_result)
        self._worker.succeeded.connect(on_done)
        self._worker.failed.connect(on_error)
        self._worker.start()

        # to cancel:
        self._worker.cancel()

    Lifecycle: single-use. Create a new instance for each run.
    """

    stage_changed = Signal(str)
    progress = Signal(int, int, object, str)
    result_ready = Signal(object)
    succeeded = Signal(object, object)
    failed = Signal(str)

    def __init__(
        self,
        selections: list[Selection],
        opts: PipelineOptions,
        parent: QObject | None = None,
    ) -> None:
        super().__init__(parent)
        self._selections: list[Selection] = list(selections)
        self._opts = opts
        self._cancel_event = threading.Event()

    @property
    def cancel_event(self) -> threading.Event:
        return self._cancel_event

    def cancel(self) -> None:
        """
        Request cooperative cancellation.

        The pipeline checks this flag between channels and stages. A
        Playwright navigation already in flight is not aborted; the
        worker will stop before the next channel starts.
        """
        self._cancel_event.set()

    def run(self) -> None:  # noqa: D401
        """Executed on the worker thread by QThread.start()."""
        try:
            results, report_paths = pipeline.run(
                self._selections,
                self._opts,
                on_stage=self._emit_stage,
                on_progress=self._emit_progress,
                on_result=self._emit_result,
                cancel_event=self._cancel_event,
            )
        except pipeline.PipelineError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return

        self.succeeded.emit(results, report_paths)

    def _emit_stage(self, stage: str) -> None:
        self.stage_changed.emit(stage)

    def _emit_progress(
        self,
        done: int,
        total: int,
        sel: Selection,
        status: str,
    ) -> None:
        self.progress.emit(done, total, sel, status)

    def _emit_result(self, res: ChannelResult) -> None:
        self.result_ready.emit(res)


def default_options(out_dir: Path | str = "output") -> PipelineOptions:
    """Build a PipelineOptions with sane GUI defaults."""
    return PipelineOptions(out_dir=Path(out_dir))
