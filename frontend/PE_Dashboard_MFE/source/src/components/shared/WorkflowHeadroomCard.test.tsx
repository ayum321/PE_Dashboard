import React from 'react';
import { render } from '@testing-library/react';
import { WorkflowHeadroomCard } from './WorkflowHeadroomCard';

describe('WorkflowHeadroomCard', () => {
  it('renders real SLA headroom and distinguishes zero buffer from a breach', () => {
    const { getByText } = render(
      <WorkflowHeadroomCard workflows={[
        { name: 'TEST_WEEKLY', runtime_h: 12.5, sla_h: 13, buffer_pct: 3.8, status: 'AT_RISK' },
        { name: 'TEST_DAYTIME', runtime_h: 16, sla_h: 16, buffer_pct: 0, status: 'NO_BUFFER' },
      ]} />,
    );

    expect(getByText('12.50h / 13.0h SLA')).toBeDefined();
    expect(getByText('3.8% buffer (30m)')).toBeDefined();
    expect(getByText('0.0% buffer (0m headroom)')).toBeDefined();
    expect(getByText(/No Buffer/)).toBeDefined();
  });
});
