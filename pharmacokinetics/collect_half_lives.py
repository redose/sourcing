"""Collect elimination half-lives for re:dose's substances from Wikipedia's drug infoboxes, into a CSV for review.

Source
  English Wikipedia, via the MediaWiki API (https://en.wikipedia.org/w/api.php). Each substance in
  site/data/substances.parquet (PsychonautWiki's list) is matched to a Wikipedia article, and the
  `elimination_half-life` field of the article's {{Infobox drug}} is read, cleaned to plain text and parsed into
  hours. The `metabolites` field is read too, and kept in the notes when it names an active metabolite.

Matching, in order (the CSV's notes say which one matched):
  1. a hand-checked override (OVERRIDES below: titles the name alone gets wrong, and plants or preparations whose
     half-life is that of one main active compound),
  2. the substance's name, then its PsychonautWiki common names, then its systematic name,
  3. Wikipedia's opensearch on the name, accepted only when the found title is the same name written differently.
  A match must be a real article with an {{Infobox drug}}; redirects to a section of another article (for example a
  research chemical redirecting to "Lysergic acid diethylamide#Analogues") are refused, so one drug's value is never
  given to another. Names that are drug classes (CLASSES below) are skipped and marked `class`.

Parsing to hours
  hours_low / hours_high are in hours. Minutes, hours, days and weeks are converted; ranges ("3-5 h", "3 to 5
  hours"), "~", "<" and "up to" are handled. Where several values are given, the parent drug's main elimination
  value is used: the terminal (beta, elimination) phase over the distribution (alpha) phase, and plain or oral values
  over route-, population- or metabolite-specific ones; everything else goes in the notes. parse_confidence is
  high when one unambiguous value or range is given, medium when a choice had to be made between several, and low
  (numbers left empty) when the text could not be read with confidence. Values are never invented.

Licence
  Wikipedia's text is CC BY-SA 4.0: half_life_text and notes quote it, so each row cites its article in source_url
  (a permanent link to the exact revision read). The numbers themselves are facts.

Nothing is loaded into any database: this writes half_lives.csv next to this script for the owner to review.

Run:
    python collect_half_lives.py                     # fetch everything and write half_lives.csv
    python collect_half_lives.py --cache DIR         # keep raw API responses in DIR (re-runs read from it)
    python collect_half_lives.py MDMA Caffeine       # just these substances, printed, nothing written
"""

import argparse
import csv
import hashlib
import html
import json
import re
import time
import unicodedata
from datetime import datetime, timezone
from pathlib import Path

import duckdb
import requests

HERE = Path(__file__).resolve().parent
SUBSTANCES = HERE.parent / "site" / "data" / "substances.parquet"
OUT = HERE / "half_lives.csv"
API = "https://en.wikipedia.org/w/api.php"
USER_AGENT = "re:dose half-life collector (https://redose.org; aaron@ajar.org) python-requests"
DELAY = 0.5  # seconds between API requests
COLUMNS = ["substance", "kind", "wikipedia_title", "half_life_text", "hours_low", "hours_high",
           "parse_confidence", "notes", "source_url", "retrieved_at"]

# Names on PsychonautWiki's list that are classes or families of drugs, not single drugs.
CLASSES = {
    "25x-NBOH", "25x-NBOMe", "2C-T-x", "2C-x", "Amphetamine (disambiguation)", "Anabolic steroids", "Antidepressants",
    "Antihistamine", "Antipsychotic", "Arylcyclohexylamines", "Barbiturates", "Benzodiazepines", "Beta-Carboline",
    "Cannabinoid", "Cathinone", "Classical psychedelics", "Deliriant", "Depressant", "Diarylethylamines",
    "Dissociatives", "DOx", "Entactogen", "Entheogen", "Eugeroics", "Hallucinogens", "Harmala alkaloid", "Hypnotic",
    "Inhalants", "Lysergamides", "MAOI", "N-(2C)-fentanyl", "Nootropic", "Opioids", "Poppers", "Psychedelic", "RIMA",
    "Racetams", "Sedative", "Selective serotonin reuptake inhibitor", "Serotonergic psychedelic",
    "Serotonin-norepinephrine reuptake inhibitor", "Stimulants", "Synthetic cannabinoid", "Thienodiazepines",
    "Xanthines",
}
CLASS_PREFIXES = ("Substituted ",)

