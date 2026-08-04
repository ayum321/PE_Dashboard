# PE Audit Dashboard UI/UX Gap Report

Date: 2026-08-01

Scope: Existing FastAPI/vanilla-JS dashboard, reviewed on the no-data intake view at desktop (1440x900) and mobile (390x844). No product or design brief existed at review time. The report therefore judges operational clarity and implementation quality, not an assumed brand direction.

Method: Dual independent assessments. One product/UX review was completed before the deterministic evidence pass; a second review measured the live page, responsive geometry, accessible names, network transfer, and Impeccable detector findings. No dashboard code was changed by this review.

## Executive Verdict

The dashboard has a strong, PE-specific operational foundation. It is not interchangeable with a generic BI dashboard: Ctrl-M, resource evidence, SLA resolution, SOW volume, and governance are modeled in its navigation and terminology.

It is not ready to call perfect. The pre-audit intake imposes too many simultaneous choices on a first-time reviewer, the closed mobile drawer remains in keyboard tab order, and the initial upload screen downloads charting libraries and a 1.14 MB application script before any analytics view is needed. These are real usability, accessibility, and performance gaps rather than visual preference alone.

## Audit Health Score

| Dimension | Score | Key finding |
| --- | ---: | --- |
| Accessibility | 2/4 | Closed mobile navigation remains keyboard-focusable; the New Engagement action is 15px tall. |
| Performance | 2/4 | Intake initially transfers 1.28 MB, including inactive analytics libraries and 1.14 MB `app.js`. |
| Responsive design | 3/4 | No page overflow at 390px or 1440px, but mobile navigation focus behavior is incorrect. |
| Theming | 3/4 | Tokenized dark system is coherent; repeated inline colors/effects make it harder to evolve. |
| Implementation integrity | 3/4 | JS/config gates pass; nine detector hits require selective review rather than automatic removal. |
| Total | **13/20** | **Acceptable; address P1 gaps before calling the UI release-ready.** |

## UX Health Score

| Heuristic | Score | Key finding |
| --- | ---: | --- |
| Visibility of system status | 3/4 | Data-source status, progress, and toasts provide useful feedback. |
| Match with PE work | 4/4 | Ctrl-M, SLA, SOW, DFU/SKU, and governance language is domain-specific. |
| User control and freedom | 3/4 | Filters, reset, exports, and charts provide control; the first path is still unclear. |
| Consistency and standards | 3/4 | Shared cards and semantic colors work; icon, emoji, and link treatments vary. |
| Error prevention | 3/4 | Upload constraints help, but users must infer the intended file sequence. |
| Recognition over recall | 3/4 | Source-status strip helps, but required versus optional evidence is not explicit. |
| Flexibility and efficiency | 3/4 | Good filtering and charts; no obvious fast triage route or analyst shortcuts. |
| Aesthetic and minimalist design | 2/4 | Persistent glow, animation, gradients, and colored surfaces compete with audit evidence. |
| Error recovery | 3/4 | Toasts and retry paths exist. |
| Help and documentation | 2/4 | Inline help exists, but no concise minimum-data workflow is visible. |
| Total | **29/40** | **Good product foundation; clarity and restrained hierarchy are the main UX opportunity.** |

## Confirmed Gaps

### P1 - Closed mobile navigation remains keyboard-focusable

Location: `templates/index.html`, mobile `#sidebar` and sidebar-toggle behavior.

Evidence: At 390px the sidebar is translated off-screen but its navigation controls remain in the tab sequence. Keyboard users can focus controls that are not visible.

Impact: Violates expected focus behavior for modal/off-canvas navigation and makes mobile keyboard navigation confusing.

Recommendation: Toggle `inert` on the closed sidebar, move focus into the drawer when opened, return it to `#sidebar-toggle` when closed, and expose `aria-expanded` plus `aria-controls` on the toggle.

Suggested command: `/impeccable harden` followed by `/impeccable adapt`.

### P1 - New Engagement target is too small

Location: `templates/index.html`, `#btn-new-engagement`.

Evidence: Measured at 100x15 CSS pixels in both desktop and mobile views.

Impact: Misses WCAG 2.5.8 minimum 24px target size and is difficult to select, especially on touch devices.

Recommendation: Give the button a minimum 24px height, 24px touch target, visible focus styling, and retain its explicit destructive-session consequence.

Suggested command: `/impeccable adapt`.

### P1 - Intake does not explain the minimum viable audit path

Location: `templates/index.html`, Upload & Intake source cards.

