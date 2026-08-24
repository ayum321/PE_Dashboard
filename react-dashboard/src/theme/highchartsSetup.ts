import Highcharts from 'highcharts';
import HighchartsMore from 'highcharts/highcharts-more';
import SolidGauge from 'highcharts/modules/solid-gauge';
import HeatmapModule from 'highcharts/modules/heatmap';
import { colors } from './colors';

// highcharts-more / solid-gauge / heatmap ship as factory functions that
// attach themselves to a Highcharts instance when invoked.
(HighchartsMore as unknown as (hc: typeof Highcharts) => void)(Highcharts);
(SolidGauge as unknown as (hc: typeof Highcharts) => void)(Highcharts);
(HeatmapModule as unknown as (hc: typeof Highcharts) => void)(Highcharts);

// Dark theme matching the rest of the dashboard (configuration/tailwind.config.js).
Highcharts.setOptions({
  chart: {
    backgroundColor: 'transparent',
    style: { fontFamily: "'Sora', 'Inter', system-ui, sans-serif" },
  },
  title: { style: { color: colors.white } },
  subtitle: { style: { color: colors.muted } },
  xAxis: {
    gridLineColor: `${colors.border}66`,
    lineColor: colors.border,
    tickColor: colors.border,
    labels: { style: { color: colors.muted, fontSize: '10px' } },
  },
  yAxis: {
    gridLineColor: `${colors.border}66`,
    lineColor: colors.border,
    tickColor: colors.border,
    labels: { style: { color: colors.muted, fontSize: '10px' } },
    title: { style: { color: colors.muted } },
  },
  legend: { itemStyle: { color: colors.muted }, itemHoverStyle: { color: colors.white } },
  tooltip: {
    backgroundColor: colors.card,
    borderColor: colors.border,
    style: { color: colors.white },
  },
  plotOptions: {
    series: { borderWidth: 0, dataLabels: { style: { color: colors.white, textOutline: 'none' } } },
  },
  credits: { enabled: false },
});

export { Highcharts };
export default Highcharts;
