import { useRef } from 'react';
import {
  DashboardPayload,
  generateFindings,
  getExecutiveDashboard,
  getFinalJudgment,
  getPeNarrative,
  getRedFlags,
} from '../api/dashboardApi';
import { AppData, useAppData } from '../context/AppDataContext';
import {
  buildAnalysisPayload,
  buildFinalJudgmentPayload,
  buildPeNarrativePayload,
} from '../utils/buildAnalysisPayload';

export function useDerivedEvidenceRefresh() {
  const {
    clearDerivedEvidence,
    setFindings,
    setRedFlags,
    setPeNarrative,
    setExecutive,
    setFinalJudgment,
  } = useAppData();
  const refreshIdRef = useRef(0);

  const refreshDerivedEvidence = async (nextData: AppData): Promise<string> => {
    const refreshId = ++refreshIdRef.current;
    const stillCurrent = () => refreshId === refreshIdRef.current;
    const payload = buildAnalysisPayload(nextData);
    let findings: DashboardPayload | null = null;
    let redFlags: DashboardPayload | null = null;
    let executive: DashboardPayload | null = null;
    const unavailable: string[] = [];

    try {
      findings = await generateFindings(payload);
      if (stillCurrent()) setFindings(findings);
    } catch { unavailable.push('findings'); }
    try {
      redFlags = await getRedFlags(payload);
      if (stillCurrent()) setRedFlags(redFlags);
    } catch { unavailable.push('questions'); }
    try {
      executive = await getExecutiveDashboard({
        ...payload,
        sla_data: nextData.slaMatrix,
        findings: findings?.findings,
      });
      if (stillCurrent()) setExecutive(executive);
    } catch { unavailable.push('executive view'); }
    try {
      const narrative = await getPeNarrative(buildPeNarrativePayload(nextData, { findings, redFlags }));
      if (stillCurrent()) setPeNarrative(narrative);
    } catch { unavailable.push('PE review summary'); }
    try {
      const judgment = await getFinalJudgment(buildFinalJudgmentPayload(nextData, { findings, redFlags, executive }));
      if (stillCurrent()) setFinalJudgment(judgment);
    } catch { unavailable.push('final judgment'); }

    if (!stillCurrent()) return 'A newer evidence change is reconciling the shared analysis.';
    return unavailable.length
      ? `Batch evidence is ready; ${unavailable.join(', ')} can be refreshed from PE Findings.`
      : 'PE Findings, review summary, executive dashboard, and final judgment refreshed.';
  };

  return { clearDerivedEvidence, refreshDerivedEvidence };
}