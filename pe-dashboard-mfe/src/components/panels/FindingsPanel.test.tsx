import React from 'react';
import { fireEvent, render, waitFor } from '@testing-library/react';
import { AppDataProvider } from '../../context/AppDataContext';
import { FindingsPanel } from './FindingsPanel';

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
    expect(getByText(/Upload batch and resource data first/i)).toBeDefined();
  });

  it('renders findings returned from the backend after clicking Generate Findings', async () => {
    jest.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({
        findings: [{ level: 'critical', text: 'Batch window breached SLA on 3 days.' }],
        summary: { critical: 1, warning: 0, total: 1 },
      }),
    } as Response);

    const { getByRole, findByText } = render(
      <AppDataProvider>
        <FindingsPanel />
      </AppDataProvider>,
    );

    fireEvent.click(getByRole('button', { name: 'Generate Findings' }));

    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    expect(await findByText(/Batch window breached SLA on 3 days/i)).toBeDefined();
  });
});
