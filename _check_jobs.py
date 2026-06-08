import pandas as pd

for name, path in [
    ("Distell", r"C:\Users\1039081\Downloads\Work\Batch-SLA-Reports\Last_30_Days_Report_of_CS_DISTELL_SCPO_2022_SUPPLY_TEST.csv"),
    ("AZSPA", r"C:\Users\1039081\Downloads\Work\Batch-SLA-Reports\Last_30_Days_Report_of_CS_AZSPA_SCPO_PROD.csv"),
]:
    df = pd.read_csv(path, encoding="utf-8-sig")
    df.columns = [c.strip() for c in df.columns]
    # What does pd.nunique count?
    raw_nunique = df["Job_Name"].nunique()
    # Stripped?
    stripped = df["Job_Name"].str.strip().nunique()
    # Upper case?
    upper = df["Job_Name"].str.strip().str.upper().nunique()
    # Check for trailing spaces or special chars
    samples = df["Job_Name"].unique()
    with_spaces = [j for j in samples if j != j.strip()]
    print(f"{name}: raw_nunique={raw_nunique}, stripped={stripped}, upper={upper}, with_trailing_spaces={len(with_spaces)}")
    if with_spaces:
        print(f"  Examples with spaces: {with_spaces[:5]}")
