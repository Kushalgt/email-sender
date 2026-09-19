# notegen.py — AI-drafted `personal_note`, with a hard grounding check

Drafts the `{{personal_note}}` line for `outreach.py` using a **local
open-weight model** (Ollama). Nothing leaves your machine: not your resume,
not your contact list.

There is also `notegen_hosted.py`, which does the exact same thing but
calls a hosted model via OpenRouter instead. The validator, prompt,
retrieval, JD lookup, caching, and the review/apply/list commands all live
in `notegen_core.py` and are shared by both scripts unchanged — a note is
judged the same way regardless of which one wrote it. The trade-off: your
resume facts and job descriptions get sent to OpenRouter, so it is no
longer local-only. Use it only if you're fine with that.

```bash
export OPENROUTER_API_KEY="..."     # put this in .env, then: source .env
# add to config.json: "notegen_hosted": { "model": "<an OpenRouter model id>" }

python3 notegen_hosted.py doctor    # is the key set? model configured?
python3 notegen_hosted.py draft     # generate
python3 notegen_hosted.py review    # same human gate as notegen.py
python3 notegen_hosted.py apply     # writes into the same jobs.csv
```

## Why it is a separate script

`outreach.py` says in its own docstring:

> It does NOT write your personalisation for you. Every contact must have a
> personal_note or the script refuses to send - either hand-written in
> jobs.csv, or, if that cell is empty, the fallback in
> templates/default_note.txt.

This feature contradicts the "write it for me" half of that rule, so
**`outreach.py`'s note-writing logic is not touched by notegen.**
`notegen.py` writes approved notes into `jobs.csv`, the same as if you had
typed them in yourself. The note is AI-drafted but human-approved before it
ever reaches the CSV.

The generic `templates/default_note.txt` fallback is a separate, much
simpler feature: it is one fixed sentence with no grounding check, used
only to keep an opening from being skipped entirely while it has no note at
all — yours or notegen's. `notegen draft` still treats an opening as
"without a note" even while the default is being used for it. Once
`notegen apply` writes an approved note into `jobs.csv`, `outreach.py`
picks it up on its next `send` for any contact not yet emailed, and it
overtakes the default there. See the README's "Default personal note"
section for how it works.

## Files

| File | What it is |
|---|---|
| `notegen.py` | the local (Ollama) tool — stdlib only, no `pip install` |
| `notegen_hosted.py` | same tool, but calls a hosted model via OpenRouter |
| `notegen_core.py` | shared validator, prompt, retrieval, caching, review/apply/list — used by both scripts above |
| `resume_facts.json` | **the grounding file.** The model may only claim things written here |
| `jds/<name>.txt` | one job description per contact |
| `test_validator.py` | offline proof that fabrications get blocked (no model needed) |
| `notes` table in `outreach.db` | draft cache + approval state |

## Setup

1. Install Ollama and pull a small instruct model (7B–14B at 4-bit is plenty
   for two sentences). Check `ollama.com/library` for what is current.
2. Point `config.json` at it:

   ```json
   "notegen": { "model": "<the exact name from `ollama list`>" }
   ```

3. **Expand `resume_facts.json`.** It is seeded with only 3 achievements
   pulled from your own templates. The model cannot be more specific than
   this file — this is the single highest-leverage thing you can do.
4. **Give each opening a job description.** Paste it straight into the `jd`
   column of `jobs.csv` — that is the simplest route and the only one that
   supports two different openings at the same company. Lookup order:
   `jd` column → `jd_file` column → `jds/<company>.txt`

   A note is drafted **once per opening**, not once per person, and is then
   shared by every contact at that company. So eight contacts at one company
   cost one model run, not eight. The prompt deliberately contains no
   recipient name, and the validator will reject a note that names one.

## Use

```bash
python3 notegen.py doctor    # is Ollama up? model pulled? JDs found?
python3 notegen.py draft     # generate
python3 notegen.py review    # [a]pprove [e]dit [r]eject — the human gate
python3 notegen.py apply     # write into jobs.csv (backs it up first)
python3 outreach.py send --dry-run   # read the real emails before sending
```

## What the validator blocks

A note is thrown away and regenerated (up to 4 attempts, rising temperature)
if it:

- names a technology, tool or company **not** in `resume_facts.json` or the JD
- claims a JD-only skill as *your* experience (`"I've shipped Rust services"`
  when Rust is in their JD but not your resume) — this is the highest-value check
- contains a number that appears in neither your facts nor the JD
- runs past 2 sentences, or 18–55 words
- has a greeting, a sign-off, or a banned phrase (`"excited to see"`, …)
- is >55% similar to a note already generated, or reuses an opening

After 4 failures the contact is marked `needs_human` and `review` asks you to
type the line yourself. It is never silently skipped.

## Known limits — read these

- **Term-level, not claim-level.** The validator checks that every *entity* you
  mention is real. It cannot catch a sentence built only from real words that
  still overstates what you did. That is why the approve step exists.
- **Small models are worse writers.** Expect to hit `[e]dit` often at first.
  Fix it by adding achievements to `resume_facts.json`, not by using a bigger model.
- **Ollama's API may drift.** The `format: "json"` option and `/api/chat` shape
  are what I used; verify against current Ollama docs if a call fails.
  `parse_note()` already falls back to regex and then raw text, so a format
  change degrades rather than crashes.
- **Cache key** = email + JD + `resume_facts.version` + prompt version + model.
  Bump `version` in `resume_facts.json` after editing it, or old drafts get reused.