Evidence: New users choose among Ctrl-M, resource evidence, SLA, SOW, Azure/manual routes, and two benchmark variants without a required/recommended/optional hierarchy.

Impact: First-time customer reviewers must understand PE vocabulary before knowing what to upload or what outcome each source unlocks.

Recommendation: Stage intake: request core Ctrl-M and resource evidence first; reveal SLA, SOW, issues, and benchmark files as labelled enrichment. Include a visible "minimum data for an initial PE view" route and a "recommended for full audit" route.

Suggested command: `/impeccable clarify` followed by `/impeccable layout`.

### P2 - Initial intake load pays for inactive analytics

Location: `templates/index.html` script tags and `static/app.js`.

Evidence: Fresh intake load transferred 1,281,298 bytes over 21 subresources. `app.js` transferred 1,139,870 bytes, while Chart.js, adapter, annotation, Hammer/zoom, and Plotly load before the user opens an analytics view.

Impact: Slower initial readiness on constrained customer networks and more parse/execute work before the primary upload task.

Recommendation: Split the monolithic front-end by view, dynamically import Plotly and Chart.js plugins when the owning view opens, and measure FCP, LCP, INP, and view-open latency before/after.

Suggested command: `/impeccable optimize`.

### P2 - Persistent decoration dilutes audit hierarchy

Location: `templates/index.html` base styles and persistent navigation/card surfaces.

Evidence: Animated logo, moving gradients, pulse/glow treatments, glass cards, and color accents appear across baseline surfaces, not only on high-priority states.

Impact: The UI is domain-specific but asks users to continuously parse decorative emphasis alongside genuinely critical PE status.

Recommendation: Reserve glow, pulse, and saturated accents for state changes, action-required conditions, and critical findings. Use one quiet default surface for normal operational content.

Suggested command: `/impeccable quieter` followed by `/impeccable polish`.

### P2 - Persistent text is often too small for customer review

Location: `templates/index.html`, sidebar/instruction/status text.

Evidence: Persistent labels use 9-11px sizing. This is dense but demanding at normal zoom, particularly for customer reviewers who do not work in the tool daily.

Impact: Reduced scanability and readability; critical source status may be overlooked.

Recommendation: Set 12px as the floor for persistent operational labels, increase line-height, and place lower-frequency technical detail behind progressive disclosure.

Suggested command: `/impeccable typeset`.

### P3 - Motion needs a reduced-motion alternative

Location: `templates/index.html`, logo, gradient, alert, and pulse keyframes.

Evidence: No `prefers-reduced-motion` handling was found for repeated animation.

Impact: Motion-sensitive users cannot reduce nonessential animation.

Recommendation: Add a targeted reduced-motion media query that stops decorative loops while retaining meaningful upload/progress state changes.

Suggested command: `/impeccable harden`.

## Detector Findings: Review, Do Not Blindly Remove

Impeccable detected nine instances in `static/app.js`: eight `side-tab` warnings at thick left-border severity/finding treatments and one `transition: width` warning in upload progress.

Assessment: These are not proven defects. The colored borders encode severity or finding category, and width transition communicates determinate upload progress. Keep them unless a focused visual review finds that they compete with stronger evidence. The detector is doing its job by flagging patterns that need a human product judgment.

## Positive Findings to Preserve

- The dashboard language and information model are genuinely PE-specific.
- Desktop/mobile page geometry has no measured horizontal overflow on the no-data view.
- The initial view has a coherent landmark/heading structure and no measured unnamed controls among the rendered controls.
- Resource-review table tooling, data-source status, upload progress, retry behavior, and chart destruction/reuse patterns are good foundations.
- Existing static gates pass: JavaScript validation and `pe_config` reference validation.

## Testing Gaps

This review did not exercise a populated customer engagement. Before release, repeat the audit after a realistic Ctrl-M/SLA/resource upload and validate:

- chart/table rendering and tab order in populated analysis views;
- long filenames, error/retry, and partially uploaded data states;
- 200% zoom and a real touch device;
- Core Web Vitals and interaction latency on a throttled connection;
- export, governance, Azure, and data-intensive table paths.

## Recommended Improvement Sequence

1. `/impeccable harden`: Fix drawer keyboard isolation, focus handling, and reduced-motion behavior.
2. `/impeccable adapt`: Increase touch target size and validate mobile navigation with a keyboard and touch input.
3. `/impeccable clarify`: Define the minimum viable audit upload path and label supplemental evidence clearly.
4. `/impeccable layout`: Convert intake into staged core-versus-enrichment workflow.
5. `/impeccable optimize`: Measure then split/defer chart dependencies and view-specific rendering.
6. `/impeccable quieter` and `/impeccable typeset`: Reduce nonsemantic decoration and raise persistent text readability.
7. `/impeccable polish`: Recheck the revised system and resolve only remaining high-value issues.

