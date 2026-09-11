import React from 'react';
import { render } from '@testing-library/react';
import { ArchivePanel } from './ArchivePanel';

describe('ArchivePanel', () => {
  beforeEach(() => {
    window['env'] = { LOCAL_APP_NAME: 'Local MFE' };
  });

  afterEach(() => {
    jest.restoreAllMocks();
  });

  it('shows the empty state when no reports have been archived', async () => {
    jest.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({ reports: [] }),
    } as Response);

    const { findByText } = render(<ArchivePanel />);

    expect(await findByText(/No reports have been generated/i)).toBeDefined();
  });

  it('renders archived reports returned from the backend', async () => {
    jest.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({
        reports: [{ customer_slug: 'acme', customer: 'Acme Corp', generated_at: '2024-01-01' }],
      }),
    } as Response);

    const { findAllByText } = render(<ArchivePanel />);

    expect((await findAllByText('Acme Corp')).length).toBeGreaterThan(0);
  });

  it('renders reviewer and customer names when present in the archive report', async () => {
    jest.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({
        reports: [{
          customer_slug: 'itc-ltd',
          customer: 'ITC Ltd',
          generated_at: '2026-09-09T11:38:00Z',
          pe_name: 'Ayush Mathur',
          cust_name: 'Antony Castaldi',
          pe_approved: true,
          cust_approved: true,
        }],
      }),
    } as Response);

    const { findByText } = render(<ArchivePanel />);

    expect(await findByText('Ayush Mathur')).toBeDefined();
    expect(await findByText('Antony Castaldi')).toBeDefined();
  });

  it('renders the Import report and Refresh registry buttons', async () => {
    jest.spyOn(global, 'fetch').mockResolvedValue({
      ok: true,
      json: async () => ({ reports: [] }),
    } as Response);

    const { findByText } = render(<ArchivePanel />);

    expect(await findByText('Import report')).toBeDefined();
    expect(await findByText('Refresh registry')).toBeDefined();
  });
});
