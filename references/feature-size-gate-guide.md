# Size Gate Guide

Assess complexity BEFORE entering the design workflow. The goal: prevent ceremony on small tasks and prevent cowboy coding on large tasks.

## Decision Matrix

| Complexity | Criteria | Design Flow | Example |
|---|---|---|---|
| **S** | Single file change, clear existing pattern, no new interfaces, low blast radius | **Skip design.** Go directly to implementation. | Add a field to an existing config, fix a bug, add a new endpoint following existing patterns, update a template |
| **M** | 2-5 files, new interface or integration point, moderate risk | **Lightweight flow.** Single architect, one worker run for context + design + DESIGN.md. | New API integration, new skill, add a notification channel, refactor a module's interface |
| **L** | Many files, new subsystem, high risk, unknown dependencies | **Full flow.** Competing architects (2-3 options), comparison, autonomous selection. | New monitoring system, new data pipeline, auth overhaul, new agent workflow, multi-service integration |

## How to Assess

Ask yourself:
1. How many files will change? (1 -> S, 2-5 -> M, 6+ -> likely L)
2. Are there new interfaces? (no -> S, one -> M, multiple -> L)
3. Is the pattern already established? (yes -> S, partially -> M, no -> L)
4. What breaks if this is wrong? (nothing -> S, one feature -> M, multiple features/data -> L)
5. Do I know all the dependencies? (yes -> S, mostly -> M, not sure -> L)

If answers are mixed, round UP (S->M or M->L). It's cheaper to over-design slightly than to rip out a bad architecture.

## User Overrides

The user can override the size gate at any time:

| Phrase | Effect |
|---|---|
| "Design this properly" / "продумай как следует" / "полный дизайн" | Force full L-flow |
| "Just build it" / "просто сделай" / "без дизайна" | Skip design entirely |
| "Design only" / "только дизайн" / "не реализуй" | Stop after DESIGN.md + concise summary |
| "Lightweight design" / "лёгкий дизайн" | Force M-flow |

## Autonomous Sizing Rule

If the supervisor assesses a task as L-complexity, run the design flow automatically — don't ask permission. Post a brief note: "This is L-complexity (N files, new subsystem). Running design first." Then proceed.

If the user says "just build it" at any point, skip/abort design and go to implementation.

## Examples by Category

### Clearly S (skip design)
- "Add retry logic to the existing API call" — one file, existing pattern
- "Update the Telegram bot token" — config change
- "Fix the date formatting bug in the report" — bug fix, known location
- "Add a new field to the LinkedIn enrichment output" — existing pipeline, add column

### Clearly M (lightweight design)
- "Create a new skill for X posting" — new skill, defined format, 3-4 files
- "Add WhatsApp delivery to an existing notification flow" — new integration, existing patterns
- "Refactor the session registry to support labels" — interface change, 2-3 files
- "Build a script that fetches and caches Twitter lists" — new script with API integration

### Clearly L (full design)
- "Design the X monitoring module with lists, engagement tracking, and auto-reports" — new subsystem, multiple integrations, unknown API constraints
- "Build an auto-voice TTS pipeline" — new subsystem, multiple services, complex state management
- "Create a parallel research execution engine" — new architecture, async orchestration, delivery routing
- "Redesign the OpenClaw skill loading system" — core infrastructure, many consumers, high breakage risk

### Edge Cases (round up)
- "Add a caching layer to the research results" — could be S (simple file cache) or M (with invalidation, TTL, multiple consumers). Infer from context; ask one clarifying question only if genuinely ambiguous.
- "Integrate a new AI provider" — could be M (if following existing pattern) or L (if new auth model, different API shape). Check existing integrations first.
