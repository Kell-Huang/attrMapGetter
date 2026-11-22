"""
Twig reference resolution for Text Template rules.

A Text Template rule stores Twig code in a ``<textarea name="template">``.
The variables it references do not appear as connections in the dataflow
graph, so the parsed rows never contain them. This module fetches the
per-attribute focus page, extracts the Twig code, works out which of the
referenced names are real attributes, and appends supplementary rows to the
channel's row list so those references show up in the report and take part in
the Allow Column Filter evaluation.

Stage rules (business requirement):
    intermediate Text Template -> may only reference IMPORT attributes
    export       Text Template -> may only reference INTERMEDIATE attributes

The module is deliberately additive: it never mutates the rows produced by
core.parser, and a failure here must never fail the channel.
"""

from __future__ import annotations

import html as htmllib
import json
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass
from pathlib import Path
from typing import Iterable

import requests

from .models import ChannelResult, PipelineOptions

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

FOCUS_HOST = "https://dataflow.productsup.com"

# intermediate focus pages address the attribute by name; export focus pages
# address it by field id (e.g. "c:149650944").
FOCUS_INTERMEDIATE = (
    FOCUS_HOST + "/index/slimedit/site/{site}/channel/{chan}/if/{attr}/focus/intermediate"
)
FOCUS_EXPORT = (
    FOCUS_HOST + "/index/slimedit/site/{site}/channel/{chan}/ef/{field}/focus/export"
)

STAGE_INTERMEDIATE = "intermediate"
STAGE_EXPORT = "export"

WORKERS = 2            # concurrent focus-page requests (keep the load low)
REQUEST_DELAY = 1.0    # seconds to sleep before each request (politeness)
REQUEST_TIMEOUT = 90
MAX_RETRIES = 2

# Platform-provided Twig variable that is only available when the column is
# already connected; it is not an attribute and must never become a row.
BUILTIN_VARS = frozenset({"currentValue"})

PLACEHOLDER = "[Column not defined]"
MISSING_SOURCE = "Missing Source"

_TEXT_TEMPLATE_RE = re.compile(r"text\s+template", re.IGNORECASE)
_TEXTAREA_RE = re.compile(
    r'<textarea[^>]*\bname="template"[^>]*>(.*?)</textarea>', re.DOTALL | re.IGNORECASE
)
_EXPORT_HREF_RE = re.compile(
    r'href="/index/slimedit/site/(?P<site>\d+)/channel/(?P<chan>\d+)'
    r'/ef/(?P<field>[^/"]+)/focus/export"[^>]*title="(?P<title>[^"]*)"'
)
_COMMENT_RE = re.compile(r"\{#.*?#\}", re.DOTALL)
_TOKEN_RE = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")


# ---------------------------------------------------------------------------
# Public types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class TwigTarget:
    """One focus page to fetch."""

    stage: str          # STAGE_INTERMEDIATE | STAGE_EXPORT
    attr: str           # attribute name as it appears in the report
    url_id: str         # attribute name (intermediate) or field id (export)

    @property
    def key(self) -> tuple[str, str]:
        return (self.stage, self.attr)


class TwigError(Exception):
    """Raised only for setup problems (missing auth file)."""


# ---------------------------------------------------------------------------
# Target discovery
# ---------------------------------------------------------------------------


def collect_targets(
    rows: list[list[str]],
    html_text: str,
) -> list[TwigTarget]:
    """
    Find every attribute whose intermediate or export rules mention a Text
    Template, and resolve the identifier its focus page needs.

    Export attributes are addressed by field id, which only exists in the raw
    HTML, so ``html_text`` is required for the export stage.
    """
    inter_attrs: list[str] = []
    export_attrs: list[str] = []

    for row in rows:
        if len(row) < 5:
            continue
        inter_rule = row[1] if len(row) > 1 else ""
        inter_attr = (row[2] or "").strip()
        export_rule = row[3] if len(row) > 3 else ""
        export_attr = (row[4] or "").strip()

        if inter_attr and _TEXT_TEMPLATE_RE.search(inter_rule or ""):
            if inter_attr not in inter_attrs:
                inter_attrs.append(inter_attr)
        if export_attr and _TEXT_TEMPLATE_RE.search(export_rule or ""):
            if export_attr not in export_attrs:
                export_attrs.append(export_attr)

    field_by_name = _export_field_ids(html_text)

    targets: list[TwigTarget] = []
    for attr in inter_attrs:
        targets.append(TwigTarget(STAGE_INTERMEDIATE, attr, attr))
    for attr in export_attrs:
        field = field_by_name.get(attr)
        if field is None:
            # Without a field id the export focus page cannot be addressed;
            # skip rather than guess a URL that would silently render the
            # overview page.
            print(f"[WARN] twig: no export field id for {attr!r}, skipped")
            continue
        targets.append(TwigTarget(STAGE_EXPORT, attr, field))
    return targets


