import { screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { ReportModuleSection } from '../../api/types';
import { sectionDigest } from '../../lib/sectionStats';
import { zh } from '../../locales/zh';
import { sampleSummary } from '../../mocks/world';
import { renderWithProviders } from '../../test/render';
import { DefaultSectionView, SECTION_VIEWS } from './sectionViews';

const section = (id: string, summary: Record<string, unknown>, extra: Partial<ReportModuleSection> = {}): ReportModuleSection => ({
  id,
  state: 'succeeded',
  gate: 'hard',
  summary,
  tables: [],
  adjudication: null,
  ...extra,
});

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
  judged_pass: 12,
  judged_reject: 2,
  to_human: 5,
  outcomes: [
    { name: 'pass', count: 12 },
    { name: 'reject', count: 2 },
    { name: 'human', count: 5 },
  ],
  human_reasons: [
    { name: 'conflict', count: 3 },
    { name: 'not_assessable', count: 2 },
  ],
  reject_subitems: [{ name: 'position_2d', count: 2 }],
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
  const View = SECTION_VIEWS[id] ?? DefaultSectionView;
  renderWithProviders(<View taskId="t" rev={1} section={section(id, summary, extra)} />);
  return document.body;
}

const INTEGRITY = {
  counts: { total: 200, pass: 195, fail: 3, abstain: 2, scored: 0, error: 0 },
  integrity_outcomes: { pass: 195, reject: 3, suspect: 2 },
  integrity_codes: [
    { code: 'file_truncated', name: '文件被截断', level: 'reject', count: 2 },
    { code: 'row_invalid', name: '数据不合规', level: 'reject', count: 1 },
    { code: 'duplicate_content', name: '与另一条的文件完全相同', level: 'suspect', count: 2 },
  ],
  integrity_files: { files: 600, bytes: 3.85e9, crc_files: 0 },
  integrity_tiers: { L1: true, L2: true, L3: false },
  integrity_dataset: [{ code: 'orphan_files', message: 'data/ 与 videos/ 下有 1 个文件不属于任何 episode：videos/x.mp4' }],
};

