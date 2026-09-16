#!/usr/bin/env python3
"""migrate_split.py - one-time split of contacts.csv into the three normalised
files that store.py reads.

    contacts.csv  ->  companies.csv + jobs.csv + people.csv

contacts.csv is NOT modified or deleted; it stays on disk as the reference
that verify_split.py checks the result against.

The one rule that matters: a company already present in outreach.db with
stage > 0 keeps its display name EXACTLY as spelled today. outreach.db keys
contacts on (email, company, job_id) with INSERT OR IGNORE, so changing a
stored company string re-inserts that contact at stage 0 and the person gets
mailed a second time. Companies with no sending history are tidied freely.

    python3 migrate_split.py --dry-run
    python3 migrate_split.py
"""

import argparse
import collections
import csv
import sqlite3
import sys
from pathlib import Path

import store
from store import slug

BASE = Path(__file__).resolve().parent
CONTACTS_CSV = BASE / "contacts.csv"
DB_PATH = BASE / "outreach.db"
JD_DIR = BASE / "jds"


def protected_names():
    """Company strings that must not change: already mailed (stage > 0)."""
    if not DB_PATH.exists():
        return set()
    try:
        conn = sqlite3.connect(DB_PATH)
        return {r[0] for r in conn.execute(
            "SELECT DISTINCT company FROM contacts WHERE stage > 0")}
    except sqlite3.OperationalError:
        return set()          # contacts table not created yet


def pick_display_name(variants, protected):
    """(name, needs_check). Protected spellings win untouched."""
    for v in variants:
        if v in protected:
            return v, False
    best = sorted(variants, key=len)[0].strip()
    if best.islower():
        # Title-casing is a guess: it gets 'Databricks' right but would write
        # 'Uipath' for UiPath and 'Paypay' for PayPay. Flagged for review.
        return best.title(), True
    return best, best != sorted(variants, key=len)[0]


def guess_jd_file(company_slug, name):
    """The jds/*.txt this company already resolves to, if any."""
    if not JD_DIR.is_dir():
        return ""
    want = {company_slug, slug(name)} - {""}
    for p in sorted(JD_DIR.glob("*.txt")):
        if slug(p.stem) in want:
            return f"jds/{p.name}"
    return ""


def main():
    ap = argparse.ArgumentParser(description="Split contacts.csv into 3 files.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the report, write nothing")
    args = ap.parse_args()

    if not CONTACTS_CSV.exists():
        sys.exit(f"ERROR: {CONTACTS_CSV.name} not found.")
    for p in (store.COMPANIES_CSV, store.JOBS_CSV, store.PEOPLE_CSV):
        if p.exists() and not args.dry_run:
            sys.exit(f"ERROR: {p.name} already exists. Move it aside first - "
                     f"this script is a one-time migration, not a re-sync.")

    with open(CONTACTS_CSV, newline="", encoding="utf-8") as f:
        rows = [{k: (v or "").strip() for k, v in r.items() if k} 
                for r in csv.DictReader(f)]

    protected = protected_names()
    problems, notes = [], []

    # ---------------------------------------------------------- companies
    variants = collections.defaultdict(set)
    domains = collections.defaultdict(collections.Counter)
    for r in rows:
        raw = r.get("company", "")
        if not raw.strip():
            problems.append(f"row with no company (job_id {r.get('job_id','')!r}) "
                            f"- SKIPPED, add it by hand")
            continue
        s = slug(raw)
        variants[s].add(raw)
        addr = r.get("email", "").lower()
        if "@" in addr:
            domains[s][addr.split("@", 1)[1]] += 1

    companies = {}
    for s, vs in sorted(variants.items()):
        name, needs_check = pick_display_name(vs, protected)
        if len(vs) > 1:
            notes.append(f"merged {sorted(vs)!r} -> slug {s!r}, name {name!r}")
        if needs_check:
            notes.append(f"CHECK NAME: {s!r} -> {name!r} (was {sorted(vs)[0]!r})")
        companies[s] = {
            "slug": s,
            "name": name,
            "domain": domains[s].most_common(1)[0][0] if domains[s] else "",
            "email_pattern": "",       # 'emailmap learn' fills this in
        }

    # ---------------------------------------------------------- jobs
    jobs = {}
    for r in rows:
        raw = r.get("company", "")
        if not raw.strip():
            continue
        s = slug(raw)
        for jid in [j.strip() for j in r.get("job_id", "").split(",")]:
            if not jid and r.get("job_id", "").strip():
                continue
            key = (s, jid)
            title = r.get("role", "")
            note = r.get("personal_note", "")
            if key not in jobs:
                jobs[key] = {
                    "company_slug": s, "job_id": jid, "job_title": title,
                    "personal_note": note, "jd": "",
                    "jd_file": guess_jd_file(s, companies[s]["name"]),
                }
                continue
            j = jobs[key]
            for col, val in (("job_title", title), ("personal_note", note)):
                if val and not j[col]:
                    j[col] = val
                elif val and j[col] != val:
                    problems.append(
                        f"{s}/{jid}: two different {col} values, kept the first\n"
                        f"      kept    {j[col]!r}\n      dropped {val!r}")

    # ---------------------------------------------------------- people
    people, seen = [], set()
    for r in rows:
        addr = r.get("email", "").lower()
        raw = r.get("company", "")
        if not addr:
            continue                    # job-only row: no person to record
        if not raw.strip():
            problems.append(f"{addr}: no company - SKIPPED")
            continue
        if addr in seen:
            notes.append(f"dropped duplicate person row: {addr}")
            continue
        seen.add(addr)
        people.append({
            "email": addr,
            "name": r.get("name", ""),
            "company_slug": slug(raw),
            "contact_type": "",         # fill in by hand over time
            "email_source": "manual" if addr else "",
        })

    # ---------------------------------------------------------- report
    print(f"\nread {len(rows)} row(s) from {CONTACTS_CSV.name}\n")
    print(f"  companies.csv : {len(companies)} row(s)")
    print(f"  jobs.csv      : {len(jobs)} row(s)")
    print(f"  people.csv    : {len(people)} row(s)")
    withjd = sum(1 for j in jobs.values() if j["jd_file"])
    withnote = sum(1 for j in jobs.values() if j["personal_note"])
    print(f"                  {withjd} with a jd_file, {withnote} with a note")

    if notes:
        print("\nnotes:")
        for n in notes:
            print(f"  [note]  {n}")
    if problems:
        print("\nproblems:")
        for p in problems:
            print(f"  [warn]  {p}")

    if args.dry_run:
        print("\nDRY RUN - nothing written. Re-run without --dry-run to write.")
        return

    store.write_companies(companies.values())
    store.write_jobs(jobs.values())
    store.write_people(people)
    print(f"\nwrote {store.COMPANIES_CSV.name}, {store.JOBS_CSV.name}, "
          f"{store.PEOPLE_CSV.name}")
    print(f"{CONTACTS_CSV.name} left untouched.")
    print("\nNext: python3 verify_split.py")


if __name__ == "__main__":
    main()
