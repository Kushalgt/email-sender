#!/usr/bin/env python3
"""
emailmap.py - predict work email addresses from a per-company pattern.

    email_patterns table        contacts.csv
    company -> domain+pattern   name, company, email, email_source
              \\                    /
               ---> predict --->  proposed address
                        |
                   YOU review the diff
                        |
                   --apply writes it

Three rules this enforces, and why:

 1. NEVER overwrite an address you typed yourself. contacts.csv gains an
    'email_source' column: manual | predicted | verified. Anything that is
    not 'predicted' is untouchable. A missing/blank value is treated as
    'manual', so existing rows are safe by default.

 2. NEVER change the address of a contact already in the outreach pipeline.
    outreach.py keys contacts on PRIMARY KEY (email, company, job_id) - so
    editing the email of an already-sent contact does not update that row,
    it creates a SECOND one, and the person gets mailed twice.

 3. NEVER guess from a name it cannot parse cleanly. A refusal costs you
    nothing; a wrong address costs a hard bounce, and bounces damage the
    sending reputation of the one Gmail account you have.

    python3 emailmap.py add acme acme.io "{first}.{last}|{f}{last}"
    python3 emailmap.py learn        # infer patterns from addresses you already have
    python3 emailmap.py list
    python3 emailmap.py predict      # dry run: show the diff
    python3 emailmap.py predict --apply
"""

import argparse
import csv
import datetime as dt
import re
import shutil
import sqlite3
import sys
import unicodedata
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "outreach.db"
CONTACTS_CSV = BASE / "contacts.csv"

SOURCE_COL = "email_source"
UNTOUCHABLE = {"manual", "verified"}     # anything not 'predicted' is protected

TOKENS = ["{first}", "{last}", "{f}", "{l}"]

SCHEMA = """
CREATE TABLE IF NOT EXISTS email_patterns (
    company      TEXT PRIMARY KEY,
    domain       TEXT NOT NULL,
    patterns     TEXT NOT NULL,
    source       TEXT NOT NULL DEFAULT 'manual',
    learned_from TEXT NOT NULL DEFAULT '',
    updated_at   TEXT NOT NULL
);
"""

# Words that are not part of anybody's name.
TITLES = re.compile(
    r"\b(dr|mr|mrs|ms|miss|prof|professor|phd|ph|md|jr|sr|ii|iii|iv|"
    r"cfa|cpa|mba|pmp|he|him|she|her|they|them)\b\.?", re.I)
EMAIL_OK = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def key(company):
    return re.sub(r"[^a-z0-9]+", "", (company or "").lower())


# ------------------------------------------------------------------ names

def parse_name(full):
    """(first, last) or (None, reason). Refusing beats guessing wrong."""
    if not full or not full.strip():
        return None, "empty name"

    s = unicodedata.normalize("NFKD", full)
    s = "".join(c for c in s if not unicodedata.combining(c))   # Jose <- Jose
    s = re.sub(r"\(.*?\)|\[.*?\]", " ", s)                      # "(She/Her)"
    s = s.split("|")[0].split(",")[0]                           # "Ravi | Hiring!"
    s = TITLES.sub(" ", s)
    if re.search(r"[^\x00-\x7F]", s):
        return None, f"non-Latin characters remain after normalising: {full!r}"
    s = re.sub(r"[^A-Za-z\s'\-]", " ", s)
    parts = [p.strip("'-") for p in s.split() if p.strip("'-")]

    if len(parts) < 2:
        return None, f"only one name part in {full!r} - need first AND last"
    if any(len(p) == 1 for p in (parts[0], parts[-1])):
        return None, (f"{full!r} uses an initial - ambiguous, "
                      f"write the address by hand")

    first, last = parts[0].lower(), parts[-1].lower()
    return (first, last), ""


def render_pattern(pattern, first, last, domain):
    local = (pattern
             .replace("{first}", first)
             .replace("{last}", last)
             .replace("{f}", first[0])
             .replace("{l}", last[0]))
    addr = local if "@" in local else f"{local}@{domain}"
    return addr.lower()


