#!/usr/bin/env python3
"""store.py - the normalised contact store: companies + jobs + people.

Replaces the single flat contacts.csv. Three hand-editable files, joined
here so the rest of the codebase keeps seeing one flat contact row.

    companies.csv   slug, name, domain, email_pattern
    jobs.csv        company_slug, job_id, job_title, personal_note, jd, jd_file
    people.csv      email, name, company_slug, contact_type, email_source

The join rule, decided deliberately: EVERY person at a company is contacted
about EVERY opening at that company. So the person->job link is not stored,
it is a cross join computed at load time by load_contacts().

Two things this module is careful about, because the old writers were not:

 1. Writes are ATOMIC. The old code did open(path, "w"), which empties the
    file immediately and then writes rows one at a time - a crash halfway
    left half a CSV with the original already gone. Here every write goes to
    a temp file first and is swapped in with os.replace(), which the OS
    guarantees happens completely or not at all.

 2. Backups are TIMESTAMPED. notegen_core and emailmap both used to back up
    to the same contacts.csv.bak, so whichever ran second destroyed the
    other's backup. That is how the corrupt header in contacts.csv.bak
    ("...,job_id,\"Software Engineer - Java, Spring Boot...\",...", where
    'role' should be) became unrecoverable.

Also: writes use a FIXED fieldnames list. The old writers echoed back
whatever DictReader happened to see, which is what let that bad header get
baked in permanently.
"""

import csv
import datetime as dt
import os
import re
import shutil
import sys
from pathlib import Path

BASE = Path(__file__).resolve().parent

COMPANIES_CSV = BASE / "companies.csv"
JOBS_CSV = BASE / "jobs.csv"
PEOPLE_CSV = BASE / "people.csv"

COMPANY_FIELDS = ["slug", "name", "domain", "email_pattern"]
JOB_FIELDS = ["company_slug", "job_id", "job_title", "personal_note",
              "jd", "jd_file"]
PEOPLE_FIELDS = ["email", "name", "company_slug", "contact_type", "email_source"]

# A small closed vocabulary on purpose. The old free-text 'role' column shows
# what happens without one: seniority spelled four different ways (-I, 1, 2,
# II, Advanced, Intermediate) and 'java developer' next to 'Java Developer'.
CONTACT_TYPES = {"", "engineer", "engineering_manager", "hiring_manager",
                 "recruiter"}


def slug(s):
    """Fold a company name or filename to a comparable key: '1% Club' -> '1_club'."""
    return re.sub(r"[^a-z0-9]+", "_", (s or "").lower()).strip("_")


# ------------------------------------------------------------------ read

def _read(path, fields, required):
    """Read a CSV into stripped dicts. Missing required columns are fatal."""
    if not path.exists():
        sys.exit(f"ERROR: {path.name} not found. Run: python3 migrate_split.py")
    with open(path, newline="", encoding="utf-8") as f:
        r = csv.DictReader(f)
        header = r.fieldnames or []
        missing = [c for c in required if c not in header]
        if missing:
            sys.exit(f"ERROR: {path.name} is missing column(s): "
                     f"{', '.join(missing)}\n  header was: {', '.join(header)}")
        rows = []
        for raw in r:
            row = {k: (raw.get(k) or "").strip() for k in fields}
            if any(row.values()):          # drop fully blank lines
                rows.append(row)
        return rows


def load_companies():
    """slug -> company dict. Fatal on a duplicate or an empty slug."""
    out = {}
    for row in _read(COMPANIES_CSV, COMPANY_FIELDS, ["slug", "name"]):
        s = row["slug"]
        if not s:
            sys.exit(f"ERROR: {COMPANIES_CSV.name} has a row with no slug: {row}")
        if s in out:
            sys.exit(f"ERROR: {COMPANIES_CSV.name} has duplicate slug {s!r}")
        out[s] = row
    return out


