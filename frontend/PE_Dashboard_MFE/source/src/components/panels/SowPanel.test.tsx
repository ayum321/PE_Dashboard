import React from 'react';
import { fireEvent, render, waitFor } from '@testing-library/react';
import { AppDataProvider } from '../../context/AppDataContext';
import { SowPanel } from './SowPanel';

describe('SowPanel', () => {
  beforeEach(() => {
    window['env'] = { LOCAL_APP_NAME: 'Local MFE' };
    window.sessionStorage.clear();
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
    expect(getByRole('button', { name: 'Save & Compare vs Actuals' })).toBeDefined();
  });

  it('restores saved actuals instead of rendering an absent value as zero', async () => {
    (global.fetch as jest.Mock).mockResolvedValue({
      ok: true,
      json: async () => ({
        baseline: { daily_dfu: 9_000_000 },
        actuals: { daily_dfu: 7_968_993 },
        compare: {
          metrics: [{ key: 'daily_dfu', label: 'Daily DFU', sow: 9_000_000, actual: 7_968_993, pct: 88.5, status: 'ACCEPTABLE' }],
          overall_status: 'ACCEPTABLE',
          summary: 'Saved comparison',
          bands: { under: 70, over: 110, crit: 120 },
        },
      }),
    } as Response);

    const { getByLabelText } = render(
      <AppDataProvider>
        <SowPanel />
      </AppDataProvider>,
    );

    await waitFor(() => expect((getByLabelText('Daily DFU actual') as HTMLInputElement).value).toBe('7968993'));
    expect((getByLabelText('Daily DFU') as HTMLInputElement).value).toBe('9000000');
  });

  it('keeps unsaved targets and actuals when the SOW tab is unmounted and reopened', async () => {
    (global.fetch as jest.Mock).mockResolvedValue({
      ok: true,
      json: async () => ({
        baseline: { daily_dfu: 9_000_000 },
        actuals: { daily_dfu: 7_000_000 },
        compare: null,
      }),
    } as Response);

    const first = render(
      <AppDataProvider>
        <SowPanel />
      </AppDataProvider>,
    );
    await waitFor(() => expect((first.getByLabelText('Daily DFU') as HTMLInputElement).value).toBe('9000000'));

    fireEvent.change(first.getByLabelText('Daily DFU'), { target: { value: '8999996' } });
    fireEvent.change(first.getByLabelText('Daily DFU actual'), { target: { value: '444444' } });
    first.unmount();

    const reopened = render(
      <AppDataProvider>
        <SowPanel />
      </AppDataProvider>,
    );
    await waitFor(() => {
      expect((reopened.getByLabelText('Daily DFU') as HTMLInputElement).value).toBe('8999996');
      expect((reopened.getByLabelText('Daily DFU actual') as HTMLInputElement).value).toBe('444444');
    });
  });
});
