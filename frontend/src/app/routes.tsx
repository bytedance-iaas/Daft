import { lazy } from 'react';
import { Navigate, Route, Routes, useLocation } from 'react-router-dom';
import { AppLayout } from '../components/AppLayout';
import { DEEPLINK_KEYS, ENDPOINT_KEYS, REGION_KEY } from '../lib/deeplink';

const named = <K extends string>(load: () => Promise<Record<K, React.ComponentType>>, name: K) =>
  lazy(async () => ({ default: (await load())[name] }));

const OverviewPage = named(() => import('../pages/overview/OverviewPage'), 'OverviewPage');
const DatasetListPage = named(() => import('../pages/datasets/DatasetListPage'), 'DatasetListPage');
const DatasetDetailPage = named(() => import('../pages/datasets/DatasetDetailPage'), 'DatasetDetailPage');
const TaskListPage = named(() => import('../pages/tasks/TaskListPage'), 'TaskListPage');
const TaskFormPage = named(() => import('../pages/task-form/TaskFormPage'), 'TaskFormPage');
const TaskDetailPage = named(() => import('../pages/task-detail/TaskDetailPage'), 'TaskDetailPage');
const ReportPage = named(() => import('../pages/report/ReportPage'), 'ReportPage');
const AdjudicationPage = named(() => import('../pages/adjudication/AdjudicationPage'), 'AdjudicationPage');
const ReportListPage = named(() => import('../pages/results/ResultListPage'), 'ReportListPage');
const AdjudicationListPage = named(() => import('../pages/results/ResultListPage'), 'AdjudicationListPage');
const KeysPage = named(() => import('../pages/keys/KeysPage'), 'KeysPage');
const NotFoundPage = named(() => import('../pages/NotFoundPage'), 'NotFoundPage');

/** Every query key v1's deep links may carry (doc 07 §2.1). */
export const DEEP_LINK_PARAMS: readonly string[] = [...DEEPLINK_KEYS, REGION_KEY, ...ENDPOINT_KEYS, 'source'];

/** `/` with deep-link parameters is v1's old entry: go to /tasks/new with the query untouched. */
export function RootRedirect() {
  const { search } = useLocation();
  const params = new URLSearchParams(search);
  const deepLink = DEEP_LINK_PARAMS.some((k) => params.has(k));
  return <Navigate to={deepLink ? `/tasks/new${search}` : '/overview'} replace />;
}

export function AppRoutes() {
  return (
    <Routes>
      <Route element={<AppLayout />}>
        <Route index element={<RootRedirect />} />
        <Route path="overview" element={<OverviewPage />} />
        <Route path="datasets" element={<DatasetListPage />} />
        <Route path="datasets/:id" element={<DatasetDetailPage />} />
        <Route path="tasks" element={<TaskListPage />} />
        <Route path="tasks/new" element={<TaskFormPage />} />
        <Route path="tasks/:id" element={<TaskDetailPage />} />
        <Route path="tasks/:id/report" element={<ReportPage />} />
        <Route path="tasks/:id/adjudication" element={<AdjudicationPage />} />
        <Route path="reports" element={<ReportListPage />} />
        <Route path="adjudication" element={<AdjudicationListPage />} />
        <Route path="credentials" element={<KeysPage />} />
        <Route path="*" element={<NotFoundPage />} />
      </Route>
    </Routes>
  );
}