# Hand-checked matches: substance -> (Wikipedia title, note). Used before any automatic matching.
OVERRIDES = {
    "Alcohol": ("Alcohol (drug)", "'Alcohol' on Wikipedia is the chemical class; matched to Alcohol (drug)."),
    "Nitrous": ("Nitrous oxide", ""),
    "Cannabis": ("Tetrahydrocannabinol", "Plant: half-life of its main active compound, THC."),
    "Tetrahydrocannabinol": ("Tetrahydrocannabinol", ""),
    "Psilocybin mushrooms": ("Psilocybin", "Mushrooms: half-life of their active compound (psilocybin, via psilocin)."),
    "Psilocybe cubensis": ("Psilocybin", "Mushroom: half-life of its active compound (psilocybin, via psilocin)."),
    "Salvia divinorum": ("Salvinorin A", "Plant: half-life of its main active compound, salvinorin A."),
    "Kratom": ("Mitragynine", "Plant: half-life of its main active alkaloid, mitragynine."),
    "Coca": ("Cocaine", "Plant: half-life of its main active alkaloid, cocaine."),
    "Cocoa": ("Theobromine", "Cocoa: half-life of its main stimulant, theobromine."),
    "Amanita muscaria": ("Muscimol", "Mushroom: half-life of its main active compound, muscimol."),
    "Tabernanthe iboga (botany)": ("Ibogaine", "Plant: half-life of its main active alkaloid, ibogaine."),
    "Morning glory": ("Ergine", "Seeds: half-life of their main active alkaloid, ergine (LSA)."),
    "1,3-Butanediol": ("1,3-Butanediol", "PsychonautWiki's common names for this entry are DMT's; matched by name only."),
    "2-FEA": (None, "Wikipedia's '2-FEA' is 2-furylethylamine, a different compound; no article on "
                    "2-fluoroethamphetamine."),
    "DOC": ("2,5-Dimethoxy-4-chloroamphetamine", ""),
    "Changa": (None, "A smokable blend (DMT with an MAOI-containing herb), not a single drug."),
    "Cake": (None, "PsychonautWiki page is an April Fools satire, not a substance."),
}

session = requests.Session()
session.headers["User-Agent"] = USER_AGENT
cache_dir = None
_last = [0.0]


def api(**params):
    """GET the MediaWiki API (JSON, formatversion 2), politely, through the optional cache."""
    params = {"action": "query", "format": "json", "formatversion": "2", "maxlag": "5", **params}
    key = hashlib.sha1(json.dumps(params, sort_keys=True).encode()).hexdigest()
    if cache_dir and (cache_dir / f"{key}.json").exists():
        return json.loads((cache_dir / f"{key}.json").read_text())
    for attempt in range(5):
        wait = DELAY - (time.time() - _last[0])
        if wait > 0:
            time.sleep(wait)
        _last[0] = time.time()
        r = session.get(API, params=params, timeout=60)
        if r.status_code == 200 and "error" not in r.json():
            data = r.json()
            break
        time.sleep(5 * (attempt + 1))  # maxlag or transient error: back off
    else:
        r.raise_for_status()
        raise RuntimeError(f"API error: {r.text[:300]}")
    if cache_dir:
        (cache_dir / f"{key}.json").write_text(json.dumps(data))
    return data


def norm(s):
    """Loose key for comparing names: lowercase, Greek letters spelled out, no punctuation or spaces."""
    s = unicodedata.normalize("NFKC", s or "").lower()
    for g, latin in (("α", "alpha"), ("β", "beta"), ("δ", "delta"), ("γ", "gamma")):
        s = s.replace(g, latin)
    return re.sub(r"[^a-z0-9]", "", s)


def clean_name(name):
    """PsychonautWiki title -> plausible Wikipedia title ('ΑMT' -> 'αMT', drop '(botany)' and the like)."""
    name = re.sub(r"\s*\((botany|compound|disambiguation)\)$", "", name)
    if name[:1] in "ΑΒΔ":  # PsychonautWiki capitalises Greek letters at the start of titles
        name = name[0].lower() + name[1:]
    return name.replace("Δ-", "Delta-")


# --- Wikipedia lookups -----------------------------------------------------------------------------------------

INFOBOX = re.compile(r"\{\{\s*(?:Infobox[ _]drug|Drugbox|Drug[ _]box)\s*[|\n}]", re.I)
CHEMBOX = re.compile(r"\{\{\s*Chembox\s*[|\n}]", re.I)  # chemical infobox; its Pharmacology section may have HalfLife
CHEMBOX_PHARM = re.compile(r"\{\{\s*Chembox[ _]Pharmacology\s*[|\n}]", re.I)


