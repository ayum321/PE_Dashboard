import React, { useState } from 'react';
import { useAppData } from '../../context/AppDataContext';
import '../../theme/dashboard.css';
import { ResetIcon } from '../../theme/icons';

export function Header() {
  const { data, resetSession } = useAppData();
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
          {data.customerName && (
            <p style={{ margin: '2px 0 0', fontSize: 11, color: '#6b7db3', fontWeight: 500 }}>Customer: {data.customerName}</p>
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
    </header>
  );
}

