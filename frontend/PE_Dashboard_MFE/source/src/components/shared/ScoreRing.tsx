import React from 'react';

interface ScoreRingProps {
  value: number;        // 0-100
  size?: number;        // px, default 80
  strokeWidth?: number; // default 6
  color: string;        // accent color
  label?: string;       // center label (e.g. 'B+')
  sub?: string;         // sub-label below ring
  animate?: boolean;    // default true
}

export function ScoreRing({ value, size = 80, strokeWidth = 6, color, label, sub, animate = true }: ScoreRingProps) {
  const radius = (size - strokeWidth) / 2;
  const circumference = 2 * Math.PI * radius;
  const offset = circumference - (Math.min(Math.max(value, 0), 100) / 100) * circumference;
  const center = size / 2;

  return (
    <div style={{ display: 'flex', flexDirection: 'column', alignItems: 'center', gap: 4 }}>
      <div style={{ position: 'relative', width: size, height: size }}>
        <svg width={size} height={size} style={{ transform: 'rotate(-90deg)' }}>
          <circle
            cx={center} cy={center} r={radius}
            fill="none"
            stroke="rgba(255,255,255,.06)"
            strokeWidth={strokeWidth}
          />
          <circle
            cx={center} cy={center} r={radius}
            fill="none"
            stroke={color}
            strokeWidth={strokeWidth}
            strokeDasharray={circumference}
            strokeDashoffset={offset}
            strokeLinecap="round"
            style={{
              transition: animate ? 'stroke-dashoffset 1.2s cubic-bezier(.4,0,.2,1)' : 'none',
              filter: `drop-shadow(0 0 6px ${color}66)`,
            }}
          />
        </svg>
        {label !== undefined && (
          <div style={{
            position: 'absolute', inset: 0,
            display: 'flex', alignItems: 'center', justifyContent: 'center',
            fontFamily: "'JetBrains Mono', 'Cascadia Code', Consolas, monospace",
            fontSize: size * 0.28, fontWeight: 800, color,
            letterSpacing: '-.02em',
            textShadow: `0 0 12px ${color}44`,
          }}>
            {label}
          </div>
        )}
      </div>
      {sub && (
        <div style={{
          fontSize: 10, fontWeight: 700, color: '#6b7db3',
          textTransform: 'uppercase', letterSpacing: '.1em', textAlign: 'center',
        }}>
          {sub}
        </div>
      )}
    </div>
  );
}
