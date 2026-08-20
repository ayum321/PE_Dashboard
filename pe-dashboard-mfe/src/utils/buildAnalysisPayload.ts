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
    servers: data.resource?.servers,
    sla_matrix: data.slaMatrix,
    benchmark: data.benchmark,
    sow_compare: data.sowCompare,
    customer_name: data.customerName,
  };
  Object.keys(payload).forEach((key) => {
    if (payload[key] === undefined || payload[key] === null) {
      delete payload[key];
    }
  });
  return payload;
}
