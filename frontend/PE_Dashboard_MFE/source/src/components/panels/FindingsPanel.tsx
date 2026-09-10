import React, { useMemo, useState } from 'react';
import { Box, Button, CircularProgress, Paper, Typography } from '@material-ui/core';
import { generateFindings, getFinalJudgment, getPeNarrative, getRedFlags } from '../../api/dashboardApi';
import { useAppData } from '../../context/AppDataContext';
import { buildAnalysisPayload, buildFinalJudgmentPayload, buildPeNarrativePayload } from '../../utils/buildAnalysisPayload';
import { FinalJudgmentCard } from './FinalJudgmentCard';
import { PeReviewSummary } from './PeReviewSummary';
import { FindingsDataGrid, FindingItem } from '../shared/FindingsDataGrid';
import { WorkflowHeadroomCard } from '../shared/WorkflowHeadroomCard';
import { FindingsCrux } from '../shared/FindingsCrux';

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

type SeverityFilter = 'all' | 'critical' | 'warning' | 'info' | 'ok';

export function buildWorkflowItems(slaMatrix: any) {
  const summaries = slaMatrix?.workflow_summary || slaMatrix?.workflows || [];
  if (!Array.isArray(summaries)) return [];
  return summaries.map((w: any) => ({
    name: w.workflow_name ?? w.workflow ?? w.sub_application ?? w.Sub_Application ?? w.name ?? 'Workflow',
    runtime_h: Number(w.runtime_h ?? w.runtime_hours ?? w.peak_hrs ?? 0),
    sla_h: Number(w.sla_h ?? w.sla_hours ?? w.sla_hrs ?? w.sla_ceiling ?? 0),
    buffer_pct: Number(w.buffer_pct != null ? w.buffer_pct : 100 - (w.sla_used_pct || 0)),
    status: String(w.status || 'OK').toUpperCase(),
    source: w.data_src || w.source,
  }));
}

