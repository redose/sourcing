"""Load dose.wiki's substance summaries and classes into MotherDuck (sourcing database).

Creates (or replaces) one table:
  substance_overviews  one row per substance we have: dose.wiki's summary, its opening sentence (the "lead" shown
                       under a substance's name), its psychoactive and chemical classes, and a link back.

Source: dose.wiki's SubstanceIndex.json (https://effectindex.com/SubstanceIndex.json), CC0 1.0 for these fields
(https://effectindex.com/docs/license). Matched to our substances the same way as load_legality.py.

Run with a MotherDuck token in the `motherduck_token` environment variable:
    python load_overviews.py PATH/TO/SubstanceIndex.json
"""

import json
import re
import sys
from datetime import datetime, timezone

import duckdb


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\[cite:[^\]]*\]", "", text or "")).strip()


def lead(text: str) -> str:
    """The first sentence, not fooled by abbreviations like "B. caapi" or "e.g."."""
    for m in re.finditer(r"[.!?](?=\s+[A-Z])", text):
        before = text[: m.start()].split()[-1] if text[: m.start()].split() else ""
        if len(before) <= 2 or before.lower() in {"e.g", "i.e", "approx", "ca", "vs", "dr", "st"}:
            continue
        return text[: m.end()].strip()
    return text.strip()


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
    rows, seen = [], set()
    for item in data:
        sub = by_name.get((item.get("title") or "").lower()) or by_alias.get((item.get("title") or "").lower())
        summary = clean(item.get("summary") or "")
        if not sub or not summary or sub in seen:
            continue
        seen.add(sub)
        cls = item.get("classification") or {}
        rows.append((sub, item.get("slug"), summary, lead(summary), cls.get("psychoactive_class") or [], cls.get("chemical_class") or [],
                     f"https://effectindex.com/{item.get('slug')}", fetched))
    con.execute("""CREATE OR REPLACE TABLE substance_overviews (
        substance VARCHAR, dosewiki_slug VARCHAR, summary VARCHAR, lead VARCHAR, psychoactive_class VARCHAR[],
        chemical_class VARCHAR[], source_url VARCHAR, fetched_at VARCHAR)""")
    con.executemany("INSERT INTO substance_overviews VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.execute("COMMENT ON TABLE substance_overviews IS 'Summaries and classes from dose.wiki SubstanceIndex.json (CC0 1.0); lead = first sentence.'")
    print(f"Loaded {len(rows)} overviews.")
    for r in rows[:4]:
        print(" ", r[0], "::", r[3])


if __name__ == "__main__":
    main(sys.argv[1])
