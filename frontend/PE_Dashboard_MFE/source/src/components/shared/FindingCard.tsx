import React, { useState } from 'react';
import { Typography } from '@material-ui/core';

interface FindingCardProps {
  level: string;
  text: string;
  sub?: string;
  impact?: string;
  recommendation?: string;
  evidence?: string;
  root_cause?: string;
  confidence?: number;
  source?: string;
  isTopAction?: boolean;
}

const SEVERITY_CONFIG: Record<string, { color: string; icon: string; bg: string }> = {
  critical: { color: '#f43f5e', icon: '🔴', bg: 'rgba(244,63,94,.08)' },
  warning: { color: '#f59e0b', icon: '🟡', bg: 'rgba(245,158,11,.06)' },
  info: { color: '#3b82f6', icon: '🔵', bg: 'rgba(59,130,246,.06)' },
  ok: { color: '#10d96e', icon: '🟢', bg: 'rgba(16,217,110,.06)' },
};

export function FindingCard({ level, text: findingText, sub, impact, recommendation, evidence, root_cause, confidence, source, isTopAction }: FindingCardProps) {
  const [evidenceOpen, setEvidenceOpen] = useState(false);
  const normalLevel = level.toLowerCase();
  const config = SEVERITY_CONFIG[normalLevel] || SEVERITY_CONFIG.info;
  const hasEvidence = !!(evidence || root_cause || source || confidence != null);

  return (
    <div className={`pe-finding-card-v2 pe-finding-card-v2--${normalLevel}`} style={{ background: config.bg }}>
      {/* Severity strip */}
      <div className="pe-finding-card-v2__strip" style={{ background: config.color }} />

      <div className="pe-finding-card-v2__body">
        {/* Header row */}
        <div className="pe-finding-card-v2__header">
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flex: 1, minWidth: 0 }}>
            <span
              className="pe-finding-card-v2__severity"
              style={{ color: config.color, borderColor: `${config.color}66`, background: `${config.color}14` }}
            >
              {normalLevel.toUpperCase()}
            </span>
            {source && (
              <span className="metric-badge metric-badge-blue" style={{ fontSize: 9, padding: '2px 6px' }}>
                {source.toUpperCase()}
              </span>
            )}
            {confidence != null && (
              <div style={{ display: 'flex', alignItems: 'center', gap: 4, flexShrink: 0 }}>
                <div style={{ width: 40, height: 4, borderRadius: 2, background: 'rgba(255,255,255,.08)', overflow: 'hidden' }}>
                  <div style={{ width: `${confidence}%`, height: '100%', borderRadius: 2, background: config.color, transition: 'width .6s ease' }} />
                </div>
                <span style={{ fontSize: 9, color: '#6b7db3', fontFamily: "'JetBrains Mono', monospace", fontWeight: 700 }}>{confidence}%</span>
              </div>
            )}
          </div>
        </div>

        {/* Title */}
        <Typography className="pe-finding-card-v2__title" style={{ fontWeight: 700, fontSize: 14, color: '#f4f8ff', lineHeight: 1.45, marginTop: 6 }}>
          {findingText}
        </Typography>

        {/* Detail */}
        {sub && <Typography variant="body2" style={{ color: '#b8c7e7', fontSize: 13, lineHeight: 1.5, marginTop: 6 }}>{sub}</Typography>}
        {impact && <Typography variant="body2" style={{ color: '#99aed1', fontSize: 13, lineHeight: 1.5, marginTop: 6 }}>{impact}</Typography>}

        {/* Recommendation */}
        {recommendation && !isTopAction && (
          <div className="pe-finding-card-v2__recommendation" style={{ borderLeft: `3px solid ${config.color}55`, background: `${config.color}08`, padding: '8px 12px', marginTop: 10, borderRadius: '0 6px 6px 0' }}>
            <Typography variant="caption" style={{ color: config.color, fontWeight: 700, letterSpacing: '.04em', fontSize: 10 }}>RECOMMENDED ACTION</Typography>
            <Typography variant="body2" style={{ marginTop: 2, lineHeight: 1.5, fontSize: 13 }}>{recommendation}</Typography>
          </div>
        )}

        {/* Evidence accordion */}
        {hasEvidence && (
          <div className="pe-finding-card-v2__evidence">
            <button
              className="pe-finding-card-v2__evidence-toggle"
              onClick={() => setEvidenceOpen(!evidenceOpen)}
              aria-expanded={evidenceOpen}
              type="button"
            >
              <span style={{ transform: evidenceOpen ? 'rotate(90deg)' : 'rotate(0deg)', transition: 'transform .2s', display: 'inline-block', fontSize: 10 }}>▶</span>
              View evidence and provenance
            </button>
            <div className={`pe-finding-card-v2__evidence-body ${evidenceOpen ? 'pe-finding-card-v2__evidence-body--open' : ''}`}>
              <div className="pe-finding-card-v2__evidence-content">
                {evidence && <Typography variant="caption" style={{ display: 'block', color: '#9fb2da', marginTop: 4 }}>Evidence: {evidence}</Typography>}
                {root_cause && <Typography variant="caption" style={{ display: 'block', color: '#9fb2da', marginTop: 4 }}>Root cause: {root_cause}</Typography>}
                {source && <Typography variant="caption" style={{ display: 'block', color: '#9fb2da', marginTop: 4 }}>Source: {source}{confidence != null ? ` · Confidence: ${confidence}%` : ''}</Typography>}
              </div>
            </div>
          </div>
        )}
      </div>
    </div>
  );
}
