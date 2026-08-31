import React, { useState } from 'react';
import {
  Box,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
  TextField,
  InputAdornment,
  Button
} from '@material-ui/core';

export interface FindingItem {
  level: string;
  text: string;
  sub?: string;
  impact?: string;
  recommendation?: string;
  evidence?: string;
  root_cause?: string;
  confidence?: number;
  evidence_class?: string;
  source?: string;
}

// Collapsing "OK" (passed-check) findings behind a summary row only helps
// readability when there are ENOUGH of them to genuinely be noise. Below
// this count, a single passing check mixed into a small findings list is a
// real, distinct data point — collapsing it hides information instead of
// reducing clutter, so it renders inline like any other finding.
const OK_AUTO_COLLAPSE_THRESHOLD = 3;

interface FindingsDataGridProps {
  findings: FindingItem[];
  filter: 'all' | 'critical' | 'warning' | 'info' | 'ok';
  onFilterChange: (filter: 'all' | 'critical' | 'warning' | 'info' | 'ok') => void;
  counts: { all: number; critical: number; warning: number; info: number; ok: number };
}

const SEVERITY_COLOR: Record<string, { color: string; bg: string }> = {
  critical: { color: '#f43f5e', bg: 'rgba(244,63,94,.14)' },
  warning: { color: '#f59e0b', bg: 'rgba(245,158,11,.14)' },
  info: { color: '#3b82f6', bg: 'rgba(59,130,246,.14)' },
  ok: { color: '#10d96e', bg: 'rgba(16,217,110,.14)' },
};

