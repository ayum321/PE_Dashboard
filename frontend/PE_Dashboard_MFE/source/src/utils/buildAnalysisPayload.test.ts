import { AppData } from '../context/AppDataContext';
import { buildAnalysisPayload, buildExportPayload, buildFinalJudgmentPayload, buildPeNarrativePayload } from './buildAnalysisPayload';

const data: AppData = {
  batch: { kpis: { compliance_pct: 91.2 } },
  resource: { servers: [{ host: 'db01' }], kpis: { n_critical: 1 } },
  slaMatrix: { compliance_pct: 88.4 },
  benchmark: { total_transactions: 20 },
  sowBaseline: { sla_windows: { DAILY: { limit_hours: 6 } } },
  sowCompare: { overall_status: 'ACCEPTABLE' },
  findings: { findings: [{ level: 'critical', text: 'A real breach' }] },
  redFlags: { by_risk: { CRITICAL: 1 } },
  peNarrative: null,
  executive: { kpis: { score: 75 } },
  finalJudgment: null,
  customerName: 'Acme',
  issues: [{ ID: 'ISS-001', Type: 'Bug', Severity: 'Critical', Status: 'Open', Owner: '', ETA: 'N/A', Description: 'Known issue', Mitigation: '', Logged: '2026-08-01' }],
  approvals: { checklist: { batch: false, issues: false, ui: false, res: false, perf: false, sow: false, data: false, ctrlm: false, res15: false }, pe: { name: '', approved: false, date: null, override_blockers: false }, customer: { name: '', approved: false, date: null }, notes: '' },
  reviewedProducts: [],
};

describe('analysis payloads', () => {
  it('preserves resource KPIs and the browser-only issue register for findings', () => {
    const payload = buildAnalysisPayload(data);
    expect(payload.resource_kpis).toEqual({ n_critical: 1 });
    expect(payload.issues).toHaveLength(1);
  });

  it('passes the same evidence, findings, and issues to final judgment', () => {
    const payload = buildFinalJudgmentPayload(data);
    expect(payload.resource).toBe(data.resource);
    expect(payload.sow_contract).toBe(data.sowBaseline);
    expect(payload.findings).toBe(data.findings);
    expect(payload.issues).toBe(data.issues);
  });

  it('sends the complete shared evidence set to the FastAPI PE review renderer', () => {
    const payload = buildPeNarrativePayload(data);
    expect(payload.batch).toBe(data.batch);
    expect(payload.resource).toBe(data.resource);
    expect(payload.sla_matrix).toBe(data.slaMatrix);
    expect(payload.findings).toBe(data.findings);
    expect(payload.red_flags).toBe(data.redFlags);
  });

  it('uses the nested FastAPI export/archive contract so frozen reports retain all evidence', () => {
    const payload = buildExportPayload(data);
    expect(payload.batch).toBe(data.batch);
    expect(payload.resource).toBe(data.resource);
    expect(payload.servers).toBe(data.resource?.servers);
    expect(payload.sow).toBe(data.sowCompare);
    expect(payload.benchmark).toBe(data.benchmark);
    expect(payload.customer_name).toBe('Acme');
  });
});
