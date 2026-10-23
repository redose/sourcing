"""Sort every name a substance goes by into kinds, and load them into MotherDuck (sourcing database).

Creates (or replaces) one table:
  substance_aliases  one row per substance and name: the name, its kind (chemical, brand, slang or mix), where it
                     came from (PsychonautWiki's common names, the slang lists, PubChem's IUPAC name), and how the
                     kind was decided.

Names come from substances.common_names, street_names (minus entries about paraphernalia or dealing, as on the site)
and substance_structures.iupac_name. Kinds are decided by an AI model through MotherDuck's prompt() function, one
call per substance; the IUPAC name is always "chemical". Lists' sources are kept so the site can still credit them.

Run with a MotherDuck token in the `motherduck_token` environment variable:
    python load_aliases.py            # classify everything and load the table
    python load_aliases.py MDMA LSD   # classify just these and print, write nothing
"""

import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import duckdb

KINDS = {"chemical", "brand", "slang", "mix"}
JUNK = "pipe|crew|sham|seller|dealer|user|vial|paraphernalia|equipment|house|amount|price|person|police|addict|buy|sell|smok|inject|money"
PROMPT = (
    "Classify each alias of the drug {sub} into exactly one kind: chemical (systematic, chemical or scientific name, "
    "or its abbreviation), brand (a pharmaceutical or commercial product name), slang (a street or informal name), "
    "or mix (only when the name itself refers to combining it with another drug, e.g. 'candy flipping' or "
    "'... (mixed with pcp)'). Shortened or foreign product names are brand, not mix. Reply with one line per alias in "
    "the form alias<TAB>kind and nothing else. Aliases: {names}"
)

local = threading.local()


def con():
    if not hasattr(local, "c"):
        local.c = duckdb.connect("md:sourcing")
    return local.c


def names_for(c, sub):
    rows = c.execute(f"""
        SELECT n, 'psychonautwiki' AS src FROM (SELECT unnest(coalesce(common_names, [])) AS n FROM substances WHERE name = ?)
        UNION ALL
        SELECT street_name, unnest(coalesce(sources, ['lists'])) FROM street_names
        WHERE substance = ? AND NOT regexp_matches(lower(coalesce(description, '')), '{JUNK}')
    """, [sub, sub]).fetchall()
    out = {}
    for n, src in rows:
        if n and n.strip():
            out.setdefault(n.strip(), set()).add(src)
    return out


def classify(sub):
    c = con()
    names = names_for(c, sub)
    iupac = c.execute("SELECT iupac_name FROM substance_structures WHERE substance = ? AND compound IS NULL AND iupac_name IS NOT NULL", [sub]).fetchone()
    kinds = {}
    items = list(names)
    for i in range(0, len(items), 80):
        chunk = items[i:i + 80]
        text = PROMPT.format(sub=sub, names=" | ".join(chunk))
        reply = c.execute("SELECT prompt(?)", [text]).fetchone()[0] or ""
        for line in reply.splitlines():
            parts = line.replace("<TAB>", "\t").split("\t")
            if len(parts) >= 2:
                k = parts[-1].strip().lower()
                if k in KINDS:
                    kinds[parts[0].strip()] = k
    rows = []
    for n, srcs in names.items():
        rows.append((sub, n, kinds.get(n, "slang" if srcs - {"psychonautwiki"} else "chemical"), sorted(srcs), "ai" if n in kinds else "default"))
    if iupac and iupac[0]:
        rows.append((sub, iupac[0], "chemical", ["pubchem"], "iupac"))
    return rows


def main(only):
    c = duckdb.connect("md:sourcing")
    subs = only or [r[0] for r in c.sql("SELECT name FROM substances ORDER BY name").fetchall()]
    rows = []
    with ThreadPoolExecutor(max_workers=8) as pool:
        for i, r in enumerate(pool.map(classify, subs)):
            rows.extend(r)
            if (i + 1) % 25 == 0:
                print(f"  {i + 1}/{len(subs)} substances", flush=True)
    kinds = {}
    for r in rows:
        kinds[r[2]] = kinds.get(r[2], 0) + 1
    print(f"{len(rows)} names for {len(subs)} substances: {kinds}")
    if only:
        for r in rows:
            print(" ", r[2].ljust(8), r[1], r[3])
        return
    generated = datetime.now(timezone.utc).isoformat(timespec="seconds")
    c.execute("""CREATE OR REPLACE TABLE substance_aliases (
        substance VARCHAR, alias VARCHAR, kind VARCHAR, sources VARCHAR[], decided_by VARCHAR, generated_at VARCHAR)""")
    c.executemany("INSERT INTO substance_aliases VALUES (?, ?, ?, ?, ?, ?)", [(*r, generated) for r in rows])
    c.execute("COMMENT ON TABLE substance_aliases IS 'Every name a substance goes by, sorted into chemical/brand/slang/mix by an AI model via prompt(); sources kept.'")
    print("Loaded substance_aliases into md:sourcing.")


if __name__ == "__main__":
    main(sys.argv[1:])
