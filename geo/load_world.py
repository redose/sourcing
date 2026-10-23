"""Load simplified world country outlines into MotherDuck (sourcing database), for the legal-status map.

Creates (or replaces) one table:
  world_countries  one row per country: its name (spelled the way substance_legality spells it) and its outline as
                   GeoJSON (longitude/latitude, rounded to 0.01 degree).

Source: Natural Earth 1:110m countries (public domain) as packaged by world-atlas
(https://cdn.jsdelivr.net/npm/world-atlas@2.0.2/countries-110m.json), converted here from TopoJSON. Antarctica is left
out. Tiny countries (Singapore, Malta…) aren't in the 1:110m set; the site lists them under the map.

Run with a MotherDuck token in the `motherduck_token` environment variable:
    python load_world.py PATH/TO/countries-110m.json
"""

import json
import sys

import duckdb

# world-atlas names → the names substance_legality uses.
RENAME = {
    "United States of America": "United States", "Czechia": "Czech Republic", "Bosnia and Herz.": "Bosnia and Herzegovina",
    "Dominican Rep.": "Dominican Republic", "Central African Rep.": "Central African Republic", "Dem. Rep. Congo": "DR Congo",
    "Eq. Guinea": "Equatorial Guinea", "S. Sudan": "South Sudan", "Solomon Is.": "Solomon Islands", "Macedonia": "North Macedonia",
    "eSwatini": "Eswatini", "W. Sahara": "Western Sahara", "Falkland Is.": "Falkland Islands", "N. Cyprus": "Northern Cyprus",
}


def decode(topo):
    sx, sy = topo["transform"]["scale"]
    tx, ty = topo["transform"]["translate"]
    arcs = []
    for arc in topo["arcs"]:
        x = y = 0
        pts = []
        for dx, dy in arc:
            x += dx
            y += dy
            pts.append([round(x * sx + tx, 2), round(y * sy + ty, 2)])
        arcs.append(pts)

    def ring(ids):
        out = []
        for i in ids:
            pts = arcs[i] if i >= 0 else arcs[~i][::-1]
            out.extend(pts if not out else pts[1:])
        return out

    feats = []
    for g in topo["objects"]["countries"]["geometries"]:
        name = g["properties"].get("name")
        if not name or name == "Antarctica" or g.get("type") is None:
            continue
        if g["type"] == "Polygon":
            geom = {"type": "Polygon", "coordinates": [ring(r) for r in g["arcs"]]}
        else:
            geom = {"type": "MultiPolygon", "coordinates": [[ring(r) for r in poly] for poly in g["arcs"]]}
        feats.append((RENAME.get(name, name), json.dumps(geom, separators=(",", ":"))))
    return feats


def main(path):
    feats = decode(json.load(open(path)))
    con = duckdb.connect("md:sourcing")
    con.execute("CREATE OR REPLACE TABLE world_countries (country VARCHAR, geojson VARCHAR)")
    con.executemany("INSERT INTO world_countries VALUES (?, ?)", feats)
    con.execute("COMMENT ON TABLE world_countries IS 'Natural Earth 1:110m country outlines (public domain) via world-atlas, as GeoJSON.'")
    print(f"Loaded {len(feats)} countries, {sum(len(f[1]) for f in feats) // 1024} KB of outlines.")


if __name__ == "__main__":
    main(sys.argv[1])
