import React from 'react';
import { NavLink } from 'react-router-dom';
import { List, ListItem, ListItemText, Paper, Theme, makeStyles } from '@material-ui/core';

const NAV_ITEMS: { path: string; label: string }[] = [
  { path: '/upload', label: 'Upload & Intake' },
  { path: '/executive', label: 'Executive Dashboard' },
  { path: '/batch', label: 'Batch Review' },
  { path: '/resource', label: 'Resource Review' },
  { path: '/sla-matrix', label: 'SLA Matrix' },
  { path: '/benchmark', label: 'Performance Benchmark' },
  { path: '/sow', label: 'SOW Contract & Volume' },
  { path: '/findings', label: 'PE Findings' },
  { path: '/red-flags', label: 'Red Flags' },
  { path: '/archive', label: 'Report Archive' },
  { path: '/settings', label: 'Settings' },
];

const useStyles = makeStyles((theme: Theme) => ({
  sidebar: {
    width: 220,
    minHeight: '100vh',
    borderRight: `1px solid ${theme.palette.divider}`,
  },
  link: {
    textDecoration: 'none',
    color: 'inherit',
    width: '100%',
  },
  activeItem: {
    backgroundColor: theme.palette.action.selected,
    borderLeft: `3px solid ${theme.palette.primary.main}`,
  },
}));

export function Sidebar() {
  const classes = useStyles();
  return (
    <Paper className={classes.sidebar} elevation={0} square component="nav" aria-label="Dashboard navigation">
      <List>
        {NAV_ITEMS.map((item) => (
          <NavLink key={item.path} to={item.path} className={classes.link} activeClassName={classes.activeItem}>
            <ListItem button>
              <ListItemText primary={item.label} />
            </ListItem>
          </NavLink>
        ))}
      </List>
    </Paper>
  );
}
