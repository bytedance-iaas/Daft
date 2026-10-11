import { screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { ReportModuleSection } from '../../api/types';
import { asLegacySection, type LegacySection } from '../../lib/records';
import { sectionDigest } from '../../lib/sectionStats';
import { zh } from '../../locales/zh';
import { sampleSummary } from '../../mocks/world';
import { renderWithProviders } from '../../test/render';
import { SECTION_VIEWS } from './sectionViews';

/** A section as ModuleSection hands it to the module's own view. */
const section = (id: string, summary: Record<string, unknown>, extra: Partial<ReportModuleSection> = {}): LegacySection =>
  asLegacySection({ id, state: 'succeeded', summary, tables: [], adjudication: null, ...extra } as ReportModuleSection);

const EEF = {
  uncalibrated: true,
  threshold_profile: 'demo 0.2',
  candidates: 3,
  assessed: 10,
  partially_assessable: 4,
  not_assessable: 2,
  errors: 1,
  coverage_median: 0.81,
  coverage_min: 0.4,
  cameras_measured: 16,
  suspect_by_subitem: [
    { name: 'position_2d', count: 3 },
    { name: 'temporal_alignment', count: 1 },
  ],
  unknown_by_subitem: [{ name: 'orientation_2d', count: 6 }],
  supported_hypotheses: [{ name: 'constant_offset', count: 2 }],
  subitem_status: { position_2d: { ok: 14, suspect: 3, unknown: 2, unsupported: 0, error: 1 }, temporal_alignment: { ok: 16, suspect: 1, unknown: 2, unsupported: 0, error: 1 } },
  labels: [{ name: 'inconsistent', count: 3 }, { name: 'possibly_inconsistent', count: 2 }, { name: 'consistent', count: 9 }, { name: 'cannot_tell', count: 1 }],
  conflict_episodes: 1,
  confidence_uncalibrated: true,
  model_cpu_agreement: 0.8,
  model_votes: 40,
  windows: 60,
  windows_answered: 55,
  windows_failed: 5,
  tracking_suspect: 1,
  vlm_requests: 70,
  review_classes: [
    { name: 'support', count: 40 },
    { name: 'refute', count: 6 },
  ],
  failure_codes: [{ name: 'timeout', count: 5 }],
  counts: { total: 20, pass: 12, fail: 2, abstain: 5, scored: 0, error: 1 },
};

function render(id: string, summary: Record<string, unknown>, extra: Partial<ReportModuleSection> = {}) {
  const View = SECTION_VIEWS[id];
  renderWithProviders(<View taskId="t" rev={1} section={section(id, summary, extra)} />);
  return document.body;
}

const INTEGRITY = {
  counts: { total: 200, pass: 195, fail: 3, abstain: 2, scored: 0, error: 0 },
  integrity_outcomes: { pass: 195, reject: 3, suspect: 2 },
  integrity_codes: [
    { code: 'file_truncated', name: '文件被截断', level: 'reject', count: 2 },
    { code: 'row_invalid', name: '数据不合规', level: 'reject', count: 1 },
    { code: 'stream_missing', name: '缺一路其他条都有的流', level: 'suspect', count: 2 },
  ],
  integrity_files: { files: 600, bytes: 3.85e9, crc_files: 0 },
  integrity_tiers: { L1: true, L2: true, L3: false },
  integrity_dataset: [{ code: 'orphan_files', message: 'data/ 与 videos/ 下有 1 个文件不属于任何 episode：videos/x.mp4' }],
};

describe('the report sections (06 §6.2, F6.2)', () => {
  it('has a view for the checks carried over from v1, duplicate check, the EEF module and the data integrity module', () => {
    expect(Object.keys(SECTION_VIEWS).sort()).toEqual(
      ['data_integrity', 'duplicate_check', 'eef_video_consistency', 'kinematic_limits', 'motion_quality', 'task_success', 'timestamp_check', 'video_action_sync', 'visual_quality'].sort(),
    );
  });

  const cases: [string, Record<string, unknown>][] = [
    ...['timestamp_check', 'kinematic_limits', 'motion_quality', 'visual_quality', 'video_action_sync', 'task_success', 'duplicate_check'].map((id) => [id, sampleSummary(id, 200)] as [string, Record<string, unknown>]),
    ['eef_video_consistency', EEF],
    ['data_integrity', INTEGRITY],
  ];
  it.each(cases)('%s: key figures and at least one chart, in Chinese, without JSON or raw keys', async (id, summary) => {
    const body = render(id, summary);
    const figures = screen.getByTestId(`summary-${id}`);
    expect(figures.querySelectorAll('.stat-cell').length).toBeGreaterThan(2);
    expect((await within(body).findAllByTestId('chart')).length).toBeGreaterThan(0);
    expect(body.textContent).not.toMatch(/[{}"]/);
    for (const raw of ['counts', 'score_hist', 'fail_reasons', 'subscores', 'verdicts', 'judgements', 'suspect_by_subitem', 'review_classes', 'abstain_reason_counts']) {
      expect(body.textContent, raw).not.toContain(raw);
    }
  });

  it('every chart has its own height as a minimum and a body that grows to the row (sixth round)', async () => {
    render('visual_quality', sampleSummary('visual_quality', 200));
    const chart = async (key: string) => within(screen.getByTestId(`chart-${key}`)).findByTestId('chart');
    // content heights; the stylesheet stretches both to the taller one when they share a row
    expect((await chart('cameras')).style.height).toBe('240px');
    expect((await chart('score')).style.height).toBe('200px');
    for (const key of ['cameras', 'score']) {
      const block = screen.getByTestId(`chart-${key}`);
      expect([...block.children].map((c) => c.className)).toEqual(expect.arrayContaining(['section-sub', 'section-chart-body']));
      expect((await chart(key)).parentElement).toHaveClass('section-chart-body');
    }
  });

  it('EEF from registry 5.0: the labels, the conflicts asked, the confidence and what single sources missed (design doc 25 §7)', async () => {
    render('eef_video_consistency', {
      labels: [{ name: 'inconsistent', count: 3 }, { name: 'possibly_inconsistent', count: 2 }, { name: 'consistent', count: 9 }, { name: 'cannot_tell', count: 1 }],
      p_bins: [{ name: '<0.2', count: 8 }, { name: '0.2–0.4', count: 1 }, { name: '0.4–0.7', count: 2 }, { name: '0.7–0.9', count: 2 }, { name: '≥0.9', count: 1 }],
      conflict_episodes: 1, conflict_cells: 1, single_source_episodes: 4,
      single_source_missing: [{ name: 'model_cannot_see', count: 5 }, { name: 'no_gripper_reference', count: 2 }],
      cannot_tell_reasons: [{ name: '判断不了：trajectory.json 里没有这一条', count: 1 }],
      inconsistent_by_subitem: [{ name: 'position_2d', count: 3 }, { name: 'temporal_alignment', count: 2 }],
      tracking_invalid_cells: 0, confidence_uncalibrated: true,
      abstain_reason_counts: [{ name: '判断不了', count: 1 }],
      opinion_aspects: [{ name: 'position', count: 5 }, { name: 'orientation', count: 3 }, { name: 'both', count: 1 }],
    }, { adjudication: { pending: 1, appealable: 0 } });
    const figures = screen.getByTestId('summary-eef_video_consistency');
    expect(figures).toHaveTextContent('不一致3');
    expect(figures).toHaveTextContent('可能不一致2');
    expect(figures).toHaveTextContent('一致9');
    expect(figures).toHaveTextContent('判断不了1');
    expect(figures).toHaveTextContent('冲突1两个渠道结论相反，请人看；人工裁决里待裁 1 条');
    expect(figures).not.toHaveTextContent('判过');
    expect(figures).not.toHaveTextContent('转人工');
    expect(await within(screen.getByTestId('chart-labels')).findByTestId('chart')).toHaveAttribute('aria-label', expect.stringContaining('不一致 3'));
    expect(await within(screen.getByTestId('chart-p-bins')).findByTestId('chart')).toHaveAttribute('aria-label', expect.stringContaining('0.7–0.9 2'));
    expect(await within(screen.getByTestId('chart-by-subitem')).findByTestId('chart')).toHaveAttribute('aria-label', expect.stringContaining('位置 3'));
    expect(await within(screen.getByTestId('chart-single-source')).findByTestId('chart')).toHaveAttribute('aria-label', expect.stringContaining('模型看不了这一项 5'));
    expect(await within(screen.getByTestId('chart-opinion-aspects')).findByTestId('chart')).toHaveAttribute('aria-label', expect.stringContaining('中心 5'));
    expect(screen.queryByTestId('chart-abstain')).toBeNull();                   // a conflict is the only question: no abstention chart
    expect(screen.getByText(/只有两边结论相反（冲突）才请人看，模块不判废/)).toHaveTextContent('置信度未校准');
    expect(sectionDigest({ counts: { total: 15, pass: 13, fail: 0, abstain: 2, scored: 0, error: 0 }, labels: [{ name: 'inconsistent', count: 3 }, { name: 'possibly_inconsistent', count: 2 }], conflict_episodes: 1, candidates: 0 }))
      .toBe('不一致 3 条，可能不一致 2 条，冲突 1 条');
  });

  it('EEF: the wrist cameras\' own motion - figures and the bad stretches by band and reason, reported only (design doc 22 §5.3)', async () => {
    render('eef_video_consistency', {
      labels: [{ name: 'consistent', count: 2 }],
      ego_motion_episodes: 2, ego_motion_suspect: 1, ego_motion_unknown: 1, ego_motion_cameras: 4, ego_motion_cameras_suspect: 1,
      ego_motion_bands: [{ name: 'minor', count: 0 }, { name: 'moderate', count: 5 }, { name: 'severe', count: 0 }],
      ego_motion_reasons: [{ name: 'time_offset', count: 5 }], ego_motion_lag_median_s: 0.467, ego_motion_uncalibrated: true,
    });
    const figures = screen.getByTestId('summary-eef_video_consistency');
    expect(figures).toHaveTextContent('读过自运动2腕部相机，只报告；阈值未校准');
    expect(figures).toHaveTextContent('与位姿不一致1画面里的转动或时间与位姿对不上');
    expect(figures).toHaveTextContent('画面匹配不足1');
    expect(figures).toHaveTextContent('时间差（中位）0.47 秒');
    expect(await within(screen.getByTestId('chart-ego-bands')).findByTestId('chart')).toHaveAttribute('aria-label', expect.stringContaining('中 5'));
    expect(await within(screen.getByTestId('chart-ego-reasons')).findByTestId('chart')).toHaveAttribute('aria-label', expect.stringContaining('时间差 5'));
    // design doc 25 §5.4: the wrist cameras' marks cannot show their own hand's pose
    expect(screen.getByTestId('eef-wrist-note')).toHaveTextContent(zh.viz.overlay.wristNote);
  });

  it('EEF: the trajectory against the dataset\'s own record - figures, why records differ, each source\'s statuses, reported only (D-E16)', async () => {
    render('eef_video_consistency', {
      ...EEF,
      record_compared: 7,
      record_suspect: 3,
      record_status: [{ name: 'ok', count: 4 }, { name: 'suspect', count: 3 }],
      record_by_source: { pose: { ok: 7, suspect: 0, unknown: 0, unsupported: 0, error: 0 }, joints: { ok: 4, suspect: 3, unknown: 0, unsupported: 0, error: 0 } },
      record_reasons: [{ name: 'joints:constant_mismatch', count: 2 }, { name: 'joints:record_deviation', count: 2 }],
      record_lag_median_frames: 0.01,
      record_internal_inconsistent: 3,
    });
    const figures = screen.getByTestId('summary-eef_video_consistency');
    expect(figures).toHaveTextContent('与数据集记录比过7只报告，不参与判决');
    expect(figures).toHaveTextContent('与记录不一致3至少一个来源可疑');
    expect(figures).toHaveTextContent('时间差（中位）0.01 帧');
    expect(figures).toHaveTextContent('数据集内部对不上3位姿列与关节角正解不一致');
    expect(await within(screen.getByTestId('chart-record-reasons')).findByTestId('chart')).toHaveAttribute('aria-label', expect.stringContaining('关节角正解 · 恒定差与映射声明的关系不符 2'));
    const table = screen.getByTestId('eef-record-sources');
    expect(table).toHaveTextContent('位姿列');
    expect(table).toHaveTextContent('关节角正解');
    expect(table).toHaveTextContent('可疑');
    expect(table).not.toHaveTextContent('出错');                 // a status nobody has gets no column
    // the CPU's sub-item statuses and the model review's agreement with it
    expect(figures).toHaveTextContent('模型与 CPU 一致率80%');
    expect(await within(screen.getByTestId('chart-suspect')).findByTestId('chart')).toHaveAttribute('aria-label', expect.stringContaining('位置 3'));
    const matrix = screen.getByTestId('eef-matrix');
    expect(matrix).toHaveTextContent('时间对齐');
    expect(matrix).toHaveTextContent('可疑');
  });

  it('data integrity: outcomes, what was read and the findings by kind, in Chinese (design doc 14)', async () => {
    render('data_integrity', INTEGRITY, { adjudication: { pending: 2, appealable: 0 } });
    const figures = screen.getByTestId('summary-data_integrity');
    expect(figures).toHaveTextContent('判废3文件损坏，不可复议');
    expect(figures).toHaveTextContent('可疑2待人工裁决 2 条（完整性存疑）');
    expect(figures).toHaveTextContent('读过的文件600共 3850.0 MB');
    expect(figures).toHaveTextContent('逐帧解码测试关');
    expect(await within(screen.getByTestId('chart-codes')).findByTestId('chart')).toHaveAttribute('aria-label', expect.stringContaining('文件被截断 2'));
    expect(screen.getByTestId('integrity-dataset')).toHaveTextContent('不属于任何 episode 的文件：data/ 与 videos/ 下有 1 个文件不属于任何 episode');
  });

  it('digests a section into one line for the scope table', () => {
    expect(sectionDigest(sampleSummary('video_action_sync', 100))).toBe('错位判废 1 条，已标注 6 条');
    expect(sectionDigest(sampleSummary('duplicate_check', 100))).toBe('重复 1 组、涉及 2 条');
    expect(sectionDigest(sampleSummary('motion_quality', 100))).toBe('平均分 0.82');
    expect(sectionDigest({ counts: { total: 49, pass: 36, fail: 5, abstain: 6, scored: 0, error: 2 } })).toBe('判废 5 条，转人工 6 条，出错 2 条');
    expect(sectionDigest({ counts: { total: 8, pass: 8, fail: 0, abstain: 0, scored: 0, error: 0 } })).toBe('8 条全部通过');
    expect(sectionDigest(EEF)).toBe('不一致 3 条，可能不一致 2 条，冲突 1 条，出错 1 条');
    expect(sectionDigest({ counts: { total: 3, pass: 3, fail: 0, abstain: 0, scored: 0, error: 0 }, candidates: 2 })).toBe('候选 2 条');
    expect(sectionDigest({})).toBeNull();
  });
});
