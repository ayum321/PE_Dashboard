import React, { useState } from 'react';
import { isValidCustomerName, useAppData } from '../../context/AppDataContext';
import '../../theme/dashboard.css';
import { ResetIcon } from '../../theme/icons';

export function Header() {
  const { data, resetSession, lastSyncTime, isLiveSyncing, syncLiveState } = useAppData();
  const [resetting, setResetting] = useState(false);

  const handleReset = async () => {
    setResetting(true);
    try {
      await resetSession();
    } finally {
      setResetting(false);
    }
  };

  return (
    <header
      style={{
        position: 'sticky',
        top: 0,
        zIndex: 30,
        background: 'rgba(13,21,38,.8)',
        backdropFilter: 'blur(6px)',
        borderBottom: '1px solid #21306099',
      }}
    >
      <div className="header-accent" />
      <div style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '10px 24px' }}>
        <div>
          <h1 style={{ margin: 0, fontSize: 14, fontWeight: 700, letterSpacing: '-0.01em', color: '#f0f4ff' }}>PE Audit Dashboard</h1>
          {isValidCustomerName(data.customerName) && (
            <p style={{ margin: '2px 0 0', fontSize: 11, color: '#6b7db3', fontWeight: 500 }}>Customer: {data.customerName}</p>
          )}
        </div>
        <div style={{ display: 'flex', alignItems: 'center', gap: 12 }}>
          <div
            onClick={() => void syncLiveState()}
            role="button"
            tabIndex={0}
            title={
              isLiveSyncing
                ? 'Syncing live session state...'
                : `Real-time synchronized session. Auto-updated every 10s. Click to sync now.${
                    lastSyncTime ? ` Last sync: ${new Date(lastSyncTime).toLocaleTimeString()}` : ''
                  }`
            }
            style={{
              display: 'inline-flex',
              alignItems: 'center',
              gap: 6,
              padding: '4px 10px',
              borderRadius: 14,
              background: isLiveSyncing ? 'rgba(59,130,246,.12)' : 'rgba(16,217,110,.08)',
              border: `1px solid ${isLiveSyncing ? 'rgba(59,130,246,.35)' : 'rgba(16,217,110,.25)'}`,
              fontSize: 11,
              fontWeight: 600,
              color: isLiveSyncing ? '#93c5fd' : '#10d96e',
              cursor: 'pointer',
              userSelect: 'none',
              transition: 'all 0.2s ease',
            }}
          >
            <span
              style={{
                width: 6,
                height: 6,
                borderRadius: '50%',
                background: isLiveSyncing ? '#3b82f6' : '#10d96e',
                boxShadow: `0 0 6px ${isLiveSyncing ? '#3b82f6' : '#10d96e'}`,
                display: 'inline-block',
              }}
            />
            <span>{isLiveSyncing ? 'Syncing…' : 'Live · Auto-updated'}</span>
            {lastSyncTime && !isLiveSyncing && (
              <span style={{ color: '#6b7db3', fontSize: 10, marginLeft: 2, fontFamily: 'monospace' }}>
                {new Date(lastSyncTime).toLocaleTimeString('en-US', { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false })}
              </span>
            )}
          </div>
          <button
            type="button"
            onClick={handleReset}
            disabled={resetting}
            title="Hard reset: wipe all session data and start fresh for a new customer"
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: 6,
              fontSize: 11,
              fontWeight: 600,
              padding: '6px 12px',
              borderRadius: 8,
              border: '1px solid rgba(244,63,94,.4)',
              color: 'rgba(244,63,94,.85)',
              background: 'rgba(244,63,94,.05)',
              cursor: resetting ? 'default' : 'pointer',
            }}
          >
            <ResetIcon />
            {resetting ? 'Resetting...' : 'New Engagement'}
          </button>
        </div>
      </div>
    </header>
  );
}

