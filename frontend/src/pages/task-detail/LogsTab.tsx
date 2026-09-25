import { Button, Card, Select, Space, Switch } from '@arco-design/web-react';
import { useInfiniteQuery, useQuery } from '@tanstack/react-query';
import { useEffect, useMemo, useRef, useState } from 'react';
import { api, unwrap } from '../../api/client';
import { EVENTS_CONFIG, liveLogsKey, type EventMode, type LiveLog } from '../../api/events';
import { qk } from '../../api/queries';
import type { LogLine, Subtask, Task } from '../../api/types';
import { PageError } from '../../components/PageError';
import { absoluteTime } from '../../lib/format';
import { readPrefs, writePrefs } from '../../lib/prefs';
import { stageLabel } from '../../lib/taskView';
import { zh } from '../../locales/zh';

const RANK: Record<string, number> = { error: 0, warn: 1, info: 2, debug: 3 };

/**
 * 日志 (07 §4.2, 03 §11): the command-line text as is (English), filtered by stage, run and
 * level. The first page is the newest; 「加载更早的日志」 walks back with the cursor. While
 * following, SSE lines are appended live, or the newest page is re-read every 5 s without SSE.
 */
export function LogsTab({ task, subtasks, mode, live }: { task: Task; subtasks: Subtask[]; mode: EventMode; live: boolean }) {
  const [stage, setStage] = useState('');
  const [scope, setScope] = useState('all');
  const [level, setLevel] = useState('');
  const [follow, setFollow] = useState(readPrefs().followLogs ?? true);
  const box = useRef<HTMLDivElement | null>(null);
  const params = { stage, scope, level };
  const q = useInfiniteQuery({
    queryKey: qk.logs(task.id, params),
    queryFn: ({ pageParam }) =>
      unwrap(
        api().GET('/tasks/{id}/logs', {
          params: {
            path: { id: task.id },
            query: {
              limit: 200,
              ...(stage ? { stage } : {}),
              ...(scope !== 'all' ? { subtask: scope } : {}),
              ...(level ? { level: level as 'error' | 'warn' } : {}),
              ...(pageParam ? { cursor: pageParam } : {}),
            },
          },
        }),
      ),
    initialPageParam: '' as string,
    getNextPageParam: (last) => (last.has_more && last.next_cursor ? last.next_cursor : undefined),
    refetchInterval: follow && live && mode !== 'sse' ? EVENTS_CONFIG.pollMs : false,
  });
  const liveLogs = useQuery<LiveLog[]>({ queryKey: liveLogsKey(task.id), queryFn: () => [], staleTime: Infinity, initialData: [] });

  const lines: LogLine[] = useMemo(() => {
    const stored = [...(q.data?.pages ?? [])].reverse().flatMap((p) => p.items ?? []);
    if (!follow || scope !== 'all') return stored;
    const seen = new Set(stored.map((l) => `${l.stage}|${l.msg}`));
    const extra = (liveLogs.data ?? []).filter((l) => (!stage || l.stage === stage) && (!level || RANK[l.level] <= RANK[level]) && !seen.has(`${l.stage}|${l.msg}`));
    return [...stored, ...extra];
  }, [q.data, liveLogs.data, follow, scope, stage, level]);

  useEffect(() => {
    if (follow && box.current) box.current.scrollTop = box.current.scrollHeight;
  }, [lines.length, follow]);

  const stageIds = [...new Set([...task.progress.stages.map((s) => s.id), 'system'])];
  return (
    <Card>
      <Space wrap style={{ marginBottom: 12 }}>
        <Select
          style={{ width: 160 }}
          value={stage}
          onChange={setStage}
          aria-label={zh.taskDetail.logsStage}
          options={[{ label: zh.taskDetail.logsAllStages, value: '' }, ...stageIds.map((s) => ({ label: s === 'system' ? zh.taskDetail.logsSystem : stageLabel(s), value: s }))]}
        />
        <Select
          style={{ width: 180 }}
          value={scope}
          onChange={setScope}
          aria-label={zh.taskDetail.logsScope}
          options={[
            { label: zh.taskDetail.logsAllScopes, value: 'all' },
            { label: zh.taskDetail.logsMain, value: '' },
            ...subtasks.map((s, i) => ({ label: `${zh.taskDetail.subtaskKind[s.kind] ?? s.kind} #${subtasks.slice(0, i + 1).filter((x) => x.kind === s.kind).length}`, value: s.id })),
          ]}
        />
        <Select
          style={{ width: 140 }}
          value={level}
          onChange={setLevel}
          aria-label={zh.taskDetail.logsLevel}
          options={[
            { label: zh.taskDetail.logsAll, value: '' },
            { label: zh.taskDetail.logsWarn, value: 'warn' },
            { label: zh.taskDetail.logsError, value: 'error' },
          ]}
        />
        <Space>
          <Switch
            checked={follow}
            onChange={(v) => {
              setFollow(v);
              writePrefs({ followLogs: v });
            }}
            aria-label={zh.taskDetail.logsFollow}
          />
          {zh.taskDetail.logsFollow}
        </Space>
      </Space>
      {q.isError && !q.data ? (
        <PageError error={q.error} onRetry={() => void q.refetch()} />
      ) : (
        <div className="log-view" ref={box} data-testid="log-view" aria-live="polite">
          {q.hasNextPage ? (
            <Button size="mini" type="text" loading={q.isFetchingNextPage} onClick={() => void q.fetchNextPage()} style={{ color: '#c9cdd4' }}>
              {zh.taskDetail.logsOlder}
            </Button>
          ) : q.data ? (
            <div className="log-line debug">{zh.taskDetail.logsNoMore}</div>
          ) : null}
          {!lines.length && !q.isLoading ? <div className="log-line debug">{zh.taskDetail.logsEmpty}</div> : null}
          {lines.map((l, i) => (
            <div key={`${l.ts}-${i}`} className={`log-line ${l.level}`}>
              {absoluteTime(l.ts).slice(11)} [{l.stage}] {l.level.toUpperCase()} {l.msg}
            </div>
          ))}
        </div>
      )}
    </Card>
  );
}
