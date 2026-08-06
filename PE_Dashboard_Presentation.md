---
marp: true
theme: default
paginate: true
size: 16:9
---

<!-- 
HOW TO USE THIS FILE
1. Install the "Marp for VS Code" extension → open this file → click "Open Preview
   to the Side" to present directly, or use the Marp status-bar icon → Export to
   PPTX/PDF (needs the Marp CLI, one-time: npm install -g @marp-team/marp-cli).
2. Or just copy each "---"-separated section into PowerPoint manually — the
   headings/bullets below map 1:1 to slide title/body.
3. Speaker notes are the italic lines starting with "Talk track:" under each
   slide — delete before printing, keep for delivery.
-->

# PE Audit Dashboard
### Architecture, Formulas & Azure-Correlated SLA Intelligence

A single source of truth for Performance Engineering audits across 250–300
customer engagements.

*Talk track: open by framing this as "how we replaced a manual, spreadsheet-driven
audit with one deterministic pipeline that never disagrees with itself."*

---

## Agenda

1. The problem this replaces
2. Tech stack
3. Architecture — one core pipeline, two independent read paths
4. Six data sources (only 3 feed the shared table)
5. SLA resolution — tiered fallback + the buffer formula
6. Job exclusion, concurrent-job handling, false-signal negation, cyclic/multi-SLA batches
7. Pulling Azure resource metrics
8. Correlating batch, SLA & resource — 5 formulas (RFCS / SRI / JRTOS / CRS / OSHS)
9. SOW contract vs. actual volume
10. Findings engine — 14 automated rule sections
11. Governance, export & sign-off
12. Roadmap / Q&A

---

## The Problem

- Legacy process: a Streamlit monolith + manual cross-referencing of Ctrl-M
  exports, SLA spreadsheets, SOW PDFs, and Azure portal screenshots
- Every customer has **different DFU/SKU/SLA values** — no two engagements
  are alike, so hardcoded thresholds silently produce wrong verdicts
- Metrics were **recomputed independently on each screen** → two panels could
  show two different compliance numbers for the same job
- No repeatable link between "batch job breached its window" and
  "the VM was under CPU/memory pressure at that exact time"

*Talk track: this is the "why" slide — it justifies the single-source-of-truth
design and the correlation formulas that follow.*

---

## Why FastAPI, Not Streamlit

| | FastAPI (this project) | Streamlit (legacy) |
|---|---|---|
| Model | Real HTTP API + separate frontend | Script re-executed top-to-bottom on every widget event |
| Frontend | Full control — vanilla JS, Tailwind, Chart.js/Plotly | Fixed widget set, limited layout control |
| Concurrency | Async, isolated per-request/session | Single-threaded per session, global state bleeds across users |
| Multi-customer isolation | Explicit session boundary (`session_cache.py`) | `st.session_state` — easy to leak between users |
| API surface | Real REST endpoints, callable by other tools | None — UI and logic are inseparable |
| Fit for 250–300 customers | Scales — deployable via uvicorn/Docker | Prototype-grade, not built for concurrent multi-tenant use |

```mermaid
flowchart LR
    U[Analyst uploads files] --> R[FastAPI routers/*.py]
    R --> S[services/*.py business logic]
    S --> C[session_cache.py\nin-memory audit context]
    C --> W[resolved_workflow_df\nsingle source of truth]
    W --> J["/api/* JSON response"]
    J --> F[static/app.js\nrenders panels + charts]
    F --> B[Browser — Tailwind, Chart.js, Plotly]
```

Every panel reads from the same `resolved_workflow_df` computed once per upload —
no screen recomputes its own numbers, so no two panels can ever disagree.

*Talk track: the Streamlit version had panels recomputing metrics independently.
Two screens would show different compliance numbers for the same job. This
architecture makes that impossible by design.*

---

## Tech Stack

| Layer | Technology |
|---|---|
| Backend | FastAPI + Python 3.14, Pydantic v2 |
| Frontend | Vanilla JS (ES2020+), Tailwind v3, Chart.js + Plotly.js |
| AI (optional) | Google Gemini (narrative generation, findings text) — toggleable, deterministic engine works with it fully off |
| Azure | `azure-identity`, `azure-monitor-query`, `azure-mgmt-compute/resource/subscription` |
| Data processing | pandas, numpy, openpyxl, PyMuPDF, python-docx, pypdf |
| Persistence | Flat JSON (`.pe_config.json` thresholds, `.pe_cache.json` session) — no external DB dependency |

*Talk track: no framework lock-in on the frontend, no DB server to provision —
the whole tool runs from one Python process, easy to deploy per-engagement.*

