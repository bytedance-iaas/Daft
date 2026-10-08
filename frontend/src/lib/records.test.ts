import { describe, expect, it } from 'vitest';
import modulesJson from '../../../docs/contracts/modules.json';
import type { ModuleRegistry, ResultRecord, ResultRecordV2 } from '../api/types';
import { asLegacyRecord, asLegacyReport, isRecordV2, legacyReportResponse, recordScore, recordVerdict } from './records';

const reg = modulesJson as unknown as ModuleRegistry;

function v2(module: string, over: Partial<ResultRecordV2> = {}): ResultRecordV2 {
  return {
    episode_index: 3, module, status: 'ok', findings: [], assessed: ['STRM-3'], unassessable: [], readings: {},
    details: { reason: 'kept as v1 wrote it' }, evidence: [], elapsed_s: 0.1, error: null, ...over,
  } as ResultRecordV2;
}

const finding = (code: string, item: string) => ({ code, item, severity: 'high' as const, message_zh: code });

describe('records of either format read as 1.0 (design doc 17 §1: what the modules\' own views read)', () => {
  it('tells the formats apart and finds the score among the readings', () => {
    const old = { episode_index: 1, module: 'motion_quality', verdict: 'scored', passed: null, score: 0.7, gate: 'soft', details: {}, evidence: [], elapsed_s: null, error: null } as ResultRecord;
    expect(isRecordV2(old)).toBe(false);
    expect(recordScore(old)).toBe(0.7);
    expect(recordVerdict(old, reg)).toBe('scored');
    expect(asLegacyRecord(old, reg)).toBe(old);
    const rec = v2('motion_quality', { readings: { score: 0.42 } });
    expect(isRecordV2(rec)).toBe(true);
    expect(recordScore(rec)).toBe(0.42);
  });

  it('follows the backend rules (records.legacy_verdict): error, blocking, review, score, nothing assessed, pass', () => {
    expect(recordVerdict(v2('timestamp_check', { status: 'error', assessed: [] }), reg)).toBe('error');
    expect(recordVerdict(v2('timestamp_check', { findings: [finding('gap', 'STRM-3')] }), reg)).toBe('fail');
    expect(recordVerdict(v2('task_success', { findings: [finding('uncertain', 'TASK-4')] }), reg)).toBe('abstain');
    expect(recordVerdict(v2('motion_quality', { readings: { score: 0.1 } }), reg)).toBe('scored');   // P18: never a reject
    expect(recordVerdict(v2('visual_quality', { assessed: [] }), reg)).toBe('abstain');
    // only the dataset-level items: nothing of its own per episode was assessed
    expect(recordVerdict(v2('motion_quality', { assessed: ['ACT-6'] }), reg)).toBe('abstain');
    expect(recordVerdict(v2('motion_quality', { assessed: ['ACT-1', 'ACT-6'] }), reg)).toBe('pass');
    // an item the module rejects on that it could not assess: an abstention, as 1.0's passed=None
    expect(recordVerdict(v2('kinematic_limits', { assessed: ['ACT-3'], unassessable: [{ item: 'ACT-4', reason: 'not_applicable', message_zh: '算不出来' }] }), reg)).toBe('abstain');
    // an info finding (a retired soft judgement) passes
    expect(recordVerdict(v2('video_action_sync', { assessed: ['AV-1', 'AV-3'], findings: [finding('undecidable', 'AV-3')] }), reg)).toBe('pass');
    expect(recordVerdict(v2('timestamp_check'), reg)).toBe('pass');
    expect(recordVerdict(undefined, reg)).toBeNull();
  });

  it('a record 2.0 gets 1.0 fields back and keeps its details', () => {
    const legacy = asLegacyRecord(v2('timestamp_check', { findings: [finding('gap', 'STRM-3')] }), reg);
    expect(legacy).toMatchObject({ episode_index: 3, module: 'timestamp_check', verdict: 'fail', passed: false, score: null, gate: 'none' });   // no gate in registry 2.0
    expect(legacy.details).toEqual({ reason: 'kept as v1 wrote it' });
    expect(asLegacyRecord(v2('task_success', { findings: [finding('uncertain', 'TASK-4')] }), reg).passed).toBeNull();
  });
});

describe('a report 2.0 read as 1.0', () => {
  const report = {
    schema_version: '2.0',
    overview: {
      reject_reasons: [
        { module: 'timestamp_check', kind: 'finding', code: 'gap', item: 'STRM-3', count: 2 },
        { module: 'timestamp_check', kind: 'finding', code: 'fragment', item: 'STRM-5', count: 1 },
        { module: 'dedup', kind: 'duplicate', code: 'duplicate', item: 'SET-1', count: 1 },
      ],
    },
    modules: [
      { id: 'motion_quality', summary: { counts: {}, assessed_episodes: 8, items: [], unassessable: [], score_hist: { score: [{ name: '0.9–1.0', count: 8 }], smoothness: [{ name: '0.9–1.0', count: 7 }] } } },
      { id: 'timestamp_check', summary: { counts: {} } },
    ],
  };

  it('sums the rejects per module, keeps the composite score distribution; no gate any more (a placeholder)', () => {
    const legacy = asLegacyReport(report);
    expect(legacy.overview.reject_reasons).toEqual([{ module: 'timestamp_check', count: 3 }, { module: 'dedup', count: 1 }]);
    expect(legacy.modules.map((m) => m.gate)).toEqual(['none', 'none']);
    expect(legacy.modules[0].summary).toEqual({ counts: {}, score_hist: [{ name: '0.9–1.0', count: 8 }] });   // 2.0 statistics left out
    expect('score_hist' in legacy.modules[1].summary).toBe(false);
  });

  it('the response keeps the report 2.0 itself for the findings views', () => {
    const r = legacyReportResponse({ revision: 1, report, links: [] });
    expect(r.v2).toBe(report);
    expect(r.report.modules.map((m) => m.id)).toEqual(['motion_quality', 'timestamp_check']);
    expect(legacyReportResponse({ revision: 1, report: { schema_version: '1.0', overview: { reject_reasons: [] }, modules: [] }, links: [] }).v2).toBeNull();
  });

  it('leaves a report 1.0 as it is (D59)', () => {
    const old = { schema_version: '1.0', overview: { reject_reasons: [] }, modules: [] };
    expect(asLegacyReport(old)).toBe(old);
  });
});
