import React, { useMemo } from 'react';
import { Box, Typography } from '@material-ui/core';
import { useAppData } from '../../context/AppDataContext';

interface WindowRow {
  run_date?: string;
  total_hrs?: number;
  effective_hrs?: number;
  breach?: boolean;
}

interface CustomerConflict {
  source?: string;
  name?: string;
  display?: string;
}

interface CustomerIdentityPayload {
  filename?: string;
  window?: WindowRow[];
  kpis?: Record<string, unknown>;
  servers?: { environment?: string }[];
  customer_name?: string;
  customer_status?: string;
  customer_cross_check?: string;
  customer_conflicts?: CustomerConflict[];
  customer_corroborated_by?: string[];
  customer_message?: string;
  customer_active_name?: string;
  customer_candidate_name?: string;
  customer_confidence?: number;
  customer_previous_name?: string;
  customer_previous_confidence?: number;
  customer_previous_source?: string;
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
  const batch = data.batch as CustomerIdentityPayload | null;
  const resource = data.resource as CustomerIdentityPayload | null;
  const sow = data.sowBaseline as CustomerIdentityPayload | null;
  const customerName = data.customerName || batch?.customer_name || resource?.customer_name || sow?.customer_name || null;

  const mismatch = useMemo(() => {
    const sources = [
      { label: 'Ctrl-M upload', payload: batch },
      { label: 'Resource upload', payload: resource },
      { label: 'SOW upload', payload: sow },
    ];
    for (const { label, payload } of sources) {
      if (!payload || payload.customer_status !== 'mismatch') continue;
      const active = payload.customer_active_name || customerName || 'current engagement';
      const candidate = payload.customer_candidate_name || 'another customer';
      const corroborated = (payload.customer_corroborated_by || []).length
        ? `Corroborated by ${payload.customer_corroborated_by?.join(', ')}.`
        : 'Single-source detection.';
      const conflicts = (payload.customer_conflicts || [])
        .map((item) => `${item.source || 'source'}=${item.display || item.name || 'Unknown'}`)
        .join(' · ');
      return {
        label,
        active,
        candidate,
        message: payload.customer_message || `This upload looks like ${candidate}, but the active engagement remains ${active}.`,
        detail: [corroborated, conflicts ? `Conflicts: ${conflicts}.` : null, 'Start a new session before switching customers.'].filter(Boolean).join(' '),
      };
    }
    return null;
  }, [batch, customerName, resource, sow]);

  // A later upload's identity evidence can outrank the one that originally
  // set the active customer (e.g. a Resource DOCX tag beats a bare Ctrl-M
  // filename guess) — customer_identity.identify() auto-corrects in that
  // case instead of just warning. Surface it distinctly from a mismatch:
  // this already happened and is informational, not a blocking conflict.
  const corrected = useMemo(() => {
    const sources = [
      { label: 'Ctrl-M upload', payload: batch },
      { label: 'Resource upload', payload: resource },
      { label: 'SOW upload', payload: sow },
    ];
    for (const { label, payload } of sources) {
      if (!payload || payload.customer_status !== 'corrected') continue;
      return {
        label,
        from: payload.customer_previous_name || 'the prior identification',
        fromConfidence: payload.customer_previous_confidence,
        fromSource: payload.customer_previous_source,
        to: payload.customer_candidate_name || payload.customer_name || 'this upload',
        toConfidence: payload.customer_confidence,
        message: payload.customer_message
          || `Customer identity corrected from ${payload.customer_previous_name || 'a prior guess'} to ${payload.customer_candidate_name || payload.customer_name}.`,
      };
    }
    return null;
  }, [batch, resource, sow]);

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

  if (!customerName && !batch && !resource && !sow) return null;

  const sourceNote = mismatch
    ? `Active engagement retained after ${mismatch.label.toLowerCase()} mismatch.`
    : corrected
      ? `Corrected by ${corrected.label.toLowerCase()} — stronger identity evidence found.`
      : batch?.customer_name
        ? 'Sourced from Ctrl-M identity checks'
        : resource?.customer_name
          ? resource.customer_message || 'Sourced from resource utilization data'
          : sow?.customer_name
            ? 'Sourced from SOW contract metadata'
            : resource?.customer_message
              ? resource.customer_message
              : 'No customer identity evidence was supplied; fleet analysis remains valid.';

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
        display: 'flex', flexDirection: 'column', gap: 12,
      }}
    >
      <Box display="flex" alignItems="center" justifyContent="space-between" style={{ gap: 16, flexWrap: 'wrap', width: '100%' }}>
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
            <Box display="flex" alignItems="center" style={{ gap: 6, marginTop: 2, flexWrap: 'wrap' }}>
              <Typography variant="caption" style={{ color: '#6b7db3' }}>
                {sourceNote}
              </Typography>
              {envBadge && <span className="metric-badge" style={{ fontSize: 8 }}>{envBadge}</span>}
              {mismatch && <span className="metric-badge metric-badge-red" style={{ fontSize: 8 }}>CUSTOMER MISMATCH</span>}
              {corrected && !mismatch && <span className="metric-badge metric-badge-green" style={{ fontSize: 8 }}>IDENTITY CORRECTED</span>}
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

      {mismatch && (
        <Box
          style={{
            width: '100%',
            borderRadius: 8,
            border: '1px solid rgba(244, 63, 94, .35)',
            background: 'rgba(244, 63, 94, .10)',
            padding: '12px 14px',
          }}
        >
          <Box display="flex" alignItems="center" style={{ gap: 8, flexWrap: 'wrap', marginBottom: 6 }}>
            <span className="metric-badge metric-badge-red">Customer mismatch</span>
            <span className="metric-badge metric-badge-amber">{mismatch.label}</span>
            <Typography variant="caption" style={{ color: '#f0f4ff', fontWeight: 700 }}>
              {`Active: ${mismatch.active} · Uploaded file: ${mismatch.candidate}`}
            </Typography>
          </Box>
          <Typography variant="body2" style={{ color: '#f0f4ff', fontWeight: 700 }}>
            {mismatch.message}
          </Typography>
          <Typography variant="caption" style={{ color: 'rgba(240,244,255,.82)' }}>
            {mismatch.detail}
          </Typography>
        </Box>
      )}

      {corrected && !mismatch && (
        <Box
          style={{
            width: '100%',
            borderRadius: 8,
            border: '1px solid rgba(16, 217, 110, .35)',
            background: 'rgba(16, 217, 110, .08)',
            padding: '12px 14px',
          }}
        >
          <Box display="flex" alignItems="center" style={{ gap: 8, flexWrap: 'wrap', marginBottom: 6 }}>
            <span className="metric-badge metric-badge-green">Identity corrected</span>
            <span className="metric-badge metric-badge-amber">{corrected.label}</span>
            <Typography variant="caption" style={{ color: '#f0f4ff', fontWeight: 700 }}>
              {`${corrected.from}${corrected.fromConfidence != null ? ` (${corrected.fromConfidence})` : ''} → ${corrected.to}${corrected.toConfidence != null ? ` (${corrected.toConfidence})` : ''}`}
            </Typography>
          </Box>
          <Typography variant="body2" style={{ color: '#f0f4ff', fontWeight: 700 }}>
            {corrected.message}
          </Typography>
        </Box>
      )}
    </Box>
  );
}
