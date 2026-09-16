#!/usr/bin/env python3
"""Offline test for the validator. No model, no network, no Ollama needed.

Run:  python3 test_validator.py
Every case below is a fabrication the model WILL eventually produce.
If this file passes, those fabrications cannot reach your outbox.
"""
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parent))
import notegen as ng

D = ng.facts()
C = ng.cfg()
CONTACT = {"email": "priya.sharma@examplecorp.com", "name": "Priya Sharma",
           "company": "ExampleCorp", "role": "Backend Software Engineer"}
JD = """Backend Software Engineer, ExampleCorp. We are moving billing off our
monolith. You will work on high-throughput data pipelines in Java and Spring
Boot against PostgreSQL, and help design the event model. Experience with Rust
and Kubernetes is a plus. Our p99 latency target is 120ms."""

CASES = [
    ("PASS  grounded",
     "Your team is moving billing off the monolith, which is the part of that work "
     "that usually hurts. I did a similar Kafka extraction at HiLabs.",
     True),
    ("BLOCK invented technology",
     "ExampleCorp's billing migration looks like hard work. I built the Elasticsearch "
     "cluster that indexed our whole event stream at HiLabs.",
     False),
    ("BLOCK invented number",
     "Your billing migration off the monolith is the interesting part. I cut our "
     "export pipeline p99 by 87% at HiLabs using cursor streaming.",
     False),
    ("BLOCK claims a JD skill as mine",
     "You are moving billing off the monolith at ExampleCorp. I have shipped "
     "production Rust services that solved exactly this.",
     False),
    ("BLOCK three sentences",
     "Your team is moving billing off the monolith. That is hard work. I did a "
     "similar Kafka extraction at HiLabs last quarter.",
     False),
    ("BLOCK greeting",
     "Hi Priya, your team is moving billing off the monolith and I did a similar "
     "Kafka extraction at HiLabs which lines up with that work directly.",
     False),
    ("BLOCK banned filler",
     "I was excited to see ExampleCorp is moving billing off the monolith. I did a "
     "similar Kafka extraction at HiLabs last quarter here.",
     False),
    ("PASS  hyphenated compound, grounded stem",
     "Your team is moving billing off the monolith, which is usually the messy "
     "half. I did a Kafka-based extraction at HiLabs last quarter.",
     True),
    # Riak, not Cassandra: Cassandra was added to resume_facts.json skills
    # after this test was written, which made the note legitimately grounded
    # and silently turned this case green. Any stem used here must be absent
    # from BOTH resume_facts.json and the JD above.
    ("BLOCK hyphenated compound, ungrounded stem",
     "Your team is moving billing off the monolith, which is usually the messy "
     "half. I ran the Riak-based ingest layer at HiLabs last quarter.",
     False),
    ("BLOCK too short",
     "Nice job posting. I use Java.",
     False),
]

fails = 0
for label, note, should_pass in CASES:
    reasons = ng.validate(note, D, JD, CONTACT, C, previous=[])
    passed = not reasons
    ok = (passed == should_pass)
    fails += 0 if ok else 1
    print(f"[{'ok ' if ok else 'FAIL'}] {label}")
    if reasons:
        print(f"         rejected because: {'; '.join(reasons)}")
    if not ok:
        print(f"         EXPECTED {'pass' if should_pass else 'block'}")

# duplicate detection uses `previous`
dup = ("Your team is moving billing off the monolith, which is the part of that "
       "work that usually hurts. I did a similar Kafka extraction at HiLabs.")
r = ng.validate(dup, D, JD, CONTACT, C, previous=[dup])
ok = bool(r)
fails += 0 if ok else 1
print(f"[{'ok ' if ok else 'FAIL'}] BLOCK duplicate of an earlier note")
if r:
    print(f"         rejected because: {'; '.join(r)}")

# A note is now written per OPENING and shared by every contact at that
# company, so the recipient's name is no longer in the allow-list. The name
# has to sit mid-sentence to be caught: a sentence-INITIAL capitalised word is
# deliberately ignored by the entity heuristic (otherwise the first word of
# every sentence would flag), so "Priya, your team..." still slips through.
# That limitation is pre-existing and unchanged here.
named = ("Your team is moving billing off the monolith, Priya, which is the "
         "hard part. I did a similar Kafka extraction at HiLabs last quarter.")
r = ng.validate(named, D, JD, CONTACT, C, previous=[])
ok = bool(r)
fails += 0 if ok else 1
print(f"[{'ok ' if ok else 'FAIL'}] BLOCK a recipient name mid-note "
      f"(notes are shared per opening)")
if r:
    print(f"         rejected because: {'; '.join(r)}")

print(f"\n{'ALL PASSED' if not fails else str(fails) + ' CASE(S) FAILED'}")
sys.exit(1 if fails else 0)