---

## Architecture — One Core Pipeline, Two Independent Read Paths

```mermaid
flowchart LR
    subgraph Uploads["Uploads — feed the ONE shared table"]
        A[Ctrl-M Batch CSV]
        C[SLA Matrix XLSX]
        D[SOW Contract PDF]
    end
    A --> P[batch_calculator.py]
    C --> M[sla_merger.py]
    D --> S[sow_parser.py]
    S --> CFG[(config_store:\n_sow_sla_windows)]
    P --> T[_compute_sla_matrix]
    M --> T
    CFG -. "Tier 2 fallback, read back in" .-> T
    T --> X[(resolved_workflow_df\nsession cache — THE shared table)]
    X --> U1[Batch SLA panel]
    X --> U2[Findings engine]
    X --> U3[Exported report]
    X -. "joined client-side" .-> U4[Executive dashboard]

    subgraph Independent["Independent paths — own data, own screen, never merged into the shared table"]
        B[Resource DOCX / Azure Live] --> R[resource_calculator.py] --> RS[(resource_summary)]
        E[Benchmark XLSX] --> BR[routers/benchmark.py] --> BS[(last_benchmark)]
    end
    RS --> N1[AI narrative / consultant]
    BS --> N2[Findings engine + AI narrative]
    RS -. "joined client-side" .-> U4

    F["Issues Register\n(typed in the browser — no file, no parser, no cache)"]
```

**What this actually says, in a nutshell:**
- **3 pillars share one number**: Batch CSV + SLA Matrix XLSX + SOW PDF all
  feed `_compute_sla_matrix`, which writes ONE table (`resolved_workflow_df`)
  that every batch/findings/report screen reads. SOW's path is real but
  indirect — it's saved to a config store first, then read back in as a
  fallback (Tier 2), not passed in directly like the other two.
- **2 pillars are separate on purpose**: Resource and Benchmark are parsed
  and cached independently. They power their own tabs and feed the AI
  narrative, but they never rewrite the shared batch/SLA table. The
  Executive dashboard is the one place batch + resource numbers appear
  together — and even that join happens in the browser, not on the server.
- **1 "pillar" isn't a pillar**: the Issues Register isn't uploaded or
  parsed at all. It only exists as a list you type into the dashboard in
  your browser tab — closing the tab loses it. It should not be listed
  alongside the five real file uploads without saying that.

**Rule: every batch/SLA screen reads from `resolved_workflow_df`. No screen
recomputes its own metrics.** That rule holds for Batch, SLA Matrix, and SOW —
it does not apply to Resource, Benchmark, or Issues Register, which are
honestly separate and shouldn't be described as part of "one pipeline."

---

## Six Data Sources (only 3 feed the shared table)

| Source | Feeds file | What it contributes | Joins `resolved_workflow_df`? |
|---|---|---|---|
| Batch | Ctrl-M CSV | Job runtimes, start/end, failures | ✓ Yes — direct |
| SLA Matrix | XLSX (BatchSLA_info) | Contracted SLA hours per workflow — **Tier 1** | ✓ Yes — direct |
| SOW Contract | PDF | Contracted DFU/SKU volumes, batch-window ceilings — **Tier 2** | ✓ Yes — via `config_store`, not direct |
| Resource | DOCX fleet report **or** direct Azure Monitor Live fetch (no upload needed) | CPU/mem/disk per server | ✗ No — own cache key, own tab |
| Benchmark | XLSX, parsed inline in `routers/benchmark.py` | Transaction/UI performance thresholds | ✗ No — own cache key, own tab |
| Issues Register | Nothing — typed directly into the dashboard | Open tickets cross-referenced against findings | ✗ No upload/parser at all — browser-only list |


*Talk track: each pillar is optional — the dashboard degrades gracefully and
tells the PE reviewer exactly which pillar is missing, rather than guessing.*

---

## SLA Resolution — Tiered Fallback, Every Default Documented

```mermaid
flowchart TD
    Q1{Tier 1: BatchSLA_info.xlsx\nexact key match found?}
    Q1 -->|yes| T1[Use Tier 1 value]
    Q1 -->|no| Q2{Tier 2: SOW PDF\nbatch-type ceiling found?}
    Q2 -->|yes| T2[Use Tier 2 value]
    Q2 -->|no| T3[Tier 3: pe_config global default\nDAILY=6h / WEEKLY=8h — always resolves]
    T1 --> V[sla_h + sla_source + reason_code + debug_runtime_source]
    T2 --> V
    T3 --> V
```

Tier 3 has no "not found" branch — a global default always resolves, so
there is never a dead end.

