# Local report archive - 2026-08-15

## Gates run (measured)

- `py -3.14 _test_report_archive.py` -> save/list, replacement, unknown lookup, checklist-count, endpoint-header, same-customer export replacement, exact export-snapshot persistence, legacy-SQLite migration, and real isolated storage-failure checks passed.
- `py -3.14 _test_export_report.py` -> export evidence, SOW consistency, and per-job SLA-status checks passed.
- `py -3.14 _check_pe_config_refs.py` -> 116 known configuration names resolved across 60 files.
- `py -3.14 _validate_js.py` -> `static/app.js` and `static/deep_dive.js` passed.
- `py -3.14 -m py_compile routers\\export.py routers\\archive.py services\\report_archive.py _test_report_archive.py` -> passed.
- `git diff --check` -> no whitespace errors; existing CRLF conversion warnings remain.
- Inline registry-script parse -> passed; Impeccable detector on `templates/report_archive.html` returned no findings.
- Live loopback smoke: `uvicorn main:app --host 127.0.0.1 --port 8765` booted cleanly. `/archive` and `/api/report-archive` returned 200, then the temporary server was stopped.
- `npm run check:js` and `npm run check:pe-config` are not defined by this repository's `package.json`; the direct Python validators above were used instead.

## What shipped

- A standalone local `/archive` page, reached through the sidebar as **Review Registry**; the previous header Archive link was removed.
- SQLite-WAL plus local HTML archive storage, retaining only the latest report for each customer.
- Export saves an archive copy after rendering and returns `X-Archive-Status: saved|failed|skipped`; archive failure does not block the download.
- An export without a real customer name returns `X-Archive-Status: skipped`: it still downloads, but it deliberately creates no misleading **Unknown Customer** registry record. The dashboard tells the reviewer to add the customer name and export again.
- An archive-save failure also creates a persistent, dismissible session banner. It survives a same-tab refresh and clears only when dismissed or after a successful subsequent export.
- The Review Registry is a data-dense customer ledger: it shows the latest exported report per customer, approval completion, actual PE/customer reviewer names, SLA/evidence exceptions, export time, filters, sorting, and direct open/download actions.
- Every new registry record also stores the export-time values already rendered in that HTML: batch compliance/jobs/runs/runtime/exceptions, resource fleet grade/score/server exceptions, SOW status/metric count, benchmark and batch-performance counts, issue count, and checklist evidence gaps. The registry displays those values as an **Exported audit snapshot**; it never re-grades a later dashboard session. Missing data on legacy reports is displayed as not captured, never zero.
- Review Registry also exposes **Reset active dashboard**. Its confirmation and behavior clear only the active audit session before returning to the dashboard; archive records and exported HTML files remain viewable.
- Archive page rendering uses DOM APIs and `textContent` for stored values; slugs are URL-encoded.
- Archive API responses are private/no-store/nosniff, and download names derive only from normalized slugs.
- `_test_report_archive.py` provides direct archive regression coverage without writing production archive data.
- `.gitignore` keeps archive databases/HTML ignored while explicitly allowing `_test_report_archive.py` to be versioned.

## Proven vs. assumed

- Proven: archive writes are atomic at the HTML-file level and latest-per-customer replacement keeps one row and updated HTML.
- Proven: route-level regression coverage compares a saved registry snapshot to the same values rendered by the export route; finite values persist exactly and NaN is kept unavailable rather than converted to a misleading zero.
- Proven: direct and live loopback requests preserve the export download when archive saving succeeds; a real isolated archive-storage failure returns `X-Archive-Status: failed` while preserving the downloadable attachment.
- Not completed: visual browser click-through. The connected browser surface reported no available browser in this session. HTTP smoke checks covered the routes instead.
- Deployment constraint: the feature is local-only when the dashboard is kept on its default loopback bind. Do not expose this unauthenticated app or its archive endpoints on a LAN/reverse proxy without adding access control.

## Product decisions implemented

- Archive-save failure is non-blocking but visibly persistent for the browser session.
- Retention is unlimited and remains bounded by one current report per customer. No pruning, hiding, revision history, or report diffing was added.
