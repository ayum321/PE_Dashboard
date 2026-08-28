"""Direct regression checks for local report archive replacement and delivery.

Run:  py -3.14 _test_report_archive.py
"""
from __future__ import annotations

import shutil
import sqlite3
import tempfile
import re
import json
import subprocess
from pathlib import Path

from fastapi import FastAPI
from fastapi.testclient import TestClient

from routers import export as export_router
from routers.archive import router as archive_router
from routers.export import _checklist_rows
from services import report_archive


def _fail(message: str) -> None:
    raise AssertionError(message)


def _assert(condition: bool, message: str) -> None:
    if not condition:
        _fail(message)


def _isolate_archive() -> tuple[Path, tuple[Path, Path, Path, object]]:
    root = Path(tempfile.mkdtemp(prefix="pe-report-archive-"))
    original = (
        report_archive._ROOT,
        report_archive._DB_PATH,
        report_archive._FILES_DIR,
        report_archive._conn,
    )
    report_archive._ROOT = root
    report_archive._DB_PATH = root / "archive.db"
    report_archive._FILES_DIR = root / "files"
    report_archive._conn = None
    return root, original


def _restore_archive(root: Path, original: tuple[Path, Path, Path, object]) -> None:
    if report_archive._conn is not None:
        report_archive._conn.close()
    (
        report_archive._ROOT,
        report_archive._DB_PATH,
        report_archive._FILES_DIR,
        report_archive._conn,
    ) = original
    shutil.rmtree(root)


def test_save_then_list_returns_one_customer() -> None:
    root, original = _isolate_archive()
    try:
        saved = report_archive.save("Acme / West", "<h1>first</h1>", {
            "generated_at": "2026-08-15T10:00:00+00:00",
            "env": "UAT",
            "checklist_mismatches": 1,
        })
        reports = report_archive.list_reports()
        _assert(saved == {"ok": True, "customer_slug": "acme-west"}, f"save: {saved}")
        _assert(len(reports) == 1, f"expected one archive row: {reports}")
        _assert(reports[0]["customer"] == "Acme / West", f"wrong customer: {reports}")
        print("  [OK] save/list returns one archived report")
    finally:
        _restore_archive(root, original)


def test_second_save_replaces_metadata_and_html() -> None:
    root, original = _isolate_archive()
    try:
        report_archive.save("Acme / West", "<h1>first</h1>", {
            "generated_at": "2026-08-15T10:00:00+00:00",
            "env": "UAT",
            "checklist_mismatches": 1,
        })
        report_archive.save("Acme / West", "<h1>second</h1>", {
            "generated_at": "2026-08-15T11:00:00+00:00",
            "env": "PROD",
            "checklist_mismatches": 2,
            "pe_approved": True,
            "cust_approved": True,
            "pe_name": "PE Reviewer",
            "cust_name": "Customer Reviewer",
        })
        reports = report_archive.list_reports()
        loaded = report_archive.get_report("acme-west")
        _assert(len(reports) == 1, f"replacement duplicated customer: {reports}")
        _assert(reports[0]["env"] == "PROD", f"second metadata missing: {reports}")
        _assert(reports[0]["checklist_mismatches"] == 2, f"second mismatch count missing: {reports}")
        _assert(reports[0]["pe_approved"] and reports[0]["cust_approved"],
                f"review completion metadata missing: {reports}")
        _assert(reports[0]["pe_name"] == "PE Reviewer" and reports[0]["cust_name"] == "Customer Reviewer",
                f"reviewer metadata missing: {reports}")
        _assert(loaded is not None and loaded["html"] == "<h1>second</h1>", f"second HTML missing: {loaded}")
        print("  [OK] second save replaces metadata and HTML")
    finally:
        _restore_archive(root, original)


def test_unknown_slug_returns_none() -> None:
    root, original = _isolate_archive()
    try:
        _assert(report_archive.get_report("missing") is None, "unknown archive must return None")
        print("  [OK] unknown archive slug returns None")
    finally:
        _restore_archive(root, original)