export function FindingsDataGrid({ findings, filter, onFilterChange, counts }: FindingsDataGridProps) {
  const [expandedRows, setExpandedRows] = useState<Set<string>>(new Set());
  const [searchQuery, setSearchQuery] = useState('');
  const [viewMode, setViewMode] = useState<'flat' | 'grouped'>('flat');
  const [okExpanded, setOkExpanded] = useState(false);

  const toggleRow = (id: string) => {
    setExpandedRows((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const handleRowKeyDown = (e: React.KeyboardEvent, id: string) => {
    if (e.key === 'Enter' || e.key === ' ') {
      e.preventDefault();
      toggleRow(id);
    }
  };

  const filtered = findings
    .filter((f) => filter === 'all' || f.level.toLowerCase() === filter)
    .filter((f) => {
      if (!searchQuery.trim()) return true;
      const q = searchQuery.toLowerCase();
      return (
        f.text.toLowerCase().includes(q) ||
        (f.sub && f.sub.toLowerCase().includes(q)) ||
        (f.recommendation && f.recommendation.toLowerCase().includes(q)) ||
        (f.source && f.source.toLowerCase().includes(q)) ||
        (f.root_cause && f.root_cause.toLowerCase().includes(q))
      );
    });

  const filteredOk = filtered.filter(f => f.level.toLowerCase() === 'ok');
  const filteredOther = filtered.filter(f => f.level.toLowerCase() !== 'ok');

  const uniqueEvidenceClasses = new Set(filtered.map(f => f.evidence_class || ''));
  const showEvidenceColumn = uniqueEvidenceClasses.size > 1;

  let visibleFindings = filtered;
  if (filter === 'all' && filteredOk.length > 0 && !okExpanded) {
    visibleFindings = filteredOther;
  }

  const getRecommendationChip = (rec?: string) => {
    if (!rec) return null;
    const match = rec.match(/^[^,.]+/);
    let text = match ? match[0] : rec;
    if (text.length > 60) text = text.slice(0, 60) + '...';
    return (
      <span style={{ 
        fontSize: 11, 
        background: 'rgba(59,130,246,.1)', 
        borderRadius: 4, 
        padding: '2px 8px', 
        color: '#60a5fa' 
      }}>
        {text}
      </span>
    );
  };

  const renderFindingRow = (f: FindingItem, idx: number) => {
    const rowId = `${f.level}-${idx}-${f.text.slice(0, 20)}`;
    const isExpanded = expandedRows.has(rowId);
    const sev = f.level.toLowerCase();
    const config = SEVERITY_COLOR[sev] || SEVERITY_COLOR.info;

    return (
      <React.Fragment key={rowId}>
        <TableRow
          hover
          tabIndex={0}
          onKeyDown={(e) => handleRowKeyDown(e, rowId)}
          aria-expanded={isExpanded}
          style={{
            cursor: 'pointer',
            background: isExpanded ? 'rgba(59, 130, 246, .08)' : undefined,
            borderLeft: `3px solid ${config.color}`,
          }}
          onClick={() => toggleRow(rowId)}
        >
          {/* Severity badge */}
          <TableCell style={{ verticalAlign: 'top', paddingTop: 10 }}>
            <span
              className="metric-badge"
              style={{
                fontSize: 10,
                padding: '2px 7px',
                color: config.color,
                borderColor: `${config.color}55`,
                background: config.bg,
                fontWeight: 800,
              }}
            >
              {sev.toUpperCase()}
            </span>
          </TableCell>

          {/* Pillar source */}
          <TableCell style={{ verticalAlign: 'top', paddingTop: 10 }}>
            <span
              className="metric-badge metric-badge-blue"
              style={{ fontSize: 9, padding: '2px 6px', fontWeight: 700 }}
            >
              {(f.source || 'GENERAL').toUpperCase()}
            </span>
          </TableCell>

          {/* Finding Title & Sub */}
          <TableCell style={{ verticalAlign: 'top', paddingTop: 10 }}>
            <Typography style={{ fontSize: 13, fontWeight: 'bold', color: '#f0f4ff', lineHeight: 1.35 }}>
              {f.text}
            </Typography>
            {f.sub && !isExpanded && (
              <Typography
                style={{
                  fontSize: 11,
                  color: '#91a7d8',
                  display: '-webkit-box',
                  WebkitLineClamp: 1,
                  WebkitBoxOrient: 'vertical',
                  overflow: 'hidden',
                  marginTop: 2,
                }}
              >
                {f.sub}
              </Typography>
            )}
          </TableCell>

          {/* Recommendation */}
          <TableCell style={{ verticalAlign: 'top', paddingTop: 10 }}>
            {f.recommendation ? getRecommendationChip(f.recommendation) : (
              <Typography variant="caption" style={{ color: '#6b7db3' }}>—</Typography>
            )}
          </TableCell>

          {/* Evidence quality */}
          {showEvidenceColumn && (
            <TableCell style={{ verticalAlign: 'top', paddingTop: 10, textAlign: 'center' }}>
              {f.evidence_class ? (
                <span
                  className="metric-badge metric-badge-blue"
                  title={f.confidence != null ? `Evidence quality score: ${f.confidence}%` : undefined}
                  style={{ fontSize: 9, padding: '2px 6px', fontWeight: 700 }}
                >
                  {f.evidence_class.replace(/_/g, ' ').toUpperCase()}
                </span>
              ) : (
                <span style={{ color: '#6b7db3' }}>—</span>
              )}
            </TableCell>
          )}

          {/* Expand toggle */}
          <TableCell style={{ verticalAlign: 'top', paddingTop: 10, textAlign: 'center' }}>
            <span
              aria-hidden="true"
              style={{
                display: 'inline-block',
                transform: isExpanded ? 'rotate(90deg)' : 'rotate(0deg)',
                transition: 'transform .2s',
                color: '#7dd3fc',
                fontSize: 12,
              }}
            >
              ▶
            </span>
          </TableCell>
        </TableRow>

        {/* Expandable Detailed Drawer */}
        {isExpanded && (
          <TableRow style={{ background: 'rgba(6, 12, 26, .75)' }}>
            <TableCell colSpan={showEvidenceColumn ? 6 : 5} style={{ padding: '12px 20px', borderBottom: '1px solid rgba(59, 130, 246, .2)' }}>
              <Box style={{ display: 'grid', gridTemplateColumns: '1fr 1fr', gap: 16 }}>
                {/* Left: Detail & Root Cause */}
                <Box style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {f.sub && (
                    <Box>
                      <Typography style={{ fontSize: 11, color: '#6b7db3', fontWeight: 800, textTransform: 'uppercase', letterSpacing: '.06em' }}>
                        Observation Evidence
                      </Typography>
                      <Typography variant="body2" style={{ color: '#e2e8f0', marginTop: 2, lineHeight: 1.5 }}>
                        {f.sub}
                      </Typography>
                    </Box>
                  )}
                  {f.impact && (
                    <Box>
                      <Typography style={{ fontSize: 11, color: '#f59e0b', fontWeight: 800, textTransform: 'uppercase', letterSpacing: '.06em' }}>
                        Operational Impact
                      </Typography>
                      <Typography variant="body2" style={{ color: '#fed7aa', marginTop: 2, lineHeight: 1.5 }}>
                        {f.impact}
                      </Typography>
                    </Box>
                  )}
                </Box>

                {/* Right: Recommendation & Provenance */}
                <Box style={{ display: 'flex', flexDirection: 'column', gap: 8 }}>
                  {f.recommendation && (
                    <Box style={{ padding: '8px 12px', background: 'rgba(59, 130, 246, .1)', borderRadius: 6, borderLeft: '3px solid #3b82f6' }}>
                      <Typography style={{ fontSize: 11, color: '#60a5fa', fontWeight: 800, textTransform: 'uppercase', letterSpacing: '.06em' }}>
                        Recommended PE Remediation
                      </Typography>
                      <Typography variant="body2" style={{ color: '#f0f4ff', marginTop: 2, lineHeight: 1.5 }}>
                        {f.recommendation}
                      </Typography>
                    </Box>
                  )}
                  <Box display="flex" flexWrap="wrap" style={{ gap: 16 }}>
                    {f.root_cause && (
                      <Typography variant="caption" style={{ color: '#94a3b8' }}>
                        <strong>Root Cause:</strong> <code style={{ color: '#38bdf8' }}>{f.root_cause}</code>
                      </Typography>
                    )}
                    {f.evidence && (
                      <Typography variant="caption" style={{ color: '#94a3b8' }}>
                        <strong>Evidence Fact:</strong> {f.evidence}
                      </Typography>
                    )}
                  </Box>
                </Box>
              </Box>
            </TableCell>
          </TableRow>
        )}
      </React.Fragment>
    );
  };

  return (
    <Box className="kpi-card" style={{ padding: '16px 20px', borderRadius: 14, marginTop: 14 }}>
      {/* Header Toolbar */}
      <Box display="flex" justifyContent="space-between" alignItems="center" flexWrap="wrap" style={{ gap: 12, marginBottom: 12 }}>
        <Box display="flex" alignItems="center" style={{ gap: 10 }}>
          <Typography variant="h6" style={{ fontWeight: 800, color: '#f0f4ff' }}>
            Findings Ledger
          </Typography>
          <span className="metric-badge metric-badge-blue" style={{ fontSize: 11 }}>
            {filtered.length} of {counts.all} findings
          </span>
        </Box>

        <Box display="flex" alignItems="center" style={{ gap: 12 }}>
          <Box display="flex" style={{ gap: 4, background: 'rgba(6, 12, 26, .6)', padding: 4, borderRadius: 8 }}>
            <Button 
              size="small" 
              onClick={() => setViewMode('flat')}
              style={{ 
                minWidth: 0, 
                color: viewMode === 'flat' ? '#f0f4ff' : '#6b7db3', 
                background: viewMode === 'flat' ? 'rgba(59, 130, 246, .2)' : 'transparent',
                fontSize: 12,
                fontWeight: 700
              }}
            >
              ≡ Flat
            </Button>
            <Button 
              size="small" 
              onClick={() => setViewMode('grouped')}
              style={{ 
                minWidth: 0, 
                color: viewMode === 'grouped' ? '#f0f4ff' : '#6b7db3', 
                background: viewMode === 'grouped' ? 'rgba(59, 130, 246, .2)' : 'transparent',
                fontSize: 12,
                fontWeight: 700
              }}
            >
              ⊞ Grouped
            </Button>
          </Box>

          {/* Search */}
          <TextField
            size="small"
            placeholder="Filter by keyword, job, server…"
            value={searchQuery}
            onChange={(e) => setSearchQuery(e.target.value)}
            variant="outlined"
            style={{ minWidth: 260 }}
            InputProps={{
              style: { height: 34, fontSize: 12, color: '#f0f4ff', background: 'rgba(6, 12, 26, .6)', borderRadius: 8 },
              startAdornment: <InputAdornment position="start" style={{ color: '#6b7db3' }}>🔍</InputAdornment>,
            }}
          />
        </Box>
      </Box>

      {/* Filter Tabs */}
      <div role="tablist" className="pe-filter-tabs" style={{ marginBottom: 12, marginTop: 0 }}>
        {([
          { key: 'all' as const, label: 'All', count: counts.all },
          { key: 'critical' as const, label: 'Critical', count: counts.critical, color: '#f43f5e' },
          { key: 'warning' as const, label: 'Warning', count: counts.warning, color: '#f59e0b' },
          { key: 'info' as const, label: 'Info', count: counts.info, color: '#3b82f6' },
          { key: 'ok' as const, label: 'OK', count: counts.ok, color: '#10d96e' },
        ]).map((tab) => (
          <button
            key={tab.key}
            role="tab"
            aria-selected={filter === tab.key}
            className={`pe-filter-tab ${filter === tab.key ? 'pe-filter-tab--active' : ''}`}
            onClick={() => {
              onFilterChange(tab.key);
              if (tab.key !== 'all') {
                setOkExpanded(true); // Always expand if explicitly choosing OK or other filters that might show OK if data somehow matches
              } else {
                setOkExpanded(false);
              }
            }}
            type="button"
          >
            {tab.label}
            <span
              className="pe-filter-tab__count"
              style={{
                color: tab.color && filter === tab.key ? tab.color : undefined,
                fontWeight: 800,
              }}
            >
              {tab.count}
            </span>
          </button>
        ))}
      </div>

      {/* Findings Data Table */}
      <Box className="pe-table-shell" style={{ border: '1px solid rgba(59, 130, 246, .2)', borderRadius: 8, maxHeight: 600, overflowY: 'auto' }}>
        <Table size="small" className="pe-table" stickyHeader>
          <TableHead>
            <TableRow>
              <TableCell style={{ width: 90, color: '#6b7db3', fontWeight: 800 }}>Severity</TableCell>
              <TableCell style={{ width: 90, color: '#6b7db3', fontWeight: 800 }}>Pillar</TableCell>
              <TableCell style={{ color: '#6b7db3', fontWeight: 800 }}>Finding & Evidence</TableCell>
              <TableCell style={{ width: 280, color: '#6b7db3', fontWeight: 800 }}>Recommended Action</TableCell>
              {showEvidenceColumn && (
                <TableCell style={{ width: 110, color: '#6b7db3', fontWeight: 800, textAlign: 'center' }}>Evidence Quality</TableCell>
              )}
              <TableCell style={{ width: 60, color: '#6b7db3', fontWeight: 800, textAlign: 'center' }}>Details</TableCell>
            </TableRow>
          </TableHead>
          <TableBody>
            {filtered.length === 0 ? (
              <TableRow>
                <TableCell colSpan={showEvidenceColumn ? 6 : 5} style={{ textAlign: 'center', padding: '32px 16px', color: '#6b7db3' }}>
                  No findings matching current filters.
                </TableCell>
              </TableRow>
            ) : (
              <>
                {viewMode === 'flat' ? (
                  visibleFindings.map((f, idx) => renderFindingRow(f, idx))
                ) : (
                  (() => {
                    const grouped = visibleFindings.reduce((acc, f) => {
                      const source = (f.source || 'GENERAL').toUpperCase();
                      if (!acc[source]) acc[source] = [];
                      acc[source].push(f);
                      return acc;
                    }, {} as Record<string, FindingItem[]>);
                    
                    return Object.entries(grouped).map(([source, groupFindings]) => (
                      <React.Fragment key={source}>
                        <TableRow style={{ background: 'rgba(6, 12, 26, 0.9)' }}>
                          <TableCell colSpan={showEvidenceColumn ? 6 : 5} style={{ padding: '8px 16px', borderBottom: '1px solid #213060' }}>
                            <Typography style={{ fontSize: 12, fontWeight: 800, color: '#f0f4ff', letterSpacing: '0.05em' }}>
                              {source} <span style={{ color: '#6b7db3', fontWeight: 'normal', marginLeft: 8 }}>({groupFindings.length})</span>
                            </Typography>
                          </TableCell>
                        </TableRow>
                        {groupFindings.map((f, idx) => renderFindingRow(f, idx))}
                      </React.Fragment>
                    ));
                  })()
                )}

                {/* Collapsed OK Summary Row */}
                {filter === 'all' && filteredOk.length > 0 && !okExpanded && (
                  <TableRow
                    hover
                    tabIndex={0}
                    onKeyDown={(e) => {
                      if (e.key === 'Enter' || e.key === ' ') {
                        e.preventDefault();
                        setOkExpanded(true);
                      }
                    }}
                    style={{ cursor: 'pointer', background: 'rgba(16, 217, 110, .05)' }}
                    onClick={() => setOkExpanded(true)}
                  >
                    <TableCell colSpan={showEvidenceColumn ? 6 : 5} style={{ padding: '16px', textAlign: 'center', borderTop: '1px solid #213060' }}>
                      <Box display="flex" alignItems="center" justifyContent="center" style={{ gap: 8 }}>
                        <span style={{ color: '#10d96e', fontWeight: 800 }}>✓ {filteredOk.length} passed checks — all within thresholds</span>
                        <span style={{ color: '#6b7db3', fontSize: 12 }}>Click to expand</span>
                      </Box>
                    </TableCell>
                  </TableRow>
                )}
              </>
            )}
          </TableBody>
        </Table>
      </Box>
    </Box>
  );
}
