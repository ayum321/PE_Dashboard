import React, { ChangeEvent, useEffect, useMemo, useState } from 'react';
import {
  Box,
  Button,
  Checkbox,
  CircularProgress,
  Dialog,
  MenuItem,
  Select,
  TextField,
  Typography,
} from '@material-ui/core';
import {
  compareSow,
  generateFindings,
  getPeNarrative,
  getFinalJudgment,
  getRedFlags,
  getReviewedProducts,
  getSowState,
  getSowProductTaxonomy,
  parseSow,
  saveReviewedProducts,
  saveSowBaseline,
} from '../../api/dashboardApi';
import { useAppData } from '../../context/AppDataContext';
import { buildAnalysisPayload, buildFinalJudgmentPayload, buildPeNarrativePayload } from '../../utils/buildAnalysisPayload';

interface SowMetric {
  key: string;
  label: string;
  sow: number;
  actual: number;
  pct: number;
  status: string;
  over_by?: number;
  over_by_pct?: number;
  capacity_buffer?: number;
  capacity_buffer_pct?: number;
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
const MILLION_SCALE = 1_000_000;
const MILLION_FIELDS = new Set(['daily_dfu', 'daily_sku']);
/**
 * Route navigation unmounts this panel.  Keep an explicit local draft of the
 * whole form so a tab switch cannot turn an in-progress SOW entry into a blank
 * form.  The draft is deliberately separate from the server-side comparison:
 * PE Findings only reads values after Save & Compare has committed them.
 */
const SOW_FORM_DRAFT_KEY = 'pe-dashboard:sow-form-draft-v2';
const LEGACY_SOW_ACTUAL_DRAFT_KEY = 'pe-dashboard:sow-actual-draft';
type VolumeUnit = 'number' | 'millions';

interface SowFormDraft {
  baseValues: Record<string, string>;
  actualValues: Record<string, string>;
  volumeUnits: Record<string, VolumeUnit>;
}

const EMPTY_SOW_DRAFT: SowFormDraft = { baseValues: {}, actualValues: {}, volumeUnits: {} };

function asStringRecord(value: unknown): Record<string, string> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .filter(([, entry]) => typeof entry === 'string' || typeof entry === 'number')
      .map(([key, entry]) => [key, String(entry)]),
  );
}

function asUnitRecord(value: unknown): Record<string, VolumeUnit> {
  if (!value || typeof value !== 'object' || Array.isArray(value)) return {};
  return Object.fromEntries(
    Object.entries(value as Record<string, unknown>)
      .filter(([, entry]) => entry === 'number' || entry === 'millions') as Array<[string, VolumeUnit]>,
  );
}

function readSowDraft(): SowFormDraft {
  try {
    const raw = window.sessionStorage.getItem(SOW_FORM_DRAFT_KEY);
    const parsed = raw ? JSON.parse(raw) : null;
    if (parsed && typeof parsed === 'object' && !Array.isArray(parsed)) {
      return {
        baseValues: asStringRecord((parsed as Record<string, unknown>).baseValues),
        actualValues: asStringRecord((parsed as Record<string, unknown>).actualValues),
        volumeUnits: asUnitRecord((parsed as Record<string, unknown>).volumeUnits),
      };
    }

    // Retain the one-version migration path for anyone who entered actuals
    // before the full-form draft existed.
    return { ...EMPTY_SOW_DRAFT, actualValues: asStringRecord(JSON.parse(window.sessionStorage.getItem(LEGACY_SOW_ACTUAL_DRAFT_KEY) || '{}')) };
  } catch {
    return EMPTY_SOW_DRAFT;
  }
}

function writeSowDraft(draft: SowFormDraft): void {
  try {
    const hasValues = Object.keys(draft.baseValues).length > 0 || Object.keys(draft.actualValues).length > 0;
    if (hasValues) window.sessionStorage.setItem(SOW_FORM_DRAFT_KEY, JSON.stringify(draft));
    else window.sessionStorage.removeItem(SOW_FORM_DRAFT_KEY);
    window.sessionStorage.removeItem(LEGACY_SOW_ACTUAL_DRAFT_KEY);
  } catch {
    // Session storage is optional in embedded Portal contexts.
  }
}

