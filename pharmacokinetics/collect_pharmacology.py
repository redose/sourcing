"""Collect cited pharmacology (Tmax, bioavailability, protein binding, metabolism and CYP enzymes, active metabolites,
excretion, onset, duration, mechanism of action) for re:dose's substances, into CSVs for review.

This extends collect_half_lives.py and reuses its substance list, class list, title matching (overrides, plant ->
main-compound mapping such as cannabis -> THC and kratom -> mitragynine, refusal of section redirects), Wikipedia
API client and cache, and wikitext cleaner. Half-lives stay in half_lives.csv and are not repeated here.

Sources (only these three; every value is cited)
  1. English Wikipedia {{Infobox drug}} (CC BY-SA 4.0), via the MediaWiki API: the fields bioavailability,
     protein_bound, metabolism, metabolites, onset, duration_of_action, excretion and routes_of_administration
     (articles with only a {{Chembox}} give its Pharmacology fields). Each row cites the exact revision read
     (https://en.wikipedia.org/w/index.php?title=...&oldid=...). Text columns quote Wikipedia, so they carry its
     licence: reuse needs attribution and share-alike.
  2. openFDA drug label API (public domain; https://api.fda.gov/drug/label.json), for FDA-approved drugs: the
     label's pharmacokinetics section (or clinical_pharmacology when there is none) is read for Tmax, half-life,
     bioavailability, protein binding, metabolising CYP enzymes and active metabolites. One label is chosen per drug:
     a prescription label whose generic name is the drug alone (salts allowed, combinations refused), preferring the
     original NDA, immediate-release and oral, then the newest. Each value cites the label by set_id and version
     (https://api.fda.gov/drug/label.json?search=set_id:"..."+AND+version:"..."); the verbatim sentence each value
     came from is kept in pharmacology_evidence.csv. A label's half-life goes only to the evidence file, as a cross-
     check on half_lives.csv. Extended-release labels give no Tmax or bioavailability (they describe the formulation).
  3. ChEMBL (CC BY-SA 3.0; https://www.ebi.ac.uk/chembl/api/data/) for mechanism of action: the molecule is found by
     the ChEMBL ID in the Wikipedia infobox, else its standard InChIKey, else its name, and the mechanism records of
     its parent compound give target and action type. Cites the ChEMBL compound page; the ChEMBL release is printed.
  Nothing from DrugBank, Drugs.com, Medscape or other restricted sources. Nothing is invented: a value not found in
  these sources is left blank.

Parsing (conservative, the same spirit as the half-life parser)
  Numbers are read only when unambiguous: ranges ("1 to 2 hours", "80-90%"), units (minutes -> hours), "~"/"about";
  "<"/"within"/"up to" give only an upper bound and ">" only a lower bound. Where several values are given, oral,
  adult, immediate-release values are preferred over route-, population-, animal- or metabolite-specific ones.
  protein_binding_pct is a single value, or the midpoint of a range no wider than 10 points; anything wider, or a
  bound like "<20%", stays as text with the number blank. CYP enzymes come from the infobox metabolism field and from
  label sentences that say the drug is metabolised by them (sentences about inhibition, induction, interactions or
  other drugs are ignored). Active metabolites are those a label calls active, those the Wikipedia article calls an
  active metabolite, and infobox metabolites that are themselves psychoactive substances in re:dose's list (the
  evidence file says which rule applied).
  Precedence per field: the FDA label first (it is primary human data), then Wikipedia. CYP enzymes and active
  metabolites are the union of both. evidence_level is human_label when any value comes from a label, human_wiki
  when values come from Wikipedia only, animal when every Wikipedia value read is stated to be from animals, none
  when nothing was found.

Outputs (next to this script; nothing is loaded into any database)
  pharmacology.csv           one row per substance
  mechanisms.csv             one row per substance x ChEMBL target
  pharmacology_evidence.csv  one row per value read: field, source, verbatim snippet, citation

Run
    python collect_pharmacology.py --cache DIR        # fetch everything, keep raw API responses in DIR
    python collect_pharmacology.py --cache DIR MDMA   # just these substances, printed, nothing written
  With --cache, re-runs read every API response (Wikipedia, openFDA, ChEMBL) from DIR and make no requests; delete
  DIR (or a subfolder of it) to refresh. Requests are polite: Wikipedia in batches of <=50 titles with a 0.5 s gap,
  openFDA in batches of names (well under its 240/min, 1000/day keyless limits), ChEMBL in batches of IDs.
"""

import argparse
import csv
import hashlib
import json
import re
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import quote

import requests

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import collect_half_lives as hl  # noqa: E402  (matching, Wikipedia client, wikitext cleaner)

USER_AGENT = "re:dose pharmacology collector (aaron@ajar.org)"
hl.session.headers["User-Agent"] = USER_AGENT
OUT = HERE / "pharmacology.csv"
OUT_MECH = HERE / "mechanisms.csv"
OUT_EVID = HERE / "pharmacology_evidence.csv"
FDA = "https://api.fda.gov/drug/label.json"
CHEMBL = "https://www.ebi.ac.uk/chembl/api/data"
COLUMNS = ["substance", "kind", "matched_title", "tmax_text", "tmax_hours_low", "tmax_hours_high",
           "bioavailability_text", "bioavailability_pct_low", "bioavailability_pct_high", "bioavailability_route",
           "protein_binding_text", "protein_binding_pct", "metabolism_text", "cyp_enzymes", "active_metabolites",
           "excretion_text", "onset_text", "duration_text", "evidence_level", "sources", "retrieved_at"]
MECH_COLUMNS = ["substance", "chembl_id", "target", "action_type", "source_url"]
EVID_COLUMNS = ["substance", "field", "source", "value", "snippet", "source_url"]

# Generic names the FDA labels use for a drug whose Wikipedia title differs (keyed by matched Wikipedia title).
LABEL_ALIASES = {
    "γ-Hydroxybutyric acid": ["oxybate"],
    "Gamma-Hydroxybutyric acid": ["oxybate"],
    "Tetrahydrocannabinol": ["dronabinol"],
    "Lysergic acid diethylamide": ["lysergide"],
    "Amphetamine": ["amphetamine"],
    "Esketamine": ["esketamine"],
}
# Drugs whose only FDA labels describe a product unrelated to how re:dose's users take it (keyed by matched title).
LABEL_SKIP = {
    "Alcohol (drug)": "FDA labels for alcohol are antiseptic or ablation (injected ethanol) products.",
    "Caffeine": "The FDA prescription caffeine labels (caffeine citrate) describe apnoea treatment in premature "
                "infants, not adult use.",
    "Nitrous oxide": "FDA nitrous oxide labels are medical gas labels without pharmacokinetics.",
}
SALT_WORDS = {
    "hydrochloride", "hcl", "dihydrochloride", "sulfate", "sulphate", "bitartrate", "tartrate", "maleate", "citrate",
    "mesylate", "dimesylate", "besylate", "phosphate", "sodium", "potassium", "calcium", "acetate", "hydrobromide",
    "fumarate", "succinate", "monohydrate", "anhydrous", "hemihydrate", "trihydrate", "dihydrate", "saccharate",
    "aspartate", "bromide", "lactate", "napsylate", "hyclate", "pamoate", "oxalate", "malate", "gluconate", "nitrate",
    "usp", "ci", "cii", "ciii", "civ", "cv", "c-ii", "c-iii", "c-iv", "c-v", "tablets", "tablet", "capsules", "capsule",
    "injection", "oral", "solution", "and", "odt", "hbr", "base", "camsylate", "edisylate", "tosylate",
}
ER_RE = re.compile(r"extended[- ]release|\bER\b|\bXR\b|\bXL\b|\bSR\b|\bCR\b|\bLA\b|controlled[- ]release|"
                   r"sustained[- ]release|prolonged[- ]release|long[- ]acting|once[- ]daily", re.I)

