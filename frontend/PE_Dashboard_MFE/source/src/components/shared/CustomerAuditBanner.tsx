import React, { useMemo } from 'react';
import { Box, Typography } from '@material-ui/core';
import { useAppData } from '../../context/AppDataContext';

interface WindowRow {
  run_date?: string;
  total_hrs?: number;
  effective_hrs?: number;
  breach?: boolean;
}

/** Deterministic 6-char audit id from customer+filename+runs+date, ported from
 * _shortHash() in app.js (djb2-style hash, base36, uppercased). */
function shortHash(s: string): string {
  let h = 5381;
  for (let i = 0; i < s.length; i++) h = ((h << 5) + h + s.charCodeAt(i)) >>> 0;
  return h.toString(36).toUpperCase().slice(0, 6).padStart(6, '0');
}

/**
 * Global customer identity + audit pulse banner — persistent across every tab,
 * ported from the #exec-customer-banner / _renderAuditPulse() (app.js). Shows the
 * customer name, a date-range/run-count tile, a daily-run sparkline tile, and a
 * deterministic audit id tile, matching the real dashboard's header hero.
 */
export function CustomerAuditBanner() {
  const { data } = useAppData();
  const customerName = data.customerName;
  const batch = data.batch as { window?: WindowRow[]; kpis?: Record<string, unknown>; filename?: string } | null;
  const resource = data.resource as { servers?: { environment?: string }[] } | null;

  const pulse = useMemo(() => {
    const winRows = (batch?.window as WindowRow[]) || [];
    const runs = Number((batch?.kpis as { total_runs?: number })?.total_runs) || 0;
    const dates = winRows
      .map((w) => w.run_date)
      .filter((d): d is string => !!d)
      .map((d) => new Date(d))
      .filter((d) => !isNaN(d.getTime()))
      .sort((a, b) => a.getTime() - b.getTime());
    const fmtShort = (d: Date) => d.toLocaleDateString('en-US', { month: 'short', day: 'numeric' });
    let rangeStr = '\u2014';
    let spanStr = '\u2014';
    if (dates.length) {
      const dMin = dates[0];
      const dMax = dates[dates.length - 1];
      rangeStr = `${fmtShort(dMin)} \u2192 ${fmtShort(dMax)}`;
      const days = Math.round((dMax.getTime() - dMin.getTime()) / 86400000) + 1;
      spanStr = `${days} day${days !== 1 ? 's' : ''} \u00b7 ${runs.toLocaleString()} runs`;
    } else if (runs) {
      rangeStr = runs.toLocaleString() + ' runs';
      spanStr = batch?.filename || '\u2014';
    }

    const vals = winRows.map((w) => Number(w.effective_hrs ?? w.total_hrs) || 0);
    const max = Math.max(1, ...vals);
    const breachCount = winRows.filter((w) => w.breach).length;

    const seed = `${customerName || ''}|${batch?.filename || ''}|${runs}|${dates[0]?.toISOString().slice(0, 10) || ''}`;
    const auditId = shortHash(seed);
    const compliance = Number((batch?.kpis as { compliance_pct?: number })?.compliance_pct ?? 100);
    const dotColor = compliance >= 95 ? '#10d96e' : compliance >= 80 ? '#f59e0b' : '#f43f5e';

    return { rangeStr, spanStr, vals, max, breachCount, auditId, dotColor, hasWindow: winRows.length > 0 };
  }, [batch, customerName]);

  // Environment mix, so the persistent header carries "which environment" too,
  // not just "which customer" — derived from whatever data is actually loaded
  // (Azure-fetched or DOCX-parsed resource rows), not just the Ctrl-M batch file.
  const envBadge = useMemo(() => {
    const envs = (resource?.servers || []).map((s) => s.environment).filter(Boolean) as string[];
    if (!envs.length) return null;
    const unique = Array.from(new Set(envs));
    return unique.length === 1 ? unique[0] : `Mixed (${unique.join(' + ')})`;
  }, [resource]);

  if (!customerName && !batch && !resource) return null;

  const sparkPoints = pulse.vals
    .map((v, i) => {
      const step = pulse.vals.length > 1 ? 156 / (pulse.vals.length - 1) : 0;
      const x = 2 + i * step;
      const y = 28 - (v / pulse.max) * 26;
      return `${x.toFixed(1)},${y.toFixed(1)}`;
    })
    .join(' ');

  return (
    <Box
      style={{
        position: 'sticky', top: 0, zIndex: 20, borderRadius: 16,
        border: '1px solid rgba(168,85,247,.4)',
        background: 'linear-gradient(90deg, rgba(168,85,247,.15), #111d36 40%, #0d1526)',
        padding: '14px 20px', marginBottom: 16,
        display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 16, flexWrap: 'wrap',
      }}
    >
      <Box display="flex" alignItems="center" style={{ gap: 14, minWidth: 0 }}>
        <Box
          style={{
            width: 44, height: 44, borderRadius: 10, background: 'rgba(168,85,247,.25)',
            border: '1px solid rgba(168,85,247,.4)', display: 'flex', alignItems: 'center',
            justifyContent: 'center', flexShrink: 0, color: '#a855f7', fontSize: 20,
          }}
        >
          🏢
        </Box>
        <Box style={{ minWidth: 0 }}>
          <Typography variant="caption" style={{ textTransform: 'uppercase', letterSpacing: '.18em', color: '#6b7db3', fontWeight: 700, fontSize: 10 }}>Customer</Typography>
          <Typography variant="h5" style={{ fontWeight: 900, color: '#f0f4ff', lineHeight: 1.1, whiteSpace: 'nowrap', overflow: 'hidden', textOverflow: 'ellipsis' }}>
            {customerName || 'Unassigned engagement'}
          </Typography>
          <Box display="flex" alignItems="center" style={{ gap: 6, marginTop: 2 }}>
            <Typography variant="caption" style={{ color: '#6b7db3' }}>
              {customerName
                ? (batch ? 'Sourced from Ctrl-M filename' : 'Sourced from Azure tags')
                : 'No customer tag was supplied; fleet analysis remains valid.'}
            </Typography>
            {envBadge && <span className="metric-badge" style={{ fontSize: 8 }}>{envBadge}</span>}
          </Box>
        </Box>
      </Box>

      {pulse.hasWindow && (
        <Box display="flex" style={{ gap: 12, flexShrink: 0 }}>
          <Box style={{ padding: '8px 12px', borderRadius: 8, border: '1px solid rgba(33,48,96,.6)', background: 'rgba(17,29,54,.5)', minWidth: 150 }}>
            <Typography variant="caption" style={{ textTransform: 'uppercase', letterSpacing: '.18em', color: '#6b7db3', fontWeight: 700, fontSize: 9, display: 'block' }}>Audit Window</Typography>
            <Typography variant="body2" style={{ fontWeight: 700, color: '#f0f4ff', lineHeight: 1.2 }}>{pulse.rangeStr}</Typography>
            <Typography variant="caption" style={{ color: '#6b7db3', fontSize: 10 }}>{pulse.spanStr}</Typography>
          </Box>

          <Box style={{ padding: '8px 12px', borderRadius: 8, border: '1px solid rgba(33,48,96,.6)', background: 'rgba(17,29,54,.5)', minWidth: 170 }}>
            <Box display="flex" justifyContent="space-between" alignItems="baseline">
              <Typography variant="caption" style={{ textTransform: 'uppercase', letterSpacing: '.18em', color: '#6b7db3', fontWeight: 700, fontSize: 9 }}>Daily Pulse</Typography>
              <Typography variant="caption" style={{ fontWeight: 900, color: '#f0f4ff', fontSize: 11 }}>{`\u2191 peak ${pulse.max}`}</Typography>
            </Box>
            <svg viewBox="0 0 160 30" preserveAspectRatio="none" style={{ width: '100%', height: 28, display: 'block' }}>
              <polyline points={sparkPoints} fill="none" stroke="#22d3ee" strokeWidth={1.4} strokeLinecap="round" strokeLinejoin="round" />
            </svg>
            <Typography variant="caption" style={{ fontSize: 9, color: pulse.breachCount > 0 ? '#f43f5e' : '#10d96e' }}>
              {pulse.breachCount > 0 ? `${pulse.breachCount} breach day${pulse.breachCount !== 1 ? 's' : ''} \u00b7 ${pulse.vals.length} pts` : `\u2713 ${pulse.vals.length} clean days`}
            </Typography>
          </Box>

          <Box style={{ padding: '8px 12px', borderRadius: 8, border: '1px solid rgba(33,48,96,.6)', background: 'rgba(17,29,54,.5)', minWidth: 140 }}>
            <Box display="flex" alignItems="center" style={{ gap: 6 }}>
              <span style={{ width: 6, height: 6, borderRadius: '50%', background: pulse.dotColor, display: 'inline-block' }} />
              <Typography variant="caption" style={{ textTransform: 'uppercase', letterSpacing: '.18em', color: '#6b7db3', fontWeight: 700, fontSize: 9 }}>Audit ID</Typography>
            </Box>
            <Typography variant="body2" style={{ fontFamily: 'monospace', fontWeight: 700, color: '#f0f4ff' }}>{`#${pulse.auditId}`}</Typography>
            <Typography variant="caption" style={{ color: '#6b7db3', fontSize: 10 }}>
              {`live \u00b7 ${new Date().toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit' })}`}
            </Typography>
          </Box>
        </Box>
      )}
    </Box>
  );
}
