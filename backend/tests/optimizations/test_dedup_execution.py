import threading
import time

from curation.pipeline.dedup_execution import _ordered_fingerprints


def test_parallel_completion_preserves_order(monkeypatch):
    from curation.dataset_level import dedup
    barrier = threading.Barrier(4)
    completed = []
    def fingerprint(row):
        i = row['episode_id']
        barrier.wait(timeout=5)
        time.sleep((3 - i) * .01)
        completed.append(i)
        return str(i % 2)
    monkeypatch.setattr(dedup, 'episode_fingerprint', fingerprint)
    result = list(_ordered_fingerprints([{'episode_id': i} for i in range(4)], 4))
    assert completed[0] == 3
    assert [(r['episode_id'], fp) for r, fp in result] == [(0, '0'), (1, '1'), (2, '0'), (3, '1')]


def test_pipeline_dedup_equivalence_and_no_first_pass_reread(tmp_path, monkeypatch):
    from parity.fixtures import make_mini_lerobot
    from curation.pipeline.run import run_pipeline
    from curation.ingest import lerobot_reader
    data = make_mini_lerobot(str(tmp_path / 'input'))
    calls = []
    reports = []
    from curation.export import report
    build = report.build_report
    def capture(*args, **kwargs):
        reports.append(args[2])
        return build(*args, **kwargs)
    monkeypatch.setattr(report, 'build_report', capture)
    read = lerobot_reader.read_lerobot_rows
    def tracked(*args, **kwargs):
        calls.append(kwargs.get('episode_indices'))
        return read(*args, **kwargs)
    monkeypatch.setattr(lerobot_reader, 'read_lerobot_rows', tracked)
    common = dict(lite=True, report_only=True, only_checks='timestamp_check,dedup')
    base = run_pipeline(None, data, str(tmp_path / 'base'), **common)
    base_calls = len(calls)
    calls.clear()
    optimized = run_pipeline(None, data, str(tmp_path / 'optimized'), **common,
                             set_overrides=['pipeline.optimizations.action_hash_reuse=true',
                                            'pipeline.optimizations.parallel_video_hash=true'])
    assert base['verdicts'] == optimized['verdicts']
    assert base['stats'] == optimized['stats']
    assert len(calls) < base_calls
    assert reports[0] == reports[1]
    assert reports[0], 'fixture must exercise duplicate_of'
