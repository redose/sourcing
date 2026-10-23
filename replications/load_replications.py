"""Load metadata about Effect Index's replications into MotherDuck (sourcing database). No images are copied.

Creates (or replaces) one table:
  effect_replications  one row per replication: its title, the effect it replicates, the artist, the kind of media,
                       and the link to its page on Effect Index.

Replications are art made by their creators; rights stay with them (https://effectindex.com/docs/license). We only
store facts about each one (title, effect, artist) and link to it, so people view it on Effect Index.

Source: the replication pages listed in https://effectindex.com/sitemap.xml, read politely (a few at a time), using
each page's schema.org metadata.

Run with a MotherDuck token in the `motherduck_token` environment variable:
    python load_replications.py          # read every page and load the table
    python load_replications.py 50       # read the first 50 and print, write nothing
"""

import json
import re
import sys
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import duckdb

UA = {"User-Agent": "re:dose (replication index; links back to Effect Index)"}


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def read(url):
    for attempt in range(3):
        try:
            html = get(url)
            break
        except Exception:
            time.sleep(2 + attempt * 3)
    else:
        return None
    time.sleep(0.15)
    for m in re.finditer(r'<script[^>]*type="application/ld\+json"[^>]*>(.*?)</script>', html, re.S):
        try:
            d = json.loads(m.group(1))
        except Exception:
            continue
        if d.get("@type") not in ("ImageObject", "VideoObject", "AudioObject", "MediaObject"):
            continue
        eff = re.search(r"subjective effects? (.+?)\.?$", d.get("description") or "")
        return (
            d.get("name"),
            eff.group(1).strip() if eff else None,
            (d.get("creator") or {}).get("name"),
            {"ImageObject": "image", "VideoObject": "video", "AudioObject": "audio"}.get(d["@type"], "media"),
            url,
        )
    return None


def main(limit):
    sitemap = get("https://effectindex.com/sitemap.xml")
    urls = re.findall(r"<loc>(https://effectindex.com/replications/[^<]+)</loc>", sitemap)
    urls = [u for u in urls if "/replications/artist/" not in u]
    if limit:
        urls = urls[:limit]
    rows = []
    with ThreadPoolExecutor(max_workers=4) as pool:
        for i, r in enumerate(pool.map(read, urls)):
            if r and r[1]:
                rows.append(r)
            if (i + 1) % 500 == 0:
                print(f"  {i + 1}/{len(urls)} pages, {len(rows)} replications", flush=True)
    print(f"{len(rows)} replications from {len(urls)} pages")
    if limit:
        for r in rows[:15]:
            print(" ", r)
        return
    fetched = datetime.now(timezone.utc).isoformat(timespec="seconds")
    con = duckdb.connect("md:sourcing")
    con.execute("""CREATE OR REPLACE TABLE effect_replications (
        title VARCHAR, effect VARCHAR, artist VARCHAR, media VARCHAR, url VARCHAR, fetched_at VARCHAR)""")
    con.executemany("INSERT INTO effect_replications VALUES (?, ?, ?, ?, ?, ?)", [(*r, fetched) for r in rows])
    con.execute("COMMENT ON TABLE effect_replications IS 'Metadata (title, effect, artist, link) for Effect Index replications; no images. Rights stay with the artists.'")
    print("Loaded effect_replications into md:sourcing.")


if __name__ == "__main__":
    main(int(sys.argv[1]) if len(sys.argv) > 1 else 0)