ANIMAL = re.compile(r"\brats?\b|\bmice\b|\bmouse\b|\bdogs?\b|\bmonkeys?\b|\brabbits?\b|\bpigs?\b|\bhorses?\b|"
                    r"\bcats?\b|\bguinea pigs?\b|\banimals?\b|\brodents?\b|in vivo \(", re.I)
POPULATION = re.compile(r"infant|neonat|prematur|pediatric|paediatric|child|adolescen|elderly|geriatric|older|"
                        r"renal|kidney|hepatic|liver (?:disease|impair)|cirrho|impair|pregnan|obese|overdose|years of age|"
                        r"\bover \d+ years|\baged\b|\d+ years|poor metaboli|ultra-?rapid", re.I)
FORMULATION = re.compile(r"extended[- ]release|\bER\b|\bXR\b|controlled[- ]release|sustained|modified[- ]release|"
                         r"depot|patch|transdermal", re.I)
FOOD = re.compile(r"\bfood\b|\bfed\b|\bmeal\b|high[- ]fat|fasting", re.I)
METABOLITE_CTX = re.compile(r"metabolite|\bM\d\b", re.I)
NEG = re.compile(r"\bnot\b|\bno\b|\bneither\b|\bnor\b|unlikely|negligibl|insignificant|minimal(?:ly)?\b|n't\b", re.I)

# re:dose names that are ordinary words or mixtures, never matched as a metabolite name in label text
NOT_METABOLITE_NAMES = {"Alcohol", "Cannabis", "Coca", "Cocoa", "Kratom", "Nitrous", "Changa", "Cake", "Ayahuasca",
                        "Kava", "Tobacco", "Datura", "Hypnotic", "Sedative", "Depressant", "Opium", "Glucose"}

fda_cache = chembl_cache = None
_last = {"fda": 0.0, "chembl": 0.0}


# --- HTTP (openFDA, ChEMBL) with the same cache layout as the Wikipedia client -----------------------------------

def get_json(kind, url, params):
    """GET url?params as JSON, politely and through the cache. openFDA's 404 (no matches) is returned as {}."""
    cdir = fda_cache if kind == "fda" else chembl_cache
    key = hashlib.sha1(json.dumps([url, params], sort_keys=True).encode()).hexdigest()
    if cdir and (cdir / f"{key}.json").exists():
        return json.loads((cdir / f"{key}.json").read_text())
    delay = 0.35 if kind == "fda" else 0.3
    for attempt in range(5):
        wait = delay - (time.time() - _last[kind])
        if wait > 0:
            time.sleep(wait)
        _last[kind] = time.time()
        try:
            r = hl.session.get(url, params=params, timeout=90)
        except requests.RequestException:
            time.sleep(5 * (attempt + 1))
            continue
        if r.status_code == 200:
            data = r.json()
            break
        if r.status_code == 404 and kind == "fda":
            data = {}
            break
        time.sleep(5 * (attempt + 1))  # 429 or transient error: back off
    else:
        raise RuntimeError(f"{url} {params}: HTTP {r.status_code} {r.text[:300]}")
    if cdir:
        (cdir / f"{key}.json").write_text(json.dumps(data))
    return data


# --- Shared text helpers ----------------------------------------------------------------------------------------

NUMBER_WORDS = {"one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6, "seven": 7, "eight": 8, "nine": 9,
                "ten": 10, "eleven": 11, "twelve": 12}
UNIT_WORD = r"(?:hours?|hrs?|h|minutes?|mins?|days?)\b"


def norm_text(t):
    t = (t or "").replace("—", "–").replace("−", "–").replace(" ", " ").replace(" ", " ")
    t = re.sub(r"\b(?:half an|one-half) hour\b", "0.5 hours", t, flags=re.I)
    t = re.sub(r"\b(?:an|one) hour\b", "1 hour", t, flags=re.I)
    words = "|".join(NUMBER_WORDS)
    num_word = lambda m: str(NUMBER_WORDS[m.group(1).lower()])
    t = re.sub(rf"\b({words})\b(?=\s*(?:to|and|or|–|-)\s*(?:{words}|\d+)\b)", num_word, t, flags=re.I)
    t = re.sub(rf"\b({words})\b(?=\s*{UNIT_WORD})", num_word, t, flags=re.I)
    # "at 2 and 3 hours, respectively": the first value is the first-named thing's (usually the parent drug)
    t = re.sub(rf"(\d+(?:\.\d+)?)\s+and\s+(\d+(?:\.\d+)?)\s*({UNIT_WORD})(,?\s*respectively)",
               r"\1 \3 [and \2 \3\4]", t, flags=re.I)
    t = re.sub(r"(\d)\s*-\s*(\d)", r"\1–\2", t)
    t = re.sub(r"(\d)\s*–\s*(\d)", r"\1–\2", t)
    t = re.sub(r"\bbetween\s+(\d+(?:\.\d+)?)\s*(%|percent|hours?|h|minutes?|min)?\s+and\s+(\d)", r"\1–\3", t, flags=re.I)
    return re.sub(r"(\d)\s*percent\b", r"\1%", t, flags=re.I)


HEADER = re.compile(r"^(?:\d+(?:\.\d+)*\s+)?(?:(?:CLINICAL PHARMACOLOGY|Clinical Pharmacology|Pharmacokinetics|"
                    r"PHARMACOKINETICS|Absorption|Distribution|Metabolism(?:/Elimination| and Excretion)?|Elimination|"
                    r"Excretion|Bioavailability|Protein Binding)\s+)+(?=[A-Z])")


def sentences(text):
    text = re.sub(r"\s+", " ", text or "")
    # sentence ends, plus the seam where a flattened table runs into prose ("... 8.06 ± 2.11 The mean ...")
    parts = re.split(r"(?<=[.!?])\s+(?=[A-Z(\[])|(?<=[\d)%])\s+(?=[A-Z][a-z]+\s[a-z]{2,}\s)", text)
    return [HEADER.sub("", s.strip()) for s in parts if s.strip()]


def snip(s, n=320):
    s = s.strip()
    return s if len(s) <= n else s[:n - 1].rsplit(" ", 1)[0] + "…"


