import React, { useEffect } from 'react';
import { render } from '@testing-library/react';
import { AppDataProvider, useAppData } from '../../context/AppDataContext';
import { SlaMatrixPanel } from './SlaMatrixPanel';

const RICH_SLA_PAYLOAD = {
  compliance_pct: 91.2,
  window_day_compliance_pct: 88.4,
  total_runs: 300,
  total_jobs: 40,
  breaching_runs: 3,
  at_risk_runs: 4,
  ok_runs: 280,
  long_job_runs: 10,
  failed_runs: 3,
  worst_job: 'JOB_A',
  worst_hrs: 7.2,
  explicit_sla_matrix: false,
  sla_limit_hrs: 6,
  sla_label: 'Daily SLA (6.0h)',
  breaches: [
    { job_name: 'JOB_A', sub_application: 'FIN', run_date: '2026-08-01', run_hrs: 7.2, sla_limit_hrs: 6, breach_margin_hrs: 1.2, status: 'BREACH', sla_source: 'global' },
    { job_name: 'JOB_B', sub_application: 'OPS', run_date: '2026-08-02', run_hrs: 5.9, sla_limit_hrs: 6, breach_margin_hrs: -0.1, status: 'AT_RISK', sla_source: 'sla_matrix' },
  ],
  job_summary: [
    { job_name: 'JOB_A', runs: 12, peak_hrs: 7.2, avg_hrs: 5.1, sla_limit: 6, buffer_pct: -20, breach_runs: 2, breach_rate: 16.6, sla_source: 'global', sla_match_confidence: 'low' },
    { job_name: 'JOB_C', runs: 20, peak_hrs: 2.1, avg_hrs: 1.8, sla_limit: 6, buffer_pct: 65, breach_runs: 0, breach_rate: 0, sla_source: 'sla_matrix', sla_match_confidence: 'high' },
  ],
  job_baselines: {
    JOB_A: { runs: 12, avg_hrs: 5.1, std_hrs: 0.8, p95_hrs: 6.5, max_hrs: 7.2, expected_hrs: 6.6, sample_size_ok: true },
  },
  outliers: [
    { job_name: 'JOB_A', run_date: '2026-08-01', start_time: '01:00', end_time: '07:00', run_hrs: 6.0, expected_hrs: 4.5, expected_margin_hrs: 1.5, outlier_z: 3.4 },
  ],
  resource_linked: [
    { job_name: 'JOB_A', run_date: '2026-08-01', start_hour: 1, run_hrs: 7.2, resource_signal: { verdict: 'RESOURCE_LINK', fleet_cpu: 88, fleet_mem: 79, hot_hour_jobs: 4, critical_hosts: ['db01'] } },
    { job_name: 'JOB_B', run_date: '2026-08-02', start_hour: 2, run_hrs: 5.9, resource_signal: { verdict: 'ISOLATED', fleet_cpu: 30, fleet_mem: 40, hot_hour_jobs: 0, critical_hosts: [] } },
  ],
};

function SlaDataInjector({ children }: { children: React.ReactNode }) {
  const { setSlaMatrix } = useAppData();
  useEffect(() => { setSlaMatrix(RICH_SLA_PAYLOAD as never); }, [setSlaMatrix]);
  return <>{children}</>;
}

describe('SlaMatrixPanel', () => {
  it('shows the upload control and empty state before any file is uploaded', () => {
    window['env'] = { LOCAL_APP_NAME: 'Local MFE' };
    const { getByText, getByRole } = render(
      <AppDataProvider>
        <SlaMatrixPanel />
      </AppDataProvider>,
    );

    expect(getByRole('button', { name: /Upload SLA Matrix/i })).toBeDefined();
    expect(getByText(/Upload a Ctrl-M file here/i)).toBeDefined();
  });

  it('renders breach crux, job summary, resource-link table and buffer chart without crashing on real-shaped data', () => {
    window['env'] = { LOCAL_APP_NAME: 'Local MFE' };
    const { getByText, getAllByText } = render(
      <AppDataProvider>
        <SlaDataInjector>
          <SlaMatrixPanel />
        </SlaDataInjector>
      </AppDataProvider>,
    );

    expect(getByText(/is the worst offender/i)).toBeDefined();
    expect(getByText(/Job Summary/i)).toBeDefined();
    expect(getByText(/Resource-Linked Breaches/i)).toBeDefined();
    expect(getAllByText(/JOB_A/i).length).toBeGreaterThan(0);
  });
});
