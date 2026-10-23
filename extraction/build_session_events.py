"""Build the unified, pre-computed session timeline for re:dose, so the website reads finished rows instead of working
them out in the browser. Loads into MotherDuck (sourcing) and exports zstd Parquet copies to site/data/.

Creates (or replaces) these four tables, and only these:
  effect_points        scoring/effect_points.csv as-is: points (-3..+3) per Effect Index effect, safety flag, how the
                       points were seeded.
  substance_half_lives pharmacokinetics/half_lives.csv as-is: elimination half-life per substance from Wikipedia.
  session_events       one row per moment of a closely-read story, in time order:
                         took  a dose (llm_extractions.extraction->doses), with the name resolved against substances,
                               the amount parsed, the nth dose of that substance and a running total;
                         felt  an observation (llm_observations + llm_observation_effects), with its effects scored
                               from effect_points plus the tone;
                         did   a non-drug moment (llm_context_events: sleep, food, setting...), if that table exists.
                       Every row carries `quote`, the writer's verbatim words for the moment (never the AI's paraphrase),
                       and `self_rating` if those words hold a Shulgin-scale mark (kept exactly as written).
  session_risky_pairs  per story, the first dose that brings two substances together within 24 hours when there is a
                       known interaction (the same severity logic as the site's pairSevCTEs: TripSit's chart first,
                       then the interactions table by name, common name and class).

Sources (MotherDuck, sourcing): experience_reports, llm_extractions, llm_observations, llm_observation_effects,
substances, interactions, tripsit_combos, tripsit_group_members, llm_context_events (optional); plus the two CSVs.

Rules worth knowing:
  - mins = minutes from the first dose, parsed from time_relative ("T+1:30" -> 90, "T-0:15" -> -15). Moments whose time
    doesn't parse ("ongoing", "next day", missing) are dropped and counted.
  - Same minute: did, then took, then felt; ties keep the source order (dose order / obs_index / ev_index).
  - quote: port of ownWords() in .dive-preview/src/dive.tsx. The AI note is checked against the story: the whole note if
    it appears verbatim ('verbatim'), else its quoted bits / pieces around an ellipsis that appear ('quoted_part'),
    else the story sentence sharing the most content words ('best_sentence'), else NULL.
  - self_rating: a Shulgin mark (+, ++, +++, ++++, +/-, plus one..four) in the quote, stored as written with
    self_rating_scale = 'shulgin'. Never converted to a number or to our score. Conservative: '++'..'++++' count on their
    own; weaker marks need rating-like wording around them; a minus only as "(-)" / "a minus" next to rating/plateau.

Run with a MotherDuck token in the `motherduck_token` environment variable (re-run any time; it is idempotent and
picks up llm_context_events once it exists):
    python build_session_events.py            # build, load, export Parquet, print checks
    python build_session_events.py --local    # read site/data/*.parquet instead, print checks, write nothing
"""

import argparse
import json
import math
import os
import re
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

import duckdb

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "site" / "data"
EFFECT_CSV = ROOT / "scoring" / "effect_points.csv"
HALF_LIFE_CSV = ROOT / "pharmacokinetics" / "half_lives.csv"
SPOT = ["erowid-106954", "erowid-100106"]
KIND_ORDER = {"did": 0, "took": 1, "felt": 2}
TONE_POINTS = {"positive": 1, "negative": -1}

# ── Time ────────────────────────────────────────────────────────────────────────────────────────────────────────────
REL = re.compile(r"^T\s*[+-]\s*~?(\d+):(\d{2})")


def rel_mins(t):
    """'T+1:30' -> 90, 'T-0:15' -> -15, anything else -> None (same as relMins in dive.tsx)."""
    if not t:
        return None
    m = REL.match(t)
    if not m:
        return None
    sign = -1 if re.match(r"^T\s*-", t) else 1
    return sign * (int(m.group(1)) * 60 + int(m.group(2)))


# ── Verbatim quotes (port of foldText / quoteWords / ownWords in dive.tsx) ──────────────────────────────────────────
QUOTE_STOP = set(
    "the and for with that this was were had have has then from into just about after before them they their there "
    "what when which while very really some more like than also been being would could just took take".split()
)
# JavaScript's \s (Python's also matches \x1c-\x1f and \x85, which the stories contain as mis-decoded cp1252).
WS = "[\t\n\v\f\r \u00a0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000\ufeff]"
SENT_SPLIT = re.compile(rf"(?<=[.!?]){WS}+|\n+")


def js_trim(t):
    return re.sub(rf"^{WS}+|{WS}+$", "", t)


def fold(t):
    t = t.lower()
    t = re.sub(r"[‘’`]", "'", t)
    t = re.sub(r"[“”]", '"', t)
    return js_trim(re.sub(WS + "+", " ", t))


