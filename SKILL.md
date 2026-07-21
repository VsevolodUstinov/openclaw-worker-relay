---
name: worker-relay
description: "Run Codex CLI, Claude Code, or another supported external agent as a supervised asynchronous worker with strict OpenClaw routing, progress notifications, result delivery, session resume, optional provider fallback, and multi-turn supervisor ownership. Use when asked to delegate substantial coding, research, analysis, document generation, or automation work to Codex/Claude, run a background worker with chat updates, continue an external worker session, or notify the originating chat when done. Do not use for native OpenClaw subagents, direct interactive CLI sessions, or quick conversational answers."
---

# Worker Relay

Worker Relay is OpenClaw's external-agent execution harness. It runs Claude Code or Codex CLI as an asynchronous worker while the current OpenClaw agent remains the supervisor and delivery owner. The canonical executable is `{baseDir}/run-task.py`; do not create another runner or copy it into an automation. The historical name `claude-code-task` may remain in upgraded deployments only as a compatibility alias, never as a second implementation.

Use these terms consistently: OpenClaw is the **supervisor**, Claude Code/Codex are **external workers**, and Worker Relay is the harness connecting dispatch, progress, delivery, wake, and continuation. A native OpenClaw subagent and a raw interactive CLI invocation are different execution surfaces.

## Non-negotiable operating rules

These rules apply on the first read, including during a live E2E:

1. Launch only `{baseDir}/run-task.py --detach`. The runner owns the transient user service needed to survive OpenClaw tool exit. Do not handwrite `nohup`, `systemd-run`, `tmux`, raw `claude`/`codex`, or another wrapper unless the user is explicitly testing that alternate surface.
2. Never edit the canonical shared-skill checkout during an E2E. Capture evidence, finish or stop the test, change the repository through its normal review/test/deploy workflow, then rerun from a clean deployed commit.
3. Run provider/mode probes sequentially. Claude Code and Codex share local login, rollout, and registry state; parallel probes can invalidate the diagnosis.
4. One `401`, transport error, `no rollout found`, timeout, or delayed wake is an observation, not a durable feature gap. Wait for the previous process to exit, run a standard control, retry the same case sequentially, and compare persisted state before changing current-state documentation.
5. Resume only after the fresh runner process has fully exited and its provider id is present in the registry. A resume failure still maps to exit `42` for callers, but the E2E diagnosis requires the control/retry sequence above.
6. Correlate asynchronous messages by engine, output path, run id, and wake id. A late wake from an older run must not trigger a new diagnosis or an extra continuation.
7. For a full live test, follow `references/testing-protocol.md` exactly. The test owns its ordered phases; do not improvise additional probes or launch phases in parallel.
8. One detached worker phase occupies one supervisor turn. After `DETACHED_LAUNCH_ACCEPTED`, make no more work/tool calls in that turn: no `while`, `sleep`, `wait`, `systemctl`, log polling, registry inspection, or next launch. End the turn and continue only when the routed completion wake starts the next turn. If the runtime exposes `sessions_yield`, it is the sole optional exception and must immediately terminate the turn; do nothing after it. Polling after detach is an E2E failure because it masks the wake and tool-exit lifecycle being tested.
9. A completion-wake turn is the next working supervisor turn, not a status-only intermission. After verifying the completed phase, if the ordered workflow still has work and no genuine blocker, launch exactly the next detached phase in that same wake turn and release it again. Never end a wake turn with only a promise such as "I will read the skill and launch the next phase"; no future trigger exists, so that is a stalled workflow and a failed E2E attempt.
10. Detached services use `systemd-run --collect`. After collection, `systemctl show` may return default-looking `Result=success` and `ExecMainStatus=0` for a nonexistent unit; if `LoadState=not-found`, those fields are not exit evidence. Determine terminal status from the correlated runner wake/output and, when needed, the unit journal line `Main process exited ... status=N`. For invalid resume, `RESUME_FAILED` plus correlated `exit=42` or journal `status=42` passes; never override it with post-collection defaults.

## Mandatory preflight

Before every launch:

