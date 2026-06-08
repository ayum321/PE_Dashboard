import pandas as pd
import sys
sys.path.insert(0, r"c:\Users\1039081\Downloads\PE_Dashboard")
from services.batch_calculator import _parse_dt

df = pd.read_csv(r"C:\Users\1039081\Downloads\Work\Batch-SLA-Reports\Last_30_Days_Report_of_CS_TATA_STEEL_SCPO_OP_PROD.csv")
df.columns = [c.strip() for c in df.columns]

df["Start_Time"] = _parse_dt(df["Start_Time"])
df["End_Time"] = _parse_dt(df["End_Time"])
df["run_date"] = df["Start_Time"].dt.date

# Check the daily elapsed (min start, max end)
daily = df.dropna(subset=["Start_Time", "End_Time"]).groupby("run_date").agg(
    first_start=("Start_Time", "min"),
    last_end=("End_Time", "max"),
)
daily["elapsed_hrs"] = (daily["last_end"] - daily["first_start"]).dt.total_seconds() / 3600
daily["breach"] = daily["elapsed_hrs"] > 6.0

print(f"Total days: {len(daily)}")
print(f"Breach days: {daily['breach'].sum()}")
print(f"Window compliance: {(1 - daily['breach'].sum()/len(daily))*100:.1f}%")
print(f"\nSample elapsed (sorted desc):")
for d, r in daily.nlargest(10, "elapsed_hrs").iterrows():
    print(f"  {d}: {r['elapsed_hrs']:.2f}h  start={r['first_start']}  end={r['last_end']}  breach={r['breach']}")

print(f"\nSample NON-breach days:")
for d, r in daily[~daily["breach"]].head(5).iterrows():
    print(f"  {d}: {r['elapsed_hrs']:.2f}h")

# Check Sub_Application values
print(f"\nSub_Applications: {df['Sub_Application'].nunique()}")
print(df["Sub_Application"].value_counts().head(10))

# Check cyclic subs
from services.batch_calculator import detect_cyclic_subs
cyclic = detect_cyclic_subs(df)
print(f"\nCyclic subs detected: {cyclic}")
