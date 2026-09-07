"""Regression and validation tests for SLA Matrix fixes:
1. Cross-Customer Data Isolation (ensure_customer wipes cache on customer switch)
2. SLA clock time vs duration parsing ("Daily at 11 AM" -> None, not 11.0h)
3. Typo and ordinal start time parsing ("Thrusday at 12:45 AM" -> 6.25h)
4. Multi-line anchor sentinels matching in _anchor_job_mask
5. Row 4 merge (ASC_NIGHTLY + PROD_ATTA) resolving in Tier 1 with 3.0h SLA
6. Umbrella/decomposed workflow scoping without 41.199h recurrence
"""

import sys
import os
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import unittest
import pandas as pd
import datetime
from services.sla_merger import parse_sla_hours, _overnight_delta_hours, parse_start_time
from services import session_cache, config_store
from routers.sla_matrix import _anchor_job_mask, _compute_sla_matrix


class TestSlaRegressionFixes(unittest.TestCase):

    def test_clock_vs_duration_parsing(self):
        # Clock strings should NOT be parsed as durations
        self.assertIsNone(parse_sla_hours("Daily at 11 AM"))
        self.assertIsNone(parse_sla_hours("11:00 AM"))
        self.assertIsNone(parse_sla_hours("2:00 PM EST"))
        self.assertIsNone(parse_sla_hours("9PM"))
        # Real durations should be parsed
        self.assertEqual(parse_sla_hours("3.5 hrs"), 3.5)
        self.assertEqual(parse_sla_hours("1h 30m"), 1.5)
        self.assertEqual(parse_sla_hours("45 min"), 0.75)

    def test_typo_and_ordinal_clock_delta(self):
        # "Daily at 11 AM" to "2:00 PM" -> 3.0h
        d1 = _overnight_delta_hours("Daily at 11 AM", "2:00 PM")
        self.assertEqual(d1, 3.0)

        # "Thrusday at 12:45 AM" to "7:00 AM" -> 6.25h (handles "Thrusday" typo)
        d2 = _overnight_delta_hours("Thrusday at 12:45 AM", "7:00 AM")
        self.assertEqual(d2, 6.25)

        # "Daily at 05 AM" to "8:30 AM" -> 3.5h
        d3 = _overnight_delta_hours("Daily at 05 AM", "8:30 AM")
        self.assertEqual(d3, 3.5)

    def test_multiline_anchor_job_mask(self):
        jobs = pd.Series([
            "JOB_START_A",
            "P_ESP_PUBLISH_FILLRATE_REPORT_MPS_FBD",
            "SOME_OTHER_JOB",
            "P_ESP_PUBLISH_FILLRATE_REPORT_MPS_PCP",
            "D_RESTART_APP1_DAILY",
        ])

        # Cell H2 with newline between sentinels
        anchor_multiline = "P_ESP_PUBLISH_FILLRATE_REPORT_MPS_FBD\nP_ESP_PUBLISH_FILLRATE_REPORT_MPS_PCP"
        mask = _anchor_job_mask(jobs, anchor_multiline)
        self.assertTrue(mask[1])
        self.assertTrue(mask[3])
        self.assertFalse(mask[0])
        self.assertFalse(mask[2])

        # Underscore-insensitive match
        mask_us = _anchor_job_mask(jobs, "DRESTARTAPP1DAILY")
        self.assertTrue(mask_us[4])

        # Environment prefix match
        mask_pfx = _anchor_job_mask(jobs, "ESP_PUBLISH_FILLRATE_REPORT_MPS_FBD")
        self.assertTrue(mask_pfx[1])

    def test_customer_isolation_and_cache_wipe(self):
        # Set up Customer A state with authentic customer names
        session_cache.ensure_customer("Acme Corp")
        config_store.set("customer_name", "Acme Corp")
        config_store.set("workflow_sla_summary", [{"workflow": "PROD_ATTA", "runtime_hours": 2.5}])
        config_store.set("job_runs_df", [{"job": "JOB_1", "duration": 100}])

        # Switch to Customer B
        switched = session_cache.ensure_customer("Target Corp")
        self.assertTrue(switched)

        # Cache should be cleared
        summary = config_store.get("workflow_sla_summary")
        self.assertEqual(summary, [])
        runs = config_store.get("job_runs_df")
        self.assertEqual(runs, [])

    def test_row_4_merge_prod_atta_tier_1_resolution(self):
        # Simulates Row 4 with workflow="ASC_NIGHTLY", module="PROD_ATTA", sla_hours=3.0
        batch_sla_rows = [
            {
                "workflow": "ASC_NIGHTLY",
                "module": "PROD_ATTA",
                "aliases": ["ASC_NIGHTLY", "PROD_ATTA"],
                "sla_hours": 3.0,
                "first_job": "JOB_START",
                "last_job": "JOB_END",
                "schedule": "Daily at 11 AM",
            }
        ]

        # Seed into config_store so _compute_sla_matrix loads it
        config_store.set("_batch_sla_xlsx", {"workflows": batch_sla_rows})

        # Ctrl-M jobs logged under Sub_Application="PROD_ATTA"
        df = pd.DataFrame([
            {
                "Job_Name": "JOB_START",
                "Sub_Application": "PROD_ATTA",
                "Start_Time": "2026-08-30 11:00:00",
                "End_Time": "2026-08-30 11:10:00",
            },
            {
                "Job_Name": "JOB_STEP_2",
                "Sub_Application": "PROD_ATTA",
                "Start_Time": "2026-08-30 11:10:00",
                "End_Time": "2026-08-30 12:30:00",
            },
            {
                "Job_Name": "JOB_END",
                "Sub_Application": "PROD_ATTA",
                "Start_Time": "2026-08-30 11:45:00",
                "End_Time": "2026-08-30 12:00:00",
            },
        ])

        resp = _compute_sla_matrix(
            df=df,
            sla_mode="daily",
            custom_sla_hrs=None,
        )

        summary = resp.workflow_summary or []
        # Should match PROD_ATTA in Tier 1 with 3.0h SLA, runtime = 1.0h, buffer = (3 - 1)/3 * 100 = 66.67%
        self.assertEqual(len(summary), 1)
        wf = summary[0]
        self.assertEqual(wf["sub_application"], "PROD_ATTA")
        self.assertEqual(wf["sla_h"], 3.0)
        self.assertAlmostEqual(wf["runtime_h"], 1.0, places=2)
        self.assertEqual(wf["status"], "OK")
        self.assertIn("batch_sla_xlsx", wf["sla_source"])

    def test_umbrella_and_multiline_anchor_isolated_runtime(self):
        # Simulates PROD_MPS with multi-line anchors and an umbrella group spanning 41 hours
        batch_sla_rows = [
            {
                "workflow": "PROD_MPS",
                "module": "PROD_MPS",
                "aliases": ["PROD_MPS"],
                "sla_hours": 4.0,
                "first_job": "MPS_JOB_FIRST",
                "last_job": "P_ESP_PUBLISH_FILLRATE_REPORT_MPS_FBD\nP_ESP_PUBLISH_FILLRATE_REPORT_MPS_PCP",
            }
        ]

        config_store.set("_batch_sla_xlsx", {"workflows": batch_sla_rows})

        df = pd.DataFrame([
            # Unrelated early job in the export (41h earlier)
            {
                "Job_Name": "EARLY_CLEANUP",
                "Sub_Application": "PROD_MPS",
                "Start_Time": "2026-08-29 00:00:00",
                "End_Time": "2026-08-29 00:30:00",
            },
            # Actual PROD_MPS batch on 2026-08-30
            {
                "Job_Name": "MPS_JOB_FIRST",
                "Sub_Application": "PROD_MPS",
                "Start_Time": "2026-08-30 10:00:00",
                "End_Time": "2026-08-30 10:30:00",
            },
            {
                "Job_Name": "P_ESP_PUBLISH_FILLRATE_REPORT_MPS_PCP",
                "Sub_Application": "PROD_MPS",
                "Start_Time": "2026-08-30 11:30:00",
                "End_Time": "2026-08-30 12:00:00",
            },
            # Unrelated late job in the export (total span 41.199h)
            {
                "Job_Name": "LATE_EXPORT",
                "Sub_Application": "PROD_MPS",
                "Start_Time": "2026-08-30 17:11:56",
                "End_Time": "2026-08-30 17:11:56",
            },
        ])

        resp = _compute_sla_matrix(
            df=df,
            sla_mode="daily",
            custom_sla_hrs=None,
        )

        summary = resp.workflow_summary or []
        self.assertEqual(len(summary), 1)
        wf = summary[0]
        # Runtime must be 2.0h (10:00 to 12:00), NOT 41.199h!
        self.assertAlmostEqual(wf["runtime_h"], 2.0, places=2)
        self.assertEqual(wf["sla_h"], 4.0)
        self.assertEqual(wf["status"], "OK")

    def test_tier_3_assumed_ceiling_synchronization(self):
        # When NO BatchSLA is loaded, workflows fall into Tier 3.
        # Testing changing assumed ceiling from 6.0h to 8.25h:
        # Table rows MUST use 8.25h, moving headroom and buffer% accordingly.
        config_store.set("_batch_sla_xlsx", {})
        config_store.set("_sow_sla_windows", {})

        df = pd.DataFrame([
            {
                "Job_Name": "ACT_JOB_1",
                "Sub_Application": "PROD_ACT",
                "Start_Time": "2026-08-30 00:00:00",
                "End_Time": "2026-08-30 10:00:00",  # 10.0h runtime
            }
        ])

        # 1. At standard 6.0h ceiling:
        resp_6 = _compute_sla_matrix(df=df, sla_mode="daily", custom_sla_hrs=6.0)
        sum_6 = resp_6.workflow_summary or []
        self.assertEqual(len(sum_6), 1)
        wf_6 = sum_6[0]
        self.assertEqual(wf_6["sla_h"], 6.0)
        self.assertAlmostEqual(wf_6["runtime_h"], 10.0, places=2)
        # Headroom = (6 - 10) * 60 = -240m
        self.assertEqual(wf_6["duration_headroom_mins"], -240)
        # Buffer = (6 - 10)/6 * 100 = -66.67%
        self.assertAlmostEqual(wf_6["buffer_pct"], -66.67, places=1)
        self.assertEqual(wf_6["status"], "BREACH")

        # 2. At modified assumed ceiling 8.25h:
        resp_825 = _compute_sla_matrix(df=df, sla_mode="daily", custom_sla_hrs=8.25)
        sum_825 = resp_825.workflow_summary or []
        self.assertEqual(len(sum_825), 1)
        wf_825 = sum_825[0]
        # SLA MUST be 8.25h, NOT frozen at 6.0h!
        self.assertEqual(wf_825["sla_h"], 8.25)
        self.assertAlmostEqual(wf_825["runtime_h"], 10.0, places=2)
        # Headroom = (8.25 - 10) * 60 = -105m (moved from -240m to -105m!)
        self.assertEqual(wf_825["duration_headroom_mins"], -105)
        # Buffer = (8.25 - 10)/8.25 * 100 = -21.21% (moved from -66.7% to -21.2%!)
        self.assertAlmostEqual(wf_825["buffer_pct"], -21.21, places=1)
        self.assertEqual(wf_825["status"], "BREACH")


if __name__ == "__main__":
    unittest.main()

