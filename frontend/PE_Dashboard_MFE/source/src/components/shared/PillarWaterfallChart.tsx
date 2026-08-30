import React from 'react';
import Highcharts from '../../theme/highchartsSetup';
import HighchartsReact from 'highcharts-react-official';
import { colors } from '../../theme/colors';
import { Box } from '@material-ui/core';

export interface PillarWaterfallChartProps {
  pillars: Record<string, number>;
  pillarWeights: Record<string, number>;
  pillarContributions: Record<string, number>;
  compositeScore: number;
}

export function PillarWaterfallChart({
  pillars,
  pillarWeights,
  pillarContributions,
  compositeScore,
}: PillarWaterfallChartProps) {
  if (!pillars || Object.keys(pillars).length === 0) {
    return null;
  }

  const data = Object.keys(pillars).map((pillarName) => {
    const score = pillars[pillarName] || 0;
    const contribution = pillarContributions[pillarName] || 0;
    let color = colors.red;
    if (score >= 90) color = colors.green;
    else if (score >= 60) color = colors.amber;

    return {
      name: pillarName.toUpperCase(),
      y: contribution,
      color: color,
    };
  });

  data.push({
    name: 'TOTAL',
    isSum: true,
    color: colors.blue,
    y: compositeScore
  } as any);

  const options: Highcharts.Options = {
    chart: {
      type: 'waterfall',
      height: 220,
      backgroundColor: 'transparent',
    },
    title: {
      text: undefined,
    },
    xAxis: {
      type: 'category',
      labels: {
        style: {
          color: colors.muted,
        }
      }
    },
    yAxis: {
      title: {
        text: null,
      },
      labels: {
        style: {
          color: colors.muted,
        }
      }
    },
    legend: {
      enabled: false,
    },
    tooltip: {
      pointFormat: '<b>{point.y:,.1f}</b>',
    },
    series: [
      {
        type: 'waterfall',
        data: data,
        dataLabels: {
          enabled: true,
          formatter: function () {
            return Highcharts.numberFormat(this.y as number, 1, '.');
          },
          style: {
            color: colors.white,
            textOutline: 'none',
          },
        },
        pointPadding: 0.15,
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
