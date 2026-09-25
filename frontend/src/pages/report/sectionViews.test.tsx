import { screen, within } from '@testing-library/react';
import { describe, expect, it } from 'vitest';
import type { ReportModuleSection } from '../../api/types';
import { sectionDigest } from '../../lib/sectionStats';
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
  it('has a view for the eight v1 modules, the EEF module and the data integrity module', () => {
    expect(Object.keys(SECTION_VIEWS).sort()).toEqual(
      ['data_integrity', 'dedup', 'eef_video_consistency', 'kinematic_limits', 'motion_quality', 'skill_profile', 'task_success', 'timestamp_check', 'video_action_sync', 'visual_quality'].sort(),
    );
  });

  const cases: [string, Record<string, unknown>][] = [
    ...['timestamp_check', 'kinematic_limits', 'motion_quality', 'visual_quality', 'video_action_sync', 'task_success', 'dedup', 'skill_profile'].map((id) => [id, sampleSummary(id, 200)] as [string, Record<string, unknown>]),
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

  it('digests a section into one line for the scope table', () => {
    expect(sectionDigest(sampleSummary('video_action_sync', 100))).toBe('错位判废 1 条，已标注 6 条');
    expect(sectionDigest(sampleSummary('skill_profile', 100))).toBe('3 个技能族，标注分歧 2 条');
    expect(sectionDigest(sampleSummary('dedup', 100))).toBe('剔除 1 条');
    expect(sectionDigest(sampleSummary('motion_quality', 100))).toBe('平均分 0.82');
    expect(sectionDigest({ counts: { total: 49, pass: 36, fail: 5, abstain: 6, scored: 0, error: 2 } })).toBe('判废 5 条，转人工 6 条，出错 2 条');
    expect(sectionDigest({ counts: { total: 8, pass: 8, fail: 0, abstain: 0, scored: 0, error: 0 } })).toBe('8 条全部通过');
    expect(sectionDigest(EEF)).toBe('候选 3 条，出错 1 条');
    expect(sectionDigest({})).toBeNull();
  });
});
