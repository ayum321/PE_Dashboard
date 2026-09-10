import React, { useMemo } from 'react';
import { Box, Typography } from '@material-ui/core';
import { FindingItem } from './FindingsDataGrid';

interface FindingsCruxProps {
  findings: FindingItem[];
  counts: { all: number; critical: number; warning: number; info: number; ok: number };
  rfcs?: number | null;
  onViewAll?: () => void;
}

const SEV_ORDER = ['critical', 'warning', 'info', 'ok'];
const SEV_COLOR: Record<string, string> = {
  critical: '#f43f5e',
  warning: '#f59e0b',
  info: '#3b82f6',
  ok: '#10d96e',
};

/** Words that carry no distinguishing signal when fingerprinting a finding. */
const STOP = new Set([
  'the', 'a', 'an', 'of', 'in', 'on', 'at', 'to', 'for', 'and', 'or', 'is',
  'are', 'was', 'were', 'has', 'have', 'with', 'from', 'by', 'that', 'this',
  'job', 'jobs', 'run', 'runs', 'sla', 'per', 'only',
]);

/**
 * Collapse findings that restate the same underlying problem.
 *
 * The engines emit the same incident from several angles — a single breaching
 * workflow can surface as a job-level breach, a per-run breach, a thin-buffer
 * warning and a workflow audit line. Those are four rows describing one thing.
 * Fingerprint on `root_cause` when the engine supplied one (that is exactly
 * what it means), otherwise on the finding's distinctive words plus any
 * UPPER_SNAKE entity it names, so the same job/server clusters together.
 */
function fingerprint(f: FindingItem): string {
  const rc = (f.root_cause || '').trim().toLowerCase();
  if (rc) return `rc:${rc}`;
  const text = (f.text || '').toLowerCase();
  const entity = (f.text || '').match(/\b[A-Z][A-Z0-9]{2,}(?:_[A-Z0-9]+)+\b/)?.[0] || '';
  const words = text
    .replace(/[^a-z0-9\s]/g, ' ')
    .split(/\s+/)
    .filter((w) => w.length > 2 && !STOP.has(w) && !/^\d+$/.test(w))
    .slice(0, 4)
    .join('-');
  return `tx:${entity}:${words}`;
}

export interface FindingCluster {
  lead: FindingItem;
  related: number;
}

export function clusterFindings(findings: FindingItem[]): FindingCluster[] {
  const groups = new Map<string, FindingItem[]>();
  for (const f of findings) {
    const key = fingerprint(f);
    const bucket = groups.get(key);
    if (bucket) bucket.push(f);
    else groups.set(key, [f]);
  }

  const rank = (f: FindingItem) => SEV_ORDER.indexOf((f.level || '').toLowerCase());
  const detail = (f: FindingItem) =>
    (f.recommendation || '').length + (f.sub || '').length + (f.evidence || '').length;

  return Array.from(groups.values())
    .map((members) => {
      // Lead with the most severe member, breaking ties on which one carries
      // the most usable evidence for a reviewer.
      const sorted = [...members].sort((a, b) => rank(a) - rank(b) || detail(b) - detail(a));
      return { lead: sorted[0], related: members.length - 1 };
    })
    .sort((a, b) => rank(a.lead) - rank(b.lead) || b.related - a.related);
}

