# Runner Testing Protocol

Use this protocol when `worker-relay` itself changes. Ordinary requests to test a target project mean that project's own tests.

## Test discipline

- Start from a clean deployed shared-skill commit. Do not patch the live checkout while a test is running.
- Use the canonical runner's `--detach` mode shown in `SKILL.md`. The runner owns its transient user service; handwritten `nohup`, `systemd-run`, raw provider CLIs, and custom wrappers are diagnostic tools only after a canonical failure has been reproduced.
- Run every live phase sequentially across separate supervisor turns. After each accepted detached launch, stop the current turn without another work call. A single terminal `sessions_yield` is allowed when exposed by the runtime, but nothing may follow it. Continue only from that worker's routed completion wake, then record its exit code, output path, provider session/thread id, run id, and wake id before starting the next phase.
- Treat each routed completion wake as the next active supervisor turn. Verify the completed phase and, if another ordered phase remains, launch exactly that next phase in the same wake turn before releasing it. Do not end with a status-only promise to continue later: without a new launch there is no next wake, so the workflow has stalled and the E2E attempt fails.
- Because canonical detach uses `systemd-run --collect`, do not treat post-completion `systemctl show` defaults as exit evidence when `LoadState=not-found`. Use the correlated wake exit metadata, runner output, or `journalctl --user -u UNIT` line `Main process exited ... status=N`. A collected unit can misleadingly display `Result=success` / `ExecMainStatus=0` after it no longer exists.
- Never use `while`, `sleep`, `wait`, `systemctl`, log polling, registry polling, or a long-running tool call to watch a detached worker. Doing so keeps the parent turn alive and invalidates both the tool-exit survival and supervisor-wake gates. Mark that attempt failed even if the worker itself succeeds.
- Treat asynchronous chat messages as correlated events, not chronological truth. Ignore delayed messages whose output/run/wake identifiers belong to an older phase.
- Do not publish a current live gap after one failure. Use the confirmation matrix below first.
- For auth, environment, routing, and process-lifetime changes, at least one behavioral trial must be launched by the real production supervising agent/session. An SSH shell or an agent asked only to explain the protocol does not reproduce inherited environment or tool-scope cleanup.

## Layer 1: deterministic local tests

```bash
python3 -m unittest discover -s {baseDir}/tests -v
python3 -m py_compile {baseDir}/run-task.py {baseDir}/session_registry.py
```

This layer must cover both engines: exit-code propagation, timeout, resume failure, stdout/stderr draining, provider-specific auth isolation, default/mode/model/Fast command construction, mode-engine validation, JSONL parsing, routing helpers, wake-state concurrency, and registry concurrency/recovery.

## Layer 2: routing validation on the target host

```bash
python3 {baseDir}/run-task.py \
  --task probe \
  --engine codex \
  --project /tmp/cc-probe \
  --session 'EXACT_LIVE_SESSION_KEY' \
  --validate-only
```

Pass only when the exact agent, channel target, and session id resolve correctly. Also test one known-invalid synthetic key and require exit `2`.

## Layer 3: real detached smoke run

Required after changes to routing, launch, lifecycle, auth, notifications, or wake delivery.

Use a harmless prompt that returns a unique marker and performs no repository edits. Launch through the normal detached path. Confirm:

1. Launcher returns `DETACHED_LAUNCH_ACCEPTED` with unit and log paths, then exits promptly; the supervisor makes no further tool call and ends that turn.
2. A routed completion wake starts the next supervisor turn; only then inspect the detached service log and terminal artifacts.
3. Launch notification reaches the source chat.
4. Output file contains the expected marker.
5. Direct completion result is visible where the channel contract requires it.
6. The exact original supervisor session receives and processes the wake.
7. Wrapper exit is `0` only when all required delivery steps succeed.
8. No `.git` directory appears in the scratch project.

For a run longer than 60 seconds, separately verify heartbeat and worker progress notification behavior. Do not make every smoke run artificially long.

For a new engine or changes to provider adapters, run one harmless fresh smoke and one resume smoke for each affected engine. Verify that the registry records the correct engine and that a deliberately invalid provider session returns `42`. For Codex, the invalid id must be syntactically valid and absent, such as a one-character UUID mutation of a completed thread id; a malformed label-like string may be interpreted as a fresh selector and is invalid test input.

### Required order for a full Codex E2E

Run these phases in order, never in parallel:

1. Deterministic tests and compilation.
2. Exact live-route validation and one known-invalid synthetic route requiring exit `2`.
3. Standard fresh `--detach` run. Treat `DETACHED_LAUNCH_ACCEPTED` as the final tool result of this supervisor turn and end it. For lifecycle changes, make this the single run longer than 60 seconds and verify survival, worker progress, and runner heartbeat.
4. Continue only in the completion-wake turn. Capture the Codex thread id from `~/.openclaw/claude_sessions.json`, verify its entry has `engine: codex`, the expected output path, and a terminal status, then launch phase 5 in this same wake turn. Do not stop after announcing that phase 5 will be launched.
5. Resume that exact thread through the same runner, then end the turn immediately after detach acceptance. In its completion-wake turn, require a unique resume marker and evidence that the initial marker was in context.
6. Run the affected explicit-mode/model smokes, if selection is in scope, only after default fresh and resume are complete; again end each launch turn and continue from its wake. A semantic-mode change must prove both default pins and every affected explicit mode sequentially.
7. Run one syntactically valid but nonexistent Codex thread id and require exit `42`; a malformed non-UUID string does not satisfy this gate. End its launch turn and inspect the failure only from its wake. Accept correlated `RESUME_FAILED` with wake `exit=42` or systemd journal `status=42`; reject post-collection `systemctl show` defaults when `LoadState=not-found`.
8. Verify direct result, accepted supervisor wake, no unexpected follow-up launch, and no `.git` in the scratch project.

Fast mode is an opt-in billing/product check. Test it only when Fast behavior changed or the user explicitly asks for it.

## Live-failure confirmation matrix

For `401`, transport/auth errors, `no rollout found`, timeout, or a missing/delayed notification:

1. Let all earlier runner/provider processes finish; do not start parallel controls.
2. Record the failing command surface, timestamp, exit code, output, thread id, and whether its rollout/registry entry exists.
3. Run one standard fresh control through the canonical runner.
4. Retry the failed case once, sequentially. For resume, retry the same id after confirming its rollout/index and also test a newly completed standard thread.
5. If the retry passes without a code/config change, classify the event as transient or test interference. Preserve evidence in the incident history, but do not list a current feature gap.
6. Call a feature persistently broken only when the same case fails repeatedly after isolation while its standard control succeeds, or when a verified deterministic defect explains it.

A raw CLI probe can help isolate runner versus provider behavior only after canonical reproduction. It does not replace the canonical pass/fail gate, and several concurrently launched raw probes are invalid evidence for an auth or state diagnosis.

Record the host, affected CLI versions, session key class (not secret tokens), result, and any failed gate in the release or incident record.
