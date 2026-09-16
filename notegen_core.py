#!/usr/bin/env python3
"""notegen_core.py - everything notegen.py (local/Ollama) and notegen_hosted.py
(OpenRouter) share: the validator, the prompt, retrieval, JD lookup, caching,
and the review/apply/list commands.

Nothing in this file talks to a model over the network. Each script supplies
its own `generate_fn(c, system, user, temperature, seed) -> raw_text` and
passes it into make_note(). That is the only seam between "local" and
"hosted" - the safety net (validate()) is identical either way.
"""

import datetime as dt
import difflib
import hashlib
import json
import re
import sqlite3
import sys
import urllib.error
from pathlib import Path

import store
from store import slug

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "outreach.db"
CONFIG_PATH = BASE / "config.json"
FACTS_PATH = BASE / "resume_facts.json"
JD_DIR = BASE / "jds"

# Bump this whenever you change the prompt. It is part of the cache key, so a
# prompt change invalidates old drafts instead of silently reusing them.
PROMPT_VERSION = "v2"

DEFAULTS = {
    "model": "",                    # each script decides how it's chosen/validated
    "timeout_seconds": 180,
    "max_attempts": 4,
    "max_sentences": 2,
    "min_words": 18,
    "max_words": 55,
    "similarity_threshold": 0.55,   # vs previously generated notes
    "top_k_facts": 2,
}

BANNED = [
    "i was excited", "i am excited", "i'm excited", "excited to see",
    "i am passionate", "i'm passionate", "passionate about",
    "i hope this email finds you", "i came across", "i wanted to reach out",
    "as a highly", "i believe i would be", "perfect fit", "dream job",
    "delve", "leverage my", "synergy", "cutting-edge", "game-changer",
]
GREETINGS = re.compile(r"^\s*(hi|hey|hello|dear|greetings)\b", re.I)
SIGNOFFS = re.compile(r"\b(best regards|kind regards|regards|sincerely|thanks in advance|warm wishes)\b", re.I)

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes (
    cache_key  TEXT PRIMARY KEY,
    email      TEXT NOT NULL DEFAULT '',      -- legacy, kept for old rows
    company    TEXT NOT NULL DEFAULT '',      -- display name, for the review UI
    company_slug TEXT NOT NULL DEFAULT '',
    job_id     TEXT NOT NULL DEFAULT '',
    note       TEXT NOT NULL DEFAULT '',
    status     TEXT NOT NULL DEFAULT 'draft',   -- draft|approved|needs_human|applied
    attempts   INTEGER NOT NULL DEFAULT 0,
    reasons    TEXT NOT NULL DEFAULT '',
    model      TEXT NOT NULL DEFAULT '',
    used_facts TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL
);
"""


# ------------------------------------------------------------------ infra

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    migrate(conn)
    return conn


def migrate(conn):
    """Add columns SCHEMA cannot add to a database that already exists,
    because it uses CREATE TABLE IF NOT EXISTS. Idempotent."""
    have = {r["name"] for r in conn.execute("PRAGMA table_info(notes)")}
    for col in ("company_slug", "job_id"):
        if col not in have:
            conn.execute(f"ALTER TABLE notes ADD COLUMN {col} "
                         f"TEXT NOT NULL DEFAULT ''")
    conn.commit()


def cfg(section, extra_defaults=None):
    """Load config.json's `section` block over DEFAULTS (+ per-backend extras).

    Each script wraps this in its own no-arg `cfg()` pointed at its own
    section, so callers (and test_validator.py) don't need to know about
    sections at all.
    """
    out = dict(DEFAULTS)
    if extra_defaults:
        out.update(extra_defaults)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            out.update(json.load(f).get(section, {}))
    return out


def facts():
    if not FACTS_PATH.exists():
        sys.exit(f"ERROR: {FACTS_PATH.name} not found - it is the grounding file, "
                 f"the model cannot run without it.")
    with open(FACTS_PATH) as f:
        d = json.load(f)
    if not d.get("achievements"):
        sys.exit("ERROR: resume_facts.json has no achievements. Add some first.")
    return d


def facts_text(d):
    """Every word the model is allowed to claim about YOU."""
    parts = [d["identity"].get("name", ""), d["identity"].get("title", ""),
             d["identity"].get("company", "")]
    parts += d.get("skills", []) + d.get("companies", [])
    for a in d["achievements"]:
        parts.append(a["text"])
        parts += a.get("tags", []) + a.get("metrics", [])
    return " ".join(parts)


def parse_note(raw):
    """Pull the note out of the model's reply, tolerating a non-JSON answer."""
    raw = (raw or "").strip()
    if not raw:
        return ""
    try:
        return str(json.loads(raw).get("note", "")).strip()
    except Exception:
        pass
    m = re.search(r'"note"\s*:\s*"(.*?)"\s*[,}]', raw, re.S)
    if m:
        return m.group(1).replace('\\"', '"').replace("\\n", " ").strip()
    # Last resort: the model ignored the format and just wrote the sentences.
    return raw.strip().strip('"').strip()