describe('the report sections (06 §6.2, F6.2)', () => {
  it('has a view for v1\'s seven modules, the EEF module and the data integrity module', () => {
    expect(Object.keys(SECTION_VIEWS).sort()).toEqual(
      ['data_integrity', 'dedup', 'eef_video_consistency', 'kinematic_limits', 'motion_quality', 'task_success', 'timestamp_check', 'video_action_sync', 'visual_quality'].sort(),
    );
  });

  const cases: [string, Record<string, unknown>][] = [
    ...['timestamp_check', 'kinematic_limits', 'motion_quality', 'visual_quality', 'video_action_sync', 'task_success', 'dedup'].map((id) => [id, sampleSummary(id, 200)] as [string, Record<string, unknown>]),
    ['eef_video_consistency', EEF],
    ['data_integrity', INTEGRITY],
  ];
  it.each(cases)('%s: key figures and at least one chart, in Chinese, without JSON or raw keys', async (id, summary) => {
    const body = render(id, summary);
    const figures = screen.getByTestId(`summary-${id}`);
    expect(figures.querySelectorAll('.stat-cell').length).toBeGreaterThan(2);
    expect((await within(body).findAllByTestId('chart')).length).toBeGreaterThan(0);
    expect(body.textContent).not.toMatch(/[{}"]/);
    for (const raw of ['counts', 'score_hist', 'fail_reasons', 'subscores', 'verdicts', 'judgements', 'family_tree', 'suspect_by_subitem', 'review_classes', 'abstain_reason_counts']) {
      expect(body.textContent, raw).not.toContain(raw);
    }
    expect(screen.queryByTestId(`old-report-${id}`)).toBeNull();
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

  it('EEF without a gripper reference: the opinion figures and charts, no verdict counts (D-E15)', async () => {
    render('eef_video_consistency', {
      judged_pass: 0, judged_reject: 0, to_human: 0,
      opinion_episodes: 7, opinion_flagged: 4, opinion_segments: 9, opinion_failed: 1, opinion_not_assessable: 2,
      abstain_reason_counts: [{ name: '这一条推不出轨迹', count: 2 }],
      opinion_aspects: [{ name: 'position', count: 5 }, { name: 'orientation', count: 3 }, { name: 'both', count: 1 }],
      opinion_confidence: [{ name: '<0.3', count: 2 }, { name: '0.3–0.5', count: 1 }, { name: '0.5–0.7', count: 3 }, { name: '0.7–0.9', count: 2 }, { name: '≥0.9', count: 1 }],
    });
    const figures = screen.getByTestId('summary-eef_video_consistency');
    expect(figures).toHaveTextContent('模型意见7没有夹爪参考，只给意见、不参与判决');
    expect(figures).toHaveTextContent('有不匹配片段4不匹配置信度 ≥ 50% 的片段');
    expect(figures).toHaveTextContent('不匹配片段9');
    expect(figures).toHaveTextContent('没问成1');
    expect(figures).toHaveTextContent('没问模型2没有轨迹：轨迹文件里没有，或推不出来；也不转人工');
    expect(figures).not.toHaveTextContent('判过');
    expect(screen.queryByTestId('chart-abstain')).toBeNull();                   // asked of nobody: no abstention chart
    expect(await within(screen.getByTestId('chart-opinion-confidence')).findByTestId('chart')).toHaveAttribute('aria-label', expect.stringContaining('0.5–0.7 3'));
    expect(await within(screen.getByTestId('chart-opinion-aspects')).findByTestId('chart')).toHaveAttribute('aria-label', expect.stringContaining('中心 5'));
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
      judged_pass: 0, judged_reject: 0, to_human: 0, uncalibrated: true,
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
    expect(screen.getByText(/只有两边结论相反（冲突）才请人看，模块不判废/)).toHaveTextContent('置信度未校准');
    expect(sectionDigest({ counts: { total: 15, pass: 13, fail: 0, abstain: 2, scored: 0, error: 0 }, labels: [{ name: 'inconsistent', count: 3 }, { name: 'possibly_inconsistent', count: 2 }], conflict_episodes: 1, candidates: 0 }))
      .toBe('不一致 3 条，可能不一致 2 条，冲突 1 条');
  });

  it('EEF: the wrist cameras\' own motion - figures and the bad stretches by band and reason, reported only (design doc 22 §5.3)', async () => {
    render('eef_video_consistency', {
      judged_pass: 0, judged_reject: 0, to_human: 0, opinion_episodes: 2, opinion_flagged: 0, opinion_segments: 0,
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

  it('EEF: the verdict figures, why people are asked and the status matrix in Chinese (D49)', async () => {
    render('eef_video_consistency', EEF, { adjudication: { pending: 3, appealable: 2 } });
    const figures = screen.getByTestId('summary-eef_video_consistency');
    expect(figures).toHaveTextContent('判过12');
    expect(figures).toHaveTextContent('判废2');
    expect(figures).toHaveTextContent('转人工5人工裁决里待裁 3 条，其余已裁或已被别的检查判废');
    expect(figures).toHaveTextContent('模型与 CPU 一致率80%');
    expect(document.body).toHaveTextContent('意见冲突、模型给不出意见或判不了的，进人工裁决（阈值未校准）');
    expect(document.body).not.toHaveTextContent('待人工看');
    expect(await within(screen.getByTestId('chart-human')).findByTestId('chart')).toHaveAttribute('aria-label', expect.stringContaining('CPU 与模型意见相反 3'));
    expect(await within(screen.getByTestId('chart-suspect')).findByTestId('chart')).toHaveAttribute('aria-label', expect.stringContaining('位置 3'));
    const matrix = screen.getByTestId('eef-matrix');
    expect(matrix).toHaveTextContent('时间对齐');
    expect(matrix).toHaveTextContent('可疑');
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

  it('a report from before the chart-ready aggregates: what there is, plus a short note', async () => {
    render('visual_quality', { counts: { total: 49, pass: 0, fail: 0, abstain: 0, scored: 49, error: 0 }, mean_score: 0.87 });
    expect(screen.getByTestId('summary-visual_quality')).toHaveTextContent('平均分0.87');
    expect(await within(document.body).findByTestId('chart')).toHaveAttribute('aria-label', '判决分布：打分 49');
    expect(screen.getByTestId('old-report-visual_quality')).toHaveTextContent('统计图上线之前');
  });

  it('an unknown module: its verdicts, scalars, series and dicts of counts as charts, the rest readable', async () => {
    render('brand_new_module', {
      counts: { total: 10, pass: 7, fail: 3, abstain: 0, scored: 0, error: 0 },
      mean: 0.5,
      by_kind: { a: 2, b: 1 },
      top: [{ name: 'x', count: 4 }],
      nested: { inner: { depth: 2 }, list: [1, 2] },
    });
    const charts = (await within(document.body).findAllByTestId('chart')).map((c) => c.getAttribute('aria-label'));
    expect(charts).toEqual(['判决分布：通过 7，判废 3', 'by_kind：a 2，b 1', 'top：x 4']);
    expect(screen.getByTestId('summary-brand_new_module')).toHaveTextContent('平均分0.5');
    expect(screen.getByTestId('section-other')).toHaveTextContent('nestedinner （depth 2） · list 1、2');
    expect(document.body.textContent).not.toMatch(/[{}"]/);
  });

  it('camera_defects: the rider reads in Chinese through the default view (registry 1.14)', async () => {
    const body = render('camera_defects', {
      counts: { total: 8, pass: 0, fail: 0, abstain: 8, scored: 0, error: 0 },
      glitch: { none: 5, minor: 1, severe: 1, unknown: 1 },
      shake: { none: 6, minor: 1, severe: 0, unknown: 1 },
      contamination: { none: 7, minor: 0, severe: 0, unknown: 1 },
      episodes_with_severe: 1,
      episodes_with_minor_or_worse: 2,
      cameras: 16,
      cameras_unanswered: 2,
      clean_ratio_mean: 0.83,
    });
    const charts = (await within(body).findAllByTestId('chart')).map((c) => c.getAttribute('aria-label'));
    expect(charts).toEqual([
      '判决分布：弃权 8',
      '花屏：无 5，轻微 1，严重 1，未知 1',
      '相机抖动：无 6，轻微 1，严重 0，未知 1',
      '镜头污染：无 7，轻微 0，严重 0，未知 1',
    ]);
    const figures = screen.getByTestId('summary-camera_defects');
    expect(figures).toHaveTextContent('模型未答的机位');
    expect(figures).toHaveTextContent('判为无缺陷的比例（均值）');
    expect(document.body.textContent).not.toMatch(/[{}"]|camera|glitch/);
  });

  it('digests a section into one line for the scope table', () => {
    expect(sectionDigest(sampleSummary('video_action_sync', 100))).toBe('错位判废 1 条，已标注 6 条');
    expect(sectionDigest(sampleSummary('skill_profile', 100))).toBe('3 个技能族，标注分歧 2 条');
    expect(sectionDigest(sampleSummary('dedup', 100))).toBe('剔除 1 条');
    expect(sectionDigest(sampleSummary('motion_quality', 100))).toBe('平均分 0.82');
    expect(sectionDigest({ counts: { total: 49, pass: 36, fail: 5, abstain: 6, scored: 0, error: 2 } })).toBe('判废 5 条，转人工 6 条，出错 2 条');
    expect(sectionDigest({ counts: { total: 8, pass: 8, fail: 0, abstain: 0, scored: 0, error: 0 } })).toBe('8 条全部通过');
    expect(sectionDigest(EEF)).toBe('候选 3 条，出错 1 条');
    // the EEF module's opinion only: its abstentions are episodes without a trajectory, asked of nobody (design doc 22 §5.4)
    const opinion = { counts: { total: 3, pass: 2, fail: 0, abstain: 1, scored: 0, error: 0 }, candidates: 0, judged_pass: 0, judged_reject: 0, to_human: 0 };
    expect(sectionDigest({ ...opinion, opinion_episodes: 3, opinion_flagged: 1, opinion_not_assessable: 1 })).toBe('模型意见 3 条，有不匹配片段 1 条，没有轨迹、没问模型 1 条');
    expect(sectionDigest({ ...opinion, opinion_episodes: 3, opinion_flagged: 0, opinion_not_assessable: 0 })).toBe('模型意见 3 条');
    expect(sectionDigest({})).toBeNull();
  });
});
