"""
Audit every Ctrl-M file in Batch-SLA-Reports against the current parser.
Run with: python _audit_parsers.py
"""
import glob, os, sys, traceback
import pandas as pd

sys.path.insert(0, "C:/Users/1039081/Downloads/PE_Dashboard")
from services.batch_calculator import load_ctrlm_bytes

FOLDER = "C:/Users/1039081/Downloads/Work/Batch-SLA-Reports"

CTRLM_FILES = (
    glob.glob(f"{FOLDER}/Last_*_Days_Report_of_CS_*.csv") +
    glob.glob(f"{FOLDER}/30_Days_Report_of_CS_*.csv") +
    glob.glob(f"{FOLDER}/GKK_PROD_CONTROLM_REPORT.xlsx") +
    glob.glob(f"{FOLDER}/BATCH_RUN_TIME.xlsx") +
    glob.glob(f"{FOLDER}/HNK_HIE_SCPO_LDE_2022_MONTHLY_REPORT.csv") +
    glob.glob(f"{FOLDER}/Micron_Prod.csv") +
    glob.glob(f"{FOLDER}/Micron_test.csv") +
    glob.glob(f"{FOLDER}/ph_batch_report*.csv") +
    glob.glob(f"{FOLDER}/DRp_and_SEQ*.csv") +
    glob.glob(f"{FOLDER}/Under_Armour_CtrlM.xlsx") +
    glob.glob(f"{FOLDER}/ControlM_BatchRun_Report.xlsx") +
    glob.glob(f"{FOLDER}/UAT Batch Report.xlsx") +
    glob.glob(f"{FOLDER}/UAT Batch Report(1).xlsx") +
    glob.glob(f"{FOLDER}/Batch TEST report.xlsx") +
    glob.glob(f"{FOLDER}/Ctrl-M_Daily_Summary*.csv") +
    glob.glob(f"{FOLDER}/ctrlm_weekly*.csv") +
    glob.glob(f"{FOLDER}/Daily_Job_Runtime_Summary*.csv") +
    glob.glob(f"{FOLDER}/ONSEMI_daily_summary.csv") +
    glob.glob(f"{FOLDER}/Yokhama_ControlM.csv") +
    glob.glob(f"{FOLDER}/Daily_Activity_Summary.csv") +
    glob.glob(f"{FOLDER}/Daily_Event_and_Runtime_Summary.csv") +
    glob.glob(f"{FOLDER}/CCH_MPWB.csv") +
    glob.glob(f"{FOLDER}/pe_summary.csv")
)

REQUIRED_COLS = ["Job_Name", "Run_Sec", "Start_Time", "Sub_Application"]
NEEDED = {"job": "Job_Name", "sec": "Run_Sec", "start": "Start_Time", "sub": "Sub_Application"}

results = []
for fpath in sorted(set(CTRLM_FILES)):
    fname = os.path.basename(fpath)
    raw = open(fpath, "rb").read()
    try:
        df = load_ctrlm_bytes(raw, fname)
        has = {k: col in df.columns and (
                   df[col].dropna().astype(str).str.strip().str.len().max() or 0) > 0
               for k, col in NEEDED.items()}
        # special check: Run_Sec must have numeric > 0
        if "Run_Sec" in df.columns:
            has["sec"] = pd.to_numeric(df["Run_Sec"], errors="coerce").max(skipna=True) > 0
        score = sum(has.values())
        missing = [k for k, v in has.items() if not v]
        status = "FULL" if score == 4 else f"PARTIAL({score}/4)"

        # Check if job names look real (not all 'UNKNOWN')
        job_quality = "?"
        if "Job_Name" in df.columns:
            unique_jobs = df["Job_Name"].dropna().unique()
            job_quality = f"{len(unique_jobs)} unique" if "UNKNOWN" not in unique_jobs else "ALL_UNKNOWN"

        # Check runtime range
        rt_max = 0
        if "Run_Sec" in df.columns:
            _rt = pd.to_numeric(df["Run_Sec"], errors="coerce").max(skipna=True)
            rt_max = float(_rt) if pd.notna(_rt) else 0

        results.append({
            "file": fname[:55],
            "status": status,
            "rows": len(df),
            "cols": list(df.columns[:8]),
            "missing": missing,
            "jobs": job_quality,
            "max_sec": int(rt_max),
        })
    except Exception as e:
        results.append({
            "file": fname[:55],
            "status": "ERROR",
            "rows": 0,
            "cols": [],
            "missing": [],
            "jobs": "—",
            "max_sec": 0,
            "error": traceback.format_exc(limit=3).split("\n")[-2],
        })

print("=" * 80)
print("  CTRL-M FILE PARSER AUDIT")
print("=" * 80)

full = [r for r in results if r["status"] == "FULL"]
partial = [r for r in results if "PARTIAL" in r["status"]]
errors = [r for r in results if r["status"] == "ERROR"]

print(f"\n  {len(full)}/{len(results)} FULL  |  {len(partial)} PARTIAL  |  {len(errors)} ERROR\n")

for r in results:
    flag = "OK" if r["status"] == "FULL" else ("ER" if r["status"] == "ERROR" else "~~")
    print(f"  {flag} {r['status']:<15} {r['rows']:>5}r  {r['file']}")
    if r["missing"]:
        print(f"             Missing: {r['missing']}   Jobs: {r['jobs']}   MaxSec: {r['max_sec']}")
    if r.get("error"):
        print(f"             ERROR: {r['error']}")

# ── Column vocabulary catalogue ──────────────────────────────────
print("\n" + "=" * 80)
print("  COLUMN VOCABULARY FOUND IN FILES (that need mapping to our canonical names)")
print("=" * 80)

vocab = {}
for fpath in sorted(set(CTRLM_FILES)):
    fname = os.path.basename(fpath)
    try:
        raw = open(fpath, "rb").read()
        if fname.endswith(".csv"):
            try: df_raw = pd.read_csv(fpath, nrows=1, encoding="utf-8", on_bad_lines="skip")
            except: df_raw = pd.read_csv(fpath, nrows=1, encoding="latin-1", on_bad_lines="skip")
        else:
            df_raw = pd.read_excel(fpath, nrows=1, engine="openpyxl" if fname.endswith(".xlsx") else "xlrd")
        for col in df_raw.columns:
            c = str(col).strip()
            if c and not c.startswith("Unnamed"):
                vocab.setdefault(c, []).append(fname[:30])
    except Exception:
        pass

# Group by semantic meaning
SEMANTIC = {
    "JOB/PROCESS name": lambda c: any(x in c.lower() for x in ["job", "process", "task", "script", "order"]),
    "START time":       lambda c: "start" in c.lower(),
    "END time":         lambda c: "end" in c.lower() or "finish" in c.lower(),
    "STATUS":           lambda c: any(x in c.lower() for x in ["status", "completion", "state"]),
    "RUNTIME (sec)":    lambda c: any(x in c.lower() for x in ["run", "sec", "dur", "elap", "time"]) and "start" not in c.lower() and "end" not in c.lower() and "name" not in c.lower(),
    "SUB_APP/Module":   lambda c: any(x in c.lower() for x in ["sub", "module", "stream", "component", "app"]),
    "DATE column":      lambda c: c.lower() in ("date", "run_date") or (c.lower().startswith("date") and len(c) < 8),
}

for label, fn in SEMANTIC.items():
    cols = [(c, fnames) for c, fnames in vocab.items() if fn(c)]
    if cols:
        print(f"\n  [{label}]")
        for c, fnames in sorted(cols, key=lambda x: x[0].lower()):
            print(f"    {c!r:<40} → {fnames[0]}")