def infer_pattern(addr, first, last):
    """Which pattern produced this known-good address? '' if none match."""
    domain = addr.split("@")[1]
    for p in ["{first}.{last}", "{first}{last}", "{f}{last}", "{first}{l}",
              "{first}_{last}", "{last}.{first}", "{f}.{last}", "{first}",
              "{last}{f}", "{first}-{last}"]:
        if render_pattern(p, first, last, domain) == addr.lower():
            return p
    return ""


# ------------------------------------------------------------------ csv

def read_contacts():
    if not CONTACTS_CSV.exists():
        sys.exit(f"ERROR: {CONTACTS_CSV.name} not found.")
    with open(CONTACTS_CSV, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        return list(r), list(r.fieldnames or [])


def write_contacts(rows, fields):
    backup = CONTACTS_CSV.with_suffix(".csv.bak")
    shutil.copy2(CONTACTS_CSV, backup)
    with open(CONTACTS_CSV, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=fields)
        w.writeheader()
        for row in rows:
            w.writerow({k: row.get(k, "") for k in fields})
    return backup


def source_of(row):
    """Blank/missing provenance means a human put it there. Safe default."""
    return (row.get(SOURCE_COL) or "").strip().lower() or "manual"


def already_in_pipeline(conn, addr):
    """True if outreach.py has already sent to this address."""
    if not addr:
        return False
    try:
        r = conn.execute(
            "SELECT 1 FROM contacts WHERE email=? AND stage>0 LIMIT 1",
            (addr.strip().lower(),)).fetchone()
        return r is not None
    except sqlite3.OperationalError:
        return False        # contacts table not created yet


# ------------------------------------------------------------------ commands

def cmd_add(args):
    for p in args.patterns.split("|"):
        if not any(t in p for t in TOKENS):
            sys.exit(f"ERROR: pattern {p!r} has no placeholder.\n"
                     f"  use any of: {', '.join(TOKENS)}\n"
                     f'  e.g. "{{first}}.{{last}}"  or  "{{f}}{{last}}"')
    conn = db()
    conn.execute(
        """INSERT INTO email_patterns
           (company, domain, patterns, source, learned_from, updated_at)
           VALUES (?,?,?,'manual','',?)
           ON CONFLICT(company) DO UPDATE SET
             domain=excluded.domain, patterns=excluded.patterns,
             source='manual', updated_at=excluded.updated_at""",
        (key(args.company), args.domain.lower().lstrip("@"), args.patterns,
         dt.datetime.now().isoformat(timespec="seconds")))
    conn.commit()
    print(f"  {key(args.company)} -> {args.patterns} @{args.domain}")
    print("  next: python3 emailmap.py predict")


def cmd_learn(args):
    """Read the addresses you already trust and work out the pattern."""
    conn = db()
    rows, _ = read_contacts()
    found = {}
    for row in rows:
        addr = (row.get("email") or "").strip().lower()
        if not addr or not EMAIL_OK.match(addr) or source_of(row) == "predicted":
            continue
        parsed, why = parse_name(row.get("name", ""))
        if not parsed:
            continue
        pattern = infer_pattern(addr, *parsed)
        if not pattern:
            print(f"  [?] {addr}: no known pattern produces this from "
                  f"{row.get('name')!r}")
            continue
        k = key(row.get("company", ""))
        found.setdefault(k, []).append((pattern, addr.split("@")[1], addr))

    if not found:
        print("Nothing to learn. Add a contact whose address you know is right.")
        return

    for k, hits in found.items():
        patterns = sorted({p for p, _, _ in hits})
        domain = hits[0][1]
        if len(patterns) > 1:
            print(f"  [warn] {k}: addresses disagree {patterns} - storing all, "
                  f"most common first")
        conn.execute(
            """INSERT INTO email_patterns
               (company, domain, patterns, source, learned_from, updated_at)
               VALUES (?,?,?,'learned',?,?)
               ON CONFLICT(company) DO UPDATE SET
                 domain=excluded.domain, patterns=excluded.patterns,
                 source='learned', learned_from=excluded.learned_from,
                 updated_at=excluded.updated_at""",
            (k, domain, "|".join(patterns), hits[0][2],
             dt.datetime.now().isoformat(timespec="seconds")))
        print(f"  [learned] {k} -> {'|'.join(patterns)} @{domain}  "
              f"(from {hits[0][2]})")
    conn.commit()


def cmd_list(args):
    conn = db()
    rows = conn.execute("SELECT * FROM email_patterns ORDER BY company").fetchall()
    if not rows:
        print("No patterns stored. Add one:\n"
              '  python3 emailmap.py add acme acme.io "{first}.{last}"')
        return
    for r in rows:
        print(f"\n{r['company']}  @{r['domain']}   [{r['source']}]")
        for i, p in enumerate(r["patterns"].split("|")):
            print(f"   {i+1}. {p}@{r['domain']}")
        if r["learned_from"]:
            print(f"   learned from: {r['learned_from']}")
    print()


def cmd_predict(args):
    conn = db()
    pats = {r["company"]: r for r in conn.execute("SELECT * FROM email_patterns")}
    if not pats:
        sys.exit("No patterns stored. Run 'add' or 'learn' first.")

    rows, fields = read_contacts()
    if SOURCE_COL not in fields:
        fields = fields + [SOURCE_COL]
        print(f"  (will add a '{SOURCE_COL}' column)")

    changes, skipped = [], []
    for row in rows:
        addr = (row.get("email") or "").strip().lower()
        src = source_of(row)
        who = row.get("name") or "(no name)"
        k = key(row.get("company", ""))

        if addr and src in UNTOUCHABLE:
            skipped.append(f"{who}: address is '{src}' - protected")
            continue
        if already_in_pipeline(conn, addr):
            skipped.append(f"{who}: already sent to {addr} - changing it would "
                           f"create a duplicate contact")
            continue
        if k not in pats:
            skipped.append(f"{who}: no pattern stored for company "
                           f"{row.get('company')!r}")
            continue

        parsed, why = parse_name(who)
        if not parsed:
            skipped.append(f"{who}: {why}")
            continue

        rec = pats[k]
        first_pattern = rec["patterns"].split("|")[0]
        new = render_pattern(first_pattern, *parsed, rec["domain"])
        if new == addr:
            continue
        alts = [render_pattern(p, *parsed, rec["domain"])
                for p in rec["patterns"].split("|")[1:]]
        changes.append((row, addr, new, alts, rec["source"]))

    for msg in skipped:
        print(f"  [skip] {msg}")
    if not changes:
        print("\nNothing to change.")
        return

    print(f"\n{'='*68}")
    for row, old, new, alts, src in changes:
        print(f"{row.get('name')}  ({row.get('company')})")
        print(f"   {old or '(empty)'}")
        print(f"   -> {new}      [pattern source: {src}]")
        if alts:
            print(f"      fallbacks if it bounces: {', '.join(alts)}")
    print("="*68)

    if not args.apply:
        print(f"\n{len(changes)} change(s). DRY RUN - nothing written.")
        print("Re-run with --apply to write them into contacts.csv.")
        return

    for row, _, new, _, _ in changes:
        row["email"] = new
        row[SOURCE_COL] = "predicted"
    for row in rows:
        row.setdefault(SOURCE_COL, "")
        if not (row.get(SOURCE_COL) or "").strip():
            row[SOURCE_COL] = "manual" if (row.get("email") or "").strip() else ""

    backup = write_contacts(rows, fields)
    print(f"\n{len(changes)} address(es) written to {CONTACTS_CSV.name} "
          f"(backup: {backup.name})")
    print("These are GUESSES. Check them, then:  python3 outreach.py send --dry-run")


def main():
    p = argparse.ArgumentParser(
        description="Predict work emails from per-company patterns.")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="store a pattern for a company")
    a.add_argument("company")
    a.add_argument("domain", help="e.g. acme.io")
    a.add_argument("patterns", help='"{first}.{last}" or "{first}.{last}|{f}{last}"')
    a.set_defaults(func=cmd_add)

    sub.add_parser("learn", help="infer patterns from addresses you already have"
                   ).set_defaults(func=cmd_learn)
    sub.add_parser("list", help="show stored patterns").set_defaults(func=cmd_list)

    d = sub.add_parser("predict", help="fill in missing addresses")
    d.add_argument("--apply", action="store_true",
                   help="actually write to contacts.csv (default is a dry run)")
    d.set_defaults(func=cmd_predict)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
