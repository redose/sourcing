"""Load dose.wiki's harm-potential and tolerance notes into MotherDuck (sourcing database).

Creates (or replaces) one table:
  substance_harms  one row per substance and area: the area (physical_dependence, psychological_dependence,
                   psychosis, seizure, ... and tolerance_full, tolerance_half, tolerance_baseline), a level where
                   dose.wiki gives one (low/moderate/high...), the explanation, and a link back.

Source: dose.wiki's SubstanceIndex.json (https://effectindex.com/SubstanceIndex.json), CC0 1.0 for these fields
(https://effectindex.com/docs/license). Matched to our substances as in load_legality.py.

Run with a MotherDuck token in the `motherduck_token` environment variable:
    python load_harms.py PATH/TO/SubstanceIndex.json
"""

import json
import re
import sys
from datetime import datetime, timezone

import duckdb


def clean(text):
    return re.sub(r"\s+", " ", re.sub(r"\[cite:[^\]]*\]", "", text or "")).strip()


def main(path):
    data = json.load(open(path))
    con = duckdb.connect("md:sourcing")
    ours = con.sql("SELECT name, coalesce(common_names, []) FROM substances").fetchall()
    by_name = {n.lower(): n for n, _ in ours}
    by_alias = {"salvia": "Salvia divinorum"}
    for n, aka in ours:
        for a in aka:
            by_alias.setdefault(a.lower(), n)
    fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows, seen, areas = [], set(), {}
    for item in data:
        title = (item.get("title") or "").lower()
        sub = by_name.get(title) or by_alias.get(title)
        if not sub or sub in seen:
            continue
        seen.add(sub)
        url = f"https://effectindex.com/{item.get('slug')}"
        harm = item.get("harm_potential") or {}
        for key, val in harm.items():
            if key == "addiction" and isinstance(val, dict):
                for sub_key, v in val.items():
                    area = "psychological_dependence" if sub_key == "psychological" else sub_key
                    if isinstance(v, dict) and clean(v.get("description")):
                        rows.append((sub, area, v.get("level"), clean(v.get("description")), url, fetched))
            elif isinstance(val, dict) and clean(val.get("description")):
                rows.append((sub, key, val.get("level"), clean(val.get("description")), url, fetched))
        tol = item.get("tolerance") or {}
        for key, area in [("full_tolerance", "tolerance_full"), ("half_tolerance", "tolerance_half"), ("baseline_tolerance", "tolerance_baseline")]:
            if isinstance(tol.get(key), str) and clean(tol[key]):
                rows.append((sub, area, None, clean(tol[key]), url, fetched))
    for r in rows:
        areas[r[1]] = areas.get(r[1], 0) + 1
    print("areas:", dict(sorted(areas.items(), key=lambda kv: -kv[1])))
    print("levels:", sorted({str(r[2]) for r in rows}))
    con.execute("""CREATE OR REPLACE TABLE substance_harms (
        substance VARCHAR, area VARCHAR, level VARCHAR, description VARCHAR, source_url VARCHAR, fetched_at VARCHAR)""")
    con.executemany("INSERT INTO substance_harms VALUES (?, ?, ?, ?, ?, ?)", rows)
    con.execute("COMMENT ON TABLE substance_harms IS 'Harm potential (dependence, psychosis, seizure...) and tolerance notes from dose.wiki SubstanceIndex.json (CC0 1.0).'")
    print(f"Loaded {len(rows)} rows for {len(seen)} substances.")


if __name__ == "__main__":
    main(sys.argv[1])
