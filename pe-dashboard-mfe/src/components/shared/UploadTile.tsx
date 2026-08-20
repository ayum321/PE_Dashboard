import React, { ChangeEvent } from 'react';

interface UploadTileProps {
  id: string;
  icon: React.ReactNode;
  accent: string;
  title: string;
  hint: string;
  browseLabel?: string;
  accept: string;
  multiple?: boolean;
  disabled?: boolean;
  compact?: boolean;
  onFiles: (files: File[]) => void;
}

/** Matches the real dashboard's dashed drop-zone upload tiles (Upload & Intake). */
export function UploadTile({ id, icon, accent, title, hint, browseLabel = 'browse', accept, multiple, disabled, compact, onFiles }: UploadTileProps) {
  const handleChange = (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []);
    event.target.value = '';
    if (files.length) onFiles(files);
  };

  return (
    <label
      htmlFor={id}
      className="upload-tile"
      style={{
        display: 'flex',
        flexDirection: compact ? 'row' : 'column',
        alignItems: 'center',
        gap: compact ? 12 : 8,
        textAlign: compact ? 'left' : 'center',
        padding: compact ? '12px 14px' : '24px 16px',
        cursor: disabled ? 'default' : 'pointer',
        opacity: disabled ? 0.6 : 1,
        minHeight: compact ? 72 : 130,
      }}
    >
      <input id={id} type="file" accept={accept} multiple={multiple} disabled={disabled} onChange={handleChange} style={{ display: 'none' }} />
      <div
        className="upload-tile-icon"
        style={{ background: `${accent}1f`, color: accent, border: `1px solid ${accent}4d`, flexShrink: 0 }}
      >
        {icon}
      </div>
      <div style={{ minWidth: 0 }}>
        <div style={{ fontSize: compact ? 11 : 12, fontWeight: 700, color: '#f0f4ff' }}>{title}</div>
        <div style={{ fontSize: 10, color: '#6b7db3', fontFamily: "'JetBrains Mono', monospace", marginTop: 2 }}>{hint}</div>
        <div style={{ fontSize: 10, color: '#6b7db3', marginTop: 2 }}>
          or <span style={{ color: accent, fontWeight: 700 }}>{browseLabel}</span>
        </div>
      </div>
    </label>
  );
}