def test_export_snapshot_preserves_finite_calculated_values_only() -> None:
    root, original = _isolate_archive()
    try:
        report_archive.save("Acme Snapshot", "<h1>snapshot</h1>", {
            "batch_metrics_captured": True,
            "batch_compliance_pct": 97.5,
            "batch_total_jobs": 24,
            "batch_total_runs": 129,
            "batch_total_hrs": 34.25,
            "batch_breach_count": 1,
            "batch_at_risk_count": 2,
            "batch_ok_count": 21,
            "resource_metrics_captured": True,
            "resource_fleet_grade": "B",
            "resource_fleet_score": 88.4,
            "resource_total_servers": 12,
            "resource_critical_count": 1,
            "resource_warning_count": 2,
            "sow_metrics_captured": True,
            "sow_status": "ACCEPTABLE",
            "sow_metrics_count": 2,
            "benchmark_metrics_captured": True,
            "benchmark_total_transactions": 7,
            "benchmark_sla_breach_count": 1,
            "benchmark_degraded_count": 2,
            "batch_perf_regression_count": 3,
            "batch_perf_total_jobs": 9,
            "issues_count": 4,
        })
        record = report_archive.list_reports()[0]
        expected = {
            "batch_metrics_captured": 1, "batch_compliance_pct": 97.5,
            "batch_total_jobs": 24, "batch_total_runs": 129, "batch_total_hrs": 34.25,
            "batch_breach_count": 1, "batch_at_risk_count": 2, "batch_ok_count": 21,
            "resource_metrics_captured": 1, "resource_fleet_grade": "B",
            "resource_fleet_score": 88.4, "resource_total_servers": 12,
            "resource_critical_count": 1, "resource_warning_count": 2,
            "sow_metrics_captured": 1, "sow_status": "ACCEPTABLE", "sow_metrics_count": 2,
            "benchmark_metrics_captured": 1, "benchmark_total_transactions": 7,
            "benchmark_sla_breach_count": 1, "benchmark_degraded_count": 2,
            "batch_perf_regression_count": 3, "batch_perf_total_jobs": 9,
            "issues_count": 4,
        }
        _assert({key: record[key] for key in expected} == expected,
                f"snapshot values drifted or were recomputed: {record}")
        report_archive.save("Non-finite", "<h1>nan</h1>", {
            "batch_metrics_captured": True,
            "batch_compliance_pct": float("nan"),
        })
        nan_record = next(row for row in report_archive.list_reports() if row["customer"] == "Non-finite")
        _assert(nan_record["batch_compliance_pct"] is None,
                f"NaN must not become a displayed value or fabricated zero: {nan_record}")
        print("  [OK] export snapshot keeps exact finite values and rejects NaN")
    finally:
        _restore_archive(root, original)


def test_existing_archive_schema_migrates_without_inventing_snapshot_data() -> None:
    root, original = _isolate_archive()
    try:
        report_archive._FILES_DIR.mkdir(parents=True, exist_ok=True)
        conn = sqlite3.connect(report_archive._DB_PATH)
        try:
            conn.executescript("""
                CREATE TABLE reports (
                    customer_slug TEXT PRIMARY KEY, customer TEXT NOT NULL,
                    generated_at TEXT NOT NULL, env TEXT, pe_approved INTEGER,
                    cust_approved INTEGER, pe_name TEXT, cust_name TEXT,
                    checklist_mismatches INTEGER, sla_breach_count INTEGER,
                    sla_at_risk_count INTEGER, sla_total_jobs INTEGER,
                    file_path TEXT NOT NULL, file_hash TEXT NOT NULL,
                    file_size_bytes INTEGER NOT NULL
                );
                INSERT INTO reports VALUES (
                    'legacy-acme', 'Legacy Acme', '2026-08-15T10:00:00+00:00', 'UAT',
                    1, 0, 'PE', 'Customer', 0, 1, 2, 10,
                    'files/legacy-acme.html', 'hash', 100
                );
            """)
        finally:
            conn.close()
        record = report_archive.list_reports()[0]
        _assert(record["customer"] == "Legacy Acme", f"legacy row lost during migration: {record}")
        _assert(record["batch_metrics_captured"] is None and record["resource_fleet_score"] is None,
                f"migration fabricated metrics for a pre-snapshot export: {record}")
        print("  [OK] existing archive schema migrates without inventing historical metrics")
    finally:
        _restore_archive(root, original)


