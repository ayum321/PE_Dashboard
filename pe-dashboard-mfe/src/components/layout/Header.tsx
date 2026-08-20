import React, { useState } from 'react';
import { AppBar, Button, Toolbar, Typography, makeStyles } from '@material-ui/core';
import { useAppData } from '../../context/AppDataContext';

const useStyles = makeStyles(() => ({
  spacer: { flexGrow: 1 },
}));

export function Header() {
  const classes = useStyles();
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
    <AppBar position="static" color="default" elevation={0}>
      <Toolbar variant="dense">
        <Typography variant="subtitle1">PE Audit Dashboard</Typography>
        <div className={classes.spacer} />
        {data.customerName && (
          <Typography variant="body2" color="textSecondary" style={{ marginRight: 16 }}>
            Customer: {data.customerName}
          </Typography>
        )}
        <Button size="small" variant="outlined" onClick={handleReset} disabled={resetting}>
          {resetting ? 'Resetting...' : 'New Engagement'}
        </Button>
      </Toolbar>
    </AppBar>
  );
}
