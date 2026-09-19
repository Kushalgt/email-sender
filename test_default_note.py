#!/usr/bin/env python3
"""Offline tests for the personal_note fallback (templates/default_note.txt).

Covers:
  1. load_default_note(): off when the file is absent, hard-fails on an
     empty file or a disallowed placeholder, otherwise returns one line.
  2. import_contacts(): applies the fallback only when jobs.csv's own note
     is empty, and only touches contacts.personal_note for a stage-0
     (not yet mailed) row - a row that has already been sent is untouched.
  3. The real templates/default_note.txt shipped in this repo passes the
     same style rules notegen_core.validate() enforces on AI-drafted notes.

No network, no model, no writes outside a temp directory.

Run:  python3 test_default_note.py
"""

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


tmp = Path(tempfile.mkdtemp(prefix="default-note-test-"))
outreach.BASE = tmp
outreach.TEMPLATES = tmp / "templates"
outreach.TEMPLATES.mkdir()
outreach.DEFAULT_NOTE_PATH = outreach.TEMPLATES / "default_note.txt"

# ------------------------------------------------------------------ load_default_note
print("--- load_default_note ---")
check("file absent -> feature off", outreach.load_default_note(), None)

write(outreach.DEFAULT_NOTE_PATH, "   \n  \n")
check_raises("empty file -> hard stop", outreach.load_default_note)

write(outreach.DEFAULT_NOTE_PATH,
      "Hi {{first_name}}, about {{role}} at {{company}}.")
check_raises("disallowed placeholder ({{first_name}}) -> hard stop",
             outreach.load_default_note)

write(outreach.DEFAULT_NOTE_PATH, """
I am writing about the {{role}} opening at {{company}}.
It looks close to my current backend work.
""")
note = outreach.load_default_note()
check("valid file collapses to one line", "\n" in note, False)
check("placeholders survive collapsing",
      "{{role}}" in note and "{{company}}" in note, True)

# ------------------------------------------------------------------ render_default_note
print("\n--- render_default_note ---")
check("fills company and role",
      outreach.render_default_note(note, "Backend Engineer", "Acme Corp"),
      note.replace("{{role}}", "Backend Engineer").replace("{{company}}", "Acme Corp"))
check("blank job_title falls back to DEFAULT_ROLE",
      outreach.DEFAULT_ROLE in outreach.render_default_note(note, "", "Acme Corp"),
      True)

# ------------------------------------------------------------------ import_contacts
print("\n--- import_contacts with a default note ---")


def fresh_csvs():
    write(tmp / "companies.csv", """
slug,name,domain,email_pattern
acme,Acme Corp,acme.io,{first}.{last}
""")
    write(tmp / "jobs.csv", """
company_slug,job_id,job_title,personal_note,jd,jd_file
acme,J1,Backend Engineer,,,
acme,J2,Platform Engineer,a real hand-written note,,
""")
    write(tmp / "people.csv", """
email,name,company_slug,contact_type,email_source
ann@acme.io,Ann Ant,acme,engineer,manual
""")
    store.COMPANIES_CSV = tmp / "companies.csv"
    store.JOBS_CSV = tmp / "jobs.csv"
    store.PEOPLE_CSV = tmp / "people.csv"


fresh_csvs()
outreach.DB_PATH = tmp / "c1.db"
conn = outreach.db()
outreach.import_contacts(conn, note)

rows = {r["job_id"]: r for r in conn.execute("SELECT * FROM contacts")}
check("empty CSV note -> the rendered default is stored",
      "{{" in rows["J1"]["personal_note"], False)
check("default mentions the company",
      "Acme Corp" in rows["J1"]["personal_note"], True)
check("real CSV note wins over the default",
      rows["J2"]["personal_note"], "a real hand-written note")

print("\n--- import_contacts with NO default note (default=None) ---")
outreach.DB_PATH = tmp / "c2.db"
conn2 = outreach.db()
outreach.import_contacts(conn2, None)
rows2 = {r["job_id"]: r["personal_note"] for r in conn2.execute("SELECT * FROM contacts")}
check("no fallback configured -> empty-note opening is skipped, as before",
      "J1" in rows2, False)
