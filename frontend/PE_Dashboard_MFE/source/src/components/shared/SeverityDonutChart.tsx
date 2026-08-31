import React from 'react';
import Highcharts from '../../theme/highchartsSetup';
import HighchartsReact from 'highcharts-react-official';
import { colors } from '../../theme/colors';
import { Box } from '@material-ui/core';

export interface SeverityDonutChartProps {
  counts: { critical: number; warning: number; info: number; ok: number };
  size?: number;
}

export function SeverityDonutChart({ counts, size = 140 }: SeverityDonutChartProps) {
  if (!counts) return null;
  const total = counts.critical + counts.warning + counts.info + counts.ok;
  if (total === 0) return null;

  const options: Highcharts.Options = {
    chart: {
      type: 'pie',
      height: size,
      width: size,
      backgroundColor: 'transparent',
      margin: [0, 0, 0, 0],
    },
    title: {
      text: total.toString(),
      align: 'center',
      verticalAlign: 'middle',
      y: 6,
      style: {
        color: colors.white,
        fontSize: '14px',
      }
    },
    plotOptions: {
      pie: {
        innerSize: '60%',
        dataLabels: {
          enabled: false,
        },
        borderWidth: 0,
      },
    },
    tooltip: {
      pointFormat: '{series.name}: <b>{point.y}</b>'
    },
    series: [
      {
        type: 'pie',
        name: 'Count',
        data: [
          { name: 'Critical', y: counts.critical, color: colors.red },
          { name: 'Warning', y: counts.warning, color: colors.amber },
          { name: 'Info', y: counts.info, color: colors.blue },
          { name: 'OK', y: counts.ok, color: colors.green },
        ].filter(d => d.y > 0),
      },
    ],
    credits: {
      enabled: false,
    },
  };

  return (
    <Box>
      <HighchartsReact highcharts={Highcharts} options={options} />
    </Box>
  );
}