def lookup(titles):
    """Resolve titles (following redirects). Returns {asked title: page dict or None}.

    A page dict has title, revid, wikitext and redirect_fragment (set when a redirect points into a section)."""
    out = {}
    titles = list(dict.fromkeys(t for t in titles if t and "|" not in t and len(t) < 250))
    for i in range(0, len(titles), 50):
        batch = titles[i:i + 50]
        d = api(titles="|".join(batch), redirects="1", prop="revisions|pageprops", rvprop="content|ids",
                rvslots="main", ppprop="disambiguation")["query"]
        norm_map = {n["from"]: n["to"] for n in d.get("normalized", [])}
        redir = {r["from"]: (r["to"], r.get("tofragment")) for r in d.get("redirects", [])}
        pages = {p["title"]: p for p in d.get("pages", [])}
        for t in batch:
            cur, frag = norm_map.get(t, t), None
            for _ in range(3):
                if cur in redir:
                    cur, f = redir[cur]
                    frag = frag or f
            p = pages.get(cur)
            if not p or p.get("missing") or p.get("invalid") or "revisions" not in p:
                out[t] = None
                continue
            out[t] = {
                "title": p["title"],
                "revid": p["revisions"][0]["revid"],
                "wikitext": p["revisions"][0]["slots"]["main"]["content"],
                "disambiguation": "disambiguation" in p.get("pageprops", {}),
                "redirect_fragment": frag,
            }
    return out


def opensearch(name):
    d = api(action="opensearch", search=name, limit="5", namespace="0", redirects="resolve")
    return d[1] if isinstance(d, list) and len(d) > 1 else []


def usable(page, drug_only=False):
    """Why a looked-up page can't be used, or None if it can (it must have a drug infobox, or a chembox unless
    drug_only)."""
    if page is None:
        return "no article"
    if page["disambiguation"]:
        return "disambiguation page"
    if page["redirect_fragment"]:
        return f"redirects to a section ({page['title']}#{page['redirect_fragment']})"
    if INFOBOX.search(page["wikitext"]) or (not drug_only and CHEMBOX.search(page["wikitext"])):
        return None
    return f"no drug infobox ({page['title']})"


# --- Infobox parsing --------------------------------------------------------------------------------------------

def template_body(text, start):
    """Given text and the index of a '{{', return the template's full source (balanced braces)."""
    depth, i = 0, start
    while i < len(text):
        if text.startswith("{{", i):
            depth, i = depth + 1, i + 2
        elif text.startswith("}}", i):
            depth, i = depth - 1, i + 2
            if depth == 0:
                return text[start:i]
        else:
            i += 1
    return text[start:]


def split_top(s, sep="|"):
    """Split on sep only outside nested {{ }} and [[ ]]."""
    parts, depth, cur, i = [], 0, [], 0
    while i < len(s):
        two = s[i:i + 2]
        if two in ("{{", "[["):
            depth, i = depth + 1, i + 2
            cur.append(two)
            continue
        if two in ("}}", "]]"):
            depth, i = max(depth - 1, 0), i + 2
            cur.append(two)
            continue
        if s[i] == sep and depth == 0:
            parts.append("".join(cur))
            cur = []
        else:
            cur.append(s[i])
        i += 1
    parts.append("".join(cur))
    return parts


def template_fields(wikitext, pattern):
    """{field name: raw value} of the first template matching pattern, or None if there is none."""
    m = pattern.search(wikitext)
    if not m:
        return None
    src = re.sub(r"<!--.*?-->", "", template_body(wikitext, m.start()), flags=re.S)
    fields = {}
    for part in split_top(src[2:-2])[1:]:
        if "=" in part:
            k, v = part.split("=", 1)
            fields[k.strip().lower().replace(" ", "_")] = v.strip()
    return fields


def infobox_fields(wikitext):
    """(box, {field: raw value}) from the article's {{Infobox drug}}, else from its {{Chembox Pharmacology}}
    (whose HalfLife field is mapped to elimination_half-life)."""
    fields = template_fields(wikitext, INFOBOX)
    if fields is not None:
        return "Infobox drug", fields
    fields = template_fields(wikitext, CHEMBOX_PHARM)
    if fields is not None:
        hl = fields.get("halflife") or fields.get("eliminationhalflife") or fields.get("elimination_half-life")
        return "Chembox", {"elimination_half-life": hl or "", "metabolites": ""}
    return ("Chembox" if CHEMBOX.search(wikitext) else None), {}


# Templates whose content is kept (first positional argument, or all of them joined).
KEEP_FIRST = {"nowrap", "nobr", "small", "smaller", "big", "abbr", "tooltip", "abbrlink", "em", "strong", "i", "b",
              "lang", "sic", "nowrap begin", "longitem", "keep together", "no wrap", "proper name", "chem name",
              "chem", "ill", "wikt", "sub", "sup", "linktext", "allcaps", "smallcaps", "math", "mvar", "tmath"}
KEEP_ALL = {"ubl", "unbulleted list", "plainlist", "flatlist", "hlist", "bulleted list", "ublist", "unbulleted"}
LITERALS = {"ndash": "–", "snd": " – ", "spaced ndash": " – ", "mdash": "—", "nbsp": " ", "±": "±", "pm": "±",
            "tilde": "~", "=": "=", "!": "|", "thinsp": " ", "·": "·", "dot": "·", "minus": "−", "x": "×",
            "times": "×", "approx": "~", "sim": "~", "lt": "<", "gt": ">", "le": "≤", "ge": "≥", "frac": None}
