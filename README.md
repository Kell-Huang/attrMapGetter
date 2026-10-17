# attrMapGetter

Batch-fetch and parse Productsup dataflow attributes into one Excel
workbook per channel.

Pipeline: **select project/site/channel → fetch iframe HTML → parse
attributes → write per-channel reports**.

---

## Architecture

### Layered design
┌──────────────────────────────────────────────────────────────────────┐
│ UI LAYER (the only place allowed to print / input / touch Qt) │
│ │
│ ui/gui.py PySide6 main window │
│ │ - QTreeWidget (project → site → channel) │
│ │ - tri-state cascade, lazy loading, search │
│ │ - Options card (refresh / headless / settle / out) │
│ │ - Log card, progress bar, status │
│ │ │
│ │ PipelineWorker(QThread) │
│ │ - runs core.pipeline.run off the GUI thread │
│ │ - emits: stage_changed / progress / result_ready / │
│ │ succeeded / failed │
│ │ │
│ ui/cli.py Terminal menu (no Qt) │
│ │ - lists projects/sites/channels │
│ │ - accepts 1,3,5 / 1-4 / all / empty │
│ │ - calls core.pipeline.run synchronously │
│ │ │
│ ui/worker.py QThread wrapper (single-use) │
└───────────────────────────────┬──────────────────────────────────────┘
│ callbacks:
│ on_stage(str)
│ on_progress(done, total, sel, status)
│ on_result(ChannelResult)
▼
┌──────────────────────────────────────────────────────────────────────┐
│ CORE LAYER (no print / no input / no Qt) │
│ │
│ core/pipeline.py orchestrator │
│ ├─ calls fetcher.fetch_many() │
│ ├─ calls parser.parse_html() (per channel) │
│ └─ calls reporter.write_reports() │
│ │
│ core/fetcher.py Playwright │
│ - launches msedge once per batch │
│ - waits for source/intermediate/export markers │
│ - dumps iframe HTML to output/html/slimedit_{cid}.txt │
│ - honours cancel_event between channels and retries │
│ │
│ core/parser.py thin wrapper around attInvestigator │
│ - bypasses AttrInvestigator.get_cur_dir() input() │
│ - returns (rows, has_export) │
│ │
│ core/reporter.py openpyxl │
│ - one .xlsx per successful channel │
│ - _errors.xlsx when any channel failed │
│ │
│ core/api.py Productsup REST │
│ - GET /platform/v2/projects │
│ - GET /platform/v2/projects/{pid}/sites │
│ - GET /platform/v2/sites/{sid}/channels │
│ - 401/403 → PuAuthError, others → PuHttpError │
│ │
│ core/credentials.py load config/credentials.json │
│ core/models.py dataclasses + constants │
└───────────────────────────────┬──────────────────────────────────────┘
│ HTTP / filesystem / subprocess
▼
┌──────────────────────────────────────────────────────────────────────┐
│ EXTERNAL │
│ │
│ Productsup REST API platform.productsup.com │
│ Productsup dataflow iframe dataflow.productsup.com │
│ Microsoft Edge (Playwright) headless or headed │
│ pu_auth.json storage_state from pu_login.py │
└──────────────────────────────────────────────────────────────────────┘

text

### Two hosts, kept apart

The REST API and the dataflow page use **separate hosts**:

| Purpose              | Config key                    | Default                                    |
|----------------------|-------------------------------|--------------------------------------------|
| REST API `/platform/v2/*` | `productsup.base_url`     | `https://platform.productsup.com`          |
| Dataflow page `/data-flow/*` | `productsup.dataflow_base_url` | `https://platform.productsup.com`   |

**Never assume they are the same.** For example, an enterprise setup may
expose the API at `platform-api.productsup.io` while the dataflow page
remains at `platform.productsup.com`. The `pu_auth.json` session only
belongs to the dataflow host, so the dataflow URL must be built from
`dataflow_base_url`, not from `base_url`.

### Data flow
pu_login.py ──────────► pu_auth.json
│
▼
config/credentials.json ──► core.api.PuApiClient
│
│ projects / sites / channels
▼
ui.gui or ui.cli ─── user picks (project, site, channel) ────┐
│
▼
list[Selection]
│
▼
┌──────────────────────── core.pipeline.run ─────────────────────────┐
│ │
│ 1. core.fetcher.fetch_many(selections, opts) │
│ └─► output/html/slimedit_{channel_id}.txt │
│ │
│ 2. core.parser.parse_html(html_path) (per channel) │
│ └─► (rows, has_export) │
│ │
│ 3. core.reporter.write_reports(results, opts.report_dir) │
│ └─► output/reports/{name}__{timestamp}.xlsx │
│ └─► output/reports/_errors.xlsx (only when some failed) │
│ │
└────────────────────────────────────────────────────────────────────┘
│
▼
ChannelResult[list] + list[Path]

