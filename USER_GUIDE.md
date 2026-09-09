# PE Dashboard — User Guide

> A step-by-step guide for Performance Engineers on how to use the live portal.
> No technical knowledge required — just open the portal and follow the steps.

---

## Table of Contents

1. [Accessing the Dashboard](#1-accessing-the-dashboard)
2. [Selecting a Customer](#2-selecting-a-customer)
3. [Step 1 — Upload Your Files](#3-step-1--upload-your-files)
4. [Step 2 — Pull Azure VM Data Live](#4-step-2--pull-azure-vm-data-live)
5. [Step 3 — Review the SLA Matrix](#5-step-3--review-the-sla-matrix)
6. [Step 4 — Review Batch Analytics](#6-step-4--review-batch-analytics)
7. [Step 5 — Review Resource Health](#7-step-5--review-resource-health)
8. [Step 6 — Review Findings and Red Flags](#8-step-6--review-findings-and-red-flags)
9. [Step 7 — AI Insights](#9-step-7--ai-insights)
10. [Step 8 — Export and Save the Report](#10-step-8--export-and-save-the-report)
11. [Viewing Past Reports (Archive)](#11-viewing-past-reports-archive)
12. [Settings](#12-settings)
13. [FAQ](#13-faq)

---

## 1. Accessing the Dashboard

Open your browser and go to the portal URL shared by your team:

```
Access the Portal:
https://pedashboard-api-ui-asre-plan-ai-agents.us.live.internal.byp.ai/uploaddashboard
```

- No installation needed — it runs fully in the browser.
- Works on Chrome, Edge, and Firefox.
- You must be on the company network or VPN to access it.

---

## 2. Selecting a Customer

Once the portal loads, the first thing to do is **select your customer**.

- At the top of the page you will see a **Customer Selector** dropdown.
- Click it and pick the customer you are working on.
- All panels — SLA Matrix, Batch, Resources, Findings — will immediately show data for that customer only.
- You can switch between customers at any time. Each customer's data is stored separately — switching does not delete anything.

> **Tip:** Always confirm the customer name at the top before starting any analysis.

---

## 3. Step 1 — Upload Your Files

Click the **Upload** tab in the navigation.

You need to bring **two files** from the customer engagement. The tool cannot compute anything without them.

---

### A. Ctrl-M Execution File *(Required)*

**What it is:** The Control-M batch execution export for the customer.
**Format:** CSV or Excel (.xlsx)
**What it contains:** All job run history — job names, start times, end times, durations, and statuses.

**How to upload:**
1. Click **"Upload Ctrl-M / Batch File"**
2. Select the file from your computer
3. The tool will parse it and confirm how many jobs were loaded

---

### B. Batch SLA File *(Required)*

**What it is:** The SLA matrix for the customer's batch workflows.
**Format:** Excel (.xlsx)
**What it contains:** Workflow names and their agreed SLA time windows (e.g. Workflow X must complete within 6 hours).

**How to upload:**
1. Click **"Upload SLA / Batch Config File"**
2. Select the file from your computer
3. The tool will map each workflow to its SLA limit

---

### C. SOW / Contract Document *(Recommended)*

**What it is:** The signed Statement of Work or contract PDF/Word document.
**What the tool does with it:** Automatically extracts data volume commitments (DFU/SKU) and SLA terms.

**How to upload:**
1. Click **"Upload SOW Document"**
2. Select the PDF or Word file

---

Once all files are uploaded, the **Batch Analytics** and **SLA Matrix** panels will automatically populate with data — no further action needed.

---

## 4. Step 2 — Pull Azure VM Data Live

Click the **Resources** tab in the navigation.

This step pulls real-time CPU, memory, and disk metrics directly from Azure Monitor for the customer's servers.

### How to do it:

1. Click **"Add / Fetch Azure VMs"**
2. In the window that appears:
   - Enter the **Azure Subscription ID** for the customer's environment
   - Enter the **Resource Group** name, or leave it blank to scan all groups
   - Choose the **time range** — for example, last 24 hours or last 7 days
3. Click **"Fetch"**
4. The portal connects to Azure and pulls live metrics for all VMs
5. VMs appear in the table with **CPU %**, **Memory %**, and **Disk %** values

### If you want to add a specific VM manually:
- Use the **Quick-Add VM** bar at the bottom of the fetch window
- Type the VM hostname and press Enter

> **Note:** If you close and reopen the fetch window, your VMs are still there. You do not need to fetch again unless you want fresh data.

---

## 5. Step 3 — Review the SLA Matrix

Click the **SLA Matrix** tab.

This is the main compliance view. Every batch workflow is listed here with its SLA health status.

---

### How workflows are grouped — the 3 Tiers

| Tier | Badge | What it means |
|---|---|---|
| **Tier 1** | `T1 · Contract` | SLA comes directly from the signed contract or Ctrl-M time window |
| **Tier 2** | `T2 · SOW` | SLA was extracted from the uploaded SOW document |
| **Tier 3** | `T3 · Assumed` | No contractual SLA was found — the tool applies an assumed ceiling |

---

### What each column shows

| Column | What it means |
|---|---|
| **Workflow** | The job or workflow name, with its tier badge |
| **SLA (h)** | The agreed SLA limit in hours |
| **Measured Duration** | How long the job actually ran (from Ctrl-M data) |
| **Headroom** | Time left before the SLA is breached — shown as a mini progress bar |
| **Buffer %** | What percentage of the SLA window is unused |
| **Status** | OK (safe) / At Risk (close to limit) / Breaching (over the limit) |

---

### Changing the assumed SLA ceiling for Tier 3

- At the top of the SLA Matrix you will see a coloured banner: **"Assumed Ceiling: X.Xh"**
- Click any of the pill buttons to change it: `6.0h` `8.25h` `10.0h` `12.0h` or `Custom...`
- All Tier-3 workflow rows will **recalculate instantly** — no need to re-upload anything

This is useful when you know the customer expects a different ceiling than the default.

---

### Searching for a specific workflow

- Type in the **Search** box above the table
- The table filters in real time across all three tiers
- Clear the box to see all workflows again

---

### Summary numbers at the top

| Metric | What it tells you |
|---|---|
| **Compliance %** | Percentage of workflows currently within their SLA |
| **Total Runs** | Total number of workflows analysed |
| **Breaching** | How many workflows are over their SLA limit |
| **At Risk** | How many are within 10% of their limit |

---

## 6. Step 4 — Review Batch Analytics

Click the **Batch** tab.

This panel goes deeper into the execution patterns from the Ctrl-M data.

**What you will see:**

- **Top longest-running jobs** — sorted by actual duration, worst first
- **Jobs by status** — breakdown of Completed, Failed, Running, Aborted counts
- **Runtime distribution chart** — shows how job durations are spread
- **Worst-day analysis** — which dates had the highest batch load
- **Waterfall chart** — a visual timeline of jobs running across the batch window

**Use this panel to find:**
- Jobs that consistently run long and eat into SLA headroom
- Jobs that frequently fail or abort
- Days where the overall batch window was dangerously full

---

## 7. Step 5 — Review Resource Health

Click the **Resources** tab (after completing Step 2).

Each Azure VM is listed with its current health metrics:

| Metric | Green (OK) | Yellow (Warning) | Red (Critical) |
|---|---|---|---|
| **CPU %** | Below 75% | 75% to 90% | Above 90% |
| **Memory %** | Below 80% | 80% to 90% | Above 90% |
| **Disk %** | Below 75% | 75% to 85% | Above 85% |

- Click any VM row to expand it and see a **time-series chart** of how that metric changed over the selected period.
- VMs are labelled by their role (DB server, App server, etc.).
- An **Infrastructure Health Score** is shown at the top of the panel.

---

## 8. Step 6 — Review Findings and Red Flags

Click the **Findings** tab.

After the tool has processed all uploaded data and Azure metrics, it automatically generates a list of findings. You do not need to create these manually.

**Findings are generated for:**
- SLA breaches and workflows running at risk
- Servers with high CPU, memory, or disk usage
- Batch jobs with high failure or abort rates
- Data volume gaps compared to what is in the SOW
- Benchmark or UAT deviations

Each finding has a **severity level**: Critical / High / Medium / Low

---

### Red Flags

The **Red Flags** section highlights the most urgent issues that must be resolved before customer go-live. These are automatically ranked by the tool's scoring engine.

---

### Final Judgment

At the top of the Findings panel you will see the **Final Judgment card**:

| Verdict | Meaning |
|---|---|
| **GO** | All pillars passed — safe to proceed |
| **HOLD** | Some concerns — review before proceeding |
| **REMEDIATE** | Issues found — fixes required first |
| **BLOCKED** | Critical failures — cannot proceed |

This verdict is calculated automatically based on scores across all four pillars: Batch, Resource Health, SOW, and Benchmark.

---

## 9. Step 7 — AI Insights

The portal includes an AI engine that reads all the computed data and writes a plain-English narrative summary.

**How to use it:**
1. In the Findings or Batch panel, click **"Generate AI Analysis"**
2. Wait 30–60 seconds
3. A written summary appears — covering what the data shows, what the risks are, and what to investigate

> This does not change any numbers or findings. It is a written interpretation to help explain the analysis to stakeholders or include in communications.

---

## 10. Step 8 — Export and Save the Report

Once your analysis is complete, download a permanent copy.

**How to export:**
1. Click **"Export Report"** in the top-right corner or from the Findings panel
2. The portal generates a **self-contained HTML report file**
3. Your browser will download it automatically — save it to your machine or SharePoint

**The report includes:**
- Customer name and date of analysis
- Full SLA compliance results with all three tiers
- Resource health summary per server
- Complete findings list with severity
- Final Judgment verdict (GO / HOLD / REMEDIATE / BLOCKED)
- Governance sign-off checklist

> The report is a single HTML file — no internet connection needed to open it. You can email it directly to stakeholders or attach it to a ticket.

---

## 11. Viewing Past Reports (Archive)

Click the **Archive** tab.

Every report you have exported is saved here, organised by customer and date.

- Click any past entry to **reload that report's data** into the dashboard
- Use this to review what was analysed in a previous engagement
- Useful for tracking if a customer's SLA health has improved or worsened between visits

---

## 12. Settings

Click the **Settings** tab to adjust thresholds and defaults.

| Setting | What it controls |
|---|---|
| **Daily SLA Limit (hrs)** | The default assumed SLA ceiling applied to Tier-3 workflows |
| **CPU / Memory / Disk thresholds** | The values at which servers are flagged as Warning or Critical |
| **AI Provider** | Switch between Gemini and NVIDIA Gemma for AI Insights |

Changes apply immediately — no page refresh needed.

> **Note:** Settings changes affect all customers on this portal. Coordinate with the team before changing thresholds.

---

## 13. FAQ

**Q: I uploaded a file but the SLA Matrix or Batch table is empty.**
A: Check that your Ctrl-M export has the standard column headers (job name, start time, end time, status). If the file is from a non-standard Ctrl-M configuration or the columns are renamed, the parser may not recognise it. Contact the PE team with a sample row.

**Q: The Azure fetch is failing or showing no VMs.**
A: Your Azure account needs at least **Reader** access on the subscription. Double-check the Subscription ID — a typo here is the most common cause. Also confirm you are on VPN.

**Q: I changed the Tier-3 ceiling but the numbers did not change.**
A: Refresh the page, re-select the customer, and try again. If it still does not update, re-upload the SLA file.

**Q: Can two engineers use the same customer session at the same time?**
A: Not recommended. The portal stores one session per customer. If two people upload files or fetch Azure data for the same customer simultaneously, the last action will overwrite the previous one.

**Q: I cannot see the AI Insights button.**
A: AI Insights may be disabled on this deployment. Contact your portal administrator to enable it.

**Q: I exported a report — where is it saved?**
A: It downloads to your browser's default downloads folder. Move it to SharePoint or your engagement folder for safekeeping.

**Q: How do I do analysis for a new customer?**
A: Select the new customer from the Customer Selector at the top. Then start from Step 1 — upload fresh Ctrl-M and SLA files for that customer.

---

*For technical issues or to request new features, contact the PE Engineering team.*
