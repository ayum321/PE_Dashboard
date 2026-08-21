import React, { ChangeEvent, DragEvent, useState } from 'react';

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
  /** 0-100 while an upload for this tile is in flight; undefined/null when idle. */
  progress?: number | null;
  onFiles: (files: File[]) => void;
}

/** Matches the real dashboard's dashed drop-zone upload tiles (Upload & Intake) —
 * click-to-browse, drag-and-drop, and a live upload-percentage bar. */
export function UploadTile({ id, icon, accent, title, hint, browseLabel = 'browse', accept, multiple, disabled, compact, progress, onFiles }: UploadTileProps) {
  const [dragOver, setDragOver] = useState(false);
  const uploading = progress != null && progress >= 0 && progress < 100;

  const acceptedExts = accept.split(',').map((s) => s.trim().toLowerCase()).filter(Boolean);
  const isAccepted = (name: string) => {
    if (!acceptedExts.length) return true;
    const lower = name.toLowerCase();
    return acceptedExts.some((ext) => lower.endsWith(ext));
  };

  const handleChange = (event: ChangeEvent<HTMLInputElement>) => {
    const files = Array.from(event.target.files || []);
    event.target.value = '';
    if (files.length) onFiles(files);
  };

  const handleDragOver = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    event.stopPropagation();
    if (!disabled) setDragOver(true);
  };
  const handleDragLeave = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    event.stopPropagation();
    setDragOver(false);
  };
  const handleDrop = (event: DragEvent<HTMLLabelElement>) => {
    event.preventDefault();
    event.stopPropagation();
    setDragOver(false);
    if (disabled) return;
    const dropped = Array.from(event.dataTransfer.files || []).filter((f) => isAccepted(f.name));
    const files = multiple ? dropped : dropped.slice(0, 1);
    if (files.length) onFiles(files);
  };

  return (
    <label
      htmlFor={id}
      className="upload-tile"
      onDragOver={handleDragOver}
      onDragEnter={handleDragOver}
      onDragLeave={handleDragLeave}
      onDrop={handleDrop}
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
        position: 'relative',
        borderRadius: 12,
        border: dragOver ? `2px dashed ${accent}` : '2px dashed transparent',
        background: dragOver ? `${accent}14` : undefined,
        transition: 'background .15s ease, border-color .15s ease',
      }}
    >
      <input id={id} type="file" accept={accept} multiple={multiple} disabled={disabled} onChange={handleChange} style={{ display: 'none' }} />
      <div
        className="upload-tile-icon"
        style={{ background: `${accent}1f`, color: accent, border: `1px solid ${accent}4d`, flexShrink: 0 }}
      >
        {icon}
      </div>
      <div style={{ minWidth: 0, width: '100%' }}>
        <div style={{ fontSize: compact ? 11 : 12, fontWeight: 700, color: '#f0f4ff' }}>{title}</div>
        <div style={{ fontSize: 10, color: '#6b7db3', fontFamily: "'JetBrains Mono', monospace", marginTop: 2 }}>{hint}</div>
        {uploading ? (
          <div style={{ marginTop: 6 }}>
            <div style={{ height: 5, borderRadius: 3, background: 'rgba(255,255,255,.08)', overflow: 'hidden' }}>
              <div
                style={{
                  height: '100%',
                  width: `${Math.max(4, progress)}%`,
                  borderRadius: 3,
                  background: `linear-gradient(90deg, ${accent}99, ${accent})`,
                  boxShadow: `0 0 8px ${accent}99`,
                  transition: 'width .2s ease',
                }}
              />
            </div>
            <div style={{ fontSize: 9, color: accent, fontWeight: 700, marginTop: 3, fontFamily: "'JetBrains Mono', monospace" }}>
              {'Uploading\u2026 '}{progress}%
            </div>
          </div>
        ) : (
          <div style={{ fontSize: 10, color: dragOver ? accent : '#6b7db3', marginTop: 2, fontWeight: dragOver ? 700 : 400 }}>
            {dragOver ? 'Drop to upload' : <>or <span style={{ color: accent, fontWeight: 700 }}>{browseLabel}</span>{' \u00b7 drag & drop'}</>}
          </div>
        )}
      </div>
    </label>
  );
}
