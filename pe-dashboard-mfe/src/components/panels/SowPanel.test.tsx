import React from 'react';
import { render, waitFor } from '@testing-library/react';
import { AppDataProvider } from '../../context/AppDataContext';
import { SowPanel } from './SowPanel';

describe('SowPanel', () => {
  beforeEach(() => {
    window['env'] = { LOCAL_APP_NAME: 'Local MFE' };
    jest.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({}),
    } as Response);
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('loads the saved baseline on mount and shows the entry form', async () => {
    const { getByLabelText, getByRole } = render(
      <AppDataProvider>
        <SowPanel />
      </AppDataProvider>,
    );

    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    expect(getByLabelText('Daily DFU')).toBeDefined();
    expect(getByRole('button', { name: 'Save Baseline' })).toBeDefined();
    expect(getByRole('button', { name: 'Compare Against Actuals' })).toBeDefined();
  });
});