1. Preserve the exact source OpenClaw session key. Never synthesize `agent:main`; use the actual agent id and live session.
2. Choose the real project directory. The runner does not create a Git repository inside it.
3. Omission of `--engine` means Codex only, using the saved ChatGPT subscription login. Select Claude explicitly with `--engine claude`; never infer or add a fallback.
4. Omission of `--mode` means Terra for Codex and Sonnet for Claude. Use `--mode sol` only with Codex or `--mode fable` only with Claude; never infer either premium mode.
5. Add `--fallback-engine claude` only when the caller explicitly requires continuity across provider unavailability. Fallback is only for provider unavailability before useful work; it is not a generic retry.
6. Decide whether this is a fresh workstream or a continuation. Use `--resume` only with a session/thread id created by the same engine, never a runner `run_id` or `wake_id`. Resume and provider fallback are intentionally mutually exclusive.
7. Write a task with a concrete Definition of Done and an appropriate self-check.
8. For routed runs, execute `--validate-only` first with the same engine, mode, and fallback flags. A routing failure is a launch blocker.

```bash
python3 {baseDir}/run-task.py \
  --task "routing probe" \
  --engine codex \
  --fallback-engine claude \
  --project /absolute/project/path \
  --session "$OPENCLAW_SESSION_KEY" \
  --validate-only
```

WhatsApp validation requires the exact session to exist in OpenClaw's gateway or local session registry. Telegram topics additionally require a resolvable target and session UUID. Never bypass validation by manually inventing a destination.

## Launch

Put substantial prompts in a file to avoid shell quoting and truncation problems:

```bash
PROMPT_FILE=/tmp/cc-prompt-$(date +%s).txt
# Write the complete task to $PROMPT_FILE with the environment's normal file tool.

python3 {baseDir}/run-task.py \
  --detach \
  --detach-log /tmp/cc-launch.log \
  --task "$(cat "$PROMPT_FILE")" \
  --engine codex \
  --fallback-engine claude \
  --project /absolute/project/path \
  --session "$OPENCLAW_SESSION_KEY" \
  --timeout 7200 \
  --session-label "short workstream label"
```

Do not claim the task launched unless the launcher returns `DETACHED_LAUNCH_ACCEPTED` with a unit and log path. That is the last work result permitted in the current turn. Do not append shell `&`, inspect the service, poll the log, or keep any tool open with `wait`; `--detach` owns durability. End the current turn immediately, either normally or with one terminal `sessions_yield` call when that runtime requires it. The next turn begins from the routed completion wake: inspect terminal evidence and, when the multi-phase goal remains active, launch exactly the next phase before ending that wake turn. A text-only promise to continue later does not preserve execution.

### Resume

```bash
python3 {baseDir}/run-task.py \
  --detach \
  --detach-log /tmp/cc-resume.log \
  --task "Focused next step" \
  --engine codex \
  --project /absolute/project/path \
  --session "$OPENCLAW_SESSION_KEY" \
  --resume "$CODEX_THREAD_ID"
```

Resume only when prior worker context is useful and the work is sequential. Never pass a Claude session id to Codex or a Codex thread id to Claude. Start fresh for unrelated work, intentional parallelism, or a missing/expired session. Exit `42` means the resume did not start; relaunch fresh when preserving that exact old session was not a user requirement.

## Execution modes

- No engine/mode/model/Fast/fallback flags: Codex on explicit `gpt-5.6-terra`, with no fallback.
- `--engine claude` without a mode/model/Fast flag: Claude on the forward-compatible `sonnet` alias, with no fallback.
- `--mode sol`: explicit Codex-only premium mode, pinned to `gpt-5.6-sol`.
- `--engine claude --mode fable`: explicit Claude-only premium mode, pinned to the forward-compatible `fable` alias.
- `--fallback-engine NAME`: one sequential retry only when the primary executable is absent or the provider fails before useful work with recognized auth, rate-limit, network, or service-unavailable evidence. The failed primary does not emit a false final wake; the fallback owns final delivery.
- `--model NAME`: expert provider-specific override on the selected subscription path.
- `--fast`: the selected provider's native Fast mode. For Codex this sets `features.fast_mode=true` and `service_tier="fast"`; use only when the user explicitly requests Fast or urgent paid speed.
- `--mode`, `--model`, and `--fast` are mutually exclusive.
- Provider fallback cannot be combined with `--resume`, `--mode`, or provider-specific `--model`. It never triggers after a tool call, a completed result, an ordinary task failure, or a timeout, avoiding duplicate side effects.

```bash
# Explicit Sol
python3 {baseDir}/run-task.py --mode sol --task "..." --project /absolute/path

# Explicit Fable
python3 {baseDir}/run-task.py --engine claude --mode fable --task "..." --project /absolute/path
```

The runner strips `ANTHROPIC_API_KEY` for Claude and `OPENAI_API_KEY`, `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`, and inherited `CODEX_HOME` for Codex. This is a billing/auth invariant: workers use the user's saved Claude or `~/.codex` ChatGPT subscription identity, not API credentials or an OpenClaw agent-local Codex home.

