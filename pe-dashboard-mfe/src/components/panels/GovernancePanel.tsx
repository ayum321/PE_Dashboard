import React, { useMemo, useState } from 'react';
import { Box, Button, Paper, TextField, Typography, makeStyles } from '@material-ui/core';
import { useAppData } from '../../context/AppDataContext';
import { ApprovalsState, IssueRecord } from '../../context/AppDataContext';
import { buildAnalysisPayload } from '../../utils/buildAnalysisPayload';
import { exportReport } from '../../api/dashboardApi';

const useStyles = makeStyles((theme) => ({
  panel: { padding: theme.spacing(3) },
  card: { borderRadius: 16, border: '1px solid #213060', background: 'linear-gradient(135deg, #111d36, #0d1526)', padding: 20 },
}));

const SEV_COLOR: Record<string, string> = {
  Critical: '#f43f5e', High: '#f97316', Medium: '#f59e0b', Low: '#10d96e', Informational: '#3b82f6',
};
const STAT_COLOR: Record<string, string> = {
  Open: '#f43f5e', 'In Progress': '#f59e0b', Waived: '#f59e0b', Resolved: '#10d96e', Deferred: '#6b7db3',
};

const CHECKLIST_ITEMS: { key: keyof ApprovalsState['checklist']; label: string }[] = [
  { key: 'batch', label: 'Batch SLA validated (daily/weekly/monthly)' },
  { key: 'issues', label: 'Issues & waivers acknowledged' },
  { key: 'ui', label: 'UI performance benchmarking approved' },
  { key: 'res', label: 'Resource utilization within thresholds' },
  { key: 'perf', label: 'Batch performance-test report reviewed' },
  { key: 'sow', label: 'SOW service IDs & scenarios confirmed' },
  { key: 'data', label: 'Data volume (DFU/SKU) vs SOW verified' },
  { key: 'ctrlm', label: 'Ctrl-M 30-day execution history reviewed' },
  { key: 'res15', label: 'Resource utilization (last 15 days) reviewed' },
];

const ISSUE_TYPES = ['Bug', 'Waiver', 'Risk', 'Performance', 'Configuration'];
const ISSUE_SEVS = ['Critical', 'High', 'Medium', 'Low', 'Informational'];
const ISSUE_STATUSES = ['Open', 'In Progress', 'Waived', 'Resolved', 'Deferred'];

const inputStyle: React.CSSProperties = {
  width: '100%', background: '#0a0f1e', border: '1px solid #213060', borderRadius: 8,
  padding: '8px 10px', fontSize: 13, color: '#f0f4ff',
};
const labelStyle: React.CSSProperties = {
  display: 'block', fontSize: 10, fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.08em', color: '#6b7db3', marginBottom: 4,
};

/** PE Validation Checklist + Issues Register + Sign-off gate, ported from
 * initGovernanceTab()/_updatePeSignoffGate()/refreshGoLiveBanner() (app.js).
 * This is vanilla's "Governance" nav item (data-view="findings", separate
 * from PE Findings/data-view="insights") — was entirely missing in React. */
