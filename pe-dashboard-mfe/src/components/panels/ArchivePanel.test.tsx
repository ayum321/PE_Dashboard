import React from 'react';
import { render, waitFor } from '@testing-library/react';
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

    const { findByText } = render(<ArchivePanel />);

    expect(await findByText('Acme Corp')).toBeDefined();
  });
});