def quote_words(t):
    return [w for w in re.findall(r"[a-z0-9µ]+", fold(t)) if re.search(r"\d", w) or (len(w) >= 3 and w not in QUOTE_STOP)]


def clean(x):
    x = js_trim(x)
    x = re.sub(rf'^(?:["“\']|{WS})+', "", x)
    return re.sub(rf'(?:["”\'.,;]|{WS})+$', "", x)


class Story:
    """One report's body, folded once, with its candidate sentences and their content words."""

    def __init__(self, body):
        self.body = body or ""
        self.folded = fold(self.body)
        self.sents = []
        for s in SENT_SPLIT.split(self.body):
            t = js_trim(s)
            if 12 <= len(t) <= 260:
                self.sents.append((t, set(quote_words(t))))

    def own_words(self, note, hints=()):
        """(quote, method) for an AI note: the writer's verbatim words, or (None, None)."""
        if not self.folded or not note or not js_trim(note):
            return None, None
        c = clean(note)
        if c and fold(c) in self.folded:
            return c, "verbatim"
        bits = [clean(m.group(1)) for m in re.finditer(r'["“]([^"”]{8,})["”]', note)]
        bits += [clean(p) for p in re.split(rf"{WS}*(?:\.\.\.|…){WS}*", note)]
        keep = []
        for b in bits:
            if len(b) >= 12 and b not in keep and fold(b) in self.folded:
                keep.append(b)
        if keep:
            return " … ".join(keep), "quoted_part"
        want = set(quote_words(note))
        for h in hints:
            want.update(quote_words(h or ""))
        best, best_score = None, 0.0
        for t, ws in self.sents:
            hit = sum(1 for w in want if w in ws)
            score = hit / math.sqrt(max(4, len(ws)))
            if hit >= 2 and score > best_score:
                best, best_score = t, score
        return (best, "best_sentence") if best else (None, None)


# ── Shulgin self-ratings ────────────────────────────────────────────────────────────────────────────────────────────
RATING_WORDS = re.compile(r"shulgin|shulgn|\bscale\b|\brating\b|\brated\b|\bplateau\b|\blevels?\b", re.I)
MINUS_WORDS = re.compile(r"shulgin|\brating\b|\brated\b|\bplateau\b", re.I)
LEAD_IN = re.compile(r"(?:\b(?:a|at|to|feel|felt|feeling|was|is|i'm|im|reached|hit|solid|definite|full|strong|"
                     r"barely|about|maybe|borderline|still|now|like)\s*|['\"‘“])$", re.I)
PAREN_GROUP = re.compile(r"\(\s*([+±][+±/\-− ]*?|[-−])\s*\)(?![\w(-])")
TOKEN = re.compile(r"(?<![\w+±/\-−(])([+±](?:[+±/\-−]*[+±\-−])?)(?![\w+±\-−)])")
# "...precluding the experience from reaching a ++" is not a rating of the moment.
NEGATED = re.compile(r"(?:\b(?:preclud\w*|prevent\w*|never|without|instead of|rather than)\b|\b(?:not|n't|never)\s+"
                     r"(?:reach\w*|hit|get(?:ting)? to|make it to))[^.!?]{0,40}$", re.I)
PLUS_WORD = re.compile(r"\bplus[\s-]+(one|two|three|four|[1-4])\b(?![.,]?\d)", re.I)
A_MINUS = re.compile(r"\ba minus\b(?![\s-]*\d)", re.I)


def near(text, start, end, words, width=60):
    return bool(words.search(text[max(0, start - width):end + width]))


def shulgin_marks(quote, shulgin_report):
    """Shulgin-scale marks in the writer's words, in order, exactly as written; [] when none."""
    if not quote:
        return []
    found = []  # (position, mark)
    taken = []

    def ok_weak(s, e):
        return near(quote, s, e, RATING_WORDS) or shulgin_report

    for m in PAREN_GROUP.finditer(quote):
        inner = m.group(1).strip()
        if inner in ("-", "−"):
            if near(quote, m.start(), m.end(), MINUS_WORDS):
                found.append((m.start(), inner))
        elif max(len(r) for r in re.findall(r"\++", inner) or [""]) > 4:
            continue
        elif "++" in inner or ok_weak(m.start(), m.end()):
            found.append((m.start(), inner))
        taken.append((m.start(), m.end()))
    for m in TOKEN.finditer(quote):
        s, e = m.span(1)
        if any(a <= s < b for a, b in taken):
            continue
        tok = m.group(1)
        runs = re.findall(r"\++", tok)
        if not runs or max(len(r) for r in runs) > 4 or tok.count("+") + tok.count("±") > 6:
            continue
        if not runs and "±" not in tok:
            continue
        before, after = quote[:s], quote[e:]
        if "++" in tok:
            if re.match(r"\s*\d", after) and not near(quote, s, e, RATING_WORDS):
                continue
            found.append((s, tok))
            continue
        # Weak marks: +, ±, +/-, +-, ++- style handled above. Not next to a number ("+ 40 mn", "2 + 2", "+/- 10mg").
        if re.match(r"\s*[\d:]", after) or re.search(r"\d\s*$", before):
            continue
        if (LEAD_IN.search(before) or re.match(r"\s*\(?\s*['\"‘“]?plus", after, re.I)) and ok_weak(s, e):
            found.append((s, tok))
    for m in PLUS_WORD.finditer(quote):
        if LEAD_IN.search(quote[:m.start()]) and ok_weak(m.start(), m.end()) or near(quote, m.start(), m.end(), RATING_WORDS, 40):
            found.append((m.start(), m.group(0)))
    for m in A_MINUS.finditer(quote):
        if near(quote, m.start(), m.end(), MINUS_WORDS):
            found.append((m.start(), m.group(0)))
    out = []
    for pos, mark in sorted(found):
        if NEGATED.search(quote[max(0, pos - 80):pos]):
            continue
        if mark not in out:
            out.append(mark)
    return out


