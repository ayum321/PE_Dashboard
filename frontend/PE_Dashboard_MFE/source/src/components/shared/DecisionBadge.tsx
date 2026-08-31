import React from 'react';

interface DecisionBadgeProps {
  decision: string;   // GO, HOLD, BLOCKED, etc.
  reason?: string;
  compact?: boolean;
}

const DECISION_CONFIG: Record<string, { color: string; icon: string; label: string }> = {
  GO: { color: '#10d96e', icon: '✓', label: 'GO' },
  GO_WITH_NOTES: { color: '#3b82f6', icon: '✓', label: 'GO WITH NOTES' },
  HOLD: { color: '#f59e0b', icon: '⏸', label: 'HOLD' },
  BLOCKED: { color: '#f43f5e', icon: '✕', label: 'BLOCKED' },
  REMEDIATE: { color: '#f43f5e', icon: '⚠', label: 'REMEDIATE' },
  INSUFFICIENT_DATA: { color: '#6b7db3', icon: '?', label: 'INSUFFICIENT DATA' },
};

export function DecisionBadge({ decision, reason, compact }: DecisionBadgeProps) {
  const config = DECISION_CONFIG[decision] || DECISION_CONFIG.INSUFFICIENT_DATA;
  const { color, icon, label } = config;

  if (compact) {
    return (
      <span
        className="pe-decision-badge pe-decision-badge--compact"
        style={{
          display: 'inline-flex', alignItems: 'center', gap: 6,
          padding: '4px 12px', borderRadius: 999,
          border: `1px solid ${color}66`, background: `${color}14`,
          color, fontSize: 12, fontWeight: 800, letterSpacing: '.06em',
        }}
        title={reason}
      >
        <span style={{ fontSize: 14 }}>{icon}</span>
        {label}
      </span>
    );
  }

  return (
    <div className="pe-decision-badge" title={reason}>
      <div
        className="pe-decision-hero"
        style={{
          position: 'relative', display: 'flex', flexDirection: 'column', alignItems: 'center',
          padding: '16px 24px', borderRadius: 16,
          border: `2px solid ${color}55`,
          background: `radial-gradient(circle at 50% 0%, ${color}18 0%, transparent 70%), linear-gradient(135deg, #0d1526 0%, #111d36 100%)`,
          boxShadow: `0 0 32px ${color}22, inset 0 1px 0 rgba(255,255,255,.04)`,
          minWidth: 120, textAlign: 'center',
        }}
      >
        <div
          style={{
            width: 44, height: 44, borderRadius: '50%',
            border: `2px solid ${color}`,
            background: `${color}1a`,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            fontSize: 22, color,
            boxShadow: `0 0 18px ${color}44`,
            marginBottom: 8,
          }}
        >
          {icon}
        </div>
        <div style={{ fontSize: 11, fontWeight: 800, letterSpacing: '.12em', color, textTransform: 'uppercase' }}>
          DECISION
        </div>
        <div style={{
          fontSize: 18, fontWeight: 800, color: '#f0f4ff',
          marginTop: 2, letterSpacing: '.02em',
        }}>
          {label}
        </div>
        {reason && (
          <div style={{
            fontSize: 11, color: '#8ba9ef', marginTop: 6,
            lineHeight: 1.4, maxWidth: 200,
          }}>
            {reason}
          </div>
        )}
        {/* Animated glow ring */}
        <div className="pe-decision-hero__glow" style={{
          position: 'absolute', inset: -1, borderRadius: 16,
          border: `1px solid ${color}33`,
          pointerEvents: 'none',
        }} />
      </div>
    </div>
  );
}
