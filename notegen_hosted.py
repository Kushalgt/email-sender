#!/usr/bin/env python3
"""notegen_hosted.py - same as notegen.py, but the model runs on OpenRouter
instead of on your machine.

    resume_facts.json + a job description
        -> one note per OPENING, shared by its contacts (retrieval)
        -> hosted model writes 2 sentences              (generation)
        -> validator rejects anything ungrounded       (the real safety net)
        -> you approve or edit, one by one             (the human gate)
        -> written into jobs.csv personal_note         (outreach.py untouched)

The validator, prompt, retrieval, JD lookup, caching, and the
review/apply/list commands are shared with notegen.py via notegen_core.py -
a note is judged exactly the same way regardless of which script wrote it.
Only the model call differs.

Trade-off vs notegen.py: this sends your resume facts and the job
description to OpenRouter (and whichever provider it routes to). Nothing
about the human-approval gate changes, but it is no longer local-only.

Requires: an OpenRouter API key. Stdlib only, no pip installs.

    export OPENROUTER_API_KEY="..."     # put this in .env, then: source .env

    python3 notegen_hosted.py doctor    # is the key set? model configured?
    python3 notegen_hosted.py draft     # generate notes for contacts missing one
    python3 notegen_hosted.py review    # approve / edit / regenerate / skip
    python3 notegen_hosted.py apply     # write approved notes into jobs.csv
    python3 notegen_hosted.py list      # show what is in the cache
"""

import argparse
import datetime as dt
import json
import os
import sys
import urllib.request

import notegen_core as core
from notegen_core import (
    cmd_apply, cmd_list, cmd_review, facts, find_jd, make_note, read_jobs,
)

API_URL = "https://openrouter.ai/api/v1/chat/completions"
API_KEY_ENV = "OPENROUTER_API_KEY"


def cfg():
    """config.json's 'notegen_hosted' block overrides core defaults.

    The API key is never read from here - only from the environment - since
    config.json (unlike .env) is not gitignored.
    """
    return core.cfg("notegen_hosted")


# ------------------------------------------------------------------ model client

