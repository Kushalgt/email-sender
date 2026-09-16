#!/usr/bin/env python3
"""notegen.py - draft the {{personal_note}} line for outreach.py with a LOCAL
open-weight model, then make you approve it before it can ever be sent.

    resume_facts.json + a job description
        -> one note per OPENING, shared by its contacts (retrieval)
        -> local model writes 2 sentences              (generation)
        -> validator rejects anything ungrounded       (the real safety net)
        -> you approve or edit, one by one             (the human gate)
        -> written into jobs.csv personal_note         (outreach.py untouched)

Deliberate design choice: this script does NOT import or modify outreach.py.
outreach.py still refuses to send any contact whose personal_note is empty.
That rule is the thing keeping you safe, so we route around it instead of
weakening it - a note only reaches jobs.csv after you approved it.

Requires: Ollama running locally. Nothing else. Stdlib only, no pip installs.
Nothing here leaves your machine - not your resume, not your contact list.
For a hosted-model alternative (OpenRouter), see notegen_hosted.py instead;
the validator, prompt and review/apply/list flow are shared with it via
notegen_core.py, so a note is judged the same way regardless of which one
wrote it.

    python3 notegen.py doctor      # is Ollama up? which models are pulled?
    python3 notegen.py draft       # generate notes for contacts missing one
    python3 notegen.py review      # approve / edit / regenerate / skip
    python3 notegen.py apply       # write approved notes into jobs.csv
    python3 notegen.py list        # show what is in the cache
"""

import argparse
import datetime as dt
import json
import sys
import urllib.request

import notegen_core as core
from notegen_core import (
    cmd_apply, cmd_list, cmd_review, facts, find_jd, make_note, read_jobs,
    validate,
)

LOCAL_DEFAULTS = {
    "host": "http://127.0.0.1:11434",
}


def cfg():
    """config.json's 'notegen' block overrides core defaults + LOCAL_DEFAULTS."""
    return core.cfg("notegen", LOCAL_DEFAULTS)


# ------------------------------------------------------------------ model client

def ollama(path, payload=None, timeout=30, host=None):
    """Minimal Ollama HTTP call. Returns parsed JSON, raises on transport error."""
    url = (host or LOCAL_DEFAULTS["host"]).rstrip("/") + path
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        url, data=data,
        headers={"Content-Type": "application/json"},
        method="POST" if data else "GET",
    )
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def installed_models(c):
    """Names of models already pulled. Empty list if Ollama is unreachable."""
    try:
        tags = ollama("/api/tags", timeout=10, host=c["host"])
        return sorted(m["name"] for m in tags.get("models", []))
    except Exception:
        return []


def generate(c, system, user, temperature, seed):
    """One chat completion, asked to return JSON. Returns raw text.

    This is the generate_fn passed into core.make_note().
    """
    payload = {
        "model": c["model"],
        "messages": [{"role": "system", "content": system},
                     {"role": "user", "content": user}],
        "stream": False,
        "format": "json",          # ask Ollama to constrain output to JSON
        "options": {"temperature": temperature, "seed": seed, "num_predict": 220},
    }
    resp = ollama("/api/chat", payload, timeout=c["timeout_seconds"], host=c["host"])
    return (resp.get("message") or {}).get("content", "")


# ------------------------------------------------------------------ commands

def cmd_doctor(args):
    c = cfg()
    print(f"\nOllama host : {c['host']}")
    models = installed_models(c)
    if not models:
        print("  [FAIL] cannot reach Ollama, or no models pulled.")
        print("         Start it, then pull a small instruct model, e.g.:")
        print("           ollama serve")
        print("           ollama pull <a 7B-14B instruct model>")
        print("         Check the current model list at ollama.com/library -"
              " I deliberately do not hardcode a name here.")
    else:
        print(f"  [ok]   reachable, {len(models)} model(s):")
        for m in models:
            print(f"           {m}")

    if not c["model"]:
        print('\n  [FAIL] no model chosen. Add this to config.json:')
        print('           "notegen": { "model": "<one of the names above>" }')
    elif models and c["model"] not in models:
        print(f"\n  [FAIL] configured model {c['model']!r} is not pulled.")
    elif c["model"]:
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
    c = cfg()
    if not c["model"]:
        sys.exit("ERROR: no model configured. Run: python3 notegen.py doctor")
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
             status, c["max_attempts"], "; ".join(reasons), c["model"], used,
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
        print("Next: python3 notegen.py review")


def main():
    p = argparse.ArgumentParser(
        description="Draft personal_note lines with a local open-weight model.")
    sub = p.add_subparsers(dest="cmd", required=True)

    sub.add_parser("doctor", help="check Ollama, model, facts and JDs").set_defaults(func=cmd_doctor)

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