def is_shulgin_report(body):
    """A story that clearly uses the scale: names Shulgin, or writes strong marks or bracketed marks more than once."""
    if not body:
        return False
    if re.search(r"shulgin|shulgn", body, re.I):
        return True
    strong = len(re.findall(r"(?<![\w+])\+{2,4}(?![\w+])", body))
    paren = len(re.findall(r"\(\s*[+±][+±/\-− ]*\)", body))
    return strong >= 2 or paren >= 2


# ── Amounts ─────────────────────────────────────────────────────────────────────────────────────────────────────────
NUM = r"(\d+\s+\d+\s*/\s*\d+|\d+\s*/\s*\d+|\d{1,3}(?:,\d{3})+(?![\d.])|\d+(?:[.,]\d+)?|\.\d+)"
UNIT = (r"(milligrams?|mgs?|micrograms?|mcgs?|µg|μg|ugs?|grams?|gr?s?|kg|milli?lit(?:er|re)s?|mls?|cl|"
        r"tabs?|tablets?|blotters?|hits?|pills?|capsules?|caps?|drops?|lines?|bumps?|bowls?|joints?|cigarettes?|"
        r"puffs?|tokes?|beers?|drinks?|shots?|glass(?:es)?|cups?|cans?|pints?|blunts?|spliffs?|seeds?|"
        r"teaspoons?|tsps?|tablespoons?|tbsps?)(?![a-z])")
