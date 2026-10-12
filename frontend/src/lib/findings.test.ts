import { describe, expect, it } from 'vitest';
import modulesJson from '../../../docs/contracts/modules.json';
import reportExamples from '../../../docs/contracts/examples/report.json';
import type { EpisodeFinding, ModuleRegistry, Report } from '../api/types';
import {
  coverageMatrix,
  groupByLevel,
  intervalOf,
  itemLabel,
  moduleItems,
  moduleLevels,
  moduleRole,
  placeLabel,
  rejectsByItem,
  reportersOf,
  scopeText,
  scoreHists,
  skippedReasons,
  unassessableTitle,
} from './findings';

const reg = modulesJson as unknown as ModuleRegistry;
const report = reportExamples.valid[0] as unknown as Report;
const spec = (id: string) => reg.modules.find((m) => m.id === id)!;

describe('the findings views read the registry and the report', () => {
  it('names items from the taxonomy and modules by the group they run in (design doc 26 §2)', () => {
    expect(itemLabel(reg, 'STRM-5')).toMatch(/^STRM-5 /);
    expect(itemLabel(reg, null)).toBe('平台自有项');
    expect(itemLabel(reg, 'XX-9')).toBe('XX-9');
    expect(placeLabel(spec('data_integrity'))).toBe('数据完整性');
    expect(placeLabel(spec('timestamp_check'))).toBe('CPU 检查');
    expect(placeLabel(spec('duplicate_check'))).toBe('CPU 检查');
    expect(placeLabel(spec('task_success'))).toBe('模型检查');
  });

  it('a module rejects, asks or only reports by its codes; under report_only only data integrity rejects', () => {
    expect(moduleRole(spec('timestamp_check'))).toBe('blocking');
    expect(moduleRole(spec('eef_video_consistency'))).toBe('review');      // registry 5.0 (D81): a conflict asks, nothing rejects
    expect(moduleRole(spec('duplicate_check'))).toBe('info');            // D87: duplicates are only reported
    expect(moduleRole(spec('visual_quality'))).toBe('blocking');         // registry 7.1 (D93): a corrupt video rejects
    expect(moduleRole(spec('timestamp_check'), 'report_only')).toBe('info');
    expect(moduleRole(spec('duplicate_check'), 'report_only')).toBe('info');
    // policy 3 (D93): a corrupt video rejects in the modules that decode it, under report_only too
    expect(moduleRole(spec('task_success'), 'report_only')).toBe('blocking');
    // an empty or cut file still rejects (requester 2026-10-05)
    expect(moduleRole(spec('data_integrity'), 'report_only')).toBe('blocking');
  });

  it('lays the coverage out by dimension, control items left out', () => {
    const m = coverageMatrix(reg, report.overview);
    expect([m.covered, m.total, m.taxonomyVersion]).toEqual([4, 6, '1.1']);
    const items = m.dimensions.flatMap((d) => d.items);
    expect(items.map((i) => i.id).sort()).toEqual(['ACT-3', 'AV-1', 'IMG-1', 'MV-1', 'STRM-3', 'TASK-4']);
    const byId = Object.fromEntries(items.map((i) => [i.id, i]));
    expect(byId['TASK-4']).toMatchObject({ status: 'covered', episodes: 13 });
    expect(byId['ACT-3'].status).toBe('not_covered');
    // dimensions in taxonomy order
    expect(m.dimensions.map((d) => d.id)).toEqual(reg.taxonomy.dimensions.map((d) => d.id).filter((d) => m.dimensions.some((x) => x.id === d)));
  });

  it('marks a covered item some episodes could not be assessed for, with the reason', () => {
    const overview = { ...report.overview, coverage: { ...report.overview.coverage, covered: [...report.overview.coverage.covered, 'ACT-4'] } };
    const act4 = coverageMatrix(reg, overview).dimensions.flatMap((d) => d.items).find((i) => i.id === 'ACT-4')!;
    expect(act4.status).toBe('unassessable');
    expect(act4.unassessable).toEqual([{ reason: 'embodiment_not_in_library', title: '本体不在规格库里，或没有给机器人型号', count: 50 }]);
  });

  it('a click on an item goes to the modules that cover it', () => {
    expect(reportersOf(reg, ['visual_quality', 'task_success'], 'TASK-4')).toEqual(['task_success']);
    expect(reportersOf(reg, ['visual_quality'], 'TASK-4')).toEqual([]);
  });

  it('rejects by item: the report counts an episode once per item; an older one is summed', () => {
    expect(rejectsByItem(reg, report.overview).map((r) => r.value)).toEqual([4, 1, 1]);
    expect(rejectsByItem(reg, report.overview)[0].name).toMatch(/^TASK-4 /);
    const older = { ...report.overview, reject_items: undefined, reject_reasons: [
      { module: 'timestamp_check', kind: 'finding' as const, code: 'gap', item: 'STRM-3', count: 2 },
      { module: 'timestamp_check', kind: 'finding' as const, code: 'jitter', item: 'STRM-3', count: 1 },
      { module: 'task_success', kind: 'human' as const, count: 1 },
    ] };
    expect(rejectsByItem(reg, older)).toEqual([{ name: itemLabel(reg, 'STRM-3'), value: 3 }, { name: '人工整条弃用', value: 1 }]);
  });

  it('a section lists its codes most episodes first, with the level and the cameras', () => {
    const task = report.modules.find((m) => m.id === 'task_success')!;
    const rows = moduleItems(reg, 'task_success', task.summary);
    expect(rows.map((r) => [r.code, r.level, r.episodes])).toEqual([['uncertain', 'review', 9], ['failure', 'blocking', 4]]);
    expect(rows[0].name).toBe(spec('task_success').codes.find((c) => c.code === 'uncertain')!.name_zh);
    const vq = report.modules.find((m) => m.id === 'visual_quality')!;
    expect(moduleItems(reg, 'visual_quality', vq.summary)[0].byCamera).toEqual([{ camera: 'wrist', episodes: 3 }]);
    expect(moduleLevels(task.summary)).toEqual({ blocking: 4, review: 9, info: 0 });
    expect(moduleLevels({ ...task.summary, levels: undefined })).toEqual({ blocking: 4, review: 9, info: 0 });
  });

  it('reads the score distributions per reading', () => {
    const vq = report.modules.find((m) => m.id === 'visual_quality')!;
    expect(scoreHists({ ...vq.summary, score_hist: { score: [{ name: '0.0–0.1', count: 2 }, { name: '0.1–0.2', count: 0 }] } })).toEqual([
      { reading: 'score', bins: [{ name: '0.0–0.1', value: 2 }, { name: '0.1–0.2', value: 0 }] },
    ]);
    expect(scoreHists(undefined)).toEqual([]);
  });
});

