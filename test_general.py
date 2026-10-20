#!/usr/bin/env python3
"""Offline tests for general inquiries (a company with no opening).

Covers:
  1. store.load_general_contacts() returns only people whose company has no
     opening, under the reserved job_id; load_jobs() refuses that job_id.
  2. import_contacts() adds a general inquiry for them, never for someone
     already contacted about an opening at that company, and supersedes an
     active inquiry once the company gets an opening.
  3. Templates, subject and job_refs for a general row: the real
     templates/general*.txt render with no unfilled placeholder and no
     "Req ID" line, and follow-ups thread under the same subject.

No network, no model, no writes outside a temp directory.

Run:  python3 test_general.py
"""

import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store
import outreach

G = store.GENERAL_JOB_ID
REAL_BASE = Path(__file__).resolve().parent
fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    fails += 0 if ok else 1
    print(f"[{'ok  ' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"         got  {got!r}\n         want {want!r}")


def check_raises(label, fn):
    global fails
    try:
        fn()
        ok = False
    except SystemExit:
        ok = True
    fails += 0 if ok else 1
    print(f"[{'ok  ' if ok else 'FAIL'}] {label}")


def write(path, text):
    path.write_text(text.lstrip("\n"), encoding="utf-8")


tmp = Path(tempfile.mkdtemp(prefix="general-test-"))
store.COMPANIES_CSV = tmp / "companies.csv"
store.JOBS_CSV = tmp / "jobs.csv"
store.PEOPLE_CSV = tmp / "people.csv"

write(tmp / "companies.csv", """
slug,name,domain,email_pattern
acme,Acme Corp,acme.io,{first}.{last}
nojobs,NoJobs Ltd,nojobs.com,
""")
JOBS_ACME_ONLY = """
company_slug,job_id,job_title,personal_note,jd,jd_file
acme,J1,Backend Engineer,a note,,
"""
write(tmp / "jobs.csv", JOBS_ACME_ONLY)
write(tmp / "people.csv", """
email,name,company_slug,contact_type,email_source
ann@acme.io,Ann Ant,acme,engineer,manual
Dana@NoJobs.com,Dana Doe,nojobs,recruiter,manual
,Eve Blank,nojobs,,manual
bad-address,Fay Bad,nojobs,,manual
""")

# ------------------------------------------------------------------ store
print("--- load_general_contacts ---")
rows = store.load_general_contacts()
by_email = {r["email"]: r for r in rows}
check("only people at a company with no opening",
      sorted(by_email), ["", "bad-address", "dana@nojobs.com"])
dana = by_email["dana@nojobs.com"]
check("reserved job_id", dana["job_id"], G)
check("company is the DISPLAY name", dana["company"], "NoJobs Ltd")
check("every opening field is blank",
      [dana[k] for k in ("role", "job_title", "personal_note", "jd", "jd_file")],
      [""] * 5)
check("contact_type comes from the person", dana["contact_type"], "recruiter")
check("load_contacts() is unchanged: no row for a company with no opening",
      [r for r in store.load_contacts() if r["company_slug"] == "nojobs"], [])

write(tmp / "jobs.csv", JOBS_ACME_ONLY + "acme,General,Ghost,,,\n")
check_raises("jobs.csv may not use the reserved job_id (any case)",
             store.load_jobs)
write(tmp / "jobs.csv", JOBS_ACME_ONLY)

# ------------------------------------------------------------------ import
print("\n--- import_contacts adds general inquiries ---")
outreach.DB_PATH = tmp / "g1.db"
outreach.SUPPRESS_CSV = tmp / "suppress.csv"
conn = outreach.db()
outreach.import_contacts(conn, None)


def keys(c):
    return sorted((r["email"], r["job_id"], r["status"])
                  for r in c.execute("SELECT * FROM contacts"))


check("Ann gets the job email, Dana a general inquiry, bad rows skipped",
      keys(conn),
      [("ann@acme.io", "J1", "active"), ("dana@nojobs.com", G, "active")])
row = conn.execute("SELECT * FROM contacts WHERE job_id=?", (G,)).fetchone()
check("general row stores no personal_note and no role",
      (row["personal_note"], row["role"]), ("", ""))

outreach.import_contacts(conn, None)
check("re-importing adds nothing twice", len(keys(conn)), 2)

print("\n--- the company gets an opening: active inquiry is superseded ---")
conn.execute("UPDATE contacts SET stage=1, first_sent_at='2026-01-01T10:00:00', "
             "last_sent_at='2026-01-01T10:00:00' WHERE job_id=?", (G,))
conn.commit()
write(tmp / "jobs.csv", JOBS_ACME_ONLY + "nojobs,N1,Backend Engineer,a note,,\n")
outreach.import_contacts(conn, None)
check("Dana's job email is queued and the general inquiry stops",
      [k for k in keys(conn) if k[0] == "dana@nojobs.com"],
      [("dana@nojobs.com", "N1", "active"), ("dana@nojobs.com", G, "superseded")])
q = outreach.build_queue(conn, {"followup_1_after_days": 0,
                                "followup_2_after_days": 0}, cap=25)
check("a superseded inquiry gets no follow-up",
      [r["job_id"] for r, _ in q if r["email"] == "dana@nojobs.com"], ["N1"])

print("\n--- a replied inquiry is left as it is ---")
outreach.DB_PATH = tmp / "g2.db"
conn2 = outreach.db()
write(tmp / "jobs.csv", JOBS_ACME_ONLY)
outreach.import_contacts(conn2, None)
conn2.execute("UPDATE contacts SET stage=1, status='replied' WHERE job_id=?", (G,))
conn2.commit()
write(tmp / "jobs.csv", JOBS_ACME_ONLY + "nojobs,N1,Backend Engineer,a note,,\n")
outreach.import_contacts(conn2, None)
check("replied stays replied; the job email still goes out",
      [k for k in keys(conn2) if k[0] == "dana@nojobs.com"],
      [("dana@nojobs.com", "N1", "active"), ("dana@nojobs.com", G, "replied")])

print("\n--- the opening is removed later: no general inquiry for her ---")
write(tmp / "jobs.csv", JOBS_ACME_ONLY)        # N1 deleted from jobs.csv
outreach.DB_PATH = tmp / "g3.db"
conn3 = outreach.db()
conn3.execute("INSERT INTO contacts (email, company, job_id, name, personal_note, "
              "stage, status) VALUES ('dana@nojobs.com', 'NoJobs Ltd', 'N1', "
              "'Dana Doe', 'a note', 3, 'done')")
conn3.commit()
outreach.import_contacts(conn3, None)
check("someone already mailed about an opening there gets no general inquiry",
      [k for k in keys(conn3) if k[0] == "dana@nojobs.com"],
      [("dana@nojobs.com", "N1", "done")])

print("\n--- suppression still applies to a general inquiry ---")
outreach.DB_PATH = tmp / "g4.db"
conn4 = outreach.db()
write(outreach.SUPPRESS_CSV, "dana@nojobs.com\n")
outreach.import_contacts(conn4, None)
check("suppressed address is not left active",
      conn4.execute("SELECT status FROM contacts WHERE job_id=?",
                    (G,)).fetchone()["status"], "suppressed")
outreach.SUPPRESS_CSV.unlink()

# ------------------------------------------------------------------ templates
print("\n--- pick_template / job_refs ---")
outreach.TEMPLATES = REAL_BASE / "templates"
check("step 1 -> general.txt",
      outreach.pick_template(1, "recruiter", general=True).name,
      "general.txt" if not (outreach.TEMPLATES / "general.recruiter.txt").is_file()
      else "general.recruiter.txt")
check("step 2 -> general_followup_1.txt",
      outreach.pick_template(2, "", general=True).name, "general_followup_1.txt")
check("step 3 -> the shared followup_2.txt",
      outreach.pick_template(3, "", general=True).name, "followup_2.txt")
check("job rows are unchanged", outreach.pick_template(1, "").name, "initial.txt")
check("job_refs: reserved id gives no body line and no subject ref",
      outreach.job_refs(G), ("", ""))
check("job_refs: a real req ID still works",
      outreach.job_refs("R1"), ("Req ID: R1", "R1"))

outreach.BASE = REAL_BASE
outreach.check_templates()              # sys.exit()s on failure
check("check_templates() passes with the real templates/ folder", True, True)

print("\n--- build_message for a general inquiry ---")
CFG = json.loads((REAL_BASE / "config.json").read_text(encoding="utf-8"))
row = {"email": "dana@nojobs.com", "name": "Dana Doe", "company": "NoJobs Ltd",
       "job_id": G, "role": "", "personal_note": "", "contact_type": "recruiter",
       "message_id": None, "subject": None}
msg = outreach.build_message(row, 1, CFG, "me@example.com")
body = msg.get_content()
check("subject comes from general_subject_template",
      msg["Subject"], outreach.render(CFG["general_subject_template"],
                                      {"company": "NoJobs Ltd"}))
check("body greets by first name", body.startswith("Hi Dana,"), True)
check("body names the company", "NoJobs Ltd" in body, True)
check("no unfilled placeholder", "{{" in body, False)
check("no Req ID / Posting line for the reserved id",
      ("Req ID" in body, "Posting:" in body), (False, False))

row.update(message_id="<first@example.com>", subject=msg["Subject"])
for step in (2, 3):
    m = outreach.build_message(row, step, CFG, "me@example.com")
    check(f"step {step}: replies under the same subject",
          m["Subject"], "Re: " + msg["Subject"])
    check(f"step {step}: threads on the first message",
          m["In-Reply-To"], "<first@example.com>")
    check(f"step {step}: no unfilled placeholder", "{{" in m.get_content(), False)

print(f"\n{'ALL PASSED' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
