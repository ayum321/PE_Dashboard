import React, { useEffect, useState } from 'react';
import { Box, Button, CircularProgress, MenuItem, Paper, TextField, Typography, makeStyles } from '@material-ui/core';
import { getConfig, updateConfig } from '../../api/dashboardApi';

const useStyles = makeStyles((theme) => ({
  panel: { padding: theme.spacing(3) },
  fields: { display: 'flex', gap: theme.spacing(2), flexWrap: 'wrap', marginTop: theme.spacing(2), maxWidth: 640 },
  field: { minWidth: 200 },
  row: { display: 'flex', gap: theme.spacing(2), alignItems: 'center', marginTop: theme.spacing(3) },
}));

export function SettingsPanel() {
  const classes = useStyles();
  const [busy, setBusy] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState<string | null>(null);
  const [saved, setSaved] = useState(false);
  const [dailySla, setDailySla] = useState('');
  const [weeklySla, setWeeklySla] = useState('');
  const [monthlySla, setMonthlySla] = useState('');
  const [benchmarkThreshold, setBenchmarkThreshold] = useState('');
  const [slaMode, setSlaMode] = useState('daily');
  const [aiTextProvider, setAiTextProvider] = useState('');

  useEffect(() => {
    getConfig()
      .then((config) => {
        if (config.daily_sla_hrs != null) setDailySla(String(config.daily_sla_hrs));
        if (config.weekly_sla_hrs != null) setWeeklySla(String(config.weekly_sla_hrs));
        if (config.monthly_sla_hrs != null) setMonthlySla(String(config.monthly_sla_hrs));
        if (config.benchmark_threshold != null) setBenchmarkThreshold(String(config.benchmark_threshold));
        if (config.sla_mode) setSlaMode(String(config.sla_mode));
        if (config.ai_text_provider) setAiTextProvider(String(config.ai_text_provider));
      })
      .catch((fetchError) => setError(fetchError instanceof Error ? fetchError.message : 'Failed to load config.'))
      .finally(() => setBusy(false));
  }, []);

  const handleSave = async () => {
    setSaving(true);
    setError(null);
    setSaved(false);
    try {
      await updateConfig({
        daily_sla_hrs: dailySla ? Number(dailySla) : undefined,
        weekly_sla_hrs: weeklySla ? Number(weeklySla) : undefined,
        monthly_sla_hrs: monthlySla ? Number(monthlySla) : undefined,
        benchmark_threshold: benchmarkThreshold ? Number(benchmarkThreshold) : undefined,
        sla_mode: slaMode,
        ai_text_provider: aiTextProvider || undefined,
      });
      setSaved(true);
    } catch (saveError) {
      setError(saveError instanceof Error ? saveError.message : 'Failed to save config.');
    } finally {
      setSaving(false);
    }
  };

  if (busy) {
    return (
      <Paper className={`${classes.panel} kpi-card`} elevation={0}>
        <CircularProgress size={22} aria-label="Loading settings" />
      </Paper>
    );
  }

  return (
    <Paper className={`${classes.panel} kpi-card`} elevation={0}>
      <Typography variant="h6">Settings</Typography>
      {error && <Typography variant="body2" color="error">{error}</Typography>}
      <Box className={classes.fields}>
        <TextField
          id="settings-daily-sla"
          className={classes.field}
          size="small"
          label="Daily SLA hours"
          value={dailySla}
          onChange={(event) => setDailySla(event.target.value)}
        />
        <TextField
          id="settings-weekly-sla"
          className={classes.field}
          size="small"
          label="Weekly SLA hours"
          value={weeklySla}
          onChange={(event) => setWeeklySla(event.target.value)}
        />
        <TextField
          id="settings-monthly-sla"
          className={classes.field}
          size="small"
          label="Monthly SLA hours"
          value={monthlySla}
          onChange={(event) => setMonthlySla(event.target.value)}
        />
        <TextField
          id="settings-benchmark-threshold"
          className={classes.field}
          size="small"
          label="Benchmark threshold %"
          value={benchmarkThreshold}
          onChange={(event) => setBenchmarkThreshold(event.target.value)}
        />
        <TextField
          id="settings-sla-mode"
          className={classes.field}
          size="small"
          select
          label="SLA mode"
          value={slaMode}
          onChange={(event) => setSlaMode(event.target.value)}
        >
          <MenuItem value="daily">Daily</MenuItem>
          <MenuItem value="weekly">Weekly</MenuItem>
          <MenuItem value="monthly">Monthly</MenuItem>
        </TextField>
        <TextField
          id="settings-ai-text-provider"
          className={classes.field}
          size="small"
          select
          label="AI text provider"
          value={aiTextProvider}
          onChange={(event) => setAiTextProvider(event.target.value)}
        >
          <MenuItem value="">Not set</MenuItem>
          <MenuItem value="gemini">Gemini</MenuItem>
          <MenuItem value="nvidia">NVIDIA</MenuItem>
        </TextField>
      </Box>
      <Box className={classes.row}>
        <Button variant="contained" color="primary" onClick={handleSave} disabled={saving}>
          Save Settings
        </Button>
        {saving && <CircularProgress size={22} aria-label="Saving" />}
        {saved && <Typography variant="body2" color="primary">Saved.</Typography>}
      </Box>
    </Paper>
  );
}
