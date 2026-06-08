import pandas as pd
import sys
sys.path.insert(0, r"c:\Users\1039081\Downloads\PE_Dashboard")
from services.batch_calculator import _parse_dt, detect_cyclic_subs

path = r"C:\Users\1039081\Downloads\Work\Batch-SLA-Reports\Last_15_Days_Report_of_CS_HNK_HNS_2022_SCPO_ESP_TEST.csv"
df = pd.read_csv(path, encoding="utf-8-sig")
df.columns = [c.strip() for c in df.columns]
print("Columns:", df.columns.tolist())
print(f"Start samples: {df['Start_Time'].head(3).tolist()}")
print(f"End samples: {df['End_Time'].head(3).tolist()}")

df["Start_Time"] = _parse_dt(df["Start_Time"])
df["End_Time"] = _parse_dt(df["End_Time"])
print(f"\nParsed Start_Time: {df['Start_Time'].notna().sum()}/{len(df)}")
print(f"Parsed End_Time: {df['End_Time'].notna().sum()}/{len(df)}")

df["run_date"] = df["Start_Time"].dt.date
print(f"\nDate range: {df['run_date'].min()} .. {df['run_date'].max()}")
print(f"Unique dates: {df['run_date'].nunique()}")

# Sub apps
print(f"\nSub apps: {df['Sub_Application'].value_counts().to_dict()}")

# Cyclic detection
cyclic = detect_cyclic_subs(df)
print(f"Cyclic subs: {cyclic}")

# Window computation matching dashboard
df_window = df[~df["Sub_Application"].isin(cyclic)] if cyclic else df

# All dates
window_all = df.groupby("run_date", as_index=False).agg(
    total_hrs=("run_time_hrs", "sum") if "run_time_hrs" in df.columns else ("Start_Time", "count"))

# Parse run_time_hrs 
if "Run_Sec" in df.columns or "Run Time (Sec.)" in df.columns:
    rt_col = "Run_Sec" if "Run_Sec" in df.columns else "Run Time (Sec.)"
    df["run_time_hrs"] = pd.to_numeric(df[rt_col], errors="coerce").fillna(0) / 3600

elap = df_window.groupby("run_date").agg(
    first_start=("Start_Time", "min"),
    last_end=("End_Time", "max")
).dropna()
elap["elapsed_hrs"] = ((elap["last_end"] - elap["first_start"]).dt.total_seconds() / 3600).clip(lower=0)

print(f"\nElapsed per day (no cyclic exclusion):")
for d, r in elap.iterrows():
    breach = "BREACH" if r["elapsed_hrs"] > 6.0 else "ok"
    print(f"  {d}: {r['elapsed_hrs']:.2f}h  ({r['first_start']} - {r['last_end']})  {breach}")
