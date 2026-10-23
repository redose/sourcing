"""Pull "context events" (set and setting) out of the closely-read Erowid reports. Loads into MotherDuck (sourcing).

Creates (or replaces) one table, only on a full run:
  llm_context_events  one row per non-drug thing the writer did or that happened around them that shapes the
                      experience: sleep, food, water, exercise, meditation, setting (moved place), social (people
                      came or went), activity (music, shower, walk...), mindset (intentions, preparation), other.
                      Each has a time relative to the first dose where the story allows it, a short plain note,
                      and a verbatim quote from the story.

Source: experience_reports.body (the story) and llm_extractions (the 1,725 closely-read reports), whose dose list
anchors the times: T+0:00 is the first dose, and each dose carries the writer's own clock time, so "lunch around
12 pm" before a 6:45 pm first dose becomes T-6:45. An AI model reads each story through MotherDuck's prompt().

Checks: an event is kept only if its quote is found in the story (ignoring case, runs of whitespace and curly
quotes); the rest are dropped and counted. time_relative ("T-1:00", "T+2:30") is parsed into mins, negative before
the first dose; anything that doesn't parse becomes null.

Run with a MotherDuck token in the `motherduck_token` environment variable:
    python context_events.py                          # every report; writes the table
    python context_events.py --limit 40               # pilot on 40 reports; prints, writes nothing
    python context_events.py --ids erowid-100106 erowid-106954   # just these; prints, writes nothing
    python context_events.py --cache replies.jsonl    # keep replies as they arrive, so a rerun picks up where it stopped
Each report is one prompt() call (gpt-5, low reasoning effort, JSON-schema output); a failed call is retried once.
A full run writes the table only when every report succeeded (or with --allow-partial). MotherDuck caps AI use per
session (10 AI units by default, reset every 24 hours; about 300 reports with gpt-5): once the cap is hit the run stops
asking, and a rerun with the same --cache carries on from where it stopped.
"""

import argparse
import json
import re
import threading
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import duckdb

MODEL = "gpt-5"
EFFORT = "low"
THREADS = 8
KINDS = ["sleep", "food", "water", "exercise", "meditation", "setting", "social", "activity", "mindset", "other"]

