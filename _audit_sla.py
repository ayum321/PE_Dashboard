"""Audit all SLA files against the current SLA parser/engine."""
import glob, os, sys, warnings, traceback
warnings.filterwarnings("ignore")

import pandas as pd
sys.path.insert(0, "C:/Users/1039081/Downloads/PE_Dashboard")

from services.sla_engine import ingest_sla_file
from services.sla_parser import extract_sla_from_xlsx

SLA_FILES = [
    r"C:\Users\1039081\Downloads\Work\Batch-SLA-Reports\Batch_Plan__SLA_Check_with__30m_Contingency.csv",
    r"C:\Users\1039081\Downloads\Work\Batch-SLA-Reports\Batch_SLA_HaleonUK.xlsx",
    r"C:\Users\1039081\Downloads\Work\Batch-SLA-Reports\Batch_SLA_Info_THS_IO.xlsx",
    r"C:\Users\1039081\Downloads\Work\Batch-SLA-Reports\BatchSLA&performance_info 1 (1).xlsx",
    r"C:\Users\1039081\Downloads\Work\Batch-SLA-Reports\BatchSLA&performance_info 1.xlsx",
    r"C:\Users\1039081\Downloads\Work\Batch-SLA-Reports\BatchSLA_info(1).xlsx",
    r"C:\Users\1039081\Downloads\Work\Batch-SLA-Reports\BatchSLA_info.xlsx",
]

print("=" * 90)
print("  SLA FILE AUDIT")
print("=" * 90)

for fpath in SLA_FILES:
    fname = os.path.basename(fpath)
    raw = open(fpath, "rb").read()

    print(f"\n{'─'*90}")
    print(f"  FILE: {fname}")

    # ── 1. Raw structure ──
    try:
        if fpath.endswith(".csv"):
            df_raw = pd.read_csv(fpath, nrows=10, encoding="latin-1")
        else:
            df_raw = pd.read_excel(fpath, sheet_name=0, nrows=10)
        print(f"  RAW COLS  : {list(df_raw.columns)}")
        print(f"  SHAPE     : {df_raw.shape}")
        for i, row in df_raw.head(3).iterrows():
            print(f"  ROW {i}     : {row.tolist()}")
    except Exception as e:
        print(f"  RAW READ FAIL: {e}")

    # ── 2. sla_engine ingest ──
    print()
    try:
        result = ingest_sla_file(raw, fname)
        print(f"  ENGINE RESULT:")
        print(f"    valid_rows       = {result.valid_rows}")
        print(f"    ceilings         = {result.ceilings}")
        print(f"    missing_ceilings = {result.missing_ceilings}")
        print(f"    contracts (first 5):")
        for c in result.contracts[:5]:
            print(f"      {c}")
        if result.warnings:
            print(f"    warnings: {result.warnings[:3]}")
    except Exception as e:
        print(f"  ENGINE ERROR: {e}")
        traceback.print_exc()

    # ── 3. sla_parser extract ──
    print()
    try:
        ceilings = extract_sla_from_xlsx(raw)
        print(f"  PARSER CEILINGS: {ceilings}")
    except Exception as e:
        print(f"  PARSER ERROR: {e}")

print("\n" + "=" * 90)
print("  DONE")
print("=" * 90)
