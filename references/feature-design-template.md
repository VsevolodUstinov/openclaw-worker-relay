# DESIGN.md Template

Use this template for the output artifact. Replace `{placeholders}` with real content. Delete sections marked "if applicable" when not needed. Keep the document shorter than the code it describes.

---

# {Module Name} — Design Document

**Status:** Draft | Approved | Implemented
**Date:** {YYYY-MM-DD}
**Designed by:** {agent}

## 1. Intention

{What we're building and why. 1-3 sentences. Include the problem it solves.}

## 2. Scope

### In Scope
- {concrete deliverable 1}
- {concrete deliverable 2}

### Out of Scope
- {explicitly excluded thing 1 — and why}
- {explicitly excluded thing 2 — and why}

## 3. Interfaces

### Inputs
{What this module receives. Be concrete: data formats, API contracts, event schemas, CLI args, config keys.}

### Outputs
{What this module produces. Be concrete: data formats, side effects, API responses, files written, events emitted.}

### Dependencies
{What this module depends on — other modules, external services, data stores, env vars, credentials.}

### Consumers
{What depends on this module — other modules, cron jobs, user-facing features.}

## 4. Architecture Options

### Option A: Minimal
{Smallest change, maximum reuse of existing patterns. Even if ugly or not ideal long-term.}

- **Approach:** {how it works — 3-5 sentences}
- **Files to create/modify:** {concrete list with paths}
- **Effort:** S / M / L
- **Risk:** {what could go wrong with this approach specifically}
- **Trade-off:** {what you gain vs what you give up}

### Option B: Clean
{Best maintainability, clear abstractions, proper separation of concerns. Even if more initial work.}

- **Approach:** {how it works — 3-5 sentences}
- **Files to create/modify:** {concrete list with paths}
- **Effort:** S / M / L
- **Risk:** {what could go wrong with this approach specifically}
- **Trade-off:** {what you gain vs what you give up}

### Option C: Pragmatic (only if A and B are meaningfully different)
{Balance of implementation speed and code quality.}

- **Approach:** {how it works — 3-5 sentences}
- **Files to create/modify:** {concrete list with paths}
- **Effort:** S / M / L
- **Risk:** {what could go wrong with this approach specifically}
- **Trade-off:** {what you gain vs what you give up}

### Comparison

| Criterion | Option A | Option B | Option C |
|---|---|---|---|
| Effort | | | |
| Risk | | | |
| Maintainability | | | |
| Reuse of existing patterns | | | |
| Future extensibility | | | |
| Complexity introduced | | | |

### Recommendation
{Which option and why. 2-3 sentences. Name the deciding factor.}

## 5. Chosen Approach

{Agent picks the best option and fills this section with the selected approach + rationale. If human overrides during review, update accordingly.}

## 6. Risks & Mitigations

| Risk | Likelihood | Impact | Mitigation |
|---|---|---|---|
| {specific risk 1} | Low/Med/High | Low/Med/High | {concrete action} |
| {specific risk 2} | Low/Med/High | Low/Med/High | {concrete action} |

## 7. Definition of Done

- [ ] {Concrete, testable criterion 1}
- [ ] {Concrete, testable criterion 2}
- [ ] {Concrete, testable criterion 3}
- [ ] All existing tests pass
- [ ] No new lint/type errors introduced
- [ ] {Module-specific acceptance criterion}

Every item must be verifiable by running a command, inspecting a file, or observing behavior. No subjective items ("code is clean").

## 8. Implementation Plan

### Stage 1: {name — e.g., "Core interface"}
- {task 1}
- {task 2}
- **Gate:** {what must be true before Stage 2 — concrete check}

### Stage 2: {name — e.g., "Integration"}
- {task 1}
- {task 2}
- **Gate:** {what must be true before Stage 3 — concrete check}

### Stage 3: {name — e.g., "Validation & polish"} (if needed)
- {task 1}
- {task 2}
- **Gate:** {final DoD verification}

No calendar timeframes. Use S/M/L effort per stage. Sequence by dependencies.

## 9. Rollback Plan

{How to undo this if it doesn't work. Specific steps, not "revert the changes."}

- Step 1: {concrete action}
- Step 2: {concrete action}
- Verification: {how to confirm rollback succeeded}

## 10. Validation Steps

{How to verify the design was implemented correctly after build. Specific commands, checks, or tests.}

1. {Run X and expect Y}
2. {Check file Z contains W}
3. {Verify behavior B in context C}

## 11. Surprises & Deviations (filled during implementation)

{Track deviations from the design that emerged during implementation. This section starts empty and is updated by the implementation agent or human as surprises are discovered.}

| Deviation | Why | Impact on design |
|---|---|---|
| | | |