def test_checklist_mismatch_count_matches_rendered_rows() -> None:
    checklist_html, mismatch_count = _checklist_rows(
        {"batch": True, "res": True, "ui": True},
        {"batch": True, "res": False, "ui": False},
    )
    rendered_count = checklist_html.count("check--mismatch")
    _assert(mismatch_count == rendered_count,
            f"mismatch count {mismatch_count} != rendered {rendered_count}")
    print("  [OK] checklist mismatch metadata equals rendered mismatch rows")


def test_archive_endpoints_use_private_no_store_and_safe_filename() -> None:
    root, original = _isolate_archive()
    try:
        report_archive.save("Acme / West", "<h1>archived</h1>", {})
        app = FastAPI()
        app.include_router(archive_router, prefix="/api")
        client = TestClient(app)
        expected_headers = {
            "cache-control": "private, no-store, no-cache, max-age=0",
            "pragma": "no-cache",
            "x-content-type-options": "nosniff",
        }
        for path in (
            "/api/report-archive",
            "/api/report-archive/acme-west",
            "/api/report-archive/acme-west/download",
        ):
            response = client.get(path)
            _assert(response.status_code == 200, f"{path}: {response.status_code} {response.text}")
            actual_headers = {key: response.headers.get(key) for key in expected_headers}
            _assert(actual_headers == expected_headers, f"{path} headers: {response.headers}")
        download = client.get("/api/report-archive/acme-west/download")
        _assert(download.headers["content-disposition"] == 'attachment; filename="PE_Audit_acme-west_archived.html"',
                f"unsafe download filename: {download.headers['content-disposition']}")
        missing = client.get("/api/report-archive/missing")
        _assert(missing.status_code == 404, f"missing archive response: {missing.status_code}")
        _assert(missing.headers.get("cache-control") == expected_headers["cache-control"],
                f"404 headers: {missing.headers}")
        print("  [OK] archive endpoints set no-store/nosniff headers and slug-only filename")
    finally:
        _restore_archive(root, original)


def test_two_exports_for_same_customer_replace_the_archived_report() -> None:
    root, original = _isolate_archive()
    try:
        app = FastAPI()
        app.include_router(export_router.router, prefix="/api")
        app.include_router(archive_router, prefix="/api")
        client = TestClient(app)
        first = client.post("/api/export-report", json={
            "approvals": {"customer_name": "Acme Archive"},
            "issues": [{"ID": "ISS-001", "Description": "First archived evidence"}],
        })
        second = client.post("/api/export-report", json={
            "approvals": {"customer_name": "Acme Archive"},
            "issues": [{"ID": "ISS-002", "Description": "Second archived evidence"}],
        })
        _assert(first.status_code == 200 and first.headers.get("x-archive-status") == "saved",
                f"first export/archive response: {first.status_code} {first.headers}")
        _assert(second.status_code == 200 and second.headers.get("x-archive-status") == "saved",
                f"second export/archive response: {second.status_code} {second.headers}")
        reports = client.get("/api/report-archive").json()["reports"]
        opened = client.get("/api/report-archive/acme-archive")
        _assert(len(reports) == 1 and reports[0]["customer"] == "Acme Archive",
                f"same customer must remain one archive row: {reports}")
        _assert(opened.status_code == 200 and "Second archived evidence" in opened.text,
                "archive Open must render the second export")
        _assert("First archived evidence" not in opened.text,
                "archive still serves the superseded export")
        print("  [OK] two exports for one customer replace the stored archive report")
    finally:
        _restore_archive(root, original)


