// Initial values of the new-task form for each way of arriving (07 §2.1, §3): a plain new task,
// editing a created task, 复制为新任务, 「新建质检任务」 from a dataset, or a v1 deep link.
import type { Credential, DatasetDetail, ModuleRegistry, Task, VlmBackend } from '../../api/types';
import { canonicalTosUri, type DeepLinkPlan, type PlannedDataset, type RegisteredDataset } from '../../lib/deeplink';
import type { Prefs } from '../../lib/prefs';
import { zh } from '../../locales/zh';
import { defaultValues, fromTask, type FormValues } from './formModel';

export interface InitArgs {
  mode: 'new' | 'edit' | 'copy';
  task: Task | null;
  dataset: DatasetDetail | null;
  plan: DeepLinkPlan | null;
  /** Detail of the registered dataset a deep link matched (for its access key). */
  planDataset: DatasetDetail | null;
  registry: ModuleRegistry | undefined;
  credentials: Credential[];
  backends: VlmBackend[];
  prefs: Prefs;
  /** Registered datasets, to tell whether a deep-linked address is one of them. */
  registered?: readonly RegisteredDataset[];
}

export interface Init {
  values: FormValues;
  linkNotes: { dataset: string[]; region: string[] };
  batch: PlannedDataset[] | null;
  keepSelection: boolean;
  /** Deep-linked datasets not registered yet: the page says creating the task registers them. */
  unregistered: number;
}

/** 07 §2.1: exactly one → it; several → the one used last time; none → nothing (去添加). */
export function pickDefault(names: string[], last: string | undefined): string {
  if (names.length === 1) return names[0];
  if (last && names.includes(last)) return last;
  return '';
}

export function buildInitial(a: InitArgs): Init {
  let v = defaultValues();
  let notes = { dataset: [] as string[], region: [] as string[] };
  let batch: PlannedDataset[] | null = null;
  let keepSelection = false;
  let unregistered = 0;
  let registrationRegion: string | null = null;

  if (a.task) {
    v = fromTask(a.task, a.registry);
    keepSelection = true;
    if (a.mode === 'copy') {
      v.name = `${a.task.name}${zh.taskForm.copySuffix}`.slice(0, 128);
      v.outputUri = a.task.output.uri;
    }
  } else if (a.dataset) {
    v.source = a.dataset.source;
    if (a.dataset.source === 'public') v.publicUri = a.dataset.uri;
    else v.datasetUri = a.dataset.uri;
    v.datasetId = a.dataset.id;
    v.region = a.dataset.region ?? '';
    v.credential = a.dataset.credential ?? '';
    registrationRegion = a.dataset.region;
  } else if (a.plan && a.plan.present) {
    const plan = a.plan;
    notes = { dataset: [...plan.notes.dataset], region: [...plan.notes.region] };
    if (plan.source === 'public') {
      v.source = 'public';
      if (plan.datasets.length === 1) v.publicUri = plan.datasets[0].uri;
      else if (plan.datasets.length > 1) batch = plan.datasets;
    } else if (plan.datasets.length === 1) {
      const d = plan.datasets[0];
      v.datasetUri = d.uri;
      if (d.datasetId && a.planDataset) {
        v.datasetId = d.datasetId;
        v.credential = a.planDataset.credential ?? '';
        registrationRegion = a.planDataset.region;
      }
    } else if (plan.datasets.length > 1) {
      batch = plan.datasets;
    } else if (plan.rootOnly) {
      v.datasetUri = `${plan.rootOnly}/`;
    }
    if (plan.region) v.region = plan.region;
    // A TOS address is matched while planning; a cache-bucket one is looked up here.
    const known = (d: PlannedDataset) =>
      Boolean(d.datasetId) || (plan.source === 'public' && (a.registered ?? []).some((r) => r.source === 'public' && canonicalTosUri(r.uri) === canonicalTosUri(d.uri)));
    unregistered = plan.datasets.filter((d) => !known(d)).length;
  }

  // The default key (C4 1.17) unless the task or dataset brings its own; without one, 07 §2.1.
  const keyNames = a.credentials.map((c) => c.name);
  const defaultKey = a.credentials.find((c) => c.is_default)?.name;
  if (v.source !== 'public' && !v.credential) v.credential = defaultKey ?? pickDefault(keyNames, a.prefs.lastCredential);
  if (!v.region && v.source !== 'public') {
    const keyRegion = a.credentials.find((c) => c.name === v.credential)?.meta.region;
    v.region = registrationRegion ?? keyRegion ?? a.prefs.lastRegion ?? 'cn-beijing';
  }
  // A HuggingFace cache bucket dataset has no key and no region of the user's: the delivery ones
  // are left blank to be picked (fourth round), not filled from the last task.
  if (!v.vlmBackend) {
    // C4 1.6.0: the default model, whatever was used last; without one, 07 §2.1 decides.
    const owner = a.backends.find((b) => b.models.some((m) => m.is_default));
    if (owner) {
      v.vlmBackend = owner.name;
      if (!v.vlmModel) v.vlmModel = owner.models.find((m) => m.is_default)!.model_name;
    } else {
      v.vlmBackend = pickDefault(
        a.backends.map((b) => b.name),
        a.prefs.lastBackend,
      );
      const b = a.backends.find((x) => x.name === v.vlmBackend);
      if (b && !v.vlmModel) v.vlmModel = pickDefault(b.models.map((m) => m.model_name), a.prefs.lastModel);
    }
  }
  return { values: v, linkNotes: notes, batch, keepSelection, unregistered };
}
