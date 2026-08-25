import React from 'react';
import { fireEvent, render, waitFor } from '@testing-library/react';
import { SettingsPanel } from './SettingsPanel';

describe('SettingsPanel', () => {
  beforeEach(() => {
    window['env'] = { LOCAL_APP_NAME: 'Local MFE' };
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('loads config on mount and saves updated values', async () => {
    jest.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({ daily_sla_hrs: 4, sla_mode: 'daily' }),
    } as Response);

    const { findByLabelText, getByRole } = render(<SettingsPanel />);

    const dailySlaField = (await findByLabelText('Daily SLA hours')) as HTMLInputElement;
    expect(dailySlaField.value).toBe('4');

    fireEvent.click(getByRole('button', { name: 'Save Settings' }));

    await waitFor(() => expect(global.fetch).toHaveBeenCalledTimes(2));
  });
});
