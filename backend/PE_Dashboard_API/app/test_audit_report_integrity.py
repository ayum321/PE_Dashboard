"""Cross-layer regression tests for exported audit evidence and sign-off."""
import unittest

from routers.export import ExportRequest, _locked_legacy_context, _top_rows, templates
from services.audit_report_payload import build_audit_report_payload


def _approvals():
    return {
        "customer_name": "Example Customer",
        "env_type": "Mixed (PROD + TEST)",
        "checklist": {key: True for key in ("batch", "issues", "ui", "res", "perf", "sow", "data", "ctrlm", "res15")},
        "pe": {"approved": True, "name": "PE Reviewer", "date": "2026-09-10"},
        "customer": {"approved": True, "name": "Customer Reviewer", "date": "2026-09-10"},
    }


def _base_body():
    return {
        "approvals": _approvals(),
        "batch": {"kpis": {"total_jobs": 9, "total_runs": 30}, "top_jobs": [{"job_name": "LEGACY", "peak_hrs": 99, "sla_hrs": 1, "buffer_status": "BREACH"}]},
        "resource": {"kpis": {"total_servers": 1, "n_critical": 0, "n_warning": 0, "n_healthy": 1, "fleet_grade": "A", "fleet_score": 100}, "servers": [{"host": "db-a", "type": "DB", "environment": "PROD", "status": "Healthy", "mem_used": 70}], "deep_dive": {"vms": {"db-a": {"series": {}}}}},
        "servers": [{"host": "db-a", "type": "DB", "environment": "PROD", "status": "Healthy", "mem_used": 70}],
        "sow": {"metrics": [{"name": "DFU", "commitment": 10, "actual": 8, "pct_of_contract": 80, "status": "ACCEPTABLE"}]},
        "benchmark": {"rows": [{"name": "txn"}], "batch_perf_summary": {"total_jobs": 1, "regressions": 0}},
        "issues": [],
    }


class AuditReportIntegrityTests(unittest.TestCase):
    def test_canonical_workflows_are_frozen_and_rendered_without_defaults(self):
        body = _base_body()
        body["sla_matrix"] = {"workflow_summary": [
            {"workflow_name": "WF_OK", "batch_type": "DAILY", "tier": "T1", "sla_h": 3, "runtime_h": 1, "buffer_pct": 66.7, "status": "OK", "status_eligible": True, "measurement_state": "VALID", "sla_source": "batch_sla_xlsx", "total_runs": 2},
            {"workflow_name": "WF_UNRESOLVED", "batch_type": "DAILY", "tier": "T1", "sla_h": 3, "runtime_h": 12.632, "buffer_pct": None, "indicative_buffer_pct": -321.1, "status": "MEASUREMENT_UNRESOLVED", "status_eligible": False, "measurement_state": "ANCHOR_UNMATCHED", "measurement_reason_code": "JOB_NAME_UNMATCHED"},
            {"workflow_name": "WF_MISSING", "tier": "T1", "sla_h": 8.25, "runtime_h": None, "buffer_pct": None, "status": "NOT_OBSERVED", "status_eligible": False, "measurement_state": "NOT_OBSERVED", "measurement_reason_code": "WORKFLOW_NOT_OBSERVED_IN_CTRL_M"},
            {"workflow_name": "WF_NO_SLA", "tier": "UNDECLARED", "sla_h": None, "runtime_h": 2.5, "buffer_pct": None, "status": "SLA_UNDECLARED", "status_eligible": False, "measurement_state": "OBSERVED_UNANCHORED", "sla_source": "SLA_UNDECLARED"},
        ]}
        request = ExportRequest(**body)
        report = build_audit_report_payload(request.model_dump(exclude_none=True), audit_id="AUD-INTEGRITY")
        summary = report["workflow_sla"]["summary"]
        self.assertEqual((summary["inventory_count"], summary["scored_count"], summary["unresolved_count"], summary["no_sla_count"]), (4, 1, 2, 1))
        self.assertEqual(summary["compliance_pct"], 100.0)
        unresolved = report["workflow_sla"]["rows"][1]
        self.assertIsNone(unresolved["buffer_pct"])
        self.assertEqual(unresolved["indicative_buffer_pct"], -321.1)
        self.assertEqual(report["meta"]["sign_off_status"], "approved_with_exceptions")
        context, _ = _locked_legacy_context(request, report)
        html = templates.get_template("report_export.html").render(**context)
        self.assertIn("MEASUREMENT UNRESOLVED", html)
        self.assertIn("NOT OBSERVED", html)
        self.assertIn("NO SLA", html)
        self.assertNotIn("99.000h", html)  # legacy job row must not replace workflow truth
        self.assertIn("APPROVED WITH EXCEPTIONS", html)
        self.assertIn("host memory used", html)
        self.assertNotIn("DB memory tolerates the SGA/PGA band", html)
        self.assertIn("Neither metric is Oracle SGA/PGA usage", html)
        self.assertIn(">10<", html)
        self.assertIn(">8<", html)
        self.assertIn(">80.0%<", html)

    def test_clean_approval_requires_clean_evidence(self):
        body = _base_body()
        body["sla_matrix"] = {"workflow_summary": [{"workflow_name": "WF_OK", "sla_h": 3, "runtime_h": 1, "buffer_pct": 66.7, "status": "OK", "status_eligible": True, "measurement_state": "VALID"}]}
        report = build_audit_report_payload(body, audit_id="AUD-CLEAN")
        self.assertEqual(report["meta"]["sign_off_status"], "clean_approved")
        self.assertEqual(report["meta"]["sign_off_blockers"], [])

        cases = []
        missing_identity = _base_body()
        missing_identity["sla_matrix"] = body["sla_matrix"]
        missing_identity["approvals"]["pe"]["name"] = ""
        cases.append(missing_identity)
        critical_finding = _base_body()
        critical_finding["sla_matrix"] = body["sla_matrix"]
        critical_finding["findings"] = {"findings": [{"severity": "critical", "status": "open", "title": "Real blocker"}]}
        cases.append(critical_finding)
        critical_resource = _base_body()
        critical_resource["sla_matrix"] = body["sla_matrix"]
        critical_resource["resource"]["kpis"]["n_critical"] = 1
        cases.append(critical_resource)
        for case in cases:
            blocked = build_audit_report_payload(case)
            self.assertEqual(blocked["meta"]["sign_off_status"], "approved_with_exceptions")
            self.assertTrue(blocked["meta"]["sign_off_blockers"])

    def test_zero_buffer_is_a_breach_in_legacy_compatibility(self):
        html = _top_rows([{"job_name": "EXACT_LIMIT", "peak_hrs": 6, "avg_hrs": 5, "sla_hrs": 6, "buffer_pct": 0}])
        self.assertIn(">BREACH<", html)
        self.assertNotIn(">AT RISK<", html)


if __name__ == "__main__":
    unittest.main()