export function FindingsCrux({ findings, counts, rfcs, onViewAll }: FindingsCruxProps) {
  const clusters = useMemo(() => clusterFindings(findings), [findings]);

  const actionable = useMemo(
    () => clusters.filter((c) => ['critical', 'warning'].includes((c.lead.level || '').toLowerCase())),
    [clusters],
  );
  const top = actionable.slice(0, 3);
  const collapsed = counts.all - clusters.length;

  if (!findings.length) return null;

  const headline = counts.critical > 0
    ? `${counts.critical} critical finding${counts.critical === 1 ? '' : 's'} block sign-off`
    : counts.warning > 0
      ? `No blockers — ${counts.warning} warning${counts.warning === 1 ? '' : 's'} to review`
      : 'All checks passed';
  const headlineColor = counts.critical > 0 ? '#f43f5e' : counts.warning > 0 ? '#f59e0b' : '#10d96e';

  const segments = SEV_ORDER
    .map((k) => ({ k, n: counts[k as keyof typeof counts] as number }))
    .filter((s) => s.n > 0);

  return (
    <Box
      className="kpi-card"
      style={{
        marginTop: 14,
        padding: '16px 20px',
        borderRadius: 14,
        border: `1px solid ${headlineColor}33`,
        background: 'linear-gradient(135deg, rgba(13,21,38,.96) 0%, rgba(17,29,54,.96) 100%)',
      }}
    >
      <Box display="flex" alignItems="baseline" justifyContent="space-between" flexWrap="wrap" style={{ gap: 10 }}>
        <Typography style={{ fontWeight: 800, fontSize: 16, color: headlineColor, letterSpacing: '-.01em' }}>
          {headline}
        </Typography>
        <Typography variant="caption" style={{ color: '#6b7db3', fontFamily: "'JetBrains Mono', monospace" }}>
          {clusters.length} distinct issue{clusters.length === 1 ? '' : 's'}
          {collapsed > 0 ? ` · ${collapsed} restated` : ''}
          {rfcs != null ? ` · RFCS ${rfcs.toFixed(0)}` : ''}
        </Typography>
      </Box>

      {/* Proportion bar — replaces the donut; same data, one scannable line. */}
      <Box display="flex" style={{ gap: 2, marginTop: 10, height: 6, borderRadius: 3, overflow: 'hidden' }}>
        {segments.map((s) => (
          <Box
            key={s.k}
            title={`${s.n} ${s.k}`}
            style={{ flex: s.n, background: SEV_COLOR[s.k], opacity: s.k === 'ok' ? 0.55 : 1 }}
          />
        ))}
      </Box>
      <Box display="flex" flexWrap="wrap" style={{ gap: 12, marginTop: 6 }}>
        {segments.map((s) => (
          <span key={s.k} style={{ fontSize: 10, color: SEV_COLOR[s.k], fontWeight: 700, letterSpacing: '.04em' }}>
            {s.n} {s.k.toUpperCase()}
          </span>
        ))}
      </Box>

      {top.length > 0 && (
        <Box style={{ marginTop: 12, display: 'flex', flexDirection: 'column', gap: 8 }}>
          {top.map((c, i) => {
            const sev = (c.lead.level || '').toLowerCase();
            return (
              <Box
                key={`${sev}-${i}`}
                style={{
                  display: 'flex',
                  gap: 10,
                  alignItems: 'flex-start',
                  padding: '8px 10px',
                  borderRadius: 8,
                  borderLeft: `3px solid ${SEV_COLOR[sev] || '#3b82f6'}`,
                  background: 'rgba(6,12,26,.5)',
                }}
              >
                <span
                  style={{
                    fontFamily: "'JetBrains Mono', monospace", fontSize: 10, fontWeight: 800,
                    color: SEV_COLOR[sev], minWidth: 16, paddingTop: 2,
                  }}
                >
                  {i + 1}
                </span>
                <Box style={{ minWidth: 0, flex: 1 }}>
                  <Typography style={{ fontSize: 13, fontWeight: 700, color: '#f0f4ff', lineHeight: 1.4 }}>
                    {c.lead.text}
                  </Typography>
                  {c.lead.recommendation && (
                    <Typography style={{ fontSize: 12, color: '#8fa3d4', marginTop: 2, lineHeight: 1.4 }}>
                      → {c.lead.recommendation}
                    </Typography>
                  )}
                </Box>
                {c.related > 0 && (
                  <span
                    title="Other findings describing this same issue"
                    style={{
                      fontSize: 10, fontWeight: 700, color: '#6b7db3', whiteSpace: 'nowrap',
                      border: '1px solid #21306099', borderRadius: 6, padding: '2px 6px', alignSelf: 'center',
                    }}
                  >
                    +{c.related} related
                  </span>
                )}
              </Box>
            );
          })}
        </Box>
      )}

      {actionable.length > top.length && onViewAll && (
        <Box style={{ marginTop: 10 }}>
          <button
            type="button"
            onClick={onViewAll}
            style={{
              all: 'unset', cursor: 'pointer', fontSize: 11, fontWeight: 700,
              color: '#60a5fa', borderBottom: '1px dashed rgba(96,165,250,.5)',
            }}
          >
            {actionable.length - top.length} more issue{actionable.length - top.length === 1 ? '' : 's'} in the full ledger →
          </button>
        </Box>
      )}
    </Box>
  );
}
