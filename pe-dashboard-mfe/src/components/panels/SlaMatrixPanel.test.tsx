import React from 'react';
import { render } from '@testing-library/react';
import { AppDataProvider } from '../../context/AppDataContext';
import { SlaMatrixPanel } from './SlaMatrixPanel';

describe('SlaMatrixPanel', () => {
  it('shows the upload control and empty state before any file is uploaded', () => {
    window['env'] = { LOCAL_APP_NAME: 'Local MFE' };
    const { getByText, getByRole } = render(
      <AppDataProvider>
        <SlaMatrixPanel />
      </AppDataProvider>,
    );

    expect(getByRole('button', { name: /Upload SLA Matrix/i })).toBeDefined();
    expect(getByText(/Upload a Ctrl-M file here/i)).toBeDefined();
  });
});