- **Works for every customer automatically** — workflow names are normalized
  before lookup (env prefix stripped, uppercased), so `PROD_SCPO_D1` and
  `SCPO_D1` match the same row. No per-customer configuration needed.
- **Every resolved value carries its source** — `sla_source` tags whether
  the number came from the customer's own XLSX file, their SOW contract, or
  the global default. A reviewer always knows which tier applied and why.
- **Tier 1 is verified** against real customer engagements. Tier 2 (SOW
  ceiling) is wired but not yet tested against a live SOW PDF — validate
  before first real use.

---

## The Buffer Formula (used everywhere)

> **bufferPct = (SLA hours − runtime hours) ÷ SLA hours × 100**

**Worked example**: SLA = 6h, job actually ran 4.5h
→ `(6 − 4.5) / 6 × 100` = **25% buffer** → LONG_JOB (see table below)

| Condition | Status |
|---|---|
| Completion code indicates failure | **FAILED** (buffer not computed) |
| `buffer_pct > 40%` | **OK** |
| `15% < buffer_pct ≤ 40%` | **LONG_JOB** (getting close) |
| `0% < buffer_pct ≤ 15%` | **AT_RISK** |
| `buffer_pct ≤ 0%` | **BREACH** |
| SLA = 0 or missing | **SLA_MISSING** (not a breach — flagged as a data gap) |

- Thresholds (`AT_RISK=15%`, `LONG_JOB=40%`) are defined once in `pe_config.py` and
  read by every component — SLA matrix, findings engine, frontend — so the same
  job can never show different statuses on different screens.
- A zero or missing SLA never silently becomes a BREACH — it's flagged as
  `SLA_MISSING` with no buffer computed at all.
- A very tight SLA (e.g. 10-minute ceiling) can produce large negative buffers
  (−3713% is a real example from a customer file). This is mathematically correct —
  it's a signal the SLA entry itself needs a second look, not a display bug.

---

## Which Jobs Get Excluded From Analysis — and Why

Every exclusion is **named and surfaced to the reviewer** (`_build_excluded_jobs_list()`
+ the `excluded_jobs`/`excluded_sub_apps` payload) — nothing is silently dropped.

**In plain terms:** a job is only ever removed from one specific number (e.g.
"the compliance %"), never wiped from the dashboard entirely — except when a
reviewer explicitly chooses to exclude it from analysis.

```mermaid
flowchart TD
    J["Every job in the Ctrl-M file"] --> Q1{"Reviewer manually\nexcluded this job?"}
    Q1 -->|yes| OUT1["Removed from SLA analysis\n(still visible in raw file/heatmap)"]
    Q1 -->|no| Q2{"Utility/infra job?\n(file_watcher, backup, export_ ...)"}
    Q2 -->|yes, and run was short| OUT2["Excluded — housekeeping,\nnot real batch work"]
    Q2 -->|no, or ran too long to be housekeeping| Q3{"Cyclic/polling job?\n(>5 runs/day, avg <15 min)"}
    Q3 -->|yes| OUT3["Dropped from window-elapsed\nmeasurement only"]
    Q3 -->|no, but retry-storm pattern| WARN["Kept, flagged RETRY_STORM —\nsurfaced as a warning"]
    Q3 -->|no| Q4{"Out-of-scope schedule?\n(MONTHLY/ADHOC/OUTBOUND/...)"}
    Q4 -->|yes| OUT4["Dropped from window-compliance\ndenominator only — still checked\nfor breach/anomaly"]
    Q4 -->|no| Q5{"Too few or too short runs\nto trust a baseline?"}
    Q5 -->|"< 3 runs"| OUT5A["INSUFFICIENT —\nexcluded from compliance %, still listed"]
    Q5 -->|"avg < 5 min"| OUT5B["SHORT_JOB —\nexcluded from compliance %, still listed"]
    Q5 -->|no| KEEP["Counted everywhere:\nSLA, compliance, findings"]
```

