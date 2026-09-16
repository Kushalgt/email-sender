#!/usr/bin/env python3
"""Offline tests for the normalised store and the send queue.

Covers the two behaviours the split introduced:
  1. load_contacts() cross-joins people x their company's openings.
  2. build_queue() sends a person at most one message per day, even when
     they are a contact for several openings.

No network, no model, no writes outside a temp directory.

Run:  python3 test_store.py
"""

import datetime as dt
import sqlite3
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import store
import outreach

fails = 0


def check(label, got, want):
    global fails
    ok = got == want
    fails += 0 if ok else 1
    print(f"[{'ok  ' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"         got  {got!r}\n         want {want!r}")


def write(path, text):
    path.write_text(text.lstrip("\n"), encoding="utf-8")


tmp = Path(tempfile.mkdtemp(prefix="store-test-"))

# ------------------------------------------------------------------ cross join
write(tmp / "companies.csv", """
slug,name,domain,email_pattern
acme,Acme Corp,acme.io,{first}.{last}
solo,Solo Inc,solo.com,
nojobs,NoJobs Ltd,nojobs.com,
""")
write(tmp / "jobs.csv", """
company_slug,job_id,job_title,personal_note,jd,jd_file
acme,J1,Backend Engineer,note one,inline jd text,
acme,J2,Platform Engineer,note two,,jds/acme2.txt
solo,,Solo Engineer,,,
nopeople,X,Ghost,,,
""".replace("nopeople,X,Ghost,,,\n", ""))     # keep the file valid
write(tmp / "people.csv", """
email,name,company_slug,contact_type,email_source
A@Acme.IO,Ann Ant,acme,engineer,manual
bob@acme.io,Bob Bee,acme,,manual
carl@solo.com,Carl Cat,solo,recruiter,manual
dana@nojobs.com,Dana Doe,nojobs,,manual
""")

store.COMPANIES_CSV = tmp / "companies.csv"
store.JOBS_CSV = tmp / "jobs.csv"
store.PEOPLE_CSV = tmp / "people.csv"

rows = store.load_contacts()

print("--- cross join ---")
# Ann and Bob each get both Acme openings; Carl gets Solo's one; Dana's
# company has no openings so she produces nothing.
check("pair count (2 people x 2 jobs + 1 x 1 + 1 x 0)", len(rows), 5)
check("Dana's company has no openings, so no pairs",
      [r for r in rows if r["email"] == "dana@nojobs.com"], [])
check("Ann is paired with both Acme openings",
      sorted(r["job_id"] for r in rows if r["email"] == "a@acme.io"), ["J1", "J2"])
check("email is lower-cased on load",
      sorted({r["email"] for r in rows}),
      ["a@acme.io", "bob@acme.io", "carl@solo.com"])

ann_j2 = next(r for r in rows if r["email"] == "a@acme.io" and r["job_id"] == "J2")
check("company is the DISPLAY name, never the slug", ann_j2["company"], "Acme Corp")
check("slug is still available for joins", ann_j2["company_slug"], "acme")
check("role mirrors job_title", (ann_j2["role"], ann_j2["job_title"]),
      ("Platform Engineer", "Platform Engineer"))
check("note comes from the job, not the person", ann_j2["personal_note"], "note two")
check("contact_type comes from the person", ann_j2["contact_type"], "engineer")
check("blank job_id is allowed",
      next(r for r in rows if r["email"] == "carl@solo.com")["job_id"], "")

print("\n--- atomic write keeps the file readable ---")
before = len(store.load_people())
store.write_people(store.load_people())
check("round-trips without losing rows", len(store.load_people()), before)
check("timestamped backup was made, not a shared .bak",
      len(list(tmp.glob("people.*.csv.bak"))), 1)
check("no temp file left behind", list(tmp.glob("*.tmp")), [])

# ------------------------------------------------------------------ queue guard
print("\n--- build_queue: one message per person per day ---")
db_path = tmp / "q.db"
outreach.DB_PATH = db_path
conn = outreach.db()

CFG = {"followup_1_after_days": 6, "followup_2_after_days": 8}


def reset(rows):
    conn.execute("DELETE FROM contacts")
    for r in rows:
        conn.execute(
            "INSERT INTO contacts (email, company, job_id, name, role, "
            "personal_note, stage, status, first_sent_at, last_sent_at) "
            "VALUES (?,?,?,?,?,?,?,?,?,?)", r)
    conn.commit()


# One person, two openings at the same company, neither contacted yet.
reset([
    ("ann@acme.io", "Acme Corp", "J1", "Ann Ant", "Backend", "n", 0, "active", None, None),
    ("ann@acme.io", "Acme Corp", "J2", "Ann Ant", "Platform", "n", 0, "active", None, None),
    ("bob@acme.io", "Acme Corp", "J1", "Bob Bee", "Backend", "n", 0, "active", None, None),
])
q = outreach.build_queue(conn, CFG, cap=25)
check("2 openings + 1 other person -> 2 sends, not 3", len(q), 2)
check("one send per distinct person",
      sorted(r["email"] for r, _ in q), ["ann@acme.io", "bob@acme.io"])

# Same day, second run: Ann was already mailed, so her other opening waits.
today = dt.datetime.now().isoformat(timespec="seconds")
reset([
    ("ann@acme.io", "Acme Corp", "J1", "Ann Ant", "Backend", "n", 1, "active", today, today),
    ("ann@acme.io", "Acme Corp", "J2", "Ann Ant", "Platform", "n", 0, "active", None, None),
])
q = outreach.build_queue(conn, CFG, cap=25)
check("re-running send the same day holds the second opening", len(q), 0)

# Yesterday's send does not block today's second opening.
yday = (dt.datetime.now() - dt.timedelta(days=1)).isoformat(timespec="seconds")
reset([
    ("ann@acme.io", "Acme Corp", "J1", "Ann Ant", "Backend", "n", 1, "active", yday, yday),
    ("ann@acme.io", "Acme Corp", "J2", "Ann Ant", "Platform", "n", 0, "active", None, None),
])
q = outreach.build_queue(conn, CFG, cap=25)
check("the held opening goes out on the next day", len(q), 1)
check("and it is the un-sent opening", q[0][0]["job_id"], "J2")

# A due follow-up must beat a fresh cold email to the same person.
old = (dt.datetime.now() - dt.timedelta(days=10)).isoformat(timespec="seconds")
reset([
    ("ann@acme.io", "Acme Corp", "J1", "Ann Ant", "Backend", "n", 1, "active", old, old),
    ("ann@acme.io", "Acme Corp", "J2", "Ann Ant", "Platform", "n", 0, "active", None, None),
])
q = outreach.build_queue(conn, CFG, cap=25)
check("warm follow-up wins over a second cold email", len(q), 1)
check("and it is the follow-up (step 2 on J1)",
      (q[0][0]["job_id"], q[0][1]), ("J1", 2))

# ------------------------------------------------------------------ templates
print("\n--- pick_template ---")
check("no contact_type -> base template",
      outreach.pick_template(1, "").name, "initial.txt")
check("unknown variant falls back to base",
      outreach.pick_template(1, "recruiter").name
      if not (outreach.TEMPLATES / "initial.recruiter.txt").is_file() else "skip",
      "initial.txt" if not (outreach.TEMPLATES / "initial.recruiter.txt").is_file() else "skip")
check("engineer variant is picked up when the file exists",
      outreach.pick_template(1, "engineer").name,
      "initial.engineer.txt" if (outreach.TEMPLATES / "initial.engineer.txt").is_file()
      else "initial.txt")
check("follow-ups use their own base", outreach.pick_template(2, "").name,
      "followup_1.txt")

# ------------------------------------------------------------------ jd source
# Two openings at one company can now carry two different JDs. Before the
# split, find_jd() could only guess one file per company by slugging its name.
print("\n--- find_jd source priority ---")
import notegen_core as core

jd_file = tmp / "written_jd.txt"
jd_file.write_text("JD from a file", encoding="utf-8")

check("inline 'jd' column wins, no file needed",
      core.find_jd({"jd": "JD pasted into the cell", "jd_file": str(jd_file),
                    "company": "Acme Corp"}),
      ("JD pasted into the cell", "csv:jd"))
check("'jd_file' is used when 'jd' is empty",
      core.find_jd({"jd": "", "jd_file": str(jd_file), "company": "Acme Corp"}),
      ("JD from a file", jd_file.name))
check("a named file that is missing is reported, not silently skipped",
      core.find_jd({"jd": "", "jd_file": "/nope/missing.txt",
                    "company": "Acme Corp"})[1].startswith("MISSING FILE"),
      True)
check("neither set, and no jds/ match -> nothing found",
      core.find_jd({"jd": "", "jd_file": "", "company": "Totally Unknown Co"}),
      ("", ""))

# The real case: Ascendion's two openings, one pasted, one from a file.
two = [{"jd": "the 50341085 description", "jd_file": "", "company": "Ascendion"},
       {"jd": "", "jd_file": str(jd_file), "company": "Ascendion"}]
check("two openings at one company resolve to two different JDs",
      [core.find_jd(j)[0] for j in two],
      ["the 50341085 description", "JD from a file"])

# ------------------------------------------------------------------ apply
# The write path: approved notes -> jobs.csv, joined on (company_slug, job_id).
# No model needed; the note is inserted into the cache by hand.
print("\n--- cmd_apply writes notes into jobs.csv ---")
core.DB_PATH = tmp / "notes.db"
nconn = core.db()


class Args:
    force = False


def add_note(slug, job_id, note, status="approved", key=None):
    nconn.execute(
        "INSERT OR REPLACE INTO notes (cache_key,email,company,company_slug,"
        "job_id,note,status,attempts,reasons,model,used_facts,created_at) "
        "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
        (key or f"k-{slug}-{job_id}", "", slug.title(), slug, job_id, note,
         status, 1, "", "test", "a1", "2026-01-01T00:00:00"))
    nconn.commit()


add_note("acme", "J2", "the J2 note")          # J2 has a note already
add_note("solo", "", "the solo note")          # blank job_id must still join
core.cmd_apply(Args())

jobs_now = {(j["company_slug"], j["job_id"]): j for j in store.load_jobs()}
check("a blank job_id still joins correctly",
      jobs_now[("solo", "")]["personal_note"], "the solo note")
check("an opening that already had a note is kept, not overwritten",
      jobs_now[("acme", "J2")]["personal_note"], "note two")
check("the kept note's cache row stays 'approved' for a later --force",
      nconn.execute("SELECT status FROM notes WHERE company_slug='acme'"
                    ).fetchone()["status"], "approved")
check("the written note's cache row flips to 'applied'",
      nconn.execute("SELECT status FROM notes WHERE company_slug='solo'"
                    ).fetchone()["status"], "applied")

# Two approved notes for ONE opening (two models) -> newest wins, loudly.
add_note("acme", "J1", "older note, qwen", key="k-old")
nconn.execute("UPDATE notes SET created_at='2026-01-01T00:00:00', "
              "model='qwen2.5:7b' WHERE cache_key='k-old'")
add_note("acme", "J1", "newer note, gpt", key="k-new")
nconn.execute("UPDATE notes SET created_at='2026-06-01T00:00:00', "
              "model='openrouter:gpt-4o-mini' WHERE cache_key='k-new'")
nconn.commit()
Args.force = True
core.cmd_apply(Args())
jobs_now = {(j["company_slug"], j["job_id"]): j for j in store.load_jobs()}
check("two approved notes for one opening -> the NEWEST is used",
      jobs_now[("acme", "J1")]["personal_note"], "newer note, gpt")

Args.force = True
core.cmd_apply(Args())
jobs_now = {(j["company_slug"], j["job_id"]): j for j in store.load_jobs()}
check("--force does overwrite an existing note",
      jobs_now[("acme", "J2")]["personal_note"], "the J2 note")

print(f"\n{'ALL PASSED' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