# ------------------------------------------------------------------ retrieval

WORD = re.compile(r"[A-Za-z][A-Za-z0-9+#._'-]*")
NUM = re.compile(r"\d+(?:[.,]\d+)*\s*%?")
SENT_SPLIT = re.compile(r"(?<=[.!?])\s+")
FIRST_PERSON = re.compile(r"\b(I|I'm|I've|my|My|me)\b")

# Capitalised mid-sentence words that are not claims about you.
COMMON_OK = {"i", "i'm", "i've", "monday", "tuesday", "wednesday", "thursday",
             "friday", "january", "february", "march", "april", "may", "june",
             "july", "august", "september", "october", "november", "december"}


def norm(tok):
    return tok.lower().strip(".,;:!?()[]\"'").removesuffix("'s")


def vocab(text):
    return {norm(t) for t in WORD.findall(text or "") if norm(t)}


def retrieve(jd_text, d, k):
    """Score achievements by tag overlap with the JD. Deterministic, no deps.

    An embedding model would rank better, but with <10 achievements lexical
    overlap is enough and it never surprises you.
    """
    jd_low = (jd_text or "").lower()
    jd_tokens = vocab(jd_text)
    scored = []
    for i, a in enumerate(d["achievements"]):
        score = 0
        for tag in a.get("tags", []):
            t = tag.lower()
            if " " in t or "-" in t:
                score += 2 if t in jd_low else 0
            elif t in jd_tokens:
                score += 2
        for skill in d.get("skills", []):
            if skill.lower() in jd_low and skill.lower() in a["text"].lower():
                score += 1
        scored.append((-score, i, a))
    scored.sort()
    picked = [a for _, _, a in scored[:k]]
    matched = scored[0][0] < 0 if scored else False
    return picked, matched


# ------------------------------------------------------------------ prompt

SYSTEM = """You write one short opening line for a cold job-application email.

HARD RULES:
1. Use ONLY the facts in FACTS. Never invent a technology, company, number or
   experience that is not written there.
2. Exactly two sentences. No greeting, no sign-off, no name.
3. Sentence 1: one concrete thing about THEIR job or company, taken from the JD.
   Sentence 2: connect ONE fact from FACTS to it.
4. Plain, direct, human. No "excited", no "passionate", no flattery.
5. Never say you used or worked with something unless FACTS says so.

Reply with JSON only: {"note": "<the two sentences>"}"""


def build_prompt(job, jd_text, picked):
    """One note per OPENING, not per person.

    There is deliberately no RECIPIENT line: every contact at a company gets
    the same note for a given opening, so a note that named one person would
    be wrong for the other seven.
    """
    chosen = "\n".join(f"- {a['text']}" for a in picked)
    jd = (jd_text or "").strip()
    if len(jd) > 3000:                 # keep the context small; less room to drift
        jd = jd[:3000] + " ..."
    return (
        f"FACTS (the only things you may claim about the sender):\n{chosen}\n\n"
        f"COMPANY: {job.get('company','')}\n"
        f"ROLE: {job.get('role','') or 'Backend Software Engineer'}\n\n"
        f"JOB DESCRIPTION:\n{jd}\n\n"
        f"Write the two sentences now."
    )


# ------------------------------------------------------------------ validator

