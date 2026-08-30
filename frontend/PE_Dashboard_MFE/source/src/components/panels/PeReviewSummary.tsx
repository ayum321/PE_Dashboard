import React, { useState } from 'react';
import {
  Box,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
} from '@material-ui/core';
import { useAppData } from '../../context/AppDataContext';
import { DecisionBadge } from '../shared/DecisionBadge';

type NarrativeTable = { headers?: unknown[]; rows?: unknown[][] };
type NarrativeKpi = { label?: string; value?: unknown; sub?: string; tone?: string; binding?: boolean };
type NarrativePanel = {
  verdict?: { status?: string; tone?: string; headline?: string };
  kpis?: NarrativeKpi[];
  explainer?: string;
  direction?: string;
};
type NarrativeSection = {
  id?: string;
  title?: string;
  prose?: string;
  table?: NarrativeTable;
  table_caption?: string;
  panel?: NarrativePanel;
  provenance?: { label?: string; note?: string; tone?: string };
};
type RedFlag = { id?: string; category?: string; context?: string; question?: string; risk?: string; data_point?: string };
type BackendUat = {
  available?: boolean;
  evidence_type?: string;
  severity?: string;
  question?: string;
  transactions?: number;
  degraded?: number;
  sla_breaches?: number;
  comparable_jobs?: number;
  regressions?: number;
};

const SECTION_STYLE: Record<string, { accent: string; number: number; icon: string; label: string }> = {
  data_volume: { accent: '#22d3ee', number: 1, icon: '📦', label: 'Data Volume (SOW)' },
  batch_sla: { accent: '#f59e0b', number: 2, icon: '⚙️', label: 'Batch & SLA' },
  infrastructure: { accent: '#a855f7', number: 3, icon: '🖥️', label: 'Infrastructure' },
  uat: { accent: '#10d96e', number: 4, icon: '🧪', label: 'Benchmark / UAT' },
};

const QUESTION_SECTION: Record<string, string> = {
  Volume: 'data_volume',
  Batch: 'batch_sla',
  Scheduling: 'batch_sla',
  'SLA & Scheduling': 'batch_sla',
  'SLA-Matrix': 'batch_sla',
  'Runtime & Regression': 'batch_sla',
  'Execution Failures': 'batch_sla',
  Correlation: 'batch_sla',
  CPU: 'infrastructure',
  Memory: 'infrastructure',
  Infrastructure: 'infrastructure',
  Testing: 'uat',
  DR: 'uat',
  Monitoring: 'uat',
  Governance: 'uat',
  Approval: 'uat',
  UAT: 'uat',
  UI: 'uat',
  'UI Performance': 'uat',
  'Batch Performance': 'uat',
  Performance: 'uat',
};

const TONE_COLOR: Record<string, string> = {
  ok: '#10d96e',
  warn: '#f59e0b',
  crit: '#f43f5e',
  critical: '#f43f5e',
  high: '#f59e0b',
  medium: '#3b82f6',
  low: '#10d96e',
};

const text = (value: unknown): string => (value === undefined || value === null || value === '' ? '—' : String(value));