PCT = re.compile(r"(?P<q>~|≈|<|>|≤|≥|about\s|approx\.?\s|approximately\s|up to\s|over\s|less than\s|more than\s|"
                 r"greater than\s|nearly\s|almost\s|at least\s)?\s*(?P<a>\d+(?:\.\d+)?)\s*%?"
                 r"(?:\s*(?:–|to)\s*(?P<b>\d+(?:\.\d+)?))?\s*%(?:\s*±\s*\d+(?:\.\d+)?\s*%?)?"
                 r"|(?P<q3>~|≈|<|>|≤|≥|about\s|approximately\s)?\s*(?P<c>\d+(?:\.\d+)?)\s*±\s*\d+(?:\.\d+)?\s*%", re.I)


def pct_mentions(seg):
    """Percent values in a segment: dicts with low, high, start, depth."""
    out = []
    for m in PCT.finditer(seg):
        if m.group("c"):
            a, b, q = float(m.group("c")), None, (m.group("q3") or "")
        else:
            a, b, q = float(m.group("a")), (float(m.group("b")) if m.group("b") else None), (m.group("q") or "")
        lo, hi = (a, b) if b is not None else (a, a)
        q = q.strip().lower()
        if q in ("<", "≤", "up to", "less than"):
            lo = None
        elif q in (">", "≥", "over", "more than", "greater than", "at least"):
            hi = None
        if seg[m.end():m.end() + 1] == "+":  # "80%+"
            hi = None
        if (lo is not None and lo > 100) or (hi is not None and hi > 100):
            continue
        out.append({"low": lo, "high": hi, "start": m.start(), "depth": hl.depth_at(seg, m.start()),
                    "text": m.group(0).strip()})
    return out


ROUTES = [
    ("intranasal", r"intranasal|insufflat|\bnasal|snort"),
    ("sublingual", r"sublingual"),
    ("buccal", r"buccal"),
    ("rectal", r"rectal"),
    ("intravenous", r"intravenous|\bIV\b|i\.v\."),
    ("intramuscular", r"intramuscular|\bIM\b|i\.m\."),
    ("subcutaneous", r"subcutaneous|\bSC\b|s\.c\."),
    ("inhaled", r"inhal|smok|vapori[sz]|vaping|pulmonary"),
    ("transdermal", r"transdermal|patch"),
    ("topical", r"topical"),
    ("epidural", r"epidural"),
    ("oral", r"\boral(?:ly)?\b|by mouth|\bp\.?o\.?\b|tablet|capsule|ingest|swallow|enteral"),
]


def route_of(text):
    for name, pat in ROUTES:
        if re.search(pat, text or "", re.I):
            return name
    return ""


# --- CYP enzymes ------------------------------------------------------------------------------------------------

CYP_RE = re.compile(
    r"\b(?:CYP\s*(?:450\s*)?|cytochrome\s+P[-\s]?450\s*(?:\(CYP\)\s*)?(?:enzymes?\s*|isoenzymes?\s*|isozymes?\s*)?|"
    r"P[-\s]?450\s*)-?(?P<iso>\d{1,2}[A-Z]\d{0,2})(?![A-Za-z])"
    r"(?P<more>(?:\s*/\s*\d{1,2}(?![\dA-Za-z%])|\s*(?:/|,|,?\s*and|,?\s*or|&)\s*(?:CYP\s*)?\d{1,2}[A-Z]\d{1,2}"
    r"(?![A-Za-z]))*)", re.I)


def cyps_in(text):
    out = []
    for m in CYP_RE.finditer(text or ""):
        first = m.group("iso").upper()
        out.append("CYP" + first)
        fam = re.match(r"\d+[A-Z]", first).group(0)
        for tok in re.findall(r"(\d{1,2}[A-Z]\d{1,2}|\d{1,2})", m.group("more") or "", re.I):
            out.append("CYP" + (tok.upper() if re.search(r"[A-Z]", tok, re.I) else fam + tok))
    return list(dict.fromkeys(out))


CYP_SORT = lambda c: [int(x) if x.isdigit() else x for x in re.findall(r"\d+|\D+", c)]


def wiki_cyps(metab_text):
    """CYP enzymes from an infobox metabolism field, skipping clauses about inhibition or induction."""
    out = []
    for chunk in re.split(r";|\.\s", metab_text or ""):
        if re.search(r"inhibit|induc|\bnot\b", chunk, re.I):
            continue
        out += cyps_in(chunk)
    return list(dict.fromkeys(out))


LABEL_METAB_VERB = re.compile(r"metaboli[sz]|biotransform|\bsubstrate\b|demethylat|dealkylat|hydroxylat|oxidi[sz]|"
                              r"catalyz|catalys|mediated|responsible|convert|formation|formed|clearance", re.I)
LABEL_METAB_BAD = re.compile(r"inhibit|induc|drugs? (?:that are )?metaboli|other drugs|substrates\b|"
                             r"coadminist|co-administ|concomitant|interaction|in combination", re.I)


def label_cyp_sentences(sents):
    out = []
    for s in sents:
        if not cyps_in(s) or not LABEL_METAB_VERB.search(s) or LABEL_METAB_BAD.search(s) or NEG.search(s):
            continue
        out.append(s)
    return out


# --- Wikipedia infobox ------------------------------------------------------------------------------------------

WIKI_FIELDS = ["bioavailability", "protein_bound", "metabolism", "metabolites", "onset", "duration_of_action",
               "excretion", "routes_of_administration", "chembl", "stdinchikey"]
CHEMBOX_MAP = {"bioavail": "bioavailability", "proteinbound": "protein_bound", "metabolism": "metabolism",
               "excretion": "excretion", "adminroutes": "routes_of_administration"}


def wiki_fields(page):
    """(box, {field: cleaned text}, {field: raw}) from the article's drug infobox, else its chembox."""
    wt = page["wikitext"]
    raw = hl.template_fields(wt, hl.INFOBOX)
    box = "Infobox drug"
    if raw is None:
        box = "Chembox"
        pharm = hl.template_fields(wt, hl.CHEMBOX_PHARM) or {}
        raw = {CHEMBOX_MAP[k]: v for k, v in pharm.items() if k in CHEMBOX_MAP}
        ids = hl.template_fields(wt, re.compile(r"\{\{\s*Chembox[ _]Identifiers\s*[|\n}]", re.I)) or {}
        raw["chembl"] = ids.get("chembl", "")
        raw["stdinchikey"] = ids.get("stdinchikey", "")
    return box, {k: hl.clean(raw.get(k, "")) for k in WIKI_FIELDS}, raw


UNKNOWN = re.compile(r"^\s*(?:unknown|n/?a|none|no data|unclear|\?|–|-)\s*\.?$", re.I)


