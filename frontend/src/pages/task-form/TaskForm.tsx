import { Alert, Button, Form, Message, Notification, Space, Steps, Tag, Tooltip, Typography } from '@arco-design/web-react';
import { useQueryClient } from '@tanstack/react-query';
import { useCallback, useEffect, useMemo, useRef, useState } from 'react';
import { useNavigate } from 'react-router-dom';
import { api, idempotencyKey, unwrap } from '../../api/client';
import { ApiError, errorMessage } from '../../api/errors';
import { qk, showTaskState } from '../../api/queries';
import type {
  BrowsedDataset,
  Credential,
  DatasetItem,
  Incompatibility,
  ModuleAvailability,
  ModuleRegistry,
  PreflightResult,
  SourceChange,
  Task,
  VlmBackend,
} from '../../api/types';
import { AccessKeyDrawer } from '../../features/keys/AccessKeyDrawer';
import { BackendDrawer } from '../../features/keys/BackendDrawer';
import { FingerprintDialog, asSourceChange } from '../../features/tasks/FingerprintDialog';
import { precheckItems } from '../../features/tasks/useTaskActions';
import { borrowedOutputUrl, publicOutputDefault, suggestDeliveryName, type PlannedDataset } from '../../lib/deeplink';
import { selectionCount } from '../../lib/episodes';
import { monthDay } from '../../lib/format';
import { readPrefs, writePrefs } from '../../lib/prefs';
import { presetSelection } from '../../lib/preflight';
import { zh } from '../../locales/zh';
import { BasicSection } from './BasicSection';
import { EpisodeSection, type PreviewSource } from './EpisodeSection';
import {
  activeModules,
  datasetDirName,
  effectiveOutputCredential,
  effectiveOutputRegion,
  inputRef,
  isTosUri,
  preflightRequest,
  selectable,
  taskParams,
  toTaskCreate,
  toTaskPatch,
  usesVlm,
  validateScreen1,
  validateScreen2,
  vlmChoice,
  embodimentOf,
  episodes as episodeSelector,
  moduleChoices,
  type Errors,
  type FormPatch,
  type FormValues,
} from './formModel';
import { AdvancedSection, ModelSection } from './ModelSection';
import { ModuleSection, type AvailabilityMap } from './ModuleSection';
import { ModuleSettings } from './ModuleSettings';
import { PreflightCard } from '../../features/preflight/PreflightCard';
import { runPreflight, useDeliveryProbe, usePreflight, type PreflightState } from '../../features/preflight/usePreflight';

export type FormMode = 'new' | 'edit' | 'copy';

export interface TaskFormProps {
  mode: FormMode;
  initial: FormValues;
  linkNotes: { dataset: string[]; region: string[] };
  batch: PlannedDataset[] | null;
  editTask: Task | null;
  keepSelection: boolean;
  incompatibilities: Incompatibility[];
  registry: ModuleRegistry | undefined;
  credentials: Credential[];
  backends: VlmBackend[];
  registered: DatasetItem[];
  publicCatalog: BrowsedDataset[] | null;
  localEnabled: boolean;
}

const RANK = { unsupported: 3, needs_input: 2, available: 1 } as const;

/** Worst availability per module across several preflights (batch mode). */
function mergeAvailability(results: PreflightResult[]): AvailabilityMap {
  if (!results.length) return null;
  const out: Record<string, ModuleAvailability> = {};
  for (const r of results) {
    for (const m of r.modules) {
      const cur = out[m.id];
      if (!cur || RANK[m.availability] > RANK[cur.availability]) out[m.id] = m;
    }
    if (!r.format.supported) for (const m of r.modules) out[m.id] = m;
  }
  return out;
}

function toMap(r: PreflightResult | null): AvailabilityMap {
  if (!r) return null;
  const out: Record<string, ModuleAvailability> = {};
  for (const m of r.modules) out[m.id] = m;
  if (!r.format.supported) for (const m of r.modules) out[m.id] = { ...m, availability: 'unsupported' };
  return out;
}

interface BatchEntry extends PreflightState {
  dataset: PlannedDataset;
}

