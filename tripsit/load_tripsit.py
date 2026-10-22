"""Load TripSit's drug-combination chart into MotherDuck (sourcing database).

Creates (or replaces) two tables:
  tripsit_combos         one row per unordered pair of TripSit groups: status, note, severity rank
  tripsit_group_members  which substances (our canonical names) belong to each TripSit group

Source: https://github.com/TripSit/drugs (combos.json). The repository has no license file;
attribution is kept in every row. Ask TripSit before republishing the notes publicly.

Run with a MotherDuck token in the `motherduck_token` environment variable:
    python load_tripsit.py            # load and print the group membership for review
    python load_tripsit.py --dry-run  # print what would be loaded, write nothing
"""

import json
import sys
import urllib.request
from datetime import datetime, timezone

import duckdb

SOURCE = "https://raw.githubusercontent.com/TripSit/drugs/main/combos.json"
ATTRIBUTION = "TripSit drug combinations chart (github.com/TripSit/drugs)"

# TripSit status → our severity rank (3 dangerous … 0 low risk). Low-risk statuses keep their own label.
RANK = {
    "Dangerous": 3,
    "Unsafe": 2,
    "Caution": 1,
    "Low Risk & Synergy": 0,
    "Low Risk & No Synergy": 0,
    "Low Risk & Decrease": 0,
}

# TripSit groups that are a single drug (or a few close equivalents), by our canonical names.
DIRECT = {
    "alcohol": ["Alcohol"],
    "caffeine": ["Caffeine"],
    "cannabis": ["Cannabis", "Tetrahydrocannabinol"],
    "cocaine": ["Cocaine"],
    "dmt": ["DMT", "Ayahuasca", "Anadenanthera peregrina"],
    "ketamine": ["Ketamine"],
    "lsd": ["LSD", "1P-LSD", "1B-LSD", "1cP-LSD", "1V-LSD"],
    "mdma": ["MDMA"],
    "mephedrone": ["Mephedrone", "4-Methylmethcathinone"],
    "mescaline": ["Mescaline", "Peyote", "San Pedro"],
    "mushrooms": ["Psilocybin mushrooms", "Psilocybin", "Psilocin"],
    "mxe": ["Methoxetamine"],
    "nitrous": ["Nitrous"],
    "pcp": ["PCP"],
    "pregabalin": ["Pregabalin"],
    "tramadol": ["Tramadol"],
    "dextromethorphan": ["Dextromethorphan"],
    "diphenhydramine": ["Diphenhydramine"],
    "lithium": ["Lithium"],
    "amt": ["αMT", "ΑMT", "AMT"],
    "ghb/gbl": ["GHB", "GBL", "1,4-Butanediol"],
    # Safety-critical drug-action classes, curated by hand.
    "ssris": ["Selective serotonin reuptake inhibitor", "Sertraline", "Fluoxetine", "Paroxetine", "Citalopram", "Escitalopram", "Fluvoxamine"],
    "maois": [
        "Monoamine oxidase inhibitor", "Peganum harmala", "Banisteriopsis caapi", "Ayahuasca", "Harmaline", "Harmine",
        "Phenelzine", "Moclobemide", "Selegiline", "Tranylcypromine",
    ],
}