AMOUNT = re.compile(NUM + r"\+?\s*" + UNIT, re.I)
WORD_NUM = {"half": 0.5, "quarter": 0.25, "a": 1, "an": 1, "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6}
WORD_AMOUNT = re.compile(r"\b(half|a\s+quarter|a|an|one|two|three|four|five|six)(?:\s+(?:a|an|of\s+a|of\s+an))?\s+"
                         r"(?:(?:small|big|large|full|single|whole)\s+)?" + UNIT, re.I)
UNIT_MAP = {
    "milligram": "mg", "mg": "mg", "microgram": "µg", "mcg": "µg", "µg": "µg", "μg": "µg", "ug": "µg",
    "gram": "g", "g": "g", "gr": "g", "kg": "kg", "milliliter": "ml", "millilitre": "ml", "milliliter": "ml", "ml": "ml",
    "cl": "cl", "tab": "tab", "tablet": "pill", "blotter": "tab", "hit": "hit", "pill": "pill", "capsule": "cap",
    "cap": "cap", "drop": "drop", "line": "line", "bump": "bump", "bowl": "bowl", "joint": "joint",
    "cigarette": "cigarette", "puff": "puff", "toke": "puff", "beer": "beer", "drink": "drink", "shot": "shot",
    "glass": "glass", "cup": "cup", "can": "can", "pint": "pint", "blunt": "blunt", "spliff": "joint", "seed": "seed",
    "teaspoon": "tsp", "tsp": "tsp", "tablespoon": "tbsp", "tbsp": "tbsp",
}


def unit_key(u):
    u = u.lower()
    if u in UNIT_MAP:
        return UNIT_MAP[u]
    for cand in (u[:-2] if u.endswith("es") else None, u[:-1] if u.endswith("s") else None):
        if cand and cand in UNIT_MAP:
            return UNIT_MAP[cand]
    if u.startswith("millilit"):
        return "ml"
    return None


def parse_number(raw):
    raw = raw.strip()
    m = re.fullmatch(r"(\d+)\s+(\d+)\s*/\s*(\d+)", raw)
    if m:
        return int(m.group(1)) + int(m.group(2)) / int(m.group(3)) if int(m.group(3)) else None
    m = re.fullmatch(r"(\d+)\s*/\s*(\d+)", raw)
    if m:
        return int(m.group(1)) / int(m.group(2)) if int(m.group(2)) else None
    if re.fullmatch(r"\d{1,3}(?:,\d{3})+", raw):
        return float(raw.replace(",", ""))
    return float(raw.replace(",", "."))


def to_addable(v, u):
    if u == "g":
        return round(v * 1000, 6), "mg"
    if u == "kg":
        return round(v * 1_000_000, 6), "mg"
    if u == "cl":
        return round(v * 10, 6), "ml"
    return round(v, 6), u


def parse_amount(a):
    """The first amount with a unit in a dose note, in a unit we can add up: '1/2 tab', '~600ug' -> 600 µg,
    '7 grams' -> 7000 mg, '1 1/2g' -> 1500 mg, 'half a pill' -> 0.5 pill. Returns (value, unit) or (None, None)."""
    if not a:
        return None, None
    for m in AMOUNT.finditer(a):
        u = unit_key(m.group(2))
        v = parse_number(m.group(1)) if u else None
        if v is not None and v > 0:
            return to_addable(v, u)
    for m in WORD_AMOUNT.finditer(a):  # "half a pill", "a bowl", "two beers"
        u = unit_key(m.group(2))
        if u:
            return to_addable(WORD_NUM[m.group(1).split()[-1].lower()], u)
    return None, None


# ── Build ───────────────────────────────────────────────────────────────────────────────────────────────────────────
def sources(local):
    """A connection whose unqualified table names are the source tables."""
    if local:
        c = duckdb.connect()
        for p in DATA.glob("*.parquet"):
            c.execute(f"CREATE VIEW \"{p.stem}\" AS SELECT * FROM read_parquet('{p}')")
        return c
    return duckdb.connect("md:sourcing")


def has_table(c, name, local):
    if local:
        return bool(c.execute("SELECT 1 FROM duckdb_views() WHERE view_name = ?", [name]).fetchall())
    return bool(c.execute(
        "SELECT 1 FROM duckdb_tables() WHERE database_name = 'sourcing' AND schema_name = 'main' AND table_name = ?",
        [name]).fetchall())


def build_events(c, local):
    points = {e: (int(p), bool(s)) for e, p, s in duckdb.sql(
        f"SELECT effect, points, safety_flag FROM read_csv('{EFFECT_CSV}')").fetchall()}

    names, commons = {}, {}
    for name, cn in c.execute("SELECT name, common_names FROM substances ORDER BY name").fetchall():
        names.setdefault(name.lower(), name)
        for x in cn or []:
            commons.setdefault(x.lower(), name)

    def resolve(raw):
        r = raw.lower()
        return names.get(r) or commons.get(r) or raw

    bodies = dict(c.execute(
        "SELECT id, body FROM experience_reports WHERE id IN (SELECT report_id FROM llm_extractions)").fetchall())
    stories = {rid: Story(b) for rid, b in bodies.items()}
    shulgin_report = {rid: is_shulgin_report(b) for rid, b in bodies.items()}

    events = defaultdict(list)  # report_id -> list of dicts
    dropped = Counter()
    unknown_effects = Counter()

    # took
    doses = c.execute("""
        SELECT report_id, (d->>'$.order')::INT AS ord,
          trim(regexp_replace(d->>'$.substance', '\\s*\\(.*\\)$', '')) AS raw,
          d->>'$.substance_original' AS said_as, d->>'$.amount' AS amount, d->>'$.route' AS route,
          coalesce(d->>'$.context', d->>'$.description') AS note, d->>'$.time_relative' AS t
        FROM llm_extractions, unnest(from_json(extraction->'$.doses', '["json"]')) AS u(d)
        ORDER BY report_id, ord""").fetchall()
    for rid, ord_, raw, said_as, amount, route, note, t in doses:
        m = rel_mins(t)
        if m is None:
            dropped["took"] += 1
            continue
        if not raw:
            dropped["took (no substance)"] += 1
            continue
        events[rid].append({"kind": "took", "mins": m, "src_index": ord_, "substance": resolve(raw), "said_as": said_as,
                            "amount_text": amount, "route": route, "note": note,
                            "_hints": [said_as or "", amount or ""]})

    # felt
    obs = c.execute("""
        SELECT o.report_id, o.obs_index, o.time_relative, o.description, o.sentiment, o.sentiment_detail,
          list(e.effect_name ORDER BY e.effect_name) FILTER (WHERE e.effect_name IS NOT NULL) AS effects,
          list(e.effect_name ORDER BY e.effect_name) FILTER (WHERE e.confidence = 'medium') AS inferred
        FROM llm_observations o
        LEFT JOIN llm_observation_effects e
          ON e.report_id = o.report_id AND e.obs_index = o.obs_index AND e.confidence <> 'low'
        GROUP BY ALL ORDER BY o.report_id, o.obs_index""").fetchall()
    for rid, idx, t, desc, tone, gist, effects, inferred in obs:
        m = rel_mins(t)
        if m is None:
            dropped["felt"] += 1
            continue
        effects = effects or []
        pts = []
        for e in effects:
            if e not in points:
                unknown_effects[e] += 1
            pts.append(points.get(e, (0, False))[0])
        tp = TONE_POINTS.get(tone, 0)
        events[rid].append({"kind": "felt", "mins": m, "src_index": idx, "gist": gist, "tone": tone, "tone_points": tp,
                            "effects": effects, "inferred_effects": inferred or [], "effect_points": pts,
                            "score": sum(pts) + tp,
                            "safety_effects": [e for e in effects if points.get(e, (0, False))[1]],
                            "note": desc, "_hints": []})

    # did
    has_did = has_table(c, "llm_context_events", local)
    if has_did:
        for rid, idx, t, mins, kind, note, quote in c.execute("""
                SELECT report_id, ev_index, time_relative, mins, kind, note, quote
                FROM llm_context_events ORDER BY report_id, ev_index""").fetchall():
            m = int(round(mins)) if mins is not None else rel_mins(t)
            if m is None:
                dropped["did"] += 1
                continue
            events[rid].append({"kind": "did", "mins": m, "src_index": idx, "activity": kind, "note": note,
                                "_quote_src": quote, "_hints": []})

    rows = []
    for rid, evs in events.items():
        story = stories.get(rid) or Story("")
        # Same minute: did, then took, then felt; ties keep the source order.
        evs.sort(key=lambda e: (e["mins"], KIND_ORDER[e["kind"]], e["src_index"] if e["src_index"] is not None else 0))
        run = {}
        for seq, e in enumerate(evs, 1):
            src = e.pop("_quote_src", None) if e["kind"] == "did" else e.get("note")
            q, method = story.own_words(src or "", e.pop("_hints"))
            marks = shulgin_marks(q, shulgin_report.get(rid, False))
            row = {"report_id": rid, "seq": seq, **e, "quote": q, "quote_method": method,
                   "self_rating": ", ".join(marks) if marks else None,
                   "self_rating_scale": "shulgin" if marks else None}
            if e["kind"] == "took":
                v, u = parse_amount(e["amount_text"])
                row["amount_value"], row["amount_unit"] = v, u
                was = run.get(e["substance"])
                if was is None:
                    tot = {"v": v or 0.0, "u": u, "ok": v is not None, "n": 1}
                else:
                    tot = {"v": was["v"] + (v or 0.0), "u": was["u"],
                           "ok": was["ok"] and v is not None and u == was["u"], "n": was["n"] + 1}
                run[e["substance"]] = tot
                row["dose_n"] = tot["n"]
                row["running_total_value"] = round(tot["v"], 6) if tot["ok"] else None
                row["running_total_unit"] = tot["u"] if tot["ok"] else None
            rows.append(row)
    return rows, dropped, unknown_effects, has_did, len(bodies)


COLUMNS = [
    ("report_id", "VARCHAR", "experience_reports.id"),
    ("seq", "INTEGER", "Order of the moment within the report, from 1 (time order; same minute: did, took, felt)"),
    ("mins", "INTEGER", "Minutes from the first dose (negative before it)"),
    ("kind", "VARCHAR", "took (a dose) | felt (an observation) | did (a non-drug moment)"),
    ("src_index", "INTEGER", "Source row: dose order / llm_observations.obs_index / llm_context_events.ev_index"),
    ("substance", "VARCHAR", "took: substance name resolved against substances (name, then common names), else as extracted"),
    ("said_as", "VARCHAR", "took: what the writer called it"),
    ("amount_text", "VARCHAR", "took: the amount as extracted"),
    ("amount_value", "DOUBLE", "took: first amount with a unit, in amount_unit (grams -> mg, ug/mcg -> µg, fractions resolved)"),
    ("amount_unit", "VARCHAR", "took: mg | µg | ml | tab | hit | pill | cap | drop | line | bowl | joint | cigarette | puff | ..."),
    ("route", "VARCHAR", "took: route as extracted"),
    ("dose_n", "INTEGER", "took: this is the nth dose of this substance in the story"),
    ("running_total_value", "DOUBLE", "took: total of this substance so far including this dose; NULL once units mix or an amount is unreadable"),
    ("running_total_unit", "VARCHAR", "took: unit of running_total_value"),
    ("gist", "VARCHAR", "felt: short gist (llm_observations.sentiment_detail)"),
    ("tone", "VARCHAR", "felt: positive | negative | mixed | neutral"),
    ("tone_points", "INTEGER", "felt: +1 positive, -1 negative, else 0"),
    ("effects", "VARCHAR[]", "felt: Effect Index effects (confidence high or medium), by name"),
    ("inferred_effects", "VARCHAR[]", "felt: the subset of effects with medium confidence"),
    ("effect_points", "INTEGER[]", "felt: points per effect from effect_points, aligned with effects (0 when unknown)"),
    ("score", "INTEGER", "felt: sum of effect_points + tone_points"),
    ("safety_effects", "VARCHAR[]", "felt: effects flagged as safety concerns in effect_points"),
    ("activity", "VARCHAR", "did: kind of moment (sleep, food, water, exercise, meditation, setting, social, activity, mindset, other)"),
    ("note", "VARCHAR", "The AI's own note for the moment (took: dose context, felt: description, did: note). Not the writer's words"),
    ("quote", "VARCHAR", "The writer's verbatim words for the moment, found in the story; NULL when none could be matched"),
    ("quote_method", "VARCHAR", "verbatim (whole AI note is in the story) | quoted_part (quoted bits of it are) | best_sentence (story sentence sharing most words) | NULL"),
    ("self_rating", "VARCHAR", "The writer's own Shulgin-scale mark(s) in quote, exactly as written (comma-separated if several). Not converted, not our score"),
    ("self_rating_scale", "VARCHAR", "'shulgin' when self_rating is set"),
]


def pair_sql():
    """session_risky_pairs: a port of pairSevCTEs (dive.tsx) over each story's doses."""
    alias = lambda col: f"""CASE lower({col})
      WHEN 'selective serotonin reuptake inhibitor' THEN ['ssri', 'ssris']
      WHEN 'serotonin-norepinephrine reuptake inhibitor' THEN ['snri', 'snris']
      WHEN 'monoamine oxidase inhibitor' THEN ['maoi', 'maois']
      WHEN 'dextromethorphan' THEN ['dxm']
      WHEN 'methoxetamine' THEN ['mxe']
      WHEN 'diphenhydramine' THEN ['dph']
      WHEN 'nitrous' THEN ['nitrous', 'n2o']
      ELSE [] END"""
    sev = "CASE i.severity WHEN 'dangerous' THEN 3 WHEN 'unsafe' THEN 2 WHEN 'uncertain' THEN 1 END"
    name_hit = lambda side, other: (f"(i.substance_id = px.{side}_id AND (list_contains(px.{other}_names, lower(i.interacts_with)) "
                                    f"OR list_contains(px.{other}_names, rtrim(lower(i.interacts_with), 's'))))")
    cls_hit = lambda side, other: (f"list_contains(px.{other}_cls, lower(i.interacts_with)) "
                                   f"OR list_contains(px.{other}_cls, rtrim(lower(i.interacts_with), 's'))")
    return f"""
CREATE OR REPLACE TABLE session_risky_pairs AS
WITH took AS (
  SELECT report_id, seq, mins, substance FROM session_events WHERE kind = 'took' AND substance IS NOT NULL
),
subs AS (
  SELECT id, name,
    list_distinct(list_transform(coalesce(common_names, []), x -> lower(x)) || [lower(name)]
      || list_filter(list_transform(string_split(name, ' '), x -> lower(x)), x -> len(x) > 4)) AS names,
    list_transform(coalesce(chemical_class, []) || coalesce(psychoactive_class, []), x -> lower(x)) AS classes
  FROM substances
),
pr AS (
  SELECT DISTINCT a.substance AS a, b.substance AS b
  FROM took a JOIN took b ON a.report_id = b.report_id AND a.substance < b.substance
),
px AS (
  SELECT pr.a, pr.b, sa.id AS a_id, sb.id AS b_id,
    coalesce(sa.names, [lower(pr.a)]) || {alias('pr.a')} AS a_names, coalesce(sa.classes, []) AS a_cls,
    coalesce(sb.names, [lower(pr.b)]) || {alias('pr.b')} AS b_names, coalesce(sb.classes, []) AS b_cls
  FROM pr LEFT JOIN subs sa ON sa.name = pr.a LEFT JOIN subs sb ON sb.name = pr.b
),
oldsev AS (
  SELECT px.a, px.b,
    max(CASE
      WHEN NOT ({name_hit('a', 'b')} OR {name_hit('b', 'a')})
        AND rtrim(lower(i.interacts_with), 's') IN ('stimulant', 'depressant', 'psychedelic', 'dissociative', 'deliriant', 'entactogen', 'hallucinogen', 'cannabinoid')
      THEN least({sev}, 1)
      ELSE {sev} END) AS sev,
    string_agg(DISTINCT i.interacts_with, ', ') AS via
  FROM px
  LEFT JOIN interactions i ON
    (i.substance_id = px.a_id AND (list_contains(px.b_names, lower(i.interacts_with)) OR list_contains(px.b_names, rtrim(lower(i.interacts_with), 's'))
       OR {cls_hit('a', 'b')}))
    OR (i.substance_id = px.b_id AND (list_contains(px.a_names, lower(i.interacts_with)) OR list_contains(px.a_names, rtrim(lower(i.interacts_with), 's'))
       OR {cls_hit('b', 'a')}))
  GROUP BY 1, 2
),
tsev AS (
  SELECT pr.a, pr.b, max(c.severity_rank) AS rank,
    arg_max(c.status, c.severity_rank) AS status, arg_max(c.note, c.severity_rank) AS note
  FROM pr
  JOIN tripsit_group_members ma ON ma.substance = pr.a
  JOIN tripsit_group_members mb ON mb.substance = pr.b
  JOIN tripsit_combos c
    ON (c.group_a = ma.group_key AND c.group_b = mb.group_key) OR (c.group_a = mb.group_key AND c.group_b = ma.group_key)
  WHERE ma.group_key <> mb.group_key
  GROUP BY 1, 2
),
pairsev AS (
  SELECT pr.a, pr.b,
    CASE WHEN t.rank IS NOT NULL THEN nullif(t.rank, 0) ELSE o.sev END AS sev,
    CASE WHEN t.rank IS NOT NULL THEN 'TripSit' ELSE o.via END AS via,
    t.status, t.note
  FROM pr LEFT JOIN tsev t USING (a, b) LEFT JOIN oldsev o USING (a, b)
),
meet AS (
  SELECT d.report_id, d.seq, d.substance, e.substance AS with_substance,
    least(d.substance, e.substance) AS a, greatest(d.substance, e.substance) AS b
  FROM took d JOIN took e
    ON e.report_id = d.report_id AND e.seq < d.seq AND e.substance <> d.substance AND d.mins - e.mins <= 24 * 60
  QUALIFY row_number() OVER (PARTITION BY d.report_id, least(d.substance, e.substance), greatest(d.substance, e.substance)
                             ORDER BY d.seq, e.seq DESC) = 1
)
SELECT m.report_id, m.seq, m.substance, m.with_substance,
  p.sev::INTEGER AS severity, p.status, p.note,
  CASE WHEN p.via = 'TripSit' THEN 'tripsit' ELSE 'interactions' END AS source,
  CASE WHEN p.via = 'TripSit' THEN NULL ELSE p.via END AS matched_on
FROM meet m JOIN pairsev p USING (a, b)
WHERE p.sev >= 1
ORDER BY m.report_id, m.seq, m.with_substance"""


def load(c, rows):
    """Write session_events through a local NDJSON file (no pandas needed)."""
    cols = [n for n, _, _ in COLUMNS]
    with tempfile.NamedTemporaryFile("w", suffix=".ndjson", delete=False, encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps({k: r.get(k) for k in cols}, ensure_ascii=False) + "\n")
        path = f.name
    try:
        spec = "{" + ", ".join(f"'{n}': '{t}'" for n, t, _ in COLUMNS) + "}"
        c.execute(f"""CREATE OR REPLACE TABLE session_events AS
            SELECT * FROM read_json('{path}', format = 'newline_delimited', columns = {spec})
            ORDER BY report_id, seq""")
    finally:
        os.unlink(path)


def comment(c, table, text):
    c.execute(f"COMMENT ON TABLE {table} IS '{text.replace(chr(39), chr(39) * 2)}'")


def write_all(c, rows, has_did):
    c.execute(f"CREATE OR REPLACE TABLE effect_points AS SELECT * FROM read_csv('{EFFECT_CSV}')")
    comment(c, "effect_points", "Points per Effect Index effect, -3 (bad) to +3 (good), used to score felt moments. "
            "Loaded as-is from scoring/effect_points.csv (seeded from how often each effect sat in a moment that felt good "
            "vs bad; safety_flag effects are always -3 and flagged). Built by extraction/build_session_events.py.")
    c.execute(f"CREATE OR REPLACE TABLE substance_half_lives AS SELECT * FROM read_csv('{HALF_LIFE_CSV}')")
    comment(c, "substance_half_lives", "Elimination half-life per substance (hours_low..hours_high) read from Wikipedia "
            "infoboxes, with the source text, parse confidence and notes. Loaded as-is from "
            "pharmacokinetics/half_lives.csv by extraction/build_session_events.py.")
    load(c, rows)
    comment(c, "session_events", "One row per moment of a closely-read story, in time order: took (a dose), felt (an "
            "observation, scored with effect_points + tone) and did (a non-drug moment from llm_context_events"
            + ("" if has_did else "; none yet, the table did not exist at build time") + "). mins is minutes from the "
            "first dose; moments without a readable time are left out. quote holds only the writer's verbatim words; "
            "self_rating keeps a Shulgin-scale mark exactly as the writer wrote it. Built by "
            "extraction/build_session_events.py from llm_extractions, llm_observations, llm_observation_effects, "
            "llm_context_events, substances and experience_reports.")
    for n, _, d in COLUMNS:
        c.execute(f"COMMENT ON COLUMN session_events.{n} IS '{d.replace(chr(39), chr(39) * 2)}'")
    c.execute(pair_sql())
    comment(c, "session_risky_pairs", "Per story, the first dose (seq in session_events) that brings two substances "
            "together within 24 hours when they have a known interaction. severity: 3 dangerous, 2 unsafe, 1 caution. "
            "source tripsit = TripSit's combination chart (status and note from it; takes precedence), interactions = the "
            "interactions table matched by name, common name or class (matched_on lists what matched; a broad class word "
            "caps at 1). Same logic as pairSevCTEs on the site. Built by extraction/build_session_events.py.")
    for t in ["effect_points", "substance_half_lives", "session_events", "session_risky_pairs"]:
        c.execute(f"COPY (SELECT * FROM {t}) TO '{DATA / (t + '.parquet')}' (FORMAT parquet, COMPRESSION zstd)")


def checks(c, rows, dropped, unknown_effects, has_did, n_reports, wrote):
    print("\n== rows by kind")
    by_kind = Counter(r["kind"] for r in rows)
    for k in ("took", "felt", "did"):
        print(f"  {k:5} {by_kind.get(k, 0):6}")
    print(f"  total {len(rows):6}" + ("" if has_did else "   (llm_context_events not found: no did rows yet)"))
    print("== dropped (time did not parse):", dict(dropped))
    print("== quote coverage by method")
    for k in ("took", "felt", "did"):
        sub = [r for r in rows if r["kind"] == k]
        if not sub:
            continue
        m = Counter(r["quote_method"] for r in sub)
        print(f"  {k:5} " + "  ".join(f"{x or 'none'}: {100 * m[x] / len(sub):.1f}%" for x in ("verbatim", "quoted_part", "best_sentence", None)))
    m = Counter(r["quote_method"] for r in rows)
    print("  all   " + "  ".join(f"{x or 'none'}: {100 * m[x] / len(rows):.1f}%" for x in ("verbatim", "quoted_part", "best_sentence", None)))
    rated = [r for r in rows if r["self_rating"]]
    print(f"== self_ratings found: {len(rated)} moments in {len({r['report_id'] for r in rated})} reports")
    print("  marks:", dict(Counter(r["self_rating"] for r in rated).most_common(15)))
    step = max(1, len(rated) // 10)
    for r in rated[::step][:10]:
        print(f"  {r['report_id']} #{r['seq']} {r['kind']} [{r['self_rating']}]  {r['quote'][:200]!r}")
    covered = len({r["report_id"] for r in rows})
    print(f"== reports covered: {covered} of {n_reports} closely-read reports")
    amt = [r for r in rows if r["kind"] == "took"]
    print(f"== took rows with a parsed amount: {sum(1 for r in amt if r['amount_value'] is not None)} of {len(amt)};"
          f" units: {dict(Counter(r['amount_unit'] for r in amt).most_common(12))}")
    print("== unresolved effect names (scored 0):", dict(unknown_effects.most_common(10)) or "none")
    if wrote:
        print("== MotherDuck tables")
        for t in ["effect_points", "substance_half_lives", "session_events", "session_risky_pairs"]:
            print(f"  {t:22} {c.execute(f'SELECT count(*) FROM {t}').fetchone()[0]}")
        print("  risky pairs by severity/source:",
              c.execute("SELECT severity, source, count(*) FROM session_risky_pairs GROUP BY ALL ORDER BY ALL").fetchall())
    for rid in SPOT:
        print(f"\n== spot check {rid}")
        for r in [r for r in rows if r["report_id"] == rid]:
            if r["kind"] == "took":
                what = (f"{r['substance']} (said {r['said_as']!r}) {r['amount_text']!r} -> {r['amount_value']} {r['amount_unit']}"
                        f" {r['route']} dose#{r['dose_n']} total={r['running_total_value']} {r['running_total_unit']}")
            elif r["kind"] == "felt":
                what = f"{r['tone']}({r['tone_points']:+d}) score={r['score']} {list(zip(r['effects'], r['effect_points']))} gist={r['gist']!r}"
            else:
                what = f"{r['activity']}: {r['note']!r}"
            rating = f" rating={r['self_rating']}" if r["self_rating"] else ""
            print(f"  {r['seq']:3} {r['mins']:6} {r['kind']:4} {what}{rating}")
            print(f"        quote[{r['quote_method']}]: {(r['quote'] or '')[:180]!r}")
        if wrote:
            for p in c.execute("SELECT * FROM session_risky_pairs WHERE report_id = ?", [rid]).fetchall():
                print("  risky pair:", p)


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--local", action="store_true", help="read site/data/*.parquet, print checks, write nothing")
    args = ap.parse_args()
    c = sources(args.local)
    rows, dropped, unknown_effects, has_did, n_reports = build_events(c, args.local)
    if not args.local:
        write_all(c, rows, has_did)
    checks(c, rows, dropped, unknown_effects, has_did, n_reports, wrote=not args.local)


if __name__ == "__main__":
    main()