def has_text_template(rows: list[list[str]] | None) -> bool:
    """
    True when any row carries a Text Template rule.

    Used by the UI to annotate the row count shown right after parsing: the
    twig rows are appended later, so that figure does not include them.
    """
    if not rows:
        return False
    for row in rows:
        if _TEXT_TEMPLATE_RE.search(_cell(row, 1)) or _TEXT_TEMPLATE_RE.search(
            _cell(row, 3)
        ):
            return True
    return False


def _export_field_ids(html_text: str) -> dict[str, str]:
    """Map export attribute name -> field id, from the overview HTML."""
    mapping: dict[str, str] = {}
    for m in _EXPORT_HREF_RE.finditer(html_text):
        name = htmllib.unescape(m.group("title")).strip()
        if name and name not in mapping:
            mapping[name] = m.group("field")
    return mapping


# ---------------------------------------------------------------------------
# Twig code extraction
# ---------------------------------------------------------------------------


def extract_templates(html_text: str) -> list[str]:
    """
    Return the non-empty Twig templates on a focus page.

    A focus page embeds the whole rule library, whose template textareas are
    empty; only the applied rule boxes carry content. Filtering on non-empty
    values therefore isolates the templates we care about.
    """
    out: list[str] = []
    for m in _TEXTAREA_RE.finditer(html_text):
        value = htmllib.unescape(m.group(1)).strip()
        if value:
            out.append(value)
    return out


def extract_refs(templates: Iterable[str]) -> set[str]:
    """
    Candidate attribute references inside Twig code.

    Comments are dropped and every identifier is collected; string literal
    contents are not treated specially because the attribute names they may
    carry (``attribute(_context, "x")``, ``fromField("x")``) are covered by
    the identifier scan of the surrounding call. Names that are not real
    attributes (filters, keywords, function names) are filtered out later by
    intersecting with the known attribute sets, so no keyword list is needed.
    """
    tokens: set[str] = set()
    for text in templates:
        cleaned = _COMMENT_RE.sub(" ", text)
        for m in _TOKEN_RE.finditer(cleaned):
            token = m.group(0)
            if token in BUILTIN_VARS:
                continue
            tokens.add(token)
    return tokens


# ---------------------------------------------------------------------------
# Fetching
# ---------------------------------------------------------------------------


class _SessionFactory:
    """One requests.Session per thread; sessions are not thread-safe."""

    def __init__(self, auth_file: Path) -> None:
        if not auth_file.exists():
            raise TwigError(f"{auth_file} not found. Log in first.")
        state = json.loads(auth_file.read_text(encoding="utf-8"))
        self._cookies = [
            (c["name"], c["value"], c.get("domain", ""), c.get("path", "/"))
            for c in state.get("cookies", [])
        ]
        self._headers = {
            "User-Agent": state.get("user_agent")
            or "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36",
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
            "Referer": "https://platform.productsup.com/",
        }
        self._local = threading.local()

    def get(self) -> requests.Session:
        session = getattr(self._local, "session", None)
        if session is None:
            session = requests.Session()
            for name, value, domain, path in self._cookies:
                session.cookies.set(name, value, domain=domain, path=path)
            session.headers.update(self._headers)
            self._local.session = session
        return session


def _fetch_one(
    factory: _SessionFactory,
    target: TwigTarget,
    site_id: str,
    channel_id: str,
) -> tuple[TwigTarget, list[str], str | None]:
    """Fetch one focus page. Returns (target, templates, error)."""
    if target.stage == STAGE_INTERMEDIATE:
        url = FOCUS_INTERMEDIATE.format(
            site=site_id, chan=channel_id, attr=target.url_id
        )
    else:
        url = FOCUS_EXPORT.format(site=site_id, chan=channel_id, field=target.url_id)

    last_error: str | None = None
    for attempt in range(MAX_RETRIES + 1):
        if attempt:
            time.sleep(REQUEST_DELAY * (2 ** attempt))
        time.sleep(REQUEST_DELAY)
        try:
            resp = factory.get().get(url, timeout=REQUEST_TIMEOUT, allow_redirects=True)
        except requests.RequestException as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            continue

        if "auth.productsup.com" in resp.url:
            return target, [], "session expired (login redirect)"
        if resp.status_code != 200:
            last_error = f"HTTP {resp.status_code}"
            continue

        # A focus URL that the app does not recognise redirects to the
        # overview page; that response contains no applied rule box.
        if "/focus/" not in resp.url:
            last_error = "focus route not resolved (redirected to overview)"
            continue

        templates = extract_templates(resp.text)
        if not templates:
            # The rule exists but carries no Twig code (or was emptied).
            # This is a normal state, not a failure.
            return target, [], None
        return target, templates, None

    return target, [], last_error or "unknown error"


