import pandas as pd
import sys
sys.path.insert(0, r"c:\Users\1039081\Downloads\PE_Dashboard")
from services.batch_calculator import _parse_dt, detect_cyclic_subs

for name, path in [
    ("Henkel", r"C:\Users\1039081\Downloads\Work\Batch-SLA-Reports\Last_15_Days_Report_of_CS_HNK_HNS_2022_SCPO_ESP_TEST.csv"),
    ("BJS", r"C:\Users\1039081\Downloads\Work\Batch-SLA-Reports\Last_30_Days_Report_of_CS_BJS_SCPO_DMS_LDE_TEST(in).csv"),
    ("Tata", r"C:\Users\1039081\Downloads\Work\Batch-SLA-Reports\Last_30_Days_Report_of_CS_TATA_STEEL_SCPO_OP_PROD.csv"),
    ("Haleon", r"C:\Users\1039081\Downloads\Work\Batch-SLA-Reports\Last_15_Days_Report_of_CS_HALEON_SCPO_2025_TEST.csv"),
]:
    df = pd.read_csv(path, encoding="utf-8-sig")
    df.columns = [c.strip() for c in df.columns]
    df["Start_Time"] = _parse_dt(df["Start_Time"])
    df["End_Time"] = _parse_dt(df["End_Time"])
    df["run_date"] = df["Start_Time"].dt.date

    # Detect cyclic subs
    cyclic = detect_cyclic_subs(df)
    df_window = df[~df["Sub_Application"].isin(cyclic)] if cyclic else df

    # Compute elapsed
    elap = (df_window.groupby("run_date")
              .agg(first_start=("Start_Time", "min"),
                   last_end=("End_Time", "max"))
              .dropna())
    elap["elapsed_hrs"] = ((elap["last_end"] - elap["first_start"]).dt.total_seconds() / 3600).clip(lower=0)
    breach_days = int((elap["elapsed_hrs"] > 6.0).sum())
    total_days = len(elap)
    window_comp = round((1 - breach_days / max(1, total_days)) * 100, 1)

    print(f"{name}: rows={len(df)}, parsed={df['Start_Time'].notna().sum()}, "
          f"cyclic={cyclic}, total_days={total_days}, breach_days={breach_days}, "
          f"window_comp={window_comp}%")
    # Show top 5 breach days
    breaches = elap[elap["elapsed_hrs"] > 6.0].nlargest(3, "elapsed_hrs")
    for d, r in breaches.iterrows():
        print(f"  {d}: {r['elapsed_hrs']:.1f}h ({r['first_start']} - {r['last_end']})")
    print()
