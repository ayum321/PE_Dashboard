import React, { useState } from 'react';
import {
  Box,
  Button,
  CircularProgress,
  Typography,
  Chip
} from '@material-ui/core';
import { getFinalJudgment } from '../../api/dashboardApi';
import { useAppData } from '../../context/AppDataContext';
import { buildFinalJudgmentPayload } from '../../utils/buildAnalysisPayload';
import { DecisionBadge } from '../shared/DecisionBadge';
import { ScoreRing } from '../shared/ScoreRing';
import { PillarWaterfallChart } from '../shared/PillarWaterfallChart';
import { PillarRadarChart } from '../shared/PillarRadarChart';

const DECISION_COLOR: Record<string, string> = {
  GO: '#10d96e',
  GO_WITH_NOTES: '#3b82f6',
  HOLD: '#f59e0b',
  BLOCKED: '#f43f5e',
  REMEDIATE: '#f43f5e',
  INSUFFICIENT_DATA: '#6b7db3',
};

export function FinalJudgmentCard() {
  const { data, setFinalJudgment } = useAppData();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [breakdownOpen, setBreakdownOpen] = useState(false);
  
  const result = data.finalJudgment;
  const decision = String(result?.decision || 'INSUFFICIENT_DATA');
  const color = DECISION_COLOR[decision] || '#6b7db3';
  const pillars = (result?.pillars as Record<string, number>) || {};
  const pillarWeights = (result?.pillar_weights as Record<string, number>) || {};
  const pillarContributions = (result?.pillar_contributions as Record<string, number>) || {};
  const pillarStatuses = (result?.pillar_statuses as Record<string, string>) || {};
  const missingPillars = (result?.missing_pillars as string[]) || [];
  const evidenceCoverage = Number(result?.evidence_coverage_pct || 0);
  const compositeScore = Number(result?.score || 0);

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

  if (!result) {
    return (
      <Box style={{ marginTop: 16, padding: 24, borderRadius: 14, border: '1px solid #21306055', background: 'rgba(9,14,31,.5)', textAlign: 'center' }}>
        <Typography variant="body2" color="textSecondary">
          Generate PE Findings, or run judgment directly, once evidence is loaded.
        </Typography>
        <Button variant="contained" color="primary" onClick={run} disabled={busy} style={{ marginTop: 12, borderRadius: 8, fontWeight: 700 }}>
          {busy ? 'Computing…' : 'Run Final Judgment'}
        </Button>
        {busy && <CircularProgress size={22} aria-label="Computing final judgment" style={{ marginTop: 10, display: 'block', marginLeft: 'auto', marginRight: 'auto' }} />}
        {error && <Typography variant="body2" color="error" style={{ marginTop: 8 }}>{error}</Typography>}
      </Box>
    );
  }

  return (
    <Box style={{ marginTop: 14 }}>
      {error && <Typography variant="body2" color="error" style={{ marginBottom: 8 }}>{error}</Typography>}

      {/* ── Command Center Hero Strip ── */}
      <div className="pe-command-center" style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '12px 20px', background: '#0d1526', borderRadius: 12, border: '1px solid #213060', minHeight: 80 }}>
        
        <Box display="flex" alignItems="center" style={{ gap: 24 }}>
          {/* Left: Decision Badge */}
          <DecisionBadge
            decision={decision}
            reason={String(result.verdict_reason || result.verdict || '')}
            compact={true}
          />

          {/* Center-left: Score Ring + Grade */}
          <Box display="flex" alignItems="center" style={{ gap: 12 }}>
            <ScoreRing
              value={result.score == null ? 0 : Number(result.score)}
              size={52}
              strokeWidth={5}
              color={color}
              label={result.score == null ? '—' : `${Number(result.score).toFixed(0)}`}
            />
            <Box>
              <Typography style={{ fontSize: 28, fontWeight: 800, color: '#a855f7', lineHeight: 1, fontFamily: "'JetBrains Mono', monospace" }}>
                {String(result.grade || '—')}
              </Typography>
            </Box>
          </Box>

          {/* Center: 4 Pillar Health Rings */}
          <Box display="flex" alignItems="center" style={{ gap: 16 }}>
            {Object.entries(pillars).map(([name, score]) => {
              const s = Number(score);
              const status = String(pillarStatuses[name] || (s >= 90 ? 'PASS' : s >= 60 ? 'WATCH' : 'FAIL'));
              const pColor = status === 'BLOCKED' || status === 'FAIL' ? '#f43f5e' : status === 'WATCH' ? '#f59e0b' : '#10d96e';
              return (
                <Box key={name} display="flex" flexDirection="column" alignItems="center" title={`${name.toUpperCase()}: ${s.toFixed(1)}% · ${status}`}>
                  <ScoreRing value={s} size={32} strokeWidth={3} color={pColor} label={`${Math.round(s)}`} />
                  <Typography style={{ fontSize: 9, fontWeight: 700, marginTop: 4, color: '#6b7db3', textTransform: 'uppercase' }}>
                    {name}
                  </Typography>
                </Box>
              );
            })}
          </Box>
        </Box>

        <Box display="flex" alignItems="center" style={{ gap: 24 }}>
          {/* Center-right: Evidence Coverage */}
          <Box textAlign="right">
            <Typography style={{ fontSize: 18, fontWeight: 700, color: '#3b82f6', lineHeight: 1, fontFamily: "'JetBrains Mono', monospace" }}>
              {evidenceCoverage.toFixed(0)}%
            </Typography>
            <Typography style={{ fontSize: 10, color: '#6b7db3', marginTop: 4, textTransform: 'uppercase' }}>
              Coverage
            </Typography>
          </Box>

          {/* Right: Refresh button */}
          <Box display="flex" alignItems="center" style={{ gap: 10 }}>
            <Button variant="contained" color="primary" onClick={run} disabled={busy} size="small" style={{ borderRadius: 8, fontWeight: 700 }}>
              {busy ? 'Computing…' : 'Refresh'}
            </Button>
            {busy && <CircularProgress size={18} />}
          </Box>
        </Box>
      </div>

      {Number(result.critical_findings || 0) > 0 && (
        <Typography variant="caption" style={{ color: '#f43f5e', fontWeight: 700, textAlign: 'center', display: 'block', marginTop: 8 }}>
          ⚠ Sign-off gate: {Number(result.critical_findings)} unresolved critical finding(s)
        </Typography>
      )}

      {missingPillars.length > 0 && (
        <Box display="flex" justifyContent="center" mt={1}>
          <Chip
            label={`Missing evidence: ${missingPillars.map(name => name.toUpperCase()).join(', ')}`}
            size="small"
            style={{ backgroundColor: 'rgba(245, 158, 11, 0.15)', color: '#f59e0b', fontWeight: 600, border: '1px solid rgba(245, 158, 11, 0.3)' }}
          />
        </Box>
      )}

      {/* Score Breakdown Section */}
      <Box style={{ marginTop: 12, border: '1px solid rgba(59,130,246,.2)', borderRadius: 8, overflow: 'hidden' }}>
        <Button
          fullWidth
          onClick={() => setBreakdownOpen((open) => !open)}
          style={{ justifyContent: 'space-between', color: '#91a7d8', padding: '8px 12px', textTransform: 'none' }}
        >
          <span>Score Breakdown</span>
          <span>{breakdownOpen ? '−' : '+'}</span>
        </Button>
        {breakdownOpen && (
          <Box display="flex" style={{ padding: '16px', gap: 16, backgroundColor: 'rgba(9, 14, 31, 0.5)' }}>
            <Box flex={1}>
              <PillarWaterfallChart
                pillars={pillars}
                pillarWeights={pillarWeights}
                pillarContributions={pillarContributions}
                compositeScore={compositeScore}
              />
            </Box>
            <Box flex={1}>
              <PillarRadarChart pillars={pillars} size={250} />
            </Box>
          </Box>
        )}
      </Box>
    </Box>
  );
}