function toDisplayValue(value: unknown, unit: VolumeUnit): string {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric === 0) return '';
  return unit === 'millions' ? String(numeric / MILLION_SCALE) : String(numeric);
}

function toCanonicalValue(value: string, unit: VolumeUnit): number {
  const numeric = Number(value);
  if (!Number.isFinite(numeric) || numeric <= 0) return 0;
  return unit === 'millions' ? numeric * MILLION_SCALE : numeric;
}

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
  const {
    data,
    setSowBaseline,
    setSowCompare,
    setReviewedProducts,
    setFindings,
    setRedFlags,
    setPeNarrative,
    setFinalJudgment,
  } = useAppData();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saveMsg, setSaveMsg] = useState<string | null>(null);
  const [initialDraft] = useState<SowFormDraft>(readSowDraft);

  const [baseValues, setBaseValues] = useState<Record<string, string>>(initialDraft.baseValues);
  const [actualValues, setActualValues] = useState<Record<string, string>>(initialDraft.actualValues);
  const [volumeUnits, setVolumeUnits] = useState<Record<string, VolumeUnit>>({
    daily_dfu: 'number',
    daily_sku: 'number',
    ...initialDraft.volumeUnits,
  });

  const [taxonomy, setTaxonomy] = useState<TaxonomyGroup[]>([]);
  const [pickerOpen, setPickerOpen] = useState(false);
  const [draftSelected, setDraftSelected] = useState<Set<string>>(new Set());
  const [search, setSearch] = useState('');
  const [activeFamily, setActiveFamily] = useState<string>('ALL');

  useEffect(() => {
    getSowState()
      .then((state) => {
        const baseline = (state.baseline && typeof state.baseline === 'object'
          ? state.baseline : state) as Record<string, unknown>;
        setSowBaseline(baseline);
        const serverBase: Record<string, string> = {};
        SOW_FIELDS.forEach(({ key }) => {
          if (baseline[key] != null) serverBase[key] = toDisplayValue(baseline[key], MILLION_FIELDS.has(key) ? volumeUnits[key] || 'number' : 'number');
        });
        // The server contains last saved evidence.  A newer local form draft
        // is intentionally overlaid so an in-progress edit survives a route
        // remount rather than being replaced by the last saved value.
        const localDraft = readSowDraft();
        setVolumeUnits((prev) => ({ ...prev, ...localDraft.volumeUnits }));
        setBaseValues({ ...serverBase, ...localDraft.baseValues });
        const savedActuals = state.actuals && typeof state.actuals === 'object'
          ? state.actuals as Record<string, unknown> : {};
        const serverActuals: Record<string, string> = {};
        if (Object.keys(savedActuals).length > 0) {
          SOW_FIELDS.forEach(({ key }) => {
            if (savedActuals[key] != null) serverActuals[key] = toDisplayValue(savedActuals[key], MILLION_FIELDS.has(key) ? volumeUnits[key] || 'number' : 'number');
          });
        }
        setActualValues({ ...serverActuals, ...localDraft.actualValues });
        if (state.compare && typeof state.compare === 'object') setSowCompare(state.compare as Record<string, unknown>);
      })
      .catch(() => undefined);
    getSowProductTaxonomy()
      .then((res) => setTaxonomy((res.groups as TaxonomyGroup[]) || []))
      .catch(() => undefined);
    getReviewedProducts()
      .then((res) => setReviewedProducts((res.products as string[]) || []))
      .catch(() => undefined);
    // Load persisted SOW values once; unit changes are handled locally so they
    // never overwrite values the user is currently editing.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  // A tab change must not erase values still being entered.  This is draft
  // storage only: Findings continues to read the saved server comparison, so
  // an unfinished input can never masquerade as a measured actual.
  useEffect(() => {
    writeSowDraft({ baseValues, actualValues, volumeUnits });
  }, [baseValues, actualValues, volumeUnits]);

  const updateBaseValue = (key: string, value: string) => {
    const next = { ...baseValues, [key]: value };
    setBaseValues(next);
    // Persist synchronously as well as through the effect so an immediate tab
    // click cannot race the post-render effect.
    writeSowDraft({ baseValues: next, actualValues, volumeUnits });
  };

  const updateActualValue = (key: string, value: string) => {
    const next = { ...actualValues, [key]: value };
    setActualValues(next);
    writeSowDraft({ baseValues, actualValues: next, volumeUnits });
  };

  const updateVolumeUnit = (key: string, nextUnit: VolumeUnit) => {
    const previousUnit = volumeUnits[key] || 'number';
    const nextUnits = { ...volumeUnits, [key]: nextUnit };
    const nextBase = { ...baseValues, [key]: toDisplayValue(toCanonicalValue(baseValues[key] || '', previousUnit), nextUnit) };
    const nextActuals = { ...actualValues, [key]: toDisplayValue(toCanonicalValue(actualValues[key] || '', previousUnit), nextUnit) };
    setVolumeUnits(nextUnits);
    setBaseValues(nextBase);
    setActualValues(nextActuals);
    writeSowDraft({ baseValues: nextBase, actualValues: nextActuals, volumeUnits: nextUnits });
  };

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
          if (result[key] != null) next[key] = toDisplayValue(result[key], MILLION_FIELDS.has(key) ? volumeUnits[key] || 'number' : 'number');
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
      const unit = MILLION_FIELDS.has(key) ? volumeUnits[key] || 'number' : 'number';
      const b = toCanonicalValue(baseValues[key] || '', unit);
      const a = toCanonicalValue(actualValues[key] || '', unit);
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
      // The server is now authoritative.  Keeping an old draft would allow it
      // to mask the newly saved comparison on the next route mount.
      try {
        window.sessionStorage.removeItem(SOW_FORM_DRAFT_KEY);
        window.sessionStorage.removeItem(LEGACY_SOW_ACTUAL_DRAFT_KEY);
      } catch { /* optional browser storage */ }

      // SOW is an evidence pillar for PE Findings, not a standalone screen.
      // Use the freshly returned comparison in every downstream request rather
      // than waiting for React state to update, so the saved values flow through
      // immediately and the user never has to click Generate Findings again.
      const analysisData = { ...data, sowBaseline: savedBaseline, sowCompare: result };
      const analysisPayload = buildAnalysisPayload(analysisData);
      const findings = await generateFindings(analysisPayload);
      setFindings(findings);

      let redFlags = data.redFlags;
      try {
        redFlags = await getRedFlags(analysisPayload);
        setRedFlags(redFlags);
      } catch {
        // Findings remain usable if optional red-flag enrichment is unavailable.
      }
      try {
        setPeNarrative(await getPeNarrative(buildPeNarrativePayload(analysisData, { findings, redFlags })));
      } catch {
        // The deterministic findings response remains available on its own.
      }
      try {
        setFinalJudgment(await getFinalJudgment(buildFinalJudgmentPayload(analysisData, { findings, redFlags })));
      } catch {
        // Do not turn a successful SOW save into a failure if judgment is unavailable.
      }
      setSaveMsg('\u2705 Saved, compared, and PE Findings refreshed');
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
    try {
      window.sessionStorage.removeItem(SOW_FORM_DRAFT_KEY);
      window.sessionStorage.removeItem(LEGACY_SOW_ACTUAL_DRAFT_KEY);
    } catch { /* optional browser storage */ }
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
        className="scope-card"
        style={{
          padding: 16, display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: 16, flexWrap: 'wrap',
        }}
      >
        <Box style={{ display: 'flex', alignItems: 'flex-start', gap: 12, minWidth: 0 }}>
          <Box
            className="scope-card__icon"
            style={{
              width: 40, height: 40, flexShrink: 0,
              display: 'flex', alignItems: 'center', justifyContent: 'center', color: '#a855f7',
            }}
          >
            <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={1.8} stroke="currentColor" width={20} height={20}>
              <path strokeLinecap="round" strokeLinejoin="round" d="M9 12.75 11.25 15 15 9.75M21 12a9 9 0 1 1-18 0 9 9 0 0 1 18 0Z" />
            </svg>
          </Box>
          <Box>
            <Typography className="scope-card__eyebrow" variant="caption">Audit Scope</Typography>
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
          className="scope-card__button"
          variant="outlined"
          onClick={openPicker}
          style={{
            textTransform: 'none', padding: '8px 18px',
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
            const unit = MILLION_FIELDS.has(key) ? volumeUnits[key] || 'number' : 'number';
            const base = toCanonicalValue(baseValues[key] || '', unit);
            const actual = toCanonicalValue(actualValues[key] || '', unit);
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
                    <Box style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', marginBottom: 4 }}>
                      <Typography variant="caption" style={{ fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', color: '#334155' }}>SOW Target</Typography>
                      {MILLION_FIELDS.has(key) && (
                        <Select
                          value={unit}
                          onChange={(e) => {
                            updateVolumeUnit(key, e.target.value as VolumeUnit);
                          }}
                          variant="outlined"
                          inputProps={{ 'aria-label': `${label} input unit` }}
                          style={{ minWidth: 112, fontSize: 11, color: '#94a3b8' }}
                        >
                          <MenuItem value="number">Number</MenuItem>
                          <MenuItem value="millions">Millions</MenuItem>
                        </Select>
                      )}
                    </Box>
                    <TextField
                      size="small" type="number" fullWidth placeholder={placeholder}
                      value={baseValues[key] || ''}
                      onChange={(e) => updateBaseValue(key, e.target.value)}
                      InputProps={{ style: { textAlign: 'right', fontWeight: 700, color: '#f1f5f9', background: 'rgba(30,41,59,.7)' } }}
                      inputProps={{ 'aria-label': label, style: { textAlign: 'right' }, min: 0, step: unit === 'millions' ? 0.01 : 1 }}
                    />
                  </Box>
                  <Box>
                    <Typography variant="caption" style={{ display: 'block', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.06em', color: '#334155', marginBottom: 4 }}>Actual {MILLION_FIELDS.has(key) && unit === 'millions' ? '(millions)' : ''}</Typography>
                    <TextField
                      size="small" type="number" fullWidth placeholder="from upload"
                      value={actualValues[key] || ''}
                      onChange={(e) => updateActualValue(key, e.target.value)}
                      InputProps={{ style: { textAlign: 'right', fontWeight: 700, color: '#f1f5f9', background: 'rgba(30,41,59,.7)' } }}
                      inputProps={{ 'aria-label': `${label} actual`, style: { textAlign: 'right' }, min: 0, step: unit === 'millions' ? 0.01 : 1 }}
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
                      <span style={{ color: '#f1f5f9', fontFamily: 'monospace', fontSize: 10 }}>Actual {fmt(m.actual)} / SOW {fmt(m.sow)}</span>
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
                    <Typography variant="caption" style={{ fontWeight: 600, color: bufferColor }}>
                    Capacity buffer: {fmt(m.capacity_buffer ?? (m.sow - m.actual))} ({(m.capacity_buffer_pct ?? (100 - m.pct)).toFixed(1)}%) · {bufferLabel}
                  </Typography>
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
      <Dialog
        open={pickerOpen}
        onClose={() => setPickerOpen(false)}
        maxWidth={false}
        fullWidth
        PaperProps={{ className: 'scope-picker-dialog' }}
        BackdropProps={{ style: { backgroundColor: 'rgba(2, 5, 13, .82)', backdropFilter: 'blur(8px)' } }}
      >
        <Box style={{ display: 'flex', flexDirection: 'column', height: '100%' }}>
          <Box className="scope-picker-header">
            <Box style={{ display: 'flex', alignItems: 'center', gap: 14, minWidth: 0 }}>
              <Box className="scope-picker-header__icon">
                <svg xmlns="http://www.w3.org/2000/svg" fill="none" viewBox="0 0 24 24" strokeWidth={1.8} stroke="currentColor" width={22} height={22}>
                  <path strokeLinecap="round" strokeLinejoin="round" d="M3.75 6A2.25 2.25 0 0 1 6 3.75h2.25A2.25 2.25 0 0 1 10.5 6v2.25a2.25 2.25 0 0 1-2.25 2.25H6a2.25 2.25 0 0 1-2.25-2.25V6ZM13.5 6A2.25 2.25 0 0 1 15.75 3.75H18A2.25 2.25 0 0 1 20.25 6v2.25A2.25 2.25 0 0 1 18 10.5h-2.25a2.25 2.25 0 0 1-2.25 2.25H15.75A2.25 2.25 0 0 1 13.5 8.25V6ZM3.75 15.75A2.25 2.25 0 0 1 6 13.5h2.25a2.25 2.25 0 0 1 2.25 2.25V18a2.25 2.25 0 0 1-2.25 2.25H6A2.25 2.25 0 0 1 3.75 18v-2.25ZM13.5 15.75a2.25 2.25 0 0 1 2.25-2.25H18a2.25 2.25 0 0 1 2.25 2.25V18A2.25 2.25 0 0 1 18 20.25h-2.25a2.25 2.25 0 0 1-2.25-2.25v-2.25Z" />
                </svg>
              </Box>
              <Box>
              <Typography variant="subtitle1" style={{ fontWeight: 800 }}>Products / Modules Reviewed</Typography>
              <Typography variant="caption" color="textSecondary">{'Defines audit scope \u2014 flows into SOW, Executive Dashboard, PE Findings and the final report.'}</Typography>
              </Box>
            </Box>
            <Button className="scope-picker-close" size="small" onClick={() => setPickerOpen(false)}>Close · Esc</Button>
          </Box>

          <Box className="scope-picker-toolbar">
            <TextField
              className="scope-picker-search"
              size="small" variant="outlined" placeholder={'Search products, modules or aliases\u2026'} value={search}
              onChange={(e) => setSearch(e.target.value)}
              style={{ minWidth: 260, flex: 1 }}
            />
            <Button className="scope-picker-action" size="small" variant="outlined" onClick={selectAllVisible}>Select visible</Button>
            <Button className="scope-picker-action" size="small" variant="outlined" onClick={clearAllDraft}>Clear all</Button>
            <span className="scope-picker-count">{draftSelected.size} selected</span>
          </Box>

          <Box className="scope-picker-body">
            <Box className="scope-picker-families">
              <Typography className="scope-picker-label" variant="caption">Families</Typography>
              <Button
                className={`scope-picker-family${activeFamily === 'ALL' ? ' is-active' : ''}`}
                fullWidth
                onClick={() => setActiveFamily('ALL')}
              >
                <span>All products</span><span>{draftSelected.size}/{taxonomy.reduce((sum, group) => sum + group.items.length, 0)}</span>
              </Button>
              {taxonomy.map((g) => (
                <Button
                  key={g.key}
                  className={`scope-picker-family${activeFamily === g.key ? ' is-active' : ''}`}
                  fullWidth
                  onClick={() => setActiveFamily(g.key)}
                >
                  <span>{g.label}</span><span>{g.items.filter((item) => draftSelected.has(item.value)).length}/{g.items.length}</span>
                </Button>
              ))}
            </Box>
            <Box className="scope-picker-products">
              {visibleItems.length === 0 && (
                <Typography variant="body2" color="textSecondary">No products match your search.</Typography>
              )}
              {Object.entries(
                visibleItems.reduce<Record<string, TaxonomyItem[]>>((acc, { group, item }) => {
                  (acc[group] ||= []).push(item);
                  return acc;
                }, {}),
              ).map(([group, items]) => (
                <Box key={group} className="scope-picker-group">
                  <Box className="scope-picker-group__heading"><Typography variant="caption">{group}</Typography><span>{items.length}</span></Box>
                  <Box className="scope-picker-grid">
                    {items.map((it) => (
                      <label key={it.value} htmlFor={`pr-${it.value}`} className={`scope-picker-item${draftSelected.has(it.value) ? ' is-selected' : ''}`}>
                        <Checkbox
                          size="small" checked={draftSelected.has(it.value)}
                          onChange={() => toggleDraft(it.value)}
                          id={`pr-${it.value}`}
                        />
                        <span>{it.label}</span>
                      </label>
                    ))}
                  </Box>
                </Box>
              ))}
            </Box>
          </Box>

          <Box className="scope-picker-footer">
            <Typography className="scope-picker-footer__hint" variant="caption">Changes are saved only when you select Save scope.</Typography>
            <Box style={{ display: 'flex', gap: 8 }}>
              <Button className="scope-picker-cancel" onClick={() => setPickerOpen(false)}>Cancel</Button>
              <Button className="scope-picker-save" variant="contained" color="primary" onClick={handleSaveScope}>Save scope</Button>
            </Box>
          </Box>
        </Box>
      </Dialog>
    </Box>
  );
}
