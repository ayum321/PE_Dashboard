import React, { useState } from 'react';
import { Box, Button, CircularProgress, Paper, Typography, makeStyles } from '@material-ui/core';
import { generateFindings } from '../../api/dashboardApi';
import { useAppData } from '../../context/AppDataContext';
import { buildAnalysisPayload } from '../../utils/buildAnalysisPayload';
import { KpiStatCard } from '../shared/KpiStatCard';

interface Finding {
  level: string;
  text: string;
  sub?: string;
  impact?: string;
  recommendation?: string;
  evidence?: string;
  root_cause?: string;
  confidence?: number;
  source?: string;
}

interface DataCoverage {
  batch: boolean;
  resource: boolean;
  sla: boolean;
  benchmark: boolean;
  sow: boolean;
}

const useStyles = makeStyles((theme) => ({
  panel: { padding: theme.spacing(3) },
  row: { display: 'flex', gap: theme.spacing(2), alignItems: 'center', marginTop: theme.spacing(2) },
  finding: { padding: theme.spacing(1.5), marginTop: theme.spacing(1.5) },
  empty: { marginTop: theme.spacing(2) },
}));

const LEVEL_COLOR: Record<string, 'error' | 'textSecondary' | 'primary' | undefined> = {
  critical: 'error',
  warning: 'primary',
  info: 'textSecondary',
  ok: undefined,
};

export function FindingsPanel() {
  const classes = useStyles();
  const { data, setFindings } = useAppData();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleGenerate = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await generateFindings(buildAnalysisPayload(data));
      setFindings(result);
    } catch (generateError) {
      setError(generateError instanceof Error ? generateError.message : 'Generating findings failed.');
    } finally {
      setBusy(false);
    }
  };

  const findings = (data.findings?.findings as Finding[]) || [];
  const summary = data.findings?.summary as { critical?: number; warning?: number; total?: number } | undefined;
  const dataCoverage = data.findings?.data_coverage as DataCoverage | undefined;

  return (
    <Paper className={`${classes.panel} kpi-card`} elevation={0}>
      <Typography variant="h6">PE Findings</Typography>
      <Box className={classes.row}>
        <Button variant="contained" color="primary" onClick={handleGenerate} disabled={busy}>
          Generate Findings
        </Button>
        {busy && <CircularProgress size={22} aria-label="Generating findings" />}
      </Box>
      {error && <Typography variant="body2" color="error">{error}</Typography>}

      {!data.findings ? (
        <Typography className={classes.empty} variant="body2" color="textSecondary">
          Upload batch and resource data first, then generate findings from the collected evidence.
        </Typography>
      ) : (
        <>
          <Box style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))', gap: 12, marginTop: 12 }}>
            {typeof data.findings.findings_grade === 'string' && data.findings.findings_grade && (
              <KpiStatCard label="Findings Grade" value={String(data.findings.findings_grade)} sub={String(data.findings.findings_grade_label || '')} accent="#a855f7" />
            )}
            <KpiStatCard label="Critical" value={summary?.critical || 0} accent="#f43f5e" />
            <KpiStatCard label="Warning" value={summary?.warning || 0} accent="#f59e0b" />
            <KpiStatCard label="Total" value={summary?.total || 0} accent="#3b82f6" />
          </Box>
          {dataCoverage && (
            <Box style={{ display: 'flex', gap: 6, flexWrap: 'wrap', marginTop: 12 }}>
              {(['batch', 'resource', 'sla', 'benchmark', 'sow'] as const).map((pillar) => (
                <span key={pillar} className={`metric-badge ${dataCoverage[pillar] ? 'metric-badge-green' : 'metric-badge-blue'}`}>
                  {pillar.toUpperCase()} {dataCoverage[pillar] ? '✓' : '—'}
                </span>
              ))}
            </Box>
          )}
          {findings.map((finding, index) => (
            <Paper key={index} className={`${classes.finding} insight-card ${finding.level}`} elevation={0}>
              <Typography variant="subtitle2" color={LEVEL_COLOR[finding.level]}>
                {finding.level.toUpperCase()}: {finding.text}
              </Typography>
              {finding.impact && <Typography variant="body2">Impact: {finding.impact}</Typography>}
              {finding.recommendation && (
                <Typography variant="body2" color="textSecondary">Action: {finding.recommendation}</Typography>
              )}
              {finding.evidence && (
                <Typography variant="caption" style={{ display: 'block', color: '#6b7db3', marginTop: 4 }}>Evidence: {finding.evidence}</Typography>
              )}
              {finding.root_cause && (
                <Typography variant="caption" style={{ display: 'block', color: '#6b7db3' }}>Root cause: {finding.root_cause}</Typography>
              )}
              {(finding.source || finding.confidence != null) && (
                <Typography variant="caption" style={{ display: 'block', color: '#6b7db3', marginTop: 4 }}>
                  {finding.source && `Source: ${finding.source}`}{finding.source && finding.confidence != null && ' · '}
                  {finding.confidence != null && `Confidence: ${finding.confidence}%`}
                </Typography>
              )}
            </Paper>
          ))}
        </>
      )}
    </Paper>
  );
}
