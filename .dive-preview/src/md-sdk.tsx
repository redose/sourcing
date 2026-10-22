/**
 * Simplified @motherduck/react-sql-query shim for local Vite preview.
 * Same API as the production dive runtime — useSQLQuery, useDiveState, useExport,
 * useConnection, useConnectionStatus, and MotherDuckSDKProvider.
 */
import { MDConnection } from '@motherduck/wasm-client';
import type { DuckDBRow } from '@motherduck/wasm-client';
import {
	createContext,
	useCallback,
	useContext,
	useEffect,
	useMemo,
	useRef,
	useState,
	useSyncExternalStore,
} from 'react';
import type { ReactNode } from 'react';

// ── Types ──────────────────────────────────────────────────────────

type QueryStatus = 'idle' | 'loading' | 'success' | 'error';

type ExportFormat = 'csv' | 'json' | 'parquet' | 'xlsx';

export type ExportQueryOptions = {
	format: ExportFormat;
	title?: string;
	filename?: string;
	[key: string]: unknown;
};

type ExportQueryRequest = { sql: string } & ExportQueryOptions;

type ConnectionState =
	| { status: 'idle' }
	| { status: 'connecting' }
	| { status: 'connected'; connection: MDConnection }
	| { status: 'error'; error: Error };

export type UseSQLQueryResult<TData = readonly DuckDBRow[]> = {
	data: TData | undefined;
	isLoading: boolean;
	isSuccess: boolean;
	isError: boolean;
	isPlaceholderData: boolean;
	error: Error | null;
	refetch: () => void;
	exportAs: (options: ExportQueryOptions) => Promise<void>;
	status: QueryStatus;
};

type UseSQLQueryOptions<TData = readonly DuckDBRow[]> = {
	enabled?: boolean;
	select?: (data: readonly DuckDBRow[]) => TData;
	initialData?: TData;
	placeholderData?: TData | ((prev: TData | undefined) => TData | undefined);
};

async function simulateLocalExport(sql: string, options: ExportQueryOptions) {
	const filename = options.filename || options.title || 'export';
	console.info('Dive export requested in local preview', { sql, options });
	window.alert?.(
		`Export requested: ${filename}.${options.format}. Saved MotherDuck dives generate and download the real file.`,
	);
}

// ── QueryObserver (external store for useSyncExternalStore) ────────

interface QueryObserverState {
	status: QueryStatus;
	data: readonly DuckDBRow[] | undefined;
	error: Error | undefined;
	hasHadData: boolean;
	lastData: readonly DuckDBRow[] | undefined;
}

class QueryObserver {
	private state: QueryObserverState = {
		status: 'idle',
		data: undefined,
		error: undefined,
		hasHadData: false,
		lastData: undefined,
	};
	private listeners = new Set<() => void>();
	private abortController: AbortController | null = null;

	subscribe = (listener: () => void) => {
		this.listeners.add(listener);
		return () => {
			this.listeners.delete(listener);
		};
	};
	getSnapshot = () => this.state;
	getStatus() {
		return this.state.status;
	}

	private setState(updates: Partial<QueryObserverState>) {
		this.state = { ...this.state, ...updates };
		this.listeners.forEach((l) => l());
	}

	async execute(connection: MDConnection, sql: string) {
		this.abortController?.abort();
		this.abortController = new AbortController();
		const { signal } = this.abortController;
		this.setState({ status: 'loading', data: undefined, error: undefined });
		try {
			const result = await connection.safeEvaluateQuery(sql);
			if (signal.aborted) return;
			if (result.status === 'error') {
				console.error('Query failed', result.err, sql);
				this.setState({ status: 'error', error: result.err, data: undefined });
				return;
			}
			const data = result.result.data.toRows();
			this.setState({ status: 'success', data, error: undefined, hasHadData: true, lastData: data });
		} catch (err) {
			if (signal.aborted) return;
			this.setState({
				status: 'error',
				error: err instanceof Error ? err : new Error(String(err)),
				data: undefined,
			});
		}
	}

	reset() {
		this.abortController?.abort();
		this.abortController = null;
		this.setState({ status: 'idle', data: undefined, error: undefined });
	}
	cancel() {
		this.abortController?.abort();
		this.abortController = null;
	}
}