def test_export_route_archives_the_rendered_metric_snapshot() -> None:
    root, original = _isolate_archive()
    try:
        app = FastAPI()
        app.include_router(export_router.router, prefix="/api")
        app.include_router(archive_router, prefix="/api")
        response = TestClient(app).post("/api/export-report", json={
            "approvals": {"customer_name": "Snapshot Customer"},
            "batch": {"kpis": {
                "compliance_pct": 96.5, "jobs_breach": 1, "jobs_at_risk": 2,
                "jobs_ok": 17, "total_jobs": 20, "total_hrs": 31.5, "total_runs": 89,
            }},
            "resource": {"kpis": {
                "fleet_grade": "B", "fleet_score": 84.5, "total_servers": 8,
                "n_critical": 1, "n_warning": 2,
            }},
            "sow": {"overall_status": "ACCEPTABLE", "metrics": [
                {"label": "DFU", "sow": 100, "actual": 95, "pct": 95, "status": "ACCEPTABLE"},
            ]},
            "benchmark": {
                "total_transactions": 6, "sla_breaches": 1, "degraded": 2,
                "rows": [{"transaction": "Login", "sla": 2, "actual": 1.5}],
                "batch_perf_summary": {"total_jobs": 5, "regressions": 1},
            },
            "issues": [{"ID": "ISS-100", "Description": "Exported issue evidence"}],
        })
        _assert(response.status_code == 200 and response.headers.get("x-archive-status") == "saved",
                f"export did not archive snapshot: {response.status_code} {response.headers}")
        record = TestClient(app).get("/api/report-archive").json()["reports"][0]
        expected = {
            "batch_compliance_pct": 96.5, "batch_total_jobs": 20, "batch_total_runs": 89,
            "batch_total_hrs": 31.5, "batch_breach_count": 1, "batch_at_risk_count": 2,
            "batch_ok_count": 17, "resource_fleet_grade": "B", "resource_fleet_score": 84.5,
            "resource_total_servers": 8, "resource_critical_count": 1,
            "resource_warning_count": 2, "sow_status": "ACCEPTABLE", "sow_metrics_count": 1,
            "benchmark_total_transactions": 6, "benchmark_sla_breach_count": 1,
            "benchmark_degraded_count": 2, "batch_perf_regression_count": 1,
            "batch_perf_total_jobs": 5, "issues_count": 1,
        }
        _assert({key: record[key] for key in expected} == expected,
                f"registry snapshot differs from exported metric values: {record}")
        _assert(all(record[key] == 1 for key in (
            "batch_metrics_captured", "resource_metrics_captured", "sow_metrics_captured", "benchmark_metrics_captured",
        )), f"captured-state flags missing: {record}")
        print("  [OK] export route archives the exact rendered metric snapshot")
    finally:
        _restore_archive(root, original)


def test_unnamed_export_downloads_without_creating_a_fake_customer_record() -> None:
    root, original = _isolate_archive()
    try:
        app = FastAPI()
        app.include_router(export_router.router, prefix="/api")
        app.include_router(archive_router, prefix="/api")
        client = TestClient(app)
        response = client.post("/api/export-report", json={})
        _assert(response.status_code == 200, f"unnamed export failed: {response.status_code}")
        _assert(response.headers.get("x-archive-status") == "skipped",
                f"unnamed export must skip registry, got: {response.headers}")
        _assert(client.get("/api/report-archive").json()["reports"] == [],
                "unnamed export created a false customer registry record")
        _assert("Customer not specified" in response.text,
                "unnamed export should make the missing customer identity explicit")
        print("  [OK] unnamed export downloads without creating a fake customer record")
    finally:
        _restore_archive(root, original)


def test_export_storage_failure_never_blocks_download() -> None:
    root, original = _isolate_archive()
    try:
        # A file at the configured archive-directory path causes the real
        # save() operation to fail before it can write, without monkeypatching
        # the export route or touching the user's local archive directory.
        report_archive._FILES_DIR.write_text("not a directory", encoding="utf-8")
        app = FastAPI()
        app.include_router(export_router.router, prefix="/api")
        response = TestClient(app).post("/api/export-report", json={
            "approvals": {"customer_name": "Archive Storage Failure"},
        })
        _assert(response.status_code == 200, f"failed archive response status: {response.status_code}")
        _assert(response.headers.get("x-archive-status") == "failed", f"failed headers: {response.headers}")
        _assert("attachment;" in response.headers.get("content-disposition", ""),
                "archive storage failure blocked the export download")
        print("  [OK] real archive storage failure is explicit and never blocks download")
    finally:
        _restore_archive(root, original)


def _registry_needs_attention(record: dict) -> bool:
    """Mirror the frozen exception facts used by the registry filter.

    Sign-off and attention deliberately remain independent: an approved audit
    can retain evidence gaps or operational exceptions.
    """
    return any((
        int(record.get("sla_breach_count") or 0) > 0,
        int(record.get("sla_at_risk_count") or 0) > 0,
        int(record.get("checklist_mismatches") or 0) > 0,
        int(record.get("resource_critical_count") or 0) > 0,
        int(record.get("benchmark_sla_breach_count") or 0) > 0,
        int(record.get("batch_perf_regression_count") or 0) > 0,
        str(record.get("sow_status") or "").upper() in {"OVER", "CRITICAL_OVER"},
    ))


