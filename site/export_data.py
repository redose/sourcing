"""Export the tables re:dose reads from MotherDuck (sourcing database) to Parquet files for the static site.

The static build queries these files in the visitor's own browser with DuckDB-WASM, so no server or database token is
involved. Run whenever the data changes, then rebuild:
    motherduck_token=... python site/export_data.py        # writes site/data/*.parquet
"""

import os
import sys

import duckdb

TABLES = [
    "report_doses", "llm_extractions", "llm_observation_effects", "experience_reports", "llm_observations", "effects",
    "substances", "timeline_sentiment", "tripsit_group_members", "street_names", "erowid_substance_map",
    "substance_aliases", "routes_of_administration", "report_timeline", "tripsit_combos", "substance_structures",
    "substance_effects", "interactions", "alias_mixes", "world_countries", "substance_overviews", "substance_legality",
    "substance_legality_international", "substance_harms", "report_phases", "effect_replications",
]

out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(out, exist_ok=True)
c = duckdb.connect("md:sourcing")
total = 0
for t in sys.argv[1:] or TABLES:
    path = os.path.join(out, f"{t}.parquet")
    # The reports' full text stays in its file and is read in slices, so keep those slices small and sorted by id:
    # opening one report then downloads about a megabyte, not the whole 27.
    sliced = t == "experience_reports"
    order, rows = ("ORDER BY id", 400) if sliced else ("", 20000)
    c.execute(f"COPY (SELECT * FROM sourcing.main.{t} {order}) TO '{path}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE {rows})")
    size = os.path.getsize(path)
    total += size
    print(f"{t:36} {size / 1e6:7.2f} MB")
print(f"{'total':36} {total / 1e6:7.2f} MB")