text

### Module dependencies
ui/gui.py ──► ui/worker.py ──► core/pipeline.py
ui/cli.py ──────────────────► core/pipeline.py
│
┌──────────────────────┼──────────────────────┐
▼ ▼ ▼
core/fetcher.py core/parser.py core/reporter.py
│ │ │
│ ▼ ▼
│ attInvestigator.py openpyxl
▼
Playwright (msedge)

core/api.py ──► core/credentials.py ──► core/models.py
core/credentials.py ──► core/models.py
core/fetcher.py ──► core/models.py
core/parser.py ──► attInvestigator.py
core/reporter.py ──► core/models.py

text

**Rules enforced by design:**

- `core/**` must not `print`, call `input`, or import PySide6.
- `ui/**` is the only layer allowed to interact with the user.
- `attInvestigator.py`, `pu_login.py`, `test_playwright.py`,
  `export_selector.py` remain untouched.

Verify:

```bash
grep -rE "print\(|input\(|PySide6" core/
No output means the rule is respected.

Directory structure
text
attrMapGetter/
├── pu_login.py               One-time login helper (unchanged)
├── attInvestigator.py        Attribute parser engine (unchanged)
├── test_playwright.py        Single-URL fetch tool (unchanged)
├── export_selector.py        API reference (unchanged)
│
├── core/                     UI-agnostic functional layer
│   ├── __init__.py
│   ├── models.py             Dataclasses + shared constants
│   ├── credentials.py        Reads config/credentials.json
│   ├── api.py                Productsup REST client
│   ├── fetcher.py            Playwright iframe fetcher
│   ├── parser.py             Thin wrapper around attInvestigator
│   ├── reporter.py           Excel report writer
│   └── pipeline.py           fetch → parse → report orchestration
│
├── ui/                       Interaction layer
│   ├── __init__.py
│   ├── worker.py             QThread wrapper around pipeline.run
│   ├── gui.py                PySide6 main window
│   └── cli.py                Terminal menu (no Qt)
│
├── main.py                   Entry point
├── style.qss                 Unified stylesheet
│
├── config/
│   └── credentials.json      API token + both hosts
├── pu_auth.json              Created by pu_login.py
└── output/
    ├── html/
    │   └── slimedit_{channel_id}.txt      Per-channel HTML cache
    └── reports/
        ├── {name}__{timestamp}.xlsx       One file per successful channel
        └── _errors.xlsx                   Only when some channels failed
Installation
bash
pip install requests openpyxl playwright PySide6
playwright install msedge
requests — API calls

openpyxl — Excel reports

playwright — iframe HTML fetch

PySide6 — GUI (skip it if you only use --ui cli)

One-time setup
1. Log in to Productsup
bash
python pu_login.py
Edge opens Productsup. Log in manually, then return to the terminal and
press Enter. Login state is written to pu_auth.json. Re-run whenever
it expires.

2. Configure API credentials
Create config/credentials.json:

json
{
  "productsup": {
    "token": "YOUR_CLIENT_ID:YOUR_CLIENT_SECRET",
    "base_url": "https://platform.productsup.com",
    "dataflow_base_url": "https://platform.productsup.com"
  }
}
token — required

base_url — REST API host (defaults to platform.productsup.com)

dataflow_base_url — dataflow page host (defaults to
platform.productsup.com)

Both hosts may differ. See the "Two hosts, kept apart" section above.

Usage
GUI (default)
bash
python main.py
Window layout:

Left: Project → Site → Channel tree, checkable, lazy-loaded

Top right: Options (refresh / headless / settle / output dir)

Bottom right: Log

Bottom: progress bar + status

Flow: expand the tree → check the Channels you want → click Start.

Tri-state: checking a Project selects all its Channels; checking only
some Channels makes the parent show a partial state.

The search box filters live, does not change the checked state, and
does not trigger network requests.

CLI
bash
python main.py --ui cli
Flow:

Lists projects; multi-select with commas, ranges or all.

For each project, lists sites; multi-select.

For each site, lists channels; multi-select.

Runs the pipeline, printing progress and per-channel results.

Input syntax: 1,3,5 / 1-4 / all / empty to skip.

Command-line arguments
Flag	Default	Description
--ui {gui,cli}	gui	Interface to use
--out DIR	output	Output directory
--refresh	off	Ignore HTML cache and re-fetch
--headless	off	Run browser headless
--settle N	15	Seconds to wait after markers (0–600)
--version	—	Print version and exit
Exit codes (CLI)
Code	Meaning
0	Success (or GUI closed normally)
1	Setup error (credentials / auth / API / no selection / aborted)
2	Partial success
3	Total failure
Output
HTML cache
output/html/slimedit_{channel_id}.txt

Reused when it exists and is non-empty (unless --refresh)

Cache hits appear in the log as [cached]

Per-channel reports
output/reports/{sanitized_name}__{timestamp}.xlsx

One workbook per successful channel, no matter which project/site it
came from.

Timestamp suffix (%Y%m%d_%H%M%S, e.g. 20261017_080000)
guarantees that repeated runs never overwrite each other.

Name collisions within a single run get an extra
__{channel_id} suffix before the timestamp:
Google Shopping__268134__20261017_080000.xlsx.

If a channel's channel_name is empty or contains only characters
invalid in filenames, its channel_id is used instead.

Error summary
output/reports/_errors.xlsx

Created only when at least one channel failed.

No timestamp — it is overwritten on every run that has failures,
so it always reflects the latest batch.

Columns: Project | Site | Channel | Channel ID | URL | Error.

Workbook layout
Each per-channel workbook contains a single block:

text
Row 1   <channel_id>  |  <channel_name>  |  <dataflow_url>
        dark background, white bold, thin border, merged across all columns

Row 2   Import | Rule | Intermediate | Export Rule | Export | Data Source
        Import / Intermediate / Export headers carry the same colours
        as attInvestigator.py:
          Import        green  92D050
          Intermediate  blue   00B0F0
          Export        green  92D050
        other headers have no fill

Row 3+  attribute rows, thin borders, centered
        orphan highlights (purple 7030A0, white text):
          only in Import        -> Import cell highlighted
          only in Intermediate  -> Intermediate cell highlighted

First two rows are frozen so the header stays visible while scrolling.
Channels without an export section use a 4-column header:
Import | Rule | Intermediate | Data Source.

Features
Tri-state cascade
Check a Project → all its Sites and Channels are checked.

Check some Channels → the Site and Project show a partial state.

Uncheck a Site → the Project shows partial, and newly loaded
Channels are not auto-checked (partial means "do not select all").

Lazy loading
On startup only the Project list is fetched.

Expanding a Project fetches its Sites.

Expanding a Site fetches its Channels.

Already-loaded nodes are never re-fetched.

Search
Matches name or id.

Keeps the ancestor chain of every match.

Auto-expands matching branches.

Does not trigger network requests, even when expanding nodes that
have not been loaded yet.

Cache
HTML cache is shared across runs (keyed by channel_id).

--refresh forces a re-fetch.

Reports always carry a timestamp, so they never overwrite each other.

Cancellation
Click Cancel in the GUI, or press Ctrl+C.

Honoured between channels, between retries, during frame discovery,
and during the settle sleep.

An in-flight Playwright navigation is not aborted; control returns
as soon as possible.

Cancelled channels are recorded with error = "ERROR: cancelled" and
end up in _errors.xlsx.

FAQ
Q: Why does a channel have only 4 columns in its report?
That channel's page has no export section. attInvestigator returns a
4-column layout in that case. This is expected.

Q: Fetch fails with marker 'xxx' missing.
The page did not finish rendering. Increase --settle (e.g. 30) and
retry with --refresh. If it keeps failing, the page structure changed;
update the marker constants in test_playwright.py / core/fetcher.py.

Q: 403 from platform-api.productsup.io/data-flow/....
The dataflow page must be built from the dataflow host, not the API
host. Set dataflow_base_url to https://platform.productsup.com in
config/credentials.json.

Q: Token expired.
pu_auth.json and the API token are separate.

pu_auth.json expired → re-run python pu_login.py

API token expired (401/403) → update config/credentials.json

Q: Can I re-parse without re-fetching?
Keep the HTML cache and do not pass --refresh; the pipeline will skip
fetch and go straight to parsing.

Q: I see many .xlsx files in output/reports/.
One report per channel is expected. Sort by name to group runs together
(the timestamp suffix is at the end). _errors.xlsx is a summary of the
latest batch that had failures.

Q: _errors.xlsx from a previous failed run is still here after a
successful run.
_errors.xlsx is only written when a run has failures; it is not
deleted on success. Delete it manually if it is stale.

Q: The same channel appears twice in the report.
It will not. The pipeline enforces unique channel_id; duplicates are
rejected by the fetcher before any file is written.

Design constraints
core/** must not print, call input, or import Qt.

core/** is the only layer doing network I/O, filesystem I/O, or
parsing.

ui/** handles user interaction and presentation only.

attInvestigator.py, pu_login.py, test_playwright.py,
export_selector.py are kept as-is and never modified.

Verify the first constraint:

bash
grep -rE "print\(|input\(|PySide6" core/
No output means the rule is respected.