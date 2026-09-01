#!/usr/bin/env python3
"""Offline tests for emailmap.py. No network, no DB writes outside the fixture."""
import sys, pathlib
sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import emailmap as em

fails = 0
def check(label, got, want):
    global fails
    ok = got == want
    fails += 0 if ok else 1
    print(f"[{'ok ' if ok else 'FAIL'}] {label}")
    if not ok:
        print(f"         got  {got!r}\n         want {want!r}")

print("--- name parsing ---")
check("plain",              em.parse_name("Priya Sharma")[0],        ("priya", "sharma"))
check("accents stripped",   em.parse_name("José Álvarez")[0],        ("jose", "alvarez"))
check("pronouns removed",   em.parse_name("Ravi Menon (He/Him)")[0], ("ravi", "menon"))
check("linkedin junk",      em.parse_name("Ravi Menon | We're Hiring!")[0], ("ravi", "menon"))
check("title removed",      em.parse_name("Dr. Anita Desai")[0],     ("anita", "desai"))
check("middle name ignored",em.parse_name("Kushal Kumar Gupta")[0],  ("kushal", "gupta"))
check("suffix removed",     em.parse_name("John Smith Jr.")[0],      ("john", "smith"))

print("\n--- refusals (a refusal is a SUCCESS: no bounce) ---")
for bad, why in [("Kushal", "single part"), ("K. Kushal", "initial"),
                 ("", "empty"), ("Ravi", "single part")]:
    parsed, reason = em.parse_name(bad)
    ok = parsed is None
    fails += 0 if ok else 1
    print(f"[{'ok ' if ok else 'FAIL'}] refuses {bad!r} ({why})")
    if parsed: print(f"         WRONGLY produced {parsed}")

parsed, reason = em.parse_name("张 伟")
ok = parsed is None
fails += 0 if ok else 1
print(f"[{'ok ' if ok else 'FAIL'}] refuses non-Latin script")

print("\n--- pattern rendering ---")
check("first.last", em.render_pattern("{first}.{last}", "priya", "sharma", "acme.io"),
      "priya.sharma@acme.io")
check("f+last",     em.render_pattern("{f}{last}", "priya", "sharma", "acme.io"),
      "psharma@acme.io")
check("first+l",    em.render_pattern("{first}{l}", "priya", "sharma", "acme.io"),
      "priyas@acme.io")
check("first only", em.render_pattern("{first}", "priya", "sharma", "acme.io"),
      "priya@acme.io")

print("\n--- pattern inference (reverse direction) ---")
check("infers first.last", em.infer_pattern("priya.sharma@acme.io", "priya", "sharma"),
      "{first}.{last}")
check("infers f+last",     em.infer_pattern("psharma@acme.io", "priya", "sharma"),
      "{f}{last}")
check("no false match",    em.infer_pattern("support@acme.io", "priya", "sharma"), "")

print(f"\n{'ALL PASSED' if not fails else str(fails) + ' FAILED'}")
sys.exit(1 if fails else 0)
