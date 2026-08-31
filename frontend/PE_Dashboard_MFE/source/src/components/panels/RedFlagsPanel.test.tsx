import React from 'react';
import { fireEvent, render, waitFor } from '@testing-library/react';
import { AppDataProvider } from '../../context/AppDataContext';
import { RedFlagsPanel } from './RedFlagsPanel';

describe('RedFlagsPanel', () => {
  beforeEach(() => {
    window['env'] = { LOCAL_APP_NAME: 'Local MFE' };
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('shows the empty state before red flags are generated', () => {
    const { getByText, getByRole } = render(
      <AppDataProvider>
        <RedFlagsPanel />
      </AppDataProvider>,
    );

    expect(getByRole('button', { name: 'Generate Red Flags' })).toBeDefined();
    expect(getByText(/Upload batch and resource data first/i)).toBeDefined();
  });

  it('renders red flags returned from the backend after clicking Generate Red Flags', async () => {
    jest.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({
        flags: [{ id: 'Q1', category: 'Volume', context: 'ctx', question: 'Why did volume spike?', risk: 'HIGH' }],
        risk_matrix: [],
        total: 1,
        by_risk: { HIGH: 1 },
      }),
    } as Response);

    const { getByRole, findByText } = render(
      <AppDataProvider>
        <RedFlagsPanel />
      </AppDataProvider>,
    );

    fireEvent.click(getByRole('button', { name: 'Generate Red Flags' }));

    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    expect(await findByText(/Why did volume spike\?/i)).toBeDefined();
  });
});
