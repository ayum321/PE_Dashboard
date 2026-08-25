import React from 'react';

interface KpiStatCardProps {
  label: string;
  value: React.ReactNode;
  sub?: React.ReactNode;
  accent: string;
  valueColor?: string;
  onClick?: () => void;
}

/** Matches the real dashboard's KPI card: gradient card, colored top accent line,
 * uppercase label, large extrabold value, muted sub-line (app/templates/index.html). */
export function KpiStatCard({ label, value, sub, accent, valueColor, onClick }: KpiStatCardProps) {
  return (
    <div
      onClick={onClick}
      style={{
        position: 'relative',
        overflow: 'hidden',
        borderRadius: 16,
        border: '1px solid #213060',
        background: 'linear-gradient(135deg, #0d1526 0%, #111d36 100%)',
        padding: 16,
        minHeight: 100,
        boxShadow: '0 4px 20px rgba(0,0,0,.4), 0 0 0 1px rgba(59,130,246,.08)',
        cursor: onClick ? 'pointer' : 'default',
      }}
    >
      <div style={{ position: 'absolute', top: 0, left: 0, right: 0, height: 2, background: `linear-gradient(90deg, transparent, ${accent}99, transparent)` }} />
      <div style={{ fontSize: '10.5px', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.12em', color: '#6b7db3' }}>{label}</div>
      <div style={{ fontSize: 32, fontWeight: 800, lineHeight: 1.05, letterSpacing: '-0.01em', marginTop: 4, color: valueColor || accent }}>{value}</div>
      {sub && <div style={{ fontSize: 11, color: '#6b7db3', marginTop: 4, fontWeight: 500 }}>{sub}</div>}
    </div>
  );
}