**Utility and cyclic checks are independent** — a job can be flagged by both
at once, since they act on different scopes (a job's own utility label vs. its
sub-application's window measurement). The one place order matters: if a job
matches a utility pattern, that's always the reason shown — never
`SHORT_JOB`/`INSUFFICIENT` — since housekeeping is checked first.

| Exclusion | What triggers it | Effect |
|---|---|---|
| **User-picked jobs** | Analyst opts a job out by name | Removed from SLA analysis; still visible in the raw file/heatmap |
| **Utility/infra jobs** | Name matches a known pattern (`file_watcher`, `backup`, `export_`, …), and — for runtime-gated patterns — the run was short | Excluded as housekeeping, not real batch work |
| **Cyclic/polling jobs** | More than 5 runs/day **and** average runtime under 15 minutes | Dropped from window-elapsed measurement only |
| **Retry storms** | A one-day spike in run count with no regular pattern — a failure retry cascade, not polling | Kept and flagged with a warning, not excluded |
| **Out-of-scope schedule types** | `MONTHLY`/`QUARTERLY`/`ADHOC`/`OUTBOUND`/etc. — schedules with no daily SLA window | Dropped from the window-compliance denominator only; still checked for breach/anomaly |
| **Adaptive-SLA quality gate** | Too few runs (`INSUFFICIENT`, < 3) or too short an average (`SHORT_JOB`, < 5 min) to trust a baseline | Excluded from the compliance % only; still listed |

*Talk track: "excluded" never means "hidden" in this dashboard — it means
"removed from a specific denominator, for a stated reason, with the reason
visible in the UI."*



---

## SLA After Upload — Exact Join First, Then Adaptive Fallback

When a customer uploads an SLA spreadsheet, the dashboard matches each Ctrl-M
workflow to its correct SLA contract using four progressively looser checks —
and refuses to guess if two workflows could plausibly match the same row.

```mermaid
flowchart TD
    U["BatchSLA_info.xlsx uploaded"] --> M1{"Exact name match?"}
    M1 -->|yes| HIT["Use this SLA row"]
    M1 -->|no| M2{"Anchor match?\n(First/Last job names)"}
    M2 -->|yes| ACOL{"Same job used by\nmultiple contracts?"}
    ACOL -->|no| HIT
    ACOL -->|yes| FILTER["Split by calendar day —\neach run scored against\nits own contract"]
    FILTER --> HIT
    M2 -->|no| M3{"Token match?"}
    M3 -->|yes| HIT
    M3 -->|no| T2["Fall through to\nTier 2 / Tier 3"]
    HIT --> COL{"Two different workflow\nnames collide on the\nsame key?"}
    COL -->|yes| DROP["Skip — never guess\nwhich workflow wins"]
    COL -->|no| USE["SLA assigned"]
```

Some customers run the same job under more than one contract, depending on
the day (e.g. a weekday rate vs. a Saturday rate). The dashboard detects this
automatically and scores each run against the contract for its own calendar
day — never a single averaged or arbitrarily-picked number.

The measured window itself is anchored to the contract's own named start/end
jobs, not just the earliest and latest timestamp in the file — so a pre-batch
prep job or a late cleanup job can't stretch the measured window.

If no SLA spreadsheet is uploaded at all, each job builds its own SLA ceiling
from its own Ctrl-M run history — more history in, a tighter and more
confident number out:

| Runs available | Confidence label | SLA is set to |
|---|---|---|
| ≥ 14 OK runs | `STRONG` | 95th-percentile runtime (p95) |
| 7–13 OK runs | `MODERATE` | the larger of p90, or average + 2 standard deviations |
| 3–6 OK runs | `WEAK` | a blended peak/variance estimate |
| < 3 OK runs | `INSUFFICIENT` | a best-guess peak — excluded from the compliance % |

Every one of these is still capped at the schedule's global default (6h
daily / 8h weekly), so a single noisy job can never claim a bigger SLA than
the engagement's own default allows.

*Talk track: even with zero uploads, every job still gets its own
history-derived ceiling — never one blanket SLA for everyone.*

---

## Concurrent & Overlapping Jobs — Busy-Time, Not a Naive Sum

**In plain terms:** if two jobs overlap, the dashboard doesn't double-count the
overlapping minutes, and it doesn't stretch the window across a long idle gap
either. Neither "add up every job's runtime" nor "first start to last end" is
accurate once jobs run in parallel or in separate clusters — so the dashboard
does neither.

```mermaid
gantt
    dateFormat HH:mm
    axisFormat %H:%M
    section Job A / B (overlap)
    01:00 - 02:00 (1h)      :a1, 01:00, 02:00
    01:30 - 03:00 (1.5h)    :a2, 01:30, 03:00
    section Job C (separate cluster)
    Idle gap                :crit, 03:00, 04:00
    04:00 - 04:30 (0.5h)    :a3, 04:00, 04:30
    section Real busy time
    Block 1 = 2h            :done, b1, 01:00, 03:00
    Block 2 = 0.5h          :done, b2, 04:00, 04:30
```

**Tested with 3 jobs** (01:00–02:00, 01:30–03:00, 04:00–04:30):
- Naive sum of runtimes: 1.0h + 1.5h + 0.5h = **3.0h**
- Naive span (first start to last end): 04:30 − 01:00 = **3.5h**
- Actual reported busy time (two blocks, gap excluded): **2.5h** ✓