def parse_wiki_bioavailability(text, routes_text):
    """(low, high, route, chosen segment, animal?) from an infobox bioavailability field."""
    t = norm_text(text)
    if not t or UNKNOWN.match(t):
        return None, None, "", "", False
    segs, carry = [], None
    for seg in hl.split_depth0(t):
        seg = seg.strip(" ,")
        lm = re.match(r"^([^:%\d]{1,60}):\s*(.*)$", seg)
        label, body = (lm.group(1), lm.group(2)) if lm else (carry, seg)
        if lm and not lm.group(2):
            carry = lm.group(1)
            continue
        ms = [m for m in pct_mentions(body) if m["depth"] == 0] or pct_mentions(body)[:1]
        if not ms:
            continue
        ctx = f"{label or ''} {body}"
        segs.append({"seg": seg, "m": ms[0], "route": route_of(ctx), "ctx": ctx, "animal": bool(ANIMAL.search(ctx))})
    if not segs:
        return None, None, "", "", False

    def score(s):
        return ((s["route"] == "oral") * 3 + (not s["route"]) * 2 + (s["route"] not in ("", "intravenous")) * 1
                - 4 * s["animal"] - 2 * bool(POPULATION.search(s["ctx"]) or FORMULATION.search(s["ctx"])))
    best = max(segs, key=lambda s: (score(s), -segs.index(s)))
    route = best["route"]
    if not route:
        rs = {route_of(r) for r in hl.split_depth0(norm_text(routes_text).replace(",", ";")) if route_of(r)}
        route = "oral (only route listed)" if rs == {"oral"} else ("unspecified" if len(segs) == 1 else "")
        if not route:  # several unlabelled values
            return None, None, "", "", best["animal"]
    if route == "intravenous" and len(segs) == 1 and best["m"]["low"] == 100:
        return None, None, "", "", False  # "IV: 100%" by definition; says nothing useful
    return best["m"]["low"], best["m"]["high"], route, best["seg"], best["animal"]


def one_pct(lo, hi):
    """A single protein-binding percentage: the value, or the midpoint of a range no wider than 10 points."""
    if lo is None or hi is None:
        return None
    if hi - lo <= 10:
        return round((lo + hi) / 2, 1)
    return None


def parse_wiki_protein(text):
    t = norm_text(text)
    if not t or UNKNOWN.match(t):
        return None, False
    segs = [s for s in hl.split_depth0(t)]
    cands = []
    for seg in segs:
        ms = [m for m in pct_mentions(seg) if m["depth"] == 0] or pct_mentions(seg)[:1]
        if ms:
            cands.append((seg, ms[0]))
    if not cands:
        return None, bool(ANIMAL.search(t))
    clean = [c for c in cands if not (ANIMAL.search(c[0]) or POPULATION.search(c[0]) or METABOLITE_CTX.search(c[0]))]
    if len(cands) > 1 and len(clean) != 1:
        return None, bool(ANIMAL.search(t))  # several values, no single plain one: ambiguous
    seg, m = (clean or cands)[0]
    return one_pct(m["low"], m["high"]), bool(ANIMAL.search(seg))


# --- Active metabolites -----------------------------------------------------------------------------------------

def metabolite_items(text):
    items = []
    for part in re.split(r"[;•]|,(?![^()]*\))|\band\b", text or ""):
        p = re.sub(r"\d+(?:\.\d+)?\s*%", "", part)
        p = re.sub(r"\((?:[^()]*)\)", "", p)
        p = re.sub(r"\s*\(.*$", "", p).strip(" .:,")  # an unclosed "(" left by the split
        p = re.sub(r"^(?:major|minor|main|primary|secondary|active|inactive)\s*:?\s*", "", p, flags=re.I).strip()
        if len(hl.norm(p)) >= 3 and not UNKNOWN.match(p) and not re.match(r"^(?:liver|kidney|urine|various)", p, re.I):
            items.append(p)
    return list(dict.fromkeys(items))


def wiki_active_metabolites(page, metab_text, psychoactive_keys, own_keys):
    """[(metabolite, rule)] from the infobox metabolites field."""
    out = []
    body = None
    for item in metabolite_items(metab_text):
        k = hl.norm(item)
        raw_item = re.search(re.escape(item) + r"[^;•,]*", metab_text)
        tail = raw_item.group(0) if raw_item else ""
        if re.search(r"\binactive\b", tail, re.I):
            continue
        if re.search(r"\bactive\b", tail, re.I):
            out.append((item, "the infobox calls it active"))
            continue
        if k in psychoactive_keys and k not in own_keys:
            out.append((item, "listed as a metabolite in the infobox and itself a psychoactive substance in re:dose's list"))
            continue
        if body is None:
            body = [s for s in sentences(hl.clean(re.sub(r"\{\{\s*(?:Infobox|Drugbox|Chembox)", "{{x", page["wikitext"])))
                    if re.search(r"\bactive metabolites?\b", s, re.I) and not re.search(r"inactive|\bnot\b", s, re.I)]
        name_re = re.compile(r"(?<![\w-])" + re.escape(item) + r"(?![\w-])", re.I)
        hit = next((s for s in body if name_re.search(s)), None)
        if hit:
            out.append((item, f"the article calls it an active metabolite: \"{snip(hit, 240)}\""))
    return out


CHEM_WORD = re.compile(r"(?<![\w-])((?:[α-ωA-Z0-9]{1,3}[-,′']+)*(?:nor|desmethyl|didesmethyl|desalkyl|hydroxy|"
                       r"dihydro|oxo|carboxy|desacetyl|acetyl|desethyl|dehydro|methyl)[a-zα-ω][\w′'-]{3,})", re.I)


def label_active_metabolites(sents, candidates, own_keys):
    """[(metabolite, sentence)] from label sentences that call something an active metabolite."""
    out = []
    for s in sents:
        if not re.search(r"\bactive metabolites?\b|\bpharmacologically active\b|\bactive moiet", s, re.I):
            continue
        if re.search(r"inactive|\bnot\b|\bno\b|unknown|less active|weakly active", s, re.I):
            continue
        found = []
        for c in candidates:
            if hl.norm(c) in own_keys:
                continue
            if re.search(r"(?<![\w-])" + re.escape(c) + r"(?![\w-])", s, re.I):
                found.append(c)
        for m in CHEM_WORD.finditer(s):
            w = m.group(1).strip("-,'′")
            if hl.norm(w) not in own_keys and not re.search(r"(?:ed|ion|ions|ing|ase|ases|ly|ive)$|methylene", w, re.I):
                found.append(w)
        found = [f for f in found if not any(hl.norm(f) != hl.norm(g) and hl.norm(f) in hl.norm(g) for g in found)]
        for f in found:
            if not any(hl.norm(f) in hl.norm(x) or hl.norm(x) in hl.norm(f) for x, _ in out):
                out.append((f, s))
    return out


# --- openFDA labels ---------------------------------------------------------------------------------------------

def strip_salts(term):
    words = re.split(r"[\s,]+", term.lower().replace("(", " ").replace(")", " "))
    return hl.norm(" ".join(w for w in words if w and w not in SALT_WORDS))


def fda_query_names(s):
    page = s.get("page")
    names = [s["name"], hl.clean_name(s["name"])]
    if page:
        names = [page["title"], re.sub(r"\s*\(.*\)$", "", page["title"])] + names + LABEL_ALIASES.get(page["title"], [])
    return [n for n in dict.fromkeys(names) if n and re.fullmatch(r"[A-Za-z0-9 \-]{4,60}", n)]


