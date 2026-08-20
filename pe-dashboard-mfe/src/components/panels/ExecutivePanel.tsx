import React, { useState } from 'react';
import { Box, Button, CircularProgress, Paper, Table, TableBody, TableCell, TableHead, TableRow, Typography, makeStyles } from '@material-ui/core';
import Highcharts from '../../theme/highchartsSetup';
import HighchartsReact from 'highcharts-react-official';
import { getExecutiveDashboard } from '../../api/dashboardApi';
import { useAppData } from '../../context/AppDataContext';
import { buildAnalysisPayload } from '../../utils/buildAnalysisPayload';
import { KpiStatCard } from '../shared/KpiStatCard';

interface OshsComponent {
  score: number | null;
  weight: number;
  contribution: number;
  available?: boolean;
}

interface Oshs {
  score: number;
  grade: string;
  label: string;
  resource_available: boolean;
  findings_penalty?: number;
  floor_applied?: string;
  components: { batch: OshsComponent; sla: OshsComponent; resource: OshsComponent };
}

interface Waterfall {
  batch_contribution: number;
  resource_contribution: number;
  sla_contribution: number;
  batch_target: number;
  resource_target: number;
  sla_target: number;
  max_score: number;
}

interface DecisionCondition {
  key: string;
  label: string;
  pass: boolean;
  actual: string;
  required: string;
  blocker: string;
}

interface Decision {
  status: string;
  reason: string;
  conditions: DecisionCondition[];
}

interface JobSlaBar {
  job_name: string;
  sub_app: string;
  peak_hrs: number;
  sla_ceiling: number;
  buffer_pct: number | null;
  sri: number;
  status: string;
}

interface WindowRisk extends JobSlaBar {
  breach_days: number;
  total_windows: number;
  breach_windows: number;
}

interface ServerHeatCell {
  host: string;
  cpu: number;
  mem: number;
  disk: number;
}

const useStyles = makeStyles((theme) => ({
  panel: { padding: theme.spacing(3) },
  row: { display: 'flex', gap: theme.spacing(2), alignItems: 'center', marginTop: theme.spacing(2) },
  kpiGrid: { display: 'flex', gap: theme.spacing(2), flexWrap: 'wrap', marginTop: theme.spacing(2) },
  kpi: { padding: theme.spacing(1.5), minWidth: 140 },
  narrative: { marginTop: theme.spacing(2), padding: theme.spacing(2) },
  section: { marginTop: theme.spacing(3) },
  empty: { marginTop: theme.spacing(2) },
}));

const isPrimitive = (value: unknown): value is string | number =>
  typeof value === 'number' || typeof value === 'string';

const STATUS_COLOR: Record<string, string> = {
  APPROVED: '#10d96e',
  CONDITIONAL_HOLD: '#f59e0b',
  BLOCKED: '#f43f5e',
  INCOMPLETE: '#6b7db3',
  BREACH: '#f43f5e',
  AT_RISK: '#f59e0b',
  UNKNOWN: '#6b7db3',
  OK: '#10d96e',
};