Both naive numbers are wrong in different ways — summing overstates by
double-counting the overlap, and spanning overstates by counting the idle
gap as if the batch were still running. The dashboard reports neither.

- A one-hour or longer idle gap between runs splits the day into separate
  batch blocks (e.g. a morning phase and an evening phase) instead of
  treating the gap as if work was happening the whole time.
- A separate risk score (CRS, used in the correlation formulas) estimates
  blast radius as "how many other jobs share this one's sub-application" —
  a practical proxy, not a true dependency chain (Ctrl-M exports don't
  record job-to-job dependencies). That score is one shared number per
  sub-application, not calculated separately for each job inside it.

---

## Negating False Signals From Ctrl-M

| Bad signal | What would happen if unhandled | Status |
|---|---|---|
| Job runs past midnight | Runtime would look negative | ✓ Corrected |
| Corrupt timestamp pairs | Runtime could look absurdly long | ✓ Capped to a sane max |
| Retry storms | Would look like a legitimate recurring job | ✓ Filtered out |
| A sudden, implausible speed-up | Would look like a genuine improvement | ✓ Flagged for review, not accepted |
| Failed runs | Would drag down real performance averages | ✓ Excluded from averages, tracked separately |
| Missing start-time data | Job silently timestamped "now" | ⚠️ **Known gap — see below** |

⚠️ **Known gap**: when a file has no usable start-time column, affected jobs
lose their real runtime and can be silently marked as if they had none —
appearing "fine" instead of being flagged as missing data. A warning badge
for this exists in the dashboard but isn't yet connected to trigger
automatically. **Fix planned**: connect the badge so any file missing
start-time data is clearly flagged to the reviewer, not silently absorbed.

---

## Multi-SLA & Cyclic vs. Non-Cyclic Batches

- **Schedule classification** — two separate functions, not one:
  - `services/sla_merger.py: detect_batch_type(name, schedule)` — full
    extended classifier used when parsing SLA XLSX files. Priority order:
    `ADHOC` → `CYCLIC_INTERVAL` → `CYCLIC/INTRADAY` → `CALENDAR_BASED` →
    `ANNUAL` → `MONTHLY_WORKDAY` → `DATE_SPECIFIC_MONTHLY` → `PERIODIC` →
    `SEQUENCING` → then the standard keyword loop (`BIWEEKLY`/`WEEKLY`/
    `MONTHLY`/`QUARTERLY`/`OUTBOUND`/`DAILY`). `ADHOC` wins if both `ADHOC`
    and `DAILY` appear in the same name — e.g. `DAILY_ADHOC_RECON` → `ADHOC`.
  - `services/pe_utils.py: detect_batch_type(job_name)` — lightweight
    4-type classifier (DAILY/WEEKLY/BIWEEKLY/MONTHLY) used when scoring
    individual Ctrl-M jobs with no schedule text available. Simpler rules,
    different scope. Two independently maintained functions, not aliases.
  - `services/sla_engine.py: classify_schedule(text)` — a third function
    operating on raw schedule text strings, used by the SLA engine layer.
  These three are separate implementations for different inputs and call
  sites. No parity test currently enforces they agree on the same name.

- **Multiple SLA ceilings in one review** (`window_inscope_ceiling_count`):
  this tracks the number of *distinct resolved ceilings across different
  sub-applications* in a single review — e.g. if one sub-app has a 6h
  ceiling and another has a 14h ceiling, it reports 2 and changes the
  headline from "within the 6h window" to "each within its own ceiling
  (6h–14h)." This operates at the **cross-sub-application level only**.
  It does NOT cover calendar-variant rows for the same workflow name
  (e.g. `SCPO_D1` vs `SCPO_D1(Saturday)` — different ceilings for the
  same sub-app depending on the day of week). That is handled by
  `_decompose_subgroup()` in `routers/sla_matrix.py` — a separate
  mechanism, separately fixed.

- **Cyclic ≠ excluded from everything** — a cyclic sub-app is dropped from
  the *window-elapsed* measurement only (it would otherwise inflate the
  window to ~24h and manufacture a false 0% compliance day). Individual
  job runs are still counted for job-level breach/anomaly/failure-rate.
  The cyclic threshold is **0.25h (15 minutes)** — confirmed in
  `pe_config.py: CYCLIC_MAX_RUNTIME_HRS = 0.25`. A job averaging under
  15 minutes AND running more than 5 times per day is classified cyclic.

---

## Pulling Azure Resource Metrics

