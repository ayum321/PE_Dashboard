import React, { useState } from 'react';
import { isValidCustomerName, useAppData } from '../../context/AppDataContext';
import { postCustomerOverride } from '../../api/dashboardApi';
import '../../theme/dashboard.css';
import { ResetIcon } from '../../theme/icons';

export function Header() {
  const { data, resetSession, lastSyncTime, isLiveSyncing, syncLiveState, setCustomerName } = useAppData();
  const [resetting, setResetting] = useState(false);
  const [editing, setEditing] = useState(false);
  const [editValue, setEditValue] = useState('');
  const [saving, setSaving] = useState(false);
  const [editError, setEditError] = useState<string | null>(null);

  const handleReset = async () => {
    setResetting(true);
    try {
      await resetSession();
    } finally {
      setResetting(false);
    }
  };

  const startEdit = () => {
    setEditError(null);
    setEditValue(isValidCustomerName(data.customerName) ? (data.customerName as string) : '');
    setEditing(true);
  };

  const cancelEdit = () => {
    setEditing(false);
    setEditError(null);
  };

  const saveEdit = async () => {
    const trimmed = editValue.trim();
    if (!trimmed) {
      setEditError('Enter a customer name.');
      return;
    }
    setSaving(true);
    setEditError(null);
    try {
      const result = await postCustomerOverride(trimmed);
      setCustomerName(result.customer_name, result.customer_verified_by_resource);
      setEditing(false);
    } catch (err) {
      setEditError(err instanceof Error ? err.message : 'Could not update customer name.');
    } finally {
      setSaving(false);
    }
  };

  const hasCustomer = isValidCustomerName(data.customerName);
  const needsVerification = hasCustomer && !data.customerVerifiedByResource;

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
          {editing ? (
            <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginTop: 3 }}>
              <input
                autoFocus
                value={editValue}
                onChange={(e) => setEditValue(e.target.value)}
                onKeyDown={(e) => {
                  if (e.key === 'Enter') void saveEdit();
                  if (e.key === 'Escape') cancelEdit();
                }}
                placeholder="Customer name"
                style={{
                  fontSize: 11, padding: '3px 6px', borderRadius: 5,
                  border: '1px solid rgba(59,130,246,.5)', background: 'rgba(13,21,38,.9)',
                  color: '#f0f4ff', width: 160,
                }}
              />
              <button
                type="button"
                onClick={() => void saveEdit()}
                disabled={saving}
                style={{ fontSize: 10, fontWeight: 600, padding: '3px 8px', borderRadius: 5, border: '1px solid rgba(16,217,110,.4)', color: '#10d96e', background: 'rgba(16,217,110,.08)', cursor: saving ? 'default' : 'pointer' }}
              >
                {saving ? 'Saving…' : 'Save'}
              </button>
              <button
                type="button"
                onClick={cancelEdit}
                disabled={saving}
                style={{ fontSize: 10, fontWeight: 600, padding: '3px 8px', borderRadius: 5, border: '1px solid #21306099', color: '#6b7db3', background: 'transparent', cursor: saving ? 'default' : 'pointer' }}
              >
                Cancel
              </button>
              {editError && <span style={{ fontSize: 10, color: '#f43f5e' }}>{editError}</span>}
            </div>
          ) : (
            hasCustomer && (
              <p style={{ margin: '2px 0 0', fontSize: 11, color: '#6b7db3', fontWeight: 500, display: 'flex', alignItems: 'center', gap: 6 }}>
                Customer: {data.customerName}
                <button
                  type="button"
                  onClick={startEdit}
                  title="Edit customer name"
                  style={{ border: 'none', background: 'transparent', color: '#6b7db3', cursor: 'pointer', fontSize: 11, padding: 0, lineHeight: 1 }}
                >
                  ✎
                </button>
                {needsVerification && (
                  <span
                    title="Not yet confirmed by a Resource Utilization report (the most reliable source). Verify or edit this name, or upload the Resource report."
                    style={{
                      fontSize: 9, fontWeight: 700, letterSpacing: '.02em', padding: '1px 6px',
                      borderRadius: 8, border: '1px solid rgba(245,158,11,.4)', color: '#f59e0b',
                      background: 'rgba(245,158,11,.08)', cursor: 'help',
                    }}
                  >
                    UNVERIFIED
                  </span>
                )}
              </p>
            )
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