export function GovernancePanel() {
  const classes = useStyles();
  const { data, setIssues, setApprovals } = useAppData();
  const [formOpen, setFormOpen] = useState(false);
  const [issueDraft, setIssueDraft] = useState({ id: '', type: 'Bug', sev: 'Critical', status: 'Open', owner: '', eta: '', desc: '', mit: '' });
  const [descError, setDescError] = useState(false);
  const [exportBusy, setExportBusy] = useState(false);
  const [exportError, setExportError] = useState<string | null>(null);

  const issues = data.issues;
  const approvals = data.approvals;

  const checklistDone = CHECKLIST_ITEMS.filter((c) => approvals.checklist[c.key]).length;
  const checklistTotal = CHECKLIST_ITEMS.length;
  const checklistPct = Math.round((checklistDone / checklistTotal) * 100);
  const checklistComplete = checklistDone === checklistTotal;
  // No live findings-engine blocker cross-check yet (would need the Findings
  // page's decision payload wired in here too) — gated on checklist completion
  // alone for now, same disclaimer-override UX as vanilla.
  const blocked = !checklistComplete;

  const handleAddIssue = () => {
    if (!issueDraft.desc.trim()) { setDescError(true); return; }
    setDescError(false);
    const autoId = `ISS-${String(issues.length + 1).padStart(3, '0')}`;
    const next: IssueRecord = {
      ID: issueDraft.id.trim() || autoId,
      Type: issueDraft.type,
      Severity: issueDraft.sev,
      Status: issueDraft.status,
      Owner: issueDraft.owner.trim(),
      ETA: issueDraft.eta.trim() || 'N/A',
      Description: issueDraft.desc.trim(),
      Mitigation: issueDraft.mit.trim(),
      Logged: new Date().toISOString().slice(0, 10),
    };
    setIssues([...issues, next]);
    setIssueDraft({ id: '', type: 'Bug', sev: 'Critical', status: 'Open', owner: '', eta: '', desc: '', mit: '' });
  };

  const handleRemoveIssue = (idx: number) => setIssues(issues.filter((_, i) => i !== idx));

  const handleChecklistToggle = (key: keyof ApprovalsState['checklist']) => {
    setApprovals({ ...approvals, checklist: { ...approvals.checklist, [key]: !approvals.checklist[key] } });
  };

  const handlePeApprove = (checked: boolean) => {
    setApprovals({
      ...approvals,
      pe: { ...approvals.pe, approved: checked, override_blockers: checked && blocked, date: checked ? (approvals.pe.date || new Date().toISOString().slice(0, 10)) : approvals.pe.date },
    });
  };

  const [ackChecked, setAckChecked] = useState(false);
  const handlePeCheckboxClick = (checked: boolean) => {
    if (checked && blocked && !ackChecked) return;
    handlePeApprove(checked);
  };

  const handleCustApprove = (checked: boolean) => {
    setApprovals({
      ...approvals,
      customer: { ...approvals.customer, approved: checked, date: checked ? (approvals.customer.date || new Date().toISOString().slice(0, 10)) : approvals.customer.date },
    });
  };

  const bothOk = approvals.pe.approved && approvals.customer.approved;
  const custDisplayName = approvals.customer.name || (data.customerName ? `${data.customerName} (unsigned)` : '\u2014');

  const kpiCounts = useMemo(() => ({
    total: issues.length,
    open: issues.filter((i) => i.Status === 'Open' || i.Status === 'In Progress').length,
    waived: issues.filter((i) => i.Status === 'Waived').length,
    resolved: issues.filter((i) => i.Status === 'Resolved').length,
  }), [issues]);

  const handleExportCsv = () => {
    if (!issues.length) return;
    const cols: (keyof IssueRecord)[] = ['ID', 'Type', 'Severity', 'Status', 'Owner', 'ETA', 'Description', 'Mitigation', 'Logged'];
    const rows = [cols.join(','), ...issues.map((iss) => cols.map((c) => `"${String(iss[c] || '').replace(/"/g, '""')}"`).join(','))];
    const blob = new Blob([rows.join('\n')], { type: 'text/csv' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url; a.download = `issues_register_${new Date().toISOString().slice(0, 10)}.csv`; a.click();
    URL.revokeObjectURL(url);
  };

  const handleExportReport = async () => {
    setExportBusy(true);
    setExportError(null);
    try {
      const payload = { ...buildAnalysisPayload(data), approvals, issues };
      const blob = await exportReport(payload);
      const url = URL.createObjectURL(blob);
      const a = document.createElement('a');
      a.href = url; a.download = `PE_Audit_${(data.customerName || 'Report').replace(/\s+/g, '_')}_${new Date().toISOString().slice(0, 10)}.html`; a.click();
      URL.revokeObjectURL(url);
    } catch (err) {
      setExportError(err instanceof Error ? err.message : 'Export failed.');
    } finally {
      setExportBusy(false);
    }
  };

  return (
    <Paper className={classes.panel} elevation={0}>
      <Typography variant="h6" style={{ marginBottom: 16 }}>Governance</Typography>

      <Box style={{ borderRadius: 12, border: '1px solid rgba(245,158,11,.4)', background: 'rgba(245,158,11,.1)', padding: '12px 20px', display: 'flex', alignItems: 'center', gap: 12, marginBottom: 16 }}>
        <span style={{ fontSize: 20 }}>{'\u26a0\ufe0f'}</span>
        <Typography variant="body2" style={{ fontWeight: 700, color: '#f59e0b' }}>Known Issues &amp; Waivers must be reviewed and acknowledged before PE sign-off.</Typography>
      </Box>

      {/* Add Issue / Waiver */}
      <Box className={classes.card} style={{ marginBottom: 16, padding: 0 }}>
        <Box onClick={() => setFormOpen((v) => !v)} display="flex" alignItems="center" justifyContent="space-between" style={{ padding: '14px 20px', cursor: 'pointer' }}>
          <Typography variant="subtitle2">{'+ Add Issue / Waiver'}</Typography>
          <Typography variant="caption" style={{ color: '#3b82f6' }}>{formOpen ? '\u25b4' : '\u25be'}</Typography>
        </Box>
        {formOpen && (
          <Box style={{ padding: '0 20px 20px', borderTop: '1px solid #213060' }}>
            <Box style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(220px, 1fr))', gap: 16, marginTop: 16 }}>
              <Box>
                <label style={labelStyle}>Issue ID</label>
                <input style={inputStyle} placeholder="ISS-001" value={issueDraft.id} onChange={(e) => setIssueDraft({ ...issueDraft, id: e.target.value })} />
              </Box>
              <Box>
                <label style={labelStyle}>Type</label>
                <select style={inputStyle} value={issueDraft.type} onChange={(e) => setIssueDraft({ ...issueDraft, type: e.target.value })}>
                  {ISSUE_TYPES.map((t) => <option key={t}>{t}</option>)}
                </select>
              </Box>
              <Box>
                <label style={labelStyle}>Severity</label>
                <select style={inputStyle} value={issueDraft.sev} onChange={(e) => setIssueDraft({ ...issueDraft, sev: e.target.value })}>
                  {ISSUE_SEVS.map((s) => <option key={s}>{s}</option>)}
                </select>
              </Box>
              <Box>
                <label style={labelStyle}>Status</label>
                <select style={inputStyle} value={issueDraft.status} onChange={(e) => setIssueDraft({ ...issueDraft, status: e.target.value })}>
                  {ISSUE_STATUSES.map((s) => <option key={s}>{s}</option>)}
                </select>
              </Box>
              <Box>
                <label style={labelStyle}>Owner</label>
                <input style={inputStyle} placeholder="PE / Customer / IT" value={issueDraft.owner} onChange={(e) => setIssueDraft({ ...issueDraft, owner: e.target.value })} />
              </Box>
              <Box>
                <label style={labelStyle}>ETA / Waiver Expiry</label>
                <input style={inputStyle} placeholder="DD-MMM-YYYY or N/A" value={issueDraft.eta} onChange={(e) => setIssueDraft({ ...issueDraft, eta: e.target.value })} />
              </Box>
            </Box>
            <Box style={{ marginTop: 16 }}>
              <label style={labelStyle}>Description</label>
              <textarea style={{ ...inputStyle, resize: 'vertical' }} rows={3} placeholder="Describe the issue, root cause, and impact\u2026" value={issueDraft.desc} onChange={(e) => setIssueDraft({ ...issueDraft, desc: e.target.value })} />
            </Box>
            <Box style={{ marginTop: 12 }}>
              <label style={labelStyle}>Mitigation / Waiver Justification</label>
              <textarea style={{ ...inputStyle, resize: 'vertical' }} rows={2} placeholder="Steps taken or reason for waiver\u2026" value={issueDraft.mit} onChange={(e) => setIssueDraft({ ...issueDraft, mit: e.target.value })} />
            </Box>
            <Box display="flex" alignItems="center" style={{ gap: 12, marginTop: 12 }}>
              <Button variant="contained" color="primary" onClick={handleAddIssue}>{'+ Add to Register'}</Button>
              {descError && <Typography variant="caption" style={{ color: '#f43f5e' }}>Description is required.</Typography>}
            </Box>
          </Box>
        )}
      </Box>

      {/* Issues Register */}
      {issues.length > 0 ? (
        <Box style={{ marginBottom: 16 }}>
          <Box style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(120px, 1fr))', gap: 12, marginBottom: 12 }}>
            <Box className={classes.card} style={{ textAlign: 'center', padding: 14 }}>
              <Typography variant="caption" style={{ fontWeight: 700, textTransform: 'uppercase', color: '#6b7db3' }}>Total</Typography>
              <Typography variant="h5" style={{ color: '#3b82f6', fontWeight: 800 }}>{kpiCounts.total}</Typography>
              <Typography variant="caption" color="textSecondary">In register</Typography>
            </Box>
            <Box className={classes.card} style={{ textAlign: 'center', padding: 14 }}>
              <Typography variant="caption" style={{ fontWeight: 700, textTransform: 'uppercase', color: '#6b7db3' }}>Open</Typography>
              <Typography variant="h5" style={{ color: kpiCounts.open > 0 ? '#f43f5e' : '#10d96e', fontWeight: 800 }}>{kpiCounts.open}</Typography>
              <Typography variant="caption" color="textSecondary">Needs action</Typography>
            </Box>
            <Box className={classes.card} style={{ textAlign: 'center', padding: 14 }}>
              <Typography variant="caption" style={{ fontWeight: 700, textTransform: 'uppercase', color: '#6b7db3' }}>Waivers</Typography>
              <Typography variant="h5" style={{ color: '#f59e0b', fontWeight: 800 }}>{kpiCounts.waived}</Typography>
              <Typography variant="caption" color="textSecondary">Accepted risk</Typography>
            </Box>
            <Box className={classes.card} style={{ textAlign: 'center', padding: 14 }}>
              <Typography variant="caption" style={{ fontWeight: 700, textTransform: 'uppercase', color: '#6b7db3' }}>Resolved</Typography>
              <Typography variant="h5" style={{ color: '#10d96e', fontWeight: 800 }}>{kpiCounts.resolved}</Typography>
              <Typography variant="caption" color="textSecondary">Closed</Typography>
            </Box>
          </Box>
          {issues.map((iss, idx) => {
            const sevColor = SEV_COLOR[iss.Severity] || '#6b7db3';
            const statColor = STAT_COLOR[iss.Status] || '#6b7db3';
            return (
              <Box key={iss.ID} style={{ borderRadius: 10, borderLeft: `4px solid ${sevColor}`, border: '1px solid #213060', background: '#111d36', padding: '10px 14px', marginBottom: 8 }}>
                <Box display="flex" alignItems="flex-start" justifyContent="space-between" style={{ gap: 8, flexWrap: 'wrap' }}>
                  <Box display="flex" alignItems="center" style={{ gap: 8, flexWrap: 'wrap' }}>
                    <Typography component="span" variant="body2" style={{ fontWeight: 800, color: sevColor }}>{iss.ID}</Typography>
                    <Typography component="span" variant="caption" color="textSecondary">{iss.Type} {'\u00b7'} {iss.Severity}</Typography>
                  </Box>
                  <Box display="flex" alignItems="center" style={{ gap: 8, flexWrap: 'wrap' }}>
                    <span style={{ fontSize: 10, fontWeight: 700, padding: '2px 8px', borderRadius: 20, color: statColor, border: `1px solid ${statColor}66`, background: `${statColor}1a` }}>{iss.Status}</span>
                    <Typography component="span" variant="caption" color="textSecondary">Owner: {iss.Owner || '\u2014'} {'\u00b7'} ETA: {iss.ETA}</Typography>
                    <Button size="small" onClick={() => handleRemoveIssue(idx)} style={{ color: '#6b7db3', fontSize: 10, minWidth: 0 }}>{'\u2715'} Remove</Button>
                  </Box>
                </Box>
                <Typography variant="body2" style={{ marginTop: 6 }}>{iss.Description}</Typography>
                {iss.Mitigation && <Typography variant="caption" color="textSecondary" style={{ display: 'block', marginTop: 4 }}>{'\ud83d\udee1 '}{iss.Mitigation}</Typography>}
                <Typography variant="caption" color="textSecondary" style={{ display: 'block', marginTop: 4 }}>Logged: {iss.Logged}</Typography>
              </Box>
            );
          })}
          <Button size="small" onClick={handleExportCsv} style={{ color: '#3b82f6', fontSize: 11 }}>{'\u2b73 Export Issues Register CSV'}</Button>
        </Box>
      ) : (
        <Typography variant="body2" color="textSecondary" style={{ marginBottom: 16 }}>{'\u2705 No issues logged. Add any known items above before PE sign-off.'}</Typography>
      )}

      {/* PE Validation Checklist */}
      <Box className={classes.card} style={{ marginBottom: 16 }}>
        <Typography variant="subtitle2" style={{ marginBottom: 12 }}>{'\ud83d\udccb PE Validation Checklist'}</Typography>
        <Box style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(260px, 1fr))', gap: '6px 24px' }}>
          {CHECKLIST_ITEMS.map((item) => (
            <label key={item.key} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 13, color: approvals.checklist[item.key] ? '#f0f4ff' : '#8899bb', cursor: 'pointer' }}>
              <input type="checkbox" checked={approvals.checklist[item.key]} onChange={() => handleChecklistToggle(item.key)} style={{ accentColor: '#3b82f6', width: 16, height: 16 }} />
              {item.label}
            </label>
          ))}
        </Box>
        <Box style={{ marginTop: 14 }}>
          <Box style={{ height: 8, borderRadius: 6, background: '#0a0f1e', overflow: 'hidden' }}>
            <div style={{ height: '100%', width: `${checklistPct}%`, borderRadius: 6, background: checklistPct === 100 ? '#10d96e' : checklistPct >= 67 ? '#f59e0b' : '#f43f5e', transition: 'width .3s ease' }} />
          </Box>
          <Typography variant="caption" color="textSecondary" style={{ display: 'block', marginTop: 4 }}>Checklist {checklistDone}/{checklistTotal} complete</Typography>
        </Box>
      </Box>

      {/* Sign-off cards */}
      <Box style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(300px, 1fr))', gap: 16, marginBottom: 16 }}>
        <Box className={classes.card}>
          <Typography variant="subtitle2" style={{ marginBottom: 12 }}>{'\ud83d\udc64 PE Engineer Sign-Off'}</Typography>
          <label style={labelStyle}>Name</label>
          <input style={inputStyle} placeholder="Enter your name" value={approvals.pe.name} onChange={(e) => setApprovals({ ...approvals, pe: { ...approvals.pe, name: e.target.value } })} />
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 12, cursor: 'pointer' }}>
            <input type="checkbox" checked={approvals.pe.approved} onChange={(e) => handlePeCheckboxClick(e.target.checked)} style={{ accentColor: '#10d96e', width: 16, height: 16 }} />
            <Typography variant="body2" color="textSecondary">I confirm all PE checklist items are validated</Typography>
          </label>
          {blocked && (
            <>
              <Typography variant="caption" style={{ display: 'block', color: '#f59e0b', marginTop: 6 }}>
                {'\u26a0 '}{checklistTotal - checklistDone} checklist item(s) incomplete.
              </Typography>
              <Box display="flex" alignItems="flex-start" style={{ gap: 8, marginTop: 8, borderRadius: 8, border: '1px solid rgba(245,158,11,.4)', background: 'rgba(245,158,11,.05)', padding: 10 }}>
                <input type="checkbox" checked={ackChecked} onChange={(e) => setAckChecked(e.target.checked)} style={{ accentColor: '#f59e0b', width: 16, height: 16, marginTop: 2 }} />
                <Typography variant="caption" style={{ color: '#f59e0b' }}>
                  I have reviewed the open item(s) above and choose to proceed with sign-off despite them. This override will be recorded on the exported report.
                </Typography>
              </Box>
            </>
          )}
          <Typography variant="caption" style={{ display: 'block', marginTop: 10, fontWeight: 700, color: approvals.pe.approved ? (approvals.pe.override_blockers ? '#f59e0b' : '#10d96e') : '#f59e0b' }}>
            {approvals.pe.approved ? (approvals.pe.override_blockers ? `\u26a0\ufe0f PE Approved (override) \u2014 ${approvals.pe.date}` : `\u2705 PE Approved \u2014 ${approvals.pe.date}`) : '\u23f3 PE Approval Pending'}
          </Typography>
        </Box>
        <Box className={classes.card}>
          <Typography variant="subtitle2" style={{ marginBottom: 12 }}>{'\ud83c\udfe2 Customer Sign-Off'}</Typography>
          <Typography variant="caption" color="textSecondary" style={{ display: 'block', marginBottom: 8 }}>Engagement customer: <span style={{ color: '#f0f4ff', fontWeight: 700 }}>{custDisplayName}</span></Typography>
          <label style={labelStyle}>Customer Representative</label>
          <input style={inputStyle} placeholder="e.g. Antony Castaldi" value={approvals.customer.name} onChange={(e) => setApprovals({ ...approvals, customer: { ...approvals.customer, name: e.target.value } })} />
          <label style={{ display: 'flex', alignItems: 'center', gap: 8, marginTop: 12, cursor: 'pointer' }}>
            <input type="checkbox" checked={approvals.customer.approved} onChange={(e) => handleCustApprove(e.target.checked)} style={{ accentColor: '#10d96e', width: 16, height: 16 }} />
            <Typography variant="body2" color="textSecondary">Customer approves current performance benchmark &amp; UI performance</Typography>
          </label>
          <Typography variant="caption" style={{ display: 'block', marginTop: 10, fontWeight: 700, color: approvals.customer.approved ? '#10d96e' : '#f59e0b' }}>
            {approvals.customer.approved ? `\u2705 Customer Approved \u2014 ${approvals.customer.date}` : '\u23f3 Customer Approval Pending'}
          </Typography>
        </Box>
      </Box>

      {/* Notes */}
      <Box className={classes.card} style={{ marginBottom: 16 }}>
        <label style={labelStyle}>{'\ud83d\udcdd PE Approval Notes / Observations'}</label>
        <TextField
          multiline minRows={3} fullWidth variant="outlined"
          placeholder="e.g. Batch window within SLA, resource utilization healthy, SOW metrics validated, UI benchmark approved."
          value={approvals.notes}
          onChange={(e) => setApprovals({ ...approvals, notes: e.target.value })}
          InputProps={{ style: { background: '#0a0f1e', color: '#f0f4ff', fontSize: 13 } }}
        />
      </Box>

      {/* Go-Live banner */}
      <Box style={{ borderRadius: 16, border: `2px solid ${bothOk ? 'rgba(16,217,110,.5)' : 'rgba(245,158,11,.5)'}`, background: bothOk ? 'rgba(16,217,110,.1)' : 'rgba(245,158,11,.1)', padding: 20, textAlign: 'center', marginBottom: 16 }}>
        <Typography variant="h6" style={{ fontWeight: 800, color: bothOk ? '#10d96e' : '#f59e0b' }}>
          Go-Live Sign-Off Status: {bothOk ? '\u2705 APPROVED' : '\u23f3 PENDING'}
        </Typography>
        <Typography variant="caption" color="textSecondary" style={{ display: 'block', marginTop: 8 }}>
          PE: {approvals.pe.name || '\u2014'} &nbsp;|&nbsp; Customer: {custDisplayName}
        </Typography>
      </Box>

      <Box display="flex" justifyContent="flex-end" flexDirection="column" alignItems="flex-end" style={{ gap: 6 }}>
        <Button variant="contained" color="primary" onClick={handleExportReport} disabled={exportBusy} style={{ fontWeight: 700 }}>
          {exportBusy ? 'Generating\u2026' : 'Export HTML Report'}
        </Button>
        {exportError && <Typography variant="caption" style={{ color: '#f43f5e' }}>{exportError}</Typography>}
      </Box>
    </Paper>
  );
}