PROMPT = """You are reading a first-person drug experience report. Its doses were already pulled out (listed below),
each with a time relative to the first dose (T+0:00) and the writer's own clock time where given.

Your job: list the NON-DRUG things the writer did, or that happened around them, that shape the experience
(set and setting) - before the first dose as well as during and after.

Kinds:
- sleep: slept, napped, woke up, went to bed, stayed up all night
- food: ate a meal or snack, or fasted / had an empty stomach
- water: drank water, juice, soda or another non-drug, non-caffeine drink
- exercise: worked out, ran, danced hard, swam
- meditation: meditated, yoga, breathing exercises, prayed
- setting: moved to a different place or the surroundings changed (went outside, drove to a park, arrived at the
  party, went to the bedroom, turned the lights off, it started raining)
- social: people arrived or left, talked or phoned someone, was left alone
- activity: music, gaming, TV or a film, shower or bath, walk, sex, drawing, reading, cleaning, work, school
- mindset: stated intentions or preparation before starting (set an intention, planned the trip all week,
  prepared a playlist, told a sitter, researched the drug)
- other: anything else that fits none of the above

Pick what shapes the experience, not every movement. Good events: sleep, meals and fasting before dosing;
preparation and intentions; where they were and every change of place; people arriving, leaving, calling, or
being alone; deliberate activities (music, a film, gaming, a walk, a shower, sex, work, an exam); going to bed,
falling asleep and waking up afterwards. Skip moment-to-moment actions during the trip (sat down, lay down,
walked around the room, stared at something, picked something up, fell over, broke things, yelled) unless they
change the setting.

Strictly leave out:
- ANY event that is taking a substance, whether or not it is in the dose list: smoking, vaping, snorting, a line
  or bump, swallowing a pill, eating mushrooms or edibles, drinking alcohol, coffee, tea or energy drinks,
  tobacco, nicotine, kratom, nootropics, medication, supplements, vitamins, injecting. Also leave out buying,
  weighing, rolling, packing or preparing drugs, handling pipes, bongs, lighters, torches or needles, and other
  people taking drugs. If a sentence mixes a drug with something else ("smoked a bowl with my friend who just
  arrived"), keep only the non-drug part (the friend arriving) and quote only that part.
- feelings, effects, visuals, thoughts, reflections or symptoms. Vomiting, nausea, sweating, passing out or
  being unable to sleep because of the drug are effects, not events. Opinions written afterwards are not mindset.
- vague background with no place in this day's timeline (past drug use, general life history, who they are),
  and anything later than the day after (writing the report, later life).
- the report's header lines (Date:, Time:, Dose:, BODY WEIGHT, Erowid notes). Only the header's "Place:" line
  may be used, as one setting event at T+0:00.

Time (time_relative): when it happened relative to the first dose, written "T-H:MM" before it or "T+H:MM" after it
(e.g. "T-6:45", "T-0:30", "T+1:30"). Give a time whenever the story lets you place it, even roughly:
- Many reports start paragraphs with a clock time ("7:35 pm:") or an offset ("T+1:30", "+2 hrs"). Events in that
  paragraph take that time, converted relative to the first dose's clock time.
- Compare clock times in the story with the dose clock times: a 6:45 pm first dose and lunch "around 12 pm"
  gives T-6:45; going to bed at 2 am after a 10 pm first dose gives T+4:00.
- Use stated offsets and durations ("an hour before I dropped", "about 30 minutes in", "two hours later").
- When several things are done in a row just before dosing, count their stated durations back from the dose
  (napped an hour, then meditated 15 minutes, then dosed: nap T-1:15, meditation T-0:15).
- An event told between two timed events can sit between them.
Use null when there is truly no clue (and for preparation over days, like "planned all week"). Don't invent
precision you don't have; rough is fine.

note: at most 8 plain words, in English, lowercase, past tense, no drug names. Examples: "napped an hour",
"ate a foot-long sandwich", "friend arrived", "went for a walk outside", "listened to music in bed".

quote: a short span (3 to 15 words) copied EXACTLY, character for character, from the story (in its own
language), showing the event. Do not fix spelling, punctuation or capitals, and do not join pieces from
different sentences.

List events in the order they happened, at most 12. If there are none, return an empty list.

DOSES:
{doses}

STORY:
{body}
"""

SCHEMA = json.dumps({
    "name": "context_events",
    "strict": True,
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "required": ["events"],
        "properties": {"events": {"type": "array", "items": {
            "type": "object",
            "additionalProperties": False,
            "required": ["time_relative", "kind", "note", "quote"],
            "properties": {
                "time_relative": {"type": ["string", "null"]},
                "kind": {"type": "string", "enum": KINDS},
                "note": {"type": "string"},
                "quote": {"type": "string"},
            },
        }}},
    },
})

# A safety net under the prompt: notes that still describe taking or handling a substance are dropped and counted.
DRUGGY = re.compile(r"\b(smok\w*|vap(e|ed|ing)|snort\w*|inject\w*|bong|pipe|joint|blunt|bowl|torch|needle|syringe|"
                    r"cigarettes?|coffee|beers?|wine|alcohol|drunk|dos(e|ed|ing)|pills?|tabs?|lines? of|bumps?|kratom)\b", re.I)

local = threading.local()
lock = threading.Lock()


def con():
    if not hasattr(local, "c"):
        local.c = duckdb.connect("md:sourcing")
    return local.c