def class_members(subs):
    """Groups defined by drug class, matched against our substances' classes and names."""
    out = {k: set() for k in ["amphetamines", "benzodiazepines", "opioids", "2c-x", "2c-t-x", "5-meo-xxt", "dox", "nbomes"]}
    for name, psy, chem in subs:
        n, psy_l, chem_l = name.lower(), [p.lower() for p in psy], [c.lower() for c in chem]
        chem_s = " ".join(chem_l)
        if "amphetamine" in chem_s and any(p.startswith("stimulant") for p in psy_l) and name not in ("MDMA", "MDA"):
            out["amphetamines"].add(name)
        if "benzodiazepine" in chem_s or "thienodiazepine" in chem_s:
            out["benzodiazepines"].add(name)
        if any(p.startswith("opioid") for p in psy_l):
            out["opioids"].add(name)
        if n.startswith("2c-t"):
            out["2c-t-x"].add(name)
        elif n.startswith("2c-"):
            out["2c-x"].add(name)
        if n.startswith("5-meo-"):
            out["5-meo-xxt"].add(name)
        if n in ("dom", "doc", "doi", "dob", "dox", "doet", "dom-fly"):
            out["dox"].add(name)
        if "nbome" in n or "nboh" in n:
            out["nbomes"].add(name)
    # Generic class entries that appear in reports as if they were one drug.
    out["amphetamines"] |= {"Amphetamine", "Methamphetamine", "Lisdexamfetamine", "Dextroamphetamine", "Substituted amphetamines"}
    out["benzodiazepines"] |= {"Benzodiazepines"}
    out["opioids"] |= {"Opioids"}
    return out


def main(dry_run: bool):
    with urllib.request.urlopen(SOURCE, timeout=30) as resp:
        combos = json.load(resp)
    fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")

    pairs = {}
    for a, row in combos.items():
        for b, v in row.items():
            if a == b or not isinstance(v, dict) or "status" not in v:
                continue
            key = tuple(sorted((a, b)))
            pairs.setdefault(key, (v["status"], v.get("note")))

    con = duckdb.connect("md:sourcing")
    subs = con.sql("SELECT name, coalesce(psychoactive_class, []), coalesce(chemical_class, []) FROM substances").fetchall()
    known = {r[0] for r in con.sql(
        "SELECT name FROM substances UNION SELECT DISTINCT canonical_substance FROM report_doses WHERE canonical_substance IS NOT NULL"
    ).fetchall()}

    members = {g: {m for m in names} for g, names in DIRECT.items()}
    for g, names in class_members(subs).items():
        members.setdefault(g, set()).update(names)
    # Keep only names we actually have (plus the curated pharma names, so future data matches).
    curated = set(DIRECT["ssris"]) | set(DIRECT["maois"])
    member_rows = sorted(
        (g, m, "curated" if m in curated else ("direct" if m in DIRECT.get(g, []) else "class"))
        for g, ms in members.items() for m in ms if m in known or m in curated
    )

    combo_rows = [(a, b, s, RANK.get(s), n, ATTRIBUTION, SOURCE, fetched) for (a, b), (s, n) in sorted(pairs.items())]

    print(f"{len(combo_rows)} group pairs, {len(member_rows)} group memberships")
    for g in sorted({r[0] for r in member_rows}):
        print(f"  {g:18s} {', '.join(m for gg, m, _ in member_rows if gg == g)}")
    missing = sorted(set(combos) - {r[0] for r in member_rows})
    if missing:
        print("  groups with no matching substance:", ", ".join(missing))
    if dry_run:
        return

    con.execute("""
        CREATE OR REPLACE TABLE tripsit_combos (
            group_a VARCHAR, group_b VARCHAR, status VARCHAR, severity_rank INTEGER, note VARCHAR,
            attribution VARCHAR, source_url VARCHAR, fetched_at VARCHAR)""")
    con.executemany("INSERT INTO tripsit_combos VALUES (?, ?, ?, ?, ?, ?, ?, ?)", combo_rows)
    con.execute("CREATE OR REPLACE TABLE tripsit_group_members (group_key VARCHAR, substance VARCHAR, matched_by VARCHAR)")
    con.executemany("INSERT INTO tripsit_group_members VALUES (?, ?, ?)", member_rows)
    con.execute(f"COMMENT ON TABLE tripsit_combos IS 'From {ATTRIBUTION}; fetched {fetched}. No license file in source repo.'")
    print("Loaded tripsit_combos and tripsit_group_members into md:sourcing.")


if __name__ == "__main__":
    main(dry_run="--dry-run" in sys.argv)
