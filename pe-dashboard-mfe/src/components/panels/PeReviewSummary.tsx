import React from 'react';
import {
  Box,
  Paper,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
} from '@material-ui/core';
import { useAppData } from '../../context/AppDataContext';
import { KpiStatCard } from '../shared/KpiStatCard';

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

const SECTION_STYLE: Record<string, { accent: string; number: number }> = {
  data_volume: { accent: '#22d3ee', number: 1 },
  batch_sla: { accent: '#f59e0b', number: 2 },
  infrastructure: { accent: '#a855f7', number: 3 },
  uat: { accent: '#10d96e', number: 4 },
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

function Questions({ sectionId, accent, flags }: { sectionId: string; accent: string; flags: RedFlag[] }) {
  const questions = flags
    .filter((flag) => QUESTION_SECTION[flag.category || ''] === sectionId)
    .sort((a, b) => ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'].indexOf(a.risk || '') - ['CRITICAL', 'HIGH', 'MEDIUM', 'LOW'].indexOf(b.risk || ''))
    .slice(0, sectionId === 'batch_sla' ? 5 : 3);

  if (!questions.length) return null;

  return (
    <Box style={{ borderTop: '1px solid #213060', background: `${accent}0d`, padding: '14px 16px' }}>
      <Typography variant="caption" style={{ color: accent, fontWeight: 700, letterSpacing: '.08em', textTransform: 'uppercase' }}>
        Questions to raise with the customer
      </Typography>
      {questions.map((flag, index) => {
        const color = TONE_COLOR[(flag.risk || '').toLowerCase()] || '#3b82f6';
        return (
          <Box key={flag.id || `${sectionId}-${index}`} style={{ display: 'flex', gap: 10, borderTop: index ? '1px solid rgba(33,48,96,.55)' : undefined, marginTop: index ? 10 : 6, paddingTop: index ? 10 : 0 }}>
            <span style={{ background: `${color}26`, color, borderRadius: 999, fontSize: 11, fontWeight: 700, height: 20, minWidth: 20, textAlign: 'center', lineHeight: '20px' }}>{index + 1}</span>
            <Box>
              {flag.context && <Typography variant="caption" color="textSecondary" style={{ display: 'block' }}>{flag.context}</Typography>}
              <Typography variant="body2">{flag.question || 'Review this evidence with the customer.'}</Typography>
              {flag.data_point && <Typography variant="caption" style={{ color: '#6b7db3' }}>{flag.data_point}</Typography>}
            </Box>
          </Box>
        );
      })}
    </Box>
  );
}

function BatchVerdictPanel({ panel }: { panel?: NarrativePanel }) {
  if (!panel?.verdict) return null;
  const verdict = panel.verdict;
  const color = TONE_COLOR[verdict.tone || ''] || '#3b82f6';
  return (
    <Box style={{ borderBottom: '1px solid #213060', padding: '12px 16px' }}>
      <Box style={{ borderLeft: `3px solid ${color}`, background: `${color}12`, padding: '9px 12px' }}>
        <Typography variant="caption" style={{ color, fontWeight: 700, letterSpacing: '.06em' }}>{text(verdict.status)}</Typography>
        <Typography variant="body2" style={{ fontWeight: 600, marginTop: 3 }}>{text(verdict.headline)}</Typography>
      </Box>
      {!!panel.kpis?.length && (
        <Box style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(145px, 1fr))', gap: 10, marginTop: 12 }}>
          {panel.kpis.map((kpi, index) => (
            <KpiStatCard
              key={`${kpi.label}-${index}`}
              label={text(kpi.label)}
              value={text(kpi.value)}
              sub={kpi.sub || ''}
              accent={TONE_COLOR[kpi.tone || ''] || '#3b82f6'}
            />
          ))}
        </Box>
      )}
      {panel.explainer && <Typography variant="caption" style={{ color: '#22d3ee', display: 'block', marginTop: 10 }}>{panel.explainer}</Typography>}
      {panel.direction && <Typography variant="caption" style={{ color: '#f0f4ff', display: 'block', marginTop: 8 }}>{panel.direction}</Typography>}
    </Box>
  );
}

/** The same deterministic structured response rendered by the FastAPI PE Findings page. */
export function PeReviewSummary() {
  const { data } = useAppData();
  const narrative = data.peNarrative;
  if (!narrative) return null;

  const verdict = text(narrative.verdict).toUpperCase();
  const verdictColor = verdict === 'APPROVED' ? '#10d96e' : verdict === 'BLOCKED' ? '#f43f5e' : '#f59e0b';
  const sections = (narrative.sections as NarrativeSection[]) || [];
  const flags = (data.redFlags?.flags as RedFlag[]) || [];

  return (
    <Paper className="kpi-card" elevation={0} style={{ marginTop: 16, padding: 16 }}>
      <Box display="flex" justifyContent="space-between" alignItems="flex-start" flexWrap="wrap" style={{ gap: 10 }}>
        <Box>
          <Box display="flex" alignItems="center" style={{ gap: 8 }}>
            <Typography variant="h6">PE Review Summary</Typography>
            <span className="metric-badge" style={{ color: verdictColor, borderColor: `${verdictColor}66`, background: `${verdictColor}1f` }}>{verdict}</span>
            <span className="metric-badge metric-badge-blue">{text(narrative.model).replace('models/', '')}</span>
          </Box>
          <Typography variant="body2" style={{ marginTop: 6 }}>{text(narrative.summary)}</Typography>
        </Box>
      </Box>
      {narrative.verdict_reason && (
        <Box style={{ borderLeft: `3px solid ${verdictColor}`, background: `${verdictColor}12`, marginTop: 12, padding: '9px 12px' }}>
          <Typography variant="caption" style={{ color: verdictColor, fontWeight: 700 }}>PE DECISION</Typography>
          <Typography variant="body2">{text(narrative.verdict_reason)}</Typography>
        </Box>
      )}

      {sections.map((section, index) => {
        const style = SECTION_STYLE[section.id || ''] || { accent: '#3b82f6', number: index + 1 };
        const table = section.table || {};
        const headers = table.headers || [];
        const rows = table.rows || [];
        return (
          <Paper key={section.id || index} elevation={0} style={{ border: '1px solid #213060', marginTop: 12, overflow: 'hidden' }}>
            <Box display="flex" alignItems="center" style={{ gap: 10, background: `${style.accent}12`, borderBottom: '1px solid #213060', padding: '10px 14px' }}>
              <span style={{ border: `1px solid ${style.accent}77`, borderRadius: 4, color: style.accent, fontFamily: 'monospace', fontSize: 12, padding: '2px 7px' }}>{style.number}</span>
              <Typography variant="subtitle2">{text(section.title)}</Typography>
            </Box>
            {section.provenance && (
              <Box style={{ borderBottom: '1px solid #213060', padding: '8px 14px' }}>
                <Typography variant="caption" style={{ color: TONE_COLOR[section.provenance.tone || ''] || '#6b7db3', fontWeight: 700 }}>{text(section.provenance.label)}</Typography>
                {section.provenance.note && <Typography variant="caption" color="textSecondary" style={{ display: 'block' }}>{section.provenance.note}</Typography>}
              </Box>
            )}
            <BatchVerdictPanel panel={section.panel} />
            {section.prose && <Typography variant="body2" style={{ borderBottom: '1px solid #213060', lineHeight: 1.6, padding: '12px 14px' }}>{section.prose}</Typography>}
            {section.table_caption && <Typography variant="caption" style={{ color: style.accent, display: 'block', fontWeight: 700, padding: '10px 14px 4px', textTransform: 'uppercase' }}>{section.table_caption}</Typography>}
            {!!headers.length && (
              <Box style={{ overflowX: 'auto' }}>
                <Table size="small" className="pe-table" aria-label={`${text(section.title)} evidence`}>
                  <TableHead><TableRow>{headers.map((header, headerIndex) => <TableCell key={headerIndex} style={{ color: style.accent }}>{text(header)}</TableCell>)}</TableRow></TableHead>
                  <TableBody>
                    {rows.map((row, rowIndex) => (
                      <TableRow key={rowIndex}>{(Array.isArray(row) ? row : [row]).map((cell, cellIndex) => <TableCell key={cellIndex}>{text(cell)}</TableCell>)}</TableRow>
                    ))}
                  </TableBody>
                </Table>
              </Box>
            )}
            <Questions sectionId={section.id || ''} accent={style.accent} flags={flags} />
          </Paper>
        );
      })}
    </Paper>
  );
}
