import React from 'react';
import Highcharts from '../../theme/highchartsSetup';
import HighchartsReact from 'highcharts-react-official';
import { colors } from '../../theme/colors';
import { Box } from '@material-ui/core';

export interface PillarRadarChartProps {
  pillars: Record<string, number>;
  size?: number;
}

export function PillarRadarChart({ pillars, size = 220 }: PillarRadarChartProps) {
  if (!pillars || Object.keys(pillars).length === 0) {
    return null;
  }

  const categories = Object.keys(pillars).map((k) => k.toUpperCase());
  const data = Object.values(pillars);

  const options: Highcharts.Options = {
    chart: {
      polar: true,
      type: 'area',
      height: size,
      backgroundColor: 'transparent',
    },
    title: {
      text: undefined,
    },
    pane: {
      size: '80%',
    },
    xAxis: {
      categories: categories,
      tickmarkPlacement: 'on',
      lineWidth: 0,
      labels: {
        style: {
          color: colors.muted,
        }
      }
    },
    yAxis: {
      gridLineInterpolation: 'polygon',
      min: 0,
      max: 100,
      lineWidth: 0,
      labels: {
        enabled: false,
      }
    },
    tooltip: {
      shared: true,
      pointFormat: 'Score: <b>{point.y:,.1f}</b>',
    },
    legend: {
      enabled: false,
    },
    series: [
      {
        name: 'Score',
        type: 'area',
        data: data,
        pointPlacement: 'on',
        color: colors.blue,
        fillColor: 'rgba(59, 130, 246, 0.3)',
        marker: {
          enabled: true,
        }
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
