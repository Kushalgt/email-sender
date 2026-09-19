# Job outreach mailer

Sends one-to-one job emails, tracks state in SQLite, follows up twice,
and **stops following up the moment someone replies**.

Python 3.9+. Standard library only — nothing to `pip install`.

---

## What it will not do

It will not send an email that still contains an unfilled `{{placeholder}}`
or `[Name]`. That is a hard abort, logged and skipped.

Every contact must have a `personal_note`. If `jobs.csv` leaves that cell
blank, `templates/default_note.txt` is used instead — see
[Default personal note](#default-personal-note-fallback) below. Only when
*both* are empty is the contact skipped.

That is deliberate. The personalisation line is the part of the email most
likely to earn a reply, so a hand-written one always wins, and the fallback
exists so a missing note holds up sending less often.

---

## Setup (once)

**1. App Password.** Turn on 2-Step Verification at
`myaccount.google.com/security`, then create an App Password. Google shows
16 characters with spaces; the script strips them, so paste either form.

> Google has been pushing hard toward OAuth-only. If App Passwords are gone
> by the time you read this, the SMTP/IMAP approach here won't work and you
> need the Gmail API with OAuth instead. Check before you build a habit on it.

**2. Environment variables.** Put these in `~/.zshrc`, `~/.bashrc`, or a
`.env` you source — never in a file you commit:

```bash
export GMAIL_ADDRESS="kushalguptadps@gmail.com"
export GMAIL_APP_PASSWORD="xxxxxxxxxxxxxxxx"
```

**3. Host your resume** somewhere with a stable link (Google Drive with
link-sharing on, or a GitHub Pages PDF) and put the URL in `config.json`
under `resume_link`. Linking beats attaching: attachments from unknown
senders get weighted as a spam signal and some corporate mail policies
quarantine them outright.

**4. Edit the templates.** `templates/initial.txt` currently has a generic
"Java, Spring Boot, Kafka" line. Replace it with **one concrete result and a
number** from your HiLabs work. That single edit will do more than the whole
script.

**5. Verify threading — do this before your first real batch.** Add your own
second email address to `people.csv`, run a real send, then open the
message and view the raw source. Check whether the `Message-ID` you see
matches what's in the DB:

```bash
sqlite3 outreach.db "SELECT email, message_id FROM contacts;"
```

If Gmail rewrote the `Message-ID` on the way out, follow-ups won't thread on
the recipient's side — they'll arrive as separate emails. Not fatal, but
worth knowing. I could not verify Gmail's current rewriting behaviour, so
please check it yourself rather than assume.

---

## Daily use

```bash
python3 outreach.py send --dry-run   # ALWAYS run this first
python3 outreach.py send             # the real thing
python3 outreach.py stats            # reply rate, funnel
python3 outreach.py sync             # just check inbox, send nothing
python3 outreach.py suppress a@b.com # permanent do-not-contact
touch PAUSE                          # kill switch; delete the file to resume
```

Workflow, in the order that costs you least typing:

1. **New opening** → one row in `jobs.csv` (`company_slug`, `job_id`,
   `job_title`), and paste the job description into its `jd` cell.
   A new company also needs one row in `companies.csv`.
2. **Write the note once per opening** — by hand, or with `notegen.py`.
   It is shared by everyone you contact at that company, so you write it
   once instead of once per person. Leaving it blank is fine too — see
   below.
3. **New people** → one short row each in `people.csv`: name,
   `company_slug`, and `contact_type`. Leave `email` blank and let
   `python3 emailmap.py predict --apply` fill it in from the company's
   pattern.

The scheduler picks them up next morning. Re-importing is safe — existing
rows are never overwritten or re-sent.

### Default personal note (fallback)

`jobs.csv`'s `personal_note` cell can be left blank. When it is,
`templates/default_note.txt` is rendered and used instead, so the opening
is not stuck unable to send just because you have not written its note yet.

- The file may only use `{{company}}` and `{{role}}` — nothing
  person-specific, since one note is shared by every contact at that
  company, same as a hand-written one.
- **Delete the file to turn this off.** With no `templates/default_note.txt`,
  an opening with a blank `personal_note` is skipped, exactly as before this
  feature existed.
- A hand-written note in `jobs.csv` always wins over the default. If you add
  one later, any contact **not yet emailed** for that opening picks it up on
  the next `send` — someone already emailed keeps the note they actually
  received.
- Every send that used the fallback is logged as `[default-note]`, and a
  refreshed one as `[refresh-note]`, so `python3 outreach.py send --dry-run`
  shows you exactly which emails used it before anything goes out.
- `notegen.py`/`notegen_hosted.py` still treat that opening as "without a
  note" and can draft a real one for it — the default only fills the gap
  until you do.

Every person at a company is contacted about every opening at that company;
that join is computed at load time, so there is nothing to maintain. A person
receives at most one message per day, so someone who is a contact for two
openings hears about the second one on the following run.

### The three files

| File | Holds | Key |
|---|---|---|
| `companies.csv` | display name, mail domain, email pattern | `slug` |
| `jobs.csv` | one row per opening: title, `personal_note`, `jd` | `(company_slug, job_id)` |
| `people.csv` | one row per contact: name, `contact_type` | `email` |

`slug` is the join key and never reaches the database — `outreach.db` stores
the display name from `companies.csv`, because its primary key is
`(email, company, job_id)` and a changed company string would re-send to
someone already contacted.

`contact_type` is one of `engineer`, `engineering_manager`, `hiring_manager`,
`recruiter`, or blank. It picks the template: `templates/initial.<type>.txt`
if that file exists, otherwise `templates/initial.txt`. All four variants
exist, so a blank only ever falls back to the generic `initial.txt`.

Each type is written for the question that person is actually asking. The
peer-engineer mail asks for a referral outright, because at most companies
the referral programme pays them and they are the best route in. The two
manager mails deliberately do **not** use the word "referral" — a manager
can simply interview you, and asking them to refer you to their own req
reads as a misunderstanding of their role; they are asked for a short
conversation or a pointer to the right team instead.

Subject lines come from `subject_templates` in `config.json`, keyed by the
same `contact_type`, and fall back to the shared `subject_template` the same
way the body templates do. The recruiter subject keeps the terse
`company + id + role` form on purpose: it is the most useful thing for
someone routing a req into an ATS, and only reads as automated to the other
three.

### `job_id` holds an ID *or* a link

Job postings give out either a req ID (`200677836`) or just a URL, so
`job_id` accepts whichever you have, and some rows have neither.
`job_refs()` in `outreach.py` turns that one column into two values the
templates use:

- `{{job_ref}}` — a whole self-labelling body line: `Req ID: 200677836`, or
  `Posting: https://…`, or nothing at all. It never prints a label with an
  empty value, and `build_message()` closes up the blank line it leaves.
- `{{subject_ref}}` — the ID only. A pasted URL is dropped here, because a
  60-character link in a subject line is unreadable.

Templates must use these, never `{{job_id}}` directly.

Long lines are re-flowed to 72 columns by `wrap_body()` after substitution,
since `{{personal_note}}` and `{{role}}` expand well past the hand-wrapped
margin. Paragraphs holding a URL and indented paragraphs are left untouched,
which is what keeps the signature block on three lines and the recruiter
mail's aligned columns aligned.

---

## Scheduling on a laptop

**Plain `cron` is the wrong tool here.** If the lid is shut at 10:00, cron
skips the run and never catches up. Use a scheduler that reruns missed jobs,
and rely on the built-in send-window guard (09:30–12:30) to prevent a
catch-up run from firing at 4 PM.

### macOS — launchd

`~/Library/LaunchAgents/com.kushal.outreach.plist`:

```xml
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.kushal.outreach</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>/Users/YOU/outreach/outreach.py</string>
    <string>send</string>
  </array>
  <key>WorkingDirectory</key><string>/Users/YOU/outreach</string>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>10</integer><key>Minute</key><integer>0</integer></dict>
  <key>StandardOutPath</key><string>/Users/YOU/outreach/run.log</string>
  <key>StandardErrorPath</key><string>/Users/YOU/outreach/run.log</string>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.kushal.outreach.plist
```

launchd runs a missed `StartCalendarInterval` job on wake. **Caveat:** GUI
launchd agents don't inherit your shell's exported variables, so add an
`EnvironmentVariables` dict to the plist, or read the credentials from a
file the script sources. Verify with `launchctl print gui/$(id -u)/com.kushal.outreach`.

### Linux — systemd user timer

`~/.config/systemd/user/outreach.service`:

```ini
[Service]
Type=oneshot
WorkingDirectory=%h/outreach
EnvironmentFile=%h/outreach/.env
ExecStart=/usr/bin/python3 %h/outreach/outreach.py send
```

`~/.config/systemd/user/outreach.timer`:

```ini
[Unit]
Description=Daily job outreach
[Timer]
OnCalendar=Mon..Fri 10:00
Persistent=true
[Install]
WantedBy=timers.target
```

```bash
systemctl --user enable --now outreach.timer
loginctl enable-linger $USER   # so it runs without an active session
```

`Persistent=true` is the part that matters — it triggers a missed run on boot/wake.

### Windows — Task Scheduler

Create a Basic Task, daily at 10:00, action `python.exe C:\...\outreach.py send`,
"Start in" set to the project folder. Then in the task's **Settings** tab tick
**"Run task as soon as possible after a scheduled start is missed."**
Set the credentials as *user* environment variables, not session ones.

---

## Guardrails (all on by default)

| Guardrail | Config key | Default |
|---|---|---|
| Daily cap | `daily_cap` | 25 |
| Random gap between sends | `min/max_gap_seconds` | 45–180s |
| Send window | `send_window` | 09:30–12:30 |
| Weekend skip | `skip_weekends` | true |
| Kill switch | — | `touch PAUSE` |
| Dedupe | — | SQLite PK on (email, company, job_id) |
| One message per person per day | — | always on |
| Unfilled-placeholder abort | — | always on |
| Empty personal_note refusal (no default note file) | — | always on |
| Default personal note | `templates/default_note.txt` | on if the file exists |

Gmail's own reported ceiling is ~500 recipients/day on a personal account,
so the cap of 25 is not about quota — it's about not looking like a bot and
not burning the mailbox you need for interview scheduling.

---

## Reading the stats

`python3 outreach.py stats` gives you a reply rate. Use it as a decision gate:

- **0% after ~40 sends** → the problem is the template or the resume, not
  the volume. Stop sending and rewrite.
- **5–10%** → fundamentals are fine, you can raise `daily_cap`.
- **15%+** → working well; spend the extra time on targeting instead.

Change one thing at a time (subject line, or opening sentence, or target
list) so you can tell what moved the number.
