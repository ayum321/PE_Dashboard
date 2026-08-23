import React, { useState } from 'react';
import { Box, Button, CircularProgress, Paper, Typography } from '@material-ui/core';
import { getFinalJudgment } from '../../api/dashboardApi';
import { useAppData } from '../../context/AppDataContext';
import { buildFinalJudgmentPayload } from '../../utils/buildAnalysisPayload';
import { KpiStatCard } from '../shared/KpiStatCard';

const DECISION_COLOR: Record<string, string> = {
  GO: '#10d96e',
  GO_WITH_NOTES: '#3b82f6',
  HOLD: '#f59e0b',
  BLOCKED: '#f43f5e',
  REMEDIATE: '#f43f5e',
  INSUFFICIENT_DATA: '#6b7db3',
};

type Evidence = { pillar?: string; fact?: string; points?: number; status?: string };
type Link = { severity?: string; text?: string };

export function FinalJudgmentCard() {
  const { data, setFinalJudgment } = useAppData();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const result = data.finalJudgment;
  const decision = String(result?.decision || 'INSUFFICIENT_DATA');
  const color = DECISION_COLOR[decision] || '#6b7db3';
  const pillars = (result?.pillars as Record<string, number>) || {};
  const evidence = (result?.evidence_chain as Evidence[]) || [];
  const links = (result?.cross_pillar_links as Link[]) || [];
  const actions = (result?.next_actions as string[]) || [];

  const run = async () => {
    setBusy(true);
    setError(null);
    try {
      setFinalJudgment(await getFinalJudgment(buildFinalJudgmentPayload(data)));
    } catch (runError) {
      setError(runError instanceof Error ? runError.message : 'Final judgment failed.');
    } finally {
      setBusy(false);
    }
  };

  return (
    <Paper className="kpi-card" elevation={0} style={{ marginTop: 16, padding: 20 }}>
      <Box display="flex" alignItems="center" justifyContent="space-between" flexWrap="wrap" style={{ gap: 12 }}>
        <Box>
          <Typography variant="h6">Final PE Judgment</Typography>
          <Typography variant="caption" color="textSecondary">
            Deterministic cross-pillar decision using the same loaded evidence as PE Findings.
          </Typography>
        </Box>
        <Button variant="contained" color="primary" onClick={run} disabled={busy}>
          {busy ? 'Computing…' : result ? 'Refresh Final Judgment' : 'Run Final Judgment'}
        </Button>
      </Box>
      {busy && <CircularProgress size={22} aria-label="Computing final judgment" style={{ marginTop: 12 }} />}
      {error && <Typography variant="body2" color="error" style={{ marginTop: 12 }}>{error}</Typography>}

      {!result ? (
        <Typography variant="body2" color="textSecondary" style={{ marginTop: 16 }}>
          Generate PE Findings, or run this judgment directly, once evidence is loaded.
        </Typography>
      ) : (
        <>
          <Box style={{ borderLeft: `4px solid ${color}`, background: `${color}14`, padding: '12px 16px', marginTop: 16 }}>
            <Typography variant="caption" style={{ color, fontWeight: 700, letterSpacing: '.08em' }}>DECISION · {decision.replace(/_/g, ' ')}</Typography>
            <Typography variant="body2" style={{ marginTop: 4 }}>{String(result.verdict_reason || result.verdict || '')}</Typography>
          </Box>
          <Box style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(150px, 1fr))', gap: 12, marginTop: 16 }}>
            <KpiStatCard label="Grade" value={String(result.grade || '—')} accent="#a855f7" />
            <KpiStatCard label="Score" value={result.score == null ? '—' : `${Number(result.score).toFixed(1)}%`} accent={color} />
            <KpiStatCard label="Evidence Pillars" value={String((result.pillars_present as unknown[] || []).length)} accent="#3b82f6" />
          </Box>
          {Object.keys(pillars).length > 0 && (
            <Box style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 14 }}>
              {Object.entries(pillars).map(([name, score]) => (
                <span key={name} className="metric-badge metric-badge-blue">{name.toUpperCase()} {Number(score).toFixed(1)}%</span>
              ))}
            </Box>
          )}
          {evidence.length > 0 && (
            <Box style={{ marginTop: 16 }}>
              <Typography variant="subtitle2">Evidence Ledger</Typography>
              {evidence.slice(0, 12).map((item, index) => (
                <Typography key={index} variant="caption" color="textSecondary" style={{ display: 'block', marginTop: 4 }}>
                  {String(item.pillar || 'Evidence').toUpperCase()}: {item.fact || '—'}{item.points ? ` (${item.points > 0 ? '+' : ''}${item.points} points)` : ''}
                </Typography>
              ))}
            </Box>
          )}
          {links.length > 0 && (
            <Box style={{ marginTop: 16 }}>
              <Typography variant="subtitle2">Cross-Pillar Links</Typography>
              {links.slice(0, 6).map((link, index) => <Typography key={index} variant="caption" color="textSecondary" style={{ display: 'block', marginTop: 4 }}>{link.text || '—'}</Typography>)}
            </Box>
          )}
          {actions.length > 0 && (
            <Box style={{ marginTop: 16 }}>
              <Typography variant="subtitle2">Next Actions</Typography>
              {actions.slice(0, 5).map((action, index) => <Typography key={index} variant="body2" style={{ marginTop: 4 }}>{index + 1}. {action}</Typography>)}
            </Box>
          )}
        </>
      )}
    </Paper>
  );
}
