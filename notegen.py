#!/usr/bin/env python3
"""
notegen.py - draft the {{personal_note}} line for outreach.py with a LOCAL
open-weight model, then make you approve it before it can ever be sent.

    resume_facts.json + a job description
        -> pick the 1-2 most relevant achievements     (retrieval)
        -> local model writes 2 sentences              (generation)
        -> validator rejects anything ungrounded       (the real safety net)
        -> you approve or edit, one by one             (the human gate)
        -> written into contacts.csv personal_note     (outreach.py untouched)

Deliberate design choice: this script does NOT import or modify outreach.py.
outreach.py still refuses to send any contact whose personal_note is empty.
That rule is the thing keeping you safe, so we route around it instead of
weakening it - a note only reaches contacts.csv after you approved it.

Requires: Ollama running locally. Nothing else. Stdlib only, no pip installs.

    python3 notegen.py doctor      # is Ollama up? which models are pulled?
    python3 notegen.py draft       # generate notes for contacts missing one
    python3 notegen.py review      # approve / edit / regenerate / skip
    python3 notegen.py apply       # write approved notes into contacts.csv
    python3 notegen.py list        # show what is in the cache
"""

import argparse
import csv
import datetime as dt
import difflib
import hashlib
import json
import re
import shutil
import sqlite3
import sys
import urllib.error
import urllib.request
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "outreach.db"
CONFIG_PATH = BASE / "config.json"
CONTACTS_CSV = BASE / "contacts.csv"
FACTS_PATH = BASE / "resume_facts.json"
JD_DIR = BASE / "jds"

# Bump this whenever you change the prompt. It is part of the cache key, so a
# prompt change invalidates old drafts instead of silently reusing them.
PROMPT_VERSION = "v1"

