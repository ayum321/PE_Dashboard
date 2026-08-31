/** @type {import('tailwindcss').Config} */
module.exports = {
  content: [
    './templates/**/*.html',
    './static/**/*.js',
  ],
  darkMode: 'class',
  safelist: [
    {
      pattern: /^(bg|border|text)-(Cgreen|Camber|Cred|Cblue|Cpurple|Ccyan|Cteal|Corange|Cpink|Cindigo)(\/(5|10|15|20|25|30|40|50|60|70|80|90))?$/,
      variants: ['hover'],
    },
  ],
  theme: {
    extend: {
      colors: {
        Cbg: '#060914',
        Ccard: '#0d1526',
        Ccard2: '#111d36',
        Cborder: '#213060',
        Cgreen: '#10d96e',
        Camber: '#f59e0b',
        Cred: '#f43f5e',
        Cblue: '#3b82f6',
        Cpurple: '#a855f7',
        Ccyan: '#22d3ee',
        Cmuted: '#6b7db3',
        Cwhite: '#f0f4ff',
        CnavBg: '#06091a',
        CnavActiveBg: '#14296a',
        CnavActiveBorder: '#3b82f6',
        CnavSep: '#1a2850',
        CnavGroup: '#1e3060',
        Cteal: '#2dd4bf',
        Corange: '#fb923c',
        Cpink: '#ec4899',
        Cindigo: '#6366f1',
      },
      fontFamily: {
        sora: ['Sora', 'Inter', 'system-ui', 'sans-serif'],
        mono: ['"JetBrains Mono"', 'Menlo', 'monospace'],
      },
      boxShadow: {
        kpi: '0 4px 20px rgba(0,0,0,.4), 0 0 0 1px rgba(59,130,246,.08), inset 0 1px 0 rgba(255,255,255,.04)',
        kpiHov: '0 12px 40px rgba(59,130,246,.18), 0 0 0 1px rgba(59,130,246,.2)',
        panel: '0 4px 24px rgba(0,0,0,.35), inset 0 1px 0 rgba(255,255,255,.04)',
      },
    },
  },
};