// ── Provider ───────────────────────────────────────────────────────

type ContextValue = { state: ConnectionState };
const SDKContext = createContext<ContextValue | null>(null);

function useSDKContext() {
	const ctx = useContext(SDKContext);
	if (!ctx) throw new Error('Must be used within MotherDuckSDKProvider');
	return ctx;
}

export function MotherDuckSDKProvider({ token, children }: { token: string; children: ReactNode }) {
	const [state, setState] = useState<ConnectionState>({ status: 'idle' });

	useEffect(() => {
		if (!token) {
			setState({ status: 'idle' });
			return;
		}
		let cancelled = false;
		let conn: MDConnection | null = null;
		(async () => {
			setState({ status: 'connecting' });
			try {
				conn = MDConnection.create({ mdToken: token, useDuckDBWasmCOI: false });
				await conn.isInitialized();
				if (!cancelled) setState({ status: 'connected', connection: conn });
			} catch (err) {
				if (!cancelled)
					setState({ status: 'error', error: err instanceof Error ? err : new Error(String(err)) });
			}
		})();
		return () => {
			cancelled = true;
			conn?.close();
		};
	}, [token]);

	const value = useMemo(() => ({ state }), [state]);
	return <SDKContext.Provider value={value}>{children}</SDKContext.Provider>;
}

// ── useSQLQuery ────────────────────────────────────────────────────

export function useSQLQuery<TData = readonly DuckDBRow[]>(
	sql: string,
	options?: UseSQLQueryOptions<TData>,
): UseSQLQueryResult<TData> {
	const { state: connState } = useSDKContext();
	const observerRef = useRef<QueryObserver | null>(null);
	if (!observerRef.current) observerRef.current = new QueryObserver();
	const observer = observerRef.current;

	const snap = useSyncExternalStore(observer.subscribe, observer.getSnapshot, observer.getSnapshot);
	const enabled = options?.enabled !== false;

	useEffect(() => {
		if (!enabled || connState.status !== 'connected') {
			if (observer.getStatus() !== 'idle') observer.reset();
			return;
		}
		observer.execute(connState.connection, sql);
		return () => observer.cancel();
	}, [sql, enabled, connState, observer]);

	const refetch = useCallback(() => {
		if (connState.status === 'connected' && enabled) observer.execute(connState.connection, sql);
	}, [observer, connState, enabled, sql]);

	const exportAs = useCallback(
		async (exportOptions: ExportQueryOptions) => {
			if (connState.status !== 'connected') throw new Error('Cannot export before the MotherDuck connection is ready.');
			await simulateLocalExport(sql, exportOptions);
		},
		[connState.status, sql],
	);

	const isLoading = snap.status === 'loading' || connState.status === 'connecting';
	const rawData = snap.data ?? snap.lastData;

	const transformed = useMemo(() => {
		if (rawData === undefined) return undefined;
		return options?.select ? options.select(rawData) : (rawData as unknown as TData);
	}, [rawData, options?.select]);

	const { data, isPlaceholderData } = useMemo(() => {
		if (transformed !== undefined) return { data: transformed, isPlaceholderData: false };
		if (!snap.hasHadData && options?.initialData !== undefined)
			return { data: options.initialData, isPlaceholderData: false };
		if (isLoading && options?.placeholderData !== undefined) {
			const ph =
				typeof options.placeholderData === 'function'
					? (options.placeholderData as (p: TData | undefined) => TData | undefined)(transformed)
					: options.placeholderData;
			if (ph !== undefined) return { data: ph, isPlaceholderData: true };
		}
		return { data: undefined, isPlaceholderData: false };
	}, [transformed, snap.hasHadData, options?.initialData, options?.placeholderData, isLoading]);

	return {
		data,
		isLoading,
		isSuccess: snap.status === 'success',
		isError: snap.status === 'error',
		isPlaceholderData,
		error: snap.error ?? null,
		refetch,
		exportAs,
		status: snap.status,
	};
}

// ── useDiveState ───────────────────────────────────────────────────
// Local stand-in for the runtime's persisted view state: one shared, in-memory bag keyed by name.
// Setting a key to undefined restores its initial value, as in production.