def test_registry_fixture_keeps_signoff_and_attention_filters_independent() -> None:
    """250 records must remain compactly filterable; states do not partition rows."""
    root, original = _isolate_archive()
    try:
        fixtures = (
            ("Signed clean", {"pe_approved": True, "cust_approved": True, "checklist_mismatches": 0}),
            ("Signed gaps", {"pe_approved": True, "cust_approved": True, "checklist_mismatches": 2}),
            ("Pending clean", {"pe_approved": False, "cust_approved": False, "checklist_mismatches": 0}),
        )
        for index, (customer, meta) in enumerate(fixtures):
            result = report_archive.save(customer, f"<h1>{customer}</h1>", {
                "generated_at": f"2026-08-16T00:00:0{index}+00:00", **meta,
            })
            _assert(result["ok"], f"fixture save failed: {customer} {result}")

        # Simulate the intended 250+ customer registry without creating any
        # production records. The UI is responsible for compact rendering;
        # this fixture ensures the API can supply the full scan volume.
        for index in range(247):
            result = report_archive.save(f"Scan customer {index:03}", "<h1>fixture</h1>", {
                "generated_at": f"2026-08-16T01:{index // 60:02}:{index % 60:02}+00:00",
            })
            _assert(result["ok"], f"scan fixture save failed at {index}: {result}")

        rows = report_archive.list_reports()
        by_customer = {row["customer"]: row for row in rows}
        _assert(len(rows) == 250, f"expected 250 scan records, got {len(rows)}")
        signed = {name for name, row in by_customer.items() if bool(row["pe_approved"] and row["cust_approved"])}
        attention = {name for name, row in by_customer.items() if _registry_needs_attention(row)}
        _assert({"Signed clean", "Signed gaps"}.issubset(signed), f"signed filter lost records: {signed}")
        _assert("Signed gaps" in attention, f"signed-with-gaps must be visible under attention: {attention}")
        _assert("Signed clean" not in attention and "Pending clean" not in attention,
                f"clean fixtures incorrectly marked attention: {attention}")
        _assert("Pending clean" not in signed, f"pending fixture incorrectly signed: {signed}")
        print("  [OK] 250-row registry fixture keeps sign-off and attention facts independent")
    finally:
        _restore_archive(root, original)


def test_exported_fleet_score_matches_the_frozen_archive_value() -> None:
    """The archived snapshot must reuse the value rendered in this exact HTML."""
    root, original = _isolate_archive()
    try:
        app = FastAPI()
        app.include_router(export_router.router, prefix="/api")
        response = TestClient(app).post("/api/export-report", json={
            "approvals": {"customer_name": "Fleet parity"},
            "resource": {"kpis": {
                "fleet_grade": "B", "fleet_score": 82.5, "total_servers": 4,
                "n_critical": 0, "n_warning": 1,
            }},
        })
        _assert(response.status_code == 200 and response.headers.get("x-archive-status") == "saved",
                f"fleet parity export failed: {response.status_code} {response.headers}")
        rendered = re.search(r'class="gauge__cap tabnum">([^<]+)/100</div>', response.text)
        _assert(rendered is not None, "exported HTML did not render the Fleet score")
        record = report_archive.get_report("fleet-parity")
        _assert(record is not None, "fleet parity archive record is missing")
        rendered_score = rendered.group(1)
        archived_score = f"{float(record['resource_fleet_score']):g}"
        _assert(rendered_score == archived_score,
                f"full HTML Fleet score {rendered_score!r} != frozen archive value {archived_score!r}")
        print("  [OK] exported Fleet score exactly matches the frozen archive value")
    finally:
        _restore_archive(root, original)


