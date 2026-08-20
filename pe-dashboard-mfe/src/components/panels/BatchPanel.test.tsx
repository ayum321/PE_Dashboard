import React from 'react';
import { render } from '@testing-library/react';
import { AppDataProvider } from '../../context/AppDataContext';
import { BatchPanel } from './BatchPanel';

describe('BatchPanel', () => {
  it('shows the empty state when no batch data has been uploaded', () => {
    window['env'] = { LOCAL_APP_NAME: 'Local MFE' };
    const { getByText } = render(
      <AppDataProvider>
        <BatchPanel />
      </AppDataProvider>,
    );

    expect(getByText(/Upload a Ctrl-M batch export/i)).toBeDefined();
  });
});