## Delivery ownership

The external coding agent is a worker, not the acceptance owner. The supervising agent must:

1. Preserve the original user goal across wake turns and compaction.
2. Ask the selected worker for the real result, not merely progress or an implementation proposal.
3. Include self-verification in the worker prompt when applicable.
4. Inspect the result and independently run the smallest meaningful verification.
5. Launch a focused follow-up when the original goal remains incomplete and no genuine blocker exists.
6. Report completion only after the original goal, not merely the latest substep, is satisfied.

The runner sends launch/progress/result notifications and wakes the original supervising session. It must never wake a different agent merely because that key looks syntactically plausible.

## Feature workflow

This skill is the canonical feature-delivery workflow; do not fork its rules into another skill.

Classify before launch:

| Size | Typical scope | Design action |
|---|---|---|
| S | One file, known pattern, low blast radius | Implement directly |
| M | Several files, integration or interface change | One pragmatic design in `DESIGN.md` |
| L | New subsystem, broad or risky change | Compare 2-3 real options in `DESIGN.md` |

User instructions override the gate: design-only stops after design; "just build it" skips design; explicit full-design requests use the L flow.

For M/L work, have the selected worker read local instructions and relevant code first, document interfaces, migration/rollback, risks, and testable DoD, then self-review the design. Continue autonomously through implementation and verification unless user input is genuinely required.

Use the detailed references only when the task needs them:

- `references/feature-size-gate-guide.md`
- `references/feature-design-template.md`
- `references/feature-architect-prompts.md`
- `references/feature-autonomous-iteration-model.md`

## Prompt contract

A strong worker task contains:

- original intent and exact deliverable;
- target repository and relevant paths;
- local instructions/bootstrap files to read;
- constraints and non-goals;
- Definition of Done;
- verification commands or evidence expected;
- requirement to report changed files, checks run, unresolved gaps, and blockers.

Do not over-specify implementation when repository conventions should decide it. For resumed sessions, keep the next-step prompt short because prior context is already present.

## Result semantics

The process exit code is meaningful:

| Code | Meaning |
|---|---|
| `0` | Worker succeeded and routed delivery/wake was confirmed when requested |
| `2` | CLI or routing validation error; worker did not start |
| `42` | Requested provider session/thread could not be resumed |
| `70` | Runner crashed |
| `75` | Worker succeeded, but final routed delivery/wake was not confirmed |
| `124` | Timeout |
| other | Selected worker child exit code is preserved |

The short `--detach` launcher returning `0` means only that systemd accepted the durable service. The worker's terminal result remains the service log, output file, registry status, and routed completion/wake; never report task success from detach acceptance alone.

Outputs retain unique `/tmp/cc-<timestamp>-<run-id>.txt` names unless `--output` is supplied. Provider sessions are tracked atomically with an `engine` field in the legacy-compatible `~/.openclaw/claude_sessions.json`; old entries without `engine` mean Claude. Corrupt registries are preserved with a `.corrupt-<timestamp>` suffix before recovery.

On WhatsApp, success requires direct result delivery plus gateway acceptance of an asynchronous `sessions_send` wake (`timeoutSeconds: 0`). The wrapper does not block on the large target session's full reasoning turn. On Telegram, the runner wakes the exact session, extracts the supervisor response, and delivers it deterministically. HTTP 200 alone is not success; the nested gateway/tool payload must also report a non-error status.

## Testing language

When the user says "run the tests" for a target project, run that project's tests. Do not reinterpret an ordinary project request as a runner E2E test.

When modifying or explicitly testing this skill itself, use both layers:

```bash
python3 -m unittest discover -s {baseDir}/tests -v
python3 -m py_compile {baseDir}/run-task.py {baseDir}/session_registry.py
```

Then follow the ordered live gates in `references/testing-protocol.md`. Do not replace its launch path or infer a persistent regression from a single live failure.

## Operational references

Load these in order when needed:

1. `CURRENT-BEHAVIOR.md` for the current implementation contract.
2. `INCIDENT-INDEX.md` when diagnosing a failure or changing routing/lifecycle code.
3. `TECHNICAL-INSIGHTS.md` for durable engineering lessons.
4. `WAKE-TROUBLESHOOTING.md` for symptom-driven commands.
5. Deployment-local `history/` when present and detailed incident evidence is required.

Update `CURRENT-BEHAVIOR.md` whenever behavior changes. For a production failure, add one compact row to `INCIDENT-INDEX.md`; deployments that retain private operational history may put detailed evidence in `history/incidents/`. Keep `CHANGELOG.md` concise and release-oriented.
