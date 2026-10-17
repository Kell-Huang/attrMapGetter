from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

# REST API host. Overridable via credentials.json's "base_url".
DEFAULT_API_BASE_URL = "https://platform.productsup.com"

# Dataflow page host. This is the host pu_login.py authenticates against,
# and the host that pu_auth.json cookies belong to. It must match the
# host that test_playwright.py hard-codes, i.e. platform.productsup.com.
DATAFLOW_BASE_URL = "https://platform.productsup.com"

# Bounds for PipelineOptions.settle_seconds. Shared by main.py
# (argument validation) and ui.gui (input clamping) so the two entry
# points accept the same range.
SETTLE_MIN = 0
SETTLE_MAX = 600


def make_dataflow_url(
    site_id: str,
    channel_id: str,
    dataflow_base_url: str | None = None,
) -> str:
    """
    Build the slimedit dataflow URL for a (site, channel) pair.

    `dataflow_base_url` is the *dataflow* host (see DATAFLOW_BASE_URL),
    NOT the API host. When None, DATAFLOW_BASE_URL is used.
    """
    base = (dataflow_base_url or DATAFLOW_BASE_URL).rstrip("/")
    return f"{base}/data-flow/site/{site_id}/export/{channel_id}"


# ---------------------------------------------------------------------------
# API-layer entities (immutable)
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Project:
    id: str
    name: str


@dataclass(frozen=True)
class Site:
    id: str
    title: str
    project_id: str


@dataclass(frozen=True)
class Channel:
    id: str
    name: str
    site_id: str
    site_name: str
    project_id: str
    project_name: str
    url: str
    fmt: str  # "csv" | "xml"
    active: bool = True

    def to_selection(self, dataflow_base_url: str) -> "Selection":
        """
        Convert this channel into a fetchable Selection.

        `dataflow_base_url` must be the *dataflow* host, not the API
        host. See DATAFLOW_BASE_URL.
        """
        return Selection(
            project_id=self.project_id,
            project_name=self.project_name,
            site_id=self.site_id,
            site_name=self.site_name,
            channel_id=self.id,
            channel_name=self.name,
            dataflow_url=make_dataflow_url(self.site_id, self.id, dataflow_base_url),
        )


# ---------------------------------------------------------------------------
# Pipeline entities
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Selection:
    """A single (project, site, channel) triple ready to be fetched."""

    project_id: str
    project_name: str
    site_id: str
    site_name: str
    channel_id: str
    channel_name: str
    dataflow_url: str


@dataclass
class ChannelResult:
    """
    Outcome of running the pipeline on one Selection.

    `selection` is immutable after construction; the other fields are
    filled progressively by the pipeline:
        fetch  -> html_path or error
        parse  -> rows, has_export, or error
    """

    selection: Selection
    html_path: Path | None = None
    rows: list[list[str]] | None = None
    has_export: bool = False
    error: str | None = None

    def __setattr__(self, name: str, value: object) -> None:
        # Guard: selection must not be replaced after construction.
        if name == "selection" and "selection" in self.__dict__:
            raise AttributeError(
                "ChannelResult.selection is immutable after construction"
            )
        super().__setattr__(name, value)

    @property
    def succeeded(self) -> bool:
        return self.error is None and self.rows is not None


@dataclass
class PipelineOptions:
    """Runtime options for core.pipeline.run()."""

    out_dir: Path = field(default_factory=lambda: Path("output"))
    refresh: bool = False
    headless: bool = False
    settle_seconds: int = 15
    max_retries: int = 1
    retry_wait: float = 2.0

    @property
    def html_dir(self) -> Path:
        """Where slimedit_{channel_id}.txt cache files live."""
        return self.out_dir / "html"

    @property
    def report_dir(self) -> Path:
        """Where per-channel .xlsx reports and _errors.xlsx are written."""
        return self.out_dir / "reports"
