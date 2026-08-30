import React, { useEffect } from 'react';
import { render } from '@testing-library/react';
import { AppDataProvider, useAppData } from '../../context/AppDataContext';
import { PeReviewSummary } from './PeReviewSummary';

function NarrativeInjector({ narrative, flags, findings, finalJudgment }: { narrative: unknown; flags?: unknown; findings?: unknown; finalJudgment?: unknown }) {
  const { setPeNarrative, setRedFlags, setFindings, setFinalJudgment } = useAppData();
  useEffect(() => {
    setPeNarrative(narrative as never);
    setRedFlags((flags ? { flags } : null) as never);
    setFindings((findings || null) as never);
    setFinalJudgment((finalJudgment || null) as never);
  }, [finalJudgment, findings, flags, narrative, setFinalJudgment, setFindings, setPeNarrative, setRedFlags]);
  return <PeReviewSummary />;
}

describe('PeReviewSummary UAT evidence gating', () => {
  it('suppresses an explicit UAT no-evidence placeholder and its questions', () => {
    const { queryByText } = render(
      <AppDataProvider>
        <NarrativeInjector
          narrative={{ verdict: 'HOLD', sections: [{ id: 'uat', title: 'UAT Validation', prose: 'No UAT evidence available.' }] }}
          flags={[{ category: 'UAT', question: 'This must stay hidden.' }]}
        />
      </AppDataProvider>,
    );
    expect(queryByText('UAT Validation')).toBeNull();
    expect(queryByText('This must stay hidden.')).toBeNull();
  });

  it('renders the explicit backend UAT evidence even without a narrative UAT section', () => {
    const { getByRole, getByText } = render(
      <AppDataProvider>
        <NarrativeInjector
          narrative={{ verdict: 'HOLD', sections: [] }}
          findings={{ uat: { available: true, evidence_type: 'batch_performance_comparison', severity: 'critical', comparable_jobs: 12, regressions: 3, question: 'Validate the three regressed workflows before release approval.' } }}
          flags={[{ category: 'Batch Performance', question: 'Confirm the regression with the customer.' }]}
        />
      </AppDataProvider>,
    );
    expect(getByRole('heading', { name: /UAT Validation/i })).toBeDefined();
    expect(getByText(/Validate the three regressed workflows/i)).toBeDefined();
    expect(getByText('Confirm the regression with the customer.')).toBeDefined();
  });

  it('shows the authoritative final decision instead of a stale narrative verdict', () => {
    const { getAllByText, getByText, queryByText } = render(
      <AppDataProvider>
        <NarrativeInjector
          narrative={{ verdict: 'CONDITIONAL', verdict_reason: 'Stale narrative reason.', summary: 'Overall verdict: CONDITIONAL. Stale narrative reason.', sections: [] }}
          finalJudgment={{ decision: 'BLOCKED', verdict_reason: '2 CRITICAL finding(s) require resolution before sign-off.' }}
        />
      </AppDataProvider>,
    );
    expect(getByText('BLOCKED')).toBeDefined();
    expect(getAllByText(/2 CRITICAL finding\(s\)/i).length).toBeGreaterThan(0);
    expect(queryByText(/Overall verdict: CONDITIONAL/i)).toBeNull();
  });
});
