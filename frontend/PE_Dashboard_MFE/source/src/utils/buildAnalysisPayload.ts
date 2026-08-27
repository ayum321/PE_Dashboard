import { AppData } from '../context/AppDataContext';
import { DashboardPayload } from '../api/dashboardApi';

/** Builds the shared analysis payload from real session data — same fields
 * the batch upload, SLA matrix, and resource panels have already populated.
 * Never fabricates values for fields with no real source. */
export function buildAnalysisPayload(data: AppData): DashboardPayload {
  const batch = data.batch || {};
  const payload: DashboardPayload = {
    batch_kpis: batch.kpis,
    top_jobs: batch.top_jobs,
    top_breaches: batch.top_breaches,
    window: batch.window,
    sub_stats: batch.sub_stats,
    anomalies: batch.anomalies,
    resource_kpis: data.resource?.kpis,
    servers: data.resource?.servers,
    sla_matrix: data.slaMatrix,
    benchmark: data.benchmark,
    sow_compare: data.sowCompare,
    issues: data.issues,
    customer_name: data.customerName,
  };
  Object.keys(payload).forEach((key) => {
    if (payload[key] === undefined || payload[key] === null) {
      delete payload[key];
    }
  });
  return payload;
}

/**
 * Contract for `/api/export-report`.
 *
 * The export router is intentionally different from the analysis endpoints:
 * it renders and freezes the nested batch/resource/SOW/benchmark evidence into
 * a standalone HTML report, then stores that exact snapshot in Review Registry.
 * Sending the flattened analysis payload here produced a download but left the
 * exported report and archive row without the actual evidence.
 */
export function buildExportPayload(data: AppData): DashboardPayload {
  const environments = Array.from(new Set(
    ((data.resource?.servers || []) as Array<{ environment?: string }>)
      .map((server) => String(server.environment || '').trim())
      .filter(Boolean),
  ));
  const payload: DashboardPayload = {
    batch: data.batch,
    resource: data.resource,
    servers: data.resource?.servers,
    sow: data.sowCompare,
    sow_contract: data.sowBaseline,
    benchmark: data.benchmark,
    issues: data.issues,
    approvals: data.approvals,
    findings: data.findings,
    red_flags: data.redFlags,
    executive: data.executive,
    final_judgment: data.finalJudgment,
    customer_name: data.customerName,
    env_type: environments.length === 1
      ? environments[0]
      : environments.length > 1
        ? `Mixed (${environments.join(' + ')})`
        : 'Not Detected',
  };
  Object.keys(payload).forEach((key) => {
    if (payload[key] === undefined || payload[key] === null) delete payload[key];
  });
  return payload;
}

/**
 * Build the /api/pe-narrative request from the same evidence as Findings.
 * This is deliberately a pass-through: the FastAPI service is the single
 * source of truth for the PE Review Summary, its SLA figures, and its prose.
 */
export function buildPeNarrativePayload(
  data: AppData,
  overrides: Partial<Pick<AppData, 'findings' | 'redFlags'>> = {},
): DashboardPayload {
  const payload: DashboardPayload = {
    batch: data.batch,
    resource: data.resource,
    sla_matrix: data.slaMatrix,
    sow_compare: data.sowCompare,
    benchmark: data.benchmark,
    red_flags: overrides.redFlags === undefined ? data.redFlags : overrides.redFlags,
    findings: overrides.findings === undefined ? data.findings : overrides.findings,
    customer_name: data.customerName,
  };
  // Omit absent evidence rather than serialising null: /api/pe-narrative
  // deliberately treats an explicit null sow_compare as a request to erase
  // its cached comparison, while an omitted value preserves the SOW Tier-2
  // fallback supplied by the parsed contract.
  Object.keys(payload).forEach((key) => {
    if (payload[key] === undefined || payload[key] === null) {
      delete payload[key];
    }
  });
  return payload;
}

/** The exact final-decision inputs used by the local dashboard.  Findings and
 * issues are included for traceability; the backend still owns all scoring. */
export function buildFinalJudgmentPayload(
  data: AppData,
  overrides: Partial<Pick<AppData, 'findings' | 'redFlags' | 'executive'>> = {},
): DashboardPayload {
  return {
    resource: data.resource,
    batch: data.batch,
    sla_matrix: data.slaMatrix,
    benchmark: data.benchmark,
    sow: data.sowCompare,
    sow_contract: data.sowBaseline,
    redflags: overrides.redFlags === undefined ? data.redFlags : overrides.redFlags,
    executive: overrides.executive === undefined ? data.executive : overrides.executive,
    findings: overrides.findings === undefined ? data.findings : overrides.findings,
    issues: data.issues,
  };
}