check("real note is unaffected either way", rows2["J2"], "a real hand-written note")

print("\n--- refresh only touches stage-0 (not yet sent) rows ---")
outreach.DB_PATH = tmp / "c3.db"
conn3 = outreach.db()
outreach.import_contacts(conn3, note)  # J1 gets the default, stage 0

# Simulate J1 already sent, J... nothing else has stage>0 here.
conn3.execute("UPDATE contacts SET stage=1 WHERE job_id='J1'")
conn3.commit()

# Now jobs.csv gets a real, hand-written note for J1.
write(tmp / "jobs.csv", """
company_slug,job_id,job_title,personal_note,jd,jd_file
acme,J1,Backend Engineer,now I wrote a real note myself,,
acme,J2,Platform Engineer,a real hand-written note,,
""")
before = conn3.execute(
    "SELECT personal_note FROM contacts WHERE job_id='J1'").fetchone()["personal_note"]
outreach.import_contacts(conn3, note)
after = conn3.execute(
    "SELECT personal_note FROM contacts WHERE job_id='J1'").fetchone()["personal_note"]
check("a contact already at stage>0 keeps the note actually sent, unchanged",
      after, before)

print("\n--- refresh DOES apply to a stage-0 (unsent) row ---")
outreach.DB_PATH = tmp / "c4.db"
conn4 = outreach.db()
fresh_csvs()  # J1 empty again
outreach.import_contacts(conn4, note)  # J1 -> default, stage stays 0

write(tmp / "jobs.csv", """
company_slug,job_id,job_title,personal_note,jd,jd_file
acme,J1,Backend Engineer,now I wrote a real note myself,,
acme,J2,Platform Engineer,a real hand-written note,,
""")
outreach.import_contacts(conn4, note)
row4 = conn4.execute("SELECT * FROM contacts WHERE job_id='J1'").fetchone()
check("unsent contact's note is refreshed once a real note is written",
      row4["personal_note"], "now I wrote a real note myself")
check("stage is untouched by the refresh", row4["stage"], 0)

print("\n--- build_message renders a defaulted note without error ---")
CFG = {
    "resume_link": "https://example.com/resume",
    "from_name": "Kushal Gupta",
    "phone": "+91-0000000000",
    "linkedin": "https://linkedin.com/in/example",
    "subject_template": "{{company}} {{subject_ref}} - {{role}}",
    "subject_templates": {},
}
default_templates = Path(__file__).resolve().parent / "templates"
outreach.TEMPLATES = default_templates
row_for_msg = dict(rows["J1"])
row_for_msg.setdefault("message_id", None)
row_for_msg.setdefault("subject", None)
msg = outreach.build_message(row_for_msg, 1, CFG, "me@example.com")
check("rendered body contains the company name",
      "Acme Corp" in msg.get_content(), True)
check("no unfilled placeholder reaches the body",
      "{{" in msg.get_content(), False)

# ------------------------------------------------------------------ the shipped default
print("\n--- the shipped templates/default_note.txt ---")
import notegen_core as core

real_path = default_templates / "default_note.txt"
if real_path.is_file():
    real_text = " ".join(real_path.read_text(encoding="utf-8").split())
    filled = real_text.replace("{{role}}", "Backend Engineer").replace(
        "{{company}}", "Acme Corp")
    words = core.WORD.findall(filled)
    sentences = [s for s in core.SENT_SPLIT.split(filled) if s.strip()]
    check("shipped default: no leftover placeholder after fill-in",
          "{{" in filled, False)
    check("shipped default: at most 2 sentences", len(sentences) <= 2, True)
    check("shipped default: 18-55 words", 18 <= len(words) <= 55, True)
    check("shipped default: no greeting", bool(core.GREETINGS.match(filled)), False)
    check("shipped default: no sign-off", bool(core.SIGNOFFS.search(filled)), False)
    low = filled.lower()
    check("shipped default: no banned phrase",
          any(p in low for p in core.BANNED), False)
else:
    print("  [skip] templates/default_note.txt not found in the repo")

print(f"\n{'ALL PASSED' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
