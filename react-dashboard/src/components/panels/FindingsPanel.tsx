import React, { useMemo, useState } from 'react';
import { Box, Button, CircularProgress, Paper, Typography, makeStyles } from '@material-ui/core';
import { generateFindings, getFinalJudgment, getPeNarrative, getRedFlags } from '../../api/dashboardApi';
import { useAppData } from '../../context/AppDataContext';
import { buildAnalysisPayload, buildFinalJudgmentPayload, buildPeNarrativePayload } from '../../utils/buildAnalysisPayload';
import { KpiStatCard } from '../shared/KpiStatCard';
import { FinalJudgmentCard } from './FinalJudgmentCard';
import { PeReviewSummary } from './PeReviewSummary';

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

interface TopAction {
  rank?: number;
  severity?: string;
  text?: string;
  impact?: string;
  recommendation?: string;
  evidence?: string;
  source?: string;
  root_cause?: string;
}

const useStyles = makeStyles((theme) => ({
  panel: { padding: theme.spacing(3) },
  row: { display: 'flex', gap: theme.spacing(2), alignItems: 'center', marginTop: theme.spacing(2) },
  finding: { padding: theme.spacing(1.5), marginTop: theme.spacing(1.5) },
  empty: { marginTop: theme.spacing(2) },
}));

export function FindingsPanel() {
  const classes = useStyles();
  const { data, setFindings, setFinalJudgment, setPeNarrative, setRedFlags } = useAppData();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleGenerate = async () => {
    setBusy(true);
    setError(null);
    try {
      const payload = buildAnalysisPayload(data);
      const findings = await generateFindings(payload);
      setFindings(findings);

      // Mirror the local dashboard: findings refresh produces the supporting
      // red-flag evidence and then refreshes the one final PE decision.
      let redFlags = data.redFlags;
      try {
        redFlags = await getRedFlags(payload);
        setRedFlags(redFlags);
      } catch {
        // Existing red flags remain valid when the optional refresh fails.
      }
      try {
        setPeNarrative(await getPeNarrative(buildPeNarrativePayload(data, { findings, redFlags })));
      } catch {
        // The flat deterministic findings remain usable if narrative enrichment is unavailable.
      }
      try {
        setFinalJudgment(await getFinalJudgment(buildFinalJudgmentPayload(data, { findings, redFlags })));
      } catch {
        // Findings are still a complete deterministic result if judgment is unavailable.
      }
    } catch (generateError) {
      setError(generateError instanceof Error ? generateError.message : 'Generating findings failed.');
    } finally {
      setBusy(false);
    }
  };

  const findings = useMemo(() => (data.findings?.findings as Finding[]) || [], [data.findings]);
  const summary = data.findings?.summary as { critical?: number; warning?: number; total?: number } | undefined;
  const dataCoverage = data.findings?.data_coverage as DataCoverage | undefined;
  // This is intentionally server-ranked: the hero must not change priority
  // locally when a user refreshes, filters, or receives additional findings.
  const topAction = data.findings?.top_action as TopAction | undefined;
  const orderedFindings = useMemo(() => {
    const severity = (level: string) => ({ critical: 0, warning: 1, info: 2, ok: 3 }[level.toLowerCase()] ?? 4);
    return [...findings].sort((a, b) => severity(a.level) - severity(b.level));
  }, [findings]);
  const immediateAction = topAction?.text ? topAction : null;
  const priorityFindings = orderedFindings.filter((finding) => ['critical', 'warning'].includes(finding.level.toLowerCase()));
  const supportingFindings = orderedFindings.filter((finding) => !['critical', 'warning'].includes(finding.level.toLowerCase()));

  const renderFinding = (finding: Finding, index: number) => {
    const level = finding.level.toLowerCase();
    return (
      <Paper key={`${level}-${index}-${finding.text}`} className={`${classes.finding} insight-card ${level} pe-finding-card`} elevation={0}>
        <Box className="pe-finding-card__header">
          <span className={`pe-finding-card__severity ${level}`}>{level.toUpperCase()}</span>
          <Typography component="span" className="pe-finding-card__title">{finding.text}</Typography>
        </Box>
        {finding.impact && <Typography component="p" className="pe-finding-card__impact">{finding.impact}</Typography>}
        {finding.recommendation && finding.text !== immediateAction?.text && (
          <Typography component="p" className="pe-finding-card__action"><strong>Recommended action:</strong> {finding.recommendation}</Typography>
        )}
        {(finding.evidence || finding.root_cause || finding.source || finding.confidence != null) && (
          <details className="pe-finding-evidence">
            <summary>View evidence and provenance</summary>
            {finding.evidence && <Typography component="p">Evidence: {finding.evidence}</Typography>}
            {finding.root_cause && <Typography component="p">Root cause: {finding.root_cause}</Typography>}
            {(finding.source || finding.confidence != null) && <Typography component="p">{finding.source && `Source: ${finding.source}`}{finding.source && finding.confidence != null && ' · '}{finding.confidence != null && `Confidence: ${finding.confidence}%`}</Typography>}
          </details>
        )}
      </Paper>
    );
  };

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
      <PeReviewSummary />

      {!data.findings ? (
        <>
          <Typography className={classes.empty} variant="body2" color="textSecondary">
            Upload batch and resource data first, then generate findings from the collected evidence.
          </Typography>
          <FinalJudgmentCard />
        </>
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
          {immediateAction && (
            <Box className="pe-immediate-action" style={{ marginTop: 16 }}>
              <Typography variant="caption" className="pe-immediate-action__eyebrow">Immediate PE action</Typography>
              <Typography variant="subtitle2">{immediateAction.recommendation || immediateAction.text}</Typography>
              {immediateAction.impact && <Typography variant="body2" color="textSecondary">Why now: {immediateAction.impact}</Typography>}
            </Box>
          )}
          <Box className="pe-findings-list" aria-label="Priority PE findings">
            {priorityFindings.length > 0 ? (
              <Typography className="pe-findings-list__heading">Priority findings — action required</Typography>
            ) : (
              <Typography className="pe-findings-list__heading">No critical or warning findings</Typography>
            )}
            {priorityFindings.map(renderFinding)}
          </Box>
          {supportingFindings.length > 0 && (
            <details className="pe-supporting-findings">
              <summary>{supportingFindings.length} supporting observation{supportingFindings.length === 1 ? '' : 's'} — informational and healthy evidence</summary>
              <Box className="pe-findings-list">{supportingFindings.map(renderFinding)}</Box>
            </details>
          )}
          <FinalJudgmentCard />
        </>
      )}
    </Paper>
  );
}
