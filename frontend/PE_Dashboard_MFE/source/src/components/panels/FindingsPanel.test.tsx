import React from 'react';
import { fireEvent, render, waitFor } from '@testing-library/react';
import { AppDataProvider } from '../../context/AppDataContext';
import { buildWorkflowItems, FindingsPanel } from './FindingsPanel';

describe('FindingsPanel', () => {
  beforeEach(() => {
    window['env'] = { LOCAL_APP_NAME: 'Local MFE' };
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('shows the empty state before findings are generated', () => {
    const { getByText, getByRole } = render(
      <AppDataProvider>
        <FindingsPanel />
      </AppDataProvider>,
    );

    expect(getByRole('button', { name: 'Generate Findings' })).toBeDefined();
    expect(getByText(/Upload batch, resource, and SLA data/i)).toBeDefined();
  });

  it('renders findings returned from the backend after clicking Generate Findings', async () => {
    jest.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({
        findings: [{ level: 'critical', text: 'Batch window breached SLA on 3 days.', recommendation: 'Resolve the three breached batch days before sign-off.', evidence: '3 affected dates' }],
        summary: { critical: 1, warning: 0, total: 1 },
        top_action: { rank: 1, severity: 'critical', text: 'Batch window breached SLA on 3 days.', recommendation: 'Resolve the three breached batch days before sign-off.' },
      }),
    } as Response);

    const { getByRole, findByText, findAllByText } = render(
      <AppDataProvider>
        <FindingsPanel />
      </AppDataProvider>,
    );

    fireEvent.click(getByRole('button', { name: 'Generate Findings' }));

    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    expect(await findByText(/Batch window breached SLA on 3 days/i)).toBeDefined();
    expect(await findByText(/Immediate Action Required/i)).toBeDefined();
    expect((await findAllByText(/Resolve the three breached batch days/i)).length).toBeGreaterThan(0);
  });

  it('keeps all findings available in the searchable ledger', async () => {
    jest.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({
        findings: [
          { level: 'critical', text: 'One action-required finding.' },
          { level: 'info', text: 'Contract SLA values were loaded.' },
          { level: 'ok', text: 'All individual jobs remain within SLA.' },
        ],
        summary: { critical: 1, warning: 0, total: 3 },
        top_action: { text: 'One action-required finding.' },
      }),
    } as Response);

    const { getByRole, findByText, findAllByText } = render(
      <AppDataProvider>
        <FindingsPanel />
      </AppDataProvider>,
    );

    fireEvent.click(getByRole('button', { name: 'Generate Findings' }));

    expect(await findByText(/Findings Ledger/i)).toBeDefined();
    expect((await findAllByText(/One action-required finding/i)).length).toBeGreaterThan(0);
    expect(await findByText(/Contract SLA values were loaded/i)).toBeDefined();
    expect(await findByText(/1 passed checks/i)).toBeDefined();
    fireEvent.click(await findByText(/1 passed checks/i));
    expect(await findByText(/All individual jobs remain within SLA/i)).toBeDefined();
  });

  it('maps the canonical workflow contract without zeroing SLA or names', () => {
    const [row] = buildWorkflowItems({
      workflow_summary: [{
        workflow_name: 'TEST_WEEKLY', runtime_h: 12.5, sla_h: 13,
        buffer_pct: 3.8, status: 'AT_RISK',
      }],
    });

    expect(row).toEqual(expect.objectContaining({
      name: 'TEST_WEEKLY', runtime_h: 12.5, sla_h: 13, buffer_pct: 3.8,
    }));
  });
});
