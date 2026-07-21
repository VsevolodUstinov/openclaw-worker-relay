# Autonomous Iteration Model

How an OpenClaw supervisor and a selected coding-agent worker collaborate during feature development.

## Optimization Target

Optimize for the user's time/attention and supervisor context continuity, not token cost. One complete worker run is better than repeated partial runs that each need user input.

## Roles

**Supervising OpenClaw agent**:
- Supervisor / technical product manager / context keeper
- Holds the full picture: user intent, prior research, cross-module knowledge, operator preferences
- Formulates worker prompts with enough context to succeed independently
- Inspects worker results against the original goal (not just "did it run")
- Decides when the work is complete — does not ask the user unless the escalation criteria are met
- Posts concise PPR summaries (see below)

**Claude Code or Codex** (execution agent):
- Worker for heavy lifting: reads codebase, generates architecture, writes DESIGN.md, implements, tests
- Must be launched through the current `worker-relay` protocol (file prompt, routing validation, launch gate, async wake continuation)
- Uses explicit `--engine`; can use `--resume` only with a session/thread from that same engine
- Can start fresh for independent review, isolated alternatives, tester/reviewer roles, or `RESUME_FAILED` recovery when the old session expired
- Should use subagents where useful (explorers, competing architects, reviewers/testers)
- The supervisor must still provide enough context in each prompt for the run to succeed without relying on fragile memory

**User/operator**:
- Receives informational summaries, not approval requests
- Can intervene at any point ("stop", "change X", "try a different approach")
- Absence = implicit approval to continue

## Iteration Protocol

No iteration cap. Continue focused iterations until the original goal is truly achieved.

### When to continue autonomously:
- Gaps remain and there is no real blocker requiring user input or external waiting
- Each iteration has a specific gap and shows clear progress
- The goal is not yet fully met

### When to pause/escalate:
- Repeated non-progress (same gap not closing across iterations)
- Genuine blocker requiring information only the user has
- Safety/product decision that fits the escalation rules in the main SKILL.md
- Non-progress means the strategy needs to change, not just "try harder"

### Iteration steps:
1. The supervisor receives the selected worker's result or recoverable launch failure (via wake continuation)
2. If status/output is `RESUME_FAILED`, treat it as no task progress; relaunch fresh without `--resume` unless the user explicitly required that exact old session
3. The supervisor inspects the result against the original goal — gap analysis
4. If gaps remain and no blocker:
   - Identify the SPECIFIC gap (not "make it better")
   - Launch a follow-up worker run with: current state, the specific gap, and targeted instructions
   - Keep follow-up prompts SHORT if using `--resume` (prior session provides context)
5. Repeat until the goal is fully met and verified

### Context handoff between runs

Every worker prompt MUST include:
- What we're building and why (1-2 sentences from the supervisor's context)
- Current state (inline or a file path the worker can read)
- The specific gap or task (concrete, not vague)
- Where to write the result

For `--resume` runs: keep the follow-up prompt under ~500 words. Prior session provides context.
For fresh runs: include the full state or a clear file path + the specific delta.

## Independent Tester/Reviewer Pattern

After the main worker believes design/implementation is ready, launch a separate independent worker tester/reviewer when risk is non-trivial.

Use a **separate/fresh session** for the tester so it is not anchored to the main worker's assumptions.

Tester prompt should include:
- Original intention and DoD
- Path to DESIGN.md and/or implementation diff
- Known regressions or historical failure modes
- Explicit instruction to challenge assumptions, find missing tests, verify edge cases, and report PASS/FAIL

If the tester finds gaps, the supervisor sends a focused follow-up to the main worker (or a repair worker) and repeats the tester loop when needed. Do not ask the user for confirmation unless escalation criteria apply.

## PPR Summary Format

After design or implementation completes, the supervisor posts to the user:

```
Design complete: {module name}

Chosen approach: {1-2 sentence summary of what was picked and why}

Key interfaces: {inputs -> outputs, concrete}

Risks: {top 1-2 risks with mitigations}

DoD: {N items, highlights}

DESIGN.md saved to: {path}

Proceeding to implementation.
```

Keep it under 10 lines. Link to DESIGN.md for details. The summary is informational — the supervisor proceeds immediately after posting.

If escalation criteria triggered a pause, the summary changes to a question:

```
Design options for: {module name}

| Criterion | Option A: Minimal | Option B: Clean |
|---|---|---|
| ... | ... | ... |

Recommendation: {option} because {reason}.

This involves {irreversible thing}. Which approach?
```

## Completion Criteria

The supervisor decides "work is complete" when:
1. All required artifacts exist and are filled (not placeholder text)
2. Interfaces are concrete (data types, field names, not "takes input")
3. DoD items are testable (runnable commands or inspectable files)
4. Implementation works and tests pass
5. Independent tester passed (if applicable)

The supervisor does NOT need the user's confirmation to proceed or declare done. The self-verification checklist is the quality gate.

## Anti-patterns

- Don't send the worker the same prompt twice hoping for a better result — identify the specific gap
- Don't keep iterating with vague prompts after non-progress — diagnose the blocker, change strategy, or escalate only if needed
- Don't include the full research report in every worker prompt — distill to relevant findings
- Don't ask the user "does this look good?" — post the summary and proceed
- Don't wait for a wake event if the run is already complete and the result artifact exists; do not poll/block the same turn just to wait for worker completion after launch
- Don't present findings and wait for approval before implementation (default is autonomous)
- Don't cap iterations at an arbitrary number — continue until the goal is met or a genuine blocker is hit
