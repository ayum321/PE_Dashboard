import os
import shutil
import tempfile
import unittest
from pathlib import Path
from services import evidence_vault, report_archive

class EvidenceVaultTests(unittest.TestCase):
    def setUp(self):
        self.temp_dir = tempfile.mkdtemp()
        self.temp_path = Path(self.temp_dir)
        self.orig_ev_root = evidence_vault._ROOT
        self.orig_ar_root = report_archive._ROOT
        evidence_vault._ROOT = self.temp_path
        report_archive._ROOT = self.temp_path
        report_archive._conn = None
        conn = report_archive._connect()
        with report_archive._lock:
            conn.execute("DELETE FROM report_evidence_documents")
            conn.execute("DELETE FROM reports")
            conn.commit()

    def tearDown(self):
        if report_archive._conn:
            try:
                report_archive._conn.close()
            except Exception:
                pass
        evidence_vault._ROOT = self.orig_ev_root
        report_archive._ROOT = self.orig_ar_root
        report_archive._conn = None
        shutil.rmtree(self.temp_dir, ignore_errors=True)

    def test_stage_and_retrieve_document(self):
        raw = b"Job_Name,Start_Time,Run_Sec\nJOB1,2026-09-01,100\n"
        res = evidence_vault.stage_document(
            customer="Acme Corp",
            document_type="ctrlm_history",
            filename="ctrlm_export.csv",
            raw_bytes=raw,
        )
        self.assertTrue(res["ok"])
        self.assertEqual(res["document_type"], "ctrlm_history")
        self.assertEqual(res["file_size_bytes"], len(raw))
        self.assertEqual(len(res["file_hash"]), 64)

        docs = evidence_vault.get_staged_documents("Acme Corp")
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["filename"], "ctrlm_export.csv")
        self.assertEqual(docs[0]["document_type"], "ctrlm_history")

    def test_stage_telemetry_snapshot(self):
        telemetry = {"source": "azure_monitor", "servers": [{"host": "vm1", "cpu_pct": 45.2}]}
        res = evidence_vault.stage_telemetry_snapshot("Acme Corp", telemetry)
        self.assertTrue(res["ok"])
        self.assertEqual(res["document_type"], "azure_telemetry")

        docs = evidence_vault.get_staged_documents("Acme Corp")
        self.assertEqual(len(docs), 1)
        self.assertIn("azure_telemetry", docs[0]["filename"])

    def test_freeze_evidence_to_audit_and_zip(self):
        raw = b"Workflow,SLA\nWF1,2.5\n"
        evidence_vault.stage_document(
            customer="Acme Corp",
            document_type="batch_sla",
            filename="BatchSLA_info.xlsx",
            raw_bytes=raw,
        )
        audit_id = "AUD-TEST-001"
        freeze_res = evidence_vault.freeze_evidence_to_audit(
            customer="Acme Corp",
            audit_id=audit_id,
            meta={"pe_name": "Ayush", "env": "PROD"},
            html_content="<html>Report</html>",
            payload_content={"meta": {"customer": "Acme Corp"}},
        )
        self.assertTrue(freeze_res["ok"])
        self.assertEqual(freeze_res["documents_count"], 1)
        self.assertTrue((self.temp_path / freeze_res["zip_path"]).exists())

        # Staging should now be empty
        staged_after = evidence_vault.get_staged_documents("Acme Corp")
        self.assertEqual(len(staged_after), 0)

        # Frozen documents should be queryable in SQLite
        frozen_docs = evidence_vault.list_audit_documents("acme-corp")
        self.assertEqual(len(frozen_docs), 1)
        self.assertEqual(frozen_docs[0]["filename"], "BatchSLA_info.xlsx")

        # Test ZIP package retrieval
        pkg = evidence_vault.get_latest_audit_package_zip("acme-corp")
        self.assertIsNotNone(pkg)
        self.assertTrue(pkg[0].name.endswith(".zip"))

    def test_path_traversal_protection(self):
        doc = evidence_vault.get_document("../../etc/passwd", customer_slug="acme-corp")
        self.assertIsNone(doc)

    def test_attach_supplementary_document(self):
        raw = b"Signed exception waiver for batch SLA"
        attach_res = evidence_vault.attach_document_to_archive(
            customer="acme-corp",
            filename="waiver_signed.pdf",
            raw_bytes=raw,
            document_type="waiver",
            label="VP SRE Waiver",
        )
        self.assertTrue(attach_res["ok"])
        docs = evidence_vault.list_audit_documents("acme-corp")
        self.assertEqual(len(docs), 1)
        self.assertEqual(docs[0]["document_label"], "VP SRE Waiver")

if __name__ == "__main__":
    unittest.main()