def fetch_all(
    targets: list[TwigTarget],
    site_id: str,
    channel_id: str,
    auth_file: Path,
) -> dict[tuple[str, str], list[str]]:
    """Fetch every target with a small thread pool; failures are skipped."""
    if not targets:
        return {}

    factory = _SessionFactory(auth_file)
    found: dict[tuple[str, str], list[str]] = {}
    workers = max(1, min(WORKERS, len(targets)))

    with ThreadPoolExecutor(max_workers=workers) as pool:
        futures = [
            pool.submit(_fetch_one, factory, t, site_id, channel_id) for t in targets
        ]
        for future in futures:
            try:
                target, templates, error = future.result()
            except Exception as exc:  # noqa: BLE001
                print(f"[WARN] twig fetch crashed: {type(exc).__name__}: {exc}")
                continue
            if error:
                print(f"[WARN] twig {target.stage}/{target.attr}: {error}")
            else:
                found[target.key] = templates
    return found


# ---------------------------------------------------------------------------
# Cache
# ---------------------------------------------------------------------------


def cache_path(cache_dir: Path, channel_id: str) -> Path:
    return cache_dir / f"{channel_id}.json"


def load_cache(path: Path) -> dict[tuple[str, str], list[str]]:
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}
    out: dict[tuple[str, str], list[str]] = {}
    if isinstance(raw, dict):
        for stage, mapping in raw.items():
            if not isinstance(mapping, dict):
                continue
            for attr, templates in mapping.items():
                if isinstance(templates, list):
                    out[(str(stage), str(attr))] = [str(t) for t in templates]
    return out


def save_cache(path: Path, data: dict[tuple[str, str], list[str]]) -> None:
    payload: dict[str, dict[str, list[str]]] = {}
    for (stage, attr), templates in data.items():
        payload.setdefault(stage, {})[attr] = templates
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
        )
    except OSError as exc:
        print(f"[WARN] cannot write twig cache {path}: {exc}")


# ---------------------------------------------------------------------------
# Row augmentation
# ---------------------------------------------------------------------------


@dataclass
class AugmentResult:
    rows: list[list[str]]
    twig_indexes: set[int]

    @property
    def added(self) -> int:
        return len(self.twig_indexes)


def _cell(row: list[str], index: int) -> str:
    if index >= len(row):
        return ""
    return (row[index] or "").strip()


def _build_indexes(rows: list[list[str]]):
    import_set: set[str] = set()
    inter_set: set[str] = set()
    export_set: set[str] = set()
    source_of: dict[str, str] = {}
    existing: set[tuple[str, str]] = set()
    existing_export: set[tuple[str, str, str]] = set()

    for row in rows:
        imp = _cell(row, 0)
        inter = _cell(row, 2)
        exp = _cell(row, 4)
        src = _cell(row, 5)

        if imp and imp != PLACEHOLDER:
            import_set.add(imp)
            if imp not in source_of and src:
                source_of[imp] = src
        if inter:
            inter_set.add(inter)
        if exp:
            export_set.add(exp)
        if imp and inter:
            existing.add((imp, inter))
            if exp:
                existing_export.add((imp, inter, exp))

    return import_set, inter_set, export_set, source_of, existing, existing_export


