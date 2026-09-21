import { useCallback, useEffect, useRef, useState } from 'react';
import { api, unwrap } from '../../api/client';
import type { PreflightRequest, PreflightResponse, PreflightResult, ProbeResult } from '../../api/types';

/** 07 §3: the preflight fires by itself 600 ms after the address stops changing. */
export const PREFLIGHT_DEBOUNCE = { ms: 600 };

export interface PreflightState {
  status: 'idle' | 'running' | 'ok' | 'error';
  result: PreflightResult | null;
  id: string | null;
  expiresAt: number;
  error: unknown;
  key: string;
}

const IDLE: PreflightState = { status: 'idle', result: null, id: null, expiresAt: 0, error: null, key: '' };

export function runPreflight(req: PreflightRequest): Promise<PreflightResponse> {
  return unwrap(api().POST('/preflight', { body: req }));
}

/**
 * Debounced, automatic preflight (no 「检测」 button). Keeps the last result visible while a new
 * one runs; ignores answers to superseded requests; ensureFresh() returns an id that is valid for
 * the current inputs and not about to expire (results are kept 30 minutes).
 */
export function usePreflight(req: PreflightRequest | null) {
  const key = req ? JSON.stringify(req) : '';
  const [state, setState] = useState<PreflightState>(IDLE);
  const seq = useRef(0);
  const stateRef = useRef(state);
  stateRef.current = state;

  const exec = useCallback(async (my: number, k: string): Promise<PreflightResponse> => {
    try {
      const res = await runPreflight(JSON.parse(k) as PreflightRequest);
      if (my === seq.current) setState({ status: 'ok', result: res.result, id: res.preflight_id, expiresAt: res.expires_at, error: null, key: k });
      return res;
    } catch (e) {
      if (my === seq.current) setState({ status: 'error', result: null, id: null, expiresAt: 0, error: e, key: k });
      throw e;
    }
  }, []);

  useEffect(() => {
    seq.current += 1;
    const my = seq.current;
    if (!key) {
      setState(IDLE);
      return undefined;
    }
    setState((s) => ({ ...s, status: 'running' }));
    const t = setTimeout(() => void exec(my, key).catch(() => undefined), PREFLIGHT_DEBOUNCE.ms);
    return () => clearTimeout(t);
  }, [key, exec]);

  const ensureFresh = useCallback(async (): Promise<{ id: string; result: PreflightResult }> => {
    const s = stateRef.current;
    if (s.status === 'ok' && s.key === key && s.id && s.result && s.expiresAt - Date.now() > 60_000) return { id: s.id, result: s.result };
    seq.current += 1;
    const my = seq.current;
    setState((x) => ({ ...x, status: 'running' }));
    const res = await exec(my, key);
    return { id: res.preflight_id, result: res.result };
  }, [key, exec]);

  const rerun = useCallback(() => {
    if (!key) return;
    seq.current += 1;
    const my = seq.current;
    setState((x) => ({ ...x, status: 'running' }));
    void exec(my, key).catch(() => undefined);
  }, [key, exec]);

  return { ...state, current: state.key === key, ensureFresh, rerun };
}

export interface ProbeState {
  key: string;
  status: 'idle' | 'running' | 'ok' | 'fail';
  message: string;
  at: number;
  credential: string;
}

/** The delivery write probe (07 §3: on blur, a real write; v1 behaviour kept). */
export function useDeliveryProbe() {
  const [state, setState] = useState<ProbeState>({ key: '', status: 'idle', message: '', at: 0, credential: '' });
  const seq = useRef(0);
  const probe = useCallback(async (uri: string, region: string, credential: string): Promise<ProbeResult | null> => {
    const k = `${uri}|${region}|${credential}`;
    seq.current += 1;
    const my = seq.current;
    setState({ key: k, status: 'running', message: '', at: 0, credential });
    try {
      const res = await unwrap(api().POST('/deliveries/probe', { body: { uri, credential, ...(region ? { region } : {}) } }));
      if (my === seq.current) setState({ key: k, status: res.ok ? 'ok' : 'fail', message: res.error?.message ?? '', at: Date.now(), credential });
      return res;
    } catch (e) {
      if (my === seq.current) setState({ key: k, status: 'fail', message: e instanceof Error ? e.message : String(e), at: Date.now(), credential });
      return null;
    }
  }, []);
  const reset = useCallback(() => {
    seq.current += 1;
    setState({ key: '', status: 'idle', message: '', at: 0, credential: '' });
  }, []);
  return { ...state, probe, reset };
}
