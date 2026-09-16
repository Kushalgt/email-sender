#!/usr/bin/env python3
"""
emailmap.py - predict work email addresses from a per-company pattern.

    companies.csv               people.csv
    slug -> domain+pattern      name, company_slug, email, email_source
              \\                    /
               ---> predict --->  proposed address
                        |
                   YOU review the diff
                        |
                   --apply writes it

Three rules this enforces, and why:

 1. NEVER overwrite an address you typed yourself. people.csv carries an
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
                                     # (patterns live in companies.csv)
    python3 emailmap.py list
    python3 emailmap.py predict      # dry run: show the diff
    python3 emailmap.py predict --apply
"""

import argparse
import collections
import re
import sqlite3
import sys
import unicodedata
from pathlib import Path

import store

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "outreach.db"

SOURCE_COL = "email_source"
UNTOUCHABLE = {"manual", "verified"}     # anything not 'predicted' is protected

TOKENS = ["{first}", "{last}", "{f}", "{l}"]

# Words that are not part of anybody's name.
TITLES = re.compile(
    r"\b(dr|mr|mrs|ms|miss|prof|professor|phd|ph|md|jr|sr|ii|iii|iv|"
    r"cfa|cpa|mba|pmp|he|him|she|her|they|them)\b\.?", re.I)
EMAIL_OK = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")


def db():
    """Only used to check the send pipeline. Patterns live in companies.csv."""
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def require_company(companies, raw):
    """Resolve a company argument to a slug that exists in companies.csv."""
    s = store.slug(raw)
    if s not in companies:
        sys.exit(f"ERROR: no company with slug {s!r} in "
                 f"{store.COMPANIES_CSV.name}.\n"
                 f"  add a row for it first (slug,name,domain,email_pattern)\n"
                 f"  known slugs: {', '.join(sorted(companies))}")
    return s


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
    companies = store.load_companies()
    s = require_company(companies, args.company)
    companies[s]["domain"] = args.domain.lower().lstrip("@")
    companies[s]["email_pattern"] = args.patterns
    store.write_companies(companies.values())
    print(f"  {s} -> {args.patterns} @{companies[s]['domain']}")
    print("  next: python3 emailmap.py predict")


def cmd_learn(args):
    """Read the addresses you already trust and work out the pattern."""
    companies = store.load_companies()
    people = store.load_people(companies)
    found = {}
    for row in people:
        addr = row["email"]
        if not addr or not EMAIL_OK.match(addr) or source_of(row) == "predicted":
            continue
        parsed, why = parse_name(row["name"])
        if not parsed:
            continue
        pattern = infer_pattern(addr, *parsed)
        if not pattern:
            print(f"  [?] {addr}: no known pattern produces this from "
                  f"{row['name']!r}")
            continue
        found.setdefault(row["company_slug"], []).append(
            (pattern, addr.split("@")[1], addr))

    if not found:
        print("Nothing to learn. Add a contact whose address you know is right.")
        return

    for slug, hits in sorted(found.items()):
        counts = collections.Counter(p for p, _, _ in hits)
        patterns = [p for p, _ in counts.most_common()]
        domain = collections.Counter(d for _, d, _ in hits).most_common(1)[0][0]
        if len(patterns) > 1:
            print(f"  [warn] {slug}: addresses disagree {patterns} - storing "
                  f"all, most common first")
        companies[slug]["domain"] = domain
        companies[slug]["email_pattern"] = "|".join(patterns)
        print(f"  [learned] {slug} -> {'|'.join(patterns)} @{domain}  "
              f"(from {hits[0][2]})")
    store.write_companies(companies.values())


def cmd_list(args):
    companies = store.load_companies()
    have = [c for c in companies.values() if c["email_pattern"]]
    if not have:
        print("No patterns stored. Add one:\n"
              '  python3 emailmap.py add acme acme.io "{first}.{last}"')
        return
    for c in sorted(have, key=lambda r: r["slug"]):
        print(f"\n{c['slug']}  @{c['domain']}   ({c['name']})")
        for i, pat in enumerate(c["email_pattern"].split("|")):
            print(f"   {i+1}. {pat}@{c['domain']}")
    print()


def cmd_predict(args):
    conn = db()
    companies = store.load_companies()
    pats = {s: c for s, c in companies.items()
            if c["email_pattern"] and c["domain"]}
    if not pats:
        sys.exit("No patterns stored. Run 'add' or 'learn' first.")

    people = store.load_people(companies)

    changes, skipped = [], []
    for row in people:
        addr = row["email"]
        src = source_of(row)
        who = row["name"] or "(no name)"
        slug = row["company_slug"]

        if addr and src in UNTOUCHABLE:
            skipped.append(f"{who}: address is '{src}' - protected")
            continue
        if already_in_pipeline(conn, addr):
            skipped.append(f"{who}: already sent to {addr} - changing it would "
                           f"create a duplicate contact")
            continue
        if slug not in pats:
            skipped.append(f"{who}: no pattern stored for company "
                           f"{companies[slug]['name']!r}")
            continue

        parsed, why = parse_name(who)
        if not parsed:
            skipped.append(f"{who}: {why}")
            continue

        rec = pats[slug]
        plist = rec["email_pattern"].split("|")
        new = render_pattern(plist[0], *parsed, rec["domain"])
        if new == addr:
            continue
        alts = [render_pattern(pat, *parsed, rec["domain"]) for pat in plist[1:]]
        changes.append((row, addr, new, alts))

    for msg in skipped:
        print(f"  [skip] {msg}")
    if not changes:
        print("\nNothing to change.")
        return

    print(f"\n{'='*68}")
    for row, old, new, alts in changes:
        print(f"{row['name']}  ({row['company']})")
        print(f"   {old or '(empty)'}")
        print(f"   -> {new}")
        if alts:
            print(f"      fallbacks if it bounces: {', '.join(alts)}")
    print("="*68)

    if not args.apply:
        print(f"\n{len(changes)} change(s). DRY RUN - nothing written.")
        print(f"Re-run with --apply to write them into "
              f"{store.PEOPLE_CSV.name}.")
        return

    for row, _, new, _ in changes:
        row["email"] = new
        row[SOURCE_COL] = "predicted"
    for row in people:
        if not (row.get(SOURCE_COL) or "").strip():
            row[SOURCE_COL] = "manual" if row["email"] else ""

    backup = store.write_people(people)
    print(f"\n{len(changes)} address(es) written to {store.PEOPLE_CSV.name}"
          + (f" (backup: {backup.name})" if backup else ""))
    print("These are GUESSES. Check them, then:  python3 outreach.py send --dry-run")


def main():
    p = argparse.ArgumentParser(
        description="Predict work emails from per-company patterns.")
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("add", help="store a pattern for a company")
    a.add_argument("company", help="the slug from companies.csv")
    a.add_argument("domain", help="e.g. acme.io")
    a.add_argument("patterns", help='"{first}.{last}" or "{first}.{last}|{f}{last}"')
    a.set_defaults(func=cmd_add)

    sub.add_parser("learn", help="infer patterns from addresses you already have"
                   ).set_defaults(func=cmd_learn)
    sub.add_parser("list", help="show stored patterns").set_defaults(func=cmd_list)

    d = sub.add_parser("predict", help="fill in missing addresses")
    d.add_argument("--apply", action="store_true",
                   help="actually write to people.csv (default is a dry run)")
    d.set_defaults(func=cmd_predict)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
