# Job outreach mailer

Sends one-to-one job emails, tracks state in SQLite, follows up twice,
and **stops following up the moment someone replies**.

Python 3.9+. Standard library only — nothing to `pip install`.

---

## What it will not do

It will not send an email where `personal_note` is blank, and it will not
send an email that still contains an unfilled `{{placeholder}}` or `[Name]`.
Both are hard aborts, logged and skipped.

That is deliberate. The personalisation line is the only part of the email
that actually earns a reply, so the tool refuses to run without it.

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
second email address to `contacts.csv`, run a real send, then open the
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

Workflow: append rows to `contacts.csv` during the evening, writing one real
`personal_note` per person. The scheduler picks them up next morning.
Re-importing is safe — existing rows are never overwritten or re-sent.

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
| Unfilled-placeholder abort | — | always on |
| Empty personal_note refusal | — | always on |

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
