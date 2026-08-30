import React, { useState } from 'react';
import { Box, Table, TableBody, TableCell, TableHead, TableRow, Typography } from '@material-ui/core';
import { ScoreRing } from './ScoreRing';

type NarrativeTable = { headers?: unknown[]; rows?: unknown[][] };
type RedFlag = { id?: string; category?: string; context?: string; question?: string; risk?: string; data_point?: string };

interface PillarCardProps {
  id: string;
  title: string;
  number: number;
  accent: string;
  icon: string;
  score?: number;         // 0-100
  status?: string;        // PASS, REVIEW, BLOCKED, MISSING, LOADED
  prose?: string;
  table?: NarrativeTable;
  tableCaption?: string;
  verdict?: { status?: string; headline?: string; tone?: string };
  kpis?: Array<{ label?: string; value?: unknown; sub?: string; tone?: string }>;
  explainer?: string;
  direction?: string;
  provenance?: { label?: string; note?: string; tone?: string };
  questions: RedFlag[];
  defaultExpanded?: boolean;
}

const TONE_COLOR: Record<string, string> = {
  ok: '#10d96e', warn: '#f59e0b', crit: '#f43f5e', critical: '#f43f5e',
  high: '#f59e0b', medium: '#3b82f6', low: '#10d96e',
};

const STATUS_BADGE: Record<string, { color: string; css: string }> = {
  PASS: { color: '#10d96e', css: 'metric-badge-green' },
  REVIEW: { color: '#f59e0b', css: 'metric-badge-amber' },
  BLOCKED: { color: '#f43f5e', css: 'metric-badge-red' },
  MISSING: { color: '#3b82f6', css: 'metric-badge-blue' },
  LOADED: { color: '#3b82f6', css: 'metric-badge-blue' },
};

const text = (v: unknown): string => (v === undefined || v === null || v === '' ? '—' : String(v));

