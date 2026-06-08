import pandas as pd
csv = r"c:\Users\1039081\Downloads\Michilen_PE\PE signoff\Last_15_Days_Report_of_CS_MICHELIN_SCPO_FF_2025_PROD.csv"
df = pd.read_csv(csv)
rc = "Run Time (Sec.) "

# Show all DMS_FILEWATCHER_DATA_REQ runs
fw = df[df["Job_Name"].str.contains("FILEWATCHER", na=False)]
print("ALL FILEWATCHER RUNS:")
for _, r in fw.iterrows():
    hrs = float(r[rc]) / 3600
    jn = r["Job_Name"]
    status = r["Completion Status"]
    start = str(r["Start_Time"])[:19]
    print(f"  {jn:40s} {hrs:.3f}h ({float(r[rc]):.0f}s)  {status}  {start}")

print()
sg = df[df["Job_Name"].str.contains("GENSDLINKS", na=False)]
print("ALL SCPO_GENSDLINKS RUNS:")
for _, r in sg.iterrows():
    hrs = float(r[rc]) / 3600
    jn = r["Job_Name"]
    status = r["Completion Status"]
    start = str(r["Start_Time"])[:19]
    print(f"  {jn:40s} {hrs:.3f}h ({float(r[rc]):.0f}s)  {status}  {start}")
