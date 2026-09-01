Before making any implementation changes, follow this workflow strictly:

### Phase 1 — Main Idea

First, analyze my requirement and explain the **main idea/approach** you propose, including:

* What you understand the problem to be
* The proposed solution at a high level
* Key design decisions and assumptions
* Important trade-offs or risks
* What you plan to change/build

**Do not write or modify any code yet.**

Then stop and ask for my approval.

### Phase 2 — Complete Architecture

Only after I explicitly approve Phase 1, provide the **complete technical architecture** before writing any code.

Cover:

* System/component architecture
* Data flow and control flow
* Modules/services and their responsibilities
* Interfaces/APIs and contracts
* Data models/schema
* Dependencies and integrations
* Configuration and environment requirements
* Error handling and edge cases
* Security considerations
* Performance and scalability considerations
* Testing strategy
* Deployment/runtime considerations
* File/folder structure and exactly which files will be added or modified
* Any assumptions, limitations, and alternatives considered

Use diagrams (e.g. Mermaid) where they improve clarity.

**Do not write or modify any code yet.**

Then stop and ask for my approval.

### Phase 3 — Implementation

Only after I explicitly approve the architecture, proceed with implementation.

During implementation:

* Follow the approved architecture.
* Do not make significant architectural changes without asking for approval first.
* Inspect the existing codebase before modifying anything.
* Reuse existing patterns, utilities, and abstractions where appropriate.
* Keep changes minimal, maintainable, and production-ready.
* Implement appropriate error handling, validation, logging, and tests.
* Do not silently make assumptions when an important decision is unclear; ask me first.
* After implementation, summarize what was changed, which files were modified, how it works, and how to test/verify it.

### Strict Approval Gates

**Requirement → Phase 1 → STOP → My approval → Phase 2 → STOP → My approval → Phase 3**

Never skip an approval gate, even if the implementation seems straightforward.

If you identify ambiguity or missing information during any phase, ask the necessary questions and wait for my response rather than proceeding based on assumptions.
