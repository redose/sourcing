"""Load 3D molecule structures from PubChem into MotherDuck (sourcing database).

Creates (or replaces) one table:
  substance_structures  one row per substance with a single well-defined molecule: PubChem CID, formula,
                        heavy atoms with 3D coordinates, and bonds, for the explorer's rotating molecule.

For each substance we ask PubChem for a compound by its name (then its common names), and take PubChem's
computed 3D conformer. Groups of drugs, plants and mixtures usually don't resolve, and are skipped.
PubChem data is freely reusable; we credit PubChem and link the compound page.

Plants, mushrooms and brews aren't one molecule, so --actives adds their main active compounds as extra rows
(compound = the molecule's short name; NULL on the rows where the substance is the molecule itself).

Run with a MotherDuck token in the `motherduck_token` environment variable:
    python load_structures.py            # look up every substance and load the table
    python load_structures.py --dry-run  # look up and print, write nothing
    python load_structures.py --actives  # add the active compounds of plants and mixtures (keeps the rest)
"""

import json
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone

import duckdb

API = "https://pubchem.ncbi.nlm.nih.gov/rest/pug"
# Names that are groups, plants or mixtures rather than one molecule.
SKIP = {
    "Inhalants", "Antidepressants", "Opioids", "Stimulants", "Sedative", "Barbiturates", "Benzodiazepines",
    "Substituted amphetamines", "Selective serotonin reuptake inhibitor", "Serotonin-norepinephrine reuptake inhibitor",
    "Monoamine oxidase inhibitor", "Cannabis", "Psilocybin mushrooms", "Peganum harmala", "Ayahuasca", "Kratom",
    "Datura", "Salvia divinorum", "Peyote", "San Pedro",
}
# Where the name alone finds the wrong thing or nothing.
ALIAS = {"Alcohol": "ethanol", "LSD": "lysergide", "MDMA": "3,4-methylenedioxymethamphetamine", "DMT": "N,N-dimethyltryptamine",
         "MDA": "3,4-methylenedioxyamphetamine"}
# The main active compounds of substances that aren't one molecule: (short name, PubChem search name).
ACTIVES = {
    "Cannabis": [("THC", "dronabinol"), ("CBD", "cannabidiol")],
    "Psilocybin mushrooms": [("Psilocybin", "psilocybin"), ("Psilocin", "psilocin")],
    "Salvia divinorum": [("Salvinorin A", "salvinorin A")],
    "Peganum harmala": [("Harmine", "harmine"), ("Harmaline", "harmaline")],
    "Banisteriopsis caapi": [("Harmine", "harmine"), ("Harmaline", "harmaline")],
    "Kratom": [("Mitragynine", "mitragynine"), ("7-OH-mitragynine", "7-hydroxymitragynine")],
    "Datura": [("Scopolamine", "scopolamine"), ("Hyoscyamine", "hyoscyamine")],
    "Atropa belladonna": [("Hyoscyamine", "hyoscyamine"), ("Scopolamine", "scopolamine")],
    "Hyoscyamus niger (botany)": [("Hyoscyamine", "hyoscyamine"), ("Scopolamine", "scopolamine")],
    "Amanita muscaria": [("Muscimol", "muscimol"), ("Ibotenic acid", "ibotenic acid")],
    "Morning glory": [("LSA", "ergine")],
    "Ayahuasca": [("DMT", "N,N-dimethyltryptamine"), ("Harmine", "harmine")],
    "Changa": [("DMT", "N,N-dimethyltryptamine"), ("Harmine", "harmine")],
    "Blue Lotus": [("Nuciferine", "nuciferine"), ("Aporphine", "aporphine")],
    "Cocoa": [("Theobromine", "theobromine"), ("Caffeine", "caffeine")],
    "Poppers": [("Amyl nitrite", "amyl nitrite")],
    "Peyote": [("Mescaline", "mescaline")],
    "San Pedro": [("Mescaline", "mescaline")],
}


def get(path: str):
    url = f"{API}/{path}"
    req = urllib.request.Request(url, headers={"User-Agent": "re:dose explorer (structures loader)"})
    with urllib.request.urlopen(req, timeout=30) as r:
        return r.read().decode()


def cid_for(name: str):
    try:
        data = json.loads(get(f"compound/name/{urllib.parse.quote(name)}/cids/JSON"))
        return data["IdentifierList"]["CID"][0]
    except Exception:
        return None


def parse_sdf(sdf: str):
    """Atoms (element, x, y, z) and bonds (a, b, order) from a V2000 molfile; hydrogens dropped."""
    lines = sdf.splitlines()
    counts = lines[3]
    n_atoms, n_bonds = int(counts[0:3]), int(counts[3:6])
    atoms, keep = [], {}
    for i in range(n_atoms):
        ln = lines[4 + i]
        x, y, z, el = float(ln[0:10]), float(ln[10:20]), float(ln[20:30]), ln[31:34].strip()
        if el != "H":
            keep[i + 1] = len(atoms)
            atoms.append([el, round(x, 3), round(y, 3), round(z, 3)])
    bonds = []
    for i in range(n_bonds):
        ln = lines[4 + n_atoms + i]
        a, b, order = int(ln[0:3]), int(ln[3:6]), int(ln[6:9])
        if a in keep and b in keep:
            bonds.append([keep[a], keep[b], order])
    return atoms, bonds


