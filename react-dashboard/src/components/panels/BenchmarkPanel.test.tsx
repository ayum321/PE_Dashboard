import React from 'react';
import { render } from '@testing-library/react';
import { AppDataProvider } from '../../context/AppDataContext';
import { BenchmarkPanel } from './BenchmarkPanel';

describe('BenchmarkPanel', () => {
  it('shows the upload control and empty state before any file is uploaded', () => {
    window['env'] = { LOCAL_APP_NAME: 'Local MFE' };
    const { getByText, getByRole } = render(
      <AppDataProvider>
        <BenchmarkPanel />
      </AppDataProvider>,
    );

    expect(getByRole('button', { name: /Upload Benchmark File/i })).toBeDefined();
    expect(getByText(/Upload a benchmark comparison file/i)).toBeDefined();
  });
});
