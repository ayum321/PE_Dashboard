import pandas as pd

df = pd.read_csv(r"C:\Users\1039081\Downloads\Work\Batch-SLA-Reports\Last_30_Days_Report_of_CS_TATA_STEEL_SCPO_OP_PROD.csv")
df.columns = [c.strip() for c in df.columns]
s = df["Start_Time"].astype(str)
print(f"Rows: {len(s)}")
print(f"Samples: {s.head(3).tolist()}")

fmts = [
    "%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d",
    "%d-%m-%Y %H:%M", "%d-%m-%Y %H:%M:%S",
    "%m/%d/%Y %H:%M", "%m/%d/%Y %H:%M:%S", "%m/%d/%Y",
    "%d/%m/%Y %H:%M", "%d/%m/%Y %H:%M:%S",
]
for fmt in fmts:
    try:
        p = pd.to_datetime(s, format=fmt, errors="coerce")
        n = p.notna().sum()
        pct = n / len(s) * 100
        hit = "<<< PICKED (>40%)" if pct > 40 else ""
        print(f"  {fmt:25s} -> {n}/{len(s)} ({pct:.1f}%) {hit}")
    except Exception as e:
        print(f"  {fmt:25s} -> ERROR: {e}")

# Check the m/d/Y parse date range
p_us = pd.to_datetime(s, format="%m/%d/%Y %H:%M", errors="coerce")
print(f"\nm/d/Y date range: {p_us.min()} .. {p_us.max()}")
p_eu = pd.to_datetime(s, format="%d/%m/%Y %H:%M", errors="coerce")
good = p_eu.dropna()
print(f"d/m/Y date range: {good.min()} .. {good.max()}")