def validate(note, d, jd_text, contact, c, previous):
    """Return a list of reasons the note is unusable. Empty list = it passed.

    This is the part that makes a small/local/hosted model safe to use. The
    model is allowed to be mediocre; it is not allowed to make things up
    about you.
    """
    reasons = []
    note = (note or "").strip()

    if not note:
        return ["empty output"]

    # -- structure -----------------------------------------------------
    sentences = [s for s in SENT_SPLIT.split(note) if s.strip()]
    if len(sentences) > c["max_sentences"]:
        reasons.append(f"{len(sentences)} sentences (max {c['max_sentences']})")
    words = WORD.findall(note)
    if len(words) < c["min_words"]:
        reasons.append(f"too short ({len(words)} words)")
    if len(words) > c["max_words"]:
        reasons.append(f"too long ({len(words)} words)")
    if GREETINGS.match(note):
        reasons.append("starts with a greeting")
    if SIGNOFFS.search(note):
        reasons.append("contains a sign-off")
    if "\n" in note:
        reasons.append("contains a line break")

    low = note.lower()
    for phrase in BANNED:
        if phrase in low:
            reasons.append(f"banned phrase: {phrase!r}")

    # -- grounding -----------------------------------------------------
    resume_v = vocab(facts_text(d))
    # No recipient name here: the note is shared across everyone at the
    # company, so naming a person must not be permitted.
    them_v = vocab(" ".join([contact.get("company", ""),
                             contact.get("role", "")]))
    jd_v = vocab(jd_text)
    allowed = resume_v | them_v | jd_v | COMMON_OK
    about_me_allowed = resume_v | them_v | COMMON_OK

    for sent in sentences:
        toks = WORD.findall(sent)
        mine = bool(FIRST_PERSON.search(sent))
        for idx, tok in enumerate(toks):
            # Strip trailing punctuation first, otherwise a sentence-final
            # period makes every last word look like "Node.js".
            core = tok.rstrip(".,;:!?)]\"'")
            if not core:
                continue
            # A whole-token match is enough ("Spring Boot" written as one word,
            # or a compound the JD itself uses).
            if norm(core) in allowed and not (mine and norm(core) not in about_me_allowed):
                continue
            # Otherwise split compounds: "Kafka-based" must be judged on
            # "Kafka", not on the literal string "kafka-based", which will
            # never appear in resume_facts.json. Only the entity-looking
            # halves are checked; ordinary English modifiers are ignored.
            for part_i, part in enumerate(re.split(r"[-/]", core)):
                if not part:
                    continue
                n = norm(part)
                if not n or n in COMMON_OK:
                    continue
                looks_like_entity = (
                    any(ch.isdigit() for ch in part)
                    or ((idx > 0 or part_i > 0) and any(ch.isupper() for ch in part))
                    or any(ch in part[1:] for ch in ".+#")
                )
                if not looks_like_entity:
                    continue
                if n not in allowed:
                    reasons.append(f"ungrounded term: {tok!r}")
                elif mine and n not in about_me_allowed:
                    reasons.append(f"claims {tok!r} as your experience, but it is "
                                   f"only in the JD - not in resume_facts.json")

    # -- numbers -------------------------------------------------------
    haystack = (facts_text(d) + " " + (jd_text or "")).lower()
    for raw in NUM.findall(note):
        n = raw.strip().replace(" ", "")
        if n and n.rstrip("%") not in haystack.replace(" ", ""):
            reasons.append(f"unverifiable number: {raw.strip()!r}")

    # -- sameness ------------------------------------------------------
    for prev in previous:
        if not prev:
            continue
        ratio = difflib.SequenceMatcher(None, low, prev.lower()).ratio()
        if ratio >= c["similarity_threshold"]:
            reasons.append(f"too similar to an earlier note ({ratio:.2f})")
            break
    opener = " ".join(w.lower() for w in words[:5])
    for prev in previous:
        if opener and opener == " ".join(WORD.findall(prev)[:5]).lower():
            reasons.append("same opening words as an earlier note")
            break

    return list(dict.fromkeys(reasons))   # de-dupe, keep order


# ------------------------------------------------------------------ jd + cache

def find_jd(row):
    """Where the job description comes from, in priority order."""
    inline = (row.get("jd") or "").strip()
    if inline:
        return inline, "csv:jd"

    named = (row.get("jd_file") or "").strip()
    if named:
        p = Path(named)
        p = p if p.is_absolute() else (BASE / p)
        if p.exists():
            return p.read_text(encoding="utf-8"), str(p.name)
        return "", f"MISSING FILE {p}"

    local = (row.get("email") or "").split("@")[0]
    raw = (row.get("company") or "").strip()
    company = slug(raw)
    for guess in (f"{local}.txt", f"{company}.txt", f"{raw}.txt"):
        p = JD_DIR / guess
        if p.exists():
            return p.read_text(encoding="utf-8"), f"jds/{guess}"

    # Last resort: match on the SLUG of each filename, so "1% Club.txt",
    # "1-club.txt" and "1_Club.txt" all resolve for company "1% Club".
    # Filenames are typed by hand; the lookup should not be pedantic.
    if JD_DIR.is_dir():
        want = {slug(local), company} - {""}
        for p in sorted(JD_DIR.glob("*.txt")):
            if slug(p.stem) in want:
                return p.read_text(encoding="utf-8"), f"jds/{p.name}"
    return "", ""