def augment_rows(
    rows: list[list[str]],
    templates: dict[tuple[str, str], list[str]],
) -> AugmentResult:
    """
    Append supplementary rows for Twig references.

    Insertion point: directly after the first row of the attribute that owns
    the template, so a channel's rows stay grouped.
    """
    if not templates:
        return AugmentResult(list(rows), set())

    (
        import_set,
        inter_set,
        _export_set,
        source_of,
        existing,
        existing_export,
    ) = _build_indexes(rows)

    pending_inter: dict[str, list[list[str]]] = {}
    pending_export: dict[str, list[list[str]]] = {}

    for (stage, attr), texts in sorted(templates.items()):
        refs = extract_refs(texts)
        if not refs:
            continue

        if stage == STAGE_INTERMEDIATE:
            # Business rule: an intermediate template may only reference
            # import attributes. Anything else (keywords, filters, names
            # from another stage) is not a valid dependency and is skipped.
            base = [r for r in rows if _cell(r, 2) == attr]
            if not base:
                continue
            rule_text = base[0][1] if len(base[0]) > 1 else ""
            pairs = sorted(
                {(_cell(r, 3), _cell(r, 4)) for r in base if _cell(r, 4)}
            ) or [("", "")]

            generated: list[list[str]] = []
            for ref in sorted(refs):
                if ref == attr or ref not in import_set:
                    continue
                if (ref, attr) in existing:
                    continue
                source = source_of.get(ref) or MISSING_SOURCE
                for export_rule, export_attr in pairs:
                    generated.append(
                        [ref, rule_text, attr, export_rule, export_attr, source]
                    )
                existing.add((ref, attr))
            if generated:
                pending_inter[attr] = generated

        else:  # STAGE_EXPORT
            # Business rule: an export template may only reference
            # intermediate attributes.
            base = [r for r in rows if _cell(r, 4) == attr]
            if not base:
                continue
            export_rule = _cell(base[0], 3)

            generated = []
            for ref in sorted(refs):
                if ref == attr or ref not in inter_set:
                    continue
                import_rows = [r for r in rows if _cell(r, 2) == ref and _cell(r, 0)]
                if not import_rows:
                    # The intermediate exists but has no import connection, so
                    # there is no import dependency to add.
                    continue
                for src_row in import_rows:
                    imp = _cell(src_row, 0)
                    if not imp or (imp, ref, attr) in existing_export:
                        continue
                    generated.append(
                        [
                            imp,
                            src_row[1] if len(src_row) > 1 else "",
                            ref,
                            export_rule,
                            attr,
                            _cell(src_row, 5),
                        ]
                    )
                    existing_export.add((imp, ref, attr))
            if generated:
                pending_export[attr] = generated

    if not pending_inter and not pending_export:
        return AugmentResult(list(rows), set())

    out: list[list[str]] = []
    twig_indexes: set[int] = set()
    emitted_inter: set[str] = set()
    emitted_export: set[str] = set()

    for row in rows:
        out.append(row)

        inter = _cell(row, 2)
        if inter in pending_inter and inter not in emitted_inter:
            emitted_inter.add(inter)
            for new_row in pending_inter[inter]:
                out.append(new_row)
                twig_indexes.add(len(out) - 1)

        exp = _cell(row, 4)
        if exp in pending_export and exp not in emitted_export:
            emitted_export.add(exp)
            for new_row in pending_export[exp]:
                out.append(new_row)
                twig_indexes.add(len(out) - 1)

    return AugmentResult(out, twig_indexes)


# ---------------------------------------------------------------------------
# Orchestration
# ---------------------------------------------------------------------------


def _is_cancelled(cancel_event: threading.Event | None) -> bool:
    return cancel_event is not None and cancel_event.is_set()


def _read_html(path: Path | None) -> str:
    if path is None:
        return ""
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except OSError as exc:
        print(f"[WARN] twig: cannot read {path}: {exc}")
        return ""


def apply_one(
    result: ChannelResult,
    opts: PipelineOptions,
    auth_file: Path,
    cancel_event: threading.Event | None = None,
) -> None:
    """
    Enrich a single ChannelResult in place.

    Never raises: any failure leaves the result untouched so the channel still
    produces a normal report.
    """
    if result.error is not None or not result.rows:
        return
    if _is_cancelled(cancel_event):
        return

    try:
        rows = result.rows

        # The export field-id map needs the raw HTML, but only export-stage
        # templates require it; avoid a 3 MB read when nothing needs it.
        needs_export = any(
            _TEXT_TEMPLATE_RE.search(_cell(r, 3)) for r in rows
        )
        html_text = _read_html(result.html_path) if needs_export else ""

        targets = collect_targets(rows, html_text)
        if not targets:
            return

        wanted = {t.key for t in targets}
        cache = cache_path(opts.twig_cache_dir, result.selection.channel_id)
        cached = {} if opts.refresh else load_cache(cache)
        templates = {k: v for k, v in cached.items() if k in wanted}

        missing = [t for t in targets if t.key not in templates]
        if missing:
            fetched = fetch_all(
                missing,
                result.selection.site_id,
                result.selection.channel_id,
                auth_file,
            )
            if fetched:
                templates.update(fetched)
                merged = dict(cached)
                merged.update(fetched)
                save_cache(cache, merged)

        if not templates:
            return

        augmented = augment_rows(rows, templates)
        if augmented.added:
            result.rows = augmented.rows
            result.twig_rows = augmented.twig_indexes
            print(
                f"[INFO] twig: +{augmented.added} row(s) for "
                f"{result.selection.channel_name}"
            )
    except Exception as exc:  # noqa: BLE001
        print(
            f"[WARN] twig enrichment skipped for "
            f"{result.selection.channel_name}: {type(exc).__name__}: {exc}"
        )
