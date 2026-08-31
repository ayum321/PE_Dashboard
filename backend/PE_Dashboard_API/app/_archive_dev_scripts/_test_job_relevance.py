"""Guards the product-job filter and the cadence inference in the export report.

The job names below are the real ones from the customer export that prompted
this work: 12 of 20 rows were filed as "Other" because `schedule_type` was
absent and nothing read the name, and three database-backup jobs sat in a table
that is meant to describe product batch work.

Run from `backend/PE_Dashboard_API/app`:  python _test_job_relevance.py
"""
from __future__ import annotations

import sys

from routers.export import (  # noqa: E402
    _job_attention,
    _job_cadence,
    _job_exclusion_reason,
    _split_jobs,
    _top_rows,
)

FAILED: list[str] = []


def check(label: str, got: object, want: object) -> None:
    if got != want:
        FAILED.append(f"{label}: expected {want!r}, got {got!r}")


# ── Cadence is inferred from the name when schedule_type is absent ──────────
for name, expected in [
    ("JDA_PROCESSING_JOB_WKLY_2", "WEEKLY"),
    ("JDA_PROCESSING_JOB_QTRLY_2", "QUARTERLY"),
    ("JDA_PROCESSING_DLY_2", "DAILY"),
    ("JDA_PROCESSING_DLY_1", "DAILY"),
    ("DB_Backup_D", "DAILY"),
    ("DB_Backup_W", "WEEKLY"),
    ("DB_Backup_M", "MONTHLY"),
    ("SOME_NIGHTLY_LOAD", "DAILY"),
    ("PLAIN_JOB_NAME", "OTHER"),
]:
    check(f"cadence({name})", _job_cadence({"Job_Name": name}), expected)

# An explicit schedule_type still wins over the name.
check("explicit schedule_type wins",
      _job_cadence({"Job_Name": "DB_Backup_W", "schedule_type": "DAILY"}), "DAILY")


# ── Housekeeping families are set aside; product work is kept ───────────────
for name in ["DB_Backup_D", "DB_Backup_W", "DB_Backup_M",
             "W_IS_FILE_WATCHER_INBOUND_Daily", "CTRL_M_FILE_WATCHER_W",
             "SCPO_BATCH_START", "ZABBIX_MONITORS_DISABLE",
             "GATHER_DB_STATS_D", "ARCHIVE_LOG_CLEANUP"]:
    if not _job_exclusion_reason({"Job_Name": name}):
        FAILED.append(f"{name} should have been set aside as housekeeping")

for name in ["JDA_PROCESSING_JOB_WKLY_2", "JDA_PROCESSING_DLY_1",
             "SCPO_FORECAST_PUBLISH_D", "DEMAND_CLASSIFICATION_W"]:
    reason = _job_exclusion_reason({"Job_Name": name})
    if reason:
        FAILED.append(f"{name} is product work but was set aside as {reason!r}")

check("backup family label",
      _job_exclusion_reason({"Job_Name": "DB_Backup_D"}), "Backup / restore")
check("watcher family label",
      _job_exclusion_reason({"Job_Name": "CTRL_M_FILE_WATCHER_W"}), "File watcher / listener")


# ── The split reproduces the customer's real table ──────────────────────────
sample = [
    {"Job_Name": "JDA_PROCESSING_JOB_WKLY_2", "peak_hrs": 8.133, "avg_hrs": 6.118,
     "sla_hrs": 13.0, "buffer_pct": 37.4, "buffer_status": "LONG_JOB"},
    {"Job_Name": "JDA_PROCESSING_JOB_QTRLY_2", "peak_hrs": 5.057, "avg_hrs": 5.057,
     "sla_hrs": 13.0, "buffer_pct": 61.1, "buffer_status": "OK"},
    {"Job_Name": "JDA_PROCESSING_DLY_2", "peak_hrs": 2.613, "avg_hrs": 2.220,
     "sla_hrs": 11.0, "buffer_pct": 76.2, "buffer_status": "OK"},
    {"Job_Name": "DB_Backup_D", "peak_hrs": 1.384, "avg_hrs": 0.896,
     "sla_hrs": 11.0, "buffer_pct": 87.4, "buffer_status": "OK"},
    {"Job_Name": "JDA_PROCESSING_DLY_1", "peak_hrs": 1.013, "avg_hrs": 0.239,
     "sla_hrs": 11.0, "buffer_pct": 90.8, "buffer_status": "OK"},
    {"Job_Name": "DB_Backup_W", "peak_hrs": 0.969, "avg_hrs": 0.879,
     "sla_hrs": 13.0, "buffer_pct": 92.5, "buffer_status": "OK"},
    {"Job_Name": "DB_Backup_M", "peak_hrs": 0.969, "avg_hrs": 0.969,
     "sla_hrs": 13.0, "buffer_pct": 92.5, "buffer_status": "OK"},
]
product, setaside = _split_jobs(sample)
check("product job count", len(product), 4)
check("set-aside job count", len(setaside), 3)
check("all set-aside are backups",
      sorted({r["_excluded_reason"] for r in setaside}), ["Backup / restore"])

html = _top_rows(sample)
for gone in ("DB_Backup_D", "DB_Backup_W", "DB_Backup_M"):
    if gone in html:
        FAILED.append(f"{gone} still rendered into the product job table")
if 'data-cadence="OTHER"' in html:
    FAILED.append("a product row is still unclassified after cadence inference")
for want in ('data-cadence="WEEKLY"', 'data-cadence="QUARTERLY"', 'data-cadence="DAILY"'):
    if want not in html:
        FAILED.append(f"missing {want} in the rendered rows")
if 'data-attention=' not in html:
    FAILED.append("rows carry no attention flag for the 'Needs attention' tab")


# ── Attention flags ────────────────────────────────────────────────────────
check("breach is flagged",
      _job_attention({"peak_hrs": 12.0, "avg_hrs": 11.0}, "BREACH"), "SLA pressure")
check("volatile runtime is flagged",
      _job_attention({"peak_hrs": 1.013, "avg_hrs": 0.239}, "OK"), "Volatile runtime")
check("steady healthy job is not flagged",
      _job_attention({"peak_hrs": 5.057, "avg_hrs": 5.057}, "OK"), "")


if FAILED:
    print("job relevance checks FAILED:")
    for line in FAILED:
        print("  -", line)
    sys.exit(1)
print("job relevance + cadence inference checks passed")
