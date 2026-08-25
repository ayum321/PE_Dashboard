import { createMuiTheme } from '@material-ui/core';
import { colors } from './colors';

// Dark theme matching the original Jinja/Tailwind PE Audit Dashboard exactly.
export const dashboardTheme = createMuiTheme({
  palette: {
    type: 'dark',
    background: { default: colors.bg, paper: colors.card },
    primary: { main: colors.blue },
    secondary: { main: colors.purple },
    error: { main: colors.red },
    warning: { main: colors.amber },
    success: { main: colors.green },
    text: { primary: colors.white, secondary: colors.muted },
    divider: colors.border,
  },
  typography: {
    fontFamily: "'Sora', 'Inter', system-ui, sans-serif",
  },
  overrides: {
    MuiPaper: {
      root: {
        backgroundImage: 'none',
      },
    },
    MuiTableCell: {
      root: {
        borderBottom: `1px solid ${colors.border}66`,
      },
      head: {
        fontSize: '.7rem',
        fontWeight: 700,
        textTransform: 'uppercase',
        letterSpacing: '.1em',
        color: colors.muted,
        background: `${colors.navBg}cc`,
      },
    },
    MuiButton: {
      root: {
        textTransform: 'none',
        borderRadius: 8,
      },
    },
    MuiTextField: {
      root: {
        '& .MuiInput-underline:before': { borderBottomColor: colors.border },
      },
    },
  },
});
