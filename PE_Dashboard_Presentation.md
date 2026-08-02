---
marp: true
theme: default
paginate: true
size: 16:9
style: |
  section {
    font-size: 28px;
    padding: 58px 72px;
    color: #172033;
  }
  h1 { color: #103a5c; font-size: 72px; }
  h2 { color: #103a5c; font-size: 48px; }
  h3 { color: #176b87; }
  table { font-size: 22px; }
  code { color: #0b5e75; }
  .lead h1 { font-size: 72px; }
  .lead h3 { font-weight: 400; }
---

<!--
How to use this deck

Open this file with the "Marp for VS Code" extension and choose "Open Preview
to the Side". You can export it to PDF or PowerPoint from Marp. Each `---`
line starts a new slide.

The italic text is optional speaker guidance. Delete it before sharing the
deck if you do not want notes in the source file.
-->

<!-- _class: lead -->

# PE Audit Dashboard

### Clear SLA checks, evidence-based findings, and one report

**A Performance Engineering audit tool for customer batch environments**


---

## The dashboard turns audit data into a clear decision

The dashboard helps a Performance Engineer answer four practical questions:

| Question | Answer from the dashboard |
|---|---|
| Did the batch finish on time? | SLA and window compliance |
| Which jobs need attention? | Breaches, tight buffers, failures, and trends |
| Is there supporting infrastructure evidence? | Resource and Azure Monitor data, when available |
| What should the customer see? | Findings, an executive summary, and an exportable report |

---

## One shared data path replaces a fragmented audit

### Before

- Reviewers had to compare Ctrl-M exports, SLA spreadsheets, SOW documents,
  resource reports, and Azure screenshots by hand.
- Different screens could calculate the same metric in different ways.
- A batch delay and a resource spike were difficult to compare at the same time.

### With the dashboard

- Customer-specific SLA values are loaded once and reused.
- The audit has a shared evidence set.
- Findings show the job, value, and time behind the conclusion.

---

## Six data sources form one audit

| Audit area | Typical input | What it adds |
|---|---|---|
| Batch operations | Ctrl-M CSV | Job runs, timings, and failures |
| SLA agreement | `BatchSLA_info.xlsx` | Workflow-specific SLA targets |
| Contract context | SOW PDF | Volumes and batch-window limits |
| Server health | Resource DOCX/PDF or Azure Monitor | CPU, memory, disk, and trends |
| User experience | Benchmark XLSX | Transaction and UI timing results |
| Known issues | Issues register | Open tickets linked to findings |

You can start with only the files you have. The dashboard shows which audit
areas are still missing instead of pretending the data is complete.

---

## One shared audit context connects every output

```text
Customer files and Azure data
             ↓
FastAPI upload routes validate and parse the inputs
             ↓
Analysis services calculate SLA, batch, resource, and contract results
             ↓
Shared audit context holds the resolved evidence
             ↓
Dashboard panels, findings, executive summary, and exported report
```

The route files handle HTTP requests. The service files contain the business
rules. Keeping those jobs separate makes the calculations easier to test.

---

## One SLA answer everywhere

The central rule is simple: **resolve each workflow's SLA once, then reuse it.**

```text
Batch SLA matrix  →  SOW ceiling  →  configured default
       first             next            last option
                         ↓
              resolved workflow data
                         ↓
   batch panel · SLA matrix · findings · executive view · report
```

This avoids a common audit problem: two screens showing different compliance
figures for the same workflow.

---

## How an SLA is selected

| Priority | Source | Use |
|---:|---|---|
| 1 | `BatchSLA_info.xlsx` | A named, workflow-level customer target |
| 2 | SOW PDF | A contract ceiling when a workflow target is unavailable |
| 3 | Dashboard settings | A configurable fallback for the schedule type |

The dashboard records where each SLA came from. A reviewer can see whether a
number came from a customer file, the SOW, or a fallback setting.

---

## The simplest health measure: buffer

> **Buffer % = (SLA hours − runtime hours) ÷ SLA hours × 100**

Example: a job has a 6-hour SLA and finishes in 4.5 hours.

```text
(6 − 4.5) ÷ 6 × 100 = 25% buffer
```

| Buffer | Status | Plain meaning |
|---:|---|---|
| More than 40% | OK | Plenty of time left |
| 15% to 40% | Long job | Watch it |
| More than 0% and up to 15% | At risk | Very little time left |
| 0% or less | Breach | Finished after its SLA |

The thresholds are configuration values, not values copied into every screen.

---

## Window compliance measures the batch as a whole

For a workflow and run date, the dashboard measures the time from the first
relevant job start to the last relevant job finish. It then compares that
window with the workflow's SLA limit.

```text
First relevant job starts                         Last relevant job ends
          │──────────────── batch window ────────────────│
```

Daily workflows contribute to the main daily compliance figure. Weekly and
other schedule types are reported separately, because comparing them against
a daily target would be misleading.

---

## Data quality is visible

The dashboard does not treat every row as equally reliable.

- Short, frequent polling jobs can be identified as cyclic activity.
- Utility jobs can be excluded from a specific calculation when they would
  distort it.
- Retry storms remain visible as warnings. They are not quietly removed.
- Missing SLA information is shown as missing data, not converted into a
  breach.
- Every exclusion should have a stated reason for the reviewer.


---

## Azure adds infrastructure evidence

When Azure access is configured, the dashboard can query Azure Monitor for
resource trends such as CPU, memory, and disk activity.

```text
Azure Monitor metric spike
            ↓ compare timestamps
Ctrl-M jobs running in that time window
            ↓
Finding with the timing evidence
```

This helps the reviewer investigate questions such as: "Did resource pressure
occur while the delayed batch was running?"

---

## What correlation means, and what it does not mean

The dashboard can identify a time overlap between a batch job and an Azure
resource spike.

| It can say | It should not say without more data |
|---|---|
| "This job was running during the CPU spike." | "This job definitely caused the CPU spike." |
| "Resource pressure and failures occurred together." | "The resource issue caused every failure." |

Ctrl-M data does not always contain a job-to-VM mapping. Without that mapping,
the evidence is time correlation, not proof of host-level causation.

---

## Findings turn evidence into an audit view

The findings engine reviews the available evidence across the audit:

| Area | Examples of checks |
|---|---|
| Batch | SLA breaches, long jobs, failed runs, and runtime drift |
| SLA coverage | Missing targets and tightest buffers |
| Resource | CPU, memory, and disk pressure |
| Contract | Volume compared with SOW commitments |
| Benchmark | Slow user actions or transactions |
| Data confidence | Missing files, low coverage, and open audit gaps |

Good findings point back to evidence: a workflow, metric, timestamp, or source
file. They should not be generic warnings.

---

## The executive view answers: "Can we sign off?"

The executive view brings the audit pillars together:

```text
Batch result + SLA coverage + resource evidence + SOW position + data confidence
                                      ↓
                               audit verdict
                                      ↓
                         customer-ready summary and report
```

If key inputs are missing, the dashboard shows that limitation. A confident
verdict needs evidence, not a complete-looking screen.

---

## AI helps with language, not with the core maths

Google Gemini is optional. The deterministic Python engines still calculate
the core SLA, batch, resource, and findings results when AI is disabled.

| With AI enabled | Without AI |
|---|---|
| Narrative and consultant responses can be written in clearer prose | The calculation and evidence views remain available |

This keeps the audit reproducible: the numbers come from the data and rules,
not from generated text.

---

## A typical audit workflow

1. Upload the Ctrl-M batch file and, where available, the SLA matrix.
2. Add resource, Azure, SOW, benchmark, and issues data.
3. Review data completeness and SLA sources.
4. Investigate breaches, long jobs, failures, and correlated resource events.
5. Review the findings and executive verdict.
6. Export the report from the same audit context shown on screen.

---

## What the dashboard deliberately avoids

- It does not use one hardcoded SLA for every customer.
- It does not recompute the same compliance value separately in each panel.
- It does not present a time overlap as proven technical causation.
- It does not hide missing data behind a clean-looking score.
- It does not require AI for the underlying audit calculations.

---

<!-- _class: lead -->

# A traceable audit, ready for review

### Customer data → resolved SLA → measured evidence → clear decision

**customer data → resolved SLA → measured evidence → clear decision**
