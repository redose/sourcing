import { createContext, useContext, useMemo, useState } from 'react';
import { useDiveState, useSQLQuery } from '@motherduck/react-sql-query';
import {
	Area,
	AreaChart,
	Bar,
	BarChart,
	CartesianGrid,
	Legend,
	Line,
	LineChart,
	ResponsiveContainer,
	Scatter,
	ScatterChart,
	Tooltip,
	XAxis,
	YAxis,
	ZAxis,
} from 'recharts';
import {
	Activity,
	AlertTriangle,
	BookOpen,
	Clock,
	GitMerge,
	Info,
	LayoutGrid,
	ListChecks,
	Pill,
	Shuffle,
	Sparkles,
	X,
} from 'lucide-react';

export const REQUIRED_DATABASES = [
	{ type: 'database', path: 'md:sourcing', alias: 'sourcing' },
];

const DB = '"sourcing"."main"';
const N = (v: unknown): number => (v == null ? 0 : Number(v));
const S = (v: unknown): string => (v == null ? '' : String(v));
const esc = (s: string) => s.replace(/'/g, "''");
const L = (v: unknown): string[] => {
	if (v == null) return [];
	if (Array.isArray(v)) return v.map(String);
	const o = v as { values?: ArrayLike<unknown>; toArray?: () => ArrayLike<unknown> };
	const vals = o.values ?? o.toArray?.();
	return vals ? Array.from(vals).map(String) : [];
};

// re:dose palette — sampled from the re:dose.org logo
const C = {
	// Logo hues, deepened slightly so they stay readable on a light background.
	coral: '#ee4f74',
	cyan: '#12a9b8',
	teal: '#11998c',
	lavender: '#8b6cf0',
	lavSoft: 'rgba(139,108,240,0.12)',
	primary: '#12a9b8',
	positive: '#11998c',
	negative: '#ee4f74',
	mixed: '#e38b1f',
	neutral: '#b8b0cf',
	bg: '#fbf9ff',
	card: '#ffffff',
	skel: '#eee9fa',
	grid: '#ece7f6',
	text: '#2a2240',
	muted: '#5f5878',
	line: '#e4ddf3',
	ink: '#ffffff',
};
const LAV = '139,108,240';
const MONO = '"Courier New", Courier, ui-monospace, monospace';
const ROUNDED = 'ui-rounded, "SF Pro Rounded", "Arial Rounded MT Bold", "Varela Round", system-ui, sans-serif';
const tipStyle = {
	contentStyle: { background: C.card, border: `1px solid ${C.line}`, borderRadius: 8, fontSize: 12 },
	labelStyle: { color: C.text },
	itemStyle: { color: C.text },
	cursor: { fill: 'rgba(139,108,240,0.10)' },
};
const PAGE_BG = `radial-gradient(900px 420px at 0% -8%, rgba(${LAV},0.20), transparent 70%), radial-gradient(700px 360px at 100% 0%, rgba(90,224,228,0.22), transparent 70%), radial-gradient(600px 300px at 50% 100%, rgba(252,108,132,0.08), transparent 70%), ${'#fbf9ff'}`;

// Plain-language reading of the −1…+1 text-sentiment score.
const moodLabel = (v: number): { text: string; color: string } =>
	v > 0.15
		? { text: 'Upbeat', color: '#11998c' }
		: v > 0.05
			? { text: 'Mostly positive', color: '#11998c' }
			: v > -0.05
				? { text: 'Neutral', color: '#5f5878' }
				: v > -0.15
					? { text: 'Mostly negative', color: '#ee4f74' }
					: { text: 'Low', color: '#ee4f74' };

const BINS = ['0–15m', '15–30m', '30–60m', '1–2h', '2–3h', '3–4h', '4–6h', '6–8h', '8–12h', '12–24h', '24h+'];
const PHASES = ['🌱 First 30 min', '🌤️ 30 min – 2 h', '🌊 2 – 4 h', '🌅 4 – 8 h', '🌙 8 – 24 h', '☀️ Next day'];
const MOOD_BINS = ['0–30m', '30–60m', '1–2h', '2–3h', '3–4h', '4–6h', '6–8h', '8–12h', '12–24h'];
const MIX_COLORS = [C.lavender, C.teal, C.mixed, '#d16ba5'];
const MAX_MIX = 4;
const BANDS = ['Below threshold', 'Light', 'Common', 'Strong', 'Heavy+'];
const SEV_LABEL: Record<number, string> = { 3: 'Dangerous', 2: 'Unsafe', 1: 'Caution' };
const SEV_COLOR: Record<number, string> = { 3: C.negative, 2: C.mixed, 1: C.muted };
const MIN_N = 5;

const fmtH = (h: number | null | undefined): string => {
	if (h == null || Number.isNaN(h)) return '—';
	if (h < 1) return `${Math.max(1, Math.round(h * 60))} min`;
	const r = Math.round(h * 10) / 10;
	return `${r % 1 === 0 ? r.toFixed(0) : r.toFixed(1)} h`;
};
const pct = (v: unknown) => (v == null ? '—' : `${Math.round(N(v))}%`);
const rows = (q: { data?: unknown }) => (Array.isArray(q.data) ? (q.data as Record<string, unknown>[]) : []);

// ── SQL builders ──────────────────────────────────────────────────

const reportsCTE = (sub: string, single: boolean) => `
reports AS (
  SELECT r.id AS report_id, coalesce(m.canonical, r.substance) AS primary_sub,
         len(list_distinct(r.substances)) AS n_subs
  FROM ${DB}."experience_reports" r
  LEFT JOIN ${DB}."erowid_substance_map" m ON m.erowid_name = r.substance
),
sel AS (
  SELECT report_id, n_subs FROM reports
  WHERE primary_sub = '${esc(sub)}'${single ? ' AND n_subs = 1' : ''}
)`;

// Annotated, timed observations for whatever report set the `sel` CTE defines.
const obsFrom = (selCTE: string) => `${selCTE},
obs AS (
  SELECT o.report_id, o.obs_index, o.sentiment, o.description, o.time_relative, h.hrs,
    CASE WHEN h.hrs < 0.25 THEN 0 WHEN h.hrs < 0.5 THEN 1 WHEN h.hrs < 1 THEN 2 WHEN h.hrs < 2 THEN 3
         WHEN h.hrs < 3 THEN 4 WHEN h.hrs < 4 THEN 5 WHEN h.hrs < 6 THEN 6 WHEN h.hrs < 8 THEN 7
         WHEN h.hrs < 12 THEN 8 WHEN h.hrs < 24 THEN 9 ELSE 10 END AS bin,
    CASE WHEN h.hrs < 0.5 THEN 0 WHEN h.hrs < 2 THEN 1 WHEN h.hrs < 4 THEN 2 WHEN h.hrs < 8 THEN 3
         WHEN h.hrs < 24 THEN 4 ELSE 5 END AS phase
  FROM ${DB}."llm_observations" o
  JOIN sel USING (report_id),
  LATERAL (
    SELECT TRY_CAST(regexp_extract(o.time_relative, '^T\\+~?(\\d+):(\\d{2})', 1) AS INTEGER)
         + TRY_CAST(regexp_extract(o.time_relative, '^T\\+~?(\\d+):(\\d{2})', 2) AS INTEGER) / 60.0 AS hrs
  ) h
  WHERE h.hrs IS NOT NULL
),
eff AS (
  SELECT o.*, e.effect_name
  FROM obs o
  JOIN ${DB}."llm_observation_effects" e USING (report_id, obs_index)
  WHERE e.confidence <> 'low'
)`;
const obsCTE = (sub: string, single: boolean) => obsFrom(reportsCTE(sub, single));

// One-sentence explainer per effect from the Subjective Effect Index / PsychonautWiki descriptions, cleaned of
// scraper artifacts (glued-on names, [n] citations, parentheticals, missing spaces between sentences).
const effectInfoSQL = `
SELECT lower(name) AS k, name, category, url,
  trim(regexp_extract(
    regexp_replace(regexp_replace(regexp_replace(regexp_replace(
      CASE WHEN starts_with(description, name) THEN name || ' ' || substr(description, len(name) + 1) ELSE description END,
      '\\[\\d+\\]', '', 'g'), '\\s*\\([^)]*\\)', '', 'g'), '([.!?])([A-Z])', '\\1 \\2', 'g'), '\\s+', ' ', 'g'),
    '^(.*?[.!?])(\\s|$)', 1)) AS blurb
FROM ${DB}."effects"
WHERE description IS NOT NULL AND len(description) > 20`;

const subIdSQL = (sub: string) => `(SELECT id FROM ${DB}."substances" WHERE name = '${esc(sub)}')`;

const substanceListSQL = `
WITH reports AS (
  SELECT r.id AS report_id, coalesce(m.canonical, r.substance) AS primary_sub,
         len(list_distinct(r.substances)) AS n_subs
  FROM ${DB}."experience_reports" r
  LEFT JOIN ${DB}."erowid_substance_map" m ON m.erowid_name = r.substance
),
llm AS (SELECT DISTINCT report_id FROM ${DB}."llm_observations")
SELECT primary_sub, count(*) AS n_reports,
  count(*) FILTER (WHERE report_id IN (SELECT report_id FROM llm)) AS n_llm,
  count(*) FILTER (WHERE report_id IN (SELECT report_id FROM llm) AND n_subs = 1) AS n_llm_single
FROM reports
WHERE primary_sub IS NOT NULL AND primary_sub <> 'Unknown'
GROUP BY 1 HAVING n_llm >= 10
ORDER BY n_llm DESC, primary_sub`;

const kpiSQL = (sub: string, single: boolean) => `
WITH ${obsCTE(sub, single)},
neff AS (
  SELECT o.report_id, o.hrs, count(e.effect_name) AS n_eff
  FROM obs o
  LEFT JOIN ${DB}."llm_observation_effects" e
    ON e.report_id = o.report_id AND e.obs_index = o.obs_index AND e.confidence <> 'low'
  GROUP BY o.report_id, o.obs_index, o.hrs
),
per AS (
  SELECT report_id,
    min(hrs) FILTER (WHERE n_eff > 0) AS onset,
    arg_max(hrs, n_eff * 1000 - hrs) FILTER (WHERE hrs < 24) AS peak,
    max(hrs) FILTER (WHERE n_eff > 0 AND hrs < 24) AS last_eff
  FROM neff GROUP BY 1
),
ex AS (SELECT e.* FROM ${DB}."llm_extractions" e JOIN sel USING (report_id))
SELECT
  (SELECT count(*) FROM sel) AS n_reports,
  (SELECT count(DISTINCT report_id) FROM obs) AS n_llm,
  (SELECT count(*) FROM obs) AS n_obs,
  (SELECT count(*) FROM ex) AS n_ex,
  (SELECT 100.0 * avg((overall_sentiment = 'positive')::INT) FROM ex) AS pct_pos,
  (SELECT 100.0 * avg((overall_sentiment = 'negative')::INT) FROM ex) AS pct_neg,
  (SELECT 100.0 * avg((overall_sentiment = 'mixed')::INT) FROM ex) AS pct_mixed,
  (SELECT 100.0 * avg(would_repeat::INT) FROM ex) AS pct_repeat,
  (SELECT count(would_repeat) FROM ex) AS n_repeat,
  median(onset) AS onset_h, quantile_cont(onset, 0.25) AS onset_p25, quantile_cont(onset, 0.75) AS onset_p75,
  median(peak) AS peak_h, quantile_cont(peak, 0.25) AS peak_p25, quantile_cont(peak, 0.75) AS peak_p75,
  median(last_eff) AS last_h, quantile_cont(last_eff, 0.25) AS last_p25, quantile_cont(last_eff, 0.75) AS last_p75
FROM per`;

const phaseDenomSQL = (obs: string) => `
WITH ${obs}
SELECT phase, count(DISTINCT report_id) AS n_rep, count(*) AS n_obs,
  100.0 * avg((sentiment = 'negative')::INT) AS pct_neg
FROM obs GROUP BY phase ORDER BY phase`;

const phaseEffSQL = (obs: string) => `
WITH ${obs}
SELECT phase, effect_name, count(DISTINCT report_id) AS c
FROM eff GROUP BY phase, effect_name
QUALIFY row_number() OVER (PARTITION BY phase ORDER BY count(DISTINCT report_id) DESC, effect_name) <= 5
ORDER BY phase, c DESC`;

const binSQL = (obs: string) => `
WITH ${obs}
SELECT bin, count(DISTINCT report_id) AS n_rep, count(*) AS n_obs,
  sum((sentiment = 'positive')::INT) AS pos, sum((sentiment = 'mixed')::INT) AS mixed,
  sum((sentiment = 'negative')::INT) AS neg, sum((sentiment NOT IN ('positive','mixed','negative') OR sentiment IS NULL)::INT) AS neu
FROM obs GROUP BY bin ORDER BY bin`;

const heatSQL = (obs: string) => `
WITH ${obs},
top AS (
  SELECT effect_name, count(DISTINCT report_id) AS n
  FROM eff GROUP BY 1 ORDER BY 2 DESC, 1 LIMIT 18
)
SELECT e.effect_name, e.bin, count(DISTINCT e.report_id) AS c, any_value(t.n) AS n_total
FROM eff e JOIN top t USING (effect_name)
GROUP BY ALL`;

const yearsSQL = (sub: string, single: boolean) => `
WITH ${reportsCTE(sub, single)},
yrs AS (
  SELECT year(r.publication_date) AS yr, count(*) AS n
  FROM sel JOIN ${DB}."experience_reports" r ON r.id = sel.report_id
  WHERE r.publication_date IS NOT NULL GROUP BY 1
)
SELECT s.yr, coalesce(yrs.n, 0) AS n
FROM generate_series(2000, 2025) AS s(yr) LEFT JOIN yrs USING (yr)
ORDER BY s.yr`;

const examplesSQL = (sub: string, single: boolean, effect: string) => `
WITH ${obsCTE(sub, single)}
SELECT report_id, time_relative, bin, sentiment, description
FROM eff WHERE effect_name = '${esc(effect)}'
ORDER BY hrs, report_id LIMIT 60`;

const profileSQL = (sub: string, single: boolean) => `
WITH ${obsCTE(sub, single)},
ref AS (
  SELECT lower(ef.name) AS lname
  FROM ${DB}."substance_effects" se JOIN ${DB}."effects" ef ON ef.id = se.effect_id
  WHERE se.substance_id = ${subIdSQL(sub)}
),
first_seen AS (SELECT report_id, effect_name, min(hrs) AS first_h FROM eff GROUP BY ALL),
first_med AS (SELECT effect_name, median(first_h) AS med_first FROM first_seen WHERE first_h < 48 GROUP BY 1),
denom AS (SELECT count(DISTINCT report_id) AS n FROM obs)
SELECT e.effect_name,
  count(DISTINCT e.report_id) AS n_rep,
  100.0 * count(DISTINCT e.report_id) / any_value(denom.n) AS pct_reports,
  any_value(fm.med_first) AS med_first_h,
  100.0 * avg((e.sentiment = 'negative')::INT) AS pct_neg,
  bool_or(lower(e.effect_name) IN (SELECT lname FROM ref)) AS in_ref,
  (SELECT count(*) FROM ref) AS n_ref,
  any_value(ef.category) AS category
FROM eff e
CROSS JOIN denom
LEFT JOIN first_med fm USING (effect_name)
LEFT JOIN ${DB}."effects" ef ON lower(ef.name) = lower(e.effect_name)
GROUP BY e.effect_name
ORDER BY n_rep DESC, e.effect_name
LIMIT 40`;

const refMissingSQL = (sub: string, single: boolean) => `
WITH ${obsCTE(sub, single)}
SELECT ef.name
FROM ${DB}."substance_effects" se JOIN ${DB}."effects" ef ON ef.id = se.effect_id
WHERE se.substance_id = ${subIdSQL(sub)}
  AND lower(ef.name) NOT IN (SELECT lower(effect_name) FROM eff)
ORDER BY ef.name`;

const roaSQL = (sub: string) => `
SELECT route, replace(dose_units, 'μg', 'µg') AS dose_units, dose_threshold, dose_light_min, dose_common_min,
  dose_common_max, dose_strong_max, dose_heavy, duration_onset, duration_comeup, duration_peak,
  duration_offset, duration_total
FROM ${DB}."routes_of_administration"
WHERE substance_id = ${subIdSQL(sub)}
ORDER BY route`;

const doseSQL = (sub: string, single: boolean) => `
WITH ${reportsCTE(sub, single)},
roa AS (
  SELECT *, replace(dose_units, 'μg', 'µg') AS u, count(*) OVER () AS n_routes
  FROM ${DB}."routes_of_administration"
  WHERE substance_id = ${subIdSQL(sub)}
    AND replace(dose_units, 'μg', 'µg') IN ('mg', 'g', 'µg') AND dose_common_min IS NOT NULL
),
d AS (
  SELECT d.report_id,
    CASE d.route WHEN 'IV' THEN 'intravenous' WHEN 'IM' THEN 'intramuscular' WHEN 'vaporized' THEN 'smoked' ELSE d.route END AS route_n,
    CASE lower(d.amount_unit) WHEN 'mg' THEN d.amount_value WHEN 'g' THEN d.amount_value * 1000
      WHEN 'ug' THEN d.amount_value / 1000 WHEN 'µg' THEN d.amount_value / 1000 END AS mg
  FROM ${DB}."report_doses" d JOIN sel USING (report_id)
  WHERE d.canonical_substance = '${esc(sub)}' AND d.amount_value > 0
),
dj AS (
  SELECT d.report_id, roa.route,
    sum(d.mg * CASE roa.u WHEN 'mg' THEN 1 WHEN 'g' THEN 0.001 ELSE 1000 END) AS total,
    any_value(roa.u) AS unit, any_value(roa.dose_threshold) AS thr, any_value(roa.dose_common_min) AS cmin,
    any_value(roa.dose_common_max) AS cmax, any_value(roa.dose_heavy) AS heavy
  FROM d JOIN roa ON roa.route = d.route_n OR (roa.n_routes = 1 AND d.route_n IS DISTINCT FROM roa.route)
  WHERE d.mg IS NOT NULL
  GROUP BY 1, 2
),
banded AS (
  SELECT report_id, route, unit, total,
    CASE WHEN total < thr THEN 0 WHEN total < cmin THEN 1 WHEN total <= cmax THEN 2
         WHEN heavy IS NULL OR total < heavy THEN 3 ELSE 4 END AS band
  FROM dj
),
val AS (
  SELECT report_id, avg(valence) AS v FROM ${DB}."timeline_sentiment"
  WHERE report_id IN (SELECT report_id FROM banded) GROUP BY 1
)
SELECT b.route, b.unit, b.band, count(*) AS n_reports, median(b.total) AS median_dose,
  avg(val.v) AS avg_valence, count(val.v) AS n_val,
  count(e.report_id) AS n_llm,
  100.0 * avg((e.overall_sentiment = 'negative')::INT) AS pct_neg
FROM banded b
LEFT JOIN val USING (report_id)
LEFT JOIN ${DB}."llm_extractions" e USING (report_id)
GROUP BY ALL ORDER BY b.route, b.band`;

const doseCoverageSQL = (sub: string, single: boolean) => `
WITH ${reportsCTE(sub, single)}
SELECT count(*) AS n_rows, count(DISTINCT d.report_id) AS n_rep,
  count(*) FILTER (WHERE d.amount_value > 0) AS n_numeric,
  count(*) FILTER (WHERE d.amount_value > 0 AND lower(d.amount_unit) IN ('mg','g','ug','µg')) AS n_mass
FROM ${DB}."report_doses" d JOIN sel USING (report_id)
WHERE d.canonical_substance = '${esc(sub)}'`;

const otherUnitsSQL = (sub: string) => `
SELECT amount_unit AS u, count(*) AS n FROM ${DB}."report_doses"
WHERE canonical_substance = '${esc(sub)}' AND amount_unit IS NOT NULL
  AND lower(amount_unit) NOT IN ('mg','g','ug','µg')
GROUP BY 1 ORDER BY 2 DESC LIMIT 5`;

const subsNamesCTE = `
subs AS (
  SELECT id, name,
    list_distinct(list_transform(coalesce(common_names, []), x -> lower(x)) || [lower(name)]
      || list_filter(list_transform(string_split(name, ' '), x -> lower(x)), x -> len(x) > 4)) AS names,
    list_transform(coalesce(chemical_class, []) || coalesce(psychoactive_class, []), x -> lower(x)) AS classes
  FROM ${DB}."substances"
)`;

// Interaction severity for each (a, b) pair produced by `prSelect`. Matches the interactions table by
// name, common name and class in both directions; substances missing from `substances` (e.g. Lithium)
// fall back to their own name.
const pairSevCTEs = (prSelect: string) => `${subsNamesCTE},
pr AS (${prSelect}),
px AS (
  SELECT pr.a, pr.b, sa.id AS a_id, sb.id AS b_id,
    coalesce(sa.names, [lower(pr.a)]) AS a_names, coalesce(sa.classes, []) AS a_cls,
    coalesce(sb.names, [lower(pr.b)]) AS b_names, coalesce(sb.classes, []) AS b_cls
  FROM pr LEFT JOIN subs sa ON sa.name = pr.a LEFT JOIN subs sb ON sb.name = pr.b
),
pairsev AS (
  SELECT px.a, px.b,
    max(CASE i.severity WHEN 'dangerous' THEN 3 WHEN 'unsafe' THEN 2 WHEN 'uncertain' THEN 1 END) AS sev,
    string_agg(DISTINCT i.interacts_with, ', ') AS via
  FROM px
  LEFT JOIN ${DB}."interactions" i ON
    (i.substance_id = px.a_id AND (list_contains(px.b_names, lower(i.interacts_with)) OR list_contains(px.b_names, rtrim(lower(i.interacts_with), 's'))
       OR list_contains(px.b_cls, lower(i.interacts_with)) OR list_contains(px.b_cls, rtrim(lower(i.interacts_with), 's'))))
    OR (i.substance_id = px.b_id AND (list_contains(px.a_names, lower(i.interacts_with)) OR list_contains(px.a_names, rtrim(lower(i.interacts_with), 's'))
       OR list_contains(px.a_cls, lower(i.interacts_with)) OR list_contains(px.a_cls, rtrim(lower(i.interacts_with), 's'))))
  GROUP BY 1, 2
)`;

const comboSQL = (sub: string) => `
WITH ${reportsCTE(sub, false)},
co AS (
  SELECT DISTINCT d.report_id, d.canonical_substance AS co_sub
  FROM ${DB}."report_doses" d JOIN sel USING (report_id)
  WHERE d.canonical_substance IS NOT NULL AND d.canonical_substance <> '${esc(sub)}'
),
val AS (
  SELECT report_id, avg(valence) AS v FROM ${DB}."timeline_sentiment"
  WHERE report_id IN (SELECT report_id FROM sel) GROUP BY 1
),
${pairSevCTEs(`SELECT DISTINCT '${esc(sub)}' AS a, co_sub AS b FROM co`)},
alone AS (
  SELECT 0 AS ord, 'Alone (single-substance reports)' AS co_sub, count(*) AS n_reports, NULL::INT AS sev, NULL AS via,
    avg(val.v) AS avg_valence, count(val.v) AS n_val, count(e.report_id) AS n_llm,
    100.0 * avg((e.overall_sentiment = 'negative')::INT) AS pct_neg
  FROM sel LEFT JOIN val USING (report_id) LEFT JOIN ${DB}."llm_extractions" e USING (report_id)
  WHERE sel.n_subs = 1
),
combos AS (
  SELECT 1 AS ord, co.co_sub, count(*) AS n_reports, any_value(ps.sev) AS sev, any_value(ps.via) AS via,
    avg(val.v) AS avg_valence, count(val.v) AS n_val, count(e.report_id) AS n_llm,
    100.0 * avg((e.overall_sentiment = 'negative')::INT) AS pct_neg
  FROM co LEFT JOIN pairsev ps ON ps.b = co.co_sub LEFT JOIN val USING (report_id)
  LEFT JOIN ${DB}."llm_extractions" e USING (report_id)
  GROUP BY co.co_sub
  ORDER BY n_reports DESC
  LIMIT 15
)
SELECT * FROM alone UNION ALL SELECT * FROM combos
ORDER BY ord, n_reports DESC`;

const interactionsSQL = (sub: string) => `
SELECT CASE severity WHEN 'dangerous' THEN 3 WHEN 'unsafe' THEN 2 ELSE 1 END AS sev,
  list(DISTINCT interacts_with ORDER BY interacts_with) AS items
FROM ${DB}."interactions"
WHERE substance_id = ${subIdSQL(sub)}
GROUP BY 1 ORDER BY 1 DESC`;

const compareSQL = `
WITH reports AS (
  SELECT r.id AS report_id, coalesce(m.canonical, r.substance) AS primary_sub,
         len(list_distinct(r.substances)) AS n_subs
  FROM ${DB}."experience_reports" r
  LEFT JOIN ${DB}."erowid_substance_map" m ON m.erowid_name = r.substance
),
obs AS (
  SELECT o.report_id, o.obs_index,
    TRY_CAST(regexp_extract(o.time_relative, '^T\\+~?(\\d+):(\\d{2})', 1) AS INTEGER)
      + TRY_CAST(regexp_extract(o.time_relative, '^T\\+~?(\\d+):(\\d{2})', 2) AS INTEGER) / 60.0 AS hrs
  FROM ${DB}."llm_observations" o
),
neff AS (
  SELECT o.report_id, o.hrs, count(e.effect_name) AS n_eff
  FROM obs o
  LEFT JOIN ${DB}."llm_observation_effects" e
    ON e.report_id = o.report_id AND e.obs_index = o.obs_index AND e.confidence <> 'low'
  WHERE o.hrs IS NOT NULL
  GROUP BY o.report_id, o.obs_index, o.hrs
),
per AS (
  SELECT report_id,
    min(hrs) FILTER (WHERE n_eff > 0) AS onset,
    arg_max(hrs, n_eff * 1000 - hrs) FILTER (WHERE hrs < 24) AS peak,
    max(hrs) FILTER (WHERE n_eff > 0 AND hrs < 24) AS last_eff
  FROM neff GROUP BY 1
)
SELECT primary_sub, count(*) AS n, median(onset) AS onset_h, median(peak) AS peak_h, median(last_eff) AS last_h
FROM per JOIN reports USING (report_id)
WHERE n_subs = 1 AND primary_sub IS NOT NULL AND primary_sub <> 'Unknown'
GROUP BY 1 HAVING count(*) >= 10
ORDER BY peak_h`;

// ── Mix explorer SQL ──────────────────────────────────────────────

const sqlList = (xs: string[]) => `[${xs.map((x) => `'${esc(x)}'`).join(', ')}]`;

// Reports grouped as 'Together' (contain every substance in the mix) or '<substance>' (that substance alone).
const mixGrpCTE = (mix: string[], exact: boolean) => `
rs AS (
  SELECT report_id, list(DISTINCT canonical_substance) AS subs
  FROM ${DB}."report_doses" WHERE canonical_substance IS NOT NULL GROUP BY 1
),
grp AS (
  SELECT report_id, 'Together' AS grp FROM rs
  WHERE list_has_all(subs, ${sqlList(mix)})${exact ? ` AND len(subs) = ${mix.length}` : ''}
  UNION ALL
  SELECT report_id, subs[1] AS grp FROM rs
  WHERE len(subs) = 1 AND list_contains(${sqlList(mix)}, subs[1])
)`;

const mixSelCTE = (mix: string[], exact: boolean) => `${mixGrpCTE(mix, exact)},
sel AS (SELECT report_id FROM grp WHERE grp = 'Together')`;

const mixOptionsSQL = `
SELECT canonical_substance AS s, count(DISTINCT report_id) AS n
FROM ${DB}."report_doses" WHERE canonical_substance IS NOT NULL
GROUP BY 1 HAVING count(DISTINCT report_id) >= 15
ORDER BY n DESC, s`;

const mixPairsSQL = (mix: string[]) => `
WITH ${pairSevCTEs(
	`SELECT a.x AS a, b.x AS b FROM unnest(${sqlList(mix)}) AS a(x), unnest(${sqlList(mix)}) AS b(x) WHERE a.x < b.x`,
)}
SELECT * FROM pairsev ORDER BY sev DESC NULLS LAST, a, b`;

const mixSummarySQL = (mix: string[], exact: boolean) => `
WITH ${mixGrpCTE(mix, exact)},
val AS (
  SELECT report_id, avg(valence) AS v FROM ${DB}."timeline_sentiment"
  WHERE report_id IN (SELECT report_id FROM grp) GROUP BY 1
)
SELECT g.grp, count(*) AS n_reports, count(e.report_id) AS n_llm,
  100.0 * avg((e.overall_sentiment = 'negative')::INT) AS pct_neg,
  100.0 * avg((e.overall_sentiment = 'positive')::INT) AS pct_pos,
  100.0 * avg(e.would_repeat::INT) AS pct_repeat, count(e.would_repeat) AS n_repeat,
  avg(val.v) AS avg_valence, count(val.v) AS n_val
FROM grp g LEFT JOIN val USING (report_id) LEFT JOIN ${DB}."llm_extractions" e USING (report_id)
GROUP BY g.grp ORDER BY g.grp = 'Together' DESC, g.grp`;

const mixEffectsSQL = (mix: string[], exact: boolean) => `
WITH ${mixGrpCTE(mix, exact)},
den AS (
  SELECT grp, count(DISTINCT report_id) AS n FROM grp
  WHERE report_id IN (SELECT report_id FROM ${DB}."llm_observations") GROUP BY grp
),
eff AS (
  SELECT DISTINCT g.grp, e.report_id, e.effect_name
  FROM grp g JOIN ${DB}."llm_observation_effects" e USING (report_id)
  WHERE e.confidence <> 'low'
),
shares AS (
  SELECT eff.grp, eff.effect_name, count(*) AS c, any_value(den.n) AS n, 100.0 * count(*) / any_value(den.n) AS share
  FROM eff JOIN den USING (grp) GROUP BY eff.grp, eff.effect_name
),
top AS (SELECT effect_name FROM shares WHERE grp = 'Together' ORDER BY share DESC, effect_name LIMIT 20)
SELECT s.grp, s.effect_name, s.share, s.c, s.n
FROM shares s JOIN top USING (effect_name)`;

const mixMoodSQL = (mix: string[], exact: boolean) => `
WITH ${mixGrpCTE(mix, exact)}
SELECT g.grp,
  CASE WHEN s.t_offset < 0.5 THEN 0 WHEN s.t_offset < 1 THEN 1 WHEN s.t_offset < 2 THEN 2 WHEN s.t_offset < 3 THEN 3
       WHEN s.t_offset < 4 THEN 4 WHEN s.t_offset < 6 THEN 5 WHEN s.t_offset < 8 THEN 6 WHEN s.t_offset < 12 THEN 7
       ELSE 8 END AS bin,
  avg(s.valence) AS v, count(DISTINCT s.report_id) AS n_rep
FROM grp g JOIN ${DB}."timeline_sentiment" s USING (report_id)
WHERE s.t_offset >= 0 AND s.t_offset < 24
GROUP BY ALL`;

const mixSeqSQL = (mix: string[], exact: boolean) => `
WITH ${mixGrpCTE(mix, exact)},
seq AS (
  SELECT d.report_id, d.canonical_substance AS sub, min(d.t_offset) AS t
  FROM ${DB}."report_doses" d JOIN grp g ON g.report_id = d.report_id AND g.grp = 'Together'
  WHERE list_contains(${sqlList(mix)}, d.canonical_substance)
  GROUP BY 1, 2
),
complete AS (SELECT report_id FROM seq WHERE t IS NOT NULL GROUP BY 1 HAVING count(*) = ${mix.length}),
ranked AS (
  SELECT s.*, s.t - min(s.t) OVER (PARTITION BY s.report_id) AS after_first,
    rank() OVER (PARTITION BY s.report_id ORDER BY s.t) AS rk
  FROM seq s JOIN complete USING (report_id)
)
SELECT sub, count(*) AS n, 100.0 * avg((rk = 1)::INT) AS pct_first,
  median(after_first) AS med_after, quantile_cont(after_first, 0.25) AS p25, quantile_cont(after_first, 0.75) AS p75
FROM ranked GROUP BY sub ORDER BY pct_first DESC, med_after`;

const mixExamplesSQL = (mix: string[], exact: boolean) => `
WITH ${mixGrpCTE(mix, exact)}
SELECT r.id AS report_id, r.experience_year AS yr, array_to_string(rs.subs, ', ') AS subs,
  e.overall_sentiment AS sentiment, left(regexp_replace(r.body, '\\s+', ' ', 'g'), 220) AS snippet
FROM grp g
JOIN rs USING (report_id)
JOIN ${DB}."experience_reports" r ON r.id = g.report_id
LEFT JOIN ${DB}."llm_extractions" e ON e.report_id = g.report_id
WHERE g.grp = 'Together'
ORDER BY e.report_id IS NULL, r.experience_year DESC NULLS LAST, r.id
LIMIT 30`;

// ── Small UI pieces ───────────────────────────────────────────────

function Skeleton({ h = 160 }: { h?: number }) {
	return <div className="animate-pulse rounded" style={{ height: h, background: C.skel }} />;
}

function ErrorBox({ q }: { q: { isError: boolean; error: unknown } }) {
	if (!q.isError) return null;
	return (
		<p role="alert" className="text-sm" style={{ color: C.negative }}>
			{String(q.error)}
		</p>
	);
}

function SectionTitle({ children, sub }: { children: React.ReactNode; sub?: React.ReactNode }) {
	return (
		<div className="mb-2">
			<h2 className="text-lg font-semibold" style={{ color: C.text, fontFamily: ROUNDED }}>
				{children}
			</h2>
			{sub && (
				<p className="text-sm" style={{ color: C.muted }}>
					{sub}
				</p>
			)}
		</div>
	);
}

function Kpi({ value, label, sub, loading }: { value: string; label: string; sub?: string; loading: boolean }) {
	return (
		<div>
			{loading ? (
				<div className="h-10 w-20 animate-pulse rounded" style={{ background: C.skel }} />
			) : (
				<p className="text-4xl font-bold" style={{ color: C.text, fontFamily: ROUNDED }}>
					{value}
				</p>
			)}
			<p className="mt-1 text-sm" style={{ color: C.muted }}>
				{label}
			</p>
			{sub && !loading && (
				<p className="text-sm" style={{ color: C.muted }}>
					{sub}
				</p>
			)}
		</div>
	);
}

function SevBadge({ sev }: { sev: number | null }) {
	if (!sev) return <span style={{ color: C.muted }}>none listed</span>;
	return (
		<span
			className="rounded px-2 py-0.5 text-sm font-medium"
			style={{ color: C.ink, background: SEV_COLOR[sev] }}
		>
			{SEV_LABEL[sev]}
		</span>
	);
}

function SentimentDot({ s }: { s: string }) {
	const color = s === 'positive' ? C.positive : s === 'negative' ? C.negative : s === 'mixed' ? C.mixed : C.neutral;
	return <span className="inline-block rounded-full" style={{ width: 8, height: 8, background: color }} title={s} />;
}

const th = 'py-2 pr-4 text-left text-sm font-semibold';
const td = 'py-2 pr-4 text-sm';

// ── Main ──────────────────────────────────────────────────────────

const TABS = [
	['forecast', 'What to expect', Sparkles, 'main'],
	['mix', 'Mixing', Shuffle, 'main'],
	['map', 'Moment by moment', LayoutGrid, 'main'],
	['profile', 'All effects', ListChecks, 'more'],
	['dose', 'Doses', Pill, 'more'],
	['combos', 'Often taken with', GitMerge, 'more'],
	['compare', 'Compare timings', Activity, 'more'],
	['notes', 'How we made this', BookOpen, 'more'],
] as const;

type EffectInfo = { name: string; blurb: string; url: string; category: string };
const EffectInfoCtx = createContext<Map<string, EffectInfo>>(new Map());
const useEffectInfo = (name: string) => useContext(EffectInfoCtx).get(name.toLowerCase());

function EffectBlurb({ name }: { name: string }) {
	const info = useEffectInfo(name);
	if (!info?.blurb) return null;
	return (
		<span className="mt-0.5 block text-sm" style={{ color: C.muted, lineHeight: 1.35 }}>
			{info.blurb}
		</span>
	);
}

function EffectExplainer({ name }: { name: string }) {
	const info = useEffectInfo(name);
	if (!info?.blurb) return null;
	return (
		<div
			className="mb-4 rounded-2xl border p-4 text-sm"
			style={{ borderColor: `rgba(${LAV},0.35)`, background: `rgba(${LAV},0.07)`, color: C.text }}
		>
			<span className="font-semibold" style={{ color: C.lavender }}>
				What is it?{' '}
			</span>
			{info.blurb}{' '}
			<a href={info.url} target="_blank" rel="noopener noreferrer" style={{ color: C.lavender, fontWeight: 600 }}>
				More on Effect Index ↗
			</a>
		</div>
	);
}

// A feeling's name with a tap-to-open explainer from the Subjective Effect Index.
function EffectName({ name }: { name: string }) {
	const info = useEffectInfo(name);
	const [open, setOpen] = useState(false);
	if (!info?.blurb) return <span>{name}</span>;
	return (
		<span className="relative" style={{ display: 'inline' }}>
			<button
				onClick={(e) => {
					e.stopPropagation();
					setOpen(!open);
				}}
				title={info.blurb}
				style={{
					color: 'inherit',
					textAlign: 'left',
					textDecoration: 'underline dotted',
					textDecorationColor: C.lavender,
					textUnderlineOffset: 3,
					cursor: 'help',
				}}
			>
				{name}
			</button>
			{open && (
				<span
					className="absolute rounded-2xl border p-3 text-sm"
					style={{
						left: 0,
						top: '1.6em',
						zIndex: 30,
						width: 300,
						display: 'block',
						background: C.card,
						borderColor: `rgba(${LAV},0.35)`,
						color: C.text,
						boxShadow: `0 12px 32px rgba(${LAV},0.22)`,
						whiteSpace: 'normal',
						fontWeight: 400,
					}}
					onClick={(e) => e.stopPropagation()}
				>
					<span style={{ display: 'block' }}>{info.blurb}</span>
					<a
						href={info.url}
						target="_blank"
						rel="noopener noreferrer"
						style={{ color: C.lavender, display: 'inline-block', marginTop: 6, fontWeight: 600 }}
					>
						More on Effect Index ↗
					</a>
				</span>
			)}
		</span>
	);
}

const LINE_COLORS =[C.cyan, C.lavender, C.coral, C.mixed, '#d16ba5'];

function MoodCell({ v, n }: { v: unknown; n: unknown }) {
	if (!N(n)) return <span style={{ color: C.muted }}>—</span>;
	const m = moodLabel(N(v));
	return (
		<span title={`Average text-sentiment ${N(v) >= 0 ? '+' : ''}${N(v).toFixed(2)} across ${N(n)} reports`} style={{ color: m.color }}>
			{m.text}
		</span>
	);
}

// Share of reports describing each window that mention each of the top effects — one line per effect.
function EffectLines({ heatQ, binQ, top = 5 }: { heatQ: Q; binQ: Q; top?: number }) {
	const { data, names } = useMemo(() => {
		const denom = new Map(rows(binQ).map((r) => [N(r.bin), N(r.n_rep)]));
		const totals = new Map<string, number>();
		const cells = new Map<string, number>();
		for (const r of rows(heatQ)) {
			totals.set(S(r.effect_name), N(r.n_total));
			cells.set(`${S(r.effect_name)}|${N(r.bin)}`, N(r.c));
		}
		const names = [...totals.entries()]
			.sort((a, b) => b[1] - a[1] || a[0].localeCompare(b[0]))
			.slice(0, top)
			.map(([n]) => n);
		const data = BINS.map((bin, b) => {
			const row: Record<string, string | number | null> = { bin };
			const d = denom.get(b) ?? 0;
			for (const n of names) row[n] = d >= MIN_N ? (100 * (cells.get(`${n}|${b}`) ?? 0)) / d : null;
			return row;
		});
		return { data, names };
		// eslint-disable-next-line react-hooks/exhaustive-deps
	}, [heatQ.data, binQ.data, top]);
	if (heatQ.isLoading || binQ.isLoading) return <Skeleton h={240} />;
	if (names.length === 0)
		return (
			<p className="text-sm" style={{ color: C.muted }}>
				No annotated effects for this selection.
			</p>
		);
	return (
		<ResponsiveContainer width="100%" height={240}>
			<LineChart data={data} margin={{ top: 4, right: 8, left: -12, bottom: 0 }}>
				<CartesianGrid strokeDasharray="3 3" stroke={C.grid} vertical={false} />
				<XAxis dataKey="bin" fontSize={13} tickLine={false} stroke={C.muted} />
				<YAxis fontSize={13} tickLine={false} stroke={C.muted} tickFormatter={(v) => `${v}%`} />
				<Tooltip {...tipStyle} formatter={(v: number, name: string) => [`${Math.round(v)}% of reports`, name]} />
				<Legend wrapperStyle={{ fontSize: 13 }} />
				{names.map((n, i) => (
					<Line
						key={n}
						type="linear"
						dataKey={n}
						stroke={LINE_COLORS[i % LINE_COLORS.length]}
						strokeWidth={2.5}
						dot={{ r: 2.5, strokeWidth: 0, fill: LINE_COLORS[i % LINE_COLORS.length] }}
						connectNulls={false}
					/>
				))}
			</LineChart>
		</ResponsiveContainer>
	);
}

// Kicks in → peaks → wears off, drawn to scale with the middle-half ranges shaded.
function TimelineStrip({ k }: { k: Record<string, unknown> }) {
	const marks = [
		{ key: 'onset', label: 'Kicks in', color: C.lavender },
		{ key: 'peak', label: 'Peaks', color: C.cyan },
		{ key: 'last', label: 'Wears off', color: C.coral },
	];
	const max = Math.max(1, N(k.last_p75), N(k.peak_p75), N(k.last_h)) * 1.08;
	const pos = (h: number) => `${Math.min(100, (100 * h) / max)}%`;
	const ticks = [0, 0.25, 0.5, 1, 2, 3, 4, 6, 8, 12, 16, 24].filter((t) => t <= max);
	const tickStep = ticks.length > 7 ? 2 : 1;
	return (
		<div className="mt-4">
			<div className="relative rounded-full" style={{ height: 14, background: C.skel }}>
				<div
					className="absolute rounded-full"
					style={{
						left: 0,
						width: pos(N(k.last_h)),
						top: 0,
						bottom: 0,
						background: `linear-gradient(90deg, ${C.lavender}, ${C.cyan} 50%, ${C.coral})`,
						opacity: 0.85,
					}}
				/>
				{marks.map((m) => {
					const lo = N(k[`${m.key}_p25`]);
					const hi = N(k[`${m.key}_p75`]);
					return (
						<div
							key={m.key}
							className="absolute rounded-full"
							title={`${m.label}: middle half ${fmtH(lo)}–${fmtH(hi)}`}
							style={{ left: pos(lo), width: `calc(${pos(hi)} - ${pos(lo)})`, top: -4, bottom: -4, border: `2px solid ${m.color}`, background: 'transparent' }}
						/>
					);
				})}
				{marks.map((m) => (
					<div
						key={`${m.key}-dot`}
						className="absolute rounded-full"
						style={{
							left: `calc(${pos(N(k[`${m.key}_h`]))} - 7px)`,
							top: -1,
							width: 16,
							height: 16,
							background: m.color,
							border: `3px solid ${C.bg}`,
						}}
					/>
				))}
			</div>
			<div className="relative mt-1" style={{ height: 16 }}>
				{ticks
					.filter((_, i) => i % tickStep === 0)
					.map((t) => (
						<span key={t} className="absolute text-sm" style={{ left: pos(t), transform: 'translateX(-50%)', color: C.muted }}>
							{t === 0 ? 'dose' : fmtH(t)}
						</span>
					))}
			</div>
		</div>
	);
}

function YearsChart({ q }: { q: Q }) {
	const data = rows(q).map((r) => ({ yr: String(N(r.yr)), Reports: N(r.n) }));
	if (q.isLoading) return <Skeleton h={160} />;
	return (
		<ResponsiveContainer width="100%" height={160}>
			<AreaChart data={data} margin={{ top: 4, right: 8, left: -20, bottom: 0 }}>
				<defs>
					<linearGradient id="yrFill" x1="0" y1="0" x2="0" y2="1">
						<stop offset="0%" stopColor={C.lavender} stopOpacity={0.6} />
						<stop offset="100%" stopColor={C.lavender} stopOpacity={0.05} />
					</linearGradient>
				</defs>
				<CartesianGrid strokeDasharray="3 3" stroke={C.grid} vertical={false} />
				<XAxis dataKey="yr" fontSize={13} tickLine={false} stroke={C.muted} interval={4} />
				<YAxis fontSize={13} tickLine={false} stroke={C.muted} allowDecimals={false} />
				<Tooltip {...tipStyle} />
				<Area type="linear" dataKey="Reports" stroke={C.lavender} strokeWidth={2} fill="url(#yrFill)" />
			</AreaChart>
		</ResponsiveContainer>
	);
}

export default function EffectForecastDive() {
	const [sub, setSub] = useDiveState<string>('substance', 'LSD');
	const [single, setSingle] = useDiveState<boolean>('singleOnly', false);
	const [tab, setTab] = useDiveState<string>('tab', 'forecast');
	const [effect, setEffect] = useDiveState<string | null>('effect', null);
	const [mix, setMix] = useDiveState<string[]>('mix', ['LSD', 'Cannabis']);
	const [mixExact, setMixExact] = useDiveState<boolean>('mixExact', false);
	const isMix = tab === 'mix';
	const mixReady = isMix && mix.length >= 2;
	const mixObs = obsFrom(mixSelCTE(mix, mixExact));

	const listQ = useSQLQuery(substanceListSQL);
	const effectInfoQ = useSQLQuery(effectInfoSQL);
	const effectInfo = useMemo(
		() =>
			new Map(
				rows(effectInfoQ).map((r) => [
					S(r.k),
					{ name: S(r.name), blurb: S(r.blurb), url: S(r.url), category: S(r.category) },
				]),
			),
		[effectInfoQ.data],
	);
	const subList = rows(listQ);

	const kpiQ = useSQLQuery(kpiSQL(sub, single));
	const k = rows(kpiQ)[0] ?? {};
	const profileQ = useSQLQuery(profileSQL(sub, single));
	const phaseDenomQ = useSQLQuery(phaseDenomSQL(obsCTE(sub, single)), { enabled: tab === 'forecast' });
	const phaseEffQ = useSQLQuery(phaseEffSQL(obsCTE(sub, single)), { enabled: tab === 'forecast' });
	const binQ = useSQLQuery(binSQL(obsCTE(sub, single)), { enabled: tab === 'forecast' || tab === 'map' });
	const heatQ = useSQLQuery(heatSQL(obsCTE(sub, single)), { enabled: tab === 'forecast' || tab === 'map' });
	const yearsQ = useSQLQuery(yearsSQL(sub, single), { enabled: tab === 'forecast' });
	const examplesQ = useSQLQuery(examplesSQL(sub, single, effect ?? ''), { enabled: tab === 'map' && !!effect });
	const refMissingQ = useSQLQuery(refMissingSQL(sub, single), { enabled: tab === 'profile' });
	const roaQ = useSQLQuery(roaSQL(sub), { enabled: tab === 'forecast' || tab === 'dose' });
	const doseQ = useSQLQuery(doseSQL(sub, single), { enabled: tab === 'dose' });
	const doseCovQ = useSQLQuery(doseCoverageSQL(sub, single), { enabled: tab === 'dose' });
	const otherUnitsQ = useSQLQuery(otherUnitsSQL(sub), { enabled: tab === 'dose' });
	const comboQ = useSQLQuery(comboSQL(sub), { enabled: tab === 'combos' });
	const interQ = useSQLQuery(interactionsSQL(sub), { enabled: tab === 'combos' || tab === 'forecast' });
	const compareQ = useSQLQuery(compareSQL, { enabled: tab === 'compare' });

	const mixQ = {
		options: useSQLQuery(mixOptionsSQL, { enabled: isMix }),
		pairs: useSQLQuery(mixPairsSQL(mix), { enabled: mixReady }),
		summary: useSQLQuery(mixSummarySQL(mix, mixExact), { enabled: mixReady }),
		effects: useSQLQuery(mixEffectsSQL(mix, mixExact), { enabled: mixReady }),
		mood: useSQLQuery(mixMoodSQL(mix, mixExact), { enabled: mixReady }),
		seq: useSQLQuery(mixSeqSQL(mix, mixExact), { enabled: mixReady }),
		examples: useSQLQuery(mixExamplesSQL(mix, mixExact), { enabled: mixReady }),
		phaseDenom: useSQLQuery(phaseDenomSQL(mixObs), { enabled: mixReady }),
		phaseEff: useSQLQuery(phaseEffSQL(mixObs), { enabled: mixReady }),
		bins: useSQLQuery(binSQL(mixObs), { enabled: mixReady }),
		heat: useSQLQuery(heatSQL(mixObs), { enabled: mixReady }),
	};
	const openMix = (subs: string[]) => {
		setMix(subs);
		setTab('mix');
	};

	const current = subList.find((r) => S(r.primary_sub) === sub);
	const nLlm = N(k.n_llm);
	const lowSample = !kpiQ.isLoading && nLlm < 20;

	const changeSub = (v: string) => {
		setSub(v);
		setEffect(undefined as unknown as null);
	};

	return (
		<EffectInfoCtx.Provider value={effectInfo}>
		<main className="p-6" style={{ background: PAGE_BG, color: C.text, minHeight: '100%' }}>
			{/* Header */}
			<div>
				<h1 className="text-3xl" style={{ lineHeight: 1.1 }}>
					<span style={{ fontFamily: MONO, color: C.coral, fontWeight: 700 }}>re</span>
					<span style={{ color: C.cyan, fontWeight: 700 }}>:</span>
					<span style={{ fontFamily: ROUNDED, color: C.coral, fontWeight: 600 }}>dose</span>
					<span style={{ color: C.cyan, fontSize: 22, marginLeft: 4 }}>♡</span>
					<span className="text-xl" style={{ fontFamily: ROUNDED, color: C.lavender, fontWeight: 600, marginLeft: 12 }}>
						what to expect
					</span>
				</h1>
				<p className="mt-1 text-sm" style={{ color: C.muted }}>
					Curious what something might feel like — and when? We read through {(13619).toLocaleString()} real trip
					reports so you can see what people actually went through. No judgement, just honest info ♡
				</p>
			</div>

			{/* Tabs */}
			<nav className="mt-4 flex flex-wrap gap-2">
				{TABS.filter((t) => t[3] === 'main').map(([id, label, Icon]) => {
					const active = tab === id;
					return (
						<button
							key={id}
							onClick={() => setTab(id)}
							className="flex items-center gap-2 rounded-full px-4 py-2 text-base"
							style={{
								background: active ? C.lavender : C.lavSoft,
								color: active ? C.ink : C.text,
								fontWeight: active ? 600 : 500,
								boxShadow: active ? `0 0 0 3px rgba(${LAV},0.25)` : 'none',
								transition: 'background 120ms',
							}}
						>
							<Icon size={14} />
							{label}
						</button>
					);
				})}
			</nav>
			<div className="mt-2 flex flex-wrap items-center gap-1.5 text-sm">
				<span style={{ color: C.muted }}>Dig deeper:</span>
				{TABS.filter((t) => t[3] === 'more').map(([id, label, Icon]) => {
					const active = tab === id;
					return (
						<button
							key={id}
							onClick={() => setTab(id)}
							className="flex items-center gap-1 rounded-full border px-2.5 py-0.5 text-sm"
							style={{
								borderColor: active ? C.lavender : C.line,
								background: active ? C.lavSoft : 'transparent',
								color: active ? C.lavender : C.muted,
								fontWeight: active ? 600 : 400,
							}}
						>
							<Icon size={13} />
							{label}
						</button>
					);
				})}
			</div>

			{/* Substance picker */}
			{!isMix && (
				<div
					className="mt-4 rounded-2xl border p-4"
					style={{ borderColor: `rgba(${LAV},0.35)`, background: `linear-gradient(180deg, rgba(${LAV},0.10), ${C.card})` }}
				>
					<div className="flex flex-wrap items-center gap-3">
						<span className="text-sm font-semibold" style={{ fontFamily: ROUNDED }}>
							What are you curious about?
						</span>
						<select
							className="border text-sm"
							style={{ background: C.bg, color: C.text, borderColor: C.lavender, borderRadius: 999, padding: '6px 14px', fontWeight: 600 }}
							value={sub}
							onChange={(e) => changeSub(e.target.value)}
						>
							{!subList.some((r) => S(r.primary_sub) === sub) && <option value={sub}>{sub}</option>}
							{subList.map((r) => (
								<option key={S(r.primary_sub)} value={S(r.primary_sub)}>
									{S(r.primary_sub)} — {N(r.n_llm)} stories
								</option>
							))}
						</select>
						<div className="flex flex-wrap gap-1.5">
							{subList.slice(0, 8).map((r) => {
								const name = S(r.primary_sub);
								const on = name === sub;
								return (
									<button
										key={name}
										onClick={() => changeSub(name)}
										className="rounded-full border px-2.5 py-0.5 text-sm"
										style={{
											borderColor: on ? C.lavender : C.line,
											background: on ? C.lavSoft : 'transparent',
											color: on ? C.lavender : C.muted,
										}}
									>
										{name}
									</button>
								);
							})}
						</div>
						<label className="ml-auto flex cursor-pointer items-center gap-2 text-sm" style={{ color: C.muted }}>
							<input
								type="checkbox"
								checked={single}
								onChange={(e) => setSingle(e.target.checked)}
								style={{ width: 14, height: 14, accentColor: C.lavender }}
							/>
							Only stories where it was taken on its own
						</label>
					</div>

					{/* KPIs */}
					<div className="mt-4 grid grid-cols-4 gap-8">
						<Kpi
							loading={kpiQ.isLoading}
							value={fmtH(k.onset_h as number)}
							label="Kicks in"
							sub={`usually ${fmtH(k.onset_p25 as number)} – ${fmtH(k.onset_p75 as number)}`}
						/>
						<Kpi
							loading={kpiQ.isLoading}
							value={fmtH(k.peak_h as number)}
							label="Strongest around"
							sub={`usually ${fmtH(k.peak_p25 as number)} – ${fmtH(k.peak_p75 as number)}`}
						/>
						<Kpi
							loading={kpiQ.isLoading}
							value={fmtH(k.last_h as number)}
							label="Mostly done by"
							sub={`last effect noted · usually ${fmtH(k.last_p25 as number)} – ${fmtH(k.last_p75 as number)}`}
						/>
						<Kpi
							loading={kpiQ.isLoading}
							value={nLlm.toLocaleString()}
							label="Stories we read closely"
							sub={`out of ${N(k.n_reports).toLocaleString()} reports about it`}
						/>
					</div>
					{!kpiQ.isLoading && nLlm > 0 && <TimelineStrip k={k} />}
					<ErrorBox q={kpiQ} />
					{lowSample && (
						<p className="mt-3 flex items-center gap-2 text-sm" style={{ color: C.mixed }}>
							<AlertTriangle size={14} /> Only {nLlm} stories so far — take these patterns with a pinch of salt 🧂
						</p>
					)}
				</div>
			)}

			<div className="mt-6">
				{tab === 'forecast' && (
					<ForecastTab
						sub={sub}
						k={k}
						kpiLoading={kpiQ.isLoading}
						phaseDenomQ={phaseDenomQ}
						phaseEffQ={phaseEffQ}
						binQ={binQ}
						heatQ={heatQ}
						yearsQ={yearsQ}
						profileQ={profileQ}
						roaQ={roaQ}
						interQ={interQ}
					/>
				)}
				{tab === 'map' && (
					<MapTab
						heatQ={heatQ}
						binQ={binQ}
						examplesQ={examplesQ}
						effect={effect}
						setEffect={setEffect}
					/>
				)}
				{tab === 'profile' && <ProfileTab profileQ={profileQ} refMissingQ={refMissingQ} sub={sub} />}
				{tab === 'dose' && (
					<DoseTab doseQ={doseQ} doseCovQ={doseCovQ} otherUnitsQ={otherUnitsQ} roaQ={roaQ} sub={sub} />
				)}
				{tab === 'mix' && (
					<MixTab
						mix={mix}
						setMix={setMix}
						exact={mixExact}
						setExact={setMixExact}
						q={mixQ}
					/>
				)}
				{tab === 'combos' && <CombosTab comboQ={comboQ} interQ={interQ} sub={sub} openMix={openMix} />}
				{tab === 'compare' && <CompareTab compareQ={compareQ} sub={sub} setSub={changeSub} setTab={setTab} />}
				{tab === 'notes' && <NotesTab current={current} />}
			</div>

			<footer
				className="mt-10 rounded-3xl border p-5"
				style={{
					borderColor: `rgba(${LAV},0.35)`,
					background: `linear-gradient(135deg, rgba(${LAV},0.12), rgba(18,169,184,0.08) 60%, rgba(238,79,116,0.08))`,
					color: C.text,
				}}
			>
				<p className="text-lg font-semibold" style={{ fontFamily: ROUNDED }}>
					<span style={{ color: C.coral }}>♡</span> Looking after yourself
				</p>
				<ul className="mt-2 grid grid-cols-2 gap-x-6 gap-y-1 text-sm">
					<li>🐢 Start low, go slow — you can always take more, never less.</li>
					<li>🔀 Check a mix before you mix (the Mixing tab can help).</li>
					<li>🧑‍🤝‍🧑 Have someone you trust around, and don't use alone.</li>
					<li>🧪 Test what you've got if you can.</li>
					<li>💧 Sip water, eat something, rest after.</li>
					<li>🛋️ Set and setting matter as much as the substance.</li>
				</ul>
				<p className="mt-3 text-sm">
					Want to talk it through?{' '}
					<a
						href="https://discord.com/invite/WzWc5MQSnW"
						target="_blank"
						rel="noopener noreferrer"
						style={{ color: C.lavender, fontWeight: 600 }}
					>
						Come say hi in the re:dose Discord ↗
					</a>
				</p>
				<p className="mt-2 text-sm" style={{ color: C.muted }}>
					Everything here comes from people's own stories — it's not medical advice or a prediction for you.
				</p>
			</footer>
		</main>
		</EffectInfoCtx.Provider>
	);
}

// ── Forecast tab ──────────────────────────────────────────────────

type Q = ReturnType<typeof useSQLQuery>;

function PhaseCards({ denomQ, effQ }: { denomQ: Q; effQ: Q }) {
	const denoms = new Map(rows(denomQ).map((r) => [N(r.phase), r]));
	const effByPhase = new Map<number, { name: string; c: number }[]>();
	for (const r of rows(effQ)) {
		const p = N(r.phase);
		if (!effByPhase.has(p)) effByPhase.set(p, []);
		effByPhase.get(p)!.push({ name: S(r.effect_name), c: N(r.c) });
	}
	if (effQ.isLoading || denomQ.isLoading) return <Skeleton h={220} />;
	return (
		<>
			<div className="grid grid-cols-3 gap-4">
				{PHASES.map((label, p) => {
					const d = denoms.get(p);
					const nRep = N(d?.n_rep);
					const effs = effByPhase.get(p) ?? [];
					return (
						<div key={label} className="rounded-2xl border p-4" style={{ borderColor: C.line, background: C.card, boxShadow: `0 6px 20px rgba(${LAV},0.10)` }}>
							<div className="flex items-baseline justify-between">
								<p className="text-sm font-semibold">{label}</p>
								<p className="text-sm" style={{ color: C.muted }}>
									{nRep} stories
								</p>
							</div>
							{nRep < MIN_N ? (
								<p className="mt-2 text-sm" style={{ color: C.muted }}>
									Not many people wrote about this part yet.
								</p>
							) : (
								<>
									<ul className="mt-2 space-y-1">
										{effs.map((e) => (
											<li key={e.name} className="text-sm">
												<div className="flex justify-between gap-2">
													<span><EffectName name={e.name} /></span>
													<span style={{ color: C.muted }}>{Math.round((100 * e.c) / nRep)}%</span>
												</div>
												<div className="mt-0.5 rounded" style={{ height: 3, background: C.line }}>
													<div
														className="rounded"
														style={{
															height: 3,
															width: `${Math.min(100, (100 * e.c) / nRep)}%`,
															background: C.primary,
														}}
													/>
												</div>
											</li>
										))}
									</ul>
									<p className="mt-2 text-sm" style={{ color: C.muted }}>
										{Math.round(N(d?.pct_neg))}% of moments felt rough
									</p>
								</>
							)}
						</div>
					);
				})}
			</div>
			<ErrorBox q={effQ} />
		</>
	);
}

function ForecastTab({
	sub,
	k,
	kpiLoading,
	phaseDenomQ,
	phaseEffQ,
	binQ,
	heatQ,
	yearsQ,
	profileQ,
	roaQ,
	interQ,
}: {
	sub: string;
	k: Record<string, unknown>;
	kpiLoading: boolean;
	phaseDenomQ: Q;
	phaseEffQ: Q;
	binQ: Q;
	heatQ: Q;
	yearsQ: Q;
	profileQ: Q;
	roaQ: Q;
	interQ: Q;
}) {
	const sentData = rows(binQ).map((r) => {
		const total = N(r.n_obs) || 1;
		return {
			bin: BINS[N(r.bin)],
			n: N(r.n_obs),
			rep: N(r.n_rep),
			Good: (100 * N(r.pos)) / total,
			Mixed: (100 * N(r.mixed)) / total,
			Neutral: (100 * N(r.neu)) / total,
			Rough: (100 * N(r.neg)) / total,
		};
	});
	const difficult = rows(profileQ)
		.filter((r) => N(r.pct_neg) >= 60 && N(r.n_rep) >= 3)
		.slice(0, 6);
	const dangerous = rows(interQ).find((r) => N(r.sev) === 3);
	const dangerousItems = L(dangerous?.items);

	return (
		<div className="flex flex-col gap-6">
			{/* Outcome strip */}
			<div className="flex flex-wrap gap-6 text-sm">
				{kpiLoading ? (
					<div className="h-5 w-96 animate-pulse rounded" style={{ background: C.skel }} />
				) : (
					<>
						<span>
							<b style={{ color: C.positive }}>{pct(k.pct_pos)}</b> had a good time
						</span>
						<span>
							<b style={{ color: C.mixed }}>{pct(k.pct_mixed)}</b> mixed
						</span>
						<span>
							<b style={{ color: C.negative }}>{pct(k.pct_neg)}</b> had a rough time
						</span>
						<span>
							<b>{pct(k.pct_repeat)}</b> would do it again{' '}
							<span style={{ color: C.muted }}>(of {N(k.n_repeat)} who said)</span>
						</span>
					</>
				)}
			</div>

			{/* Phase cards */}
			<div>
				<SectionTitle sub="What people mention most at each stage — the % is how many of the stories about that stage mention it">
					<span className="inline-flex items-center gap-2">
						<Clock size={18} /> How it usually unfolds
					</span>
				</SectionTitle>
				<PhaseCards denomQ={phaseDenomQ} effQ={phaseEffQ} />
			</div>

			{/* Effects over time */}
			<div>
				<SectionTitle sub="The five things people mention most, and how they come and go hour by hour">
					How the main feelings rise and fall
				</SectionTitle>
				<EffectLines heatQ={heatQ} binQ={binQ} />
				<ErrorBox q={heatQ} />
			</div>

			{/* Sentiment over time */}
			<div>
				<SectionTitle sub="How people described feeling at each point — good, mixed or rough">
					How it feels along the way
				</SectionTitle>
				{binQ.isLoading ? (
					<Skeleton h={220} />
				) : (
					<ResponsiveContainer width="100%" height={220}>
						<AreaChart data={sentData} margin={{ top: 4, right: 8, left: -12, bottom: 0 }} stackOffset="expand">
							<CartesianGrid strokeDasharray="3 3" stroke={C.grid} vertical={false} />
							<XAxis dataKey="bin" fontSize={13} tickLine={false} stroke={C.muted} />
							<YAxis fontSize={13} tickFormatter={(v) => `${Math.round(v * 100)}%`} tickLine={false} stroke={C.muted} />
							<Tooltip
								{...tipStyle}
								formatter={(v: number, name: string) => [`${v.toFixed(0)}%`, name]}
								labelFormatter={(l, p) => {
									const row = p?.[0]?.payload;
									return row ? `${l} · ${row.n} moments from ${row.rep} reports` : l;
								}}
							/>
							<Legend wrapperStyle={{ fontSize: 13 }} />
							<Area type="linear" dataKey="Good" stackId="s" stroke={C.positive} fill={C.positive} fillOpacity={0.75} />
							<Area type="linear" dataKey="Mixed" stackId="s" stroke={C.mixed} fill={C.mixed} fillOpacity={0.75} />
							<Area type="linear" dataKey="Neutral" stackId="s" stroke={C.neutral} fill={C.neutral} fillOpacity={0.75} />
							<Area type="linear" dataKey="Rough" stackId="s" stroke={C.negative} fill={C.negative} fillOpacity={0.75} />
						</AreaChart>
					</ResponsiveContainer>
				)}
				<ErrorBox q={binQ} />
			</div>

			<div className="grid grid-cols-2 gap-6">
				{/* Difficult effects */}
				<div>
					<SectionTitle sub="Feelings people mostly found unpleasant — good to know about ahead of time">
						Things that can feel hard
					</SectionTitle>
					{profileQ.isLoading ? (
						<Skeleton h={140} />
					) : difficult.length === 0 ? (
						<p className="text-sm" style={{ color: C.muted }}>
							None reach the threshold in this sample.
						</p>
					) : (
						<table className="w-full">
							<thead style={{ color: C.muted }}>
								<tr>
									<th className={th}>Feeling</th>
									<th className={th}>How often</th>
									<th className={th}>Usually around</th>
								</tr>
							</thead>
							<tbody>
								{difficult.map((r) => (
									<tr key={S(r.effect_name)} className="border-t" style={{ borderColor: C.line }}>
										<td className={td}><EffectName name={S(r.effect_name)} /></td>
										<td className={td}>{pct(r.pct_reports)}</td>
										<td className={td}>{fmtH(r.med_first_h as number)}</td>
									</tr>
								))}
							</tbody>
						</table>
					)}
				</div>

				{/* Reference + safety */}
				<div className="flex flex-col gap-4">
					<div>
						<SectionTitle sub="From PsychonautWiki. Their numbers came without units — usually minutes for onset, hours for the rest">
							What the reference says
						</SectionTitle>
						{roaQ.isLoading ? (
							<Skeleton h={80} />
						) : rows(roaQ).length === 0 ? (
							<p className="text-sm" style={{ color: C.muted }}>
								No reference data for {sub}.
							</p>
						) : (
							<table className="w-full">
								<thead style={{ color: C.muted }}>
									<tr>
										<th className={th}>Route</th>
										<th className={th}>Onset</th>
										<th className={th}>Peak</th>
										<th className={th}>Total</th>
									</tr>
								</thead>
								<tbody>
									{rows(roaQ).map((r) => (
										<tr key={S(r.route)} className="border-t" style={{ borderColor: C.line }}>
											<td className={td}>{S(r.route)}</td>
											<td className={td}>{S(r.duration_onset) || '—'}</td>
											<td className={td}>{S(r.duration_peak) || '—'}</td>
											<td className={td}>{S(r.duration_total) || '—'}</td>
										</tr>
									))}
								</tbody>
							</table>
						)}
					</div>
					{dangerousItems.length > 0 && (
						<div className="rounded-xl border p-3" style={{ borderColor: C.negative, background: C.card }}>
							<p className="flex items-center gap-2 text-sm font-semibold" style={{ color: C.negative }}>
								<AlertTriangle size={14} /> Please don't mix with
							</p>
							<p className="mt-1 text-sm">{dangerousItems.join(', ')}</p>
						</div>
					)}
				</div>
			</div>

			{/* Reports per year */}
			<div>
				<SectionTitle sub="Stories by the year they were shared on Erowid">When people wrote these</SectionTitle>
				<YearsChart q={yearsQ} />
				<ErrorBox q={yearsQ} />
			</div>
		</div>
	);
}

// ── Effect map tab ────────────────────────────────────────────────

function MapTab({
	heatQ,
	binQ,
	examplesQ,
	effect,
	setEffect,
}: {
	heatQ: Q;
	binQ: Q;
	examplesQ: Q;
	effect: string | null;
	setEffect: (v: string | null) => void;
}) {
	const binDenom = new Map(rows(binQ).map((r) => [N(r.bin), N(r.n_rep)]));
	const { effects, cells, maxPct } = useMemo(() => {
		const byEffect = new Map<string, { total: number; bins: Map<number, number> }>();
		for (const r of rows(heatQ)) {
			const name = S(r.effect_name);
			if (!byEffect.has(name)) byEffect.set(name, { total: N(r.n_total), bins: new Map() });
			byEffect.get(name)!.bins.set(N(r.bin), N(r.c));
		}
		const effects = [...byEffect.entries()].sort((a, b) => b[1].total - a[1].total || a[0].localeCompare(b[0]));
		let maxPct = 0;
		const cells = new Map<string, number>();
		for (const [name, v] of effects) {
			for (let b = 0; b < BINS.length; b++) {
				const d = binDenom.get(b) ?? 0;
				if (d < MIN_N) continue;
				const p = (100 * (v.bins.get(b) ?? 0)) / d;
				cells.set(`${name}|${b}`, p);
				maxPct = Math.max(maxPct, p);
			}
		}
		return { effects, cells, maxPct: maxPct || 1 };
		// eslint-disable-next-line react-hooks/exhaustive-deps
	}, [heatQ.data, binQ.data]);

	return (
		<div className="flex flex-col gap-6">
			<div>
				<SectionTitle sub="How often each feeling comes up at each point in the trip — darker means more people mentioned it. Tap a feeling to read what people wrote.">
					Moment by moment
				</SectionTitle>
				{heatQ.isLoading || binQ.isLoading ? (
					<Skeleton h={420} />
				) : effects.length === 0 ? (
					<p className="text-sm" style={{ color: C.muted }}>
						No annotated effects for this selection.
					</p>
				) : (
					<div style={{ overflowX: 'auto' }}>
						<table style={{ borderCollapse: 'separate', borderSpacing: 2, width: '100%' }}>
							<thead>
								<tr>
									<th />
									{BINS.map((b, i) => (
										<th key={b} className="text-center text-sm font-normal" style={{ color: C.muted }}>
											{b}
											<div style={{ fontSize: 10, opacity: 0.8 }}>n={binDenom.get(i) ?? 0}</div>
										</th>
									))}
								</tr>
							</thead>
							<tbody>
								{effects.map(([name, v]) => (
									<tr key={name}>
										<td
											className="cursor-pointer pr-2 text-sm"
											style={{
												color: effect === name ? C.primary : C.text,
												fontWeight: effect === name ? 600 : 400,
												whiteSpace: 'nowrap',
												maxWidth: 190,
												overflow: 'hidden',
												textOverflow: 'ellipsis',
											}}
											title={`${name} — mentioned in ${v.total} reports`}
											onClick={() => setEffect(effect === name ? (undefined as unknown as null) : name)}
										>
											{name}
										</td>
										{BINS.map((_, b) => {
											const p = cells.get(`${name}|${b}`);
											if (p == null)
												return (
													<td
														key={b}
														className="text-center text-sm"
														style={{ background: '#f6f3fc', color: C.neutral, height: 22, borderRadius: 3 }}
														title="Too few reports describe this window"
													>
														·
													</td>
												);
											const a = p / maxPct;
											return (
												<td
													key={b}
													className="cursor-pointer text-center text-sm"
													style={{
														background: p === 0 ? C.skel : `rgba(${LAV},${0.15 + 0.85 * a})`,
														color: a > 0.6 ? C.ink : C.text,
														borderRadius: 3,
														height: 22,
														minWidth: 40,
													}}
													title={`${name} · ${BINS[b]}: ${p.toFixed(0)}% of ${binDenom.get(b)} reports`}
													onClick={() => setEffect(name)}
												>
													{p >= 1 ? Math.round(p) : ''}
												</td>
											);
										})}
									</tr>
								))}
							</tbody>
						</table>
					</div>
				)}
				<ErrorBox q={heatQ} />
			</div>

			{effect && (
				<div>
					<SectionTitle sub="In their own words (lightly paraphrased), in the order it happened">
						What people said about “{effect}”
					</SectionTitle>
					<EffectExplainer name={effect} />
					{examplesQ.isLoading ? (
						<Skeleton h={160} />
					) : (
						<div className="flex flex-col gap-2" style={{ maxHeight: 360, overflowY: 'auto' }}>
							{rows(examplesQ).map((r, i) => {
								const id = S(r.report_id).replace('erowid-', '');
								return (
									<div key={i} className="flex gap-3 border-t pt-2 text-sm" style={{ borderColor: C.line }}>
										<div className="flex shrink-0 items-start gap-2" style={{ width: 92 }}>
											<SentimentDot s={S(r.sentiment)} />
											<span className="text-sm" style={{ color: C.muted }}>
												{S(r.time_relative)}
											</span>
										</div>
										<p className="flex-1">{S(r.description)}</p>
										<a
											href={`https://erowid.org/experiences/exp.php?ID=${id}`}
											target="_blank"
											rel="noopener noreferrer"
											className="shrink-0 text-sm"
											style={{ color: C.primary }}
										>
											Read the full story ↗
										</a>
									</div>
								);
							})}
						</div>
					)}
					<ErrorBox q={examplesQ} />
				</div>
			)}
		</div>
	);
}

// ── Effect profile tab ────────────────────────────────────────────

function ProfileTab({ profileQ, refMissingQ, sub }: { profileQ: Q; refMissingQ: Q; sub: string }) {
	const data = rows(profileQ);
	const nRef = N(data[0]?.n_ref);
	const missing = rows(refMissingQ).map((r) => S(r.name));
	return (
		<div className="flex flex-col gap-6">
			<div>
				<SectionTitle
					sub={
						nRef > 0
							? `Checked against PsychonautWiki's list of ${nRef} effects for ${sub}`
							: `PsychonautWiki has no effect list for ${sub} in this database — reference column unavailable`
					}
				>
					Everything people mentioned
				</SectionTitle>
				{profileQ.isLoading ? (
					<Skeleton h={400} />
				) : (
					<div style={{ maxHeight: 460, overflowY: 'auto' }}>
						<table className="w-full">
							<thead style={{ color: C.muted, position: 'sticky', top: 0, background: C.bg }}>
								<tr>
									<th className={th}>Feeling</th>
									<th className={th}>Type</th>
									<th className={th}>How often</th>
									<th className={th}>Usually starts</th>
									<th className={th}>Felt rough</th>
									<th className={th}>On PsychonautWiki</th>
								</tr>
							</thead>
							<tbody>
								{data.map((r) => {
									const share = N(r.pct_reports);
									const neg = N(r.pct_neg);
									return (
										<tr key={S(r.effect_name)} className="border-t" style={{ borderColor: C.line }}>
											<td className={td} style={{ maxWidth: 320 }}>
												<EffectName name={S(r.effect_name)} />
												<EffectBlurb name={S(r.effect_name)} />
											</td>
											<td className={td} style={{ color: C.muted }}>
												{S(r.category).replace(' Effects', '') || '—'}
											</td>
											<td className={td}>
												<div className="flex items-center gap-2">
													<div className="rounded" style={{ width: 60, height: 6, background: C.line }}>
														<div
															className="rounded"
															style={{ width: `${Math.min(100, share)}%`, height: 6, background: C.primary }}
														/>
													</div>
													<span>{Math.round(share)}%</span>
												</div>
											</td>
											<td className={td}>{fmtH(r.med_first_h as number)}</td>
											<td className={td} style={{ color: neg >= 60 ? C.negative : C.text }}>
												{Math.round(neg)}%
											</td>
											<td className={td}>{nRef === 0 ? '—' : r.in_ref ? '✓' : <span style={{ color: C.mixed }}>not listed</span>}</td>
										</tr>
									);
								})}
							</tbody>
						</table>
					</div>
				)}
				<ErrorBox q={profileQ} />
			</div>
			{nRef > 0 && (
				<div>
					<SectionTitle sub="PsychonautWiki lists these, but nobody mentioned them in these stories">
						Listed, but not mentioned
					</SectionTitle>
					{refMissingQ.isLoading ? (
						<Skeleton h={40} />
					) : missing.length === 0 ? (
						<p className="text-sm" style={{ color: C.muted }}>
							Every listed effect appears at least once.
						</p>
					) : (
						<p className="text-sm">{missing.join(' · ')}</p>
					)}
				</div>
			)}
		</div>
	);
}

// ── Dose tab ──────────────────────────────────────────────────────

function DoseTab({
	doseQ,
	doseCovQ,
	otherUnitsQ,
	roaQ,
	sub,
}: {
	doseQ: Q;
	doseCovQ: Q;
	otherUnitsQ: Q;
	roaQ: Q;
	sub: string;
}) {
	const data = rows(doseQ);
	const routes = [...new Set(data.map((r) => S(r.route)))];
	const cov = rows(doseCovQ)[0] ?? {};
	const units = rows(otherUnitsQ);
	const roa = rows(roaQ).filter((r) => r.dose_common_min != null);

	return (
		<div className="flex flex-col gap-6">
			<div>
				<SectionTitle sub="PsychonautWiki dose bands for each route">Reference doses</SectionTitle>
				{roaQ.isLoading ? (
					<Skeleton h={80} />
				) : roa.length === 0 ? (
					<p className="text-sm" style={{ color: C.muted }}>
						No reference dose bands for {sub}.
					</p>
				) : (
					<table className="w-full">
						<thead style={{ color: C.muted }}>
							<tr>
								<th className={th}>Route</th>
								<th className={th}>Threshold</th>
								<th className={th}>Light</th>
								<th className={th}>Common</th>
								<th className={th}>Strong</th>
								<th className={th}>Heavy</th>
							</tr>
						</thead>
						<tbody>
							{roa.map((r) => {
								const u = S(r.dose_units);
								return (
									<tr key={S(r.route)} className="border-t" style={{ borderColor: C.line }}>
										<td className={td}>{S(r.route)}</td>
										<td className={td}>
											{N(r.dose_threshold)} {u}
										</td>
										<td className={td}>
											{N(r.dose_light_min)}–{N(r.dose_common_min)} {u}
										</td>
										<td className={td}>
											{N(r.dose_common_min)}–{N(r.dose_common_max)} {u}
										</td>
										<td className={td}>
											{N(r.dose_common_max)}–{N(r.dose_strong_max)} {u}
										</td>
										<td className={td}>
											{N(r.dose_heavy)}+ {u}
										</td>
									</tr>
								);
							})}
						</tbody>
					</table>
				)}
			</div>

			<div>
				<SectionTitle
					sub={
						doseCovQ.isLoading
							? ' '
							: `${N(cov.n_mass)} of ${N(cov.n_rows)} dose entries are in mg/g/µg and can be compared${
									units.length ? ` · other units: ${units.map((u) => `${S(u.u)} (${N(u.n)})`).join(', ')}` : ''
								}`
					}
				>
					How much people took, and how it went
				</SectionTitle>
				{doseQ.isLoading ? (
					<Skeleton h={240} />
				) : data.length === 0 ? (
					<p className="text-sm" style={{ color: C.muted }}>
						No reports with a comparable dose for {sub}.
					</p>
				) : (
					routes.map((route) => {
						const rr = data.filter((r) => S(r.route) === route);
						const unit = S(rr[0]?.unit);
						const chart = BANDS.map((label, b) => {
							const r = rr.find((x) => N(x.band) === b);
							return { band: label, Reports: N(r?.n_reports) };
						});
						return (
							<div key={route} className="mb-6">
								<p className="mb-2 text-sm font-semibold">{route}</p>
								<div className="grid grid-cols-2 gap-6">
									<ResponsiveContainer width="100%" height={200}>
										<BarChart data={chart} margin={{ top: 4, right: 8, left: -16, bottom: 0 }}>
											<CartesianGrid strokeDasharray="3 3" stroke={C.grid} vertical={false} />
											<XAxis dataKey="band" fontSize={13} tickLine={false} stroke={C.muted} interval={0} />
											<YAxis fontSize={13} allowDecimals={false} tickLine={false} stroke={C.muted} />
											<Tooltip {...tipStyle} />
											<Bar dataKey="Reports" fill={C.primary} />
										</BarChart>
									</ResponsiveContainer>
									<table className="w-full self-start">
										<thead style={{ color: C.muted }}>
											<tr>
												<th className={th}>Band</th>
												<th className={th}>Median</th>
												<th className={th}>Overall mood</th>
												<th className={th}>Negative</th>
											</tr>
										</thead>
										<tbody>
											{rr.map((r) => (
												<tr key={N(r.band)} className="border-t" style={{ borderColor: C.line }}>
													<td className={td}>{BANDS[N(r.band)]}</td>
													<td className={td}>
														{Math.round(N(r.median_dose) * 10) / 10} {unit}
													</td>
													<td className={td} title={`text sentiment, ${N(r.n_val)} reports`}>
														<MoodCell v={r.avg_valence} n={r.n_val} />
													</td>
													<td className={td} title={`${N(r.n_llm)} annotated reports`}>
														{N(r.n_llm) >= 3 ? `${Math.round(N(r.pct_neg))}%` : '—'}
														<span className="text-sm" style={{ color: C.muted }}>
															{' '}
															(n={N(r.n_llm)})
														</span>
													</td>
												</tr>
											))}
										</tbody>
									</table>
								</div>
							</div>
						);
					})
				)}
				<p className="text-sm" style={{ color: C.muted }}>
					Avg mood = mean text-sentiment score (−1 to +1) of the report's timed passages. Negative = share of annotated
					reports Claude rated negative overall (shown when n ≥ 3). Reports whose route has no reference band are
					matched to the substance's only route when it has just one.
				</p>
				<ErrorBox q={doseQ} />
			</div>
		</div>
	);
}

// ── Combinations tab ──────────────────────────────────────────────

function CombosTab({
	comboQ,
	interQ,
	sub,
	openMix,
}: {
	comboQ: Q;
	interQ: Q;
	sub: string;
	openMix: (subs: string[]) => void;
}) {
	const data = rows(comboQ);
	const inter = rows(interQ);
	return (
		<div className="flex flex-col gap-6">
			<div>
				<SectionTitle
					sub={`Other substances taken in reports where ${sub} was the main substance. "Interaction" comes from the interactions table (TripSit-style cautions); "none listed" is not the same as safe.`}
				>
					Often taken with
				</SectionTitle>
				{comboQ.isLoading ? (
					<Skeleton h={320} />
				) : (
					<table className="w-full">
						<thead style={{ color: C.muted }}>
							<tr>
								<th className={th}>With</th>
								<th className={th}>Reports</th>
								<th className={th}>Interaction</th>
								<th className={th}>Overall mood</th>
								<th className={th}>Negative overall</th>
								<th className={th} />
							</tr>
						</thead>
						<tbody>
							{data.map((r) => {
								const isAlone = N(r.ord) === 0;
								return (
									<tr
										key={S(r.co_sub)}
										className="border-t"
										style={{ borderColor: C.line, background: isAlone ? 'rgba(18,169,184,0.08)' : undefined }}
									>
										<td className={td} style={{ fontWeight: isAlone ? 600 : 400 }}>
											{S(r.co_sub)}
										</td>
										<td className={td}>{N(r.n_reports)}</td>
										<td className={td} title={S(r.via)}>
											{isAlone ? '' : <SevBadge sev={r.sev == null ? null : N(r.sev)} />}
										</td>
										<td className={td} title={`${N(r.n_val)} reports with text sentiment`}>
											<MoodCell v={r.avg_valence} n={r.n_val} />
										</td>
										<td className={td}>
											{N(r.n_llm) >= 3 ? `${Math.round(N(r.pct_neg))}%` : '—'}
											<span className="text-sm" style={{ color: C.muted }}>
												{' '}
												(n={N(r.n_llm)})
											</span>
										</td>
										<td className={td}>
											{!isAlone && (
												<button
													className="text-sm"
													style={{ color: C.cyan, whiteSpace: 'nowrap' }}
													onClick={() => openMix([sub, S(r.co_sub)])}
												>
													See this mix →
												</button>
											)}
										</td>
									</tr>
								);
							})}
						</tbody>
					</table>
				)}
				<ErrorBox q={comboQ} />
			</div>
			<div>
				<SectionTitle sub="Everything the interactions table flags for this substance">
					Mixing safety notes
				</SectionTitle>
				{interQ.isLoading ? (
					<Skeleton h={80} />
				) : inter.length === 0 ? (
					<p className="text-sm" style={{ color: C.muted }}>
						No interactions listed for {sub}.
					</p>
				) : (
					<div className="flex flex-col gap-2">
						{inter.map((r) => {
							const items = L(r.items);
							return (
								<div key={N(r.sev)} className="flex gap-3 text-sm">
									<div style={{ width: 90 }}>
										<SevBadge sev={N(r.sev)} />
									</div>
									<p className="flex-1">{items.join(', ')}</p>
								</div>
							);
						})}
					</div>
				)}
			</div>
		</div>
	);
}

// ── Mix explorer tab ──────────────────────────────────────────────

function MixTab({
	mix,
	setMix,
	exact,
	setExact,
	q,
}: {
	mix: string[];
	setMix: (m: string[]) => void;
	exact: boolean;
	setExact: (b: boolean) => void;
	q: Record<
		'options' | 'pairs' | 'summary' | 'effects' | 'mood' | 'seq' | 'examples' | 'phaseDenom' | 'phaseEff' | 'bins' | 'heat',
		Q
	>;
}) {
	const options = rows(q.options).filter((r) => !mix.includes(S(r.s)));
	const pairs = rows(q.pairs);
	const dangerousPairs = pairs.filter((p) => N(p.sev) === 3);
	const summary = rows(q.summary);
	const together = summary.find((r) => S(r.grp) === 'Together');
	const nTogether = N(together?.n_reports);
	const nTogetherLlm = N(together?.n_llm);
	const colorOf = (g: string) => (g === 'Together' ? C.cyan : MIX_COLORS[Math.max(0, mix.indexOf(g)) % MIX_COLORS.length]);

	const moodData = useMemo(() => {
		const byBin = MOOD_BINS.map((bin) => ({ bin }) as Record<string, string | number | null>);
		for (const r of rows(q.mood)) {
			const b = N(r.bin);
			if (byBin[b] && N(r.n_rep) >= MIN_N) byBin[b][S(r.grp)] = N(r.v);
		}
		return byBin;
		// eslint-disable-next-line react-hooks/exhaustive-deps
	}, [q.mood.data]);

	const effectRows = useMemo(() => {
		const m = new Map<string, Record<string, { share: number; n: number }>>();
		for (const r of rows(q.effects)) {
			const name = S(r.effect_name);
			if (!m.has(name)) m.set(name, {});
			m.get(name)![S(r.grp)] = { share: N(r.share), n: N(r.n) };
		}
		return [...m.entries()]
			.map(([name, g]) => {
				const tog = g.Together?.share ?? 0;
				const aloneMax = Math.max(0, ...mix.map((s) => g[s]?.share ?? 0));
				return { name, g, tog, diff: tog - aloneMax };
			})
			.sort((a, b) => b.tog - a.tog || a.name.localeCompare(b.name));
		// eslint-disable-next-line react-hooks/exhaustive-deps
	}, [q.effects.data, mix.join('|')]);
	const groupN = (g: string) => N(summary.find((r) => S(r.grp) === g)?.n_llm);

	return (
		<div className="flex flex-col gap-6">
			{/* Picker */}
			<div>
				<SectionTitle sub="Pick 2–4 things. We compare stories where they were taken together with stories where each was taken on its own.">
					What happens when you mix?
				</SectionTitle>
				<div className="flex flex-wrap items-center gap-2">
					{mix.map((s, i) => (
						<span
							key={s}
							className="flex items-center gap-1 rounded-full border px-3 py-1 text-sm"
							style={{ borderColor: MIX_COLORS[i % MIX_COLORS.length], color: C.text, background: C.card }}
						>
							{s}
							<button
								aria-label={`Remove ${s}`}
								onClick={() => setMix(mix.filter((x) => x !== s))}
								style={{ color: C.muted, display: 'flex' }}
							>
								<X size={14} />
							</button>
						</span>
					))}
					{mix.length < MAX_MIX && (
						<select
							className="rounded-full border px-3 py-1 text-sm"
							style={{ background: C.card, color: C.cyan, borderColor: C.line }}
							value=""
							onChange={(e) => e.target.value && setMix([...mix, e.target.value])}
						>
							<option value="">+ add something</option>
							{options.map((r) => (
								<option key={S(r.s)} value={S(r.s)}>
									{S(r.s)} ({N(r.n)} reports)
								</option>
							))}
						</select>
					)}
					<label className="ml-2 flex cursor-pointer items-center gap-2 text-sm" style={{ color: C.muted }}>
						<input
							type="checkbox"
							checked={exact}
							onChange={(e) => setExact(e.target.checked)}
							style={{ width: 14, height: 14, accentColor: C.cyan }}
						/>
						Only these, nothing else
					</label>
				</div>
			</div>

			{mix.length < 2 ? (
				<p className="text-sm" style={{ color: C.muted }}>
					Pick at least two things to see how they mix.
				</p>
			) : (
				<>
					{/* Interaction cautions */}
					<div>
						<SectionTitle sub="From TripSit-style interaction notes. “No warning listed” doesn't mean safe — just that there's no note on it.">
							Mixing check ♡
						</SectionTitle>
						{q.pairs.isLoading ? (
							<Skeleton h={60} />
						) : (
							<>
								{dangerousPairs.length > 0 && (
									<div
										className="mb-3 flex items-start gap-2 rounded-xl border p-3 text-sm"
										style={{ borderColor: C.negative, background: C.card, color: C.negative }}
									>
										<AlertTriangle size={16} style={{ flexShrink: 0, marginTop: 2 }} />
										<span>
											Heads up — this mix is listed as <b>dangerous</b>: {dangerousPairs.map((p) => `${S(p.a)} + ${S(p.b)}`).join(', ')}. Please take extra care, or skip it.
										</span>
									</div>
								)}
								<div className="flex flex-col gap-1">
									{pairs.map((p) => (
										<div key={`${S(p.a)}|${S(p.b)}`} className="flex items-center gap-3 text-sm">
											<span style={{ width: 260 }}>
												{S(p.a)} + {S(p.b)}
											</span>
											<SevBadge sev={p.sev == null ? null : N(p.sev)} />
											{p.via != null && (
												<span className="text-sm" style={{ color: C.muted }}>
													via {S(p.via)}
												</span>
											)}
										</div>
									))}
								</div>
							</>
						)}
						<ErrorBox q={q.pairs} />
					</div>

					{/* Together vs alone */}
					<div>
						<SectionTitle sub="How the trips went overall — together vs. each on its own">
							Together vs. on its own
						</SectionTitle>
						{q.summary.isLoading ? (
							<Skeleton h={100} />
						) : (
							<table className="w-full">
								<thead style={{ color: C.muted }}>
									<tr>
										<th className={th}>Stories where…</th>
										<th className={th}>Stories</th>
										<th className={th}>Overall mood</th>
										<th className={th}>Rough trips</th>
										<th className={th}>Would do it again</th>
									</tr>
								</thead>
								<tbody>
									{summary.map((r) => {
										const g = S(r.grp);
										const isTog = g === 'Together';
										return (
											<tr
												key={g}
												className="border-t"
												style={{ borderColor: C.line, background: isTog ? 'rgba(18,169,184,0.08)' : undefined }}
											>
												<td className={td}>
													<span
														className="mr-2 inline-block rounded-full"
														style={{ width: 8, height: 8, background: colorOf(g) }}
													/>
													<span style={{ fontWeight: isTog ? 600 : 400 }}>
														{isTog ? `all ${mix.length} taken together` : `${g} on its own`}
													</span>
												</td>
												<td className={td}>{N(r.n_reports)}</td>
												<td className={td}>
													<MoodCell v={r.avg_valence} n={r.n_val} />
												</td>
												<td className={td}>
													{N(r.n_llm) >= 3 ? `${Math.round(N(r.pct_neg))}%` : '—'}
													<span className="text-sm" style={{ color: C.muted }}>
														{' '}
														(n={N(r.n_llm)})
													</span>
												</td>
												<td className={td}>
													{N(r.n_repeat) >= 3 ? `${Math.round(N(r.pct_repeat))}%` : '—'}
													<span className="text-sm" style={{ color: C.muted }}>
														{' '}
														(n={N(r.n_repeat)})
													</span>
												</td>
											</tr>
										);
									})}
								</tbody>
							</table>
						)}
						{!q.summary.isLoading && nTogether === 0 && (
							<p className="mt-2 text-sm" style={{ color: C.mixed }}>
								No stories include all of these together{exact ? ' (and nothing else)' : ''} yet.
							</p>
						)}
						{!q.summary.isLoading && nTogether > 0 && nTogetherLlm < 10 && (
							<p className="mt-2 flex items-center gap-2 text-sm" style={{ color: C.mixed }}>
								<AlertTriangle size={14} /> We've read {nTogetherLlm} of these {nTogether} stories closely — take the
								patterns below with a pinch of salt 🧂
							</p>
						)}
						<ErrorBox q={q.summary} />
					</div>

					{nTogether > 0 && (
						<>
							{/* Timeline */}
							<div>
								<SectionTitle sub="Effects mentioned in each window of the combined reports (share of reports that describe that window), timed from the first dose">
									<span className="inline-flex items-center gap-2">
										<Clock size={18} /> How it usually unfolds together
									</span>
								</SectionTitle>
								<PhaseCards denomQ={q.phaseDenom} effQ={q.phaseEff} />
							</div>

							{/* Effects over time */}
							<div>
								<SectionTitle sub="The five most-reported effects in the combined reports, hour by hour since the first dose">
									How the main feelings rise and fall together
								</SectionTitle>
								<EffectLines heatQ={q.heat} binQ={q.bins} />
								<ErrorBox q={q.heat} />
							</div>

							{/* Mood over time */}
							<div>
								<SectionTitle sub="How good or rough people felt, hour by hour — higher is better">
									Mood along the way
								</SectionTitle>
								{q.mood.isLoading ? (
									<Skeleton h={220} />
								) : (
									<ResponsiveContainer width="100%" height={220}>
										<LineChart data={moodData} margin={{ top: 4, right: 8, left: -12, bottom: 0 }}>
											<CartesianGrid strokeDasharray="3 3" stroke={C.grid} vertical={false} />
											<XAxis dataKey="bin" fontSize={13} tickLine={false} stroke={C.muted} />
											<YAxis
												fontSize={13}
												tickLine={false}
												stroke={C.muted}
												tickFormatter={(v) => (v > 0 ? `+${v}` : `${v}`)}
											/>
											<Tooltip
												{...tipStyle}
												formatter={(v: number, name: string) => [(v >= 0 ? '+' : '') + v.toFixed(2), name]}
											/>
											<Legend wrapperStyle={{ fontSize: 13 }} />
											<Line
												type="linear"
												dataKey="Together"
												stroke={C.cyan}
												strokeWidth={3}
												dot={false}
												connectNulls={false}
											/>
											{mix.map((s) => (
												<Line
													key={s}
													type="linear"
													dataKey={s}
													name={`${s} alone`}
													stroke={colorOf(s)}
													strokeWidth={2}
													strokeDasharray="4 3"
													dot={false}
													connectNulls={false}
												/>
											))}
										</LineChart>
									</ResponsiveContainer>
								)}
								<ErrorBox q={q.mood} />
							</div>

							{/* Effects together vs alone */}
							<div>
								<SectionTitle sub="How often each feeling came up when mixed vs. on its own. Big jumps are highlighted.">
									What changes when you mix
								</SectionTitle>
								{q.effects.isLoading ? (
									<Skeleton h={300} />
								) : effectRows.length === 0 ? (
									<p className="text-sm" style={{ color: C.muted }}>
										No annotated effects for the combined reports.
									</p>
								) : (
									<div style={{ maxHeight: 420, overflowY: 'auto' }}>
										<table className="w-full">
											<thead style={{ color: C.muted, position: 'sticky', top: 0, background: C.bg }}>
												<tr>
													<th className={th}>Effect</th>
													<th className={th}>Together (n={groupN('Together')})</th>
													{mix.map((s) => (
														<th key={s} className={th}>
															{s} (n={groupN(s)})
														</th>
													))}
													<th className={th}>Change</th>
												</tr>
											</thead>
											<tbody>
												{effectRows.map((e) => (
													<tr key={e.name} className="border-t" style={{ borderColor: C.line }}>
														<td className={td}><EffectName name={e.name} /></td>
														<td className={td} style={{ color: C.cyan }}>
															{Math.round(e.tog)}%
														</td>
														{mix.map((s) => (
															<td key={s} className={td} style={{ color: C.muted }}>
																{e.g[s] ? `${Math.round(e.g[s].share)}%` : groupN(s) ? '0%' : '—'}
															</td>
														))}
														<td
															className={td}
															style={{
																color: e.diff >= 15 && groupN('Together') >= 5 ? C.coral : C.muted,
																fontWeight: e.diff >= 15 ? 600 : 400,
															}}
														>
															{e.diff >= 0 ? '+' : ''}
															{Math.round(e.diff)} pts
														</td>
													</tr>
												))}
											</tbody>
										</table>
									</div>
								)}
								<ErrorBox q={q.effects} />
							</div>

							{/* Order & timing */}
							<div>
								<SectionTitle sub="From stories that say when each thing was taken. Taken at the same time counts as first for both.">
									What people took first, and when
								</SectionTitle>
								{q.seq.isLoading ? (
									<Skeleton h={80} />
								) : rows(q.seq).length === 0 ? (
									<p className="text-sm" style={{ color: C.muted }}>
										No reports give dose times for all of these.
									</p>
								) : (
									<table className="w-full">
										<thead style={{ color: C.muted }}>
											<tr>
												<th className={th}>Substance</th>
												<th className={th}>Taken first</th>
												<th className={th}>Typically taken at</th>
												<th className={th}>Reports</th>
											</tr>
										</thead>
										<tbody>
											{rows(q.seq).map((r) => (
												<tr key={S(r.sub)} className="border-t" style={{ borderColor: C.line }}>
													<td className={td}>{S(r.sub)}</td>
													<td className={td}>{Math.round(N(r.pct_first))}%</td>
													<td className={td}>
														{N(r.med_after) === 0 ? 'T+0' : `T+${fmtH(N(r.med_after))}`}
														<span className="text-sm" style={{ color: C.muted }}>
															{' '}
															(middle half T+{fmtH(N(r.p25))}–{fmtH(N(r.p75))})
														</span>
													</td>
													<td className={td}>{N(r.n)}</td>
												</tr>
											))}
										</tbody>
									</table>
								)}
								<ErrorBox q={q.seq} />
							</div>

							{/* Example reports */}
							<div>
								<SectionTitle sub="The start of each story — tap through to read the whole thing">
									Real stories
								</SectionTitle>
								{q.examples.isLoading ? (
									<Skeleton h={200} />
								) : (
									<div className="flex flex-col gap-2" style={{ maxHeight: 420, overflowY: 'auto' }}>
										{rows(q.examples).map((r) => {
											const id = S(r.report_id).replace('erowid-', '');
											return (
												<div key={id} className="border-t pt-2 text-sm" style={{ borderColor: C.line }}>
													<div className="flex items-center gap-2 text-sm" style={{ color: C.muted }}>
														{r.sentiment != null && <SentimentDot s={S(r.sentiment)} />}
														<span>{r.yr != null ? N(r.yr) : '—'}</span>
														<span>· {S(r.subs)}</span>
														<a
															href={`https://erowid.org/experiences/exp.php?ID=${id}`}
															target="_blank"
															rel="noopener noreferrer"
															className="ml-auto"
															style={{ color: C.cyan }}
														>
															Read the full story ↗
														</a>
													</div>
													<p className="mt-1">{S(r.snippet)}…</p>
												</div>
											);
										})}
									</div>
								)}
								<ErrorBox q={q.examples} />
							</div>
						</>
					)}
				</>
			)}
		</div>
	);
}

// ── Compare tab ───────────────────────────────────────────────────

function CompareTab({
	compareQ,
	sub,
	setSub,
	setTab,
}: {
	compareQ: Q;
	sub: string;
	setSub: (s: string) => void;
	setTab: (t: string) => void;
}) {
	const data = rows(compareQ).map((r) => ({
		name: S(r.primary_sub),
		n: N(r.n),
		onset: Math.max(0.01, N(r.onset_h)),
		peak: Math.max(0.01, N(r.peak_h)),
		last: Math.max(0.01, N(r.last_h)),
	}));
	const pts = (key: 'onset' | 'peak' | 'last') => data.map((d) => ({ name: d.name, x: d[key], n: d.n, sel: d.name === sub }));
	const ticks = [1 / 60, 5 / 60, 0.25, 0.5, 1, 2, 4, 8, 16];
	return (
		<div>
			<SectionTitle sub="Medians from single-substance annotated reports (substances with ≥ 10). Log time scale. Click a name to open it.">
				How long things last, side by side
			</SectionTitle>
			{compareQ.isLoading ? (
				<Skeleton h={480} />
			) : (
				<>
					<ResponsiveContainer width="100%" height={Math.max(260, data.length * 20 + 60)}>
						<ScatterChart margin={{ top: 4, right: 16, left: 8, bottom: 0 }}>
							<CartesianGrid strokeDasharray="3 3" stroke={C.grid} />
							<XAxis
								type="number"
								dataKey="x"
								scale="log"
								domain={[0.01, 24]}
								ticks={ticks}
								tickFormatter={(v) => fmtH(v)}
								fontSize={13}
								allowDataOverflow
								stroke={C.muted}
							/>
							<YAxis
								type="category"
								dataKey="name"
								width={150}
								stroke={C.muted}
								fontSize={13}
								interval={0}
								allowDuplicatedCategory={false}
								tick={(props: { x: number; y: number; payload: { value: string } }) => (
									<text
										x={props.x}
										y={props.y}
										dy={4}
										textAnchor="end"
										fontSize={13}
										fill={props.payload.value === sub ? C.primary : C.text}
										fontWeight={props.payload.value === sub ? 700 : 400}
										style={{ cursor: 'pointer' }}
										onClick={() => {
											setSub(props.payload.value);
											setTab('forecast');
										}}
									>
										{props.payload.value}
									</text>
								)}
							/>
							<ZAxis range={[40, 40]} />
							<Tooltip
								content={({ payload }) => {
									const p = payload?.[0]?.payload;
									if (!p) return null;
									const d = data.find((x) => x.name === p.name);
									return (
										<div
											className="rounded-lg border p-2 text-sm"
											style={{ borderColor: C.line, background: C.card, color: C.text }}
										>
											<b>{p.name}</b> (n={d?.n})
											<br />
											First effects {fmtH(d?.onset)} · peak {fmtH(d?.peak)} · last {fmtH(d?.last)}
										</div>
									);
								}}
							/>
							<Legend wrapperStyle={{ fontSize: 13 }} />
							<Scatter name="First effects" data={pts('onset')} fill={C.lavender} />
							<Scatter name="Most intense" data={pts('peak')} fill={C.cyan} />
							<Scatter name="Last effect" data={pts('last')} fill={C.coral} shape="diamond" />
						</ScatterChart>
					</ResponsiveContainer>
				</>
			)}
			<ErrorBox q={compareQ} />
		</div>
	);
}

// ── Notes tab ─────────────────────────────────────────────────────

function NotesTab({ current }: { current?: Record<string, unknown> }) {
	return (
		<div className="flex flex-col gap-4 text-sm" style={{ maxWidth: 720 }}>
			<div>
				<SectionTitle>
					<span className="inline-flex items-center gap-2">
						<Info size={16} /> How to read this
					</span>
				</SectionTitle>
				<ul className="list-disc space-y-1 pl-5">
					<li>
						These are <b>patterns in what people chose to write</b>, not clinical probabilities. Erowid reports
						over-represent memorable (very good or very bad) experiences.
					</li>
					<li>
						Timing, effects and tone come from Claude (claude-opus-4-6) annotating 1,725 of 13,619 Erowid reports.
						Each report is split into timed moments (T+h:mm since first dose), each tagged with effects from the
						EffectIndex vocabulary. Low-confidence tags are excluded.
					</li>
					<li>
						Heatmap and phase percentages are divided by the number of reports that <i>describe</i> each window, so
						early hours (which people write about most) are not over-weighted. Windows with fewer than {MIN_N} reports
						are greyed out.
					</li>
					<li>
						“First effects” = first moment with a tagged effect (T+0 counts, for fast-acting routes). “Most intense”
						= the moment with the most tagged effects. “Last effect” = last tagged moment within 24 h.
					</li>
					<li>
						The main substance is the report's primary Erowid substance mapped to a canonical name. Most reports
						involve other substances too; tick <i>Single-substance reports only</i> for cleaner (but smaller)
						samples.
					</li>
					{current && (
						<li>
							Current substance: {N(current.n_reports)} reports in total, {N(current.n_llm)} annotated (
							{N(current.n_llm_single)} single-substance).
						</li>
					)}
				</ul>
			</div>
			<div>
				<SectionTitle>Known gaps in the data</SectionTitle>
				<ul className="list-disc space-y-1 pl-5">
					<li>
						<code>routes_of_administration.duration_*</code> values were stored without units (e.g. DMT smoked onset
						“20–40” is seconds, LSD onset “15–30” is minutes, LSD peak “3–5” is hours).
					</li>
					<li>
						<code>substance_effects</code> covers only 163 of 374 substances — MDMA, psilocybin mushrooms, Salvia and
						others have no reference effect list.
					</li>
					<li>
						<code>report_phases</code> (rule-based) is not used: median session end is ~24 h for almost every
						substance and some durations are negative.
					</li>
					<li>
						<code>report_doses.amount_unit</code> is often a parse fragment (e.g. “- 10 mg” from ranges); only clean
						mg/g/µg entries are used for dose bands.
					</li>
					<li>
						<code>interactions</code> uses class names (“Amphetamines”, “SSRIs”); they are matched to substances by
						name, common name and class, which covers about 58 of 68 targets.
					</li>
					<li>
						<code>report_doses.canonical_substance</code> joins to <code>substances.name</code>, not{' '}
						<code>substances.id</code>.
					</li>
					<li>
						<code>experience_reports.title</code> and <code>author</code> are empty for all 13,619 reports.
					</li>
					<li>
						Mix explorer: a report counts as “together” when <code>report_doses</code> lists every substance in the
						mix (not necessarily at the same time), and as “alone” when it lists only that one substance. Only ~43% of
						dose rows have a time, so the order-and-spacing table covers a subset.
					</li>
				</ul>
			</div>
		</div>
	);
}
