import { Result, Spin } from '@arco-design/web-react';
import { useQuery } from '@tanstack/react-query';
import { useMemo, useState } from 'react';
import { Navigate, useLocation } from 'react-router-dom';
import { api, unwrap } from '../../api/client';
import { qk, useBackends, useCredentials, useModules, usePublicCatalog, useTask } from '../../api/queries';
import type { Incompatibility } from '../../api/types';
import { PageError } from '../../components/PageError';
import { PageHeader } from '../../components/PageHeader';
import { planDeepLink } from '../../lib/deeplink';
import { readPrefs } from '../../lib/prefs';
import { zh } from '../../locales/zh';
import { DEEP_LINK_PARAMS } from '../../app/routes';
import { buildInitial } from './initial';
import { TaskForm, type FormMode } from './TaskForm';

declare global {
  interface Window {
    /** Optional site feature flags injected by the Daemon next to __CURATOR_BASE__ (README). */
    __CURATOR_FEATURES__?: { local_input?: boolean; home_region?: string };
  }
}

/**
 * /tasks/new — also 编辑待启动任务 (?edit=id), 复制为新任务 (?copy=id), 从数据集新建
 * (?dataset_id=), and v1 deep links (dataset / dataset_url / url / region / endpoint / source).
 * The URL is read once: later URL changes (e.g. ?edit= after saving) never re-initialise the form.
 */
export function TaskFormPage() {
  const location = useLocation();
  const [entry] = useState(() => ({ search: new URLSearchParams(location.search), state: location.state as { incompatibilities?: Incompatibility[] } | null }));
  const params = entry.search;
  const editId = params.get('edit');
  const copyId = params.get('copy');
  const datasetId = params.get('dataset_id');
  const deepLink = DEEP_LINK_PARAMS.some((k) => params.has(k));
  const mode: FormMode = editId ? 'edit' : copyId ? 'copy' : 'new';

  const registry = useModules();
  const credentials = useCredentials();
  const backends = useBackends();
  const publicCatalog = usePublicCatalog();
  const task = useTask(editId ?? copyId ?? undefined);
  const registered = useQuery({
    queryKey: qk.datasets({ page: 1, page_size: 100 }),
    queryFn: () => unwrap(api().GET('/datasets', { params: { query: { page: 1, page_size: 100 } } })),
  });
  const dataset = useQuery({
    queryKey: qk.dataset(datasetId ?? ''),
    queryFn: () => unwrap(api().GET('/datasets/{id}', { params: { path: { id: datasetId! } } })),
    enabled: Boolean(datasetId),
  });
  const plan = useMemo(() => {
    if (!deepLink || !registered.data || publicCatalog.isLoading) return null;
    return planDeepLink(params, {
      registered: registered.data.items,
      publicDatasets: publicCatalog.data ?? null,
      homeRegion: window.__CURATOR_FEATURES__?.home_region ?? null,
    });
  }, [deepLink, registered.data, publicCatalog.isLoading, publicCatalog.data, params]);
  const planDatasetId = plan && plan.datasets.length === 1 ? plan.datasets[0].datasetId : undefined;
  const planDataset = useQuery({
    queryKey: qk.dataset(planDatasetId ?? ''),
    queryFn: () => unwrap(api().GET('/datasets/{id}', { params: { path: { id: planDatasetId! } } })),
    enabled: Boolean(planDatasetId),
  });

  const loading =
    registry.isLoading ||
    credentials.isLoading ||
    backends.isLoading ||
    publicCatalog.isLoading ||
    registered.isLoading ||
    (Boolean(editId ?? copyId) && task.isLoading) ||
    (Boolean(datasetId) && dataset.isLoading) ||
    (deepLink && !plan) ||
    (Boolean(planDatasetId) && planDataset.isLoading);

  const [init, setInit] = useState<ReturnType<typeof buildInitial> | null>(null);
  const failed = registry.error ?? credentials.error ?? backends.error ?? task.error ?? dataset.error;
  if (!init && !loading && !failed) {
    // Computed exactly once, when everything the defaults depend on has arrived.
    queueMicrotask(() =>
      setInit(
        (cur) =>
          cur ??
          buildInitial({
            mode,
            task: task.data ?? null,
            dataset: dataset.data ?? null,
            plan,
            planDataset: planDataset.data ?? null,
            registry: registry.data,
            credentials: credentials.data?.items ?? [],
            backends: backends.data?.items ?? [],
            prefs: readPrefs(),
          }),
      ),
    );
  }

  const crumb = mode === 'edit' ? zh.taskForm.titleEdit : mode === 'copy' ? zh.taskForm.titleCopy : zh.taskForm.crumbNew;
  const header = <PageHeader crumbs={[{ label: zh.taskList.title, to: '/tasks' }, { label: crumb }]} title="" docTitle={crumb} />;

  if (failed) {
    return (
      <>
        {header}
        <PageError error={failed} />
      </>
    );
  }
  if (mode === 'edit' && task.data && task.data.state !== 'created') {
    return <Navigate to={`/tasks/${task.data.id}`} replace state={{ notice: zh.taskForm.notEditable }} />;
  }
  if (!init) {
    return (
      <>
        {header}
        <Spin style={{ display: 'block', margin: '80px auto' }} />
      </>
    );
  }
  if (!registry.data) return <Result status="error" title={zh.errors.pageTitle} />;
  return (
    <>
      {header}
      <TaskForm
        mode={mode}
        initial={init.values}
        linkNotes={init.linkNotes}
        batch={init.batch}
        editTask={mode === 'edit' ? task.data ?? null : null}
        keepSelection={init.keepSelection}
        incompatibilities={entry.state?.incompatibilities ?? []}
        registry={registry.data}
        credentials={credentials.data?.items ?? []}
        backends={backends.data?.items ?? []}
        registered={registered.data?.items ?? []}
        publicCatalog={publicCatalog.data ?? null}
        localEnabled={Boolean(window.__CURATOR_FEATURES__?.local_input)}
      />
    </>
  );
}