DEFAULTS = {
    "model": "",                    # doctor will list what you actually have
    "host": "http://127.0.0.1:11434",
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
    email      TEXT NOT NULL,
    company    TEXT NOT NULL DEFAULT '',
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
    return conn


def cfg():
    """config.json is optional here; a 'notegen' block overrides DEFAULTS."""
    out = dict(DEFAULTS)
    if CONFIG_PATH.exists():
        with open(CONFIG_PATH) as f:
            out.update(json.load(f).get("notegen", {}))
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


# ------------------------------------------------------------------ model client

def ollama(path, payload=None, timeout=30, host=None):
    """Minimal Ollama HTTP call. Returns parsed JSON, raises on transport error."""
    url = (host or DEFAULTS["host"]).rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if data else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def installed_models(c):
    """Names of models already pulled. Empty list if Ollama is unreachable."""
    try:
        tags = ollama("/api/tags", timeout=10, host=c["host"])
        return sorted(m["name"] for m in tags.get("models", []))
    except Exception:
        return []


def generate(c, system, user, temperature, seed):
    """One chat completion, asked to return JSON. Returns raw text."""
    payload = {
        "model": c["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False,
        "format": "json",          # ask Ollama to constrain output to JSON
        "options": {"temperature": temperature, "seed": seed, "num_predict": 220},
    }
    resp = ollama("/api/chat", payload, timeout=c["timeout_seconds"], host=c["host"])
    return (resp.get("message") or {}).get("content", "")


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


def build_prompt(contact, jd_text, picked):
    chosen = "\n".join(f"- {a['text']}" for a in picked)
    jd = (jd_text or "").strip()
    if len(jd) > 3000:                 # keep the context small; less room to drift
        jd = jd[:3000] + " ..."
    return (
        f"FACTS (the only things you may claim about the sender):\n{chosen}\n\n"
        f"COMPANY: {contact.get('company','')}\n"
        f"ROLE: {contact.get('role','') or 'Backend Software Engineer'}\n"
        f"RECIPIENT: {contact.get('name','')}\n\n"
        f"JOB DESCRIPTION:\n{jd}\n\n"
        f"Write the two sentences now."
    )


# ------------------------------------------------------------------ validator

def validate(note, d, jd_text, contact, c, previous):
    """Return a list of reasons the note is unusable. Empty list = it passed.

    This is the part that makes a small local model safe to use. The model is
    allowed to be mediocre; it is not allowed to make things up about you.
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
    them_v = vocab(" ".join([contact.get("company", ""), contact.get("name", ""),
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

def slug(s):
    """Fold a company name or filename to a comparable key: '1% Club' -> '1_club'."""
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")


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
    blob = "|".join([
        (row.get("email") or "").lower(), row.get("company", ""),
        row.get("job_id", ""), jd_text, str(d.get("version", "")),
        PROMPT_VERSION, c["model"],
    ])
    return hashlib.sha256(blob.encode()).hexdigest()[:32]


def known_notes(conn):
    """Everything already written, so we can refuse to repeat ourselves."""
    out = [r["note"] for r in conn.execute(
        "SELECT note FROM notes WHERE note != ''")]
    if CONTACTS_CSV.exists():
        with open(CONTACTS_CSV, newline="", encoding="utf-8") as f:
            out += [(r.get("personal_note") or "").strip()
                    for r in csv.DictReader(f)]
    return [n for n in out if n]


def read_contacts():
    if not CONTACTS_CSV.exists():
        sys.exit(f"ERROR: {CONTACTS_CSV.name} not found.")
    with open(CONTACTS_CSV, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        return list(r), (r.fieldnames or [])


# ------------------------------------------------------------------ generation

def make_note(c, d, row, jd_text, previous):
    """Try until the validator is happy. Returns (note, reasons, used_fact_ids)."""
    picked, matched = retrieve(jd_text, d, c["top_k_facts"])
    user = build_prompt(row, jd_text, picked)
    used = ",".join(a["id"] for a in picked) + ("" if matched else " (no tag match)")

    temps = [0.35, 0.6, 0.85, 1.0]
    last = []
    for attempt in range(c["max_attempts"]):
        t = temps[min(attempt, len(temps) - 1)]
        try:
            raw = generate(c, SYSTEM, user, t, seed=1000 + attempt)
        except urllib.error.URLError as e:
            return "", [f"cannot reach Ollama at {c['host']}: {e}"], used
        except Exception as e:
            return "", [f"model call failed: {e!r}"], used

        note = " ".join(parse_note(raw).split())
        reasons = validate(note, d, jd_text, row, c, previous)
        if not reasons:
            return note, [], used
        last = reasons
        print(f"    attempt {attempt+1} rejected: {'; '.join(reasons[:3])}")
    return "", last, used


# ------------------------------------------------------------------ commands

def cmd_doctor(args):
    c = cfg()
    print(f"\nOllama host : {c['host']}")
    models = installed_models(c)
    if not models:
        print("  [FAIL] cannot reach Ollama, or no models pulled.")
        print("         Start it, then pull a small instruct model, e.g.:")
        print("           ollama serve")
        print("           ollama pull <a 7B-14B instruct model>")
        print("         Check the current model list at ollama.com/library -"
              " I deliberately do not hardcode a name here.")
    else:
        print(f"  [ok]   reachable, {len(models)} model(s):")
        for m in models:
            print(f"           {m}")

    if not c["model"]:
        print('\n  [FAIL] no model chosen. Add this to config.json:')
        print('           "notegen": { "model": "<one of the names above>" }')
    elif models and c["model"] not in models:
        print(f"\n  [FAIL] configured model {c['model']!r} is not pulled.")
    elif c["model"]:
        print(f"\n  [ok]   model: {c['model']}")

    print(f"\nGrounding   : {FACTS_PATH.name}")
    if FACTS_PATH.exists():
        d = facts()
        print(f"  [ok]   version {d.get('version')}, "
              f"{len(d['achievements'])} achievement(s), "
              f"{len(d.get('skills', []))} skill(s)")
        if len(d["achievements"]) < 4:
            print("  [warn] fewer than 4 achievements - notes will get repetitive.")
    else:
        print("  [FAIL] missing.")

    rows, _ = read_contacts()
    need = [r for r in rows if not (r.get("personal_note") or "").strip()]
    print(f"\nContacts    : {len(rows)} row(s), {len(need)} without a note")
    for r in need:
        jd, src = find_jd(r)
        mark = "ok " if jd else "NO JD"
        print(f"  [{mark}] {r.get('email','')}  <- {src or 'nothing found'}")
    if need and not any(find_jd(r)[0] for r in need):
        print(f"\n  Put job descriptions in {JD_DIR.name}/ as <email-local-part>.txt")
        print(f"  or add a 'jd_file' / 'jd' column to contacts.csv.")
    print()


def cmd_draft(args):
    c = cfg()
    if not c["model"]:
        sys.exit("ERROR: no model configured. Run: python3 notegen.py doctor")
    d = facts()
    conn = db()
    rows, _ = read_contacts()
    previous = known_notes(conn)
    made = failed = skipped = 0

    for row in rows:
        addr = (row.get("email") or "").strip().lower()
        if not addr:
            continue
        if (row.get("personal_note") or "").strip() and not args.force:
            continue

        jd_text, src = find_jd(row)
        if not jd_text:
            print(f"  [skip] {addr}: no job description found ({src or 'nothing'})")
            skipped += 1
            continue

        key = cache_key(row, jd_text, d, c)
        hit = conn.execute("SELECT * FROM notes WHERE cache_key=?", (key,)).fetchone()
        if hit and hit["status"] in ("draft", "approved", "applied") and not args.force:
            print(f"  [cached] {addr}: {hit['status']}")
            continue

        print(f"  [gen] {addr}  (jd: {src})")
        note, reasons, used = make_note(c, d, row, jd_text, previous)
        status = "draft" if note else "needs_human"
        conn.execute(
            """INSERT OR REPLACE INTO notes
               (cache_key,email,company,note,status,attempts,reasons,model,
                used_facts,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (key, addr, row.get("company", ""), note, status, c["max_attempts"],
             "; ".join(reasons), c["model"], used,
             dt.datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        if note:
            previous.append(note)
            made += 1
            print(f"    -> {note}")
        else:
            failed += 1
            print(f"    -> GAVE UP: {'; '.join(reasons[:3])}")

    print(f"\n{made} drafted, {failed} need you, {skipped} skipped (no JD).")
    if made:
        print("Next: python3 notegen.py review")


def cmd_review(args):
    """The human gate. Nothing reaches contacts.csv without passing through here."""
    conn = db()
    rows = conn.execute(
        "SELECT * FROM notes WHERE status IN ('draft','needs_human') "
        "ORDER BY created_at").fetchall()
    if not rows:
        print("Nothing to review. Run: python3 notegen.py draft")
        return

    for r in rows:
        print("\n" + "=" * 68)
        print(f"{r['email']}   ({r['company']})   facts used: {r['used_facts']}")
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
    """Write approved notes into contacts.csv. outreach.py takes it from there."""
    conn = db()
    approved = {r["email"]: r for r in conn.execute(
        "SELECT * FROM notes WHERE status='approved'")}
    if not approved:
        print("No approved notes. Run: python3 notegen.py review")
        return

    rows, fields = read_contacts()
    if "personal_note" not in fields:
        sys.exit("ERROR: contacts.csv has no personal_note column.")

    backup = CONTACTS_CSV.with_suffix(".csv.bak")
    shutil.copy2(CONTACTS_CSV, backup)

    written = 0
    for row in rows:
        addr = (row.get("email") or "").strip().lower()
        hit = approved.get(addr)
        if not hit:
            continue
        if (row.get("personal_note") or "").strip() and not args.force:
            print(f"  [keep] {addr}: already has a note (use --force to replace)")
            continue
        row["personal_note"] = hit["note"]
        conn.execute("UPDATE notes SET status='applied' WHERE cache_key=?",
                     (hit["cache_key"],))
        written += 1
        print(f"  [write] {addr}")

    with open(CONTACTS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})
    conn.commit()

    print(f"\n{written} note(s) written to {CONTACTS_CSV.name} "
          f"(backup: {backup.name})")
    print("Now check them for real:  python3 outreach.py send --dry-run")


def cmd_list(args):
    conn = db()
    rows = conn.execute("SELECT * FROM notes ORDER BY created_at").fetchall()
    if not rows:
        print("Cache is empty.")
        return
    for r in rows:
        print(f"\n{r['status']:<12} {r['email']}  [{r['model']}]")
        print(f"  {r['note'] or '(none)'}")
        if r["reasons"]:
            print(f"  reasons: {r['reasons']}")
    print()


def main():
    p = argparse.ArgumentParser(
        description="Draft personal_note lines with a local open-weight model.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="check Ollama, model, facts and JDs").set_defaults(func=cmd_doctor)

    g = sub.add_parser("draft", help="generate notes for contacts missing one")
    g.add_argument("--force", action="store_true",
                   help="regenerate even if cached or already written")
    g.set_defaults(func=cmd_draft)

    sub.add_parser("review", help="approve / edit each draft").set_defaults(func=cmd_review)

    a = sub.add_parser("apply", help="write approved notes into contacts.csv")
    a.add_argument("--force", action="store_true", help="overwrite existing notes")
    a.set_defaults(func=cmd_apply)

    sub.add_parser("list", help="show the note cache").set_defaults(func=cmd_list)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()