DROP = {"citation needed", "cn", "fact", "clarify", "efn", "sfn", "sfnp", "harvnb", "r", "rp", "dubious",
        "when", "which", "vague", "according to whom", "by whom", "update needed", "better source needed",
        "medcn", "refn", "verify source", "page needed", "self-published inline", "failed verification",
        "unreliable medical source", "disputed inline", "specify", "who", "citation", "cite journal", "cite web",
        "cite book", "anchor", "dead link", "full citation needed", "primary source inline", "tl", "nbsp-word"}


def expand_template(inner):
    parts = split_top(inner)
    name = parts[0].strip().lower().replace("_", " ")
    args = [p for p in parts[1:] if not re.match(r"^\s*[\w -]+\s*=", p)]  # positional only
    if name in DROP or name.startswith("cite ") or name.startswith("efn"):
        return ""
    if name in LITERALS and LITERALS[name] is not None:
        return LITERALS[name]
    if name == "frac" and args:
        return "/".join(a.strip() for a in args) if len(args) > 1 else f"1/{args[0].strip()}"
    if name in ("val", "cvt", "convert"):
        named = dict(p.split("=", 1) for p in parts[1:] if re.match(r"^\s*[\w -]+\s*=", p))
        named = {k.strip(): v.strip() for k, v in named.items()}
        nums = [a.strip() for a in args if a.strip()]
        if name != "val":  # {{convert|20|-|100|h|...}}: the unit is the last positional before the target unit
            unit = next((a for a in nums[1:] if not re.match(r"^[\d.]+$|^[-–]$|^to$", a)), "")
            nums = nums[:nums.index(unit)] if unit else nums
            named.setdefault("u", unit)
        nums = " ".join(nums).replace(" - ", "–").replace(" to ", "–")
        return nums + (" " + (named.get("u") or named.get("ul") or "") if named.get("u") or named.get("ul") else "")
    if name in KEEP_ALL:
        return "; ".join(a.strip().lstrip("*").strip() for a in args if a.strip())
    if name in KEEP_FIRST or name.startswith("nowrap"):
        return args[0] if args else ""
    if name.startswith("blist") or name.startswith("plainlist"):
        return "; ".join(args)
    return "; ".join(args) if args else ""  # unknown: keep its text, flagged by the parser if it doesn't read


def clean(raw):
    """Wikitext value -> plain text."""
    s = raw or ""
    s = re.sub(r"<!--.*?-->", "", s, flags=re.S)
    s = re.sub(r"<ref[^>]*/\s*>", "", s, flags=re.I)
    s = re.sub(r"<ref[^>]*>.*?</ref\s*>", "", s, flags=re.I | re.S)
    s = re.sub(r"<br\s*/?>", "; ", s, flags=re.I)
    for _ in range(10):  # innermost templates first
        new = re.sub(r"\{\{([^{}]*)\}\}", lambda m: expand_template(m.group(1)), s)
        if new == s:
            break
        s = new
    s = re.sub(r"\[\[(?:[^|\]]*\|)?([^\]]*)\]\]", r"\1", s)        # [[link|text]] -> text
    s = re.sub(r"\[https?://\S+\s+([^\]]*)\]", r"\1", s)           # [url text] -> text
    s = re.sub(r"\[https?://\S+\]", "", s)
    s = re.sub(r"'{2,}", "", s)                                     # bold / italic
    s = re.sub(r"</?(sub|sup|small|span|div|abbr|i|b|em|strong|nowiki|p|li|ul)[^>]*>", "", s, flags=re.I)
    s = re.sub(r"<[^>]+>", "", s)
    s = html.unescape(s).replace(" ", " ").replace(" ", " ").replace(" ", " ")
    s = re.sub(r"^\s*[*#:]\s*", "", s, flags=re.M)                  # list bullets
    s = re.sub(r"\s*\n\s*", "; ", s)
    s = re.sub(r"\s+", " ", s)
    s = re.sub(r"(;\s*)+", "; ", s)
    s = re.sub(r"\s+([,;.)])", r"\1", s).replace("( ", "(")
    return s.strip(" ;,")


# --- Matching substances to articles ----------------------------------------------------------------------------

def load_substances(only=None):
    rows = duckdb.sql(f"select name, common_names, systematic_name from '{SUBSTANCES}' order by name").fetchall()
    subs = [{"name": n, "common_names": cn or [], "systematic_name": sn} for n, cn, sn in rows]
    if only:
        wanted = {norm(o) for o in only}
        subs = [s for s in subs if norm(s["name"]) in wanted]
    return subs


def is_class(name):
    return name in CLASSES or name.startswith(CLASS_PREFIXES)