export function FindingsPanel() {
  const { data, setFindings, setFinalJudgment, setPeNarrative, setRedFlags } = useAppData();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [filter, setFilter] = useState<SeverityFilter>('all');
  const [ledgerOpen, setLedgerOpen] = useState(false);

  const handleGenerate = async () => {
    setBusy(true);
    setError(null);
    try {
      const payload = buildAnalysisPayload(data);
      const findings = await generateFindings(payload);
      setFindings(findings);

      let redFlags = data.redFlags;
      try {
        redFlags = await getRedFlags(payload);
        setRedFlags(redFlags);
      } catch {
        // Red flags refresh is optional
      }
      try {
        setPeNarrative(await getPeNarrative(buildPeNarrativePayload(data, { findings, redFlags })));
      } catch {
        // Narrative is optional
      }
      try {
        setFinalJudgment(await getFinalJudgment(buildFinalJudgmentPayload(data, { findings, redFlags })));
      } catch {
        // Final judgment is optional
      }
    } catch (generateError) {
      setError(generateError instanceof Error ? generateError.message : 'Generating findings failed.');
    } finally {
      setBusy(false);
    }
  };

  const findings = useMemo(() => (data.findings?.findings as FindingItem[]) || [], [data.findings]);
  const topAction = data.findings?.top_action as TopAction | undefined;
  const immediateAction = topAction?.text ? topAction : null;

  // Severity counts for filter tabs
  const counts = useMemo(() => {
    const c = { all: findings.length, critical: 0, warning: 0, info: 0, ok: 0 };
    findings.forEach((f) => {
      const lvl = f.level.toLowerCase() as keyof typeof c;
      if (lvl in c && lvl !== 'all') c[lvl]++;
    });
    return c;
  }, [findings]);

  // Extract workflow data for visual headroom bar
  const workflowItems = useMemo(() => {
    return buildWorkflowItems(data.slaMatrix);
  }, [data.slaMatrix]);

  // Failure-Resource Correlation Score, sourced from the executive-dashboard endpoint.
  const rfcs = data.executive?.rfcs as number | undefined;

  return (
    <Box p={2} pb={4}>
      {/* ═══ PAGE HEADER ═══ */}
      <Box display="flex" alignItems="center" justifyContent="space-between" flexWrap="wrap" style={{ gap: 12, marginBottom: 12 }}>
        <Box>
          <Typography variant="h5" style={{ fontWeight: 800, letterSpacing: '-.01em', color: '#f0f4ff' }}>
            PE Verdict &amp; Findings
          </Typography>
          <Typography variant="caption" color="textSecondary">
            Sign-off decision and the evidence behind it — synthesized across SOW, Batch Execution, Azure Infrastructure, and Benchmark telemetry
          </Typography>
        </Box>
        <Box display="flex" alignItems="center" style={{ gap: 8 }}>
          <Button
            variant="contained"
            color="primary"
            onClick={handleGenerate}
            disabled={busy}
            style={{ borderRadius: 10, fontWeight: 700 }}
          >
            {busy ? 'Analyzing…' : data.findings ? 'Regenerate Findings' : 'Generate Findings'}
          </Button>
          {busy && <CircularProgress size={22} aria-label="Generating findings" />}
        </Box>
      </Box>

      {error && (
        <Paper elevation={0} style={{ padding: '10px 14px', marginBottom: 12, borderRadius: 8, border: '1px solid rgba(244,63,94,.4)', background: 'rgba(244,63,94,.08)' }}>
          <Typography variant="body2" color="error">{error}</Typography>
        </Paper>
      )}

      {/* ═══ TIER 1: COMMAND CENTER (Decision, Score, Canonical Pillar Gauges, Ledger Table) ═══ */}
      <FinalJudgmentCard />

      {/* ═══ IMMEDIATE ACTION BANNER (Only when urgent action is required) ═══ */}
      {immediateAction && (
        <div className="pe-action-banner-v2" style={{ marginTop: 14 }}>
          <div className="pe-action-banner-v2__icon">⚡</div>
          <div style={{ flex: 1, minWidth: 0 }}>
            <Typography variant="caption" style={{ color: '#ff8aa5', fontWeight: 800, letterSpacing: '.1em', textTransform: 'uppercase', display: 'block', marginBottom: 3 }}>
              Immediate Action Required
            </Typography>
            <Typography variant="subtitle2" style={{ lineHeight: 1.45, fontWeight: 700, color: '#f0f4ff' }}>
              {immediateAction.recommendation || immediateAction.text}
            </Typography>
            {immediateAction.impact && (
              <Typography variant="body2" color="textSecondary" style={{ marginTop: 4, fontSize: 13 }}>
                Why now: {immediateAction.impact}
              </Typography>
            )}
          </div>
        </div>
      )}

      {/* ═══ VISUAL WORKFLOW HEADROOM & UTILIZATION BAR ═══ */}
      {workflowItems.length > 0 && <WorkflowHeadroomCard workflows={workflowItems} />}

      {/* ═══ TIER 2: TABBED SYNTHESIZED PILLAR EVIDENCE & QUESTIONNAIRE ═══ */}
      <PeReviewSummary />

      {data.findings && counts.all > 0 && (
        <FindingsCrux
          findings={findings}
          counts={counts}
          rfcs={rfcs}
          onViewAll={() => { setFilter('critical'); setLedgerOpen(true); }}
        />
      )}

      {/* ═══ TIER 3: FULL LEDGER — opt-in, so the crux above stays the headline ═══ */}
      {!data.findings ? (
        <Paper
          elevation={0}
          style={{ marginTop: 14, padding: 24, textAlign: 'center', borderRadius: 12, border: '1px dashed #213060', background: 'transparent' }}
        >
          <Typography variant="body2" color="textSecondary">
            Upload batch, resource, and SLA data, then generate findings to populate this ledger.
          </Typography>
        </Paper>
      ) : (
        <Box style={{ marginTop: 14 }}>
          <button
            type="button"
            onClick={() => setLedgerOpen((v) => !v)}
            aria-expanded={ledgerOpen}
            style={{
              all: 'unset', cursor: 'pointer', display: 'flex', alignItems: 'center', gap: 8,
              fontSize: 12, fontWeight: 700, color: '#91a7d8', padding: '6px 2px',
            }}
          >
            <span style={{ transform: ledgerOpen ? 'rotate(90deg)' : 'none', transition: 'transform .15s' }}>▸</span>
            {ledgerOpen ? 'Hide' : 'Show'} full findings ledger ({counts.all})
          </button>
          {ledgerOpen && (
            <FindingsDataGrid
              findings={findings}
              filter={filter}
              onFilterChange={setFilter}
              counts={counts}
            />
          )}
        </Box>
      )}
    </Box>
  );
}