export function PillarCard({
  id, title, number: num, accent, icon, score, status, prose,
  table, tableCaption, verdict, kpis, explainer, direction,
  provenance, questions, defaultExpanded = true,
}: PillarCardProps) {
  const [expanded, setExpanded] = useState(defaultExpanded);
  const statusInfo = STATUS_BADGE[status || ''] || STATUS_BADGE.LOADED;
  const headers = table?.headers || [];
  const rows = table?.rows || [];

  return (
    <div className="pe-pillar-card" id={`pillar-${id}`} style={{ '--pillar-accent': accent } as React.CSSProperties}>
      {/* Header */}
      <div
        className="pe-pillar-card__header"
        onClick={() => setExpanded(!expanded)}
        role="button"
        tabIndex={0}
        onKeyDown={(e) => { if (e.key === 'Enter' || e.key === ' ') { e.preventDefault(); setExpanded(!expanded); } }}
        aria-expanded={expanded}
      >
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flex: 1, minWidth: 0 }}>
          <span className="pe-pillar-card__number" style={{ borderColor: `${accent}77`, color: accent }}>{num}</span>
          <span style={{ fontSize: 18, flexShrink: 0 }}>{icon}</span>
          <div style={{ minWidth: 0 }}>
            <Typography variant="subtitle2" style={{ fontWeight: 700, color: '#f0f4ff', lineHeight: 1.2 }}>{title}</Typography>
            {provenance && (
              <Typography variant="caption" style={{ color: TONE_COLOR[provenance.tone || ''] || '#6b7db3', fontWeight: 600, display: 'block' }}>
                {text(provenance.label)}
              </Typography>
            )}
          </div>
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 10, flexShrink: 0 }}>
          {score != null && <ScoreRing value={score} size={42} strokeWidth={4} color={accent} label={`${Math.round(score)}`} />}
          <span className={`metric-badge ${statusInfo.css}`}>{status || 'LOADED'}</span>
          <span className="pe-pillar-card__chevron" style={{ transform: expanded ? 'rotate(180deg)' : 'rotate(0deg)' }}>▾</span>
        </div>
      </div>

      {/* Expandable body */}
      <div className={`pe-pillar-card__body ${expanded ? 'pe-pillar-card__body--open' : ''}`}>
        <div className="pe-pillar-card__content">
          {/* Verdict panel */}
          {verdict?.headline && (
            <div style={{ borderLeft: `3px solid ${TONE_COLOR[verdict.tone || ''] || accent}`, background: `${(TONE_COLOR[verdict.tone || ''] || accent)}12`, padding: '9px 12px', marginBottom: 12, borderRadius: '0 6px 6px 0' }}>
              <Typography variant="caption" style={{ color: TONE_COLOR[verdict.tone || ''] || accent, fontWeight: 700, letterSpacing: '.06em' }}>{text(verdict.status)}</Typography>
              <Typography variant="body2" style={{ fontWeight: 600, marginTop: 2 }}>{text(verdict.headline)}</Typography>
            </div>
          )}

          {/* KPIs */}
          {kpis && kpis.length > 0 && (
            <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))', gap: 8, marginBottom: 12 }}>
              {kpis.map((kpi, i) => (
                <div key={i} style={{ padding: '8px 10px', borderRadius: 8, border: '1px solid #213060', background: '#0d152680' }}>
                  <div style={{ fontSize: 9.5, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.1em', color: '#6b7db3' }}>{text(kpi.label)}</div>
                  <div style={{ fontSize: 20, fontWeight: 800, color: TONE_COLOR[kpi.tone || ''] || accent, marginTop: 2 }}>{text(kpi.value)}</div>
                  {kpi.sub && <div style={{ fontSize: 10, color: '#6b7db3', marginTop: 2 }}>{kpi.sub}</div>}
                </div>
              ))}
            </div>
          )}

          {/* Explainer */}
          {explainer && <Typography variant="caption" style={{ color: '#22d3ee', display: 'block', marginBottom: 8 }}>{explainer}</Typography>}
          {direction && <Typography variant="caption" style={{ color: '#f0f4ff', display: 'block', marginBottom: 8 }}>{direction}</Typography>}

          {/* Prose */}
          {prose && <Typography variant="body2" style={{ lineHeight: 1.6, marginBottom: 12, color: '#c7d4ed' }}>{prose}</Typography>}

          {/* Table */}
          {tableCaption && <Typography variant="caption" style={{ color: accent, display: 'block', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.08em', marginBottom: 4 }}>{tableCaption}</Typography>}
          {headers.length > 0 && (
            <Box style={{ overflowX: 'auto', marginBottom: 12 }}>
              <Table size="small" className="pe-table" aria-label={`${title} evidence`}>
                <TableHead><TableRow>{headers.map((h, i) => <TableCell key={i} style={{ color: accent }}>{text(h)}</TableCell>)}</TableRow></TableHead>
                <TableBody>
                  {rows.map((row, ri) => (
                    <TableRow key={ri}>{(Array.isArray(row) ? row : [row]).map((cell, ci) => <TableCell key={ci}>{text(cell)}</TableCell>)}</TableRow>
                  ))}
                </TableBody>
              </Table>
            </Box>
          )}

          {/* Questions */}
          {questions.length > 0 && (
            <div className="pe-pillar-card__questions" style={{ borderTop: `1px solid ${accent}22`, background: `${accent}08`, padding: '12px 14px', borderRadius: '0 0 10px 10px', marginTop: 8 }}>
              <Typography variant="caption" style={{ color: accent, fontWeight: 700, letterSpacing: '.08em', textTransform: 'uppercase', display: 'block', marginBottom: 8 }}>
                Questions to raise with the customer
              </Typography>
              {questions.map((flag, i) => {
                const qColor = TONE_COLOR[(flag.risk || '').toLowerCase()] || '#3b82f6';
                return (
                  <div key={flag.id || `${id}-q-${i}`} style={{ display: 'flex', gap: 10, marginTop: i ? 8 : 0, paddingTop: i ? 8 : 0, borderTop: i ? '1px solid rgba(33,48,96,.55)' : undefined }}>
                    <span style={{ background: `${qColor}26`, color: qColor, borderRadius: 999, fontSize: 11, fontWeight: 700, height: 20, minWidth: 20, textAlign: 'center', lineHeight: '20px', flexShrink: 0 }}>{i + 1}</span>
                    <div style={{ minWidth: 0 }}>
                      {flag.context && <Typography variant="caption" color="textSecondary" style={{ display: 'block' }}>{flag.context}</Typography>}
                      <Typography variant="body2" style={{ lineHeight: 1.45 }}>{flag.question || 'Review this evidence with the customer.'}</Typography>
                      {flag.data_point && <Typography variant="caption" style={{ color: '#6b7db3' }}>{flag.data_point}</Typography>}
                    </div>
                  </div>
                );
              })}
            </div>
          )}
        </div>
      </div>
    </div>
  );
}