- Auth: `InteractiveBrowserCredential` (no `DefaultAzureCredential` — banned,
  it hangs 30s+ probing IMDS on non-Azure machines). Sign in once, silently
  restored from a local auth-record cache on reload/restart.
- The one-line fleet summary (CPU/Memory/Disk snapshot) requests **AVERAGE +
  MAXIMUM + MINIMUM in a single Azure Monitor call** per VM — not three
  separate round trips.
- The Deep Dive timeseries panel is a **different code path and deliberately
  makes two calls per VM**: one for AVERAGE (the chart line), one for
  MAXIMUM + MINIMUM (the true-peak overlay). They're kept independent so a
  failure in one doesn't blank out the other — and the Max series is what
  catches a brief spike that would otherwise average away to nothing over a
  7/15/30-day window, critical for spotting a short nightly-batch CPU spike a
  daily average would hide.

```mermaid
sequenceDiagram
    participant PE as PE Reviewer (browser)
    participant App as FastAPI backend
    participant Azure as Azure Monitor
    note over App: Deep Dive panel — auth + fetch flow
    alt cached credential valid
        App->>App: restore from local auth-record cache
    else first use / cache expired
        PE->>App: Sign in (InteractiveBrowserCredential)
        App-->>PE: session established
    end
    App->>Azure: Query 1 — AVERAGE (chart line), N days back
    Azure-->>App: per-VM average series
    App->>Azure: Query 2 — MAXIMUM + MINIMUM (true-peak overlay)
    Azure-->>App: per-VM max/min series
    App->>App: _compute_baseline_analysis()
    App-->>PE: Deep Dive panel (heatmaps, trends, hot hours)
```

### Corporate-Machine Auth Safeguards (baked into `services/azure_monitor.py`)

| Safeguard | Problem it fixes |
|---|---|
| `DefaultAzureCredential` banned | IMDS probe hangs 30s+ on non-Azure machines |
| IPv4 forced for DNS | Corporate DNS returns only IPv6 for `login.microsoftonline.com`; IPv6 broken → 83–180s timeout |
| `platform.platform()` stubbed | Azure identity calls this at import time; WMI hang under corporate group policy |
| MSAL DPAPI bypassed | `TokenCachePersistenceOptions` hangs on Python 3.14 free-threaded; replaced with UTF-8 `FilePersistence` |
| Per-session credentials | Concurrent analysts never share or overwrite each other's Azure token |
| Failure-isolated metric groups | One unsupported metric on a VM doesn't fail the entire query call |
| Percentage-only grading | Raw byte/ops counters are chart-only — never fed into severity classifier (byte values would grade every point as critical) |

---

## Azure Baseline Intelligence

`_compute_baseline_analysis()` turns raw timeseries into judgment-grade
evidence (needs ≥ 2 days of data, **15 days recommended**):

| Signal | What it detects |
|---|---|
| Hot hours | Consistent pressure at the same clock hour → batch fingerprint |
| Trend acceleration | Metric getting worse across the observation window |
| Weekday vs weekend divergence | Batch-driven load vs. steady-state load |
| Chronic pressure | High utilization for many consecutive days → undersized VM |
| Multi-day recurring spikes | Same hour spiking on N different days → definitive batch signature |
| Fleet-wide trend | Roll-up across all VMs for an executive verdict |

*Talk track: this is what upgrades a finding's evidence class from "inferred"
to "measured" — Azure telemetry corroborating a Ctrl-M-only observation.*

---

## Correlating Batch, SLA & Resource — 5 Formulas

`services/correlation_engine.py` — the cross-pillar math no single-source
tool can do, because it needs both Ctrl-M **and** Azure Monitor data:

| Formula | Meaning |
|---|---|
| **RFCS** (0–100) | Resource-Failure Correlation Score — how much resource stress lines up with job failures |
| **SRI** (0–∞) | SLA Risk Index per job — buffer distance amplified by CPU pressure |
| **JRTOS** (0–1, one score per hour-of-day bucket 0–23) | Job-Resource Temporal Overlap — which hour of day is riskiest |
| **CRS** (0–1) | Cascade Risk Score — likelihood a breach cascades downstream |
| **OSHS** (0–100 → A–F) | Overall System Health Score — executive-dashboard grade |

```mermaid
flowchart LR
    CM["Ctrl-M batch data\nfailures · runtimes · buffers"] --> RFCS
    CM --> SRI
    CM --> CRS
    AZ["Azure Monitor data\nCPU · memory"] --> RFCS
    AZ --> SRI
    RFCS --> OSHS["OSHS\nexecutive grade A–F"]
    SRI --> OSHS
    CRS -.->|"per-job risk —\nfeeds findings, not OSHS directly"| FIND["Findings engine"]
```