def resolve(subs):
    """Fill s['page'], s['matched_via'] and s['tried'] for each drug substance."""
    pending = [s for s in subs if s["kind"] == "drug"]
    for s in pending:
        s["tried"] = []
        s["page"] = None

    def attempt(stage, candidates_of):
        todo = [s for s in pending if s["page"] is None and not s.get("stop")]
        cands = {s["name"]: [c for c in candidates_of(s) if c] for s in todo}
        found = lookup([c for cs in cands.values() for c in cs])
        for s in todo:
            for c in cands[s["name"]]:
                page = found.get(c)
                why = usable(page)
                if why is None:
                    s["page"], s["matched_via"] = page, f"{stage} '{c}'"
                    break
                s["tried"].append(f"{c}: {why}")

    # 1. overrides
    for s in pending:
        if s["name"] in OVERRIDES and OVERRIDES[s["name"]][0] is None:
            s["stop"] = True
    attempt("override", lambda s: [OVERRIDES[s["name"]][0]] if s["name"] in OVERRIDES else [])
    for s in pending:  # an override that fails is reported, not silently replaced by a guess
        if s["name"] in OVERRIDES:
            s["stop"] = True
    # 2. name, common names, systematic name
    attempt("name", lambda s: list(dict.fromkeys([s["name"], clean_name(s["name"])])))
    attempt("common name", lambda s: [c for c in s["common_names"] if len(norm(c)) >= 3])
    attempt("systematic name", lambda s: [s["systematic_name"]])
    # 3. the name is a disambiguation page: accept its one linked article that has a drug infobox, if exactly one
    todo = [s for s in pending if s["page"] is None and not s.get("stop")]
    dis = {s["name"]: p for s in todo for p in [lookup([clean_name(s["name"])]).get(clean_name(s["name"]))]
           if p and p["disambiguation"]}
    links = {n: list(dict.fromkeys(re.findall(r"\[\[([^|\]#]+)", p["wikitext"])))[:500] for n, p in dis.items()}
    found = lookup([l for ls in links.values() for l in ls])
    for s in todo:
        if s["name"] not in dis:
            continue
        drugs = {found[l]["title"]: found[l] for l in links[s["name"]] if usable(found.get(l), drug_only=True) is None}
        if len(drugs) == 1:
            (title, page), = drugs.items()
            s["page"], s["matched_via"] = page, f"the only drug on disambiguation page '{dis[s['name']]['title']}'"
        else:
            s["tried"].append(f"disambiguation page lists {len(drugs)} drugs: {', '.join(list(drugs)[:4])}")
    # 4. opensearch, accepted only for the same name written differently
    for s in pending:
        if s["page"] is not None or s.get("stop"):
            continue
        keys = {norm(s["name"]), norm(clean_name(s["name"]))} | {norm(c) for c in s["common_names"] if len(norm(c)) >= 3}
        hits = opensearch(clean_name(s["name"]))
        same = [h for h in hits if norm(h) in keys or norm(re.sub(r"\s*\(.*\)$", "", h)) in keys]
        if not same:
            if hits:
                s["tried"].append(f"opensearch: only {', '.join(hits[:3])}")
            continue
        page = lookup(same[:1]).get(same[0])
        why = usable(page)
        if why is None:
            s["page"], s["matched_via"] = page, f"opensearch '{same[0]}'"
        else:
            s["tried"].append(f"opensearch {same[0]}: {why}")


# --- Text to hours ----------------------------------------------------------------------------------------------

UNITS = {"sec": 1 / 3600, "second": 1 / 3600, "min": 1 / 60, "minute": 1 / 60, "h": 1, "hr": 1, "hour": 1,
         "d": 24, "day": 24, "week": 168, "wk": 168}
NUM = r"\d+(?:\.\d+)?"
QUAL = r"(?:~|≈|<|>|≤|≥|about\s|approx\.?\s|approximately\s|up to\s|over\s|less than\s|more than\s|under\s)?\s*"
UNIT = r"(?P<unit>seconds?|secs?|minutes?|mins?|hours?|hrs?|hr|h|days?|d|weeks?|wks?)\b(?!\s*[/-])"
MENTION = re.compile(
    rf"(?P<q1>{QUAL})(?P<a>{NUM})(?:\s*±\s*(?P<sd>{NUM}))?"
    rf"(?:\s*(?:{UNIT.replace('unit', 'unit1')})?\s*(?:–|to)\s*(?P<q2>{QUAL})(?P<b>{NUM}))?"
    rf"\s*(?P<paren>\([^()]*\))?\s*{UNIT}", re.I)
BARE = re.compile(rf"(?P<q1>{QUAL})(?P<a>{NUM})(?:\s*–\s*(?P<b>{NUM}))?(?=\s*[;,)])")  # unit-less, e.g. "(10–40; 20–60 hours)"