/** Tabbed Synthesized View of the 4 Evidence Pillars. */
export function PeReviewSummary() {
  const { data } = useAppData();
  const [activeTab, setActiveTab] = useState<string>('data_volume');
  const [expandedProseTabs, setExpandedProseTabs] = useState<Record<string, boolean>>({});
  const [expandedQuestionsTabs, setExpandedQuestionsTabs] = useState<Record<string, boolean>>({});

  const narrative = data.peNarrative;
  if (!narrative) return null;

  const sections = (narrative.sections as NarrativeSection[]) || [];
  const flags = (data.redFlags?.flags as RedFlag[]) || [];
  const backendUat = data.findings?.uat as BackendUat | undefined;
  const finalJudgment = data.finalJudgment as { decision?: string; verdict?: string; verdict_reason?: string; pillars?: Record<string, number> } | null;
  const pillarScores = (finalJudgment?.pillars as Record<string, number>) || {};

  // The narrative's own verdict/summary text is generated ahead of final
  // judgment scoring and can go stale the moment findings/final-judgment
  // are recomputed (e.g. a new critical finding lands after the narrative
  // was drafted). Final judgment's decision is the single source of truth
  // for sign-off status, so whenever it's present it takes over the
  // top-level verdict display entirely — the narrative's own summary line
  // is never rendered here once an authoritative decision exists.
  const authoritativeDecision = finalJudgment?.decision ? String(finalJudgment.decision) : null;
  const authoritativeReason = String(finalJudgment?.verdict_reason || finalJudgment?.verdict || '');

  // UAT evidence detection
  const narrativeHasUatEvidence = sections.some((section) => {
    if ((section.id || '').toLowerCase() !== 'uat') return false;
    const evidenceText = [
      section.prose,
      section.provenance?.label,
      section.provenance?.note,
      section.table_caption,
      ...(section.table?.headers || []).map(text),
      ...((section.table?.rows || []).flatMap((row) => (Array.isArray(row) ? row : [row])).map(text)),
    ].join(' ').trim().toLowerCase();
    const hasStructuredEvidence = Boolean(section.panel || section.table?.rows?.length);
    return (hasStructuredEvidence || Boolean(evidenceText))
      && !/^(n\/?a|not loaded|no uat|no ui|missing evidence)[\s.!-]*$/.test(evidenceText)
      && !/no (uat|ui|performance) (evidence|document|benchmark|data)|uat (evidence|data) (is )?not (loaded|available)/.test(evidenceText);
  });
  const hasBackendUatEvidence = backendUat?.available === true;
  const hasUatEvidence = hasBackendUatEvidence || narrativeHasUatEvidence;
  const backendUatSection: NarrativeSection | null = hasBackendUatEvidence ? {
    id: 'uat',
    title: 'UAT Validation',
    prose: backendUat.question,
    provenance: {
      label: `Evidence: ${text(backendUat.evidence_type).replace(/_/g, ' ')}`,
      tone: backendUat.severity,
    },
    table: {
      headers: ['Comparable', 'Degraded / regressed', 'SLA breaches'],
      rows: [[
        backendUat.comparable_jobs ?? backendUat.transactions ?? '—',
        backendUat.regressions ?? backendUat.degraded ?? '—',
        backendUat.sla_breaches ?? '—',
      ]],
    },
  } : null;
  const hasNarrativeUatSection = sections.some((section) => (section.id || '').toLowerCase() === 'uat' && narrativeHasUatEvidence);
  const sectionSource = hasBackendUatEvidence && !hasNarrativeUatSection
    ? [...sections.filter((section) => (section.id || '').toLowerCase() !== 'uat'), backendUatSection!]
    : sections;
  const visibleSections = sectionSource.filter((section) => (section.id || '').toLowerCase() !== 'uat' || hasUatEvidence);

  // Helper: get questions for a section
  const getQuestions = (sectionId: string) =>
    flags
      .filter((flag) => QUESTION_SECTION[flag.category || ''] === sectionId)
      .sort((a, b) => ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'].indexOf(a.risk || '') - ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'].indexOf(b.risk || ''))
      .slice(0, sectionId === 'batch_sla' ? 5 : 3);

  // Pillar scores & status
  const getPillarStatus = (sectionId: string) => {
    const pillarKey = { data_volume: 'sow', batch_sla: 'batch', infrastructure: 'resource', uat: 'benchmark' }[sectionId] || sectionId;
    const score = pillarScores[pillarKey];
    if (score == null) return { score: null, label: 'LOADED', color: '#3b82f6' };
    if (score < 60) return { score, label: 'BLOCKED', color: '#f43f5e' };
    if (score < 90) return { score, label: 'REVIEW', color: '#f59e0b' };
    return { score, label: 'PASS', color: '#10d96e' };
  };

  const currentSection = visibleSections.find((s) => (s.id || '') === activeTab) || visibleSections[0];
  const currentStyle = SECTION_STYLE[currentSection?.id || ''] || { accent: '#3b82f6', number: 1, icon: '📋', label: 'Section' };
  const currentQuestions = currentSection ? getQuestions(currentSection.id || '') : [];
  const currentTable = currentSection?.table || {};
  const headers = currentTable.headers || [];
  const rows = currentTable.rows || [];

  const isInfra = currentSection?.id === 'infrastructure';
  const peakIndex = headers.findIndex((h) => String(h).toUpperCase().includes('PEAK'));
  const thresholdIndex = headers.findIndex((h) => String(h).toUpperCase().includes('THRESHOLD'));

  const renderTableCell = (cell: unknown, ci: number, row: unknown[]) => {
    let content = <>{text(cell)}</>;

    if (isInfra && ci === peakIndex && thresholdIndex !== -1) {
      const peakStr = String(cell);
      const threshStr = String(row[thresholdIndex]);
      const peakNum = parseFloat(peakStr.replace(/[^0-9.]/g, ''));
      const threshNum = parseFloat(threshStr.replace(/[^0-9.]/g, ''));
      
      if (!isNaN(peakNum) && !isNaN(threshNum) && threshNum > 0) {
        const pct = Math.min(100, Math.max(0, (peakNum / threshNum) * 100));
        const color = pct < 60 ? '#10d96e' : pct <= 80 ? '#f59e0b' : '#f43f5e';
        
        content = (
          <Box display="flex" alignItems="center" style={{ gap: 6, minWidth: 100 }}>
            <span>{text(cell)}</span>
            <Box className="pe-inline-gauge" style={{ width: 60 }}>
              <Box className="pe-inline-gauge__bar">
                <Box className="pe-inline-gauge__fill" style={{ width: `${pct}%`, background: color }} />
              </Box>
            </Box>
          </Box>
        );
      }
    }
    
    return <TableCell key={ci} style={{ fontSize: 12 }}>{content}</TableCell>;
  };

  return (
    <Box className="kpi-card" style={{ padding: '16px 20px', borderRadius: 14, marginTop: 14 }}>
      {/* ── Pillar Tab Switcher ── */}
      <Box display="flex" justifyContent="space-between" alignItems="center" flexWrap="wrap" style={{ gap: 10, borderBottom: '1px solid rgba(33, 48, 96, .6)', paddingBottom: 12 }}>
        <Box display="flex" flexWrap="wrap" style={{ gap: 8 }} role="tablist">
          {visibleSections.map((sec) => {
            const secId = sec.id || '';
            const style = SECTION_STYLE[secId] || { accent: '#3b82f6', number: 1, icon: '📋', label: sec.title || 'Pillar' };
            const stat = getPillarStatus(secId);
            const isActive = activeTab === secId;

            return (
              <button
                key={secId}
                type="button"
                role="tab"
                aria-selected={isActive}
                onClick={() => setActiveTab(secId)}
                style={{
                  all: 'unset',
                  cursor: 'pointer',
                  display: 'inline-flex',
                  alignItems: 'center',
                  gap: 8,
                  padding: '8px 14px',
                  borderRadius: 10,
                  background: isActive ? `${style.accent}18` : 'rgba(6, 12, 26, .5)',
                  border: `1px solid ${isActive ? style.accent : 'rgba(33, 48, 96, .6)'}`,
                  color: isActive ? '#f0f4ff' : '#91a7d8',
                  fontWeight: isActive ? 800 : 600,
                  fontSize: 13,
                  transition: 'all .15s ease',
                  boxShadow: isActive ? `0 0 16px ${style.accent}22` : 'none',
                }}
              >
                <span>{style.icon}</span>
                <span>{style.label}</span>
                {stat.score != null && (
                  <span
                    style={{
                      fontFamily: "'JetBrains Mono', monospace",
                      fontSize: 11,
                      padding: '1px 6px',
                      borderRadius: 4,
                      background: `${stat.color}22`,
                      color: stat.color,
                      fontWeight: 800,
                    }}
                  >
                    {stat.score.toFixed(0)}%
                  </span>
                )}
              </button>
            );
          })}
        </Box>
      </Box>

      {/* ── Authoritative Final Decision ──
          Rendered independent of pillar-tab content (even with zero
          sections) because it reflects the sign-off gate, not a
          per-pillar narrative — see comment above authoritativeDecision. */}
      {authoritativeDecision && (
        <Box
          display="flex"
          alignItems="center"
          flexWrap="wrap"
          style={{
            gap: 12, marginTop: 14, padding: '10px 14px', borderRadius: 8,
            border: '1px solid rgba(33, 48, 96, .6)', background: 'rgba(6, 12, 26, .5)',
          }}
        >
          <DecisionBadge decision={authoritativeDecision} reason={authoritativeReason} compact />
          {authoritativeReason && (
            <Typography variant="body2" style={{ color: '#c7d4ed', fontWeight: 600 }}>
              {authoritativeReason}
            </Typography>
          )}
        </Box>
      )}

      {/* ── Active Pillar Synthesized Content ── */}
      {currentSection && (
        <Box style={{ marginTop: 14 }}>
          {/* Header & Verdict */}
          <Box display="flex" justifyContent="space-between" alignItems="flex-start" flexWrap="wrap" style={{ gap: 12, marginBottom: 12 }}>
            <Box>
              <Typography variant="h6" style={{ fontWeight: 800, color: '#f0f4ff' }}>
                {currentStyle.icon} {text(currentSection.title)}
              </Typography>
              {currentSection.provenance && (
                <Typography variant="caption" style={{ color: TONE_COLOR[currentSection.provenance.tone || ''] || '#6b7db3', fontWeight: 700 }}>
                  {text(currentSection.provenance.label)} {currentSection.provenance.note ? `· ${currentSection.provenance.note}` : ''}
                </Typography>
              )}
            </Box>
          </Box>

          {/* Verdict Banner */}
          {currentSection.panel?.verdict?.headline && (
            <Box
              style={{
                borderLeft: `4px solid ${TONE_COLOR[currentSection.panel.verdict.tone || ''] || currentStyle.accent}`,
                background: `${TONE_COLOR[currentSection.panel.verdict.tone || ''] || currentStyle.accent}14`,
                padding: '10px 14px',
                borderRadius: '0 8px 8px 0',
                marginBottom: 12,
              }}
            >
              <Typography variant="caption" style={{ color: TONE_COLOR[currentSection.panel.verdict.tone || ''] || currentStyle.accent, fontWeight: 800, letterSpacing: '.06em' }}>
                {text(currentSection.panel.verdict.status)}
              </Typography>
              <Typography variant="body2" style={{ fontWeight: 700, marginTop: 2, color: '#f0f4ff' }}>
                {text(currentSection.panel.verdict.headline)}
              </Typography>
            </Box>
          )}

          {/* KPIs Grid */}
          {currentSection.panel?.kpis && currentSection.panel.kpis.length > 0 && (
            <Box style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(130px, 1fr))', gap: 10, marginBottom: 12 }}>
              {currentSection.panel.kpis.map((kpi, i) => (
                <Box key={i} style={{ padding: '10px 12px', borderRadius: 8, border: '1px solid rgba(33, 48, 96, .6)', background: 'rgba(6, 12, 26, .5)' }}>
                  <Typography variant="caption" style={{ color: '#6b7db3', fontWeight: 800, textTransform: 'uppercase', letterSpacing: '.06em', fontSize: 9.5 }}>
                    {text(kpi.label)}
                  </Typography>
                  <Typography style={{ fontSize: 22, fontWeight: 800, color: TONE_COLOR[kpi.tone || ''] || currentStyle.accent, marginTop: 2, fontFamily: "'JetBrains Mono', monospace" }}>
                    {text(kpi.value)}
                  </Typography>
                  {kpi.sub && <Typography variant="caption" style={{ color: '#6b7db3', fontSize: 10 }}>{kpi.sub}</Typography>}
                </Box>
              ))}
            </Box>
          )}

          {/* Prose */}
          {currentSection.prose && (
            <Box style={{ marginBottom: 12 }}>
              {expandedProseTabs[currentSection.id || ''] ? (
                <Box className="pe-info-bar" onClick={() => setExpandedProseTabs({ ...expandedProseTabs, [currentSection.id || '']: false })}>
                  <Box className="pe-info-bar__text" style={{ whiteSpace: 'normal', color: '#c7d4ed', fontSize: 13, lineHeight: 1.6 }}>
                    {currentSection.prose}
                  </Box>
                  <span className="pe-info-bar__toggle">▲ Collapse</span>
                </Box>
              ) : (
                <Box className="pe-info-bar" onClick={() => setExpandedProseTabs({ ...expandedProseTabs, [currentSection.id || '']: true })}>
                  <span style={{ fontSize: 13, filter: 'grayscale(1)' }}>ℹ️</span>
                  <span className="pe-info-bar__text">
                    {currentSection.prose.length > 80 ? `${currentSection.prose.substring(0, 80)}...` : currentSection.prose}
                  </span>
                  <span className="pe-info-bar__toggle">▼ Expand</span>
                </Box>
              )}
            </Box>
          )}

          {/* Data Table */}
          {headers.length > 0 && (
            <Box style={{ marginBottom: 12 }}>
              {currentSection.table_caption && (
                <Typography variant="caption" style={{ color: currentStyle.accent, fontWeight: 800, letterSpacing: '.08em', textTransform: 'uppercase', display: 'block', marginBottom: 4 }}>
                  {currentSection.table_caption}
                </Typography>
              )}
              <Box className="pe-table-shell" style={{ border: '1px solid rgba(59, 130, 246, .2)', borderRadius: 8 }}>
                <Table size="small" className="pe-table">
                  <TableHead>
                    <TableRow>
                      {headers.map((h, i) => (
                        <TableCell key={i} style={{ color: currentStyle.accent, fontWeight: 800 }}>
                          {text(h)}
                        </TableCell>
                      ))}
                    </TableRow>
                  </TableHead>
                  <TableBody>
                    {rows.map((row, ri) => (
                      <TableRow key={ri} hover>
                        {(Array.isArray(row) ? row : [row]).map((cell, ci) => renderTableCell(cell, ci, Array.isArray(row) ? row : [row]))}
                      </TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Box>
            </Box>
          )}

          {/* Questions to raise */}
          {currentQuestions.length > 0 && (
            <Box
              style={{
                borderTop: `1px solid ${currentStyle.accent}33`,
                background: `${currentStyle.accent}0a`,
                padding: '12px 16px',
                borderRadius: 8,
                marginTop: 10,
              }}
            >
              <Typography variant="caption" style={{ color: currentStyle.accent, fontWeight: 800, letterSpacing: '.08em', textTransform: 'uppercase', display: 'block', marginBottom: 8 }}>
                Questions to raise with the customer ({currentQuestions.length})
              </Typography>
              <Box style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                {currentQuestions.map((flag, i) => {
                  if (!expandedQuestionsTabs[currentSection.id || ''] && i >= 2) return null;
                  const qColor = TONE_COLOR[(flag.risk || '').toLowerCase()] || '#3b82f6';
                  return (
                    <Box
                      key={flag.id || `${currentSection.id}-q-${i}`}
                      className="pe-question-card"
                      style={{ borderLeft: `4px solid ${qColor}` }}
                    >
                      {flag.context && <Typography className="pe-question-card__context">{flag.context}</Typography>}
                      <Typography className="pe-question-card__text">{flag.question || 'Review this evidence with the customer.'}</Typography>
                      {flag.data_point && <Typography className="pe-question-card__data">{flag.data_point}</Typography>}
                    </Box>
                  );
                })}
                {!expandedQuestionsTabs[currentSection.id || ''] && currentQuestions.length > 2 && (
                  <button
                    type="button"
                    className="pe-view-toggle__btn"
                    style={{ alignSelf: 'flex-start', marginTop: 4 }}
                    onClick={() => setExpandedQuestionsTabs({ ...expandedQuestionsTabs, [currentSection.id || '']: true })}
                  >
                    Show {currentQuestions.length - 2} more questions
                  </button>
                )}
              </Box>
            </Box>
          )}
        </Box>
      )}
    </Box>
  );
}
