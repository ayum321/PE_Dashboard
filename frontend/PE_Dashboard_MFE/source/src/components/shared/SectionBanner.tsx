import React from 'react';

interface SectionBannerProps {
  eyebrow: string;
  title: string;
  description?: string;
  headline?: React.ReactNode;
  headlineLabel?: string;
  accent?: string;
}

/** Matches the real dashboard's story banner (SLA Matrix / SOW headers):
 * gradient-tinted card, eyebrow label, bold question/title, optional right-aligned headline stat. */
export function SectionBanner({ eyebrow, title, description, headline, headlineLabel, accent = '#a855f7' }: SectionBannerProps) {
  return (
    <div
      style={{
        borderRadius: 16,
        border: `1px solid ${accent}4d`,
        background: `linear-gradient(135deg, ${accent}1a 0%, #0d1526 60%, #111d36 100%)`,
        boxShadow: '0 4px 24px rgba(0,0,0,.35)',
        padding: 20,
        display: 'flex',
        alignItems: 'flex-start',
        justifyContent: 'space-between',
        gap: 20,
        flexWrap: 'wrap',
      }}
    >
      <div style={{ minWidth: 0, flex: 1 }}>
        <span style={{ fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.15em', color: accent }}>{eyebrow}</span>
        <h2 style={{ fontSize: 16, fontWeight: 700, color: '#f0f4ff', margin: '6px 0 0', lineHeight: 1.4 }}>{title}</h2>
        {description && <p style={{ fontSize: 11, color: 'rgba(240,244,255,.7)', marginTop: 6, lineHeight: 1.6 }}>{description}</p>}
      </div>
      {headline !== undefined && (
        <div style={{ textAlign: 'right', flexShrink: 0 }}>
          {headlineLabel && <div style={{ fontSize: 10, color: '#6b7db3', textTransform: 'uppercase', letterSpacing: '.12em', fontWeight: 600 }}>{headlineLabel}</div>}
          <div style={{ fontSize: 32, fontWeight: 700, color: '#f0f4ff', lineHeight: 1, marginTop: 4 }}>{headline}</div>
        </div>
      )}
    </div>
  );
}