PRESCRIPTION = 'openfda.product_type:"HUMAN PRESCRIPTION DRUG" AND (_exists_:clinical_pharmacology OR _exists_:pharmacokinetics)'


def find_labels(subs):
    """s['label'] = the chosen openFDA label (or None), s['label_note'] = why not, for each matched drug."""
    todo = [s for s in subs if s["kind"] == "drug" and s.get("page")]
    for s in todo:
        s["label"], s["label_note"] = None, ""
        if s["page"]["title"] in LABEL_SKIP:
            s["label_note"] = LABEL_SKIP[s["page"]["title"]]
    todo = [s for s in todo if not s["label_note"]]
    keys = {s["name"]: {hl.norm(n) for n in fda_query_names(s)} for s in todo}
    terms_of = {s["name"]: [] for s in todo}
    for i in range(0, len(todo), 15):
        batch = todo[i:i + 15]
        names = sorted({n.lower() for s in batch for n in fda_query_names(s)})
        if not names:
            continue
        q = "openfda.generic_name:(" + " ".join(f'"{n}"' for n in names) + ") AND " + PRESCRIPTION
        d = get_json("fda", FDA, {"search": q, "count": "openfda.generic_name.exact", "limit": "1000"})
        for r in d.get("results", []):
            k = strip_salts(r["term"])
            for s in batch:
                if k in keys[s["name"]]:
                    terms_of[s["name"]].append(r["term"])
    for s in todo:
        terms = terms_of[s["name"]]
        if not terms:
            continue
        exact = "openfda.generic_name.exact:(" + " ".join(f'"{t}"' for t in terms) + ") AND " + PRESCRIPTION
        labels = get_json("fda", FDA, {"search": exact + " AND openfda.application_number:NDA*", "limit": "50"}
                          ).get("results", [])
        best = max(labels, key=label_rank) if labels else None
        if best is None or is_er(best) or "ORAL" not in best.get("openfda", {}).get("route", []):
            # no NDA label, or only extended-release / non-oral ones: look at the other (generic) labels too
            labels += get_json("fda", FDA, {"search": exact, "limit": "15"}).get("results", [])
        if labels:
            s["label"] = max(labels, key=label_rank)
            s["label_er"] = is_er(s["label"])


ER_BRANDS = {"gralise", "horizant", "xelstrym"}  # gastroretentive / prodrug ER / patch: not immediate-release oral


def is_er(lab):
    o = lab.get("openfda", {})
    if {b.lower() for b in o.get("brand_name", [])} & ER_BRANDS:
        return True
    head = " ".join(o.get("brand_name", []) + o.get("generic_name", []) +
                    [x[:300] for x in lab.get("spl_product_data_elements", [])] +
                    [x[:400] for x in lab.get("dosage_forms_and_strengths", [])[:1] + lab.get("description", [])[:1]])
    return bool(ER_RE.search(head))


def label_rank(lab):
    o = lab.get("openfda", {})
    app = (o.get("application_number") or [""])[0]
    brand = (o.get("brand_name") or [""])[0]
    generic = (o.get("generic_name") or [""])[0]
    return (not is_er(lab), "ORAL" in o.get("route", []), app.startswith("NDA"),
            bool(lab.get("pharmacokinetics")), strip_salts(brand) != strip_salts(generic),
            lab.get("effective_time", ""))


def label_url(lab):
    return (f"{FDA}?search=set_id:%22{lab['set_id']}%22+AND+version:%22{lab['version']}%22")


TMAX_KW = re.compile(r"\bT\s?max\b|time to (?:reach )?(?:peak|maximum|max)|peak (?:plasma |serum |blood )?"
                     r"(?:drug )?(?:concentration|level)s?|peak (?:plasma|serum|blood)\b|maximum (?:plasma |serum |blood )?"
                     r"(?:drug )?concentrations?|maximal (?:plasma |serum )?concentrations?|\bpeak\b", re.I)
COMPARATIVE = re.compile(r"compared|longer|shorter|delayed|prolong|reduced|increased|decreased|relative to|"
                         r"versus|\bvs\.?\b|\bthan\b", re.I)
HALF_KW = re.compile(r"half[- ]?li(?:fe|ves)|\bt\s?½|\bt\s?1/2", re.I)


def tmax_from(sent):
    """(low, high, text) for the first time value after a Tmax keyword in a sentence."""
    t = norm_text(sent)
    for k in TMAX_KW.finditer(t):
        after = t[k.end():k.end() + 160]
        hl_pos = HALF_KW.search(after)
        ms = [m for m in hl.mentions(after) if not hl_pos or m["span"][0] < hl_pos.start()]
        if not ms:
            continue
        m = ms[0]
        pre = after[max(0, m["span"][0] - 14):m["span"][0]].lower()
        lo, hi = m["low"], m["high"]
        if lo == hi and re.search(r"within|\bby\b|up to|less than|under", pre):
            lo = None
        return lo, hi, m["text"]
    return None


