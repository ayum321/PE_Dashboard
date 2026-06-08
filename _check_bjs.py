import pandas as pd

df = pd.read_csv(r"C:\Users\1039081\Downloads\Work\Batch-SLA-Reports\Last_30_Days_Report_of_CS_BJS_SCPO_DMS_LDE_TEST(in).csv")
df.columns = [c.strip() for c in df.columns]
s = df["Start_Time"].astype(str)

# Check samples of failing rows
p = pd.to_datetime(s, dayfirst=True, errors="coerce")
failed = s[p.isna()]
print(f"Total: {len(s)}, Parsed: {p.notna().sum()}, Failed: {len(failed)}")
print(f"\nFailed samples:")
for v in failed.head(10):
    print(f"  [{v}]")

# Check: do ALL non-parsing have slashes?
has_slash = failed.str.contains("/")
has_dash = failed.str.contains("-")
print(f"\nFailed with slash: {has_slash.sum()}, with dash: {has_dash.sum()}")

# Try parsing failed ones separately
failed_parsed = pd.to_datetime(failed, format="%d/%m/%Y %H:%M", errors="coerce")
print(f"\nFailed parsed as d/m/Y: {failed_parsed.notna().sum()}/{len(failed)}")

# Check if the issue is the column has a leading space
print(f"\nColumn name: [{df.columns[4]}]")
print(f"First value repr: {repr(df.iloc[0, 4])}")

# Check end_time column too
end_s = df["End_Time"].astype(str)
print(f"\nEnd_Time samples:")
for v in end_s.head(5):
    print(f"  [{v}]")