def load_jobs(companies=None):
    """Openings, in file order. Each job carries its company display name so
    notegen_core.find_jd()'s slug-based JD guess keeps working unchanged."""
    companies = companies if companies is not None else load_companies()
    rows = _read(JOBS_CSV, JOB_FIELDS, ["company_slug", "job_id"])
    seen, out = set(), []
    for row in rows:
        cs = row["company_slug"]
        if cs not in companies:
            sys.exit(f"ERROR: {JOBS_CSV.name} references unknown company_slug "
                     f"{cs!r}. Add it to {COMPANIES_CSV.name} or fix the typo.")
        key = (cs, row["job_id"])
        if key in seen:
            sys.exit(f"ERROR: {JOBS_CSV.name} has two rows for "
                     f"company_slug={cs!r} job_id={row['job_id']!r}")
        seen.add(key)
        row["company"] = companies[cs]["name"]
        # 'role' mirrors job_title so a job dict and a joined contact dict are
        # interchangeable for find_jd(), build_prompt() and validate().
        row["role"] = row["job_title"]
        out.append(row)
    return out


def load_people(companies=None):
    """Contacts, in file order. Email may be blank - emailmap fills those in."""
    companies = companies if companies is not None else load_companies()
    rows = _read(PEOPLE_CSV, PEOPLE_FIELDS, ["email", "name", "company_slug"])
    seen, out = set(), []
    for row in rows:
        cs = row["company_slug"]
        if cs not in companies:
            sys.exit(f"ERROR: {PEOPLE_CSV.name} references unknown company_slug "
                     f"{cs!r}. Add it to {COMPANIES_CSV.name} or fix the typo.")
        ct = row["contact_type"].lower()
        if ct not in CONTACT_TYPES:
            sys.exit(f"ERROR: {PEOPLE_CSV.name}: contact_type {row['contact_type']!r} "
                     f"for {row['email'] or row['name']!r} is not one of: "
                     f"{', '.join(sorted(t for t in CONTACT_TYPES if t))}")
        row["contact_type"] = ct
        row["email"] = row["email"].lower()
        if row["email"]:
            if row["email"] in seen:
                sys.exit(f"ERROR: {PEOPLE_CSV.name} lists {row['email']} twice")
            seen.add(row["email"])
        row["company"] = companies[cs]["name"]
        out.append(row)
    return out


def load_contacts():
    """The cross join: one row per (person, opening at that person's company).

    Emits the same flat shape the old contacts.csv had, so downstream code
    barely changes. 'company' is the DISPLAY name, never the slug - the
    outreach.db primary key is (email, company, job_id), so feeding it a slug
    would change every key and re-mail people who were already contacted.

    Both 'role' and 'job_title' are provided for the same value: 'role' is
    what the templates and the notegen prompt already ask for.
    """
    companies = load_companies()
    jobs = load_jobs(companies)
    people = load_people(companies)

    by_company = {}
    for j in jobs:
        by_company.setdefault(j["company_slug"], []).append(j)

    out = []
    for p in people:
        for j in by_company.get(p["company_slug"], []):
            out.append({
                "email": p["email"],
                "name": p["name"],
                "company": p["company"],
                "company_slug": p["company_slug"],
                "contact_type": p["contact_type"],
                "email_source": p["email_source"],
                "job_id": j["job_id"],
                "role": j["job_title"],
                "job_title": j["job_title"],
                "personal_note": j["personal_note"],
                "jd": j["jd"],
                "jd_file": j["jd_file"],
            })
    return out


# ------------------------------------------------------------------ write

def _write(path, fields, rows):
    """Atomic replace + timestamped backup. Returns the backup path or None."""
    backup = None
    if path.exists():
        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = path.with_name(f"{path.stem}.{ts}.csv.bak")
        shutil.copy2(path, backup)

    tmp = path.with_name(path.name + ".tmp")
    try:
        with open(tmp, "w", newline="", encoding="utf-8") as f:
            # extrasaction='ignore' so a joined contact dict (which carries
            # extra keys like 'company' and 'role') can be written back.
            w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
            w.writeheader()
            for row in rows:
                w.writerow({k: (row.get(k) or "") for k in fields})
        os.replace(tmp, path)          # atomic: all of it, or none of it
    except BaseException:
        try:
            tmp.unlink()
        except OSError:
            pass
        raise
    return backup


def write_companies(rows):
    return _write(COMPANIES_CSV, COMPANY_FIELDS, rows)


def write_jobs(rows):
    return _write(JOBS_CSV, JOB_FIELDS, rows)


def write_people(rows):
    return _write(PEOPLE_CSV, PEOPLE_FIELDS, rows)
