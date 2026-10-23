"""Load 3D molecule structures from PubChem into MotherDuck (sourcing database).

Creates (or replaces) one table:
  substance_structures  one row per substance with a single well-defined molecule: PubChem CID, formula,
                        heavy atoms with 3D coordinates, and bonds, for the explorer's rotating molecule.

For each substance we ask PubChem for a compound by its name (then its common names), and take PubChem's
computed 3D conformer. Groups of drugs, plants and mixtures usually don't resolve, and are skipped.
PubChem data is freely reusable; we credit PubChem and link the compound page.

Run with a MotherDuck token in the `motherduck_token` environment variable:
    python load_structures.py            # look up every substance and load the table
    python load_structures.py --dry-run  # look up and print, write nothing
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
ALIAS = {"Alcohol": "ethanol", "LSD": "lysergide", "MDMA": "3,4-methylenedioxymethamphetamine", "DMT": "N,N-dimethyltryptamine"}


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
    main(dry_run="--dry-run" in sys.argv)
