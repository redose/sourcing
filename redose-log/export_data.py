"""Export the reference data the re:dose dose log needs from MotherDuck into data.js.

Run with a MotherDuck token in the `motherduck_token` env var:
    python export_data.py            # writes data.js next to this file

Everything the page shows comes from here; the page itself never talks to MotherDuck.
"""

import json
import pathlib

import duckdb

OUT = pathlib.Path(__file__).with_name("data.js")
MIN_TIMING_N = 6  # stories needed before we show a timing estimate
MIN_PHASE_N = 5  # stories describing a phase before we show what people felt then

con = duckdb.connect("md:sourcing")

# Annotated observations, timed from the first dose, keyed by that first dose's substance and route.
con.sql(
    r"""
CREATE OR REPLACE TEMP TABLE obs AS
WITH fd AS (
  SELECT report_id,
    extraction->>'$.doses[0].substance' AS sub,
    CASE lower(extraction->>'$.doses[0].route')
      WHEN 'inhaled' THEN 'smoked' WHEN 'vaporized' THEN 'smoked' WHEN 'vaped' THEN 'smoked'
      WHEN 'iv' THEN 'injected' WHEN 'intravenous' THEN 'injected' WHEN 'im' THEN 'injected'
      WHEN 'snorted' THEN 'insufflated'
      ELSE lower(extraction->>'$.doses[0].route') END AS route
  FROM llm_extractions
)
SELECT fd.sub, fd.route, o.report_id, o.obs_index, o.sentiment,
  TRY_CAST(regexp_extract(o.time_relative, '^T\+~?(\d+):(\d{2})', 1) AS INTEGER)
    + TRY_CAST(regexp_extract(o.time_relative, '^T\+~?(\d+):(\d{2})', 2) AS INTEGER) / 60.0 AS hrs
FROM llm_observations o JOIN fd USING (report_id)
WHERE fd.sub IS NOT NULL
"""
)
con.sql(
    """
CREATE OR REPLACE TEMP TABLE obs_eff AS
SELECT o.*, (SELECT count(*) FROM llm_observation_effects e
             WHERE e.report_id = o.report_id AND e.obs_index = o.obs_index AND e.confidence <> 'low') AS n_eff
FROM obs o WHERE hrs IS NOT NULL
"""
)

TIMING_SQL = """
WITH per AS (
  SELECT sub, {route_col} AS route, report_id,
    min(hrs) FILTER (WHERE n_eff > 0) AS onset,
    arg_max(hrs, n_eff * 1000 - hrs) FILTER (WHERE hrs < 24) AS peak,
    max(hrs) FILTER (WHERE n_eff > 0 AND hrs < 24) AS last_eff
  FROM obs_eff GROUP BY ALL
)
SELECT sub, route, count(*) AS n,
  median(onset), quantile_cont(onset, 0.25), quantile_cont(onset, 0.75),
  median(peak), quantile_cont(peak, 0.25), quantile_cont(peak, 0.75),
  median(last_eff), quantile_cont(last_eff, 0.25), quantile_cont(last_eff, 0.75)
FROM per WHERE onset IS NOT NULL
GROUP BY ALL HAVING count(*) >= {min_n}
"""


def timing_rows(route_col):
    rows = con.sql(TIMING_SQL.format(route_col=route_col, min_n=MIN_TIMING_N)).fetchall()
    out = {}
    for sub, route, n, *v in rows:
        r = lambda x: None if x is None else round(x, 3)  # noqa: E731
        out[(sub, route)] = {
            "n": n,
            "onset": [r(v[0]), r(v[1]), r(v[2])],
            "peak": [r(v[3]), r(v[4]), r(v[5])],
            "end": [r(v[6]), r(v[7]), r(v[8])],
        }
    return out


timing_all = timing_rows("'*'")
timing_route = timing_rows("route")

# What people describe in each phase (hours since first dose), as a share of stories describing that phase.
PHASE_SQL = f"""
WITH ph AS (
  SELECT *, CASE WHEN hrs < 0.5 THEN 0 WHEN hrs < 2 THEN 1 WHEN hrs < 4 THEN 2 WHEN hrs < 8 THEN 3
                 WHEN hrs < 24 THEN 4 ELSE 5 END AS phase
  FROM obs WHERE hrs IS NOT NULL
),
den AS (
  SELECT sub, phase, count(DISTINCT report_id) AS n,
    100.0 * avg((sentiment = 'negative')::INT) AS pct_rough
  FROM ph GROUP BY ALL HAVING count(DISTINCT report_id) >= {MIN_PHASE_N}
),
eff AS (
  SELECT ph.sub, ph.phase, e.effect_name, count(DISTINCT ph.report_id) AS c
  FROM ph JOIN llm_observation_effects e USING (report_id, obs_index)
  WHERE e.confidence <> 'low' GROUP BY ALL
)
SELECT d.sub, d.phase, d.n, d.pct_rough,
  list({{'e': eff.effect_name, 'p': round(100.0 * eff.c / d.n)}} ORDER BY eff.c DESC, eff.effect_name)[1:4] AS top
FROM den d JOIN eff USING (sub, phase)
GROUP BY ALL
"""
phases = {}
used_effects = set()
for sub, phase, n, pct_rough, top in con.sql(PHASE_SQL).fetchall():
    phases.setdefault(sub, [None] * 6)[phase] = {"n": n, "rough": round(pct_rough), "top": top}
    used_effects.update(t["e"] for t in top)

