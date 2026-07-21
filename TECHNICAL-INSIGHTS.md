# Technical Insights

These are current engineering invariants, not a chronological log. See `INCIDENT-INDEX.md` for failures and `history/` for evidence.

## Routing is identity, not string parsing

A channel-looking key is insufficient. Direct message delivery can succeed while the intended supervisor session does not exist. Resolve the exact session and its delivery context first; parsing is only for extracting a route from verified metadata or a known key format.

Do not silently substitute another agent id. A worker must wake the exact live supervisor that launched it unless the caller explicitly chooses a different verified session.

## Delivery is a transaction

Worker completion, visible result delivery, and supervisor wake are separate outcomes. Report them separately and never reduce success to HTTP status alone. Gateway calls can return HTTP 200 while the tool payload says failure.

For large persistent sessions, `sessions_send` with its default nonzero timeout waits for the target agent turn and can outlive an HTTP client. Use `timeoutSeconds: 0` for an asynchronous handoff and require a parsed `accepted` or other non-error tool result. The direct result remains the human-visible completion guarantee; the accepted wake is the continuation guarantee.

Dedupe must be claim/commit, not check/mark: atomically claim a wake, perform the slow action, commit only after visible delivery, and release the claim on failure. This keeps retries possible without concurrent duplicates.

## Process status is an API

Scheduled callers depend on process exit codes even when humans mostly observe chat messages. Preserve the selected worker's child code, distinguish timeout and resume failure, and use a dedicated code for notification failure after successful work.

Read stdout and stderr concurrently. Pipe capacity is finite; waiting to read stderr until a verbose child exits can deadlock the entire wrapper.

A stream result is stronger evidence of task completion than a lingering child process. After a short grace period, a post-result tail hang may be terminated while preserving the emitted result.

Shell detachment is not process-manager detachment. OpenClaw may clean up descendants when a bash tool scope exits, so `nohup ... &` can disappear without a normal runner terminal path. The canonical runner must move durable work into a user service itself; agents must not reconstruct service-manager commands or keep an expensive tool turn open with `wait`.

## The runner does not own repository topology

Creating a project directory is a convenience; creating `.git` changes ownership and backup semantics. The wrapper must never initialize or nest repositories. Repository setup belongs to the caller or project bootstrap.

## Subscription auth is explicit and provider-specific

The server may contain API credentials for unrelated tooling. Worker tasks intentionally use logged-in subscriptions: strip `ANTHROPIC_API_KEY` from every Claude child path and strip `OPENAI_API_KEY`, `CODEX_API_KEY`, `CODEX_ACCESS_TOKEN`, and inherited `CODEX_HOME` from Codex. `CODEX_HOME` matters only inside some real parent agents, which is why an SSH-only smoke can pass while the routed worker fails. Codex must fall back to the user's saved `~/.codex` ChatGPT login. Detached Node CLIs also need the resolved CLI's directory in `PATH`, because an absolute `/usr/bin/env node` shim alone is insufficient.

Semantic modes are provider-specific but the opt-in contract is shared. Default runs pin stable everyday choices, Terra for Codex and the forward-compatible Sonnet alias for Claude. Sol and Fable require explicit `--mode` selections, validate against their owning engine, and never arise from fallback or inference. Fast is a separate provider product mode, not an alias for low reasoning effort.

## One lifecycle, narrow provider adapters

Routing, launch notices, progress helpers, heartbeat, timeout/stall handling, result delivery, supervisor wake, output persistence, and exit semantics must remain shared. Provider branches belong only at the boundaries: auth environment, binary discovery, command construction, stream parsing, final-text extraction, and resume-error recognition. A second runner would inevitably drift in the failure paths that matter most.

Session identity is namespaced by the recorded `engine`, even though the legacy registry filename is retained for compatibility. Reject a known cross-engine id before launch; never ask one provider to interpret the other's session id.

Provider fallback is a failure classifier, not `primary || secondary`. Retry only when the primary is unavailable before useful work, and suppress its terminal wake so the secondary remains the single acceptance path. A timeout, tool call, completed result, or ordinary task failure may already have side effects and must never trigger an automatic second agent. Keep fallback fresh-only because provider session ids and explicit model names are not portable across engines.

Defaults must be useful without being surprising. The no-flag runner path is Codex/Terra only; selecting Claude, Sol, Fable, a custom model, Fast, or any provider fallback requires an explicit flag. Explicit Claude without a mode means Sonnet. Production automations may deliberately choose explicit Codex-to-Claude fallback, but that policy belongs to each caller rather than the global default.

## Live E2E evidence needs isolation

Provider CLIs share mutable login, rollout, and local registry state. Parallel model/auth/resume probes can fail together and make independent features look broken. A live failure becomes durable current truth only after all earlier processes have exited, a standard control has established the baseline, and the same case has failed again sequentially with matching persisted-state evidence.

The canonical runner is part of the behavior under test. Replacing its documented detached launch with `systemd-run`, raw CLI calls, or handwritten orchestration changes the experiment. Raw probes are useful only after canonical reproduction and must be reported as diagnostics, not substituted for the E2E result.

## State files need filesystem semantics

JSON read-modify-write is unsafe under cron and overlapping tasks. Use a lock around the transaction and atomic replace for persistence. Preserve corrupt input as evidence before recovery instead of silently deleting the only clue.

## Instructions should follow loading priority

Agents need the current contract and incident recognition patterns after compaction. They do not need every old release narrative in the forced context. Keep:

- `SKILL.md`: decisions and operating workflow;
- `CURRENT-BEHAVIOR.md`: exact current contract;
- `INCIDENT-INDEX.md`: compact failure memory;
- `TECHNICAL-INSIGHTS.md`: durable principles;
- `history/`: detailed evidence loaded only during diagnosis.
