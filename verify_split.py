#!/usr/bin/env python3
"""verify_split.py - prove the split lost nothing and, above all, that nobody
is about to be emailed twice.

Run this after migrate_split.py and BEFORE anything sends.

The check that matters is C: outreach.db keys contacts on
(email, company, job_id) and import uses INSERT OR IGNORE, so if the company
display name a contact resolves to has changed, that contact is re-inserted at
stage 0 and gets mailed a second time. C asserts that has not happened for
anyone already mailed.

    python3 verify_split.py
"""

import csv
import sqlite3
import sys
from pathlib import Path

import store
from store import slug

BASE = Path(__file__).resolve().parent
CONTACTS_CSV = BASE / "contacts.csv"
DB_PATH = BASE / "outreach.db"

fails = []
warns = []


def check(ok, label, detail=""):
    print(f"[{'ok  ' if ok else 'FAIL'}] {label}")
    if not ok:
        fails.append(label)
        if detail:
            print(detail)


def main():
    if not CONTACTS_CSV.exists():
        sys.exit(f"ERROR: {CONTACTS_CSV.name} is the reference for this check "
                 f"and is missing.")

    with open(CONTACTS_CSV, newline="", encoding="utf-8") as f:
        original = [{k: (v or "").strip() for k, v in r.items() if k}
                    for r in csv.DictReader(f)]

    contacts = store.load_contacts()
    people = store.load_people()
    jobs = store.load_jobs()

    # Compare on the SLUG, not the raw company string: tidying 'uipath' to
    # 'Uipath' is intended. Check C is what guards the exact string where it
    # actually matters.
    got_triples = {(c["email"], c["company_slug"], c["job_id"]) for c in contacts}
    got_jobs = {(j["company_slug"], j["job_id"]): j for j in jobs}

    print("\n--- A. nothing lost ---")
    missing, skipped = [], []
    for r in original:
        addr, raw = r.get("email", "").lower(), r.get("company", "")
        if not addr:
            continue                    # job-only row, no contact expected
        if not raw.strip():
            skipped.append(addr)
            continue
        for jid in [j.strip() for j in r.get("job_id", "").split(",")]:
            if not jid and r.get("job_id", "").strip():
                continue
            if (addr, slug(raw), jid) not in got_triples:
                missing.append((addr, slug(raw), jid))
    check(not missing, f"every original contact still resolves "
                       f"({len(got_triples)} pairs from {len(people)} people "
                       f"x their company's openings)",
          "\n".join(f"         MISSING {m}" for m in missing))
    if skipped:
        warns.append(f"{len(skipped)} row(s) skipped for having no company: {skipped}")

    print("\n--- B. job fields round-trip ---")
    bad = []
    for r in original:
        raw = r.get("company", "")
        if not raw.strip():
            continue
        for jid in [j.strip() for j in r.get("job_id", "").split(",")]:
            if not jid and r.get("job_id", "").strip():
                continue
            j = got_jobs.get((slug(raw), jid))
            if j is None:
                bad.append(f"job {(slug(raw), jid)} vanished")
                continue
            if r.get("role", "") and j["job_title"] != r["role"]:
                bad.append(f"{slug(raw)}/{jid} job_title {j['job_title']!r} "
                           f"!= original role {r['role']!r}")
            if r.get("personal_note", "") and j["personal_note"] != r["personal_note"]:
                bad.append(f"{slug(raw)}/{jid} personal_note changed")
    check(not bad, f"job_title and personal_note preserved across {len(jobs)} job(s)",
          "\n".join(f"         {b}" for b in bad))

    print("\n--- C. no key drift for anyone already mailed ---")
    drift, absent = [], []
    if DB_PATH.exists():
        conn = sqlite3.connect(DB_PATH)
        try:
            sent = conn.execute(
                "SELECT email, company, job_id, stage FROM contacts "
                "WHERE stage > 0").fetchall()
        except sqlite3.OperationalError:
            sent = []
        known = {p["email"] for p in people}
        exact = {(c["email"], c["company"], c["job_id"]) for c in contacts}
        for addr, company, jid, stage in sent:
            if addr not in known:
                absent.append(f"{addr} ({company}) - not in people.csv")
                continue
            if (addr, company, jid) not in exact:
                drift.append(f"{addr}: db has company={company!r} job_id={jid!r}, "
                             f"loader no longer produces that key -> WOULD RE-SEND")
        check(not drift, f"all {len(sent) - len(absent)} already-mailed contact(s) "
                         f"keep their exact (email, company, job_id) key",
              "\n".join(f"         {d}" for d in drift))
        if absent:
            warns.append("already-mailed rows not in people.csv (pre-existing, "
                         "they simply will not be re-imported): " + "; ".join(absent))
    else:
        check(True, "no outreach.db yet, nothing to drift")

    print("\n--- D. new pairs created by the cross join ---")
    orig_pairs = set()
    for r in original:
        addr, raw = r.get("email", "").lower(), r.get("company", "")
        if not addr or not raw.strip():
            continue
        for jid in [j.strip() for j in r.get("job_id", "").split(",")]:
            orig_pairs.add((addr, slug(raw), jid))
    new = sorted(got_triples - orig_pairs)
    print(f"       {len(new)} new person-opening pair(s):")
    for addr, cs, jid in new:
        print(f"         {addr}  ->  {cs} / {jid}")

    if warns:
        print("\n--- warnings (not failures) ---")
        for w in warns:
            print(f"[warn] {w}")

    print()
    if fails:
        print(f"{len(fails)} CHECK(S) FAILED - do not send until this is fixed.")
        return 1
    print("ALL CHECKS PASSED.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
