import React from 'react';
import { render, fireEvent, waitFor } from '@testing-library/react';
import { AzureFetchModal } from './AzureFetchModal';
import * as dashboardApi from '../../api/dashboardApi';

jest.mock('../../api/dashboardApi', () => ({
  connectAzure: jest.fn(),
  disconnectAzure: jest.fn(),
  getAzureAuthStatus: jest.fn(),
  getAzureSubscriptions: jest.fn(),
  getAzureResourceGroups: jest.fn(),
  searchAzureVms: jest.fn(),
  discoverAzureVms: jest.fn(),
  fetchAzureResourcesWithProgress: jest.fn(),
  updateConfig: jest.fn(),
}));

describe('AzureFetchModal', () => {
  beforeEach(() => {
    jest.clearAllMocks();
    (dashboardApi.getAzureAuthStatus as jest.Mock).mockResolvedValue({
      method: 'browser',
      display_name: 'test@contoso.com',
      logged_in: true,
    });
    (dashboardApi.getAzureSubscriptions as jest.Mock).mockResolvedValue({
      ok: true,
      subscriptions: [{ id: 'sub-1', name: 'Contoso Sub' }],
    });
  });

  it('renders quick select chips including enterprise customers and regions', async () => {
    const { getByText, getByPlaceholderText } = render(
      <AzureFetchModal open={true} onClose={jest.fn()} onFetched={jest.fn()} />
    );

    await waitFor(() => {
      expect(getByText('Target')).toBeDefined();
      expect(getByText('Costco')).toBeDefined();
      expect(getByText('Europe')).toBeDefined();
      expect(getByText('APAC')).toBeDefined();
      expect(getByPlaceholderText(/Search any customer/)).toBeDefined();
    });
  });

  it('discovers VMs across multiple customers & regions and filters by customer', async () => {
    const mockVms = [
      {
        resource_id: '/sub/1/vm/targetapp01',
        name: 'targetapp01',
        type: 'APP',
        location: 'eastus2',
        customer: 'Target Corp',
        application: 'SCPO',
      },
      {
        resource_id: '/sub/1/vm/targetdb01',
        name: 'targetdb01',
        type: 'DB',
        location: 'eastus2',
        customer: 'Target Corp',
        application: 'SCPO',
      },
      {
        resource_id: '/sub/2/vm/costcoapp01',
        name: 'costcoapp01',
        type: 'APP',
        location: 'centralus',
        customer: 'Costco Wholesale',
        application: 'SCPO',
      },
      {
        resource_id: '/sub/3/vm/dhlapp01',
        name: 'dhlapp01',
        type: 'APP',
        location: 'westeurope',
        customer: 'DHL Supply Chain',
        application: 'SCPO',
      },
    ];

    (dashboardApi.searchAzureVms as jest.Mock).mockResolvedValue({
      total: 4,
      vms: mockVms,
    });

    const { getByText, getAllByText } = render(
      <AzureFetchModal open={true} onClose={jest.fn()} onFetched={jest.fn()} />
    );

    await waitFor(() => expect(getByText('All Customers')).toBeDefined());
    fireEvent.click(getByText('All Customers'));

    await waitFor(() => {
      expect(getByText('4 VMs · 3 customers')).toBeDefined();
    });

    // Customer filter buttons row and table headers should be visible
    expect(getAllByText('Target Corp').length).toBeGreaterThanOrEqual(1);
    expect(getAllByText('Costco Wholesale').length).toBeGreaterThanOrEqual(1);
    expect(getAllByText('DHL Supply Chain').length).toBeGreaterThanOrEqual(1);

    // Region filter buttons row should be visible
    expect(getAllByText('eastus2').length).toBeGreaterThanOrEqual(1);
    expect(getAllByText('centralus').length).toBeGreaterThanOrEqual(1);
    expect(getAllByText('westeurope').length).toBeGreaterThanOrEqual(1);

    // Filter by Costco
    fireEvent.click(getAllByText('Costco Wholesale')[0]);
    await waitFor(() => {
      expect(getByText('costcoapp01')).toBeDefined();
      expect(getByText('Select visible (1)')).toBeDefined();
    });
  });

  it('correctly toggles visible VMs and updates footer count without affecting other customer VMs', async () => {
    const mockVms = [
      { resource_id: '/sub/1/vm/targetapp01', name: 'targetapp01', type: 'APP', customer: 'Target Corp', location: 'eastus2' },
      { resource_id: '/sub/1/vm/targetdb01', name: 'targetdb01', type: 'DB', customer: 'Target Corp', location: 'eastus2' },
      { resource_id: '/sub/2/vm/costcoapp01', name: 'costcoapp01', type: 'APP', customer: 'Costco Wholesale', location: 'centralus' },
    ];

    (dashboardApi.searchAzureVms as jest.Mock).mockResolvedValue({
      total: 3,
      vms: mockVms,
    });

    const { getByText, getAllByText, getByLabelText } = render(
      <AzureFetchModal open={true} onClose={jest.fn()} onFetched={jest.fn()} />
    );

    await waitFor(() => expect(getByText('All Customers')).toBeDefined());
    fireEvent.click(getByText('All Customers'));

    await waitFor(() => {
      expect(getByText('3 VMs · 2 customers')).toBeDefined();
    });

    // Master header checkbox starts unchecked because initial selection is empty (0 selected by default)
    const masterCheckbox = getByLabelText('Select all visible VMs') as HTMLInputElement;
    expect(masterCheckbox.checked).toBe(false);
    expect(getByText('0 of 3')).toBeDefined();

    // Click "Select all VMs" button to select all 3
    fireEvent.click(getByText('Select all VMs'));
    await waitFor(() => {
      expect(masterCheckbox.checked).toBe(true);
      expect(getByText('3 of 3')).toBeDefined();
    });

    // Filter to Costco Wholesale
    fireEvent.click(getAllByText('Costco Wholesale')[0]);
    await waitFor(() => {
      expect(getByText('1 of 1 visible')).toBeDefined();
      expect(getByText(/selected \(3 of 3 total\)/)).toBeDefined();
    });

    // Toggle the customer group checkbox for Costco to unselect it
    const costcoGroupCheckbox = getByLabelText('Select all Costco Wholesale VMs') as HTMLInputElement;
    expect(costcoGroupCheckbox.checked).toBe(true);

    fireEvent.click(costcoGroupCheckbox);
    await waitFor(() => {
      expect(getByText('0 of 1 visible')).toBeDefined();
      expect(getByText(/selected \(2 of 3 total\)/)).toBeDefined();
      expect(getByText('Clear 2 hidden')).toBeDefined();
    });

    // Fetch button should be disabled when 0 visible are selected under active filter
    const fetchButton = getByText('Fetch Metrics').closest('button');
    expect(fetchButton).toBeDisabled();

    // Toggle back on via master visible checkbox
    fireEvent.click(masterCheckbox);
    await waitFor(() => {
      expect(getByText('1 of 1 visible')).toBeDefined();
      expect(getByText(/selected \(3 of 3 total\)/)).toBeDefined();
      expect(fetchButton).not.toBeDisabled();
    });
  });
});
