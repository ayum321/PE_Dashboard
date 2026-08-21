import React, { ChangeEvent, useEffect, useMemo, useState } from 'react';
import {
  Box,
  Button,
  Checkbox,
  CircularProgress,
  Dialog,
  IconButton,
  TextField,
  Typography,
} from '@material-ui/core';
import {
  compareSow,
  getReviewedProducts,
  getSowBaseline,
  getSowProductTaxonomy,
  parseSow,
  saveReviewedProducts,
  saveSowBaseline,
} from '../../api/dashboardApi';
import { useAppData } from '../../context/AppDataContext';

interface SowMetric {
  key: string;
  label: string;
  sow: number;
  actual: number;
  pct: number;
  status: string;
  over_by?: number;
  over_by_pct?: number;
}

interface Overconsumption {
  count: number;
  critical_count: number;
  severity: string;
  worst_label: string;
  worst_pct: number;
  worst_over_by: number;
}

interface TaxonomyItem { value: string; label: string }
interface TaxonomyGroup { key: string; label: string; items: TaxonomyItem[] }

const SOW_FIELDS: Array<{ key: string; label: string; sub: string; icon: string; placeholder: string }> = [
  { key: 'daily_dfu', label: 'Daily DFU', sub: 'Demand Forecast Units', icon: '\u{1F4E6}', placeholder: 'e.g. 500000' },
  { key: 'daily_sku', label: 'Daily SKU Count', sub: 'Stock Keeping Units', icon: '\u{1F3F7}\uFE0F', placeholder: 'e.g. 80000' },
  { key: 'daily_orders', label: 'Daily Orders', sub: 'Transaction volume', icon: '\u{1F4CB}', placeholder: 'e.g. 200000' },
  { key: 'batch_jobs', label: 'Batch Jobs / Day', sub: 'Scheduled processes', icon: '\u2699\uFE0F', placeholder: 'e.g. 450' },
  { key: 'peak_users', label: 'Peak Concurrent Users', sub: 'Simultaneous sessions', icon: '\u{1F465}', placeholder: 'e.g. 500' },
];

const DEFAULT_BANDS = { under: 70, over: 110, crit: 120 };

interface Achievement { pct: number; color: string; label: string }

function computeAchievement(base: number, actual: number, bands: typeof DEFAULT_BANDS): Achievement | 'awaiting' | null {
  if (!base) return null;
  if (!actual) return 'awaiting';
  const pct = (actual / base) * 100;
  if (pct > bands.crit) return { pct, color: '#dc2626', label: 'CRITICAL OVER' };
  if (pct > bands.over) return { pct, color: '#f43f5e', label: 'OVER' };
  if (pct >= 90) return { pct, color: '#10b981', label: 'OPTIMAL' };
  if (pct >= bands.under) return { pct, color: '#22d3ee', label: 'ACCEPTABLE' };
  return { pct, color: '#f59e0b', label: 'LOW' };
}

const STATUS_BADGE: Record<string, string> = {
  LOW: 'metric-badge-blue',
  ACCEPTABLE: 'metric-badge-teal',
  OPTIMAL: 'metric-badge-green',
  OVER: 'metric-badge-amber',
  CRITICAL_OVER: 'metric-badge-red',
};

function fmt(n: number): string {
  return Number.isFinite(n) ? n.toLocaleString() : '\u2014';
}

