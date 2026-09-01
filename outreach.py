#!/usr/bin/env python3
"""
Job-outreach mailer: sends one-to-one emails, tracks state, follows up,
and stops following up when someone replies.

Design rule: this tool removes mechanical work. It does NOT write your
personalisation for you. Every contact must have a hand-written
personal_note or the script refuses to send.

Commands:
    python3 outreach.py send          # the real thing (used by the scheduler)
    python3 outreach.py send --dry-run
    python3 outreach.py sync          # only check inbox for replies/bounces
    python3 outreach.py stats
    python3 outreach.py suppress a@b.com

Secrets come from the environment, never from a file in git:
    export GMAIL_ADDRESS="you@gmail.com"
    export GMAIL_APP_PASSWORD="xxxxxxxxxxxxxxxx"
"""

import argparse
import csv
import datetime as dt
import email.utils
import imaplib
import json
import os
import random
import re
import smtplib
import sqlite3
import ssl
import sys
import time
from email.message import EmailMessage
from pathlib import Path

BASE = Path(__file__).resolve().parent
DB_PATH = BASE / "outreach.db"
CONFIG_PATH = BASE / "config.json"
CONTACTS_CSV = BASE / "contacts.csv"
SUPPRESS_CSV = BASE / "suppress.csv"
PAUSE_FILE = BASE / "PAUSE"
TEMPLATES = BASE / "templates"