# Substances: names, aliases, classes, reference dose bands per route.
subs = con.sql(
    """
SELECT s.id, s.name, coalesce(s.common_names, []) AS aliases, coalesce(s.psychoactive_class, []) AS cls,
  list({'route': r.route, 'unit': replace(r.dose_units, 'μg', 'µg'), 'threshold': r.dose_threshold,
        'light': r.dose_light_min, 'common': [r.dose_common_min, r.dose_common_max],
        'strong': r.dose_strong_max, 'heavy': r.dose_heavy} ORDER BY r.route)
    FILTER (WHERE r.route IS NOT NULL) AS routes
FROM substances s LEFT JOIN routes_of_administration r ON r.substance_id = s.id
GROUP BY ALL ORDER BY s.name
"""
).fetchall()

# Interaction severity for every pair of known substances (name / common name / class matching, both directions).
pairs = con.sql(
    """
WITH subs AS (
  SELECT id, name,
    list_distinct(list_transform(coalesce(common_names, []), x -> lower(x)) || [lower(name)]
      || list_filter(list_transform(string_split(name, ' '), x -> lower(x)), x -> len(x) > 4)) AS names,
    list_transform(coalesce(chemical_class, []) || coalesce(psychoactive_class, []), x -> lower(x)) AS classes
  FROM substances
),
hit AS (
  SELECT i.substance_id AS a_id, t.name AS b, i.severity
  FROM interactions i JOIN subs t ON
    list_contains(t.names, lower(i.interacts_with)) OR list_contains(t.names, rtrim(lower(i.interacts_with), 's'))
    OR list_contains(t.classes, lower(i.interacts_with)) OR list_contains(t.classes, rtrim(lower(i.interacts_with), 's'))
)
SELECT least(a.name, hit.b) AS x, greatest(a.name, hit.b) AS y,
  max(CASE hit.severity WHEN 'dangerous' THEN 3 WHEN 'unsafe' THEN 2 ELSE 1 END) AS sev
FROM hit JOIN subs a ON a.id = hit.a_id
WHERE a.name <> hit.b
GROUP BY ALL
"""
).fetchall()

# Raw "don't mix with" lists, for things that aren't substances in the table (e.g. Lithium, MAOIs).
avoid = {}
for name, iw, sev in con.sql(
    """
SELECT s.name, i.interacts_with, CASE i.severity WHEN 'dangerous' THEN 3 WHEN 'unsafe' THEN 2 ELSE 1 END
FROM interactions i JOIN substances s ON s.id = i.substance_id
"""
).fetchall():
    avoid.setdefault(name, []).append([iw, sev])

effects = {
    name: {"blurb": blurb, "url": url}
    for name, blurb, url in con.sql(
        r"""
SELECT name,
  trim(regexp_extract(
    regexp_replace(regexp_replace(regexp_replace(regexp_replace(
      CASE WHEN starts_with(description, name) THEN name || ' ' || substr(description, len(name) + 1) ELSE description END,
      '\[\d+\]', '', 'g'), '\s*\([^)]*\)', '', 'g'), '([.!?])([A-Z])', '\1 \2', 'g'), '\s+', ' ', 'g'),
    '^(.*?[.!?])(\s|$)', 1)),
  url
FROM effects WHERE description IS NOT NULL
"""
    ).fetchall()
    if name in used_effects and blurb
}

out_subs = []
for sid, name, aliases, cls, routes in subs:
    t_routes = {route: t for (s, route), t in timing_route.items() if s == name}
    out_subs.append(
        {
            "name": name,
            "aliases": [a for a in aliases if a.lower() != name.lower()],
            "cls": cls,
            "routes": routes or [],
            "timing": timing_all.get((name, "*")),
            "timingByRoute": t_routes or None,
            "phases": phases.get(name),
            "avoid": sorted(avoid.get(name, []), key=lambda x: (-x[1], x[0])),
        }
    )

data = {
    "substances": out_subs,
    "pairs": {f"{x}|{y}": sev for x, y, sev in pairs},
    "effects": effects,
    "phaseHours": [0, 0.5, 2, 4, 8, 24],
}
OUT.write_text("window.REDOSE_DATA = " + json.dumps(data, ensure_ascii=False, separators=(",", ":")) + ";\n")

with_timing = sum(1 for s in out_subs if s["timing"])
print(f"{len(out_subs)} substances ({with_timing} with timing), {len(data['pairs'])} pairs, "
      f"{len(effects)} effect blurbs -> {OUT} ({OUT.stat().st_size // 1024} KB)")
