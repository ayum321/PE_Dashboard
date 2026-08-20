import React, { useState } from 'react';
import {
  Box,
  Button,
  CircularProgress,
  Paper,
  Table,
  TableBody,
  TableCell,
  TableHead,
  TableRow,
  Typography,
  makeStyles,
} from '@material-ui/core';
import { getRedFlags } from '../../api/dashboardApi';
import { useAppData } from '../../context/AppDataContext';
import { buildAnalysisPayload } from '../../utils/buildAnalysisPayload';

interface RedFlag {
  id: string;
  category: string;
  context: string;
  question: string;
  risk: string;
}

interface RiskItem {
  area: string;
  risk: string;
  impact: string;
  recommendation: string;
}

const useStyles = makeStyles((theme) => ({
  panel: { padding: theme.spacing(3) },
  row: { display: 'flex', gap: theme.spacing(2), alignItems: 'center', marginTop: theme.spacing(2) },
  empty: { marginTop: theme.spacing(2) },
}));

export function RedFlagsPanel() {
  const classes = useStyles();
  const { data, setRedFlags } = useAppData();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  const handleGenerate = async () => {
    setBusy(true);
    setError(null);
    try {
      const result = await getRedFlags(buildAnalysisPayload(data));
      setRedFlags(result);
    } catch (generateError) {
      setError(generateError instanceof Error ? generateError.message : 'Generating red flags failed.');
    } finally {
      setBusy(false);
    }
  };

  const flags = (data.redFlags?.flags as RedFlag[]) || [];
  const riskMatrix = (data.redFlags?.risk_matrix as RiskItem[]) || [];

  return (
    <Paper className={`${classes.panel} kpi-card`} elevation={0}>
      <Typography variant="h6">Red Flags</Typography>
      <Box className={classes.row}>
        <Button variant="contained" color="primary" onClick={handleGenerate} disabled={busy}>
          Generate Red Flags
        </Button>
        {busy && <CircularProgress size={22} aria-label="Generating red flags" />}
      </Box>
      {error && <Typography variant="body2" color="error">{error}</Typography>}

      {!data.redFlags ? (
        <Typography className={classes.empty} variant="body2" color="textSecondary">
          Upload batch and resource data first, then generate the RCA priority matrix.
        </Typography>
      ) : (
        <>
          {riskMatrix.length > 0 && (
            <Table size="small" className="pe-table" aria-label="Risk matrix" style={{ marginTop: 16 }}>
              <TableHead>
                <TableRow>
                  <TableCell>Area</TableCell>
                  <TableCell>Risk</TableCell>
                  <TableCell>Impact</TableCell>
                  <TableCell>Recommendation</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {riskMatrix.map((item, index) => (
                  <TableRow key={index}>
                    <TableCell>{item.area}</TableCell>
                    <TableCell>{item.risk}</TableCell>
                    <TableCell>{item.impact}</TableCell>
                    <TableCell>{item.recommendation}</TableCell>
                  </TableRow>
                ))}
              </TableBody>
            </Table>
          )}
          {flags.length > 0 && (
            <Table size="small" className="pe-table" aria-label="Red flag questions" style={{ marginTop: 16 }}>
              <TableHead>
                <TableRow>
                  <TableCell>ID</TableCell>
                  <TableCell>Category</TableCell>
                  <TableCell>Question</TableCell>
                  <TableCell>Risk</TableCell>
                </TableRow>
              </TableHead>
              <TableBody>
                {flags.map((flag) => (
                  <TableRow key={flag.id}>
                    <TableCell>{flag.id}</TableCell>
                    <TableCell>{flag.category}</TableCell>
                    <TableCell>{flag.question}</TableCell>
                    <TableCell>{flag.risk}</TableCell>
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
