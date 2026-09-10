import { clusterFindings } from './FindingsCrux';
import { FindingItem } from './FindingsDataGrid';

const f = (level: string, text: string, extra: Partial<FindingItem> = {}): FindingItem => ({
  level, text, ...extra,
});

describe('clusterFindings', () => {
  it('collapses findings the engines restate about the same job', () => {
    // Real shape from the deployed ledger: one breaching workflow surfaced
    // four different ways.
    const findings = [
      f('critical', "Per-Job SLA: 1 run(s) BREACHED individual SLA ceiling", { root_cause: 'JOB_SLA_BREACH' }),
      f('critical', "1 job(s) breaching individual SLA ceiling", { root_cause: 'JOB_SLA_BREACH' }),
      f('critical', "Thin SLA buffer: 'JDA_PROCESSING_JOB_WKLY_2' has only ~55.5% headroom", { root_cause: 'JOB_SLA_BREACH' }),
      f('warning', 'Fleet grade C', { root_cause: 'FLEET_HEALTH' }),
    ];
    const clusters = clusterFindings(findings);
    expect(clusters).toHaveLength(2);
    expect(clusters[0].lead.level).toBe('critical');
    expect(clusters[0].related).toBe(2);
  });

  it('orders critical clusters ahead of warnings', () => {
    const clusters = clusterFindings([
      f('warning', 'Something to watch', { root_cause: 'W' }),
      f('critical', 'Something blocking', { root_cause: 'C' }),
    ]);
    expect(clusters[0].lead.text).toBe('Something blocking');
  });

  it('keeps genuinely distinct findings separate', () => {
    const clusters = clusterFindings([
      f('critical', 'Job SLA ceiling breached on batch window'),
      f('critical', 'Server CPU saturation above ninety percent'),
    ]);
    expect(clusters).toHaveLength(2);
    expect(clusters.every((c) => c.related === 0)).toBe(true);
  });

  it('clusters on the named entity when no root_cause is supplied', () => {
    const clusters = clusterFindings([
      f('critical', "SLA breach — root cause unidentified: 'JDA_PROCESSING_JOB_WKLY_2'"),
      f('critical', "SLA breach — root cause unidentified: 'JDA_PROCESSING_JOB_WKLY_2' repeated"),
    ]);
    expect(clusters).toHaveLength(1);
    expect(clusters[0].related).toBe(1);
  });

  it('promotes the member carrying the most actionable evidence', () => {
    const clusters = clusterFindings([
      f('critical', 'Bare restatement', { root_cause: 'X' }),
      f('critical', 'Detailed version', { root_cause: 'X', recommendation: 'Do this specific thing now' }),
    ]);
    expect(clusters[0].lead.text).toBe('Detailed version');
  });
});