PHASE_EARLY = re.compile(r"\bα\b|alpha|initial|distribution|rapid phase", re.I)
PHASE_LATE = re.compile(r"\bβ\b|\bγ\b|\bɣ\b|beta|gamma|terminal|elimination phase|slow phase", re.I)
FAVOURED = re.compile(r"\boral|by mouth|\badults?\b|in humans|\bIR\b|immediate|single.dose|\balone\b|exogenous|"
                      r"extensive metaboli|healthy|parent compound", re.I)
DISFAVOURED = re.compile(
    r"\brats?\b|\bmice\b|\bmouse\b|\bpigs?\b|\bdogs?\b|\bmonkeys?\b|animal|infant|neonat|prematur|child|elderly|"
    r"\b65\+|older|poor metaboli|kidney|renal|liver|hepatic|cirrhosis|failure|disease|repeated|multi.?dose|"
    r"multiple dos|chronic|with maoi|overdose|high concentration|\bER\b|\bCR\b|\bXR\b|extended|"
    r"controlled.release|sustained|\bIM\b|\bSC\b|s\.c\.|\bIV\b|intravenous|transdermal|intranasal|buccal|"
    r"sublingual|inhal|depot|metabolite|endogenous|enterohepatic|\bas [\w-]+\)|range|\bavg|\bmean\b", re.I)
ENANTIOMER = re.compile(r"^\s*(?:\([RS]\)|[RS]-|[DL]-|dextro|levo)", re.I)
EXPLAINED = re.compile(r"range|avg|average|mean|±", re.I)  # parentheticals that only describe the value itself
NOT_FIXED = re.compile(r"zero.order|constant.rate", re.I)


def to_hours(x, unit):
    return round(float(x) * UNITS[unit.lower().rstrip("s")], 4)


def split_depth0(t):
    """Split at ';' and at ', ' before a new value, outside parentheses and brackets."""
    out, depth, cur = [], 0, ""
    for i, ch in enumerate(t):
        depth += ch in "([" 
        depth -= ch in ")]"
        if depth <= 0 and (ch == ";" or (ch == "," and re.match(r",\s*[~<>≈]?\s*\d", t[i:]))):
            out.append(cur.strip())
            cur = ""
            depth = 0
        else:
            cur += ch
    out.append(cur.strip())
    return [s for s in out if s]


def depth_at(seg, pos):
    return seg[:pos].count("(") - seg[:pos].count(")") + seg[:pos].count("[") - seg[:pos].count("]")


def mentions(seg):
    """Values with units in a segment: dicts with low, high (hours), span, depth, kind ('<', '>', or '')."""
    out = []
    for m in MENTION.finditer(seg):
        unit = m.group("unit")
        a = to_hours(m.group("a"), m.group("unit1") or unit)
        b = to_hours(m.group("b"), unit) if m.group("b") else None
        q = (m.group("q2") if b is not None else m.group("q1")) or ""
        q1 = (m.group("q1") or "").strip().lower()
        lo, hi = (a, b) if b is not None else (a, a)
        if q1 in ("<", "≤", "up to", "less than", "under"):
            lo = None
        elif q1 in (">", "≥", "over", "more than"):
            hi = None
        out.append({"low": lo, "high": hi, "span": m.span(), "depth": depth_at(seg, m.start()), "text": m.group(0)})
        paren = m.group("paren")  # "8.7 (range 4.6–16) hours": numbers inside share the unit
        if paren:
            for n in BARE.finditer(paren + ";"):
                start = m.start("paren") + n.start()
                out.append({"low": to_hours(n.group("a"), unit),
                            "high": to_hours(n.group("b") or n.group("a"), unit),
                            "span": (start, m.start("paren") + n.end()), "depth": 1, "text": n.group(0)})
    # unit-less values that borrow the unit of the next value, e.g. "Biphasic (10–40; 20–60 hours)"
    taken = [x["span"] for x in out]
    for n in BARE.finditer(seg):
        if any(s <= n.start() < e for s, e in taken):
            continue
        nxt = next((x for x in out if x["span"][0] >= n.end() and x["span"][0] - n.end() < 6), None)
        if nxt:
            unit = MENTION.match(nxt["text"]).group("unit")
            out.append({"low": to_hours(n.group("a"), unit), "high": to_hours(n.group("b") or n.group("a"), unit),
                        "span": n.span(), "depth": depth_at(seg, n.start()), "text": n.group(0)})
    return sorted(out, key=lambda x: x["span"][0])


def fmt(lo, hi):
    f = lambda x: "" if x is None else (f"{x:.4g}")
    return f"{f(lo)}–{f(hi)} h" if lo != hi else f"{f(lo)} h"


