---
name: sla-evidence
description: Use when changing SLA Matrix, Daily Batch Window, buffer/headroom, status bands, peak runtime, duration labels, spikes, or PE Findings evidence.
---

# SLA Evidence Integrity

1. Read `CLAUDE.md`, the relevant backend calculation, and the MFE interface
   together. Confirm each displayed field is returned by the API.
2. Keep the canonical buffer equation backend-owned. Do not derive a competing
   status in React.
3. Label the duration basis: workflow worst run, elapsed span, active busy time,
   effective contiguous window, or fixed clock deadline. Never call all of them
   “peak window.”
4. Preserve `sla_source`, `reason_code`, and debug provenance. Thresholds must
   come from `pe_config.py` through live configuration.
5. Test idle gaps, overlapping jobs, missing SLA, fixed deadlines, failures, and
   spikes. Mutation-test any new status or headroom guard.
