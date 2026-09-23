"""A single-consumer v1 funnel using the unchanged per-episode check bodies."""
from __future__ import annotations

import json

from .config import KNOWN_CHECKS, enabled
from .execution import _to_thread

_INTERNAL = '_curation_execution'


def _build_funnel_chain(df, cfg, registry, vlm_completion, executor_key=None, cache_key=None):
    import daft
    from daft import col
    from daft.functions import to_struct
    from . import funnel as f
    from functools import partial
    from .frame_cache import _decode_cached
    decode = partial(_decode_cached, cache_key) if cache_key else None

    if _INTERNAL in df.column_names:
        raise ValueError(f'Reserved internal column: {_INTERNAL}')
    columns = {field.name: field.dtype for field in df.schema()}
    numeric = []
    if enabled(cfg, 'timestamp_check'):
        numeric.append(('timestamp_check', f.make_timestamp_check(cfg)))
    if enabled(cfg, 'kinematic_limits'):
        numeric.append(('kinematic_limits', f.make_kinematic_check(cfg, registry)))
    if enabled(cfg, 'motion_quality'):
        numeric.append(('motion_quality', f.make_motion_check(cfg, registry)))
    for name, _ in numeric:
        columns['check_' + name] = f._result_dtype()
    visual, sync, plot_mode = f.frame_modes(cfg)
    frame = f.make_frame_checks(cfg, registry, decode=decode) if visual or sync else None
    if visual:
        columns['check_visual_quality'] = f._result_dtype()
    if sync:
        columns['check_video_action_sync'] = f._result_dtype()
        if plot_mode != 'off':
            columns['_sync_curves'] = daft.DataType.string()
    deps = None
    if enabled(cfg, 'task_success') and vlm_completion is not None:
        try:
            voter = f.build_endstate_voter(cfg)
        except Exception as e:
            voter = None
            print(f'[curation] review unavailable: {type(e).__name__}: {e}', flush=True)
        try:
            arb = f.build_arbitration_deps(cfg)
        except Exception as e:
            arb = None
            print(f'[curation] arbitration unavailable: {type(e).__name__}: {e}', flush=True)
        deps = f.TaskDeps(vlm_completion, voter, arb, decode=decode)
        columns['check_task_success'] = f._result_dtype()
    columns['verdict'] = daft.DataType.string()
    result_columns = {k: v for k, v in columns.items() if k not in df.column_names}
    # Also support callers whose input already contains check/verdict columns.
    result_columns.update({k: v for k, v in columns.items() if k.startswith('check_') or k == 'verdict'})
    result_columns['_killed_by'] = daft.DataType.string()
    concurrency = int(cfg.get('pipeline', {}).get('vlm_episode_concurrency', 8))

    def execute(row):
        out = {key: row.get(key) for key in result_columns}
        out['_killed_by'] = ''
        for name, check in numeric:
            if name == 'timestamp_check':
                args = (row['timestamps'], row['fps'])
            elif name == 'kinematic_limits':
                args = (row['action'], row['embodiment_id'], row['fps'],
                        row.get('action_space', 'joint'), row.get('control_mode', 'unknown'),
                        row['proprio_state'], row.get('proprio_space', 'joint'))
            else:
                args = (row['action'], row['proprio_state'], row['fps'],
                        row.get('action_space', 'joint'), row.get('control_mode', 'unknown'),
                        row.get('proprio_space', 'joint'), row['embodiment_id'],
                        row.get('stuck_strategy', 'auto'), row.get('semantics_extras', '{}'))
            out['check_' + name] = check(*args)
        # Legacy computes every numeric check before filtering gates in this order.
        for name in ('timestamp_check', 'kinematic_limits'):
            value = out.get('check_' + name)
            if (enabled(cfg, name) and value is not None
                    and value['passed'] is not None and not value['passed']):
                out['_killed_by'] = name
                return out
        if frame is not None:
            result = frame(row['video'], row['proprio_state'], row['timestamps'], row['fps'],
                           row.get('proprio_space', 'joint'), row['embodiment_id'])
            if visual:
                out['check_visual_quality'] = result['visual']
            if sync:
                out['check_video_action_sync'] = result['sync']
                if plot_mode != 'off':
                    out['_sync_curves'] = result['curves']
                if (result['sync'] is not None and result['sync']['passed'] is not None
                        and not result['sync']['passed']):
                    out['_killed_by'] = 'video_action_sync'
                    return out
        if deps is not None:
            try:
                out['check_task_success'] = f.task_check_episode(
                    cfg, registry, deps, row['video'], row.get('task_desc', row.get('instruction')),
                    row.get('task_desc_source', '原始标注'), row['fps'], row['action'],
                    row['timestamps'], row['embodiment_id'], row.get('semantics_extras', '{}'))
            except Exception as e:
                out['check_task_success'] = f.internal_error_struct(e)
        checks = {name: dict(out['check_' + name]) for name in KNOWN_CHECKS
                  if out.get('check_' + name) is not None}
        # Legacy passes through a typed Daft struct before verdict evaluation.
        for check in checks.values():
            if check.get('passed') is not None:
                check['passed'] = bool(check['passed'])
            if check.get('score') is not None:
                check['score'] = float(check['score'])
        out['verdict'] = json.dumps(f.episode_verdict(checks, cfg), ensure_ascii=False)
        return out

    @daft.func(return_dtype=daft.DataType.struct(result_columns))
    async def evaluate(row):
        async with f._episode_gate(concurrency):
            return await _to_thread(executor_key, execute, row)

    chain = df.with_column(_INTERNAL, evaluate(to_struct(*[col(c) for c in df.column_names])))
    return chain, columns, deps is not None and deps.arb_deps is not None