export function ExecutivePanel() {
  const classes = useStyles();
  const { data, setExecutive } = useAppData();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleGenerate = async () => {
    setBusy(true);
    setError(null);
    try {
      const basePayload = buildAnalysisPayload(data);
      const payload = {
        ...basePayload,
        sla_data: data.slaMatrix,
        findings: data.findings?.findings,
      };
      const result = await getExecutiveDashboard(payload);
      setExecutive(result);
    } catch (generateError) {
      setError(generateError instanceof Error ? generateError.message : 'Generating executive view failed.');
    } finally {
      setBusy(false);
    }
  };

  const kpis = (data.executive?.kpis as Record<string, unknown>) || {};
  const kpiEntries = Object.entries(kpis).filter(([, value]) => isPrimitive(value));
  const subAppMetrics = (data.executive?.sub_app_metrics as Record<string, unknown>[]) || [];
  const subAppColumns = subAppMetrics.length > 0 ? Object.keys(subAppMetrics[0]).filter((key) => isPrimitive(subAppMetrics[0][key])) : [];
  const oshs = data.executive?.oshs as Oshs | undefined;
  const rfcs = data.executive?.rfcs as number | undefined;
  const waterfall = data.executive?.waterfall as Waterfall | undefined;
  const decision = data.executive?.decision as Decision | undefined;
  const windowRisk = ((data.executive?.window_risk as WindowRisk[]) || []);
  const jobSlaBars = ((data.executive?.job_sla_bars as JobSlaBar[]) || []);
  const riskRows = windowRisk.length > 0 ? windowRisk : jobSlaBars;
  const serverHeatmap = ((data.executive?.server_heatmap as ServerHeatCell[]) || []);

  const waterfallOptions: Highcharts.Options | null = waterfall ? {
    chart: { type: 'column', height: 260 },
    title: { text: undefined },
    xAxis: { categories: ['Batch', 'SLA', 'Resource'] },
    yAxis: { title: { text: 'Score contribution' }, max: 100 },
    tooltip: { shared: false },
    series: [
      {
        type: 'column',
        name: 'Target',
        data: [waterfall.batch_target, waterfall.sla_target, waterfall.resource_target],
        color: 'rgba(107,125,179,.35)',
      },
      {
        type: 'column',
        name: 'Actual contribution',
        data: [waterfall.batch_contribution, waterfall.sla_contribution, waterfall.resource_contribution],
        color: '#3b82f6',
      },
    ],
  } : null;

  const heatmapOptions: Highcharts.Options | null = serverHeatmap.length > 0 ? {
    chart: { type: 'heatmap', height: Math.max(240, serverHeatmap.length * 22) },
    title: { text: undefined },
    xAxis: { categories: ['CPU %', 'Memory %', 'Disk %'] },
    yAxis: { categories: serverHeatmap.map((s) => s.host), title: { text: undefined }, reversed: true },
    colorAxis: { min: 0, max: 100, stops: [[0, '#10d96e'], [0.75, '#f59e0b'], [1, '#f43f5e']] },
    tooltip: {
      formatter(this: Highcharts.TooltipFormatterContextObject) {
        const metric = ['CPU', 'Memory', 'Disk'][this.point.x as number];
        return `<b>${serverHeatmap[this.point.y as number]?.host}</b><br/>${metric}: ${this.point.value}%`;
      },
    },
    series: [{
      type: 'heatmap',
      data: serverHeatmap.flatMap((server, y) => [
        { x: 0, y, value: server.cpu },
        { x: 1, y, value: server.mem },
        { x: 2, y, value: server.disk },
      ]),
    }],
  } : null;

  return (
    <Paper className={`${classes.panel} kpi-card`} elevation={0}>
      <Typography variant="h6">Executive Dashboard</Typography>
      <Box className={classes.row}>
        <Button variant="contained" color="primary" onClick={handleGenerate} disabled={busy}>
          Generate Executive Summary
        </Button>
        {busy && <CircularProgress size={22} aria-label="Generating executive summary" />}
      </Box>
      {error && <Typography variant="body2" color="error">{error}</Typography>}

      {!data.executive ? (
        <Typography className={classes.empty} variant="body2" color="textSecondary">
          Upload batch and resource data first, then generate the executive correlation summary.
        </Typography>
      ) : (
        <>
          {decision && (
            <Box
              className={classes.section}
              style={{
                borderRadius: 16,
                border: `1px solid ${STATUS_COLOR[decision.status] || '#6b7db3'}55`,
                background: `${STATUS_COLOR[decision.status] || '#6b7db3'}14`,
                padding: 16,
              }}
            >
              <Typography variant="caption" style={{ textTransform: 'uppercase', letterSpacing: '.1em', fontWeight: 700, color: STATUS_COLOR[decision.status] || '#6b7db3' }}>
                Go-Live Decision Gate
              </Typography>
              <Typography variant="subtitle1" style={{ color: STATUS_COLOR[decision.status] || '#f0f4ff', marginTop: 4 }}>{decision.status.replace('_', ' ')}</Typography>
              <Typography variant="body2" style={{ marginTop: 4 }}>{decision.reason}</Typography>
              {decision.conditions && decision.conditions.length > 0 && (
                <Box style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(200px, 1fr))', gap: 8, marginTop: 12 }}>
                  {decision.conditions.map((condition) => (
                    <Box key={condition.key} style={{ display: 'flex', alignItems: 'center', gap: 8, fontSize: 12 }}>
                      <span className={`status-dot ${condition.pass ? 'status-dot-green' : 'status-dot-red'}`} />
                      <span>
                        <strong>{condition.label}</strong>: {condition.actual} (req. {condition.required})
                      </span>
                    </Box>
                  ))}
                </Box>
              )}
            </Box>
          )}

          <Box style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(140px, 1fr))', gap: 12, marginTop: 16 }}>
            {oshs && (
              <KpiStatCard
                label="OSHS Score"
                value={`${oshs.grade} · ${oshs.score.toFixed(0)}`}
                sub={oshs.label}
                accent={oshs.score >= 85 ? '#10d96e' : oshs.score >= 70 ? '#f59e0b' : '#f43f5e'}
              />
            )}
            {rfcs != null && (
              <KpiStatCard label="RFCS" value={rfcs.toFixed(0)} sub="Failure-resource correlation" accent={rfcs >= 50 ? '#f43f5e' : rfcs >= 25 ? '#f59e0b' : '#10d96e'} />
            )}
            {kpiEntries.map(([key, value]) => (
              <KpiStatCard key={key} label={key.replace(/_/g, ' ')} value={String(value)} accent="#3b82f6" />
            ))}
          </Box>

          {oshs && (
            <Box className={classes.section} style={{ display: 'flex', gap: 8, flexWrap: 'wrap' }}>
              <span className="metric-badge metric-badge-blue">Batch {oshs.components.batch.contribution.toFixed(1)} / {(oshs.components.batch.weight * 100).toFixed(0)}%</span>
              <span className="metric-badge metric-badge-purple">SLA {oshs.components.sla.contribution.toFixed(1)} / {(oshs.components.sla.weight * 100).toFixed(0)}%</span>
              {oshs.components.resource.available !== false && (
                <span className="metric-badge metric-badge-teal">Resource {oshs.components.resource.contribution.toFixed(1)} / {(oshs.components.resource.weight * 100).toFixed(0)}%</span>
              )}
              {oshs.findings_penalty ? <span className="metric-badge metric-badge-red">-{oshs.findings_penalty} findings penalty</span> : null}
              {oshs.floor_applied && <span className="metric-badge metric-badge-amber">Capped: {oshs.floor_applied}</span>}
            </Box>
          )}

          {typeof data.executive.narrative === 'string' && data.executive.narrative && (
            <Paper className={`${classes.narrative} chart-panel`} elevation={0}>
              <Typography variant="body2">{data.executive.narrative}</Typography>
            </Paper>
          )}

          {waterfallOptions && (
            <Box className={classes.section}>
              <Typography variant="subtitle2">Score Waterfall</Typography>
              <Typography variant="caption" color="textSecondary">Actual contribution vs each pillar's target weight</Typography>
              <HighchartsReact highcharts={Highcharts} options={waterfallOptions} />
            </Box>
          )}

          {riskRows.length > 0 && (
            <Box className={classes.section}>
              <Typography variant="subtitle2">{windowRisk.length > 0 ? 'Window Risk (Sub-App)' : 'Job SLA Risk'}</Typography>
              <Table size="small" className="pe-table" aria-label="Executive risk table" style={{ marginTop: 8 }}>
                <TableHead>
                  <TableRow>
                    <TableCell>{windowRisk.length > 0 ? 'Sub-App' : 'Job'}</TableCell>
                    <TableCell align="right">Peak hrs</TableCell>
                    <TableCell align="right">SLA ceiling</TableCell>
                    <TableCell align="right">Buffer %</TableCell>
                    <TableCell align="right">SRI</TableCell>
                    <TableCell>Status</TableCell>
                  </TableRow>
                </TableHead>
                <TableBody>
                  {riskRows.slice(0, 15).map((row, index) => {
                    const color = STATUS_COLOR[row.status] || '#6b7db3';
                    return (
                      <TableRow key={`${row.job_name}-${index}`}>
                        <TableCell>{row.job_name}{row.sub_app ? ` (${row.sub_app})` : ''}</TableCell>
                        <TableCell align="right">{row.peak_hrs.toFixed(2)}</TableCell>
                        <TableCell align="right">{row.sla_ceiling.toFixed(2)}</TableCell>
                        <TableCell align="right">{row.buffer_pct != null ? `${row.buffer_pct.toFixed(1)}%` : '—'}</TableCell>
                        <TableCell align="right">{row.sri.toFixed(2)}</TableCell>
                        <TableCell>
                          <span className="metric-badge" style={{ color, borderColor: `${color}40`, background: `${color}1f` }}>{row.status}</span>
                        </TableCell>
                      </TableRow>
                    );
                  })}
                </TableBody>
              </Table>
            </Box>
          )}

          {heatmapOptions && (
            <Box className={classes.section}>
              <Typography variant="subtitle2">Server Heatmap</Typography>
              <Typography variant="caption" color="textSecondary">CPU / Memory / Disk utilization by server</Typography>
              <HighchartsReact highcharts={Highcharts} options={heatmapOptions} />
            </Box>
          )}

          {subAppMetrics.length > 0 && (
            <Table size="small" className="pe-table" aria-label="Sub-application metrics" style={{ marginTop: 16 }}>
              <TableHead>
                <TableRow>
                  {subAppColumns.map((column) => (
                    <TableCell key={column}>{column.replace(/_/g, ' ')}</TableCell>
                  ))}
                </TableRow>
              </TableHead>
              <TableBody>
                {subAppMetrics.slice(0, 25).map((row, index) => (
                  <TableRow key={index}>
                    {subAppColumns.map((column) => (
                      <TableCell key={column}>{String(row[column])}</TableCell>
                    ))}
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
        </>
      )}
    </Paper>
  );
}