def parse_half_life(text, raw, parent_names):
    """Cleaned half-life text -> (hours_low, hours_high, confidence, notes)."""
    if not text:
        return None, None, "", []
    if NOT_FIXED.search(text) or NOT_FIXED.search(raw or ""):
        return None, None, "low", ["No fixed half-life: elimination is zero-order (constant-rate) at usual "
                                   "concentrations, so a half-life does not describe it."]
    t = re.sub(r"\b[tT]\s?(?:1/2|½)", "t½", text)
    t = t.replace("—", "–").replace("−", "–")
    t = re.sub(r"(\d)\s*-\s*(\d)", r"\1–\2", t)
    t = re.sub(r"(\d)\s*–\s*(\d)", r"\1–\2", t)
    if not re.search(r"\d", t):
        return None, None, "low", [f"Not a number: '{text}'."]
    parents = {norm(n) for n in parent_names if n}

    segs, carry = [], None
    for seg in split_depth0(t):
        seg = seg.strip(" ,")
        label, body = None, seg
        lm = re.match(r"^([^:]{1,60}):\s*(.*)$", seg)
        if lm and not MENTION.search(lm.group(1)):
            label, body = lm.group(1).strip(), lm.group(2).strip()
            carry = None if body else label  # a label on its own ("IR dosing:") applies to the next values
            if not body:
                continue
        elif carry:
            label, seg = carry, f"{carry}: {seg}"
        ms = mentions(body)
        if not ms:
            continue
        wrapped = body.startswith("(") and depth_at(body, len(body) - 1) == 1 and body.endswith(")")
        main = next((m for m in ms if m["depth"] == 0), None)
        phased = re.search(r"phasic", body, re.I) and main is None
        if phased:  # "Biphasic (10–40; 20–60 hours)": the last phase is the terminal one
            main = ms[-1]
        if main is None and wrapped:
            main = ms[0]
        if main is None:
            continue
        rest = (body[:main["span"][0]] + " " + body[main["span"][1]:]).strip()
        extra = [m for m in ms if m is not main and not EXPLAINED.search(body[max(0, m["span"][0] - 12):m["span"][0]])]
        segs.append({"seg": seg, "label": label, "main": main, "context": f"{label or ''} {rest}".strip(),
                     "extra": extra,
                     "wrapped": wrapped, "phased": bool(phased),
                     "parent": bool(label) and (norm(label) in parents or "parent" in rest.lower())})
    if not segs:
        return None, None, "low", [f"Could not read a value from '{text}'."]

    notes, medium = [], False
    cands = [s for s in segs if not s["wrapped"]] or segs
    late = [s for s in cands if PHASE_LATE.search(s["context"])]
    early = [s for s in cands if PHASE_EARLY.search(s["context"])]
    if [s for s in cands if s["parent"]]:
        chosen = [s for s in cands if s["parent"]][0]
    elif late:
        chosen, medium = late[-1], True
        notes.append("Several phases given; used the terminal one.")
    elif early and len(cands) == len(early):
        return None, None, "low", [f"Only an initial (distribution) phase is given ('{text}'), not the "
                                   "elimination half-life."]
    else:
        pool = [s for s in cands if s not in early]
        clean_pool = [s for s in pool if s["label"] and ENANTIOMER.match(s["label"])
                      and not DISFAVOURED.search(s["context"])]
        if len(clean_pool) >= 2:
            lows = [s["main"]["low"] for s in clean_pool if s["main"]["low"] is not None]
            highs = [s["main"]["high"] for s in clean_pool if s["main"]["high"] is not None]
            notes.append("No value for the racemic drug; the range spans the values given for its enantiomers ("
                         + "; ".join(s["seg"] for s in clean_pool) + ").")
            others = [s["seg"] for s in segs if s not in clean_pool]
            if others:
                notes.append("Also given: " + "; ".join(others) + ".")
            return (min(lows) if lows else None), (max(highs) if highs else None), "medium", notes
        # favoured qualifiers (oral, adults, IR, ...) and plain unqualified values score alike; ties go to the first
        score = lambda s: (len(FAVOURED.findall(s["context"])) or not s["context"]) \
            - 2 * bool(DISFAVOURED.search(s["context"]))
        chosen = max(pool or cands, key=lambda s: (score(s), -cands.index(s)))
    others = [s["seg"] for s in segs if s is not chosen]
    lo, hi = chosen["main"]["low"], chosen["main"]["high"]
    if len(segs) > 1:
        medium = medium or not chosen["parent"]
        notes.insert(0, f"Used '{chosen['seg']}'.")
        notes.append("Also given: " + "; ".join(others) + ".")
    if chosen["extra"]:
        medium = True
        if len(segs) == 1:
            notes.append("The entry gives more than one value; used the first.")
    if DISFAVOURED.search(re.sub(r"range|mean|avg|average", "", chosen["context"], flags=re.I)):
        medium = True
        if len(segs) == 1:
            notes.append(f"The only value given is qualified: '{chosen['seg']}'.")
    if lo is None or hi is None:
        medium = True
        notes.append("Only an upper bound is given." if lo is None else "Only a lower bound is given.")
    if chosen["phased"]:
        medium = True
        notes.append("Multi-phase elimination; used the last (terminal) phase.")
    return lo, hi, ("medium" if medium else "high"), notes


