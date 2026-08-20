import React from 'react';
import { render, waitFor } from '@testing-library/react';
import { AppDataProvider } from '../../context/AppDataContext';
import { ResourcePanel } from './ResourcePanel';

describe('ResourcePanel', () => {
  beforeEach(() => {
    window['env'] = { LOCAL_APP_NAME: 'Local MFE' };
    jest.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({ method: 'none' }),
    } as Response);
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('shows the empty state and an Azure connect control when no servers are loaded', async () => {
    const { getByText, getByRole } = render(
      <AppDataProvider>
        <ResourcePanel />
      </AppDataProvider>,
    );

    await waitFor(() => expect(global.fetch).toHaveBeenCalled());
    expect(getByText(/Upload a resource report/i)).toBeDefined();
    expect(getByRole('button', { name: /Connect Azure/i })).toBeDefined();
  });
});