def _materialize(rows, columns):
    import daft
    # Explicit series dtypes retain tensors, nullability and the empty-result schema.
    return daft.from_pydict({name: daft.Series.from_pylist(
        [row.get(name) for row in rows], name=name, dtype=dtype)
        for name, dtype in columns.items()})


def _execute_funnel(df, cfg, registry, vlm_completion, executor_key=None, *, sink=None,
                    checkpoint=None, order=None, cache_key=None):
    from .funnel import arbitration_stats
    restored = []
    if checkpoint is not None:
        restored = [record for (kind, eid), record in checkpoint.records.items()
                    if kind == 'episode']
        if restored:
            from daft import col
            df = df.filter(~col('episode_id').is_in([r['row']['episode_id'] for r in restored]))
    chain, columns, has_arb = _build_funnel_chain(df, cfg, registry, vlm_completion,
                                                executor_key, cache_key)
    stats = {'input': 0, 'hard_killed': [], 'after_numeric_gates': 0,
             'survivors_for_vlm': 0}
    rows, killed = [], {'timestamp_check': [], 'kinematic_limits': [], 'video_action_sync': []}
    completed = list(restored)
    for index, row in enumerate(chain.iter_rows(results_buffer_size='num_cpus')):
        result = row.pop(_INTERNAL)
        gate = result.pop('_killed_by')
        row.update(result)
        if sink is not None:
            sink(index, row, gate)
        record = {'index': index, 'row': row, 'gate': gate}
        if checkpoint is not None:
            checkpoint.append('episode', row['episode_id'], record)
        completed.append(record)
    if order is not None:
        positions = {eid: index for index, eid in enumerate(order)}
        completed.sort(key=lambda item: positions[item['row']['episode_id']])
    for item in completed:
        row, gate = item['row'], item['gate']
        stats['input'] += 1
        if gate not in ('timestamp_check', 'kinematic_limits'):
            stats['after_numeric_gates'] += 1
        if gate:
            killed[gate].append({'episode_id': row['episode_id'], 'check': gate,
                                 'detail': (row.get('check_' + gate) or {}).get('detail', '')})
        else:
            stats['survivors_for_vlm'] += 1
            rows.append(row)
    # Legacy groups killed rows by gate, then preserves input order within each gate.
    stats['hard_killed'] = [item for group in killed.values() for item in group]
    if has_arb:
        stats['arbitration'] = arbitration_stats(row['check_task_success'] for row in rows)
    stats['output'] = len(rows)
    return _materialize(rows, columns), stats