# Catches {{placeholder}} and legacy [Placeholder] that never got filled in.
UNFILLED = re.compile(r"\{\{[^}]*\}\}|\[[A-Za-z][A-Za-z _/-]{1,28}\]")
PLACEHOLDER = re.compile(r"\{\{(\w+)\}\}")
EMAIL_OK = re.compile(r"^[^@\s]+@[^@\s]+\.[A-Za-z]{2,}$")

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts (
    email         TEXT NOT NULL,
    company       TEXT NOT NULL,
    job_id        TEXT NOT NULL DEFAULT '',
    name          TEXT NOT NULL,
    role          TEXT NOT NULL DEFAULT '',
    personal_note TEXT NOT NULL DEFAULT '',
    stage         INTEGER NOT NULL DEFAULT 0,
    status        TEXT NOT NULL DEFAULT 'active',
    first_sent_at TEXT,
    last_sent_at  TEXT,
    message_id    TEXT,
    subject       TEXT,
    PRIMARY KEY (email, company, job_id)
);
CREATE TABLE IF NOT EXISTS log (
    ts     TEXT NOT NULL,
    email  TEXT NOT NULL,
    action TEXT NOT NULL,
    detail TEXT NOT NULL DEFAULT ''
);
"""


# ---------------------------------------------------------------- infra

def db():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def log(conn, addr, action, detail=""):
    conn.execute(
        "INSERT INTO log (ts, email, action, detail) VALUES (?,?,?,?)",
        (dt.datetime.now().isoformat(timespec="seconds"), addr, action, detail),
    )
    conn.commit()
    print(f"  [{action}] {addr} {detail}".rstrip())


def load_config():
    with open(CONFIG_PATH) as f:
        return json.load(f)


def creds():
    addr = os.environ.get("GMAIL_ADDRESS")
    pw = os.environ.get("GMAIL_APP_PASSWORD")
    if not addr or not pw:
        sys.exit("ERROR: set GMAIL_ADDRESS and GMAIL_APP_PASSWORD in your environment.")
    return addr, pw.replace(" ", "")  # Google displays the 16 chars with spaces


# ---------------------------------------------------------------- guardrails

def preflight(cfg, ignore_window):
    """Return None if it is safe to send, else a reason string."""
    if PAUSE_FILE.exists():
        return f"{PAUSE_FILE.name} file exists - delete it to resume"

    now = dt.datetime.now()
    if cfg["skip_weekends"] and now.weekday() >= 5:
        return "weekend"

    if not ignore_window:
        start = dt.time.fromisoformat(cfg["send_window"][0])
        end = dt.time.fromisoformat(cfg["send_window"][1])
        if not (start <= now.time() <= end):
            return (f"outside send window {cfg['send_window'][0]}-"
                    f"{cfg['send_window'][1]} (now {now.strftime('%H:%M')})")
    return None


def render(template, fields):
    """Substitute {{field}} then hard-fail if anything is left unfilled."""
    out = PLACEHOLDER.sub(lambda m: str(fields.get(m.group(1), m.group(0))), template)
    leftover = UNFILLED.findall(out)
    if leftover:
        raise ValueError(f"unfilled placeholder(s): {sorted(set(leftover))}")
    return out


# ---------------------------------------------------------------- contacts

def import_contacts(conn):
    """Upsert rows from contacts.csv. Existing rows are never overwritten,
    so re-running is safe and you can keep appending to the CSV."""
    if not CONTACTS_CSV.exists():
        sys.exit(f"ERROR: {CONTACTS_CSV} not found.")

    added = skipped = 0
    with open(CONTACTS_CSV, newline="", encoding="utf-8") as f:
        for row in csv.DictReader(f):
            addr = (row.get("email") or "").strip().lower()
            note = (row.get("personal_note") or "").strip()
            company = (row.get("company") or "").strip()
            name = (row.get("name") or "").strip()

            if not addr:
                continue
            if not EMAIL_OK.match(addr):
                print(f"  [skip] bad email syntax: {addr}")
                skipped += 1
                continue
            if not note:
                print(f"  [skip] {addr}: personal_note is empty - write one line first")
                skipped += 1
                continue
            if not company or not name:
                print(f"  [skip] {addr}: name and company are both required")
                skipped += 1
                continue

            cur = conn.execute(
                """INSERT OR IGNORE INTO contacts
                   (email, company, job_id, name, role, personal_note)
                   VALUES (?,?,?,?,?,?)""",
                (addr, company, (row.get("job_id") or "").strip(),
                 name, (row.get("role") or "").strip(), note),
            )
            added += cur.rowcount

    conn.commit()
    load_suppression(conn)
    print(f"  imported {added} new, skipped {skipped}")


def load_suppression(conn):
    if not SUPPRESS_CSV.exists():
        return
    with open(SUPPRESS_CSV, newline="", encoding="utf-8") as f:
        for row in csv.reader(f):
            if row and row[0].strip() and not row[0].startswith("#"):
                conn.execute(
                    "UPDATE contacts SET status='suppressed' WHERE email=?",
                    (row[0].strip().lower(),),
                )
    conn.commit()


# ---------------------------------------------------------------- imap sync

def imap_sync(conn, cfg):
    """Mark contacts as replied or bounced by reading the inbox.

    Reply rule:  any inbound mail from that address after we sent = replied.
    Bounce rule: a mailer-daemon message whose body contains the address.
                 This is a heuristic, not a guarantee.
    """
    rows = conn.execute(
        "SELECT * FROM contacts WHERE status='active' AND stage > 0"
    ).fetchall()
    if not rows:
        return

    addr, pw = creds()
    try:
        imap = imaplib.IMAP4_SSL(cfg["imap_host"], cfg["imap_port"])
        imap.login(addr, pw)
        imap.select("INBOX")
    except Exception as e:
        print(f"  [warn] IMAP unavailable, skipping reply check: {e}")
        return

    try:
        for r in rows:
            since = dt.datetime.fromisoformat(r["first_sent_at"])
            since_str = since.strftime("%d-%b-%Y")

            typ, data = imap.search(None, "FROM", f'"{r["email"]}"',
                                    "SINCE", since_str)
            if typ == "OK" and data and data[0].split():
                conn.execute(
                    "UPDATE contacts SET status='replied' WHERE email=? AND company=? AND job_id=?",
                    (r["email"], r["company"], r["job_id"]),
                )
                log(conn, r["email"], "replied", "follow-ups cancelled")
                continue

            typ, data = imap.search(None, "FROM", '"mailer-daemon"',
                                    "SINCE", since_str)
            if typ != "OK" or not data or not data[0].split():
                continue
            for num in data[0].split()[-20:]:  # only recent daemon mail
                typ, msg = imap.fetch(num, "(BODY.PEEK[])")
                if typ != "OK" or not msg or not isinstance(msg[0], tuple):
                    continue
                if r["email"].encode() in msg[0][1].lower():
                    conn.execute(
                        "UPDATE contacts SET status='bounced' WHERE email=? AND company=? AND job_id=?",
                        (r["email"], r["company"], r["job_id"]),
                    )
                    log(conn, r["email"], "bounced", "address looks dead")
                    break
    finally:
        try:
            imap.logout()
        except Exception:
            pass


# ---------------------------------------------------------------- queue

def build_queue(conn, cfg, cap):
    """Follow-ups first (they are warmer), then new contacts, up to cap."""
    today = dt.date.today()
    queue = []

    for r in conn.execute(
        "SELECT * FROM contacts WHERE status='active' AND stage IN (1,2) "
        "ORDER BY last_sent_at"
    ):
        gap = cfg["followup_1_after_days"] if r["stage"] == 1 else cfg["followup_2_after_days"]
        due = dt.datetime.fromisoformat(r["last_sent_at"]).date() + dt.timedelta(days=gap)
        if due <= today:
            queue.append((r, r["stage"] + 1))

    for r in conn.execute(
        "SELECT * FROM contacts WHERE status='active' AND stage=0 ORDER BY rowid"
    ):
        queue.append((r, 1))

    return queue[:cap]


def build_message(row, step, cfg, from_addr):
    tmpl_name = {1: "initial", 2: "followup_1", 3: "followup_2"}[step]
    body_tmpl = (TEMPLATES / f"{tmpl_name}.txt").read_text(encoding="utf-8")

    fields = {
        "name": row["name"],
        "first_name": row["name"].split()[0],
        "company": row["company"],
        "role": row["role"] or "Backend Software Engineer",
        "job_id": row["job_id"] or "",
        "personal_note": row["personal_note"],
        "resume_link": cfg["resume_link"],
        "from_name": cfg["from_name"],
        "phone": cfg["phone"],
        "linkedin": cfg["linkedin"],
        "email": from_addr,
    }

    body = render(body_tmpl, fields)
    subject = (row["subject"] if step > 1 and row["subject"]
               else render(cfg["subject_template"], fields))
    subject = " ".join(subject.split())  # empty job_id leaves a dangling space

    msg = EmailMessage()
    msg["Subject"] = subject if step == 1 else f"Re: {subject}"
    msg["From"] = email.utils.formataddr((cfg["from_name"], from_addr))
    msg["To"] = email.utils.formataddr((row["name"], row["email"]))
    msg["Message-ID"] = email.utils.make_msgid(domain=from_addr.split("@")[1])
    msg["Date"] = email.utils.formatdate(localtime=True)
    if step > 1 and row["message_id"]:
        msg["In-Reply-To"] = row["message_id"]
        msg["References"] = row["message_id"]
    msg.set_content(body)  # plain text only, on purpose
    return msg


# ---------------------------------------------------------------- send

def check_templates():
    """Fail early and clearly if the templates/ folder is missing or incomplete."""
    required = ["initial.txt", "followup_1.txt", "followup_2.txt"]
    if not TEMPLATES.is_dir():
        stray = [p.name for p in BASE.glob("*.txt")]
        msg = f"ERROR: no 'templates' folder next to outreach.py\n  looked in: {TEMPLATES}"
        if stray:
            msg += ("\n  but found loose .txt files here: " + ", ".join(sorted(stray)) +
                    "\n  -> they belong inside templates/ (see README)")
        sys.exit(msg)

    missing = [n for n in required if not (TEMPLATES / n).is_file()]
    if missing:
        have = sorted(p.name for p in TEMPLATES.glob("*.txt"))
        sys.exit(f"ERROR: templates/ is missing: {', '.join(missing)}\n"
                 f"  templates/ currently has: {', '.join(have) or '(nothing)'}\n"
                 f"  note the underscores - 'followup 1.txt' is not 'followup_1.txt'")


def cmd_send(args):
    cfg = load_config()
    check_templates()
    conn = db()

    reason = preflight(cfg, args.ignore_window)
    if reason and not args.dry_run:
        print(f"Not sending: {reason}")
        return

    print("Importing contacts...")
    import_contacts(conn)

    if not args.dry_run:
        print("Checking inbox for replies and bounces...")
        imap_sync(conn, cfg)

    cap = args.limit or cfg["daily_cap"]
    queue = build_queue(conn, cfg, cap)
    if not queue:
        print("Nothing due today.")
        return

    from_addr, pw = creds() if not args.dry_run else (cfg["from_email"], None)
    print(f"\n{len(queue)} message(s) queued (cap {cap})"
          f"{'  [DRY RUN]' if args.dry_run else ''}\n")

    sent = 0
    for i, (row, step) in enumerate(queue):
        try:
            msg = build_message(row, step, cfg, from_addr)
        except ValueError as e:
            log(conn, row["email"], "ABORTED", str(e))
            continue

        if args.dry_run:
            print("=" * 68)
            print(f"step {step}  ->  {msg['To']}")
            print(f"Subject: {msg['Subject']}")
            print("-" * 68)
            print(msg.get_content())
            continue

        try:
            with smtplib.SMTP(cfg["smtp_host"], cfg["smtp_port"], timeout=30) as s:
                s.starttls(context=ssl.create_default_context())
                s.login(from_addr, pw)
                s.send_message(msg)
        except Exception as e:
            log(conn, row["email"], "SEND-FAILED", repr(e))
            continue

        now = dt.datetime.now().isoformat(timespec="seconds")
        conn.execute(
            """UPDATE contacts SET stage=?, last_sent_at=?,
               first_sent_at=COALESCE(first_sent_at, ?),
               message_id=COALESCE(message_id, ?),
               subject=COALESCE(subject, ?),
               status=CASE WHEN ?=3 THEN 'done' ELSE status END
               WHERE email=? AND company=? AND job_id=?""",
            (step, now, now, msg["Message-ID"],
             msg["Subject"].removeprefix("Re: "), step,
             row["email"], row["company"], row["job_id"]),
        )
        log(conn, row["email"], f"sent-step-{step}", row["company"])
        sent += 1

        if i < len(queue) - 1:
            time.sleep(random.uniform(cfg["min_gap_seconds"], cfg["max_gap_seconds"]))

    print(f"\nDone. {sent} sent.")


def cmd_sync(args):
    conn = db()
    load_suppression(conn)
    imap_sync(conn, load_config())
    print("Sync complete.")


def cmd_stats(args):
    conn = db()
    print("\nBy status:")
    for r in conn.execute(
        "SELECT status, COUNT(*) n FROM contacts GROUP BY status ORDER BY n DESC"
    ):
        print(f"  {r['status']:<12} {r['n']}")

    print("\nBy stage:")
    for r in conn.execute(
        "SELECT stage, COUNT(*) n FROM contacts GROUP BY stage ORDER BY stage"
    ):
        label = {0: "not contacted", 1: "initial sent",
                 2: "follow-up 1", 3: "follow-up 2"}[r["stage"]]
        print(f"  {label:<15} {r['n']}")

    total = conn.execute("SELECT COUNT(*) n FROM contacts WHERE stage>0").fetchone()["n"]
    replied = conn.execute("SELECT COUNT(*) n FROM contacts WHERE status='replied'").fetchone()["n"]
    if total:
        print(f"\nReply rate: {replied}/{total} = {100*replied/total:.1f}%")
        print("  (<5% after ~40 sends means fix the template, not the volume)")
    print()


def cmd_suppress(args):
    conn = db()
    addr = args.email.strip().lower()
    conn.execute("UPDATE contacts SET status='suppressed' WHERE email=?", (addr,))
    conn.commit()
    with open(SUPPRESS_CSV, "a", encoding="utf-8") as f:
        f.write(addr + "\n")
    print(f"Suppressed {addr} permanently.")


def main():
    p = argparse.ArgumentParser(description="Job outreach mailer")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("send")
    s.add_argument("--dry-run", action="store_true", help="render only, send nothing")
    s.add_argument("--limit", type=int, help="override daily cap")
    s.add_argument("--ignore-window", action="store_true", help="bypass send-window check")
    s.set_defaults(func=cmd_send)

    sub.add_parser("sync").set_defaults(func=cmd_sync)
    sub.add_parser("stats").set_defaults(func=cmd_stats)

    x = sub.add_parser("suppress")
    x.add_argument("email")
    x.set_defaults(func=cmd_suppress)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()