def test_registry_template_compacts_rows_and_filters_independent_facts() -> None:
    """Exercise the actual inline filter/chip functions without a browser."""
    template = (
        Path(__file__).resolve().parents[3]
        / "backend"
        / "legacy-ui"
        / "templates"
        / "report_archive.html"
    ).read_text(encoding="utf-8")
    script = template.split("<script>", 1)[1].split("</script>", 1)[0]
    # The first DOM listener starts browser wiring. Everything before it is the
    # pure presentation/filter surface we want to execute against test records.
    pure_script = script.split('\n\ndocument.querySelectorAll(".filter").forEach', 1)[0]
    records = [
        {"customer": "Signed clean", "pe_approved": 1, "cust_approved": 1, "checklist_mismatches": 0},
        {"customer": "Signed gaps", "pe_approved": 1, "cust_approved": 1, "checklist_mismatches": 2},
        {"customer": "Pending clean", "pe_approved": 0, "cust_approved": 0, "checklist_mismatches": 0},
    ]
    chip_sample = {
        "batch_metrics_captured": 1, "batch_compliance_pct": 100,
        "batch_breach_count": 0, "batch_at_risk_count": 0,
        "batch_total_jobs": 10, "batch_ok_count": 10, "batch_total_runs": 20, "batch_total_hrs": 5,
        "resource_metrics_captured": 1, "resource_fleet_grade": "B", "resource_fleet_score": 82.5,
        "resource_total_servers": 4, "resource_critical_count": 0, "resource_warning_count": 0,
        "sow_metrics_captured": 1, "sow_status": "ACCEPTABLE", "sow_metrics_count": 2,
        "benchmark_metrics_captured": 0,
        "issues_count": 0, "checklist_mismatches": 2,
    }
    harness = pure_script + """
const records = JSON.parse(process.argv[1]);
const chipSample = JSON.parse(process.argv[2]);
const result = {};
for (const filter of ["signed", "attention", "pending"]) {
  ACTIVE_FILTER = filter;
  result[filter] = records.filter(matchesFilter).map(record => record.customer).sort();
}
result.chips = snapshotGroups(chipSample).map(group => group.compactLabel);
console.log(JSON.stringify(result));
"""
    completed = subprocess.run(
        ["node", "-e", harness, json.dumps(records), json.dumps(chip_sample)],
        capture_output=True, text=True, check=False,
    )
    _assert(completed.returncode == 0, f"registry template harness failed: {completed.stderr}")
    result = json.loads(completed.stdout)
    _assert(result["signed"] == ["Signed clean", "Signed gaps"], f"signed filter: {result}")
    _assert(result["attention"] == ["Signed gaps"], f"attention filter: {result}")
    _assert(result["pending"] == ["Pending clean"], f"pending filter: {result}")
    _assert(len(result["chips"]) == 6, f"expected six compact snapshot chips: {result}")
    _assert(result["chips"][0] == "SLA 100%", f"SLA chip drifted: {result}")
    _assert(result["chips"][1].startswith("Fleet B") and result["chips"][1].endswith("82.5"),
            f"Fleet chip drifted: {result}")
    _assert(result["chips"][2] == "SOW ACCEPTABLE" and result["chips"][3] == "Benchmark N/A",
            f"SOW/benchmark chips drifted: {result}")
    _assert(result["chips"][4] == "Issues 0" and result["chips"][5].endswith("2 gaps"),
            f"issue/gap chips drifted: {result}")
    _assert("⚠ ${formatCount(mismatches)} gap" in template,
            "gap chip must retain its warning marker next to sign-off")
    _assert('if (!expanded && !detail.dataset.ready)' in template,
            "breakdown panels must be created only when the row is expanded")
    _assert('cell.colSpan = 6' in template, "expanded detail row must span the six registry columns")
    print("  [OK] registry compact chips and independent signed/attention filters")


def main() -> None:
    print("Report archive regression suite")
    print("-" * 60)
    test_save_then_list_returns_one_customer()
    test_second_save_replaces_metadata_and_html()
    test_unknown_slug_returns_none()
    test_export_snapshot_preserves_finite_calculated_values_only()
    test_existing_archive_schema_migrates_without_inventing_snapshot_data()
    test_checklist_mismatch_count_matches_rendered_rows()
    test_archive_endpoints_use_private_no_store_and_safe_filename()
    test_two_exports_for_same_customer_replace_the_archived_report()
    test_export_route_archives_the_rendered_metric_snapshot()
    test_unnamed_export_downloads_without_creating_a_fake_customer_record()
    test_export_storage_failure_never_blocks_download()
    test_registry_fixture_keeps_signoff_and_attention_filters_independent()
    test_exported_fleet_score_matches_the_frozen_archive_value()
    test_registry_template_compacts_rows_and_filters_independent_facts()
    print("-" * 60)
    print("REPORT ARCHIVE CHECKS PASSED")


if __name__ == "__main__":
    main()
