# PE Dashboard — User Guide

> A step-by-step walkthrough for Performance Engineers.
> No prior knowledge of the code is needed — just follow each section in order.

---

## Table of Contents

1. [Opening the Dashboard](#1-opening-the-dashboard)
2. [Selecting a Customer](#2-selecting-a-customer)
3. [Step 1 — Upload Your Files](#3-step-1--upload-your-files)
4. [Step 2 — Pull Azure VM Data](#4-step-2--pull-azure-vm-data)
5. [Step 3 — Review the SLA Matrix](#5-step-3--review-the-sla-matrix)
6. [Step 4 — Review Batch Analytics](#6-step-4--review-batch-analytics)
7. [Step 5 — Review Resource Health](#7-step-5--review-resource-health)
8. [Step 6 — Review Findings & Red Flags](#8-step-6--review-findings--red-flags)
9. [Step 7 — AI Insights (Optional)](#9-step-7--ai-insights-optional)
10. [Step 8 — Export & Save the Report](#10-step-8--export--save-the-report)
11. [Viewing Past Reports (Archive)](#11-viewing-past-reports-archive)
12. [Settings](#12-settings)
13. [FAQ](#13-faq)

---

## 1. Opening the Dashboard

**Fastest way (local setup):**

1. Double-click `start.bat` in the root folder of the project.
2. Wait ~30 seconds — it will start both the API and the UI automatically.
3. Your browser will open at `http://127.0.0.1:3000`.

**If it is hosted on a server:**
Go to the URL shared by your team (e.g. `https://<your-host>/pe-dashboard`).

---

## 2. Selecting a Customer

- At the top of the dashboard you will see a **Customer Selector** dropdown.
- Select the customer you are doing the engagement for.
- All panels — SLA Matrix, Batch, Resources, Findings — will update to show only that customer's data.
- You can switch customers at any time without losing data.

> **Tip:** Each customer's data is stored separately. Switching customers does not clear the other customer's uploaded files.

---

## 3. Step 1 — Upload Your Files

Go to the **Upload** tab.

You need to upload **two files** before the tool can do any analysis:

### A. Ctrl-M Execution File
- This is the Control-M batch execution export (CSV or Excel format).
- It contains all job run history — start times, end times, durations, statuses.
- Upload it using the **"Ctrl-M / Batch File"** upload button.

### B. Batch SLA Data
- This is the SLA matrix file — usually an Excel file with workflow names and their SLA time windows.
- It tells the tool what the agreed SLA limit is for each workflow.
- Upload it using the **"SLA / Batch Config File"** upload button.

### C. SOW / Contract Document (Optional but recommended)
- Upload the Statement of Work (PDF or Word document).
- The tool will extract data volume commitments (DFU/SKU) and SLA terms from it automatically.

Once both required files are uploaded, the tool will process them automatically. You will see the **Batch Analytics** and **SLA Matrix** panels populate with data within seconds.

---

## 4. Step 2 — Pull Azure VM Data

Go to the **Resources** tab.

This step connects to Azure and pulls live CPU, memory, and disk metrics for the customer's VMs.

### How to pull:

1. Click **"Add / Fetch Azure VMs"**.
2. In the modal that opens:
   - Enter your **Azure Subscription ID**.
   - Enter the **Resource Group** name (or leave blank to scan all).
   - Select the **time range** (e.g. last 24 hours, last 7 days).
3. Click **"Fetch"**.
4. The tool will connect to Azure Monitor and pull live metrics for all VMs in that subscription.
5. Once done, VMs will appear in the resource table with CPU %, Memory %, and Disk % columns.

### Adding VMs manually (Quick-Add):
If you already know the VM name and want to add it quickly:
- Use the **Quick-Add VM** bar at the bottom of the modal.
- Type the VM name and press Enter.

> **Note:** If you close the modal and reopen it, your previously fetched VMs are still there. You do not need to re-fetch.

---

## 5. Step 3 — Review the SLA Matrix

Go to the **SLA Matrix** tab.

This is the core compliance view. It shows every batch workflow grouped by SLA tier:

| Tier | What it means |
|---|---|
| **Tier 1 - Contract** | SLA is directly from the signed contract / Ctrl-M time window |
| **Tier 2 - SOW** | SLA is extracted from the Statement of Work document |
| **Tier 3 - Assumed** | No contractual SLA found — tool uses an assumed ceiling |

### What each column means:

| Column | Description |
|---|---|
| **Workflow** | Job / workflow name with its tier badge |
| **SLA (h)** | The SLA time limit in hours |
| **Measured Duration** | Actual run time from the Ctrl-M data |
| **Headroom** | Time remaining before the SLA is breached (SLA minus duration) |
| **Buffer %** | What % of the SLA window is still unused |
| **Status** | OK / At Risk / Breaching |

### Changing the assumed ceiling (Tier 3):
- At the top of the SLA Matrix panel you will see a banner: **"Assumed Ceiling: X.Xh"**
- Click any of the pill buttons — `6.0h`, `8.25h`, `10.0h`, `12.0h`, or `Custom...` — to change it.
- All Tier-3 rows will **recalculate instantly** without re-uploading any files.

### Searching workflows:
- Use the **Search** box above the table to filter by workflow name.
- Works across all three tiers simultaneously.

### Key metrics at the top:
- **Compliance %** — percentage of workflows within SLA
- **Total Runs** — total number of workflows analysed
- **Breaching** — count of workflows over SLA
- **At Risk** — count within 10% of their SLA limit

---

## 6. Step 4 — Review Batch Analytics

Go to the **Batch** tab.

This panel gives a deeper look at batch execution patterns from the Ctrl-M data.

- **Top longest-running jobs** — sorted by duration
- **Jobs by status** — Completed, Failed, Running, Aborted
- **Runtime distribution** — how job durations are spread
- **Worst-day analysis** — which days had the highest batch load
- **Waterfall chart** — job timeline across the batch window

Use this panel to identify jobs that are consistently slow, frequently failing, or running dangerously close to their SLA window.

---

## 7. Step 5 — Review Resource Health

Go to the **Resources** tab (after fetching Azure data in Step 2).

Each VM is shown with:

| Column | Threshold |
|---|---|
| **CPU %** | Green < 75%, Yellow 75-90%, Red > 90% |
| **Memory %** | Green < 80%, Yellow 80-90%, Red > 90% |
| **Disk %** | Green < 75%, Yellow 75-85%, Red > 85% |

- Click any VM row to expand and see a time-series chart of its metrics.
- VMs are colour-coded by role (DB server, App server, etc.).
- The panel shows an overall **Infrastructure Health Score**.

---

## 8. Step 6 — Review Findings & Red Flags

Go to the **Findings** tab.

The tool automatically generates findings based on everything it has analysed:

- SLA breaches and near-breaches
- High CPU / memory servers
- Jobs with high failure rates
- Data volume gaps vs. SOW commitments
- Benchmark deviations

Each finding has a **severity**: Critical / High / Medium / Low.

### Red Flags tab:
- Shows the most urgent items that need to be addressed before go-live.
- These are automatically prioritised by the tool's scoring engine.

### Final Judgment card:
- Shows the overall verdict: **GO / HOLD / BLOCKED / REMEDIATE**
- Based on the combined score across all four pillars (Batch, Resource, SOW, Benchmark).

---

## 9. Step 7 — AI Insights (Optional)

If AI is enabled on your deployment:

1. Go to the **AI Insights** section (available in the Findings or Batch panel).
2. Click **"Generate AI Analysis"**.
3. The tool sends all the computed data to the AI engine (Gemini / NVIDIA Gemma).
4. Within 30-60 seconds, a written narrative appears — summarising what the data shows, what the risks are, and what to investigate next.

> This does not change any numbers. It just writes a human-readable summary of what the tool found.

---

## 10. Step 8 — Export & Save the Report

Once your analysis is complete:

1. Click **"Export Report"** (usually in the top-right or Findings panel).
2. The tool generates a **self-contained HTML report** — all data, charts, and findings are embedded in a single file.
3. Download and save it.

The report includes:
- Customer name and engagement date
- All SLA compliance results
- Resource health summary
- Findings list with severity
- Final judgment verdict
- Governance sign-off checklist

> The HTML file is self-contained — no internet connection needed to open it. Safe to email to customers or store in SharePoint.

---

## 11. Viewing Past Reports (Archive)

Go to the **Archive** tab.

- All previously exported reports are listed here by customer and date.
- Click any entry to reload that report's data into the dashboard.
- Use this to compare what was analysed in previous engagements.

---

## 12. Settings

Go to the **Settings** tab to configure:

| Setting | What it controls |
|---|---|
| **Daily SLA Limit (hrs)** | Default assumed SLA ceiling for Tier-3 workflows |
| **CPU / Memory / Disk thresholds** | When a server is flagged as Warning vs. Critical |
| **AI Provider** | Switch between Gemini and NVIDIA Gemma |
| **Cookie / session settings** | For multi-user deployments |

Changes take effect immediately without restarting the tool.

---

## 13. FAQ

**Q: I uploaded a file but the table is empty — what's wrong?**
A: Check that the Ctrl-M file has the expected column headers (job name, start time, end time, status). The tool expects a standard Ctrl-M export format. If columns are missing or renamed, contact the PE team.

**Q: The Azure fetch is failing — what do I check?**
A: Make sure you are logged in with an Azure account that has at least **Reader** role on the subscription. Also verify the Subscription ID is correct.

**Q: I changed the assumed ceiling but the numbers did not update.**
A: Refresh the page and re-select the customer. If the issue persists, re-upload the SLA file.

**Q: Can two engineers work on the same customer at the same time?**
A: Not recommended — the tool stores one session per customer. The last person to upload files or fetch Azure data will overwrite the previous state.

**Q: Where are the saved reports stored on the server?**
A: In the `PE_STATE_DIR` volume (configured in `docker-compose.yml`, defaults to `/data` inside the container). On a local setup, check the `backend/PE_Dashboard_API/data/` folder.

**Q: The AI Insights button is not visible.**
A: AI is disabled by default. Ask your system administrator to set `AI_ENABLED=true` in the environment configuration.

---

*For technical issues or feature requests, contact the PE Engineering team.*
