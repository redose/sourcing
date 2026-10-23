/**
 * The static site's database: DuckDB running in the visitor's own browser, reading the Parquet files exported by
 * site/export_data.py. No server and no token; nothing about what anyone looks at leaves their browser.
 *
 * It answers queries the same way the MotherDuck connection does (safeEvaluateQuery → rows), so md-sdk.tsx can use
 * either. Tables load into memory at start (a few MB), except the reports' full text, which stays in its file and is
 * read a small slice at a time when a report is opened.
 */
import * as duckdb from '@duckdb/duckdb-wasm';
import mvpWasm from '@duckdb/duckdb-wasm/dist/duckdb-mvp.wasm?url';
import mvpWorker from '@duckdb/duckdb-wasm/dist/duckdb-browser-mvp.worker.js?url';
import ehWasm from '@duckdb/duckdb-wasm/dist/duckdb-eh.wasm?url';
import ehWorker from '@duckdb/duckdb-wasm/dist/duckdb-browser-eh.worker.js?url';

const IN_MEMORY = [
	'report_doses', 'llm_extractions', 'llm_observation_effects', 'llm_observations', 'effects', 'substances',
	'timeline_sentiment', 'tripsit_group_members', 'street_names', 'erowid_substance_map', 'substance_aliases',
	'routes_of_administration', 'report_timeline', 'tripsit_combos', 'substance_structures', 'substance_effects',
	'interactions', 'alias_mixes', 'world_countries', 'substance_overviews', 'substance_legality',
	'substance_legality_international', 'substance_harms', 'report_phases', 'effect_replications',
	'substance_half_lives', 'llm_context_events',
];
const FROM_FILE = ['experience_reports'];

// Arrow values → the plain JS the app expects (numbers, arrays, objects).
function plain(v: unknown): unknown {
	if (v == null) return v;
	if (typeof v === 'bigint') return Number(v);
	if (typeof v !== 'object') return v;
	if (v instanceof Date) return v;
	if (ArrayBuffer.isView(v)) return Array.from(v as unknown as ArrayLike<unknown>, plain);
	// Rows and structs turn into objects, lists into arrays (both have toJSON; rows also have a toArray of their bare
	// values, so toJSON comes first or the column names get lost).
	const o = v as { toJSON?: () => unknown; toArray?: () => ArrayLike<unknown> };
	if (typeof o.toJSON === 'function') {
		const j = o.toJSON();
		if (Array.isArray(j)) return j.map(plain);
		if (j && typeof j === 'object') return Object.fromEntries(Object.entries(j as Record<string, unknown>).map(([k, x]) => [k, plain(x)]));
		return j;
	}
	if (typeof o.toArray === 'function') return Array.from(o.toArray(), plain);
	return v;
}

// Arrow hands DECIMAL values over as four 32-bit words of the scaled integer; turn them into ordinary numbers.
function decimal(v: unknown, scale: number): number | null {
	if (v == null) return null;
	const w = v as Uint32Array;
	const big = BigInt(w[0]) | (BigInt(w[1]) << 32n) | (BigInt(w[2]) << 64n) | (BigInt(w[3]) << 96n);
	return Number(BigInt.asIntN(128, big)) / 10 ** scale;
}

type Result = { status: 'success'; result: { data: { toRows: () => Record<string, unknown>[] } } } | { status: 'error'; err: Error };

export async function openStaticDatabase() {
	const bundle = await duckdb.selectBundle({ mvp: { mainModule: mvpWasm, mainWorker: mvpWorker }, eh: { mainModule: ehWasm, mainWorker: ehWorker } });
	const db = new duckdb.AsyncDuckDB(new duckdb.VoidLogger(), new Worker(bundle.mainWorker!));
	await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
	const setup = await db.connect();
	const base = new URL(`${import.meta.env.BASE_URL}data/`, window.location.href).href;
	await setup.query(`ATTACH ':memory:' AS sourcing`);
	// Fetch the files side by side (so they're in the browser's cache), then create the tables one at a time, since
	// DuckDB won't create tables in the same database from several connections at once.
	await Promise.all(IN_MEMORY.map((t) => fetch(`${base}${t}.parquet`).then((r) => r.arrayBuffer()).catch(() => null)));
	for (const t of IN_MEMORY) await setup.query(`CREATE TABLE sourcing.main.${t} AS SELECT * FROM read_parquet('${base}${t}.parquet')`);
	for (const t of FROM_FILE) await setup.query(`CREATE VIEW sourcing.main.${t} AS SELECT * FROM read_parquet('${base}${t}.parquet')`);
	await setup.query(`USE sourcing`);
	// A few connections, so one slow query doesn't hold up the rest of the page.
	const pool = await Promise.all([0, 1, 2, 3].map(async () => {
		const c = await db.connect();
		await c.query(`USE sourcing`);
		return c;
	}));
	let next = 0;
	return {
		async safeEvaluateQuery(sql: string): Promise<Result> {
			const c = pool[next++ % pool.length];
			try {
				const table = await c.query(sql);
				const decs = table.schema.fields
					.filter((f) => String(f.type).startsWith('Decimal'))
					.map((f) => [f.name, (f.type as unknown as { scale: number }).scale] as const);
				const rows = table.toArray().map((r) => {
					const o = plain(r) as Record<string, unknown>;
					for (const [k, sc] of decs) o[k] = decimal((r as unknown as Record<string, unknown>)[k], sc);
					return o;
				});
				return { status: 'success', result: { data: { toRows: () => rows } } };
			} catch (err) {
				return { status: 'error', err: err instanceof Error ? err : new Error(String(err)) };
			}
		},
		async close() {
			await Promise.all(pool.map((c) => c.close()));
			await setup.close();
			await db.terminate();
		},
	};
}