export function SowPanel() {
  const { data, setSowBaseline, setSowCompare, setReviewedProducts } = useAppData();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);

  const [baseValues, setBaseValues] = useState<Record<string, string>>({});
  const [actualValues, setActualValues] = useState<Record<string, string>>({});

  const [taxonomy, setTaxonomy] = useState<TaxonomyGroup[]>([]);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [draftSelected, setDraftSelected] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState('');
  const [activeFamily, setActiveFamily] = useState<string>('ALL');

  useEffect(() => {
    getSowBaseline()
      .then((baseline) => {
        setSowBaseline(baseline);
        const next: Record<string, string> = {};
        SOW_FIELDS.forEach(({ key }) => {
          if (baseline[key] != null) next[key] = String(baseline[key]);
        });
        setBaseValues(next);
      })
      .catch(() => undefined);
    getSowProductTaxonomy()
      .then((res) => setTaxonomy((res.groups as TaxonomyGroup[]) || []))
      .catch(() => undefined);
    getReviewedProducts()
      .then((res) => setReviewedProducts((res.products as string[]) || []))
      .catch(() => undefined);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // Pre-fill batch_jobs actual from Batch Review once it's loaded, same wiring as vanilla.
  useEffect(() => {
    const kpis = data.batch?.kpis as { total_jobs?: number } | undefined;
    if (kpis?.total_jobs != null) {
      setActualValues((prev) => (prev.batch_jobs ? prev : { ...prev, batch_jobs: String(kpis.total_jobs) }));
    }
  }, [data.batch]);

  const bandsFromServer = (data.sowCompare?.bands as { under?: number; over?: number; crit?: number } | undefined) || {};
  const bands = {
    under: bandsFromServer.under ?? DEFAULT_BANDS.under,
    over: bandsFromServer.over ?? DEFAULT_BANDS.over,
    crit: bandsFromServer.crit ?? DEFAULT_BANDS.crit,
  };

  const handleParse = async (event: ChangeEvent<HTMLInputElement>) => {
    const file = event.target.files?.[0];
    event.target.value = '';
    if (!file) return;
    setBusy(true);
    setError(null);
    try {
      const result = await parseSow(file);
      setSowBaseline(result);
      setBaseValues((prev) => {
        const next = { ...prev };
        SOW_FIELDS.forEach(({ key }) => {
          if (result[key] != null) next[key] = String(result[key]);
        });
        return next;
      });
    } catch (parseError) {
      setError(parseError instanceof Error ? parseError.message : 'SOW parse failed.');
    } finally {
      setBusy(false);
    }
  };

  const handleSaveAndCompare = async () => {
    const baseline: Record<string, number> = {};
    const actuals: Record<string, number> = {};
    SOW_FIELDS.forEach(({ key }) => {
      const b = Number(baseValues[key]);
      const a = Number(actualValues[key]);
      if (b > 0) baseline[key] = b;
      if (a > 0) actuals[key] = a;
    });
    if (Object.keys(baseline).length === 0) {
      setError('Enter at least one SOW target value before comparing.');
      return;
    }
    setBusy(true);
    setError(null);
    setSaveMsg(null);
    try {
      const savedBaseline = await saveSowBaseline(baseline);
      setSowBaseline(savedBaseline);
      const result = await compareSow({ actuals });
      setSowCompare(result);
      setSaveMsg('\u2705 Saved and compared');
      setTimeout(() => setSaveMsg(null), 3000);
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : 'SOW comparison failed.');
    } finally {
      setBusy(false);
    }
  };

  const handleClearAll = () => {
    setBaseValues({});
    setActualValues({});
    setSowCompare(null);
  };

  const openPicker = () => {
    setDraftSelected(new Set(data.reviewedProducts));
    setSearch('');
    setActiveFamily('ALL');
    setPickerOpen(true);
  };

  const visibleItems = useMemo(() => {
    const q = search.trim().toLowerCase();
    const groups = activeFamily === 'ALL' ? taxonomy : taxonomy.filter((g) => g.key === activeFamily);
    const out: Array<{ group: string; item: TaxonomyItem }> = [];
    groups.forEach((g) => {
      g.items.forEach((it) => {
        if (!q || it.label.toLowerCase().includes(q) || it.value.toLowerCase().includes(q)) {
          out.push({ group: g.label, item: it });
        }
      });
    });
    return out;
  }, [taxonomy, search, activeFamily]);

  const toggleDraft = (value: string) => {
    setDraftSelected((prev) => {
      const next = new Set(prev);
      if (next.has(value)) next.delete(value); else next.add(value);
      return next;
    });
  };
  const selectAllVisible = () => setDraftSelected((prev) => {
    const next = new Set(prev);
    visibleItems.forEach(({ item }) => next.add(item.value));
    return next;
  });
  const clearAllDraft = () => setDraftSelected(new Set());

  const handleSaveScope = async () => {
    const products = Array.from(draftSelected);
    try {
      const res = await saveReviewedProducts(products);
      setReviewedProducts((res.products as string[]) || products);
      setPickerOpen(false);
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Could not save products reviewed selection.');
    }
  };

  const productLabelMap = useMemo(() => {
    const map: Record<string, string> = {};
    taxonomy.forEach((g) => g.items.forEach((it) => { map[it.value] = it.label; }));
    return map;
  }, [taxonomy]);

  const metrics = (data.sowCompare?.metrics as SowMetric[]) || [];
  const overconsumption = data.sowCompare?.overconsumption as Overconsumption | undefined;
  const summary = typeof data.sowCompare?.summary === 'string' ? data.sowCompare.summary : '';
  const overallStatus = data.sowCompare?.overall_status ? String(data.sowCompare.overall_status) : null;
  const reviewedProducts = data.reviewedProducts || [];

  const OVERALL_BADGE: Record<string, { bg: string; color: string; border: string; icon: string; label?: string }> = {
    OPTIMAL: { bg: 'rgba(16,185,129,.15)', color: '#10b981', border: 'rgba(16,185,129,.4)', icon: '\u2705' },
    ACCEPTABLE: { bg: 'rgba(16,185,129,.15)', color: '#10b981', border: 'rgba(16,185,129,.4)', icon: '\u2705' },
    MODERATE: { bg: 'rgba(245,158,11,.15)', color: '#f59e0b', border: 'rgba(245,158,11,.4)', icon: '\u26A0\uFE0F' },
    LOW: { bg: 'rgba(245,158,11,.15)', color: '#f59e0b', border: 'rgba(245,158,11,.4)', icon: '\u{1F4C9}' },
    OVER: { bg: 'rgba(244,63,94,.15)', color: '#f43f5e', border: 'rgba(244,63,94,.5)', icon: '\u{1F534}', label: 'OVERCONSUMPTION' },
    CRITICAL_OVER: { bg: 'rgba(244,63,94,.25)', color: '#f43f5e', border: 'rgba(244,63,94,.7)', icon: '\u{1F6D1}', label: 'CRITICAL OVERCONSUMPTION' },
  };

  return (
    <Box style={{ display: 'flex', flexDirection: 'column', gap: 16 }}>
      {/* Page header */}
      <Box
        style={{
          borderRadius: 16,
          border: '1px solid rgba(168,85,247,.3)',
          background: 'linear-gradient(135deg, #0d1526 0%, #111d36 100%)',
          padding: 16,
          display: 'flex',
          alignItems: 'flex-start',
          gap: 12,
        }}
      >
        <Box
          style={{
            width: 40, height: 40, borderRadius: 12, flexShrink: 0,
            background: 'rgba(168,85,247,.2)', border: '1px solid rgba(168,85,247,.45)',
            display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#a855f7',
          }}
        >
          <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={1.8} stroke="currentColor" width={20} height={20}>
            <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6A2.25 2.25 0 0 1 6 3.75h2.25A2.25 2.25 0 0 1 10.5 6v2.25a2.25 2.25 0 0 1-2.25 2.25H6a2.25 2.25 0 0 1-2.25-2.25V6ZM13.5 6A2.25 2.25 0 0 1 15.75 3.75H18A2.25 2.25 0 0 1 20.25 6v2.25A2.25 2.25 0 0 1 18 10.5h-2.25a2.25 2.25 0 0 1-2.25-2.25V6ZM3.75 15.75A2.25 2.25 0 0 1 6 13.5h2.25a2.25 2.25 0 0 1 2.25 2.25V18a2.25 2.25 0 0 1-2.25 2.25H6A2.25 2.25 0 0 1 3.75 18v-2.25ZM13.5 15.75a2.25 2.25 0 0 1 2.25-2.25H18a2.25 2.25 0 0 1 2.25 2.25V18A2.25 2.25 0 0 1 18 20.25h-2.25A2.25 2.25 0 0 1 13.5 18v-2.25Z" />
          </svg>
        </Box>
        <Box style={{ flex: 1, minWidth: 0 }}>
          <Typography variant="h6" style={{ lineHeight: 1.2 }}>SOW Volume &amp; Products</Typography>
          <Typography variant="caption" color="textSecondary">
            {'Contracted commitments, SLA ceilings, volume ramp and operational standards \u2014 all cross-wired to the PE Narrative.'}
          </Typography>
        </Box>
        <input id="sow-parse-input" type="file" accept=".pdf,.docx" style={{ display: 'none' }} onChange={handleParse} />
        <label htmlFor="sow-parse-input">
          <Button component="span" variant="outlined" size="small" disabled={busy}>Upload SOW PDF</Button>
        </label>
        {busy && <CircularProgress size={20} aria-label="Processing" />}
      </Box>
      {error && <Typography variant="body2" color="error">{error}</Typography>}

      {/* Audit Scope: Products / Modules Reviewed */}
      <Box
        style={{
          borderRadius: 16, border: '1px solid rgba(168,85,247,.3)',
          background: 'linear-gradient(135deg, #0d1526 0%, #111d36 100%)',
          padding: 16, display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 16, flexWrap: 'wrap',
        }}
      >
        <Box style={{ display: 'flex', alignItems: 'flex-start', gap: 12, minWidth: 0 }}>
          <Box
            style={{
              width: 40, height: 40, borderRadius: 12, flexShrink: 0,
              background: 'rgba(168,85,247,.2)', border: '1px solid rgba(168,85,247,.45)',
              display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#a855f7',
            }}
          >
            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={1.8} stroke="currentColor" width={20} height={20}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75 11.25 15 15 9.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" />
            </svg>
          </Box>
          <Box>
            <Typography variant="caption" style={{ textTransform: 'uppercase', letterSpacing: '.15em', color: '#6b7db3', fontWeight: 700 }}>Audit Scope</Typography>
            <Typography variant="subtitle1" style={{ fontWeight: 800, lineHeight: 1.2 }}>Products / Modules Reviewed</Typography>
            {reviewedProducts.length === 0 ? (
              <Typography variant="caption" color="textSecondary">
                {'No modules selected yet \u2014 set the scope so every panel and the final report state exactly what was reviewed.'}
              </Typography>
            ) : (
              <Box style={{ display: 'flex', flexWrap: 'wrap', gap: 6, marginTop: 6 }}>
                {reviewedProducts.map((p) => (
                  <span key={p} className="metric-badge metric-badge-purple">{productLabelMap[p] || p}</span>
                ))}
              </Box>
            )}
          </Box>
        </Box>
        <Button
          variant="outlined"
          onClick={openPicker}
          style={{
            borderColor: 'rgba(168,85,247,.45)', color: '#f1f5f9', textTransform: 'none',
            padding: '8px 18px', borderRadius: 12,
          }}
        >
          {reviewedProducts.length ? 'Edit Scope' : 'Select Products'}
        </Button>
      </Box>

      {/* Volume Targets */}
      <Box
        style={{
          borderRadius: 16, overflow: 'hidden',
          border: '1px solid rgba(59,130,246,.22)',
          background: 'linear-gradient(160deg, rgba(13,18,30,.98) 0%, rgba(7,11,20,.99) 100%)',
        }}
      >
        <Box style={{
          display: 'flex', alignItems: 'center', justifyContent: 'space-between',
          padding: '14px 18px', borderBottom: '1px solid rgba(255,255,255,.05)',
        }}
        >
          <Box>
            <Typography variant="subtitle1" style={{ fontWeight: 800, color: '#f1f5f9' }}>Volume Targets</Typography>
            <Typography variant="caption" style={{ color: 'rgba(100,116,139,.85)' }}>
              {'SOW commitments vs actuals \u2014 auto-wired to PE Findings & Executive Dashboard'}
            </Typography>
          </Box>
          <Button size="small" onClick={handleClearAll} style={{ color: '#64748b', textTransform: 'none' }}>Clear All</Button>
        </Box>

        <Box style={{
          display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(260px, 1fr))', gap: 16, padding: 18,
        }}
        >
          {SOW_FIELDS.map(({ key, label, sub, icon, placeholder }) => {
            const base = Number(baseValues[key]) || 0;
            const actual = Number(actualValues[key]) || 0;
            const ach = computeAchievement(base, actual, bands);
            const barPct = ach && ach !== 'awaiting' ? Math.min(ach.pct, 100) : 0;
            const barColor = ach && ach !== 'awaiting' ? ach.color : 'rgba(71,85,105,.45)';
            const borderColor = ach === 'awaiting'
              ? 'rgba(59,130,246,.28)'
              : ach ? `${ach.color}55` : 'rgba(59,130,246,.16)';
            return (
              <Box key={key} style={{ borderRadius: 12, padding: 16, background: 'rgba(15,23,42,.65)', border: `1px solid ${borderColor}`, transition: 'all .3s' }}>
                <Box style={{ display: 'flex', alignItems: 'flex-start', justifyContent: 'space-between', marginBottom: 16 }}>
                  <Box style={{ display: 'flex', alignItems: 'center', gap: 10 }}>
                    <span style={{ fontSize: 20, lineHeight: 1 }}>{icon}</span>
                    <Box>
                      <Typography variant="body2" style={{ fontWeight: 700, color: '#e2e8f0', lineHeight: 1.2 }}>{label}</Typography>
                      <Typography variant="caption" style={{ color: '#475569' }}>{sub}</Typography>
                    </Box>
                  </Box>
                  {ach && (
                    <span
                      style={{
                        fontSize: 8, fontWeight: 800, textTransform: 'uppercase', letterSpacing: '.08em',
                        padding: '4px 8px', borderRadius: 6,
                        color: ach === 'awaiting' ? '#60a5fa' : ach.color,
                        background: ach === 'awaiting' ? 'rgba(59,130,246,.1)' : `${ach.color}18`,
                        border: `1px solid ${ach === 'awaiting' ? 'rgba(59,130,246,.22)' : `${ach.color}40`}`,
                      }}
                    >
                      {ach === 'awaiting' ? 'AWAITING' : ach.label}
                    </span>
                  )}
                </Box>
                <Box style={{ display: 'flex', flexDirection: 'column', gap: 8, marginBottom: 14 }}>
                  <Box>
                    <Typography variant="caption" style={{ display: 'block', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', color: '#334155', marginBottom: 4 }}>SOW Target</Typography>
                    <TextField
                      size="small" type="number" fullWidth placeholder={placeholder}
                      value={baseValues[key] || ''}
                      onChange={(e) => setBaseValues((prev) => ({ ...prev, [key]: e.target.value }))}
                      InputProps={{ style: { textAlign: 'right', fontWeight: 700, color: '#f1f5f9', background: 'rgba(30,41,59,.7)' } }}
                      inputProps={{ style: { textAlign: 'right' }, min: 0 }}
                    />
                  </Box>
                  <Box>
                    <Typography variant="caption" style={{ display: 'block', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', color: '#334155', marginBottom: 4 }}>Actual</Typography>
                    <TextField
                      size="small" type="number" fullWidth placeholder="from upload"
                      value={actualValues[key] || ''}
                      onChange={(e) => setActualValues((prev) => ({ ...prev, [key]: e.target.value }))}
                      InputProps={{ style: { textAlign: 'right', fontWeight: 700, color: '#f1f5f9', background: 'rgba(30,41,59,.7)' } }}
                      inputProps={{ style: { textAlign: 'right' }, min: 0 }}
                    />
                  </Box>
                </Box>
                <Box style={{ height: 4, borderRadius: 999, overflow: 'hidden', background: 'rgba(30,41,59,.9)' }}>
                  <Box style={{ height: '100%', borderRadius: 999, width: `${barPct}%`, background: barColor, transition: 'all .5s' }} />
                </Box>
                <Box style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginTop: 8 }}>
                  <Typography variant="caption" style={{ color: '#334155' }}>Achievement</Typography>
                  <Typography variant="caption" style={{ fontWeight: 700, color: ach && ach !== 'awaiting' ? ach.color : '#475569' }}>
                    {ach && ach !== 'awaiting' ? `${ach.pct.toFixed(1)}%` : '\u2014'}
                  </Typography>
                </Box>
              </Box>
            );
          })}
        </Box>

        <Box style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '4px 18px 18px' }}>
          <Button
            variant="outlined" onClick={handleSaveAndCompare} disabled={busy}
            style={{
              borderColor: 'rgba(59,130,246,.45)', color: '#60a5fa', textTransform: 'none',
              fontWeight: 700, borderRadius: 12, padding: '10px 20px',
            }}
          >
            Save &amp; Compare vs Actuals
          </Button>
          {saveMsg && <Typography variant="caption" style={{ color: '#10b981', fontWeight: 600 }}>{saveMsg}</Typography>}
        </Box>
      </Box>

      {/* Contract Intelligence Grid */}
      {metrics.length > 0 && (
        <Box style={{ borderRadius: 16, border: '1px solid #1a2850', background: 'linear-gradient(160deg, #0d1526 0%, #111d36 100%)', padding: 18, display: 'flex', flexDirection: 'column', gap: 12 }}>
          <Box style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
            <Box>
              <Typography variant="subtitle1" style={{ fontWeight: 800 }}>Volume Compliance vs SOW Contract</Typography>
              <Typography variant="caption" color="textSecondary">{'Contracted vs actual, buffer to threshold, status and PE finding \u2014 per metric'}</Typography>
            </Box>
            {overallStatus && (() => {
              const cfg = OVERALL_BADGE[overallStatus] || { bg: 'rgba(100,116,139,.15)', color: '#64748b', border: 'rgba(100,116,139,.3)', icon: '\u2014' };
              return (
                <span style={{
                  fontSize: 11, fontWeight: 700, padding: '4px 12px', borderRadius: 999,
                  background: cfg.bg, color: cfg.color, border: `1px solid ${cfg.border}`,
                }}
                >
                  {cfg.icon} {cfg.label || overallStatus}
                </span>
              );
            })()}
          </Box>

          {summary && (
            <Box style={{ borderRadius: 8, border: '1px solid #1a2850', background: 'rgba(2,6,14,.4)', padding: '10px 14px' }}>
              <Typography variant="body2" style={{ color: '#f0f4ff', fontWeight: 600 }}>{summary}</Typography>
            </Box>
          )}

          {/* Zone legend */}
          <Box style={{ display: 'flex', flexWrap: 'wrap', alignItems: 'center', gap: 12, fontSize: 10, fontWeight: 600, color: '#94a3b8' }}>
            <span style={{ textTransform: 'uppercase', letterSpacing: '.08em', color: '#64748b' }}>Zones:</span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}><span style={{ width: 10, height: 10, borderRadius: 3, background: 'rgba(245,158,11,.7)', display: 'inline-block' }} />Below floor &lt;{bands.under}%</span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}><span style={{ width: 10, height: 10, borderRadius: 3, background: 'rgba(16,185,129,.7)', display: 'inline-block' }} />Standard window {bands.under}{'\u2013'}{bands.over}%</span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}><span style={{ width: 10, height: 10, borderRadius: 3, background: 'rgba(244,63,94,.5)', display: 'inline-block' }} />Overconsumption {bands.over}{'\u2013'}{bands.crit}%</span>
            <span style={{ display: 'flex', alignItems: 'center', gap: 4 }}><span style={{ width: 10, height: 10, borderRadius: 3, background: '#f43f5e', display: 'inline-block' }} />Critical &gt;{bands.crit}%</span>
          </Box>

          {overconsumption && overconsumption.count > 0 && (
            <Box
              style={{
                borderRadius: 12, padding: 12,
                border: `1px solid ${overconsumption.severity === 'CRITICAL_OVER' ? 'rgba(244,63,94,.4)' : 'rgba(245,158,11,.4)'}`,
                background: overconsumption.severity === 'CRITICAL_OVER' ? 'rgba(244,63,94,.08)' : 'rgba(245,158,11,.08)',
              }}
            >
              <Typography variant="body2" style={{ fontWeight: 700, color: overconsumption.severity === 'CRITICAL_OVER' ? '#f43f5e' : '#f59e0b' }}>
                {overconsumption.count} metric(s) over contracted scope {'\u2014'} worst: {overconsumption.worst_label} at {overconsumption.worst_pct.toFixed(1)}% of SOW (+{overconsumption.worst_over_by.toFixed(0)} over)
              </Typography>
            </Box>
          )}

          {/* Per-metric bars */}
          <Box style={{ display: 'flex', flexDirection: 'column', gap: 10 }}>
            {metrics.map((m) => {
              const AXIS = Math.max(150, Math.ceil((bands.crit * 1.25) / 10) * 10);
              const isCrit = m.pct > bands.crit;
              const isOver = m.pct > bands.over;
              const isLow = m.pct < bands.under;
              const color = isCrit ? '#dc2626' : isOver ? '#f43f5e' : m.pct >= bands.under ? '#22d3ee' : '#f59e0b';
              const statusBg = isOver ? 'rgba(244,63,94,.2)' : m.pct >= bands.under ? 'rgba(34,211,238,.2)' : 'rgba(245,158,11,.2)';
              const statusColor = isOver ? '#f43f5e' : m.pct >= bands.under ? '#22d3ee' : '#f59e0b';
              const over = m.over_by && m.over_by > 0 ? m.over_by : (m.sow > 0 && m.actual > m.sow ? m.actual - m.sow : 0);
              const bufferPct = +(bands.over - m.pct).toFixed(1);
              let bufferLabel: string; let bufferColor: string;
              if (isOver) {
                bufferLabel = `${bufferPct.toFixed(1)}% (over ceiling by ${Math.abs(bufferPct).toFixed(1)}pp)`;
                bufferColor = '#f43f5e';
              } else if (isLow) {
                const shortfall = +(bands.under - m.pct).toFixed(1);
                bufferLabel = `+${(bands.over - bands.under).toFixed(1)}% to ceiling \u2014 ${shortfall.toFixed(1)}pp below floor`;
                bufferColor = '#f59e0b';
              } else {
                bufferLabel = `+${bufferPct.toFixed(1)}% to ceiling`;
                bufferColor = '#10b981';
              }
              const findingTag = isCrit || isOver
                ? { text: '\u26A0 PE Finding \u00b7 CRITICAL \u00b7 SOW_VOLUME_OVER', bg: 'rgba(244,63,94,.15)', border: 'rgba(244,63,94,.4)', color: '#f43f5e' }
                : isLow
                  ? { text: '\u26A0 PE Finding \u00b7 WARNING \u00b7 SOW_VOLUME_UNDER', bg: 'rgba(245,158,11,.15)', border: 'rgba(245,158,11,.4)', color: '#f59e0b' }
                  : { text: '\u2713 No PE finding \u00b7 within standard window', bg: 'rgba(16,185,129,.15)', border: 'rgba(16,185,129,.35)', color: '#10b981' };
              const disclaimer = isCrit
                ? `\u{1F6D1} CRITICAL OVERCONSUMPTION \u2014 ${fmt(m.actual)} against a contracted ${fmt(m.sow)}, exceeding contract by ${fmt(over)}. Must be commercially resolved before PE sign-off (NO GO until amended or brought back inside contract).`
                : isOver ? `\u{1F534} Overconsumption \u2014 exceeding contracted scope by ${fmt(over)} (${m.pct.toFixed(1)}% of SOW). Formal review & written customer acknowledgment required before sign-off.`
                  : isLow ? `\u26A0 Below the ${bands.under}%\u2013${bands.over}% standard process window \u2014 findings below are validated at this tested volume only, not at full contracted scale.`
                    : `Within the ${bands.under}%\u2013${bands.over}% standard process window \u2014 no formal review required.`;
              const pctClamped = Math.min(m.pct, AXIS);
              const barPct = (pctClamped / AXIS) * 100;
              return (
                <Box key={m.key} style={{ borderRadius: 12, border: '1px solid #1a2850', background: 'rgba(2,6,14,.2)', padding: 12, display: 'flex', flexDirection: 'column', gap: 6 }}>
                  <Box style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 6 }}>
                    <Typography variant="body2" style={{ fontWeight: 700 }}>{m.label}</Typography>
                    <Box style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
                      <span style={{ color: '#64748b', fontFamily: 'monospace', fontSize: 10 }}>{fmt(m.actual)} / {fmt(m.sow)}</span>
                      <span style={{ fontWeight: 700, color }}>{m.pct.toFixed(1)}%</span>
                      <span style={{ fontSize: 10, fontWeight: 700, padding: '2px 8px', borderRadius: 999, textTransform: 'uppercase', background: statusBg, color: statusColor }}>
                        {STATUS_BADGE[m.status] ? m.status.replace('_', ' ') : m.status}
                      </span>
                    </Box>
                  </Box>
                  <Box style={{ position: 'relative', height: 16, borderRadius: 8, overflow: 'hidden', background: '#02060e', border: '1px solid #1a2850' }}>
                    <Box style={{ position: 'absolute', inset: 0, left: 0, width: `${(bands.under / AXIS) * 100}%`, background: 'rgba(245,158,11,.2)' }} />
                    <Box style={{ position: 'absolute', top: 0, bottom: 0, left: `${(bands.under / AXIS) * 100}%`, width: `${((bands.over - bands.under) / AXIS) * 100}%`, background: 'rgba(16,185,129,.15)' }} />
                    <Box style={{ position: 'absolute', top: 0, bottom: 0, left: `${(bands.over / AXIS) * 100}%`, width: `${((bands.crit - bands.over) / AXIS) * 100}%`, background: 'rgba(244,63,94,.15)' }} />
                    <Box style={{ position: 'absolute', top: 0, bottom: 0, left: `${(bands.crit / AXIS) * 100}%`, right: 0, background: 'rgba(244,63,94,.3)' }} />
                    <Box style={{ position: 'absolute', top: 0, bottom: 0, width: 1, left: `${(100 / AXIS) * 100}%`, background: 'rgba(255,255,255,.3)' }} />
                    <Box style={{ position: 'absolute', top: 0, bottom: 0, left: 0, borderRadius: 8, width: `${barPct}%`, background: color, opacity: 0.85, transition: 'all .7s' }} />
                  </Box>
                  <Box style={{ display: 'flex', justifyContent: 'space-between', fontSize: 9, color: '#64748b', fontFamily: 'monospace' }}>
                    <span>0</span><span style={{ color: '#f59e0b' }}>{bands.under}%</span><span>100%</span><span style={{ color: '#f43f5e' }}>{bands.over}%</span><span style={{ color: '#f43f5e', fontWeight: 700 }}>{bands.crit}%</span><span>{AXIS}%+</span>
                  </Box>
                  <Box style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', flexWrap: 'wrap', gap: 8 }}>
                    <Typography variant="caption" style={{ fontWeight: 600, color: bufferColor }}>Buffer: {bufferLabel}</Typography>
                    <span style={{ fontSize: 9, fontWeight: 700, padding: '2px 8px', borderRadius: 999, textTransform: 'uppercase', background: findingTag.bg, border: `1px solid ${findingTag.border}`, color: findingTag.color }}>
                      {findingTag.text}
                    </span>
                  </Box>
                  <Typography variant="caption" style={{ color: isCrit || isOver ? '#f43f5e' : isLow ? '#f59e0b' : '#64748b', fontWeight: isCrit || isOver || isLow ? 600 : 400 }}>
                    {disclaimer}
                  </Typography>
                </Box>
              );
            })}
          </Box>
        </Box>
      )}

      {/* Products / Modules Reviewed picker dialog */}
      <Dialog open={pickerOpen} onClose={() => setPickerOpen(false)} maxWidth="md" fullWidth PaperProps={{ style: { background: '#0b0f1c', border: '1px solid #1a2850', borderRadius: 16, height: '80vh' } }}>
        <Box style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
          <Box style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', padding: '16px 20px', borderBottom: '1px solid #1a2850' }}>
            <Box>
              <Typography variant="subtitle1" style={{ fontWeight: 800 }}>Products / Modules Reviewed</Typography>
              <Typography variant="caption" color="textSecondary">{'Defines audit scope \u2014 flows into SOW, Executive Dashboard, PE Findings and the final report.'}</Typography>
            </Box>
            <IconButton size="small" onClick={() => setPickerOpen(false)} aria-label="Close"><span style={{ fontSize: 20, color: '#94a3b8' }}>{'\u00D7'}</span></IconButton>
          </Box>

          <Box style={{ display: 'flex', alignItems: 'center', gap: 12, padding: '12px 20px', borderBottom: '1px solid #1a2850', flexWrap: 'wrap' }}>
            <TextField
              size="small" placeholder={'Search products, modules or aliases\u2026'} value={search}
              onChange={(e) => setSearch(e.target.value)}
              style={{ minWidth: 260, flex: 1 }}
            />
            <Button size="small" variant="outlined" onClick={selectAllVisible}>Select visible</Button>
            <Button size="small" variant="outlined" onClick={clearAllDraft}>Clear all</Button>
            <span className="metric-badge metric-badge-teal">{draftSelected.size} selected</span>
          </Box>

          <Box style={{ flex: 1, minHeight: 0, display: 'flex' }}>
            <Box style={{ width: 220, flexShrink: 0, borderRight: '1px solid #1a2850', overflowY: 'auto', padding: 10 }}>
              <Button
                fullWidth
                onClick={() => setActiveFamily('ALL')}
                style={{
                  justifyContent: 'flex-start', textTransform: 'none', marginBottom: 4,
                  background: activeFamily === 'ALL' ? 'rgba(59,130,246,.15)' : 'transparent',
                  color: activeFamily === 'ALL' ? '#60a5fa' : '#94a3b8',
                }}
              >
                All products
              </Button>
              {taxonomy.map((g) => (
                <Button
                  key={g.key}
                  fullWidth
                  onClick={() => setActiveFamily(g.key)}
                  style={{
                    justifyContent: 'flex-start', textTransform: 'none', marginBottom: 4, fontSize: 12,
                    background: activeFamily === g.key ? 'rgba(59,130,246,.15)' : 'transparent',
                    color: activeFamily === g.key ? '#60a5fa' : '#94a3b8',
                  }}
                >
                  {g.label}
                </Button>
              ))}
            </Box>
            <Box style={{ flex: 1, minWidth: 0, overflowY: 'auto', padding: 16 }}>
              {visibleItems.length === 0 && (
                <Typography variant="body2" color="textSecondary">No products match your search.</Typography>
              )}
              {Object.entries(
                visibleItems.reduce<Record<string, TaxonomyItem[]>>((acc, { group, item }) => {
                  (acc[group] ||= []).push(item);
                  return acc;
                }, {}),
              ).map(([group, items]) => (
                <Box key={group} style={{ marginBottom: 16 }}>
                  <Typography variant="caption" style={{ textTransform: 'uppercase', letterSpacing: '.08em', color: '#64748b', fontWeight: 700 }}>{group}</Typography>
                  <Box style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(240px, 1fr))' }}>
                    {items.map((it) => (
                      <Box key={it.value} style={{ display: 'flex', alignItems: 'center', gap: 4 }}>
                        <Checkbox
                          size="small" checked={draftSelected.has(it.value)}
                          onChange={() => toggleDraft(it.value)}
                          id={`pr-${it.value}`}
                        />
                        <label htmlFor={`pr-${it.value}`} style={{ cursor: 'pointer', fontSize: 13 }}>{it.label}</label>
                      </Box>
                    ))}
                  </Box>
                </Box>
              ))}
            </Box>
          </Box>

          <Box style={{ display: 'flex', alignItems: 'center', justifyContent: 'flex-end', gap: 8, padding: '14px 20px', borderTop: '1px solid #1a2850' }}>
            <Button onClick={() => setPickerOpen(false)} style={{ textTransform: 'none', color: '#94a3b8' }}>Cancel</Button>
            <Button variant="contained" color="primary" onClick={handleSaveScope} style={{ textTransform: 'none' }}>Save scope</Button>
          </Box>
        </Box>
      </Dialog>
    </Box>
  );
}