def label_extract(s):
    """Fill s['lab'] with fields read from the chosen label, each with its verbatim sentence."""
    lab = s.get("label")
    s["lab"] = {}
    if not lab:
        return
    pk = (lab.get("pharmacokinetics") or [""])[0]
    text = pk if len(pk) > 200 else (lab.get("clinical_pharmacology") or [pk])[0]
    sents = sentences(text)
    s["lab_sents"] = sents
    own = {hl.norm(n) for n in fda_query_names(s)}
    names_re = re.compile("|".join(re.escape(n) for n in fda_query_names(s)), re.I) if fda_query_names(s) else None
    L = s["lab"]
    er = s.get("label_er")

    def ok(sent, allow_metab=False):
        return not (ANIMAL.search(sent) or POPULATION.search(sent) or FOOD.search(sent)
                    or (not allow_metab and METABOLITE_CTX.search(sent)))

    # Tmax
    if not er:
        for strict in (True, False):
            for sent in sents:
                if not TMAX_KW.search(sent) or (strict and not ok(sent)) or FORMULATION.search(sent):
                    continue
                if not strict and (ANIMAL.search(sent) or POPULATION.search(sent) or FOOD.search(sent)):
                    continue
                if not strict and METABOLITE_CTX.search(sent) and not (names_re and names_re.search(sent)):
                    continue
                if re.search(r"steady[- ]state|multiple dos|repeated dos", sent, re.I) and strict:
                    continue
                if COMPARATIVE.search(sent):
                    continue
                r = tmax_from(sent)
                if r and (r[0] is not None or r[1] is not None) and (r[1] is None or r[1] <= 48):
                    L["tmax"] = {"low": r[0], "high": r[1], "text": r[2], "snippet": sent,
                                 "medium": not strict}
                    break
            if "tmax" in L:
                break
    # half-life (evidence only)
    hls = [x for x in sents if HALF_KW.search(x) and ok(x) and hl.mentions(norm_text(x))
           and not re.search(r"alpha|initial|distribution", x, re.I)]
    hls = [x for x in hls if re.search(r"elimination|terminal", x, re.I)] or hls
    if hls:
        L["half_life"] = {"snippet": hls[0]}
    # bioavailability
    if not er:
        for sent in sents:
            if not re.search(r"bioavailab", sent, re.I) or not ok(sent):
                continue
            if re.search(r"increas|decreas|reduc|higher|lower|relative|compared|bioequival|by \d+%|"
                         r"\bratio\b|times\b|-fold", sent, re.I):
                continue
            t = norm_text(sent)
            k = re.search(r"bioavailab", t, re.I).start()
            ms = [m for m in pct_mentions(t) if m["start"] > k - 80]
            if not ms:
                continue
            m = min(ms, key=lambda m: abs(m["start"] - k))
            if len({(x["low"], x["high"]) for x in ms}) > 1:
                continue  # several different values (by sex, formulation, ...): ambiguous
            route = route_of(sent) or ("oral (label route)" if (lab.get("openfda", {}).get("route") == ["ORAL"]) else "")
            L["bio"] = {"low": m["low"], "high": m["high"], "route": route, "text": m["text"], "snippet": sent}
            break
    # protein binding
    for sent in sents:
        if not (re.search(r"\bbound\b|\bbinding\b", sent, re.I) and re.search(r"protein|albumin|plasma|serum", sent, re.I)):
            continue
        if ANIMAL.search(sent) or POPULATION.search(sent) or METABOLITE_CTX.search(sent):
            continue
        t = norm_text(sent)
        ms = pct_mentions(t)
        k = re.search(r"\bbound\b|\bbinding\b", t, re.I).start()
        ms = [m for m in ms if abs(m["start"] - k) < 90]
        if len(ms) != 1 and not (len(ms) == 2 and ms[0]["low"] == ms[1]["low"]):
            continue
        m = ms[0]
        L["protein"] = {"pct": one_pct(m["low"], m["high"]), "text": m["text"], "snippet": sent}
        break
    # CYP enzymes
    about = re.compile(r"\b(?:it|its|metabolites?|is metaboli[sz]ed|are formed)\b" +
                       (("|" + names_re.pattern) if names_re else ""), re.I)
    cs = [x for x in label_cyp_sentences(sents) if about.search(x)]
    if cs:
        L["cyp"] = {"enzymes": list(dict.fromkeys(c for x in cs for c in cyps_in(x))), "snippets": cs}
    # metabolism snippet (first sentence about metabolism)
    for sent in sents:
        if re.search(r"metaboli[sz]ed|biotransform", sent, re.I) and not LABEL_METAB_BAD.search(sent):
            L["metabolism"] = {"snippet": sent}
            break
    # active metabolites
    cands = s.get("metabolite_candidates", [])
    am = label_active_metabolites(sents, cands, own)
    if am:
        L["active"] = am


# --- ChEMBL -----------------------------------------------------------------------------------------------------

def chembl_batch(endpoint, field, ids, extra=None):
    out = []
    ids = list(dict.fromkeys(i for i in ids if i))
    for i in range(0, len(ids), 40):
        params = {f"{field}__in": ",".join(ids[i:i + 40]), "limit": "1000", **(extra or {})}
        d = get_json("chembl", f"{CHEMBL}/{endpoint}.json", params)
        out += d.get(endpoint + "s", d.get("molecules", []))
    return out


def find_mechanisms(subs):
    """s['chembl'] = parent ChEMBL ID, s['chembl_via'], s['mechanisms'] = [(target, action, mechanism text)]."""
    cand = [s for s in subs if s["kind"] in ("drug", "not_found")]
    for s in cand:
        s["chembl"], s["mechanisms"], s["chembl_via"] = None, [], ""
        wf = s.get("wf", {})
        cid = re.sub(r"\D", "", wf.get("chembl", "").split(";")[0])
        s["_cid"] = f"CHEMBL{cid}" if cid else None
        ik = re.search(r"[A-Z]{14}-[A-Z]{10}-[A-Z]", wf.get("stdinchikey", "") or "")
        s["_ik"] = ik.group(0) if ik else None
    mol_fields = {"only": "molecule_chembl_id,pref_name,molecule_hierarchy,molecule_structures"}
    by_id = {m["molecule_chembl_id"]: m for m in chembl_batch("molecule", "molecule_chembl_id",
                                                               [s["_cid"] for s in cand], mol_fields)}
    by_ik = {}
    for m in chembl_batch("molecule", "molecule_structures__standard_inchi_key",
                          [s["_ik"] for s in cand if s["_ik"] and s["_cid"] not in by_id], mol_fields):
        if m.get("molecule_structures"):
            by_ik[m["molecule_structures"]["standard_inchi_key"]] = m
    for s in cand:
        m, via = None, ""
        if s["_cid"] in by_id:
            m, via = by_id[s["_cid"]], "ChEMBL ID in the Wikipedia infobox"
        elif s["_ik"] in by_ik:
            m, via = by_ik[s["_ik"]], "InChIKey in the Wikipedia infobox"
        else:
            names = ([s["page"]["title"]] if s.get("page") else []) + [s["name"]]
            for n in dict.fromkeys(names):
                if not re.fullmatch(r"[\w ,()\-]{3,80}", n):
                    continue
                d = get_json("chembl", f"{CHEMBL}/molecule.json", {"pref_name__iexact": n, **mol_fields})
                if len(d.get("molecules", [])) == 1:
                    m, via = d["molecules"][0], f"ChEMBL preferred name '{n}'"
                    break
        if m:
            s["chembl"] = (m.get("molecule_hierarchy") or {}).get("parent_chembl_id") or m["molecule_chembl_id"]
            s["chembl_via"] = via
    mechs = chembl_batch("mechanism", "parent_molecule_chembl_id", [s["chembl"] for s in cand])
    # ChEMBL sometimes files the marketed (racemic or stereo-unspecified) drug under another ID than the one the
    # infobox gives (e.g. a single stereoisomer). When that ID has no mechanism, use the molecule with the same
    # connectivity (first InChIKey block) whose ChEMBL preferred name is the drug's own name.
    with_mech = {m["parent_molecule_chembl_id"] for m in mechs}
    for s in cand:
        if not s["chembl"] or s["chembl"] in with_mech or not s.get("page"):
            continue
        ik = s["_ik"] or ((by_id.get(s["_cid"]) or {}).get("molecule_structures") or {}).get("standard_inchi_key")
        if not ik:
            continue
        names = {hl.norm(n) for n in [s["name"], s["page"]["title"], *s["common_names"]]}
        d = get_json("chembl", f"{CHEMBL}/molecule.json",
                     {"molecule_structures__standard_inchi_key__startswith": ik[:14], "limit": "50", **mol_fields})
        same = [m for m in d.get("molecules", []) if hl.norm(m.get("pref_name") or "") in names
                and (m.get("molecule_hierarchy") or {}).get("parent_chembl_id", m["molecule_chembl_id"]) != s["chembl"]]
        if len(same) == 1:
            m = same[0]
            s["chembl"] = (m.get("molecule_hierarchy") or {}).get("parent_chembl_id") or m["molecule_chembl_id"]
            s["chembl_via"] += f"; mechanisms filed under {m['pref_name']} (same connectivity, InChIKey {ik[:14]})"
            mechs += chembl_batch("mechanism", "parent_molecule_chembl_id", [s["chembl"]])
    tnames = {t["target_chembl_id"]: t["pref_name"] for t in
              chembl_batch("target", "target_chembl_id", [m["target_chembl_id"] for m in mechs],
                           {"only": "target_chembl_id,pref_name"})}
    by_parent = {}
    for m in mechs:
        by_parent.setdefault(m["parent_molecule_chembl_id"], []).append(m)
    for s in cand:
        seen = set()
        for m in by_parent.get(s["chembl"], []):
            target = tnames.get(m["target_chembl_id"]) or ""
            act = (m.get("action_type") or "").lower()
            if not target or (target, act) in seen:
                continue
            seen.add((target, act))
            s["mechanisms"].append((target, act, m.get("mechanism_of_action") or ""))