def structure(query: str):
    """(cid, formula, iupac, atoms, bonds) for a PubChem name, or None."""
    cid = cid_for(query)
    time.sleep(0.25)
    if not cid:
        return None
    try:
        sdf = get(f"compound/cid/{cid}/SDF?record_type=3d")
        time.sleep(0.25)
        props = json.loads(get(f"compound/cid/{cid}/property/MolecularFormula,IUPACName/JSON"))["PropertyTable"]["Properties"][0]
        time.sleep(0.25)
        atoms, bonds = parse_sdf(sdf)
    except Exception:
        return None
    return cid, props.get("MolecularFormula"), props.get("IUPACName"), atoms, bonds


def actives(dry_run: bool):
    con = duckdb.connect("md:sourcing")
    have = {r[0] for r in con.sql("SELECT name FROM substances").fetchall()}
    fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = []
    for sub, compounds in ACTIVES.items():
        if sub not in have:
            print(f"  -  {sub}: not in substances")
            continue
        for short, query in compounds:
            got = structure(query)
            if not got:
                print(f"  -  {sub} / {short}: no 3D structure")
                continue
            cid, formula, iupac, atoms, bonds = got
            out.append((sub, cid, formula, iupac, json.dumps(atoms), json.dumps(bonds), f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}", fetched, short))
            print(f"  ok {sub} / {short}: CID {cid}, {formula}, {len(atoms)} atoms")
    # Single molecules the first run missed (a name that needed an alias).
    missing = [n for n in ALIAS if n in have and not con.execute("SELECT 1 FROM substance_structures WHERE substance = ?", [n]).fetchone()]
    for name in missing:
        got = structure(ALIAS[name])
        if got:
            cid, formula, iupac, atoms, bonds = got
            out.append((name, cid, formula, iupac, json.dumps(atoms), json.dumps(bonds), f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}", fetched, None))
            print(f"  ok {name}: CID {cid}, {formula}, {len(atoms)} atoms")
    print(f"{len(out)} structures")
    if dry_run:
        return
    con.execute("ALTER TABLE substance_structures ADD COLUMN IF NOT EXISTS compound VARCHAR")
    con.execute("DELETE FROM substance_structures WHERE compound IS NOT NULL")
    con.executemany("INSERT INTO substance_structures VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", out)
    print("Added active compounds to substance_structures.")


def main(dry_run: bool):
    con = duckdb.connect("md:sourcing")
    subs = con.sql("SELECT name, coalesce(common_names, []) FROM substances ORDER BY name").fetchall()
    fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
    out = []
    for name, aka in subs:
        if name in SKIP:
            continue
        cid = None
        for q in [ALIAS.get(name, name), *aka[:3]]:
            cid = cid_for(q)
            time.sleep(0.25)  # PubChem asks for at most 5 requests a second
            if cid:
                break
        if not cid:
            print(f"  -  {name}: not found")
            continue
        try:
            sdf = get(f"compound/cid/{cid}/SDF?record_type=3d")
            time.sleep(0.25)
            props = json.loads(get(f"compound/cid/{cid}/property/MolecularFormula,IUPACName/JSON"))["PropertyTable"]["Properties"][0]
            time.sleep(0.25)
            atoms, bonds = parse_sdf(sdf)
        except Exception as e:  # no 3D conformer (salts, big or odd molecules)
            print(f"  -  {name}: CID {cid}, no 3D ({type(e).__name__})")
            continue
        out.append((name, cid, props.get("MolecularFormula"), props.get("IUPACName"), json.dumps(atoms), json.dumps(bonds),
                    f"https://pubchem.ncbi.nlm.nih.gov/compound/{cid}", fetched))
        print(f"  ok {name}: CID {cid}, {props.get('MolecularFormula')}, {len(atoms)} atoms")
    print(f"{len(out)} structures")
    if dry_run:
        return
    con.execute("""
        CREATE OR REPLACE TABLE substance_structures (
            substance VARCHAR, pubchem_cid BIGINT, formula VARCHAR, iupac_name VARCHAR,
            atoms JSON, bonds JSON, source_url VARCHAR, fetched_at VARCHAR)""")
    con.executemany("INSERT INTO substance_structures VALUES (?, ?, ?, ?, ?, ?, ?, ?)", out)
    con.execute("COMMENT ON TABLE substance_structures IS '3D structures (heavy atoms) from PubChem computed conformers; see source_url.'")
    print("Loaded substance_structures into md:sourcing.")


if __name__ == "__main__":
    if "--actives" in sys.argv:
        actives(dry_run="--dry-run" in sys.argv)
    else:
        main(dry_run="--dry-run" in sys.argv)
