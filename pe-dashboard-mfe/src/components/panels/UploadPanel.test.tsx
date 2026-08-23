import React from 'react';
import { fireEvent, render, waitFor } from '@testing-library/react';
import { MemoryRouter } from 'react-router-dom';
import { AppDataProvider } from '../../context/AppDataContext';
import { UploadPanel } from './UploadPanel';

jest.mock('../../api/dashboardApi', () => ({
  connectAzure: jest.fn(),
  disconnectAzure: jest.fn(),
  getAzureAuthStatus: jest.fn().mockResolvedValue({ method: 'none' }),
  getExecutiveDashboard: jest.fn(),
  getFinalJudgment: jest.fn(),
  getPeNarrative: jest.fn(),
  getRedFlags: jest.fn(),
  generateFindings: jest.fn(),
  parseSow: jest.fn(),
  processBatchMulti: jest.fn(),
  refreshBatch: jest.fn(),
  uploadBatchSlaXlsx: jest.fn(),
  uploadBenchmark: jest.fn(),
  uploadDashboardFile: jest.fn(),
}));

const api = require('../../api/dashboardApi') as {
  getAzureAuthStatus: jest.Mock;
  processBatchMulti: jest.Mock;
  uploadBatchSlaXlsx: jest.Mock;
};

const never = () => new Promise(() => undefined);

describe('UploadPanel intake activity', () => {
  beforeEach(() => {
    api.getAzureAuthStatus.mockResolvedValue({ method: 'none' });
    api.processBatchMulti.mockImplementation((_files: File[], onProgress?: (pct: number, loaded?: number, total?: number) => void) => {
      onProgress?.(51, 510, 1024);
      return never();
    });
    api.uploadBatchSlaXlsx.mockImplementation((_file: File, onProgress?: (pct: number, loaded?: number, total?: number) => void) => {
      onProgress?.(33, 330, 1000);
      return never();
    });
  });

  afterEach(() => jest.clearAllMocks());

  it('keeps Workflow SLA available while Ctrl-M uploads and shows byte-level progress', async () => {
    const { container, getByText } = render(
      <MemoryRouter>
        <AppDataProvider><UploadPanel /></AppDataProvider>
      </MemoryRouter>,
    );
    const batchInput = container.querySelector('#batch-upload-input') as HTMLInputElement;
    const slaInput = container.querySelector('#workflow-sla-input') as HTMLInputElement;

    fireEvent.change(batchInput, { target: { files: [new File(['batch'], 'ctrlm.csv', { type: 'text/csv' })] } });

    await waitFor(() => expect(api.processBatchMulti).toHaveBeenCalledTimes(1));
    expect(batchInput.disabled).toBe(true);
    expect(slaInput.disabled).toBe(false);
    expect(getByText(/510 B \/ 1.0 KB/i)).toBeDefined();

    fireEvent.change(slaInput, { target: { files: [new File(['sla'], 'BatchSLA_info.xlsx')] } });
    await waitFor(() => expect(api.uploadBatchSlaXlsx).toHaveBeenCalledTimes(1));
    expect(getByText(/330 B \/ 1000 B/i)).toBeDefined();
  });
});