⚠️ **Calibration caveat**: across the real engagements reviewed so far
(failure rates consistently <1%, average CPU consistently <5%), RFCS and
SRI's amplification terms rarely activate — both formulas were designed
assuming a wider input range than this Ctrl-M/Oracle batch workload class
actually produces. On this class of engagement they may not discriminate a
0.1%-failure customer from a 3%-failure one as clearly as the "0–100"/CPU-
amplified framing implies. Worth validating against a heavier-load
engagement before presenting either as a differentiated signal to a customer.

---

## Formula Detail (with the clamps that actually ship)

**Read this table first — it's the whole section in one glance:**

| Formula | In plain English | Goes up when | Bounded to | Worked example |
|---|---|---|---|---|
| **RFCS** | Resource stress lining up with job failures | Servers are stressed **and** jobs are failing at the same time — failures alone on calm servers don't move it | 0–100 | 20% failure rate, 85% CPU, 70% mem, 3 critical servers → **≈ 23** |
| **SRI** | How close one job is to breaching its SLA | Runtime nears the SLA ceiling, worse if CPU was also under load | `>1.0` = breach (no upper cap) | 5h job vs 6h ceiling, 85% CPU → **≈ 0.96** |
| **CRS** | Odds a job's failure cascades to jobs behind it | Job fails **and** has many downstream jobs **and** its own SLA breach was deep | 0–1 | Failed job, 8 downstream jobs, −200% buffer → **≈ 0.62** |
| **OSHS** | One executive grade for the engagement | A blend: 40% batch health + 35% SLA health + 25% resource health | 0–100 → A–F | Re-weights to ~0.53/0.47 batch/SLA if no resource data exists |
| **JRTOS** | Which hour of the day is riskiest | Job volume, failure rate, and CPU pressure all peak in the same hour | 0–1 (naturally, no clamp needed) | 2am: busiest hour, 16.7% fail rate, 40% CPU → **≈ 0.067** |

The raw math and the caveats worth knowing before quoting a number to a
customer:

**RFCS** — `cap100( failureRate × (0.6×avgCPU + 0.4×avgMem)/100 × (1 + 0.15×min(criticalServers,10)) )`
`failureRate` is a percentage (`100 − compliance_pct`), not a fraction. The
raw value can reach ~250 before the clamp caps it at 100.

**SRI** — `(peakHours / slaCeilingHours) × (1 + max(0, (avgCPU−70)/100))`
`peakHours / slaCeilingHours` is algebraically `1 − bufferPct/100`, so SRI
reuses the same buffer fact you already trust — it's not a second
computation that could quietly disagree with the buffer number elsewhere.

**CRS** — `cap1( failedFlag × (downstreamCount/(downstreamCount+5)) × (1 − clamp(slaBuffer,0,100)/100) )`
`slaBuffer` is clamped to `[0,100]` before use, so a −200% (deep breach)
becomes `0`, not a negative that could push CRS past 1.
⚠️ **Known limitation**: because of that clamp, every breach past 0% buffer
maxes out the buffer-risk term identically — verified with a real worst-case
row (`calc_crs(True, 8, -3713.1)` returns the same `0.615` as a plain −20%
breach with the same downstream count). CRS can't tell "barely breached"
apart from "catastrophically breached." Only chain size still varies it
between two failed jobs.

**OSHS** — `0.40×batchScore + 0.35×slaScore + 0.25×resourceScore`
Weights re-normalize over batch+SLA only when no resource data exists — it
never fabricates a resource score just to fill the formula.

**JRTOS** — `(jobs[h]/maxJobsInAnyHour) × (failRate[h]/100) × (peakCPU/100)`, one score per hour `h` (0–23)
All three factors are ratios in `[0,1]`, so the result is naturally bounded
with no clamp needed. The "0–23" is the hour index, not the score's range.

*Talk track: every formula here is clamped at both ends in code — the deck
now states the clamps and a worked example for each, instead of leaving the
math abstract.*

---

## Spike-to-Batch Attribution (the unique correlation)

For every Azure Monitor spike (critical / critical-sustained / warning
severity only), find which Ctrl-M jobs were **running during that exact
window**:

```mermaid
flowchart LR
    A[Azure spike: 95% CPU\n02:13–02:41 UTC] -->|time overlap join| B{Ctrl-M job_runs_df}
    B --> C[CALCPLAN_Daily\nran 01:50–06:05, 4.2h]
    C --> D["Finding: spike coincides with CALCPLAN_Daily"]
```