// Readable preview links, e.g. #/Datura+Amphetamine/49804?at=0:30 or #/effects?effect=Anxiety.
// (A saved MotherDuck dive keeps its state in links of its own format.)
const enc = (s: string) => encodeURIComponent(s).replace(/%20/g, '_');
const dec = (s: string) => decodeURIComponent(s.replace(/_/g, '%20'));
const fmtAt = (h: number) => `${Math.floor(h)}:${String(Math.round((h % 1) * 60)).padStart(2, '0')}`;
const parseAt = (s: string) => {
	const [h, mm] = s.split(':').map(Number);
	return Number.isFinite(h) ? h + (Number.isFinite(mm) ? mm / 60 : 0) : undefined;
};

function readRoute(): [string, unknown][] {
	const hash = window.location.hash.replace(/^#\/?/, '');
	if (!hash) return [];
	const [path, query = ''] = hash.split('?');
	const parts = path.split('/').filter(Boolean);
	const out: [string, unknown][] = [];
	if (parts[0] === 'substances' || parts[0] === 'effects') out.push(['view', parts[0]]);
	else if (parts[0]) {
		const [s1, s2] = parts[0].split('+').map(dec);
		out.push(['substance', s1]);
		if (s2) out.push(['with', s2]);
		if (parts[1]) out.push(['report', `erowid-${parts[1]}`]);
	}
	const q = new URLSearchParams(query);
	const at = q.get('at');
	if (at && parseAt(at) != null) out.push(['at', parseAt(at)]);
	for (const kind of ['effect', 'substance'] as const) {
		const v = q.get(kind);
		if (v) out.push(['open', { kind, name: dec(v) }]);
	}
	return out;
}

function writeRoute(state: Map<string, unknown>) {
	const view = state.get('view') as string | undefined;
	const sub = state.get('substance') as string | undefined;
	const withSub = state.get('with') as string | undefined;
	const report = state.get('report') as string | undefined;
	const at = state.get('at') as number | undefined;
	const open = state.get('open') as { kind: string; name: string } | undefined;
	let path = '';
	if (view && view !== 'reports') path = view;
	else if (sub) path = enc(sub) + (withSub ? '+' + enc(withSub) : '') + (report ? '/' + report.replace(/^erowid-/, '') : '');
	const q: string[] = [];
	if (report && at) q.push('at=' + fmtAt(at));
	if (open?.name) q.push(`${open.kind}=${enc(open.name)}`);
	const hash = path || q.length ? `#/${path}${q.length ? '?' + q.join('&') : ''}` : '';
	history.replaceState(null, '', hash || window.location.pathname);
}

const diveState = new Map<string, unknown>(readRoute());
const writeHash = () => writeRoute(diveState);
const diveListeners = new Set<() => void>();
let diveVersion = 0;

export function useDiveState<T>(key: string, initial: T): [T, (value: T) => void] {
	useSyncExternalStore(
		(l) => {
			diveListeners.add(l);
			return () => diveListeners.delete(l);
		},
		() => diveVersion,
	);
	const value = diveState.has(key) ? (diveState.get(key) as T) : initial;
	const set = useCallback(
		(next: T) => {
			if (next === undefined) diveState.delete(key);
			else diveState.set(key, next);
			writeHash();
			diveVersion++;
			diveListeners.forEach((l) => l());
		},
		[key],
	);
	return [value, set];
}

// ── useConnection / useConnectionStatus / useExport ─────────────────

export function useConnection(): MDConnection | null {
	const { state } = useSDKContext();
	return state.status === 'connected' ? state.connection : null;
}

export function useConnectionStatus() {
	const { state } = useSDKContext();
	return {
		isConnected: state.status === 'connected',
		isConnecting: state.status === 'connecting',
		error: state.status === 'error' ? state.error : null,
	};
}

export function useExport() {
	const { state } = useSDKContext();
	const exportQuery = useCallback(
		async ({ sql, ...options }: ExportQueryRequest) => {
			if (state.status !== 'connected') throw new Error('Cannot export before the MotherDuck connection is ready.');
			await simulateLocalExport(sql, options);
		},
		[state.status],
	);
	return { exportQuery };
}