# --- Rows -------------------------------------------------------------------------------------------------------

def build_row(s, retrieved_at):
    row = dict.fromkeys(COLUMNS, "")
    row.update(substance=s["name"], kind=s["kind"], retrieved_at=retrieved_at)
    notes = []
    if s["name"] in OVERRIDES and OVERRIDES[s["name"]][1]:
        notes.append(OVERRIDES[s["name"]][1])
    if s["kind"] == "class":
        row["notes"] = "A class of drugs, not a single drug."
        return row
    page = s.get("page")
    if page is None:
        row["kind"] = "not_found"
        tried = "; ".join(dict.fromkeys(s.get("tried", [])))
        row["notes"] = " ".join(notes + ([f"No Wikipedia drug article found (tried: {tried})."] if tried else []))
        return row
    box, fields = infobox_fields(page["wikitext"])
    raw = fields.get("elimination_half-life", "")
    text = clean(raw)
    lo, hi, conf, parse_notes = parse_half_life(text, raw, [s["name"], clean_name(s["name"]), page["title"]])
    row.update(wikipedia_title=page["title"], half_life_text=text, parse_confidence=conf,
               hours_low="" if lo is None else lo, hours_high="" if hi is None else hi,
               source_url=f"https://en.wikipedia.org/w/index.php?title={requests.utils.quote(page['title'].replace(' ', '_'))}"
                          f"&oldid={page['revid']}")
    if not s["matched_via"].startswith(("name", "override")) or norm(page["title"]) != norm(s["name"]):
        notes.append(f"Matched via {s['matched_via']}.")
    if box == "Chembox":
        notes.append("Read from the article's chembox (it has no drug infobox).")
    if not text:
        notes.append("The article's infobox gives no elimination half-life.")
    notes += parse_notes
    metab = clean(fields.get("metabolites", ""))
    s["metabolites"] = metab
    if re.search(r"\bactive\b", metab, re.I) and not re.search(r"\binactive\b", metab, re.I):
        notes.append(f"Metabolites (infobox): {metab}.")
    row["notes"] = " ".join(n if n.endswith((".", ")")) else n + "." for n in notes)
    return row


def cross_reference_metabolites(subs, rows):
    """Note metabolites (from the infobox) that are themselves in this list with a half-life, e.g. heroin -> morphine."""
    by_key = {}
    for s, r in zip(subs, rows):
        if r["hours_low"] != "" or r["hours_high"] != "":
            for k in (s["name"], r["wikipedia_title"], *s["common_names"]):
                if len(norm(k)) >= 3:
                    by_key.setdefault(norm(k), r)
    for s, r in zip(subs, rows):
        found = []
        for m in re.split(r"[;,•]|\band\b", re.sub(r"\(.*?\)", "", s.get("metabolites") or "")):
            m = re.sub(r"\d+(\.\d+)?\s*%", "", m).strip(" .")
            hit = by_key.get(norm(m))
            if hit and hit is not r and hit["wikipedia_title"] != r["wikipedia_title"] and hit not in [h for _, h in found]:
                found.append((m, hit))
        if found:
            listed = ", ".join(f"{m} ({fmt(h['hours_low'] or None, h['hours_high'] or None)}, see {h['substance']})"
                               for m, h in found)
            r["notes"] = (r["notes"] + " " if r["notes"] else "") + f"Metabolites also in this list: {listed}."


def main():
    global cache_dir
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("only", nargs="*", help="substance names: just these, printed, nothing written")
    ap.add_argument("--cache", type=Path, help="directory to keep raw API responses in")
    args = ap.parse_args()
    if args.cache:
        cache_dir = args.cache
        cache_dir.mkdir(parents=True, exist_ok=True)
    retrieved_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    subs = load_substances(args.only)
    for s in subs:
        s["kind"] = "class" if is_class(s["name"]) else "drug"
    resolve(subs)
    rows = [build_row(s, retrieved_at) for s in subs]
    cross_reference_metabolites(subs, rows)
    if args.only:
        for r in rows:
            print("\n".join(f"  {k}: {v}" for k, v in r.items() if k != "retrieved_at"), end="\n\n")
        return
    with OUT.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=COLUMNS)
        w.writeheader()
        w.writerows(rows)
    kinds = {k: sum(r["kind"] == k for r in rows) for k in ("drug", "class", "not_found")}
    numeric = sum(r["hours_low"] != "" or r["hours_high"] != "" for r in rows)
    high = sum(r["parse_confidence"] == "high" for r in rows)
    print(f"Wrote {len(rows)} rows to {OUT}: {kinds}; {numeric} with a numeric half-life, {high} high confidence.")


if __name__ == "__main__":
    main()