# --- Rows -------------------------------------------------------------------------------------------------------

def fmtn(x):
    return "" if x is None else (int(x) if float(x).is_integer() else round(x, 3))


def build(s, retrieved_at, psychoactive_keys):
    row = dict.fromkeys(COLUMNS, "")
    row.update(substance=s["name"], kind=s["kind"], retrieved_at=retrieved_at)
    ev, sources = [], []

    def evid(field, source, value, snippet, url):
        ev.append({"substance": s["name"], "field": field, "source": source, "value": value,
                   "snippet": snip(snippet or ""), "source_url": url})
        if url and source in ("wikipedia", "openfda") and url not in sources:
            sources.append(url)

    page = s.get("page")
    if s["kind"] == "class":
        row["evidence_level"] = "none"
        return row, ev
    if page is None:
        row["kind"] = "not_found"
        row["evidence_level"] = "none"
        return row, ev
    wurl = (f"https://en.wikipedia.org/w/index.php?title={quote(page['title'].replace(' ', '_'))}"
            f"&oldid={page['revid']}")
    wf = s["wf"]
    row["matched_title"] = page["title"]
    L = s.get("lab", {})
    lurl = label_url(s["label"]) if s.get("label") else ""
    lab_used = wiki_used = False
    animal_flags = []

    # Tmax (labels only: the infobox has no such field)
    if "tmax" in L:
        t = L["tmax"]
        routes = s["label"].get("openfda", {}).get("route", [])
        prefix = "" if "ORAL" in routes or not routes else f"(label route: {', '.join(routes).lower()}) "
        row.update(tmax_text=prefix + snip(t["snippet"], 240), tmax_hours_low=fmtn(t["low"]),
                   tmax_hours_high=fmtn(t["high"]))
        evid("tmax", "openfda", f"{hl.fmt(t['low'], t['high'])}", t["snippet"], lurl)
        lab_used = True
    # bioavailability
    wlo, whi, wroute, wseg, wanimal = parse_wiki_bioavailability(wf["bioavailability"], wf["routes_of_administration"])
    if wf["bioavailability"]:
        evid("bioavailability", "wikipedia", "" if wlo is None and whi is None else
             f"{fmtn(wlo)}–{fmtn(whi)}% ({wroute})", wf["bioavailability"], wurl)
        animal_flags.append(wanimal)
    lab_bounded = "bio" in L and L["bio"]["low"] is not None and L["bio"]["high"] is not None
    wiki_bounded = wlo is not None and whi is not None and wroute.startswith("oral")
    if "bio" in L and ((L["bio"]["route"].startswith("oral") and (lab_bounded or not wiki_bounded))
                       or (wlo is None and whi is None)):
        b = L["bio"]
        row.update(bioavailability_text=snip(b["snippet"], 240), bioavailability_pct_low=fmtn(b["low"]),
                   bioavailability_pct_high=fmtn(b["high"]), bioavailability_route=b["route"] or "unspecified")
        evid("bioavailability", "openfda", f"{fmtn(b['low'])}–{fmtn(b['high'])}% ({b['route']})", b["snippet"], lurl)
        lab_used = True
    elif wf["bioavailability"]:
        row.update(bioavailability_text=wf["bioavailability"], bioavailability_pct_low=fmtn(wlo),
                   bioavailability_pct_high=fmtn(whi), bioavailability_route=wroute if (wlo or whi) else "")
        wiki_used = True
    # protein binding
    wpct, wpanimal = parse_wiki_protein(wf["protein_bound"])
    if wf["protein_bound"]:
        evid("protein_binding", "wikipedia", fmtn(wpct), wf["protein_bound"], wurl)
        animal_flags.append(wpanimal)
    if "protein" in L and (L["protein"]["pct"] is not None or wpct is None):
        p = L["protein"]
        row.update(protein_binding_text=snip(p["snippet"], 240), protein_binding_pct=fmtn(p["pct"]))
        evid("protein_binding", "openfda", fmtn(p["pct"]), p["snippet"], lurl)
        lab_used = True
    elif wf["protein_bound"]:
        row.update(protein_binding_text=wf["protein_bound"], protein_binding_pct=fmtn(wpct))
        wiki_used = True
    # metabolism and CYP enzymes
    cyps = []
    if wf["metabolism"]:
        row["metabolism_text"] = wf["metabolism"]
        wc = wiki_cyps(wf["metabolism"])
        evid("metabolism", "wikipedia", ";".join(wc), wf["metabolism"], wurl)
        cyps += wc
        wiki_used = True
        animal_flags.append(bool(ANIMAL.search(wf["metabolism"])))
    elif "metabolism" in L:
        row["metabolism_text"] = snip(L["metabolism"]["snippet"], 300)
        evid("metabolism", "openfda", "", L["metabolism"]["snippet"], lurl)
        lab_used = True
    if "cyp" in L:
        for sn in L["cyp"]["snippets"]:
            evid("cyp_enzymes", "openfda", ";".join(cyps_in(sn)), sn, lurl)
        cyps += L["cyp"]["enzymes"]
        lab_used = True
    row["cyp_enzymes"] = ";".join(sorted(dict.fromkeys(cyps), key=CYP_SORT))
    # active metabolites
    actives = []
    if wf["metabolites"]:
        evid("metabolites", "wikipedia", "", wf["metabolites"], wurl)
    for name, rule in s.get("wiki_active", []):
        actives.append(name)
        evid("active_metabolites", "wikipedia", name, rule, wurl)
        wiki_used = True
    for name, sent in L.get("active", []):
        if hl.norm(name) not in {hl.norm(a) for a in actives}:
            actives.append(name)
        evid("active_metabolites", "openfda", name, sent, lurl)
        lab_used = True
    row["active_metabolites"] = ";".join(actives)
    # excretion, onset, duration (Wikipedia)
    for col, f in (("excretion_text", "excretion"), ("onset_text", "onset"), ("duration_text", "duration_of_action")):
        if wf[f]:
            row[col] = wf[f]
            evid(f, "wikipedia", "", wf[f], wurl)
            wiki_used = True
    if wf["routes_of_administration"]:
        evid("routes_of_administration", "wikipedia", "", wf["routes_of_administration"], wurl)
    if "half_life" in L:
        evid("half_life (label; cross-check of half_lives.csv)", "openfda", "", L["half_life"]["snippet"], lurl)
    if s.get("label") and s.get("label_er"):
        evid("label_note", "openfda", "", "Only an extended-release label was found: Tmax and bioavailability "
             "were not read from it.", lurl)
    if s.get("label_note"):
        evid("label_note", "openfda", "", s["label_note"], "")
    if s.get("box") == "Chembox":
        evid("wikipedia_note", "wikipedia", "", "Read from the article's chembox (it has no drug infobox).", wurl)

    if lab_used:
        row["evidence_level"] = "human_label"
    elif wiki_used or any(row[c] for c in ("bioavailability_text", "protein_binding_text")):
        row["evidence_level"] = "animal" if animal_flags and all(animal_flags) else "human_wiki"
    else:
        row["evidence_level"] = "none"
    if row["evidence_level"] != "none":
        row["sources"] = ";".join(sources)
    return row, ev


