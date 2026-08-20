import React from 'react';
import { render } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppDataProvider } from '../../context/AppDataContext';
import { BatchPanel } from './BatchPanel';

describe('BatchPanel', () => {
  it('shows the empty state when no batch data has been uploaded', () => {
    window['env'] = { LOCAL_APP_NAME: 'Local MFE' };
    const { getByText } = render(
      <MemoryRouter>
        <AppDataProvider>
          <BatchPanel />
        </AppDataProvider>
      </MemoryRouter>,
    );

    expect(getByText(/No Ctrl-M data loaded yet/i)).toBeDefined();
  });
});