describe('one episode', () => {
  const f = (level: string, code: string): EpisodeFinding =>
    ({ module: 'task_success', level, appealable: false, finding: { code, item: 'TASK-4', severity: 'high', message_zh: code } }) as EpisodeFinding;

  it('groups the findings by level in their order', () => {
    const g = groupByLevel([f('info', 'a'), f('blocking', 'b'), f('info', 'c')]);
    expect(g.blocking.map((x) => x.finding.code)).toEqual(['b']);
    expect(g.info.map((x) => x.finding.code)).toEqual(['a', 'c']);
    expect(g.review).toEqual([]);
  });

  it('says what a finding is about and where', () => {
    expect(scopeText({ camera: 'wrist' })).toBe('wrist 相机');
    expect(scopeText({ cameras: ['wrist', 'top'], channel: 'gripper' })).toBe('wrist、top 相机 · gripper 通道');
    expect(scopeText(null)).toBe('');
    expect(intervalOf({ time_s: [1.5, 3.25] })).toEqual({ text: '1.5–3.25 秒', seekS: 1.5 });
    expect(intervalOf({ frames: [10, 10] })).toEqual({ text: '第 11 帧', seekS: null });
    expect(intervalOf({ frames: [0, 9] })?.text).toBe('第 1–10 帧');
    expect(intervalOf({})).toBeNull();
  });
});

describe('episodes a module skipped without judging them (D94, design doc 28 §3)', () => {
  type Summary = Report['modules'][number]['summary'];
  const summary = (over: Partial<Summary>) => ({ assessed_episodes: 38, items: [], unassessable: [], ...over }) as Summary;

  it('says why, most first, in the registry\'s words', () => {
    const s = summary({ skipped_episodes: 10, skipped_reasons: [{ name: 'integrity_rejected', count: 8 }, { name: 'no_task_text', count: 2 }] });
    expect(skippedReasons(s)).toBe('数据完整性已判废 8、没有任务标注 2');
    // one reason: its name alone, the count is the figure next to it
    expect(skippedReasons(summary({ skipped_episodes: 6, skipped_reasons: [{ name: 'integrity_rejected', count: 6 }] }))).toBe('数据完整性已判废');
    expect(skippedReasons(summary({ skipped_episodes: 1, skipped_reasons: [{ name: 'something_new', count: 1 }] }))).toBe('something_new');
  });

  it('says nothing when the module skipped none (or an older report has no such keys)', () => {
    expect(skippedReasons(summary({ skipped_episodes: 0, skipped_reasons: [] }))).toBeNull();
    expect(skippedReasons(summary({}))).toBeNull();
  });

  it('the registry names the two new reasons a module could not assess an item for (registry 7.1)', () => {
    expect(unassessableTitle(reg, 'integrity_rejected')).toBe('数据完整性已判废，没有评估');
    expect(unassessableTitle(reg, 'video_undecodable')).toBe('视频解不开（文件损坏）');
  });
});
