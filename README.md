# attrMapGetter

Batch-fetch and parse Productsup dataflow attributes into one Excel
workbook per channel.

Pipeline: **select project/site/channel → fetch iframe HTML → parse
attributes → write per-channel reports**.

---

## Architecture

### Layered design

```text
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
│ ui/cli.py Terminal menu │
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
│ CORE │
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
└──────────────────────────────────────────────────────────────────────
```
### Directory structure
```text
attrMapGetter/
├── pu_login.py               One-time login helper
├── attInvestigator.py        Attribute parser engine
├── export_selector.py        API reference
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
│   └── cli.py                Terminal menu
|   └── icons/
|       └── sliders.svg
|       └── tree.svg
|       └── terminal.svg       
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
```
### 😖say good byeeeeee to tedious investigationnnnnnn

### Please remember that pip -m install requirements.txt!!!