def cross_check_psychonautwiki(rows, subs):
    """Print this file's bioavailability next to PsychonautWiki's (routes_of_administration.parquet), for review."""
    import duckdb
    path = HERE.parent / "site" / "data" / "routes_of_administration.parquet"
    pw = duckdb.sql(f"""select s.name, r.route, r.bioavailability_min, r.bioavailability_max from '{path}' r
                        join '{hl.SUBSTANCES}' s on s.id = r.substance_id
                        where r.bioavailability_min is not null or r.bioavailability_max is not null""").fetchall()
    by = {r["substance"]: r for r in rows}
    print("Bioavailability cross-check against PsychonautWiki (substance, PW route: min-max | here: low-high route):")
    for name, route, lo, hi in pw:
        r = by.get(name)
        if r:
            print(f"  {name}, {route}: {lo}-{hi} | {r['bioavailability_pct_low']}-{r['bioavailability_pct_high']} "
                  f"{r['bioavailability_route']}")


def main():
    global fda_cache, chembl_cache
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("only", nargs="*", help="substance names: just these, printed, nothing written")
    ap.add_argument("--cache", type=Path, help="directory to keep raw API responses in (subfolders per source)")
    args = ap.parse_args()
    if args.cache:
        hl.cache_dir, fda_cache, chembl_cache = (args.cache / "wikipedia", args.cache / "openfda", args.cache / "chembl")
        for d in (hl.cache_dir, fda_cache, chembl_cache):
            d.mkdir(parents=True, exist_ok=True)
    retrieved_at = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")

    all_subs = hl.load_substances()
    psychoactive_keys = {hl.norm(k) for s in all_subs for k in [s["name"], *s["common_names"]] if len(hl.norm(k)) >= 3}
    # re:dose names that can be named as a metabolite in label text: single drugs, not classes, plants or words
    metabolite_names = sorted({x["name"] for x in all_subs if len(x["name"]) >= 4 and not hl.is_class(x["name"])
                               and x["name"] not in hl.OVERRIDES and x["name"] not in NOT_METABOLITE_NAMES})
    subs = hl.load_substances(args.only) if args.only else all_subs
    for s in subs:
        s["kind"] = "class" if hl.is_class(s["name"]) else "drug"
    hl.resolve(subs)
    for s in subs:
        if s["kind"] == "drug" and s.get("page") is None:
            s["kind"] = "not_found"
    for s in subs:
        if s.get("page"):
            s["box"], s["wf"], _ = wiki_fields(s["page"])
            own = {hl.norm(n) for n in [s["name"], s["page"]["title"], *s["common_names"]]}
            s["wiki_active"] = wiki_active_metabolites(s["page"], s["wf"]["metabolites"], psychoactive_keys, own)
            s["metabolite_candidates"] = metabolite_items(s["wf"]["metabolites"]) + metabolite_names
    find_labels(subs)
    for s in subs:
        label_extract(s)
    find_mechanisms(subs)

    rows, evidence, mech_rows = [], [], []
    for s in subs:
        r, ev = build(s, retrieved_at, psychoactive_keys)
        rows.append(r)
        evidence += ev
        for target, act, moa in s.get("mechanisms", []):
            mech_rows.append({"substance": s["name"], "chembl_id": s["chembl"], "target": target, "action_type": act,
                              "source_url": f"https://www.ebi.ac.uk/chembl/explore/compound/{s['chembl']}"})
        if s.get("chembl"):
            evidence.append({"substance": s["name"], "field": "chembl_match", "source": "chembl", "value": s["chembl"],
                             "snippet": f"Matched via {s['chembl_via']}; {len(s['mechanisms'])} mechanism record(s).",
                             "source_url": f"https://www.ebi.ac.uk/chembl/explore/compound/{s['chembl']}"})
    if args.only:
        for r in rows:
            print("\n".join(f"  {k}: {v}" for k, v in r.items() if k != "retrieved_at" and v != ""), end="\n")
            print("  mechanisms:", [(m["target"], m["action_type"]) for m in mech_rows if m["substance"] == r["substance"]])
            print("  evidence:")
            for e in evidence:
                if e["substance"] == r["substance"]:
                    print(f"    [{e['source']}] {e['field']}: {e['value']} | {e['snippet'][:200]}")
            print()
        return
    for path, cols, data in ((OUT, COLUMNS, rows), (OUT_MECH, MECH_COLUMNS, mech_rows), (OUT_EVID, EVID_COLUMNS, evidence)):
        with path.open("w", newline="", encoding="utf-8") as f:
            w = csv.DictWriter(f, fieldnames=cols)
            w.writeheader()
            w.writerows(data)
    status = get_json("chembl", f"{CHEMBL}/status.json", {})
    cross_check_psychonautwiki(rows, subs)
    kinds = {k: sum(r["kind"] == k for r in rows) for k in ("drug", "class", "not_found")}
    has = lambda c: sum(bool(r[c]) for r in rows)
    print(f"Wrote {len(rows)} rows to {OUT}: {kinds}")
    print(f"  Tmax {has('tmax_hours_high') or 0}/{has('tmax_text')} numeric/text; bioavailability "
          f"{sum(bool(r['bioavailability_pct_low'] != '' or r['bioavailability_pct_high'] != '') for r in rows)} numeric; "
          f"protein binding {has('protein_binding_pct')} numeric; CYP {has('cyp_enzymes')}; active metabolites "
          f"{has('active_metabolites')}; labels {sum(bool(s.get('label')) for s in subs)}")
    print(f"Wrote {len(mech_rows)} rows to {OUT_MECH} ({len({m['substance'] for m in mech_rows})} substances; "
          f"{status.get('chembl_db_version')}, released {status.get('chembl_release_date')})")
    print(f"Wrote {len(evidence)} rows to {OUT_EVID}")


if __name__ == "__main__":
    main()
