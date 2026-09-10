# Generic SLA and Audit Report Integrity Plan

## Goal

Keep the existing UI layout while making the SLA Matrix, exported report, governance sign-off, and archived evidence consume the same customer-independent facts.

## Priority order

1. Preserve measurement eligibility and provenance at the SLA-engine boundary.
2. Freeze canonical workflow rows in the immutable audit payload.
3. Render canonical rows without customer-specific names or invented SLA defaults.
4. Gate clean approval on identities, checklist evidence, open critical evidence, SLA breaches, and unresolved measurements.
5. Correct resource metric direction and terminology.
6. Add regression tests, build, verify diffs, and deploy only after all gates pass.

## Invariants

- An unmatched configured sentinel is diagnostic evidence, not a scored breach.
- An explicit No SLA or missing runtime remains null and never receives a report-layer default.
- Workflow compliance uses eligible workflow rows only; workload jobs/runs retain their independent totals.
- Exact threshold boundaries are BREACH <= 0, AT_RISK > 0 to <= configured at-risk, LONG_JOB > at-risk to <= configured long-job, and OK above configured long-job.
- Clean approval requires two named, dated signatures and no blockers.
- Snapshot memory means host memory used; Azure time-series memory means available memory.
- Customer strings remain escaped in generated HTML.

## Verification gates

- SLA engine regression suite.
- Audit payload and legacy HTML regression suites.
- Canonical export integrity and exact-boundary tests.
- Focused React tests and production TypeScript build.
- Configuration reference check and `git diff --check`.
- Deployment only after the above pass.

## Completion evidence

- Backend active integrity suites passed, including 12 SLA and 3 cross-layer export cases.
- Archived payload, legacy HTML, and report-archive regression checks passed.
- Frontend full suite passed: 20 suites and 71 tests.
- Production React build completed; only the existing AzureFetchModal hook warning remains.
- Python compilation and `git diff --check` passed.
- Docker image build was unavailable on this workstation because Docker is not installed; source deployment uses the verified repository branch.