def generate(c, system, user, temperature, seed):
    """One chat completion via OpenRouter, asked to return JSON. Returns raw text.

    This is the generate_fn passed into core.make_note().
    """
    api_key = os.environ.get(API_KEY_ENV)
    payload = {
        "model": c["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "temperature": temperature,
        "seed": seed,
        "max_tokens": 220,
        "response_format": {"type": "json_object"},
    }
    req = urllib.request.Request(
        API_URL,
        data=json.dumps(payload).encode(),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=c["timeout_seconds"]) as r:
        resp = json.loads(r.read().decode())

    # Diagnostic only: set NOTEGEN_DEBUG=1 to see exactly what the model sent
    # back, before parse_note() and validate() touch it. Reasoning models put
    # their answer in places other than message.content, so print the whole
    # response rather than guessing which field matters.
    if os.environ.get("NOTEGEN_DEBUG"):
        print("\n" + "-" * 68, file=sys.stderr)
        print(f"RAW RESPONSE  (temperature={temperature}, seed={seed})",
              file=sys.stderr)
        print("-" * 68, file=sys.stderr)
        print(json.dumps(resp, indent=2, ensure_ascii=False), file=sys.stderr)
        print("-" * 68 + "\n", file=sys.stderr)

    choices = resp.get("choices") or [{}]
    return (choices[0].get("message") or {}).get("content", "")


# ------------------------------------------------------------------ commands

def cmd_doctor(args):
    c = cfg()
    print(f"\nOpenRouter  : {API_URL}")
    if os.environ.get(API_KEY_ENV):
        print(f"  [ok]   {API_KEY_ENV} is set")
    else:
        print(f"  [FAIL] {API_KEY_ENV} not set.")
        print(f"         Add it to .env (see .env.example), then: source .env")

    if not c["model"]:
        print('\n  [FAIL] no model chosen. Add this to config.json:')
        print('           "notegen_hosted": { "model": "<an OpenRouter model id>" }')
        print('         Browse ids at https://openrouter.ai/models')
    else:
        print(f"\n  [ok]   model: {c['model']}")

    print(f"\nGrounding   : {core.FACTS_PATH.name}")
    if core.FACTS_PATH.exists():
        d = facts()
        print(f"  [ok]   version {d.get('version')}, "
              f"{len(d['achievements'])} achievement(s), "
              f"{len(d.get('skills', []))} skill(s)")
        if len(d["achievements"]) < 4:
            print("  [warn] fewer than 4 achievements - notes will get repetitive.")
    else:
        print("  [FAIL] missing.")

    jobs = read_jobs()
    need = [j for j in jobs if not j["personal_note"]]
    print(f"\nOpenings    : {len(jobs)} row(s), {len(need)} without a note")
    for j in need:
        jd, src = find_jd(j)
        mark = "ok " if jd else "NO JD"
        where = f"{j['company_slug']}/{j['job_id']}".rstrip("/")
        print(f"  [{mark}] {where}  <- {src or 'nothing found'}")
    if need and not any(find_jd(j)[0] for j in need):
        print(f"\n  Paste the job description into the 'jd' column of "
              f"{core.store.JOBS_CSV.name},")
        print(f"  or drop a file in {core.JD_DIR.name}/ and name it in 'jd_file'.")
    print()


def cmd_draft(args):
    if not os.environ.get(API_KEY_ENV):
        sys.exit(f"ERROR: {API_KEY_ENV} not set. Add it to .env "
                  f"(see .env.example), then: source .env")
    c = cfg()
    if not c["model"]:
        sys.exit('ERROR: no model configured. Add "notegen_hosted": '
                  '{"model": "<an OpenRouter model id>"} to config.json')
    d = facts()
    conn = core.db()
    jobs = read_jobs()
    previous = core.known_notes(conn)
    made = failed = skipped = 0

    for job in jobs:
        where = f"{job['company_slug']}/{job['job_id']}".rstrip("/")
        if job["personal_note"] and not args.force:
            continue

        jd_text, src = find_jd(job)
        if not jd_text:
            print(f"  [skip] {where}: no job description found ({src or 'nothing'})")
            skipped += 1
            continue

        key = core.cache_key(job, jd_text, d, c)
        hit = conn.execute("SELECT * FROM notes WHERE cache_key=?", (key,)).fetchone()
        if hit and hit["status"] in ("draft", "approved", "applied") and not args.force:
            print(f"  [cached] {where}: {hit['status']}")
            continue

        print(f"  [gen] {where}  (jd: {src})")
        note, reasons, used = make_note(c, d, job, jd_text, previous, generate)
        status = "draft" if note else "needs_human"
        conn.execute(
            """INSERT OR REPLACE INTO notes
               (cache_key,email,company,company_slug,job_id,note,status,attempts,
                reasons,model,used_facts,created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?,?,?)""",
            (key, "", job["company"], job["company_slug"], job["job_id"], note,
             status, c["max_attempts"], "; ".join(reasons), f"openrouter:{c['model']}", used,
             dt.datetime.now().isoformat(timespec="seconds")))
        conn.commit()
        if note:
            previous.append(note)
            made += 1
            print(f"    -> {note}")
        else:
            failed += 1
            print(f"    -> GAVE UP: {'; '.join(reasons[:3])}")

    print(f"\n{made} drafted, {failed} need you, {skipped} skipped (no JD).")
    if made:
        print("Next: python3 notegen_hosted.py review")


def main():
    p = argparse.ArgumentParser(
        description="Draft personal_note lines with a hosted model via OpenRouter.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="check API key, model, facts and JDs").set_defaults(func=cmd_doctor)

    g = sub.add_parser("draft", help="generate notes for contacts missing one")
    g.add_argument("--force", action="store_true",
                   help="regenerate even if cached or already written")
    g.set_defaults(func=cmd_draft)

    sub.add_parser("review", help="approve / edit each draft").set_defaults(func=cmd_review)

    a = sub.add_parser("apply", help="write approved notes into jobs.csv")
    a.add_argument("--force", action="store_true", help="overwrite existing notes")
    a.set_defaults(func=cmd_apply)

    sub.add_parser("list", help="show the note cache").set_defaults(func=cmd_list)

    args = p.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
