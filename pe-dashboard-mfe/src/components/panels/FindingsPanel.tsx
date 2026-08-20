import React, { useState } from 'react';
import { Box, Button, CircularProgress, Paper, Typography, makeStyles } from '@material-ui/core';
import { generateFindings } from '../../api/dashboardApi';
import { useAppData } from '../../context/AppDataContext';
import { buildAnalysisPayload } from '../../utils/buildAnalysisPayload';

interface Finding {
  level: string;
  text: string;
  sub?: string;
  impact?: string;
  recommendation?: string;
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

  return (
    <Paper className={classes.panel} elevation={0}>
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
          {summary && (
            <Typography variant="body2" style={{ marginTop: 8 }}>
              {summary.critical || 0} critical, {summary.warning || 0} warning, {summary.total || 0} total.
            </Typography>
          )}
          {findings.map((finding, index) => (
            <Paper key={index} className={classes.finding} variant="outlined">
              <Typography variant="subtitle2" color={LEVEL_COLOR[finding.level]}>
                {finding.level.toUpperCase()}: {finding.text}
              </Typography>
              {finding.impact && <Typography variant="body2">Impact: {finding.impact}</Typography>}
              {finding.recommendation && (
                <Typography variant="body2" color="textSecondary">Action: {finding.recommendation}</Typography>
              )}
            </Paper>
          ))}
        </>
      )}
    </Paper>
  );
}
