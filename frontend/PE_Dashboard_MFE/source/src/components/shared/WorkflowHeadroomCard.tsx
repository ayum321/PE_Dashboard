import React from 'react';
import { Box, Typography } from '@material-ui/core';

interface WorkflowItem {
  name: string;
  runtime_h: number;
  sla_h: number;
  buffer_pct: number;
  status: string; // BREACH | NO_BUFFER | AT_RISK | LONG_JOB | OK
  source?: string;
}

interface WorkflowHeadroomCardProps {
  workflows?: WorkflowItem[];
}

// Single source of truth for the buffer bands — the legend below is rendered
// from this same table, so the swatches can never drift from the bar colours.
// Ordered most-severe first; the first match wins.
// NO_BUFFER is its own band, not a shade of AT_RISK: sla_merger.py classifies
// a buffer within ±0.5% as "runs to the exact edge, zero slack", which is a
// materially different finding from merely having a thin margin.
const BANDS = [
  {
    color: '#f43f5e',
    label: 'Breach (<0%)',
    match: (s: string, b: number) => s === 'BREACH' || b < -0.5,
  },
  {
    color: '#fb923c',
    label: 'No Buffer (±0.5%)',
    match: (s: string, b: number) => s === 'NO_BUFFER' || Math.abs(b) <= 0.5,
  },
  {
    color: '#f59e0b',
    label: 'At Risk (0.5–15%)',
    match: (s: string, b: number) => s === 'AT_RISK' || b <= 15,
  },
  {
    color: '#22d3ee',
    label: 'Long Job (15–40%)',
    match: (s: string, b: number) => s === 'LONG_JOB' || b <= 40,
  },
  {
    color: '#10d96e',
    label: 'OK (>40%)',
    match: () => true,
  },
] as const;

export function WorkflowHeadroomCard({ workflows }: WorkflowHeadroomCardProps) {
  if (!workflows || workflows.length === 0) return null;

  const getStatusColor = (status: string, buffer: number) =>
    (BANDS.find((band) => band.match(status.toUpperCase(), buffer)) || BANDS[BANDS.length - 1]).color;

  return (
    <Box
      className="kpi-card"
      style={{
        padding: '16px 20px',
        borderRadius: 14,
        marginTop: 14,
        border: '1px solid rgba(59, 130, 246, .2)',
        background: 'linear-gradient(135deg, rgba(13, 21, 38, .95) 0%, rgba(17, 29, 54, .95) 100%)',
      }}
    >
      <Box display="flex" justifyContent="space-between" alignItems="center" flexWrap="wrap" style={{ gap: 8, marginBottom: 12 }}>
        <Box>
          <Typography variant="subtitle2" style={{ fontWeight: 800, color: '#f0f4ff', letterSpacing: '.02em' }}>
            Workflow SLA Headroom & Utilization
          </Typography>
          <Typography variant="caption" color="textSecondary">
            Measured runtimes vs contracted ceiling limits across all active workflows
          </Typography>
        </Box>
        <Box display="flex" flexWrap="wrap" style={{ gap: 8 }}>
          {BANDS.map((band) => (
            <span key={band.label} style={{ fontSize: 10, color: band.color, display: 'inline-flex', alignItems: 'center', gap: 4 }}>
              <span style={{ width: 8, height: 8, borderRadius: '50%', background: band.color }} /> {band.label}
            </span>
          ))}
        </Box>
      </Box>

      <Box style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
        {workflows.map((wf, idx) => {
          const usedPct = Math.min(Math.max(100 - wf.buffer_pct, 0), 120);
          const barColor = getStatusColor(wf.status, wf.buffer_pct);
          const headroomMin = Math.max(Math.round((wf.sla_h - wf.runtime_h) * 60), 0);

          return (
            <Box
              key={`${wf.name}-${idx}`}
              style={{
                background: 'rgba(6, 12, 26, .5)',
                padding: '10px 14px',
                borderRadius: 8,
                border: '1px solid rgba(33, 48, 96, .5)',
              }}
            >
              <Box display="flex" justifyContent="space-between" alignItems="center" style={{ marginBottom: 6 }}>
                <Box display="flex" alignItems="center" style={{ gap: 8 }}>
                  <Typography style={{ fontWeight: 700, fontSize: 13, color: '#f0f4ff', fontFamily: "'JetBrains Mono', monospace" }}>
                    {wf.name}
                  </Typography>
                  <span
                    className="metric-badge"
                    style={{
                      fontSize: 9,
                      padding: '2px 6px',
                      color: barColor,
                      borderColor: `${barColor}55`,
                      background: `${barColor}15`,
                    }}
                  >
                    {wf.status.toUpperCase()}
                  </span>
                </Box>
                <Box display="flex" alignItems="center" style={{ gap: 12 }}>
                  <Typography variant="caption" style={{ color: '#91a7d8', fontFamily: "'JetBrains Mono', monospace" }}>
                    {wf.runtime_h.toFixed(2)}h / {wf.sla_h.toFixed(1)}h SLA
                  </Typography>
                  <Typography variant="caption" style={{ fontWeight: 800, color: barColor, fontFamily: "'JetBrains Mono', monospace" }}>
                    {wf.buffer_pct > 0
                      ? `${wf.buffer_pct.toFixed(1)}% buffer (${headroomMin}m)`
                      : wf.buffer_pct < 0
                        ? `${Math.abs(wf.buffer_pct).toFixed(1)}% overrun`
                        : '0.0% buffer (0m headroom)'}
                  </Typography>
                </Box>
              </Box>

              {/* Multi-tier progress track */}
              <Box style={{ position: 'relative', height: 8, borderRadius: 4, background: 'rgba(255, 255, 255, .06)', overflow: 'hidden' }}>
                <Box
                  style={{
                    position: 'absolute',
                    top: 0,
                    left: 0,
                    bottom: 0,
                    width: `${Math.min(usedPct, 100)}%`,
                    background: barColor,
                    borderRadius: 4,
                    transition: 'width .6s ease',
                    boxShadow: `0 0 8px ${barColor}66`,
                  }}
                />
              </Box>
            </Box>
          );
        })}
      </Box>
    </Box>
  );
}
