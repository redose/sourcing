"""Load legal status by country from dose.wiki (Effect Index) into MotherDuck (sourcing database).

Creates (or replaces) two tables:
  substance_legality                one row per substance and country: the status as dose.wiki words it, a simple group
                                    (illegal, prescription, restricted, decriminalized, legal, unclear), its note, a link back.
  substance_legality_international  one row per substance and statement about its status under the UN conventions.

Source: dose.wiki's downloadable SubstanceIndex.json (https://effectindex.com/SubstanceIndex.json). Its legality
field is dose.wiki's own writing, released under CC0 1.0 (https://effectindex.com/docs/license); we credit them anyway.
dose.wiki substances are matched to ours by name, then by our common names.

Run with a MotherDuck token in the `motherduck_token` environment variable:
    python load_legality.py PATH/TO/SubstanceIndex.json            # match, load the table
    python load_legality.py PATH/TO/SubstanceIndex.json --dry-run  # match and print, write nothing
"""

import json
import re
import sys
from datetime import datetime, timezone

import duckdb


def group(status: str) -> str:
    """dose.wiki's statuses are worded per country ("Schedule I", "Anlage II BtMG", "Class A"…); sort them into a few."""
    s = status.lower()
    if re.search(r"decriminali[sz]", s):
        return "decriminalized"
    if re.search(r"prescription|rx\b|medicine|medication|pharmac|abgabekategorie [ab]|tarja preta|black stripe", s):
        return "prescription"
    if re.search(r"\bun(controlled|scheduled|regulated)|not (controlled|scheduled|listed|regulated)|\blegal\b|over[- ]the[- ]counter|\botc\b|dietary supplement|exempt", s) and not re.search(r"illegal", s):
        return "legal"
    if re.search(r"behind (the )?counter|restrict|grey|gray|licen|conditionally", s):
        return "restricted"
    if re.search(r"illegal|prohibit|banned|schedule|class [a-z0-9]|controlled|anlage|verzeichnis|btmg|npsg|smg|narcotic|psychotropic|annex|appendix|list i|lijst i|list [12ab]\\b|lista |illicit|nps class|new psychoactive|felony|misdemeanor|designated|dangerous|category [a-z0-9]|grade [0-9]", s):
        return "illegal"
    return "unclear"


def clean(text: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\[cite:[^\]]*\]", "", text or "")).strip()


def main(path: str, dry_run: bool):
    data = json.load(open(path))
    con = duckdb.connect("md:sourcing")
    ours = con.sql("SELECT name, coalesce(common_names, []) FROM substances").fetchall()
    by_name = {n.lower(): n for n, _ in ours}
    by_alias = {"salvia": "Salvia divinorum"}
    for n, aka in ours:
        for a in aka:
            by_alias.setdefault(a.lower(), n)
    fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows, intl, matched, unmatched = [], [], 0, []
    for item in data:
        title = item.get("title") or ""
        sub = by_name.get(title.lower()) or by_alias.get(title.lower())
        if not sub:
            unmatched.append(title)
            continue
        for text in (item.get("legality") or {}).get("international") or []:
            if clean(text):
                intl.append((sub, clean(text), f"https://effectindex.com/{item.get('slug')}", fetched))
        countries = (item.get("legality") or {}).get("countries") or {}
        if not countries:
            continue
        matched += 1
        url = f"https://effectindex.com/{item.get('slug')}"
        for country, entry in countries.items():
            status = clean(entry.get("status", ""))
            if not status:
                continue
            rows.append((sub, item.get("slug"), country, status, group(status), clean(entry.get("notes", "")), url, fetched))
    print(f"matched {matched} dose.wiki substances to ours, {len(rows)} country rows; {len(unmatched)} unmatched")
    print("unmatched sample:", unmatched[:25])
    groups = {}
    for r in rows:
        groups[r[4]] = groups.get(r[4], 0) + 1
    print("groups:", groups)
    others = sorted({r[3] for r in rows if r[4] == "unclear"})
    print("statuses grouped as unclear:", others[:40])
    if dry_run:
        return
    con.execute("""
        CREATE OR REPLACE TABLE substance_legality (
            substance VARCHAR, dosewiki_slug VARCHAR, country VARCHAR, status VARCHAR, status_group VARCHAR,
            notes VARCHAR, source_url VARCHAR, fetched_at VARCHAR)""")
    con.executemany("INSERT INTO substance_legality VALUES (?, ?, ?, ?, ?, ?, ?, ?)", rows)
    con.execute("COMMENT ON TABLE substance_legality IS 'Legal status by country from dose.wiki SubstanceIndex.json (CC0 1.0); see source_url.'")
    con.execute("CREATE OR REPLACE TABLE substance_legality_international (substance VARCHAR, statement VARCHAR, source_url VARCHAR, fetched_at VARCHAR)")
    con.executemany("INSERT INTO substance_legality_international VALUES (?, ?, ?, ?)", intl)
    print(f"Loaded substance_legality ({len(rows)} rows) and substance_legality_international ({len(intl)} rows) into md:sourcing.")


if __name__ == "__main__":
    main(sys.argv[1], dry_run="--dry-run" in sys.argv)
