from __future__ import annotations

import sys
import threading
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QObject, Qt, QThread, QTimer, Signal
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFrame,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QPlainTextEdit,
    QProgressBar,
    QPushButton,
    QSplitter,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core import api as pu_api
from core import session as pu_session
from core.credentials import CredentialsError, load_credentials, redact_token
from core.models import (
    SETTLE_MAX,
    SETTLE_MIN,
    Channel,
    PipelineOptions,
    Project,
    Selection,
    Site,
)
from core.pipeline import STAGE_DONE, STAGE_FETCHING, STAGE_PARSING, STAGE_WRITING
from core.reporter import ERRORS_FILENAME

from .worker import PipelineWorker

APP_TITLE = "attrMapGetter"
APP_SUBTITLE = "Batch fetch and parse Productsup dataflow attributes"
APP_VERSION = "v0.1.0"


def _make_card(title: str) -> tuple[QFrame, QVBoxLayout]:
    card = QFrame()
    card.setProperty("card", True)
    layout = QVBoxLayout(card)
    layout.setContentsMargins(10, 8, 10, 10)
    layout.setSpacing(6)

    header = QLabel(title)
    header.setProperty("panelTitle", True)
    layout.addWidget(header)
    return card, layout


def _make_flat_button(text: str) -> QPushButton:
    button = QPushButton(text)
    button.setProperty("flat", True)
    return button


def _make_secondary_button(text: str) -> QPushButton:
    button = QPushButton(text)
    button.setProperty("secondary", True)
    return button


class _ApiWorker(QThread):
    loaded = Signal(object)
    failed = Signal(str)

    def __init__(self, fn, *args, parent=None):
        super().__init__(parent)
        self._fn = fn
        self._args = args

    def run(self) -> None:  # noqa: D401
        try:
            result = self._fn(*self._args)
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(redact_token(f"{type(exc).__name__}: {exc}"))
            return
        self.loaded.emit(result)