def reports(c, limit=None, ids=None):
    sql = """
        SELECT l.report_id, e.body, CAST(l.extraction->'$.doses' AS VARCHAR)
        FROM llm_extractions l JOIN experience_reports e ON e.id = l.report_id
    """
    if ids:
        return c.execute(sql + " WHERE l.report_id IN (SELECT unnest(?)) ORDER BY l.report_id", [ids]).fetchall()
    if limit:
        # A stable spread of reports, not just the first ids.
        return c.execute(sql + " ORDER BY hash(l.report_id) LIMIT ?", [limit]).fetchall()
    return c.execute(sql + " ORDER BY l.report_id").fetchall()


def dose_lines(doses_json):
    try:
        doses = json.loads(doses_json or "[]") or []
    except ValueError:
        doses = []
    lines = []
    for d in doses:
        bits = [d.get("substance") or d.get("substance_original") or "?"]
        if d.get("amount"):
            bits.append(str(d["amount"]))
        if d.get("route"):
            bits.append(str(d["route"]))
        clock = f" (writer's clock: {d['time_original']})" if d.get("time_original") else ""
        lines.append(f"- {d.get('time_relative') or 'time unknown'}{clock}: {', '.join(bits)}")
    return "\n".join(lines) or "- (no doses extracted)"


def norm(s):
    s = (s or "").translate(str.maketrans({"‘": "'", "’": "'", "‚": "'", "‛": "'", "′": "'",
                                           "“": '"', "”": '"', "„": '"', "‟": '"', "″": '"',
                                           " ": " "}))
    return re.sub(r"\s+", " ", s).strip().lower()


def to_mins(t):
    m = re.fullmatch(r"T\s*([+-])\s*(\d{1,3}):(\d{2})", (t or "").strip())
    if not m:
        return None
    mins = int(m.group(2)) * 60 + int(m.group(3))
    return -mins if m.group(1) == "-" else mins


usage = Counter()
capped = threading.Event()  # MotherDuck's AI session cap: once hit, every later call fails too, so stop asking


def ask(row, cache=None):
    rid, body, doses = row
    text = PROMPT.format(doses=dose_lines(doses), body=body)
    err = "skipped: AI session cap reached"
    for attempt in range(2):  # one retry
        if capped.is_set():
            break
        try:
            reply = con().execute("SELECT prompt(?, model := ?, reasoning_effort := ?, json_schema := ?::JSON)",
                                  [text, MODEL, EFFORT, SCHEMA]).fetchone()[0]
            with lock:
                usage["calls"] += 1
                usage["chars_in"] += len(text)
                usage["chars_out"] += len(reply or "")
            if reply:
                events = (json.loads(reply) if isinstance(reply, str) else reply).get("events") or []
                if cache:
                    with lock, open(cache, "a") as f:
                        f.write(json.dumps({"id": rid, "events": events}) + "\n")
                return rid, body, events, None
            err = "empty reply"
        except Exception as e:  # noqa: BLE001 - network, parse or model errors all get the one retry
            err = str(e)[:200]
            if "session cap" in err:
                capped.set()
            if hasattr(local, "c"):
                del local.c
    return rid, body, None, err


def check(rid, body, events):
    """Keep events whose quote is in the story and whose note isn't drug use; tidy notes and times.
    Returns (kept rows, quote drops, drug drops)."""
    nb = norm(body)
    kept, dropped, druggy = [], 0, 0
    for ev in events:
        quote = (ev.get("quote") or "").strip()
        if len(norm(quote)) < 3 or norm(quote) not in nb:
            dropped += 1
            continue
        kind = ev.get("kind") if ev.get("kind") in KINDS else "other"
        note = re.sub(r"\s+", " ", ev.get("note") or "").strip().rstrip(".")
        note = " ".join(note.split()[:8])
        if DRUGGY.search(note):
            druggy += 1
            continue
        t = (ev.get("time_relative") or "").strip() or None
        mins = to_mins(t)
        if mins is None:
            t = None
        else:
            t = f"T{'-' if mins < 0 else '+'}{abs(mins) // 60}:{abs(mins) % 60:02d}"
        kept.append((rid, len(kept), t, mins, kind, note, quote))
    return kept, dropped, druggy