- Join condition: `run.start < spike.end AND run.end > spike.start`
- Explicitly labeled **time-coincidence, not host-pinned causation** — Ctrl-M
  exports carry no host column today. Ranked by job runtime, top-N surfaced
  per spike.
- *(Upgrade path already scaffolded: once a job→VM map exists, the same join
  narrows to genuine host-pinned causation with one filter added.)*

---

## SOW Contract vs. Actual Volume

- SOW PDF parsed for contracted **DFU/SKU/order volumes** and **batch-window
  ceilings** (Gemini-assisted extraction with hard bounds validation: SLA
  windows 0.5–48h, volumes 1–50M, availability 90–100% — out-of-range values
  are rejected, never silently accepted).
- Actual volume (from Batch/manual entry) compared against the SOW baseline:

> **pct = actual ÷ SOW contracted × 100**

**Worked example**: SOW contracted = 500,000 DFU/day, actual = 265,000
→ `265,000 / 500,000 × 100` = **53% → LOW**, under-utilized vs. contract

**In plain terms:** the same percentage is tested against four thresholds in
a fixed order, and the **first one it satisfies wins** — so a number can never
accidentally match two statuses at once.

```mermaid
flowchart TD
    P["pct = actual ÷ SOW contracted × 100"] --> C1{"> CRITICAL threshold?"}
    C1 -->|yes| R1["CRITICAL_OVER\nblocks sign-off without disclaimer"]
    C1 -->|no| C2{"> OVER threshold?"}
    C2 -->|yes| R2["OVER"]
    C2 -->|no| C3{"< LOW threshold?"}
    C3 -->|yes| R3["LOW — under-utilized vs. contract"]
    C3 -->|no| C4{"< 90%?"}
    C4 -->|yes| R4["ACCEPTABLE — inside window, lower end"]
    C4 -->|no| R5["OPTIMAL — preferred zone"]
```

| Order | Condition | Status |
|---|---|---|
| 1 | `> SOW_OVER_CRIT_PCT` | **CRITICAL_OVER** — blocks PE sign-off without disclaimer |
| 2 | `> SOW_OVER_PCT` | **OVER** |
| 3 | `< SOW_UNDER_PCT` | **LOW** — under-utilized vs. contract |
| 4 | `< 90%` (remaining) | **ACCEPTABLE** — inside window, lower end |
| 5 | else | **OPTIMAL** — preferred zone |

✅ **Fixed**: the `90%` boundary is now `pe_config.SOW_ACCEPTABLE_PCT` — a
named, config-store-backed constant (`sow_acceptable_pct`), not a hardcoded
literal. It was previously duplicated as a raw `90` in 5 places
(`routers/sow.py`, `routers/export.py`, `routers/findings.py`,
`routers/pe_narrative.py`, `static/app.js`) — all five now read the same
constant. Verified the strict `if/elif` order never produces a gap or
overlap even when `SOW_UNDER_PCT` is configured above 90 (mutation-tested).

*Talk track: this is what tells a customer "you're running 53% under your
contracted daily volume" or "you've exceeded scope and need a change order."*

---

## Findings Engine — 14 Automated Rule Sections

1. Data confidence / audit coverage
2. Batch rules (R0–R8): compliance, window breach, buffer, anomalies
3. Resource rules: fleet grade, CPU/mem/disk pressure bands
4. Cross-source: batch breach + CPU pressure correlation
5. SLA Matrix: per-run, tightest buffer, repeat offenders
6. Benchmark: threshold breaches, worst transactions
7. SOW compare: exceeded / under / optimal
8. Regression: z-score > 2σ critical, 1.5–2σ drift warning
9. Adaptive SLA: p95 within 10% of ceiling
10. Issues register cross-reference
11. Intelligence (A1–A10): misleading-green detection, waiver detection, contradictions
12. Narrative + open audit gaps

*Talk track: every rule cites its evidence — a job name, a number, a
timestamp — never a generic "performance issue detected."*

---

## Governance, Export & Sign-off

- PE reviewer checklist gates sign-off on data completeness
- Blockers can be overridden with an explicit, logged disclaimer — never
  silently bypassed
- One-click **HTML report export**: pulls Batch, Resource, SLA, SOW,
  Benchmark, Findings, and Approval sections from the same
  `resolved_workflow_df` / session cache the live dashboard uses — the report
  a customer receives is provably the same numbers the reviewer saw on screen

---

## Roadmap / Q&A

- Host-pinned spike attribution (once job→VM mapping is available)
- Wider AI narrative coverage (Findings/Executive Summary sections in export)
- Additional Azure metrics (already extensible — one query call per metric group)

### Questions?
