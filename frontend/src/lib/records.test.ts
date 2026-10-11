import { describe, expect, it } from 'vitest';
import modulesJson from '../../../docs/contracts/modules.json';
import type { ModuleRegistry, Report, ResultRecord } from '../api/types';
import { asLegacyRecord, asLegacyReport, asLegacySection, recordScore, recordVerdict, rejectsByModule } from './records';

const reg = modulesJson as unknown as ModuleRegistry;

function rec(module: string, over: Partial<ResultRecord> = {}): ResultRecord {
  return {
    episode_index: 3, module, status: 'ok', findings: [], assessed: ['STRM-3'], unassessable: [], readings: {},
    details: { reason: 'kept as the module wrote it' }, evidence: [], elapsed_s: 0.1, error: null, ...over,
  } as ResultRecord;
}

const finding = (code: string, item: string) => ({ code, item, severity: 'high' as const, message_zh: code });

describe('records read by verdict and score (design doc 17 §1: what the modules\' own views read)', () => {
  it('finds the score among the readings', () => {
    expect(recordScore(rec('motion_quality', { readings: { score: 0.42 } }))).toBe(0.42);
    expect(recordScore(rec('motion_quality'))).toBeNull();
    expect(recordScore(undefined)).toBeNull();
  });

  it('follows the backend rules (records.legacy_verdict): error, blocking, review, score, nothing assessed, pass', () => {
    expect(recordVerdict(rec('timestamp_check', { status: 'error', assessed: [] }), reg)).toBe('error');
    expect(recordVerdict(rec('timestamp_check', { findings: [finding('gap', 'STRM-3')] }), reg)).toBe('fail');
    expect(recordVerdict(rec('task_success', { findings: [finding('uncertain', 'TASK-4')] }), reg)).toBe('abstain');
    expect(recordVerdict(rec('motion_quality', { readings: { score: 0.1 } }), reg)).toBe('scored');   // P18: never a reject
    expect(recordVerdict(rec('visual_quality', { assessed: [] }), reg)).toBe('abstain');
    // only the dataset-level items: nothing of its own per episode was assessed
    expect(recordVerdict(rec('motion_quality', { assessed: ['ACT-6'] }), reg)).toBe('abstain');
    expect(recordVerdict(rec('motion_quality', { assessed: ['ACT-1', 'ACT-6'] }), reg)).toBe('pass');
    // an item the module rejects on that it could not assess: an abstention
    expect(recordVerdict(rec('kinematic_limits', { assessed: ['ACT-3'], unassessable: [{ item: 'ACT-4', reason: 'not_applicable', message_zh: '算不出来' }] }), reg)).toBe('abstain');
    // an info finding passes
    expect(recordVerdict(rec('video_action_sync', { assessed: ['AV-1', 'AV-3'], findings: [finding('undecidable', 'AV-3')] }), reg)).toBe('pass');
    expect(recordVerdict(rec('timestamp_check'), reg)).toBe('pass');
    expect(recordVerdict(undefined, reg)).toBeNull();
  });

  it('a record gets the verdict words and keeps its details', () => {
    const legacy = asLegacyRecord(rec('timestamp_check', { findings: [finding('gap', 'STRM-3')] }), reg);
    expect(legacy).toMatchObject({ episode_index: 3, module: 'timestamp_check', verdict: 'fail', passed: false, score: null });
    expect(legacy.details).toEqual({ reason: 'kept as the module wrote it' });
    expect(asLegacyRecord(rec('task_success', { findings: [finding('uncertain', 'TASK-4')] }), reg).passed).toBeNull();
  });
});

describe('a report read by the modules\' own views', () => {
  const report = {
    schema_version: '2.0',
    overview: {
      reject_reasons: [
        { module: 'timestamp_check', kind: 'finding', code: 'gap', item: 'STRM-3', count: 2 },
        { module: 'timestamp_check', kind: 'finding', code: 'fragment', item: 'STRM-5', count: 1 },
        { module: 'task_success', kind: 'finding', code: 'failure', item: 'TASK-4', count: 1 },
      ],
    },
    modules: [
      { id: 'motion_quality', summary: { counts: {}, assessed_episodes: 8, items: [], unassessable: [], score_hist: { score: [{ name: '0.9–1.0', count: 8 }], smoothness: [{ name: '0.9–1.0', count: 7 }] } } },
      { id: 'timestamp_check', summary: { counts: {}, assessed_episodes: 8, items: [], unassessable: [] } },
    ],
  } as unknown as Report;

  it('a section keeps its own keys and the composite score distribution; the findings statistics are left out', () => {
    const legacy = asLegacyReport(report);
    expect(legacy.modules[0].summary).toEqual({ counts: {}, score_hist: [{ name: '0.9–1.0', count: 8 }] });
    expect('score_hist' in legacy.modules[1].summary).toBe(false);
    expect(asLegacySection(report.modules[1]).summary).toEqual({ counts: {} });
  });

  it('sums the rejects per module', () => {
    expect(rejectsByModule(report.overview)).toEqual([{ module: 'timestamp_check', count: 3 }, { module: 'task_success', count: 1 }]);
  });
});