## Fresh Review Addendum

Second live pass: desktop 1440x900 and mobile 390x844, 2026-08-01. JavaScript validation passed. No horizontal page overflow was measured at either width.

### Newly Confirmed P1 Gaps

#### Upload tiles are not keyboard-operable

Location: `templates/index.html`, `#batch-drop-zone`, `#bench-batch-drop-zone`, `#bench-ui-drop-zone`, `#batch-sla-info-drop-zone`, and `#sow-intake-drop-zone`.

Evidence: At 390px each upload tile is a non-native `div` with `tabIndex=-1` and no button role. The tile may be clicked or receive a drag/drop action, but it cannot be reached or triggered by keyboard.

Impact: Keyboard-only users cannot upload core audit evidence. This is a task-blocking accessibility defect.

Recommendation: Use a visible `<label for="file-input">` or native button that triggers the hidden input. Preserve drag/drop as an enhancement, not the sole interaction. Give each control an accessible name and visible focus treatment.

Suggested command: `/impeccable harden`.

#### Partial data is labelled ready for analysis

Location: `templates/index.html`, `#upload-complete-banner` / "Ready to analyse" messaging and Executive Dashboard CTA.

Evidence: The upload-complete state can be revealed when any source has loaded, while the Executive Dashboard empty state states that Ctrl-M, Resource, and SLA data are needed for a complete view.

Impact: A reviewer can be led toward conclusions from a partial evidence set without understanding the coverage gap.

Recommendation: Replace the generic success state with source-aware coverage: "1 of 3 core evidence sources loaded"; show the next highest-value input and only label the audit ready when the chosen readiness threshold is met.

Suggested command: `/impeccable clarify` then `/impeccable layout`.

#### Mobile drawer has no accessible state contract

Location: `templates/index.html`, `#sidebar-toggle`, `#sidebar`, and `toggleSidebar()`.

Evidence: On mobile, the hidden off-canvas drawer retains ten focusable controls. `#sidebar-toggle` has no `aria-expanded` or `aria-controls` values, and no observed Escape-to-close path.

Impact: Keyboard users can enter invisible navigation content and lose orientation.

Recommendation: Add `aria-controls="sidebar"`, update `aria-expanded`, apply `inert` while the drawer is closed, move focus into the drawer when opened, close on Escape, and restore focus to the toggle.

Suggested command: `/impeccable harden`.

### Newly Confirmed P2 Gaps

#### Production Tailwind runtime warning

Location: `templates/index.html`, Tailwind Play CDN script.

Evidence: The live browser logs Tailwind's production warning on each load. The CDN compiler also adds startup work to an application already serving a large runtime script.

Impact: Not a user-visible blocker, but it is avoidable production overhead and a warning in normal browser diagnostics.

Recommendation: Compile Tailwind during build/release and serve the generated CSS locally. Keep the existing tokens/configuration, but remove the runtime CDN compiler from production.

Suggested command: `/impeccable optimize`.

#### Desktop intake visually compresses unrelated evidence paths

Location: `templates/index.html`, Upload & Intake grid.

Evidence: At wide desktop width, Ctrl-M, resource, benchmark, workflow SLA, and SOW inputs appear in one dense row with nearly equal visual weight. At mobile they become a long stack without an explicit sequence.

Impact: The user must infer dependency and priority from domain knowledge rather than the interface.

Recommendation: Present a compact two-step core workflow first, then an "Add confidence and context" section for SLA, SOW, benchmarks, and issues. Preserve all paths, but stop presenting them as equivalent prerequisites.

Suggested command: `/impeccable layout`.

### Fresh Evidence Notes

- The second pass found no horizontal overflow at 390px or 1440px.
- The initial dashboard has a valid primary heading and no runtime JavaScript error during this review.
- Initial desktop transfer was 1.17 MB in the local measurement, dominated by `static/app.js` (1.14 MB); third-party chart resources loaded on the upload screen but did not expose transfer-size timing in this browser because of cross-origin timing restrictions.
- Three decorative animations (`gradient-slide`, `logo-pulse`, and `logo-spin`) run immediately on the intake route. They reinforce the case for a targeted reduced-motion path and quieter default chrome.