def main():
    global MODEL, EFFORT
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, help="pilot on N reports; print, write nothing")
    ap.add_argument("--ids", nargs="+", help="just these report ids; print, write nothing")
    ap.add_argument("--include", nargs="*", default=[], help="with --limit: ids to add to the pilot")
    ap.add_argument("--cache", help="JSONL file of replies: written as they arrive, reused on a rerun")
    ap.add_argument("--allow-partial", action="store_true", help="full run: write the table even if some reports failed")
    ap.add_argument("--model", default=MODEL)
    ap.add_argument("--effort", default=EFFORT, help="reasoning effort: minimal, low, medium, high")
    a = ap.parse_args()
    MODEL, EFFORT = a.model, a.effort
    full = not a.limit and not a.ids

    c = duckdb.connect("md:sourcing")
    rows = reports(c, a.limit, a.ids)
    if a.limit and a.include:
        rows = reports(c, ids=a.include) + [r for r in rows if r[0] not in a.include][: a.limit - len(a.include)]
    print(f"{len(rows)} reports, model {MODEL} ({EFFORT} effort)")

    done = {}
    if a.cache:
        try:
            with open(a.cache) as f:
                for line in f:
                    j = json.loads(line)
                    done[j["id"]] = j["events"]
        except FileNotFoundError:
            pass
    todo = [r for r in rows if r[0] not in done]
    print(f"{len(rows) - len(todo)} already in the cache, asking for {len(todo)}")
    with ThreadPoolExecutor(THREADS) as pool:
        asked = {r[0]: r for r in pool.map(lambda r: ask(r, a.cache), todo)}
    results = [asked.get(r[0]) or (r[0], r[1], done[r[0]], None) for r in rows]

    out, drops, druggy, errs = [], 0, 0, []
    for rid, body, events, err in results:
        if events is None:
            errs.append((rid, err))
            continue
        kept, dropped, drug = check(rid, body, events)
        out += kept
        drops += dropped
        druggy += drug

    kinds = Counter(r[4] for r in out)
    print(f"prompt() calls {usage['calls']}, ~{usage['chars_in'] // 4:,} tokens in, ~{usage['chars_out'] // 4:,} out")
    print(f"events kept {len(out)}, quote drops {drops}, drug-note drops {druggy}, failed reports {len(errs)}, "
          f"reports with >=1 event {len({r[0] for r in out})}, with time {sum(1 for r in out if r[3] is not None)}")
    print("by kind:", dict(kinds.most_common()))
    for rid, err in errs:
        print("FAILED", rid, err)

    if not full:
        for r in out:
            print(f"{r[0]:15} {r[2] or '   ?':>7} {r[4]:10} {r[5]:45} | {r[6]}")
        return
    if errs and not a.allow_partial:
        print(f"not writing the table: {len(errs)} reports failed. Rerun with the same --cache to finish them"
              + (" once MotherDuck's AI session cap resets or is raised" if capped.is_set() else "")
              + ", or pass --allow-partial.")
        return

    c.execute("""CREATE OR REPLACE TABLE llm_context_events (
        report_id VARCHAR, ev_index INTEGER, time_relative VARCHAR, mins INTEGER, kind VARCHAR, note VARCHAR,
        quote VARCHAR, model VARCHAR, extracted_at TIMESTAMP)""")
    c.execute("""COMMENT ON TABLE llm_context_events IS 'Set and setting in the closely-read Erowid reports: non-drug
        things the writer did or that happened around them (sleep, food, water, exercise, meditation, setting, social,
        activity, mindset, other), read by an AI model via prompt(). time_relative/mins are relative to the first dose
        (negative = before); quote is verbatim from experience_reports.body and was checked to be there. Built by
        extraction/context_events.py.'""")
    now = datetime.now(timezone.utc).replace(tzinfo=None)
    c.executemany("INSERT INTO llm_context_events VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)", [(*r, MODEL, now) for r in out])
    print(f"llm_context_events: {len(out)} rows")


if __name__ == "__main__":
    main()