function incompatibilityErrors(list: Incompatibility[], v: FormValues): { errors: Errors; marked: Record<string, string>; other: string[] } {
  const errors: Errors = {};
  const marked: Record<string, string> = {};
  const other: string[] = [];
  for (const i of list) {
    if (i.field === 'modules' && i.module) marked[i.module] = i.reason;
    else if (i.field === 'episodes') errors[v.episodeMode === 'head' ? 'headN' : v.episodeMode === 'explicit' ? 'expr' : 'episodes'] = i.reason;
    else if (i.field === 'embodiment_id') errors.embodiment = i.reason;
    else other.push(i.reason);
  }
  return { errors, marked, other };
}

export function TaskForm(p: TaskFormProps) {
  const navigate = useNavigate();
  const qc = useQueryClient();
  const reg = p.registry;
  const [v, setV] = useState<FormValues>(p.initial);
  const touched = useRef(new Set<string>(p.mode === 'new' ? [] : ['outputUri']));
  const [screen, setScreen] = useState<1 | 2>(1);
  const [shown, setShown] = useState<{ 1: boolean; 2: boolean }>({ 1: false, 2: false });
  const initialIncompat = useMemo(() => incompatibilityErrors(p.incompatibilities, p.initial), [p.incompatibilities, p.initial]);
  const [serverErrors, setServerErrors] = useState<Errors>(initialIncompat.errors);
  const [marked, setMarked] = useState<Record<string, string>>(initialIncompat.marked);
  const [incompatNotes, setIncompatNotes] = useState<string[]>(p.incompatibilities.length ? p.incompatibilities.map((i) => i.reason) : []);
  const [taskId, setTaskId] = useState<string | null>(p.mode === 'edit' ? p.editTask?.id ?? null : null);
  const [updatedAt, setUpdatedAt] = useState<number | null>(p.mode === 'edit' ? p.editTask?.updated_at ?? null : null);
  // Files of screen 2 still going up or being checked: nothing is created until they are done
  // (fourth round), `<module>.<param>` -> busy.
  const [uploading, setUploading] = useState<Record<string, boolean>>({});
  const onUploadBusy = useCallback(
    (key: string, busy: boolean) => setUploading((u) => (Boolean(u[key]) === busy ? u : { ...u, [key]: busy })),
    [],
  );
  const uploadBusy = Object.values(uploading).some(Boolean);
  // Where a submit is (the buttons spin meanwhile; no text since the fourth round).
  const [phase, setPhase] = useState<'preflight' | 'register' | 'create' | 'start' | null>(null);
  const [fp, setFp] = useState<{ taskId: string; change: SourceChange | null } | null>(null);
  const [fpBusy, setFpBusy] = useState(false);
  const [outputNote, setOutputNote] = useState('');
  const [addKey, setAddKey] = useState(false);
  const [addBackend, setAddBackend] = useState(false);
  const probe = useDeliveryProbe();
  const keepSelection = useRef(p.keepSelection);

  const latest = useRef(v);
  latest.current = v;
  // A function patch is applied to the state as it is when React applies it: a value written back
  // late (an upload that finished after another one) must not undo what changed meanwhile.
  const set = useCallback((patch: FormPatch, touchedKey?: keyof FormValues) => {
    if (touchedKey) touched.current.add(touchedKey);
    setV((prev) => ({ ...prev, ...(typeof patch === 'function' ? patch(prev) : patch) }));
    setServerErrors((prev) => {
      const keys = Object.keys(typeof patch === 'function' ? patch(latest.current) : patch);
      if (!keys.some((k) => k in prev)) return prev;
      const next = { ...prev };
      for (const k of keys) delete next[k];
      return next;
    });
  }, []);

  // ------------------------------------------------------------ preflight (single and batch)
  const req = useMemo(() => (p.batch ? null : preflightRequest(v)), [p.batch, v]);
  const preflight = usePreflight(req);
  const [batchState, setBatchState] = useState<BatchEntry[]>(() =>
    (p.batch ?? []).map((d) => ({ dataset: d, status: 'idle', result: null, id: null, expiresAt: 0, error: null, key: '' })),
  );
  const batchKey = p.batch ? JSON.stringify([p.batch, v.region, v.credential, v.vlmBackend]) : '';
  useEffect(() => {
    if (!p.batch || (p.batch.some((d) => d.uri.startsWith('tos://')) && (!v.region || !v.credential) && v.source === 'tos')) return undefined;
    let cancelled = false;
    setBatchState((s) => s.map((e) => ({ ...e, status: 'running' })));
    const t = setTimeout(() => {
      void Promise.allSettled(
        p.batch!.map((d) =>
          runPreflight({
            input: d.datasetId ? { dataset_id: d.datasetId } : v.source === 'public' ? { source: 'public', uri: d.uri } : { source: 'tos', uri: d.uri, region: v.region, credential: v.credential },
            ...(v.vlmBackend ? { vlm_backend: v.vlmBackend } : {}),
          }),
        ),
      ).then((results) => {
        if (cancelled) return;
        setBatchState(
          results.map((r, i) =>
            r.status === 'fulfilled'
              ? { dataset: p.batch![i], status: 'ok', result: r.value.result, id: r.value.preflight_id, expiresAt: r.value.expires_at, error: null, key: batchKey }
              : { dataset: p.batch![i], status: 'error', result: null, id: null, expiresAt: 0, error: r.reason, key: batchKey },
          ),
        );
      });
    }, 300);
    return () => {
      cancelled = true;
      clearTimeout(t);
    };
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [batchKey]);

  const result: PreflightResult | null = p.batch ? batchState.find((e) => e.result)?.result ?? null : preflight.current ? preflight.result : preflight.result;
  const availability: AvailabilityMap = p.batch ? mergeAvailability(batchState.filter((e) => e.result).map((e) => e.result!)) : toMap(preflight.result);
  const total = p.batch
    ? batchState.reduce<number | null>((min, e) => (e.result?.dataset ? (min === null ? e.result.dataset.episode_count : Math.min(min, e.result.dataset.episode_count)) : min), null)
    : result?.dataset?.episode_count ?? null;

  // Presets follow a new availability (07 §3); an edited or copied task keeps its selection once.
  const availKey = availability ? JSON.stringify(Object.values(availability).map((a) => [a?.id, a?.availability])) : '';
  useEffect(() => {
    if (!availability || !reg) return;
    const merged = { schema_version: '1.0', format: { kind: 'lerobot', version: 'v2', supported: true, detail: '' }, validation: [], dataset: null, modules: Object.values(availability).filter(Boolean), meta_fingerprint: '', warnings: [] } as unknown as PreflightResult;
    setV((prev) => {
      if (keepSelection.current) {
        keepSelection.current = false;
        return { ...prev, modules: prev.modules.filter((id) => selectable(merged, id)) };
      }
      if (prev.preset === 'custom') return { ...prev, modules: prev.modules.filter((id) => selectable(merged, id)) };
      return { ...prev, modules: presetSelection(prev.preset, reg, merged) };
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [availKey, reg]);

  // ------------------------------------------------------------ delivery directory default
  const publicBuckets = useMemo(() => [...new Set((p.publicCatalog ?? []).map((d) => d.uri.replace(/^tos:\/\//, '').split('/')[0]))], [p.publicCatalog]);
  const dirName = p.batch ? p.batch[0]?.name ?? '' : datasetDirName(v);
  const outCred = effectiveOutputCredential(v);
  const outRegion = effectiveOutputRegion(v);
  useEffect(() => {
    if (touched.current.has('outputUri') || !dirName) return;
    const lastRoot = readPrefs().lastDeliveryRoot;
    const finish = (root: string) => (p.batch ? root : `${root}/${suggestDeliveryName(dirName, monthDay())}`);
    if (v.source === 'public') {
      const [root, note] = publicOutputDefault(lastRoot);
      setOutputNote(note);
      if (root) setV((prev) => ({ ...prev, outputUri: finish(root) }));
      return;
    }
    const uri = p.batch ? p.batch[0]?.uri ?? '' : v.datasetUri;
    const root = borrowedOutputUrl(uri, lastRoot, publicBuckets);
    if (!root) return;
    if (lastRoot) {
      setOutputNote('');
      setV((prev) => ({ ...prev, outputUri: finish(root) }));
      return;
    }
    // Borrowing the dataset's bucket needs a write probe first: a read-only bucket is not lent.
    if (!outCred) return;
    const target = finish(root);
    void probe.probe(target, outRegion, outCred).then((res) => {
      if (touched.current.has('outputUri')) return;
      if (res?.ok) {
        setOutputNote('');
        setV((prev) => ({ ...prev, outputUri: target }));
      } else {
        setV((prev) => ({ ...prev, outputUri: '' }));
        setOutputNote(zh.deeplink.borrowFailed(res?.error?.message ?? ''));
      }
    });
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [dirName, v.source, outCred]);

  const onOutputBlur = () => {
    const uri = v.outputUri.trim();
    if (isTosUri(uri) && outCred && !p.batch) {
      setOutputNote('');
      void probe.probe(uri, outRegion, outCred);
    }
  };
  // Re-probe when the key or region of an already probed directory changes.
  useEffect(() => {
    const uri = v.outputUri.trim();
    if (probe.key && isTosUri(uri) && outCred && probe.key !== `${uri}|${outRegion}|${outCred}` && touched.current.has('outputUri')) void probe.probe(uri, outRegion, outCred);
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [outCred, outRegion]);

  // ------------------------------------------------------------ validation
  const ctx = { registry: reg, preflight: result, batch: Boolean(p.batch) };
  const errors1 = shown[1] ? validateScreen1(v, ctx) : {};
  const errors2 = shown[2] ? validateScreen2(v, ctx) : {};
  const errors: Errors = { ...errors1, ...errors2, ...serverErrors };
  const vlm = usesVlm(v, reg);
  const count = selectionCount(v.episodeMode, v.headN, v.expr, total);
  const footer = zh.taskForm.footerSummary(count === null ? '?' : String(count), activeModules(v).length);

  const preview: PreviewSource | null = p.batch
    ? null
    : v.datasetId
      ? { dataset_id: v.datasetId }
      : req && 'source' in req.input
        ? (req.input as PreviewSource)
        : null;

  const preflightReady = (): string | null => {
    if (p.batch) {
      if (batchState.some((e) => e.status !== 'ok')) return batchState.some((e) => e.status === 'error') ? zh.taskForm.batchPreflightFailed : zh.taskForm.preflightRunning;
      if (batchState.some((e) => e.result && !e.result.format.supported)) return zh.taskForm.preflightUnsupported;
      return null;
    }
    if (preflight.status === 'error') return errorMessage(preflight.error);
    if (preflight.status !== 'ok' || !preflight.result) return zh.taskForm.preflightIdle;
    if (!preflight.result.format.supported) return zh.taskForm.preflightUnsupported;
    return null;
  };

  const next = () => {
    setShown((s) => ({ ...s, 1: true }));
    const e = validateScreen1(v, ctx);
    const pfProblem = preflightReady();
    if (Object.keys(e).length || pfProblem) {
      if (pfProblem && !e.datasetUri && !e.publicUri) setServerErrors((prev) => ({ ...prev, [v.source === 'public' ? 'publicUri' : 'datasetUri']: pfProblem }));
      Message.error(zh.taskForm.leaveFirstScreen);
      return;
    }
    setScreen(2);
    window.scrollTo(0, 0);
  };

  const savePrefs = () => {
    const root = v.outputUri.trim().replace(/\/+$/, '');
    writePrefs({
      ...(v.credential ? { lastCredential: v.credential } : {}),
      ...(outCred ? { lastOutputCredential: outCred } : {}),
      ...(v.vlmBackend ? { lastBackend: v.vlmBackend, lastModel: v.vlmModel } : {}),
      ...(v.region ? { lastRegion: v.region } : {}),
      lastDeliveryRoot: p.batch ? root : root.slice(0, root.lastIndexOf('/')) || root,
    });
  };

  const applyPrecheck = (err: ApiError) => {
    const field: Record<string, string> = { input: v.source === 'public' ? 'publicUri' : 'datasetUri', output: 'outputUri', vlm: 'vlmBackend' };
    const next: Errors = {};
    for (const item of precheckItems(err.details)) if (!item.ok && field[item.check]) next[field[item.check]] = item.reason ?? err.message;
    if (!Object.keys(next).length) next[field.input] = err.message;
    setServerErrors((prev) => ({ ...prev, ...next }));
    setScreen(1);
  };

  const handleIncompatible = (list: Incompatibility[]) => {
    const r = incompatibilityErrors(list, v);
    setServerErrors((prev) => ({ ...prev, ...r.errors }));
    setMarked(r.marked);
    setIncompatNotes(list.map((i) => i.reason));
    setScreen(list.some((i) => i.field === 'embodiment_id') && !list.some((i) => i.field !== 'embodiment_id') ? 2 : 1);
    preflight.rerun();
  };

  const startTask = async (id: string, warnings: string[]) => {
    setPhase('start');
    try {
      const task = await unwrap(api().POST('/tasks/{id}/actions/{action}', { params: { path: { id, action: 'start' }, header: { 'Idempotency-Key': idempotencyKey() } } }));
      showTaskState(qc, task);
      Message.success(zh.taskForm.started);
      warnings.forEach((w) => Notification.warning({ title: zh.taskForm.titleNew, content: w }));
      void qc.invalidateQueries({ queryKey: qk.tasksAll });
      navigate(`/tasks/${id}`);
    } catch (e) {
      if (e instanceof ApiError && e.code === 'source_changed') setFp({ taskId: id, change: asSourceChange(e.details) });
      else if (e instanceof ApiError && e.code === 'precheck_failed') {
        applyPrecheck(e);
        Message.warning(zh.taskForm.precheckStay);
      } else Message.error(errorMessage(e));
    }
  };

  const fieldFromDetails = (e: ApiError): string | null => {
    // a module parameter the Daemon refuses (`modules.<id>.params.<key>`): its field on screen 2
    const located = ((e.details?.errors as { field?: unknown }[] | undefined) ?? []).map((x) => x.field);
    const param = [e.details?.field, ...located].map((x) => /^modules\.([^.]+)\.params\.(.+)$/.exec(String(x ?? ''))).find(Boolean);
    if (param) return `params.${param[1]}.${param[2]}`;
    const f = e.details?.field;
    if (f === 'episodes') return v.episodeMode === 'head' ? 'headN' : 'expr';
    if (f === 'embodiment_id') return 'embodiment';
    if (f === 'vlm') return 'vlmBackend';
    if (f === 'modules') return 'modules';
    if (f === 'output') return 'outputUri';
    if (f === 'input') return 'datasetUri';
    return null;
  };

  const submitBatch = async (start: boolean) => {
    const items = [];
    for (const e of batchState) {
      let pfId = e.id;
      if (!pfId || e.expiresAt - Date.now() < 60_000) {
        const res = await runPreflight({ input: e.dataset.datasetId ? { dataset_id: e.dataset.datasetId } : v.source === 'public' ? { source: 'public', uri: e.dataset.uri } : { source: 'tos', uri: e.dataset.uri, region: v.region, credential: v.credential } });
        pfId = res.preflight_id;
      }
      let datasetId = e.dataset.datasetId ?? null;
      if (!datasetId) {
        setPhase('register');
        const d = await unwrap(
          api().POST('/datasets', {
            params: { header: { 'Idempotency-Key': idempotencyKey() } },
            body: { input: v.source === 'public' ? { source: 'public', uri: e.dataset.uri } : { source: 'tos', uri: e.dataset.uri, region: v.region, credential: v.credential } },
          }),
        );
        datasetId = d.id;
      }
      const root = v.outputUri.trim().replace(/\/+$/, '');
      items.push({
        name: zh.taskForm.batchName(v.name.trim(), e.dataset.name).slice(0, 128),
        input: { dataset_id: datasetId },
        output: { uri: `${root}/${suggestDeliveryName(e.dataset.name, monthDay())}`, ...(outRegion ? { region: outRegion } : {}), credential: outCred },
        preflight_id: pfId,
      });
    }
    setPhase('create');
    const embodiment = embodimentOf(v, result);
    const vlmC = vlmChoice(v, reg);
    const res = await unwrap(
      api().POST('/tasks/batch', {
        params: { header: { 'Idempotency-Key': idempotencyKey() } },
        body: {
          items,
          shared: {
            ...(v.note.trim() ? { note: v.note.trim() } : {}),
            episodes: episodeSelector(v),
            modules: moduleChoices(v, reg),
            ...(embodiment ? { embodiment_id: embodiment } : {}),
            ...(vlmC ? { vlm: vlmC } : {}),
            params: taskParams(v, reg, start),
          },
        },
      }),
    );
    savePrefs();
    Message.success(zh.taskForm.batchCreated(res.tasks.length));
    void qc.invalidateQueries({ queryKey: qk.tasksAll });
    navigate('/tasks');
  };

  const submit = async (start: boolean) => {
    if (uploadBusy) return;
    setShown({ 1: true, 2: true });
    const e1 = validateScreen1(v, ctx);
    const e2 = validateScreen2(v, ctx);
    if (Object.keys(e1).length) {
      setScreen(1);
      Message.error(zh.taskForm.leaveFirstScreen);
      return;
    }
    if (Object.keys(e2).length) return;
    try {
      if (p.batch) {
        await submitBatch(start);
        return;
      }
      setPhase('preflight');
      const pf = await preflight.ensureFresh();
      let datasetId = v.datasetId;
      if (!datasetId && v.source !== 'local') {
        // D36: an address typed here is registered as a dataset, so the start-time fingerprint
        // check (D37) has something to compare with. Registering twice returns the same one.
        setPhase('register');
        try {
          const d = await unwrap(api().POST('/datasets', { params: { header: { 'Idempotency-Key': idempotencyKey() } }, body: { input: inputRef(v) } }));
          datasetId = d.id;
          setV((prev) => ({ ...prev, datasetId: d.id }));
          void qc.invalidateQueries({ queryKey: qk.datasetsAll });
        } catch (err) {
          setServerErrors((prev) => ({ ...prev, [v.source === 'public' ? 'publicUri' : 'datasetUri']: errorMessage(err) }));
          setScreen(1);
          return;
        }
      }
      setPhase('create');
      let id = taskId;
      let warnings: string[] = [];
      if (id && updatedAt !== null) {
        const t = await unwrap(api().PATCH('/tasks/{id}', { params: { path: { id }, header: { 'If-Match': String(updatedAt) } }, body: toTaskPatch(v, reg, pf.result, pf.id, datasetId) }));
        setUpdatedAt(t.updated_at);
      } else {
        const c = await unwrap(api().POST('/tasks', { params: { header: { 'Idempotency-Key': idempotencyKey() } }, body: toTaskCreate(v, reg, pf.result, pf.id, datasetId, false) }));
        id = c.id;
        warnings = c.warnings;
        setTaskId(c.id);
        setUpdatedAt(c.created_at);
        // Keep the created task: another submit edits it instead of creating a second one.
        const t = await unwrap(api().GET('/tasks/{id}', { params: { path: { id: c.id } } }));
        setUpdatedAt(t.updated_at);
        navigate(`/tasks/new?edit=${encodeURIComponent(c.id)}`, { replace: true });
      }
      savePrefs();
      void qc.invalidateQueries({ queryKey: qk.tasksAll });
      void qc.invalidateQueries({ queryKey: qk.task(id) });
      if (!start) {
        Message.success(p.mode === 'edit' ? zh.taskForm.savedEdit : zh.taskForm.created);
        warnings.forEach((w) => Notification.warning({ title: zh.taskForm.titleNew, content: w }));
        navigate(`/tasks/${id}`);
        return;
      }
      await startTask(id, warnings);
    } catch (e) {
      if (e instanceof ApiError) {
        if (e.code === 'preflight_expired') {
          preflight.rerun();
          Message.warning(e.message);
        } else if (e.code === 'precondition_failed' && taskId) {
          Message.warning(e.message);
          const t = await unwrap(api().GET('/tasks/{id}', { params: { path: { id: taskId } } })).catch(() => null);
          if (t) setUpdatedAt(t.updated_at);
        } else if (e.code === 'precheck_failed') {
          applyPrecheck(e);
        } else {
          const field = fieldFromDetails(e);
          if (field) {
            setServerErrors((prev) => ({ ...prev, [field]: e.message }));
            if (['datasetUri', 'outputUri', 'headN', 'expr', 'vlmBackend', 'modules'].includes(field)) setScreen(1);
          } else Message.error(e.message);
        }
      } else Message.error(errorMessage(e));
    } finally {
      setPhase(null);
    }
  };

  const confirmRepreflight = async () => {
    if (!fp) return;
    setFpBusy(true);
    try {
      const res = await unwrap(api().POST('/tasks/{id}/repreflight', { params: { path: { id: fp.taskId }, header: { 'Idempotency-Key': idempotencyKey() } } }));
      setFp(null);
      void qc.invalidateQueries({ queryKey: qk.tasksAll });
      if (res.compatible) {
        Message.success(zh.fingerprint.compatible);
        navigate(`/tasks/${fp.taskId}`);
      } else {
        setUpdatedAt(res.task.updated_at);
        Message.warning(zh.fingerprint.incompatible);
        handleIncompatible(res.incompatibilities);
      }
    } catch (e) {
      Message.error(errorMessage(e));
    } finally {
      setFpBusy(false);
    }
  };

  const title = p.batch ? zh.taskForm.titleBatch(p.batch.length) : p.mode === 'edit' ? zh.taskForm.titleEdit : p.mode === 'copy' ? zh.taskForm.titleCopy : zh.taskForm.titleNew;
  const embodimentOptions = useMemo(() => [...new Set((result?.modules ?? []).flatMap((m) => (m.input_hint?.field === 'embodiment_id' ? m.input_hint.options ?? [] : [])))], [result]);

  const batchList = p.batch ? (
    <div style={{ marginBottom: 16 }}>
      <Typography.Text>{zh.taskForm.batchDatasets}</Typography.Text>
      <Alert type="info" style={{ margin: '8px 0' }} content={zh.deeplink.batchMode(p.batch.length)} />
      {p.linkNotes.dataset.map((n) => (
        <div key={n} className="field-note-error" role="alert">
          {n}
        </div>
      ))}
      <ul style={{ paddingLeft: 18 }} data-testid="batch-list">
        {batchState.map((e) => (
          <li key={e.dataset.uri} className="mono">
            {e.dataset.uri}{' '}
            {e.status === 'ok' && e.result ? (
              <Tag size="small" color={e.result.format.supported ? 'green' : 'red'}>
                {e.result.format.supported ? `${zh.taskForm.preflightOk} · ${e.result.dataset ? zh.common.items(e.result.dataset.episode_count) : '?'}` : zh.taskForm.preflightUnsupported}
              </Tag>
            ) : e.status === 'error' ? (
              <Tag size="small" color="red">
                {zh.taskForm.batchPreflightFailed}：{errorMessage(e.error)}
              </Tag>
            ) : (
              <Tag size="small">{zh.taskForm.batchPreflightPending}</Tag>
            )}
          </li>
        ))}
      </ul>
    </div>
  ) : null;

  return (
    <Form layout="vertical" onSubmit={() => undefined}>
      <div className="card-gap">
        <div>
          <Typography.Title heading={5} style={{ margin: 0 }}>
            {title}
          </Typography.Title>
          <Typography.Text type="secondary">{zh.taskForm.desc}</Typography.Text>
        </div>
        <Steps current={screen} size="small">
          <Steps.Step title={zh.taskForm.step1} />
          <Steps.Step title={zh.taskForm.step2} />
        </Steps>
        {incompatNotes.length ? (
          <Alert
            type="error"
            title={zh.taskForm.incompatibleTitle}
            content={
              <ul style={{ margin: 0, paddingLeft: 18 }} data-testid="incompatibilities">
                {incompatNotes.map((n) => (
                  <li key={n}>{n}</li>
                ))}
              </ul>
            }
          />
        ) : null}

        <div style={{ display: screen === 1 ? 'block' : 'none' }} className="card-gap" data-testid="screen-1">
          <BasicSection
            v={v}
            set={set}
            errors={errors}
            batch={Boolean(p.batch)}
            batchList={batchList}
            credentials={p.credentials}
            registered={p.registered}
            publicCatalog={p.publicCatalog}
            localEnabled={p.localEnabled}
            probe={probe}
            onOutputBlur={onOutputBlur}
            onPickRegistered={(d) => {
              void unwrap(api().GET('/datasets/{id}', { params: { path: { id: d.id } } }))
                .then((detail) => set({ datasetUri: detail.uri, datasetId: detail.id, source: detail.source, region: detail.region ?? v.region, credential: detail.credential ?? v.credential }))
                .catch((e: unknown) => Message.error(errorMessage(e)));
            }}
            onAddCredential={() => setAddKey(true)}
            linkNotes={p.linkNotes}
            outputNote={outputNote}
          />
          {!p.batch ? <PreflightCard state={preflight} registry={reg} onRerun={preflight.rerun} /> : null}
          <EpisodeSection v={v} set={set} errors={errors} total={total} preview={preview} />
          <ModuleSection v={v} set={set} errors={errors} registry={reg} availability={availability} marked={marked} />
          {vlm ? <ModelSection v={v} set={set} errors={errors} backends={p.backends} registry={reg} onAddBackend={() => setAddBackend(true)} /> : null}
          <AdvancedSection v={v} set={set} errors={errors} vlm={vlm} />
        </div>

        <div style={{ display: screen === 2 ? 'block' : 'none' }} data-testid="screen-2">
          <ModuleSettings v={v} set={set} errors={errors} registry={reg} preflight={result} embodimentOptions={embodimentOptions} onUploadBusy={onUploadBusy} />
        </div>
      </div>

      <div className="sticky-footer">
        <Space>
          {screen === 2 ? (
            <Button onClick={() => setScreen(1)} disabled={Boolean(phase)}>
              {zh.taskForm.prev}
            </Button>
          ) : (
            <Button onClick={() => navigate(p.mode === 'edit' && p.editTask ? `/tasks/${p.editTask.id}` : '/tasks')}>{zh.taskForm.cancel}</Button>
          )}
          {/* only once the preflight knows the episodes (fourth round): no 「? 条 episode」 before */}
          {count !== null ? <span data-testid="footer-summary">{footer}</span> : null}
        </Space>
        <Space>
          {screen === 1 ? (
            <Button type="primary" onClick={next}>
              {zh.taskForm.next}
            </Button>
          ) : (
            <Tooltip content={zh.taskForm.uploadBusy} disabled={!uploadBusy}>
              <Space>
                <Button loading={phase !== null} disabled={uploadBusy} onClick={() => void submit(false)}>
                  {p.mode === 'edit' ? zh.taskForm.save : zh.taskForm.saveDraft}
                </Button>
                <Button type="primary" loading={phase !== null} disabled={uploadBusy} onClick={() => void submit(true)}>
                  {p.mode === 'edit' ? zh.taskForm.saveStart : zh.taskForm.createStart}
                </Button>
              </Space>
            </Tooltip>
          )}
        </Space>
      </div>

      <FingerprintDialog visible={Boolean(fp)} change={fp?.change ?? null} loading={fpBusy} onCancel={() => setFp(null)} onConfirm={() => void confirmRepreflight()} />
      <AccessKeyDrawer
        visible={addKey}
        editing={null}
        onClose={() => setAddKey(false)}
        onSaved={(c) => {
          if (!v.credential) set({ credential: c.name, ...(v.region ? {} : { region: c.meta.region }) }, 'credential');
        }}
      />
      <BackendDrawer
        visible={addBackend}
        editing={null}
        onClose={() => setAddBackend(false)}
        onSaved={(b) => {
          if (!v.vlmBackend) set({ vlmBackend: b.name, vlmModel: b.models.length === 1 ? b.models[0].model_name : '' }, 'vlmBackend');
        }}
      />
    </Form>
  );
}