def cache_key(row, jd_text, d, c):
    """Identifies an OPENING. The recipient's email is deliberately absent:
    it used to be in here, which meant eight contacts at one company cost
    eight model runs to produce eight copies of the same note."""
    blob = "|".join([
        row.get("company_slug", "") or slug(row.get("company", "")),
        row.get("job_id", ""), jd_text, str(d.get("version", "")),
        PROMPT_VERSION, c["model"],
    ])
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def known_notes(conn):
    """Everything already written, so we can refuse to repeat ourselves."""
    out = [r["note"] for r in conn.execute(
        "SELECT note FROM notes WHERE note != ''")]
    out += [j["personal_note"] for j in store.load_jobs()]
    return [n for n in out if n]


def read_jobs():
    """The openings to write notes for. One note per opening, shared by every
    contact at that company."""
    return store.load_jobs()


# ------------------------------------------------------------------ generation

def make_note(c, d, row, jd_text, previous, generate_fn):
    """Try until the validator is happy. Returns (note, reasons, used_fact_ids).

    generate_fn(c, system, user, temperature, seed) -> raw_text is the only
    thing that differs between a local (Ollama) and hosted (OpenRouter) run.
    """
    picked, matched = retrieve(jd_text, d, c["top_k_facts"])
    user = build_prompt(row, jd_text, picked)
    used = ",".join(a["id"] for a in picked) + ("" if matched else " (no tag match)")

    temps = [0.35, 0.6, 0.85, 1.0]
    last = []
    for attempt in range(c["max_attempts"]):
        t = temps[min(attempt, len(temps) - 1)]
        try:
            raw = generate_fn(c, SYSTEM, user, t, 1000 + attempt)
        except urllib.error.URLError as e:
            return "", [f"cannot reach model API: {e}"], used
        except Exception as e:
            return "", [f"model call failed: {e!r}"], used

        note = " ".join(parse_note(raw).split())
        reasons = validate(note, d, jd_text, row, c, previous)
        if not reasons:
            return note, [], used
        last = reasons
        print(f"    attempt {attempt+1} rejected: {'; '.join(reasons[:3])}")
    return "", last, used


# ------------------------------------------------------------------ shared commands
# review/apply/list don't care which backend produced a draft, so both
# scripts point straight at these instead of duplicating them.

def cmd_review(args):
    """The human gate. Nothing reaches jobs.csv without passing through here."""
    conn = db()
    rows = conn.execute(
        "SELECT * FROM notes WHERE status IN ('draft','needs_human') "
        "ORDER BY created_at").fetchall()
    if not rows:
        print("Nothing to review. Run: python3 notegen.py draft")
        return

    for r in rows:
        print("\n" + "=" * 68)
        where = f"{r['company'] or r['company_slug']} {r['job_id']}".strip()
        print(f"{where}   facts used: {r['used_facts']}")
        print("-" * 68)
        if r["status"] == "needs_human":
            print("  the model could not produce a valid note.")
            print(f"  last reasons: {r['reasons']}")
            print("  type a note yourself, or press Enter to skip.")
            typed = input("  > ").strip()
            if typed:
                conn.execute(
                    "UPDATE notes SET note=?, status='approved' WHERE cache_key=?",
                    (typed, r["cache_key"]))
                conn.commit()
                print("  approved (yours).")
            continue

        print(f"  {r['note']}")
        print("-" * 68)
        choice = input("  [a]pprove  [e]dit  [r]eject  [s]kip  [q]uit > ").strip().lower()
        if choice == "q":
            break
        if choice == "a":
            conn.execute("UPDATE notes SET status='approved' WHERE cache_key=?",
                         (r["cache_key"],))
            conn.commit()
            print("  approved.")
        elif choice == "e":
            print("  type the corrected note (one line):")
            typed = input("  > ").strip()
            if typed:
                conn.execute(
                    "UPDATE notes SET note=?, status='approved' WHERE cache_key=?",
                    (typed, r["cache_key"]))
                conn.commit()
                print("  approved (edited).")
        elif choice == "r":
            conn.execute("UPDATE notes SET status='needs_human' WHERE cache_key=?",
                         (r["cache_key"],))
            conn.commit()
            print("  rejected - re-run draft --force to try again.")

    n = conn.execute("SELECT COUNT(*) n FROM notes WHERE status='approved'").fetchone()["n"]
    print(f"\n{n} approved note(s) ready. Next: python3 notegen.py apply")


