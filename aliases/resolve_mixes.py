"""Work out which "mix" names really are mixes, and what goes into each one. Loads into MotherDuck (sourcing database).

Creates (or replaces) one table:
  alias_mixes  one row per name that the slang lists flag as a combination (street_names.is_combo) or that
               load_aliases.py sorted as a mix: whether it really names a combination of drugs, which drugs, and a
               few plain words on what it is.

The lists' combo flag is loose: plenty of flagged names are just pills or slang ("percs", "blues"). An AI model reads
each name with the substances it's listed under and the lists' own descriptions, through MotherDuck's prompt().

Run with a MotherDuck token in the `motherduck_token` environment variable:
    python resolve_mixes.py                     # resolve everything and load the table
    python resolve_mixes.py "candy flip" percs  # resolve just these and print, write nothing
"""

import re
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import duckdb

PROMPT = (
    "Each line below is a drug street name, the drugs it is listed under, and what slang lists say about it. "
    "Decide whether the name refers to a MIX: two or more drugs taken, sold or prepared together (for example "
    "'candy flip' = LSD + MDMA, 'speedball' = heroin + cocaine). A name for one drug, a pill, a brand, a dose or a "
    "quality is NOT a mix, even if the lists say it is. Reply with one line per name, in the same order, in the form "
    "name<TAB>yes or no<TAB>the drugs it combines, comma-separated, using common names (empty if no)<TAB>what it is "
    "in at most eight plain words. Nothing else.\n\n{lines}"
)

local = threading.local()


def con():
    if not hasattr(local, "c"):
        local.c = duckdb.connect("md:sourcing")
    return local.c


def candidates(c, only=None):
    rows = c.execute("""
        WITH names AS (
          SELECT lower(street_name) AS k, street_name AS alias, substance, description FROM street_names WHERE is_combo
          UNION ALL
          SELECT lower(alias), alias, substance, NULL FROM substance_aliases WHERE kind = 'mix'
        )
        SELECT k, min(alias), list(DISTINCT substance ORDER BY substance), string_agg(DISTINCT description, '; ')
        FROM names GROUP BY k ORDER BY k
    """).fetchall()
    if only:
        want = {o.lower() for o in only}
        rows = [r for r in rows if r[0] in want] or [(o.lower(), o, [], None) for o in only]
    return rows


def resolve(chunk):
    c = con()
    lines = "\n".join(f"{alias} | listed under: {', '.join(subs) or 'unknown'} | lists say: {desc or 'nothing'}" for _, alias, subs, desc in chunk)
    reply = c.execute("SELECT prompt(?)", [PROMPT.format(lines=lines)]).fetchone()[0] or ""
    got = {}
    for line in reply.splitlines():
        parts = [p.strip() for p in line.replace("<TAB>", "\t").split("\t")]
        if len(parts) >= 2 and parts[1].lower() in ("yes", "no"):
            got[parts[0].lower()] = (
                parts[1].lower() == "yes",
                [p.strip() for p in (parts[2] if len(parts) > 2 else "").split(",") if p.strip()],
                parts[3] if len(parts) > 3 else "",
            )
    out = []
    for k, alias, subs, _ in chunk:
        # "x (mixed with mdma)" says so in the name itself: what it's listed under, plus the other drug.
        named = re.search(r"\((?:mixed|combined|laced|cut) with ([^)]+)\)", alias, re.I)
        if named and subs:
            out.append((alias, True, [subs[0], named.group(1).strip()], f"{subs[0]} mixed with {named.group(1).strip()}"))
        elif k in got:
            is_mix, drugs, what = got[k]
            out.append((alias, is_mix and len(drugs) >= 2, drugs if is_mix else [], what))
    return out


def tidy(done, names):
    """Drug names as the site spells them (Cannabis, not cannabis), and no stray placeholders in the notes."""
    spell = {n.lower(): n for n in names}
    spell.update({"marijuana": "Cannabis", "weed": "Cannabis", "ecstasy": "MDMA", "crack": "Cocaine", "crack cocaine": "Cocaine",
                  "pcp": "PCP", "lsd": "LSD", "ghb": "GHB", "meth": "Methamphetamine", "viagra": "Sildenafil"})
    out = []
    for alias, is_mix, parts, what in done:
        parts = list(dict.fromkeys(spell.get(p.lower(), p[:1].upper() + p[1:]) for p in parts))
        what = re.sub(r"^[|\s]+", "", what or "").strip().rstrip(".")
        if len(what) < 4 or what.lower() in ("empty", "none", "n/a") or not is_mix:
            what = "" if not is_mix else what
        out.append((alias, is_mix and len(parts) >= 2, parts if is_mix else [], what))
    return out


def main(only):
    c = duckdb.connect("md:sourcing")
    rows = candidates(c, only)
    chunks = [rows[i:i + 20] for i in range(0, len(rows), 20)]
    with ThreadPoolExecutor(8) as pool:
        done = [r for part in pool.map(resolve, chunks) for r in part]
    done = tidy(done, [r[0] for r in c.execute("SELECT name FROM substances").fetchall()])
    if only:
        for r in done:
            print(r)
        return
    c.execute("""CREATE OR REPLACE TABLE alias_mixes (
        alias VARCHAR, is_mix BOOLEAN, parts VARCHAR[], what VARCHAR, resolved_at TIMESTAMP)""")
    c.execute("COMMENT ON TABLE alias_mixes IS 'Names flagged as mixes, checked by an AI model via prompt(): whether each really is a combination, and of what.'")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    c.executemany("INSERT INTO alias_mixes VALUES (?, ?, ?, ?, ?)", [(*r, now) for r in done])
    n_mix = sum(1 for r in done if r[1])
    print(f"alias_mixes: {len(done)} of {len(rows)} names resolved, {n_mix} real mixes")


if __name__ == "__main__":
    main(sys.argv[1:])
