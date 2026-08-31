import React from 'react';
import { fireEvent, render, waitFor } from '@testing-library/react';
import { AppDataProvider } from '../../context/AppDataContext';
import { ExecutivePanel } from './ExecutivePanel';

describe('ExecutivePanel', () => {
  beforeEach(() => {
    window['env'] = { LOCAL_APP_NAME: 'Local MFE' };
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('shows the empty state before the executive summary is generated', () => {
    const { getByText, getByRole } = render(
      <AppDataProvider>
        <ExecutivePanel />
      </AppDataProvider>,
    );

    expect(getByRole('button', { name: 'Generate Executive Summary' })).toBeDefined();
    expect(getByText(/Upload batch and resource data first/i)).toBeDefined();
  });

  it('renders KPIs returned from the backend after clicking Generate Executive Summary', async () => {
    jest.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({
        kpis: { sla_compliance_canonical: 92.5 },
        narrative: 'Overall batch health is stable.',
        sub_app_metrics: [],
      }),
    } as Response);

    const { getByRole, findByText } = render(
      <AppDataProvider>
        <ExecutivePanel />
      </AppDataProvider>,
    );

    fireEvent.click(getByRole('button', { name: 'Generate Executive Summary' }));

    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    expect(await findByText(/Overall batch health is stable/i)).toBeDefined();
  });
});