def cmd_apply(args):
    """Write approved notes into jobs.csv. outreach.py takes it from there.

    The join is on (company_slug, job_id) - the opening - not on an email
    address. The old email-keyed join could not tell one person's two
    applications apart, and left drafts orphaned whenever an address changed.
    """
    conn = db()

    # One opening can have SEVERAL approved notes: cache_key includes the
    # model, so drafting with notegen.py and then notegen_hosted.py caches two
    # rows for the same opening. Take the newest and say what was ignored,
    # rather than letting a dict silently pick one at random.
    approved, superseded = {}, {}
    for r in conn.execute("SELECT * FROM notes WHERE status='approved' "
                          "ORDER BY created_at"):
        k = (r["company_slug"], r["job_id"])
        if k in approved:
            superseded[k] = superseded.get(k, 1) + 1
        approved[k] = r
    if not approved:
        print("No approved notes. Run: python3 notegen.py review")
        return

    for (cs, jid), n in sorted(superseded.items()):
        label = f"{cs}/{jid}".strip("/") or "(no opening recorded)"
        print(f"  [note] {label}: {n} approved notes, using the newest "
              f"(model {approved[(cs, jid)]['model']})")

    jobs = store.load_jobs()

    pending, written = [], 0
    for job in jobs:
        hit = approved.get((job["company_slug"], job["job_id"]))
        if not hit:
            continue
        where = f"{job['company_slug']}/{job['job_id']}".rstrip("/")
        if job["personal_note"] and not args.force:
            print(f"  [keep] {where}: already has a note (use --force to replace)")
            continue
        job["personal_note"] = hit["note"]
        pending.append(hit["cache_key"])
        written += 1
        print(f"  [write] {where}")

    # An approved note whose (company_slug, job_id) matches no opening can
    # never be applied. Say so instead of silently doing nothing - rows cached
    # before the split have both fields blank and land here.
    keys = {(j["company_slug"], j["job_id"]) for j in jobs}
    orphans = [r for k, r in approved.items() if k not in keys]
    for r in orphans:
        k = (r["company_slug"], r["job_id"])
        label = f"{k[0]}/{k[1]}".strip("/") or "(no opening recorded)"
        n = superseded.get(k, 1)
        print(f"  [orphan] {label}: {n} approved note(s), but no such opening "
              f"in {store.JOBS_CSV.name} - cannot be applied")

    if not written:
        print("\nNothing to write.")
        if orphans:
            n_rows = sum(superseded.get((r["company_slug"], r["job_id"]), 1)
                         for r in orphans)
            print(f"{n_rows} approved note(s) match no opening. If they "
                  f"are left over from before the split, drop them with:\n"
                  f"  sqlite3 outreach.db \"DELETE FROM notes WHERE "
                  f"company_slug='';\"")
        return

    # Write the file FIRST, then commit the status flips. If the write raises,
    # nothing is marked 'applied', so the approved note is still there to
    # retry. The reverse order would strand it: flagged applied, never written.
    backup = store.write_jobs(jobs)
    for key in pending:
        conn.execute("UPDATE notes SET status='applied' WHERE cache_key=?", (key,))
    conn.commit()

    print(f"\n{written} note(s) written to {store.JOBS_CSV.name}"
          + (f" (backup: {backup.name})" if backup else ""))
    print("Now check them for real:  python3 outreach.py send --dry-run")


def cmd_list(args):
    conn = db()
    rows = conn.execute("SELECT * FROM notes ORDER BY created_at").fetchall()
    if not rows:
        print("Cache is empty.")
        return
    for r in rows:
        where = f"{r['company'] or r['company_slug']} {r['job_id']}".strip()
        print(f"\n{r['status']:<12} {where or r['email']}  [{r['model']}]")
        print(f"  {r['note'] or '(none)'}")
        if r["reasons"]:
            print(f"  reasons: {r['reasons']}")
    print()
