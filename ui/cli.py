from __future__ import annotations

import sys
from pathlib import Path

from core import api as pu_api
from core import pipeline
from core.credentials import CredentialsError, load_credentials, redact_token
from core.models import Channel, PipelineOptions, Project, Selection, Site
from core.reporter import ERRORS_FILENAME


def _print_header(title: str) -> None:
    print()
    print("=" * 60)
    print(title)
    print("=" * 60)


def _ask_indices(prompt: str, count: int) -> list[int]:
    while True:
        try:
            raw = input(prompt).strip()
        except EOFError:
            print()
            return []

        if not raw:
            return []
        if raw.lower() == "all":
            return list(range(1, count + 1))

        chosen: set[int] = set()
        ok = True
        for token in raw.split(","):
            token = token.strip()
            if not token:
                continue
            if "-" in token:
                parts = token.split("-", 1)
                try:
                    start = int(parts[0].strip())
                    end = int(parts[1].strip())
                except ValueError:
                    ok = False
                    break
                if start > end:
                    start, end = end, start
                for i in range(start, end + 1):
                    if 1 <= i <= count:
                        chosen.add(i)
                    else:
                        ok = False
                        break
                if not ok:
                    break
            else:
                try:
                    i = int(token)
                except ValueError:
                    ok = False
                    break
                if 1 <= i <= count:
                    chosen.add(i)
                else:
                    ok = False
                    break

        if not ok or not chosen:
            print(
                f"Invalid input. Use 1..{count}, ranges like 1-3, "
                f"'all', or empty to cancel."
            )
            continue

        return sorted(chosen)


def _list_items(items, label_fn) -> None:
    for i, item in enumerate(items, start=1):
        print(f"  {i:>3}) {label_fn(item)}")


def _pick_projects(client: pu_api.PuApiClient) -> list[Project]:
    _print_header("Projects")
    projects = client.list_projects()
    if not projects:
        print("No projects found.")
        return []
    _list_items(projects, lambda p: p.name or p.id)
    idx = _ask_indices(
        "Select projects (comma, range, 'all', empty to cancel): ",
        len(projects),
    )
    return [projects[i - 1] for i in idx]


def _pick_sites(client: pu_api.PuApiClient, project: Project) -> list[Site]:
    _print_header(f"Sites of project: {project.name} ({project.id})")
    sites = client.list_sites(project.id)
    if not sites:
        print("No sites in this project.")
        return []
    _list_items(sites, lambda s: f"{s.title or s.id}  (id={s.id})")
    idx = _ask_indices(
        "Select sites (comma, range, 'all', empty to skip): ",
        len(sites),
    )
    return [sites[i - 1] for i in idx]


def _pick_channels(
    client: pu_api.PuApiClient, site: Site, project: Project
) -> list[Channel]:
    _print_header(f"Channels of site: {site.title} ({site.id})")
    channels = client.list_channels(site, project)
    if not channels:
        print("No channels in this site.")
        return []

    def _channel_line(c: Channel) -> str:
        tags: list[str] = []
        if not c.active:
            tags.append("[inactive]")
        if not c.url:
            tags.append("[no feed url]")
        prefix = " ".join(tags) + " " if tags else ""
        return f"{prefix}{c.name or c.id}  (id={c.id}, fmt={c.fmt})"

    _list_items(channels, _channel_line)
    idx = _ask_indices(
        "Select channels (comma, range, 'all', empty to skip): ",
        len(channels),
    )
    return [channels[i - 1] for i in idx]


def _collect_selections(
    client: pu_api.PuApiClient,
    dataflow_base_url: str,
) -> list[Selection]:
    selections: list[Selection] = []

    projects = _pick_projects(client)
    for project in projects:
        try:
            sites = _pick_sites(client, project)
        except pu_api.PuAuthError:
            raise
        except pu_api.PuHttpError as exc:
            print(
                f"[ERROR] Cannot load sites for '{project.name}': "
                f"{redact_token(str(exc))}"
            )
            continue

        for site in sites:
            try:
                channels = _pick_channels(client, site, project)
            except pu_api.PuAuthError:
                raise
            except pu_api.PuHttpError as exc:
                print(
                    f"[ERROR] Cannot load channels for "
                    f"'{site.title}': {redact_token(str(exc))}"
                )
                continue

            for ch in channels:
                selections.append(ch.to_selection(dataflow_base_url))

    return selections


def _on_stage(stage: str) -> None:
    print(f"[stage] {stage}")


def _on_progress(done: int, total: int, sel: Selection, status: str) -> None:
    print(
        f"[{done:>3}/{total}] [{status}] " f"{sel.channel_name} (id={sel.channel_id})"
    )


def _on_result(result) -> None:
    sel = result.selection
    if result.error:
        # result.error already carries the "ERROR: " prefix set by the
        # pipeline / fetcher. Do not add another one here.
        print(f"      -> {redact_token(result.error)}")
    else:
        rows = len(result.rows or [])
        cols = 6 if result.has_export else 4
        print(f"      -> {rows} rows, {cols} cols")


def run(opts: PipelineOptions | None = None) -> int:
    """
    Run the CLI flow. Returns a process exit code:
        0  every selected channel succeeded
        1  setup error (credentials / auth / API / no selection / aborted)
        2  partial success (some channels failed)
        3  total failure (no channel succeeded)
    """
    opts = opts or PipelineOptions(out_dir=Path("output"))

    # -- credentials ----------------------------------------------------
    try:
        creds = load_credentials()
    except CredentialsError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    base_url = creds["base_url"]
    dataflow_base_url = creds["dataflow_base_url"]

    print(f"[INFO] API base URL:      {base_url}")
    print(f"[INFO] Dataflow base URL: {dataflow_base_url}")

    try:
        client = pu_api.PuApiClient(creds["token"], base_url)
    except ValueError as exc:
        print(f"[ERROR] {exc}", file=sys.stderr)
        return 1

    # -- selection ------------------------------------------------------
    try:
        selections = _collect_selections(client, dataflow_base_url)
    except pu_api.PuAuthError as exc:
        print(f"[ERROR] {redact_token(str(exc))}", file=sys.stderr)
        return 1
    except pu_api.PuHttpError as exc:
        print(f"[ERROR] {redact_token(str(exc))}", file=sys.stderr)
        return 1
    except (KeyboardInterrupt, EOFError):
        print("\nAborted.")
        return 1

    if not selections:
        print("No channels selected. Nothing to do.")
        return 1

    print()
    print(f"Queued {len(selections)} channel(s) for processing.")

    # -- run pipeline ---------------------------------------------------
    try:
        results, report_paths = pipeline.run(
            selections,
            opts,
            on_stage=_on_stage,
            on_progress=_on_progress,
            on_result=_on_result,
        )
    except pipeline.PipelineError as exc:
        print(f"[ERROR] {redact_token(str(exc))}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nInterrupted.")
        return 1

    # -- summary --------------------------------------------------------
    succeeded = sum(1 for r in results if r.succeeded)
    failed = len(results) - succeeded
    reports_dir = opts.report_dir

    print()
    print("=" * 60)
    print(f"Done: {succeeded} succeeded, {failed} failed.")
    if report_paths:
        print(f"Reports: {reports_dir}  ({len(report_paths)} file(s))")
    if failed > 0:
        print(f"Errors:  {reports_dir / ERRORS_FILENAME}")
    print("=" * 60)

    if failed == 0:
        return 0
    if succeeded == 0:
        return 3
    return 2
