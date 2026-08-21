import React from 'react';
import HighchartsReact from 'highcharts-react-official';
import Highcharts from '../../theme/highchartsSetup';

interface MiniGaugeProps {
  label: string;
  value: number;
  sub: string;
  threshold: number;
  size?: number;
  tooltip?: string;
  overrideColor?: string;
}

/** Small solid-gauge ring matching the original dashboard's Avg CPU/Mem/Disk rings. */
export function MiniGauge({ label, value, sub, threshold, size = 90, tooltip, overrideColor }: MiniGaugeProps) {
  const color = overrideColor || (value >= threshold ? '#f43f5e' : value >= threshold * 0.8 ? '#f59e0b' : '#10d96e');

  const options: Highcharts.Options = {
    chart: { type: 'solidgauge', height: size, width: size, backgroundColor: 'transparent' },
    title: { text: undefined },
    pane: {
      center: ['50%', '50%'],
      size: '100%',
      startAngle: 0,
      endAngle: 360,
      background: [{ backgroundColor: 'rgba(255,255,255,.06)', innerRadius: '75%', outerRadius: '100%', shape: 'arc', borderWidth: 0 }],
    },
    yAxis: { min: 0, max: 100, lineWidth: 0, tickWidth: 0, labels: { enabled: false }, stops: [[0, color]] },
    series: [{
      type: 'solidgauge',
      data: [{ y: value, color }],
      dataLabels: {
        format: `<span style="font-size:13px;font-weight:800;color:${color}">{y:.0f}%</span>`,
        useHTML: true,
        y: -size * 0.05,
      },
      rounded: false,
    }],
  };

  return (
    <div
      className="kpi-card"
      title={tooltip}
      style={{ borderRadius: 12, padding: 12, display: 'flex', flexDirection: 'column', alignItems: 'center', minWidth: 120, cursor: tooltip ? 'help' : 'default' }}
    >
      <div style={{ fontSize: '10.5px', fontWeight: 700, textTransform: 'uppercase', letterSpacing: '.1em', color: '#6b7db3' }}>{label}</div>
      <HighchartsReact highcharts={Highcharts} options={options} />
      <div style={{ fontSize: 11, color: '#6b7db3' }}>{sub}</div>
    </div>
  );
}