class LoginWorker(QThread):
    """Run pu_session.capture_session off the GUI thread."""

    browser_opened = Signal()
    finished_ok = Signal(object)  # Path
    failed = Signal(str)

    def __init__(self, auth_file: Path, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self._auth_file = auth_file
        self._confirmed = threading.Event()
        self._cancelled = threading.Event()

    def confirm(self) -> None:
        self._confirmed.set()

    def cancel(self) -> None:
        self._cancelled.set()
        self._confirmed.set()  # unblock the wait in capture_session

    def run(self) -> None:  # noqa: D401
        try:
            pu_session.capture_session(
                self._auth_file,
                on_browser_opened=self.browser_opened.emit,
                confirmed=self._confirmed,
                cancelled=self._cancelled,
            )
        except RuntimeError as exc:
            self.failed.emit(str(exc))
            return
        except Exception as exc:  # noqa: BLE001
            self.failed.emit(f"{type(exc).__name__}: {exc}")
            return
        self.finished_ok.emit(self._auth_file)


class LoginDialog(QDialog):
    """
    Modal prompt shown while the user logs in via the headed browser.

    The [I have logged in] button is disabled until the browser
    signals that the login page has loaded.
    """

    def __init__(self, worker: LoginWorker, parent=None) -> None:
        super().__init__(parent)
        self._worker = worker
        self.setWindowTitle("Login to Productsup")
        self.setModal(True)
        self.setMinimumWidth(440)

        layout = QVBoxLayout(self)

        text = QLabel(
            "A browser window is opening.\n\n"
            "Wait for [I have logged in] below to become enabled, then "
            "log in to Productsup in that browser window. Once you can "
            "see the projects list, click [I have logged in]."
        )
        text.setWordWrap(True)
        layout.addWidget(text)

        buttons = QDialogButtonBox(self)
        self._confirm_btn = buttons.addButton(
            "I have logged in", QDialogButtonBox.AcceptRole
        )
        self._cancel_btn = buttons.addButton("Cancel", QDialogButtonBox.RejectRole)
        self._confirm_btn.setEnabled(False)
        self._cancel_btn.clicked.connect(self.reject)
        self._confirm_btn.clicked.connect(self._on_confirm)
        layout.addWidget(buttons)

    def _on_confirm(self) -> None:
        self._worker.confirm()
        self.accept()

    def enable_confirm(self) -> None:
        self._confirm_btn.setEnabled(True)


class AttrMapGetterWindow(QMainWindow):
    def __init__(self) -> None:
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setObjectName("mainWindow")
        self.resize(1280, 800)

        self._client: pu_api.PuApiClient | None = None
        self._credentials: dict | None = None
        self._base_url: str = ""
        self._dataflow_base_url: str = ""

        self._project_cache: dict[str, Project] = {}
        self._site_cache: dict[str, Site] = {}
        self._channel_cache: dict[str, Channel] = {}

        self._checked_channels: set[str] = set()
        self._site_intent: set[str] = set()
        self._project_intent: set[str] = set()

        self._suppress_changed = False
        self._suppress_expand = False
        self._suppress_expand = False
        self._search_timer = QTimer(self)
        self._search_timer.setSingleShot(True)
        self._search_timer.setInterval(200)
        self._search_timer.timeout.connect(self._flush_search)
        self._api_workers: list[QThread] = []
        self._worker: PipelineWorker | None = None
        # Directory the running worker actually writes to. Captured at
        # Start time so callbacks read the same value even if the user
        # edits the Options card mid-run.
        self._active_reports_dir: Path | None = None
        # Login state and worker.
        self._session_ok: bool = False
        self._login_worker: LoginWorker | None = None
        self._login_dialog: LoginDialog | None = None

        self._build_ui()

        QTimer.singleShot(0, self._bootstrap)

    def _build_ui(self) -> None:
        central = QWidget(self)
        central.setObjectName("centralWidget")
        self.setCentralWidget(central)

        root = QVBoxLayout(central)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)

        root.addWidget(self._build_header())
        root.addWidget(self._build_splitter(), 1)
        root.addWidget(self._build_status_bar())

        self._refresh_start_enabled()

    def _build_header(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("headerFrame")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(12, 8, 12, 8)
        layout.setSpacing(8)

        title = QLabel(APP_TITLE)
        title.setObjectName("titleLabel")
        subtitle = QLabel(APP_SUBTITLE)
        subtitle.setObjectName("subtitleLabel")
        version = QLabel(APP_VERSION)
        version.setObjectName("versionLabel")

        text_box = QVBoxLayout()
        text_box.setContentsMargins(0, 0, 0, 0)
        text_box.setSpacing(2)
        text_box.addWidget(title)
        text_box.addWidget(subtitle)

        self._start_button = QPushButton("Start")
        self._start_button.clicked.connect(self._on_start_clicked)

        self._login_button = QPushButton("Log in")
        self._login_button.setProperty("secondary", True)
        self._login_button.clicked.connect(self._on_login_clicked)

        self._cancel_button = _make_secondary_button("Cancel")
        self._cancel_button.setEnabled(False)
        self._cancel_button.clicked.connect(self._on_cancel_clicked)

        layout.addLayout(text_box)
        layout.addStretch(1)
        layout.addWidget(version)
        layout.addSpacing(12)
        layout.addWidget(self._login_button)
        layout.addWidget(self._start_button)
        layout.addWidget(self._cancel_button)
        return frame

    def _build_splitter(self) -> QWidget:
        splitter = QSplitter(Qt.Horizontal)
        splitter.addWidget(self._build_tree_card())
        splitter.addWidget(self._build_right_column())
        splitter.setStretchFactor(0, 55)
        splitter.setStretchFactor(1, 45)
        splitter.setSizes([700, 580])
        return splitter

    def _build_tree_card(self) -> QWidget:
        card, layout = _make_card("Channels")

        search_row = QHBoxLayout()
        self._search_edit = QLineEdit()
        self._search_edit.setObjectName("searchInput")
        self._search_edit.setPlaceholderText("Search by name or id ...")
        self._search_edit.textChanged.connect(self._on_search_input)
        expand_btn = _make_flat_button("Expand all")
        collapse_btn = _make_flat_button("Collapse all")
        expand_btn.clicked.connect(lambda: self._tree.expandAll())
        collapse_btn.clicked.connect(lambda: self._tree.collapseAll())
        search_row.addWidget(self._search_edit, 1)
        search_row.addWidget(expand_btn)
        search_row.addWidget(collapse_btn)

        self._tree = QTreeWidget()
        self._tree.setHeaderHidden(True)
        self._tree.setUniformRowHeights(True)
        self._tree.itemExpanded.connect(self._on_item_expanded)
        self._tree.itemChanged.connect(self._on_item_changed)

        hint = QLabel("Check Project, Site or Channel. Tri-state cascades both ways.")
        hint.setObjectName("hintLabel")

        layout.addLayout(search_row)
        layout.addWidget(self._tree, 1)
        layout.addWidget(hint)
        return card

    def _build_right_column(self) -> QWidget:
        wrapper = QWidget()
        layout = QVBoxLayout(wrapper)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(8)
        layout.addWidget(self._build_options_card())
        layout.addWidget(self._build_log_card(), 1)
        return wrapper

    def _build_options_card(self) -> QWidget:
        card, layout = _make_card("Options")

        self._refresh_check = QCheckBox("Force refresh HTML (ignore cache)")
        self._headless_check = QCheckBox("Run browser headless")
        self._refresh_check.setChecked(False)
        self._headless_check.setChecked(False)

        settle_row = QHBoxLayout()
        settle_label = QLabel("Settle seconds:")
        settle_label.setObjectName("sectionLabel")
        self._settle_edit = QLineEdit("15")
        self._settle_edit.setObjectName("searchInput")
        self._settle_edit.setFixedWidth(60)
        settle_row.addWidget(settle_label)
        settle_row.addWidget(self._settle_edit)
        settle_row.addStretch(1)

        out_row = QHBoxLayout()
        out_label = QLabel("Output dir:")
        out_label.setObjectName("sectionLabel")
        self._out_edit = QLineEdit("output")
        self._out_edit.setObjectName("searchInput")
        browse_btn = _make_secondary_button("Browse ...")
        browse_btn.clicked.connect(self._on_browse_out_dir)
        out_row.addWidget(out_label)
        out_row.addWidget(self._out_edit, 1)
        out_row.addWidget(browse_btn)

        layout.addWidget(self._refresh_check)
        layout.addWidget(self._headless_check)
        layout.addLayout(settle_row)
        layout.addLayout(out_row)
        return card

    def _build_log_card(self) -> QWidget:
        card, layout = _make_card("Log")
        self._log_view = QPlainTextEdit()
        self._log_view.setObjectName("logTextEdit")
        self._log_view.setReadOnly(True)
        self._log_view.setMaximumBlockCount(5000)
        layout.addWidget(self._log_view, 1)
        return card

    def _build_status_bar(self) -> QWidget:
        frame = QFrame()
        frame.setObjectName("statusBarFrame")
        layout = QHBoxLayout(frame)
        layout.setContentsMargins(12, 6, 12, 6)
        layout.setSpacing(10)

        self._status_label = QLabel("Ready")
        self._status_label.setObjectName("statusLabel")

        self._progress_bar = QProgressBar()
        self._progress_bar.setObjectName("progressBar")
        self._progress_bar.setRange(0, 100)
        self._progress_bar.setValue(0)
        self._progress_bar.setTextVisible(False)

        self._progress_label = QLabel("0/0")
        self._progress_label.setObjectName("progressLabel")

        self._status_value = QLabel("Idle")
        self._status_value.setObjectName("statusValue")
        self._status_value.setProperty("status", "ready")

        layout.addWidget(self._status_label)
        layout.addWidget(self._progress_bar, 1)
        layout.addWidget(self._progress_label)
        layout.addWidget(self._status_value)
        return frame

    def _bootstrap(self) -> None:
        try:
            self._credentials = load_credentials()
        except CredentialsError as exc:
            self._log("ERROR", str(exc))
            self._set_status_value("No credentials", "error")
            QMessageBox.warning(
                self,
                "Cannot start",
                "config/credentials.json is missing or invalid.\n\n" f"{exc}",
            )
            return

        self._base_url = self._credentials["base_url"]
        self._dataflow_base_url = self._credentials["dataflow_base_url"]

        try:
            self._client = pu_api.PuApiClient(
                self._credentials["token"], self._base_url
            )
        except ValueError as exc:
            self._log("ERROR", f"API client init failed: {exc}")
            self._set_status_value("Init failed", "error")
            return

        self._log("INFO", f"API base URL:      {self._base_url}")
        self._log("INFO", f"Dataflow base URL: {self._dataflow_base_url}")

        self._session_ok = pu_session.session_exists()
        if self._session_ok:
            self._log("INFO", "pu_auth.json found.")
        else:
            self._log(
                "WARNING",
                "pu_auth.json not found. Click [Log in] to authenticate.",
            )
            self._set_status_value("Not logged in", "error")

        self._refresh_login_button()
        self._refresh_start_enabled()
        self._load_projects()

    def _load_projects(self) -> None:
        if self._client is None:
            return
        self._log("INFO", "Loading projects ...")
        self._set_status_value("Loading projects", "running")

        worker = _ApiWorker(self._client.list_projects, parent=self)
        worker.loaded.connect(self._on_projects_loaded)
        worker.failed.connect(self._on_api_failed)
        worker.finished.connect(lambda w=worker: self._release_worker(w))
        self._api_workers.append(worker)
        worker.start()

    def _on_projects_loaded(self, projects: list[Project]) -> None:
        self._log("INFO", f"Loaded {len(projects)} project(s).")
        self._tree.clear()

        self._suppress_changed = True
        try:
            for proj in projects:
                self._project_cache[proj.id] = proj
                item = QTreeWidgetItem(self._tree)
                item.setText(0, proj.name or proj.id)
                item.setData(
                    0,
                    Qt.UserRole,
                    {
                        "type": "project",
                        "id": proj.id,
                        "loaded": False,
                        "loading": False,
                        "data": proj,
                    },
                )
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(0, Qt.Unchecked)
                item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
        finally:
            self._suppress_changed = False

        self._set_status_value("Ready", "ready")
        self._refresh_start_enabled()

    def _on_item_expanded(self, item: QTreeWidgetItem) -> None:
        if self._suppress_expand:
            return
        info = item.data(0, Qt.UserRole) or {}
        node_type = info.get("type")
        if info.get("loaded") or info.get("loading"):
            return

        if node_type == "project":
            info["loading"] = True
            item.setData(0, Qt.UserRole, info)
            self._load_sites(item, info["data"])
        elif node_type == "site":
            info["loading"] = True
            item.setData(0, Qt.UserRole, info)
            self._load_channels(item, info["data"])

    def _load_sites(self, project_item: QTreeWidgetItem, project: Project) -> None:
        if self._client is None:
            return
        self._log("INFO", f"Loading sites for '{project.name}' ...")
        worker = _ApiWorker(self._client.list_sites, project.id, parent=self)
        worker.loaded.connect(
            lambda sites, item=project_item, proj=project: (
                self._populate_sites(item, proj, sites)
            )
        )
        worker.failed.connect(
            lambda err, item=project_item: self._on_child_load_failed(item, err)
        )
        worker.finished.connect(lambda w=worker: self._release_worker(w))
        self._api_workers.append(worker)
        worker.start()

    def _populate_sites(
        self,
        project_item: QTreeWidgetItem,
        project: Project,
        sites: list[Site],
    ) -> None:
        info = project_item.data(0, Qt.UserRole) or {}
        info["loading"] = False
        info["loaded"] = True
        project_item.setData(0, Qt.UserRole, info)
        project_item.takeChildren()

        project_intent = project.id in self._project_intent

        self._suppress_changed = True
        try:
            for site in sites:
                self._site_cache[site.id] = site
                item = QTreeWidgetItem(project_item)
                item.setText(0, site.title or site.id)
                item.setData(
                    0,
                    Qt.UserRole,
                    {
                        "type": "site",
                        "id": site.id,
                        "loaded": False,
                        "loading": False,
                        "data": site,
                    },
                )
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator)
                if project_intent or site.id in self._site_intent:
                    item.setCheckState(0, Qt.Checked)
                else:
                    item.setCheckState(0, Qt.Unchecked)
            self._update_parent_check_state(project_item)
        finally:
            self._suppress_changed = False

        self._refresh_start_enabled()

    def _load_channels(self, site_item: QTreeWidgetItem, site: Site) -> None:
        if self._client is None:
            return
        project = self._project_cache.get(site.project_id)
        if project is None:
            self._log("ERROR", f"Unknown project for site {site.id}, skipping.")
            return

        self._log("INFO", f"Loading channels for '{site.title}' ...")
        worker = _ApiWorker(self._client.list_channels, site, project, parent=self)
        worker.loaded.connect(
            lambda channels, item=site_item, s=site: (
                self._populate_channels(item, s, channels)
            )
        )
        worker.failed.connect(
            lambda err, item=site_item: self._on_child_load_failed(item, err)
        )
        worker.finished.connect(lambda w=worker: self._release_worker(w))
        self._api_workers.append(worker)
        worker.start()

    def _populate_channels(
        self,
        site_item: QTreeWidgetItem,
        site: Site,
        channels: list[Channel],
    ) -> None:
        info = site_item.data(0, Qt.UserRole) or {}
        info["loading"] = False
        info["loaded"] = True
        site_item.setData(0, Qt.UserRole, info)
        site_item.takeChildren()

        project_intent = site.project_id in self._project_intent
        site_intent = site.id in self._site_intent

        self._suppress_changed = True
        try:
            for ch in channels:
                self._channel_cache[ch.id] = ch
                item = QTreeWidgetItem(site_item)
                item.setText(0, self._channel_label(ch))
                item.setData(
                    0,
                    Qt.UserRole,
                    {
                        "type": "channel",
                        "id": ch.id,
                        "data": ch,
                    },
                )
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setToolTip(
                    0,
                    f"id={ch.id}\n"
                    f"active={ch.active}\n"
                    f"format={ch.fmt}\n"
                    f"url={ch.url or '(none)'}",
                )
                if project_intent or site_intent or ch.id in self._checked_channels:
                    item.setCheckState(0, Qt.Checked)
                    self._checked_channels.add(ch.id)
                else:
                    item.setCheckState(0, Qt.Unchecked)
            self._update_parent_check_state(site_item)
        finally:
            self._suppress_changed = False

        self._refresh_start_enabled()

    @staticmethod
    def _channel_label(ch: Channel) -> str:
        tags: list[str] = []
        if not ch.active:
            tags.append("[inactive]")
        if not ch.url:
            tags.append("[no feed url]")
        prefix = " ".join(tags) + " " if tags else ""
        return f"{prefix}{ch.name or ch.id}"

    def _on_child_load_failed(self, item: QTreeWidgetItem, message: str) -> None:
        info = item.data(0, Qt.UserRole) or {}
        info["loading"] = False
        info["loaded"] = True
        item.setData(0, Qt.UserRole, info)
        item.takeChildren()

        error_item = QTreeWidgetItem(item)
        error_item.setText(0, f"Error: {message}")
        error_item.setData(0, Qt.UserRole, {"type": "error"})
        error_item.setDisabled(True)

        self._log("ERROR", f"Load failed: {message}")

    def _on_api_failed(self, message: str) -> None:
        self._log("ERROR", message)
        self._set_status_value("API error", "error")
        QMessageBox.warning(self, "API error", message)

    def _release_worker(self, worker: QThread) -> None:
        try:
            self._api_workers.remove(worker)
        except ValueError:
            pass
        worker.deleteLater()

    def _on_item_changed(self, item: QTreeWidgetItem, _column: int) -> None:
        if self._suppress_changed:
            return
        info = item.data(0, Qt.UserRole) or {}
        node_type = info.get("type")
        state = item.checkState(0)

        self._suppress_changed = True
        try:
            if node_type == "project":
                self._apply_state_down(item, state)
                self._update_intent(self._project_intent, info["id"], state)
                if state == Qt.Unchecked:
                    self._clear_intent_below_project(info["id"])
            elif node_type == "site":
                self._apply_state_down(item, state)
                self._update_intent(self._site_intent, info["id"], state)
                self._update_parent_check_state(item.parent())
            elif node_type == "channel":
                if state == Qt.Checked:
                    self._checked_channels.add(info["id"])
                else:
                    self._checked_channels.discard(info["id"])
                self._update_parent_check_state(item.parent())
        finally:
            self._suppress_changed = False

        self._refresh_start_enabled()

    def _apply_state_down(self, item: QTreeWidgetItem, state: Qt.CheckState) -> None:
        for i in range(item.childCount()):
            child = item.child(i)
            child_info = child.data(0, Qt.UserRole) or {}
            child_type = child_info.get("type")
            child_id = child_info.get("id")

            if state == Qt.Checked:
                child.setCheckState(0, Qt.Checked)
                if child_type == "channel" and child_id:
                    self._checked_channels.add(child_id)
                elif child_type == "site" and child_id:
                    self._site_intent.add(child_id)
                elif child_type == "project" and child_id:
                    self._project_intent.add(child_id)
            elif state == Qt.Unchecked:
                child.setCheckState(0, Qt.Unchecked)
                if child_type == "channel" and child_id:
                    self._checked_channels.discard(child_id)
                elif child_type == "site" and child_id:
                    self._site_intent.discard(child_id)
                elif child_type == "project" and child_id:
                    self._project_intent.discard(child_id)

            self._apply_state_down(child, state)

    def _update_intent(
        self, bucket: set[str], item_id: str, state: Qt.CheckState
    ) -> None:
        if state == Qt.Checked:
            bucket.add(item_id)
        else:
            bucket.discard(item_id)

    def _clear_intent_below_project(self, project_id: str) -> None:
        stale = [
            sid
            for sid in self._site_intent
            if sid in self._site_cache
            and self._site_cache[sid].project_id == project_id
        ]
        for sid in stale:
            self._site_intent.discard(sid)

    def _update_parent_check_state(self, parent: QTreeWidgetItem | None) -> None:
        if parent is None:
            return
        states: list[Qt.CheckState] = []
        for i in range(parent.childCount()):
            child = parent.child(i)
            info = child.data(0, Qt.UserRole) or {}
            if info.get("type") in (None, "error", "placeholder"):
                continue
            states.append(child.checkState(0))

        if not states:
            return

        if all(s == Qt.Checked for s in states):
            new_state = Qt.Checked
        elif all(s == Qt.Unchecked for s in states):
            new_state = Qt.Unchecked
        else:
            new_state = Qt.PartiallyChecked

        if parent.checkState(0) != new_state:
            parent.setCheckState(0, new_state)

        info = parent.data(0, Qt.UserRole) or {}
        pid = info.get("id")
        ptype = info.get("type")
        if pid and ptype == "site":
            if new_state == Qt.Checked:
                self._site_intent.add(pid)
            else:
                self._site_intent.discard(pid)
        elif pid and ptype == "project":
            if new_state == Qt.Checked:
                self._project_intent.add(pid)
            else:
                self._project_intent.discard(pid)

        self._update_parent_check_state(parent.parent())

    def _on_search_changed(self, text: str) -> None:
        needle = (text or "").strip().lower()
        root = self._tree.invisibleRootItem()
        self._suppress_expand = True
        try:
            if not needle:
                self._set_all_visible(root)
                return
            self._filter_tree(root, needle)
        finally:
            self._suppress_expand = False

    def _on_search_input(self, _text: str) -> None:
        self._search_timer.start()

    def _flush_search(self) -> None:
        self._on_search_changed(self._search_edit.text())

    def _set_all_visible(self, parent: QTreeWidgetItem) -> None:
        for i in range(parent.childCount()):
            child = parent.child(i)
            child.setHidden(False)
            self._set_all_visible(child)

    def _filter_tree(self, parent: QTreeWidgetItem, needle: str) -> bool:
        any_visible = False
        for i in range(parent.childCount()):
            child = parent.child(i)
            info = child.data(0, Qt.UserRole) or {}
            name = (child.text(0) or "").lower()
            item_id = str(info.get("id") or "").lower()
            own_match = needle in name or needle in item_id
            sub_match = self._filter_tree(child, needle)
            visible = own_match or sub_match
            child.setHidden(not visible)
            if sub_match:
                child.setExpanded(True)
            if visible:
                any_visible = True
        return any_visible

    def _on_browse_out_dir(self) -> None:
        chosen = QFileDialog.getExistingDirectory(
            self, "Select output directory", self._out_edit.text() or "."
        )
        if chosen:
            self._out_edit.setText(chosen)

    def _settle_seconds(self) -> int:
        try:
            value = int(self._settle_edit.text().strip() or "15")
        except ValueError:
            value = 15
        return max(SETTLE_MIN, min(SETTLE_MAX, value))

    def _iter_channel_items(self):
        root = self._tree.invisibleRootItem()

        def walk(item):
            for i in range(item.childCount()):
                child = item.child(i)
                info = child.data(0, Qt.UserRole) or {}
                if info.get("type") == "channel":
                    yield child
                yield from walk(child)

        yield from walk(root)

    def _collect_selections(self) -> list[Selection]:
        selections: list[Selection] = []
        for item in self._iter_channel_items():
            if item.checkState(0) != Qt.Checked:
                continue
            info = item.data(0, Qt.UserRole) or {}
            cid = info.get("id")
            ch = self._channel_cache.get(cid)
            if ch is None:
                continue
            selections.append(ch.to_selection(self._dataflow_base_url))
        return selections

    def _refresh_start_enabled(self) -> None:
        has_creds = self._client is not None
        has_selection = bool(self._collect_selections())
        not_running = self._worker is None
        logged_in = self._session_ok
        enabled = has_creds and has_selection and not_running and logged_in
        self._start_button.setEnabled(enabled)
        if not logged_in:
            self._start_button.setToolTip("Please log in to Productsup first.")
        else:
            self._start_button.setToolTip("")

    def _on_start_clicked(self) -> None:
        selections = self._collect_selections()
        if not selections:
            QMessageBox.information(
                self,
                "No channels",
                "Please check at least one channel first.",
            )
            return

        opts = PipelineOptions(
            out_dir=Path(self._out_edit.text().strip() or "output"),
            refresh=self._refresh_check.isChecked(),
            headless=self._headless_check.isChecked(),
            settle_seconds=self._settle_seconds(),
        )
        self._active_reports_dir = opts.report_dir

        self._log_view.clear()
        self._progress_bar.setValue(0)
        self._progress_label.setText(f"0/{len(selections)}")
        self._set_status_value("Running", "running")
        self._log(
            "INFO",
            f"Starting pipeline: {len(selections)} channel(s), "
            f"out_dir={opts.out_dir}, refresh={opts.refresh}, "
            f"headless={opts.headless}, settle={opts.settle_seconds}s",
        )

        self._worker = PipelineWorker(selections, opts)
        self._worker.stage_changed.connect(self._on_stage)
        self._worker.progress.connect(self._on_progress)
        self._worker.result_ready.connect(self._on_result)
        self._worker.succeeded.connect(self._on_succeeded)
        self._worker.failed.connect(self._on_failed)
        self._worker.finished.connect(self._on_worker_finished)

        self._start_button.setEnabled(False)
        self._cancel_button.setEnabled(True)
        self._worker.start()

    # -- login ---------------------------------------------------------

    def _refresh_login_button(self) -> None:
        """Update the [Log in] button label and styling."""
        if self._session_ok:
            self._login_button.setText("Re-login")
            self._login_button.setProperty("flat", True)
            self._login_button.setProperty("secondary", False)
        else:
            self._login_button.setText("Log in")
            self._login_button.setProperty("flat", False)
            self._login_button.setProperty("secondary", True)
        style = self._login_button.style()
        style.unpolish(self._login_button)
        style.polish(self._login_button)

    def _on_login_clicked(self) -> None:
        # Clean up any stale worker from a previous run BEFORE creating
        # the new one, so its queued signals cannot reach the handlers
        # and clobber the worker we are about to create.
        if self._login_worker is not None:
            if self._login_worker.isRunning():
                QMessageBox.information(
                    self,
                    "Login in progress",
                    "A login window is already open.",
                )
                return
            old = self._login_worker
            self._login_worker = None
            self._drop_login_worker(old)
            old.wait(1000)
            old.deleteLater()

        auth_file = pu_session.DEFAULT_AUTH_FILE
        worker = LoginWorker(auth_file, parent=self)
        worker.browser_opened.connect(self._on_login_browser_opened)
        worker.finished_ok.connect(self._on_login_finished)
        worker.failed.connect(self._on_login_failed)
        self._login_worker = worker

        dialog = LoginDialog(worker, self)
        self._login_dialog = dialog
        worker.start()
        result = dialog.exec()
        self._login_dialog = None

        if result != QDialog.Accepted:
            # User closed the dialog without confirming. Cancel the
            # worker if it is still alive; it may already have failed
            # and been cleaned up by _on_login_failed.
            if worker.isRunning():
                worker.cancel()

    @staticmethod
    def _drop_login_worker(worker: LoginWorker) -> None:
        """Disconnect every signal from a stale worker."""
        for signal in (
            worker.browser_opened,
            worker.finished_ok,
            worker.failed,
        ):
            try:
                signal.disconnect()
            except (RuntimeError, TypeError):
                pass

    def _on_login_browser_opened(self) -> None:
        self._log(
            "INFO",
            "Browser opened. Please log in to Productsup.",
        )
        if self._login_dialog is not None:
            self._login_dialog.enable_confirm()

    def _on_login_finished(self, auth_file) -> None:
        # Do NOT clear self._login_worker here. The worker object stays
        # referenced until _on_login_clicked is called for the next
        # login; that method handles disconnect + deleteLater.
        self._log("INFO", f"Login captured: {auth_file}")
        self._session_ok = True
        self._refresh_login_button()
        self._set_status_value("Ready", "ready")
        # Only reload the tree if it is empty; otherwise preserve the
        # user's existing expansion and selection state.
        if self._tree.topLevelItemCount() == 0:
            self._load_projects()
        self._refresh_start_enabled()

    def _on_login_failed(self, message: str) -> None:
        # Do NOT clear self._login_worker here; see _on_login_finished.
        self._log("ERROR", f"Login failed: {message}")
        self._refresh_login_button()
        self._refresh_start_enabled()

        # Close the dialog if it is still open. If the worker failed
        # before the user confirmed, exec() is still blocking and must
        # be released here.
        if self._login_dialog is not None:
            self._login_dialog.reject()

        # Cancellation is not an error worth a popup.
        if "cancelled" in message.lower():
            return

        QMessageBox.warning(self, "Login failed", message)

    def _on_cancel_clicked(self) -> None:
        if self._worker is not None:
            self._worker.cancel()
            self._cancel_button.setEnabled(False)
            self._set_status_value("Cancelling ...", "running")
            self._log("INFO", "Cancel requested.")

    def _on_stage(self, stage: str) -> None:
        labels = {
            STAGE_FETCHING: "Fetching HTML",
            STAGE_PARSING: "Parsing HTML",
            STAGE_WRITING: "Writing report",
            STAGE_DONE: "Done",
        }
        self._log("INFO", f"Stage: {stage}")
        if stage == STAGE_DONE:
            self._set_status_value(labels[stage], "done")
        else:
            self._set_status_value(labels.get(stage, stage), "running")

    def _on_progress(
        self,
        done: int,
        total: int,
        selection: Selection,
        status: str,
    ) -> None:
        pct = int(done * 100 / total) if total > 0 else 0
        self._progress_bar.setValue(pct)
        self._progress_label.setText(f"{done}/{total}")
        self._log(
            "INFO",
            f"[{status}] {selection.channel_name} " f"(id={selection.channel_id})",
        )

    def _on_result(self, result) -> None:
        sel = result.selection
        if result.error:
            self._log(
                "ERROR",
                f"{sel.channel_name} ({sel.channel_id}): " f"{result.error}",
            )
        else:
            rows = len(result.rows or [])
            cols = 6 if result.has_export else 4
            self._log(
                "INFO",
                f"{sel.channel_name} ({sel.channel_id}): " f"{rows} rows, {cols} cols",
            )

    def _on_succeeded(self, results, report_paths) -> None:
        succeeded = sum(1 for r in results if r.succeeded)
        failed = len(results) - succeeded
        self._log(
            "INFO",
            f"Pipeline done: {succeeded} succeeded / {failed} failed.",
        )

        reports_dir: Path | None = None
        if report_paths:
            reports_dir = report_paths[0].parent
            self._log(
                "INFO",
                f"Reports: {reports_dir}  ({len(report_paths)} file(s))",
            )
        elif failed > 0:
            reports_dir = (
                self._active_reports_dir
                if self._active_reports_dir is not None
                else Path(self._out_edit.text().strip() or "output") / "reports"
            )

        if failed > 0 and reports_dir is not None:
            error_file = reports_dir / ERRORS_FILENAME
            self._log(
                "WARNING",
                f"{failed} channel(s) failed. See {error_file}",
            )

        self._set_status_value(f"Done ({succeeded}/{len(results)})", "done")

        # Auth failures -> offer re-login, skip the completion popup.
        # 403/401 from a dataflow fetch is a per-channel error, so it
        # lands in _on_succeeded, not _on_failed.
        auth_failures = [
            r
            for r in results
            if r.error
            and ("403" in r.error or "401" in r.error or "unauthor" in r.error.lower())
        ]
        if auth_failures:
            answer = QMessageBox.question(
                self,
                "Session may have expired",
                f"{len(auth_failures)} channel(s) failed with a "
                f"403/401 error.\n\n"
                f"The Productsup session may have expired.\n\n"
                f"Log in again now?",
                QMessageBox.Yes | QMessageBox.No,
                QMessageBox.Yes,
            )
            if answer == QMessageBox.Yes:
                self._session_ok = False
                self._refresh_login_button()
                self._refresh_start_enabled()
                self._on_login_clicked()
                return

        # Normal completion popup.
        if report_paths:
            msg = f"{len(report_paths)} report file(s) written to:\n" f"{reports_dir}"
            if failed > 0:
                msg += (
                    f"\n\n{failed} channel(s) failed. "
                    f"See {ERRORS_FILENAME} in the same directory."
                )
            QMessageBox.information(self, "Reports ready", msg)
        elif failed > 0 and reports_dir is not None:
            QMessageBox.warning(
                self,
                "All channels failed",
                f"All {failed} channel(s) failed.\n"
                f"See {ERRORS_FILENAME} in:\n{reports_dir}",
            )

    def _on_failed(self, message: str) -> None:
        self._log("ERROR", message)
        self._set_status_value("Failed", "error")
        QMessageBox.warning(self, "Pipeline failed", message)

    def _on_worker_finished(self) -> None:
        self._cancel_button.setEnabled(False)
        self._worker = None
        self._active_reports_dir = None
        self._refresh_start_enabled()

    def _log(self, level: str, message: str) -> None:
        ts = datetime.now().strftime("%H:%M:%S")
        self._log_view.appendPlainText(f"[{ts}] [{level}] {redact_token(message)}")

    def _set_status_value(self, text: str, status: str) -> None:
        self._status_value.setText(text)
        if self._status_value.property("status") != status:
            self._status_value.setProperty("status", status)
            style = self._status_value.style()
            style.unpolish(self._status_value)
            style.polish(self._status_value)

    def closeEvent(self, event) -> None:  # noqa: N802
        if self._worker is not None and self._worker.isRunning():
            self._worker.cancel()
            self._worker.wait(5000)
        if self._login_worker is not None and self._login_worker.isRunning():
            self._login_worker.blockSignals(True)
            self._login_worker.cancel()
            self._login_worker.wait(5000)
        for worker in list(self._api_workers):
            if worker.isRunning():
                worker.wait(2000)
        super().closeEvent(event)


def _load_stylesheet(app: QApplication) -> None:
    candidates = [
        Path("style.qss"),
        Path(__file__).resolve().parent.parent / "style.qss",
    ]
    for candidate in candidates:
        if candidate.exists():
            try:
                with candidate.open("r", encoding="utf-8") as handle:
                    app.setStyleSheet(handle.read())
                return
            except OSError:
                continue


def run(opts: PipelineOptions | None = None) -> int:  # noqa: ARG001
    app = QApplication.instance() or QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    _load_stylesheet(app)
    window = AttrMapGetterWindow()
    window.show()
    return app.exec()
