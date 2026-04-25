# CHANGELOG — claude-code-task

## 2026-04-24 (two-mode wrapper: standard + `--fast`)

### Changed (product simplification)

- Wrapper now exposes exactly **two execution modes**:
  - `standard` (default) — claude is invoked with **no model flag**; the CLI picks its current default.
  - `fast` (`--fast`) — wrapper requests true Claude Code Fast mode via subscription/OAuth auth.
- Removed the previous explicit `--model opus` default. The wrapper no longer pins any model in standard mode.
- Removed the prior `--fast` fail-fast guard and the unsupported-mode error message. There is no mapping to `--effort low`.
- Hard billing invariant: all Claude Code child processes are launched with `ANTHROPIC_API_KEY` stripped from the environment, so `claude-code-task` cannot accidentally burn API credits. Runs use the logged-in subscription/OAuth path.
- Fast implementation detail: prefer native `claude -p --fast` when a future CLI exposes it; on local 2.1.119 use `--settings '{"fastMode":true}' --model claude-opus-4-6` under the same stripped-env OAuth/subscription path.
- Fast mode cost guard documented: roughly **$30 per 10 minutes of continuous work**; use `--fast` only when the user explicitly asks for Fast/Fast mode/urgent speed.
- Removed the `--large-context` advisory flag and all "1M (Opus default)" / "Context: …" annotations from the launch, completion, and validate-only output. With no model pinning the annotation was misleading; the two-mode mental model is the entire wrapper surface.

### Notifications

- Launch (Markdown + HTML) and startup logs now include a single `Mode: standard|fast` line in place of the old `Model: opus | Context: …` line.
- Completion (success / timeout / stall / error) notifications include `Mode: <mode>` always; `Model: <resolved>` is appended only when Claude reports a model name via the stream-json `init`/`result` event. The wrapper never asserts a pinned model it didn't pass.
- Cost semantics tightened: standard Claude Code task completion reports `Est. cost: subscription` for the stripped-env subscription path, but Fast mode reports dollar estimates from `total_cost_usd` because it consumes paid extra usage even under subscription/OAuth auth.
- `--validate-only` output simplified: prints `mode: standard|fast` and drops the previous `model`, `context`, `fast_mode` lines.
- Automation context now tells Claude to announce any deliberate wait/sleep/backoff/rate-limit pause longer than 60 seconds before starting it, including expected duration and reason, then notify again when the wait ends.
  - Validated with a real `sleep 70` regression run (`STATUS: PASS`) and a deterministic shim test confirming the wait-notice instruction is present in the child Claude prompt.

### Files changed

- `run-task.py` — argparse, validate-only, startup log, launch (Markdown+HTML), claude_cmd assembly, completion model line.
- `SKILL.md` — Execution Modes section, Claude Code Flags section, Current Stable Behavior section.
- `CHANGELOG.md` — this entry.

## 2026-04-24 (superseded — Opus default + `--fast` fail-fast + estimated cost reporting)

### Added

- **`--model opus` always explicit in claude command.** Using the alias (not pinned version) ensures forward compatibility when Anthropic updates the Opus line without requiring code changes.
  - Live testing confirms: `opus` resolves to `claude-opus-4-7` with a 1M context window by default.

- **`--large-context` flag:** advisory annotation for long-context tasks.
  - Confirmed: `--model opus` already resolves to a model with `contextWindow=1000000` by default.
  - Flag adds "1M (explicit)" annotation to launch/completion notifications; does not change model behavior.
  - `CLAUDE_CODE_DISABLE_1M_CONTEXT` env var can be used to disable 1M context if needed (Claude Code built-in).

- **Estimated cost in completion notifications:** parsed from `total_cost_usd`, `usage`, `num_turns` in the stream-json result event.
  - Format: `~$X.XXXX est. | in:NNK out:NNK [cache↩:NNK] [cache↑:NNK] | turns:N`
  - `cache↩` = cache-read tokens; `cache↑` = cache-creation tokens (often the dominant cost component).
  - Labeled "est." to distinguish from authoritative billing (Max subscription accounting differs from per-token API cost).
  - Cost line included in success, stall, and timeout completion notifications when data is available; absent on error paths where result event never fired.

- **Resolved model name in notifications:** captured from `system/init` (init event) and `result` event in stream-json. Launch and completion notifications show the actual resolved model (e.g. `claude-opus-4-7`).

- **`--validate-only` output extended:** now shows `model`, `context`, and `fast_mode` availability alongside routing info.

### `--fast` — intentionally unsupported (fail-fast)

- **An earlier draft of this release silently mapped `--fast` → `--effort low`. That mapping has been removed.** `--effort` (deliberation budget) and `/fast` (Fast mode) are distinct mechanisms. Labeling the effort-low flag as "fast" misrepresented what the wrapper was actually doing, and was explicitly rejected.
- **True Fast mode cannot be enabled via this wrapper.** Verified empirically with claude CLI 2.1.119 under `ANTHROPIC_API_KEY` auth:
  - `claude -p --model claude-opus-4-6 --settings '{"fastMode":true}'` → `fast_mode_state: "off"`, `speed: "standard"`.
  - stream-json input `/fast on` → `"/fast isn't available in this environment."`.
  - Binary strings explicitly emit: `"Fast mode is not available in the Agent SDK"`, and `"Fast mode requires a paid subscription"` when auth is OAuth/free or `"Fast mode unavailable during evaluation..."` when auth is an API key.
  - Two preconditions fail simultaneously: (1) Fast mode requires a Claude subscription OAuth session, not `ANTHROPIC_API_KEY`; (2) Fast mode is not available in Agent SDK / non-interactive `-p` mode.
- **New behavior:** passing `--fast` prints a detailed error and exits with code 2. The user-facing message explains both preconditions and suggests `--effort low` passed directly to the claude CLI as a distinct lower-latency option the caller can choose themselves.

### Changed

- All completion notification paths (success, timeout, stall, error) updated to include `Model | Context` line and estimated cost summary.
- Launch notification updated to include `Model | Context` line.
- Startup log prints now include `Model | Context`.

### Technical details

- `parse_stream_line`: captures `model` from `system/init` event → `state["resolved_model"]`; captures `total_cost_usd`, `usage`, `duration_ms`, `num_turns` from `result` event. (`fast_mode_state` is no longer tracked — always `"off"` under this wrapper.)
- `format_cost_summary()`: new helper that formats estimated cost + token usage for completion messages.
- State dict extended with: `resolved_model`, `result_cost_usd`, `result_usage`, `result_duration_ms`, `result_num_turns`.

## 2026-04-22 (memory_search hang diagnosis + operator docs update)

### Documented
- Captured a production incident where an apparent OpenClaw "hang" after `memory_search` was caused by `agents.defaults.memorySearch.extraPaths` including `~/SharedWorkspace`, which triggered blocking reindex work on a very large tree (~15GB / 169K files).
- Documented the concrete evidence pattern for this failure mode:
  - very long `memory_search` duration in gateway logs,
  - large growing `.tmp-*` files held open by the gateway process,
  - disappearance of SharedWorkspace-related file descriptors after config correction.
- Recorded the paired operator lesson that a Claude Code run can also appear hung after finishing substantive work because `run-task.py` tail-hang detection is currently `observe`-only after `result_seen`.

### Operational impact
- Clarifies that not every post-tool "hang" is model reasoning or routing failure; local memory index scope can be the real blocker.
- Establishes that giant operational repos like SharedWorkspace must not be placed in `memorySearch.extraPaths`.
- Adds a durable reminder that incident cleanup may include deleting orphaned sqlite temp files left by interrupted indexing.

### Documentation
- Updated `TECHNICAL-INSIGHTS.md` with the new root-cause pattern, evidence checklist, and the distinction between:
  - the original OpenClaw hang root cause (`memory_search` reindex scope), and
  - the separate Claude Code wrapper tail-hang behavior after `result_seen`.

## 2026-04-14 (OpenClaw runtime compatibility fix)

### Fixed
- Restored `run-task.py` startup in environments where Python no longer has the third-party `requests` package preinstalled.
- Replaced the wrapper's HTTP dependency on `requests` with a stdlib-based transport (`urllib.request`) for:
  - gateway `/tools/invoke` calls,
  - direct Telegram Bot API sends,
  - direct Telegram message edits.
- Unblocked the canonical E2E flow again: `--validate-only` routing precheck now runs successfully instead of failing during module import.

### Technical impact
- Failure mode before fix: `ModuleNotFoundError: No module named 'requests'` at process startup, before routing validation, launch proof, or Claude execution.
- This surfaced right after an OpenClaw/runtime update that left the skill running under Python 3.14 without `requests` available.
- Result: the orchestration wrapper was more fragile than it needed to be for a system script.

### Documentation
- Added a compatibility note to `SKILL.md` warning that `run-task.py` must not rely on ambient third-party Python packages for core transport.
- Added a technical insight entry documenting the root cause and the preferred stdlib transport pattern for wrapper reliability.

## 2026-04-01 (legacy cleanup + completion-tail fix)

### Changed
- Removed legacy `Mode` / `Iteration limit` launch fields from operator-facing notifications.
- Removed legacy `--completion-mode` / `--max-iterations` CLI knobs from `run-task.py`; each Claude run is now treated as a single detached execution, and follow-up launches are decided by the waking OpenClaw agent.

### Fixed
- Post-completion crash after Claude finished successfully: leftover references to removed `completion_mode` / `iter_budget` in notify paths caused `AttributeError` and stopped heartbeat/delivery tail handling.
- Resume-path `Prompt is too long` failures: resumed runs now collapse large orchestration prompts into a short continuation form before invoking Claude, so prior session history remains the main context source.
- Verified with smoke test: routing validate PASS, run completed, wake delivered correctly.

### Documentation
- Updated `SKILL.md` / `WAKE-TROUBLESHOOTING.md` wording to remove stale references to iteration mode / iteration budget and reflect wake-driven follow-up decisions.

## 2026-03-03 (routing + e2e clarification pass)

### Added
- Explicit **Async Boundary Rule** in `SKILL.md`: after successful `nohup` launch, acknowledge and stop turn; continue only on wake/completion event.
- Explicit interpretation of "run tests" in `SKILL.md`: for this skill it means **E2E operator validation**, not `pytest`/`unittest` discovery by default.
- Guidance in E2E prompt pattern: `>4500` filler can be used for useful creative/output-generating work (not meaningless junk).

### Changed
- Removed legacy `completion mode` / `max iterations` operator-facing launch fields and CLI knobs; follow-up launches are now decided by the waking OpenClaw agent.
- Added smart stall guard in built-in shadow mode (no new launch parameters):
  - semantic no-progress detector + flat-heartbeat heuristic
  - post-result tail-hang detector with short grace window
  - sends "would terminate" notifications for data collection without killing the process
- `run-task.py` thread id extraction now supports both key formats:
  - `agent:main:main:thread:<thread_id>`
  - `agent:main:main:thread:<sender_id>:<thread_id>`
  (uses last segment as thread/topic id in composite form).
- Telegram wake dispatch hardening in `run-task.py`:
  - increased subprocess timeout (`40s -> 90s`)
  - added one retry before fallback send path.

### Fixed
- False routing failures when session key used composite thread format.
- Frequent wake timeout flakes caused by short wake subprocess timeout budget.

### Documentation
- Added/updated testing guidance to make E2E validation intent explicit and prevent "Ran 0 tests" false positives.
- Clarified async execution expectations to avoid same-turn waiting after detached launch.
- Refined SKILL frontmatter `description` to be trigger-rich with explicit NOT-for boundaries and explicit mention of strict routing + E2E validation workflow.
- Linked "run tests" section directly to `references/testing-protocol.md` as canonical protocol.
- Added **Launch Confirmation Gate** rule to prevent false "launched" acknowledgments before process/log/routing proof is present.
- Added mandatory pre-launch planning note: agent must state plan, expected result, assumptions/questions, and one-iteration vs phased intent before launch.
- Added `references/testing-protocol.md` with mandatory launch-proof and async completion semantics.

## 2026-02-28 (stabilization pass)

### Added
- `--trace-live` mode in `run-task.py` for live technical thread traces.
- Wake payload correlation fields: `run_id`, `wake_id`.
- Per-project orchestrator state file: `/tmp/cc-orchestrator-state-<hash>.json`.
- Wake dedupe guard to skip duplicate/stale dispatches.

### Changed
- Telegram wake now uses `openclaw agent --deliver` so continuation turns are visible in chat.
- Wake wording shifted to same-agent continuity (same session, same conversation).
- Explicit continuity rule: evaluate against original user goal, not only last sub-step.
- No-silent-launch policy: visible decision turn required before launching next iteration.
- Launch notification now always includes `Resume: <session-id|new>`.

### Fixed
- Missing visible continuation turns in chat after Claude completion (delivery path).
- `session_registry.py` label lookup null-safe handling (`label=None` entries).

### Documentation
- SKILL.md updated with deterministic wake guard + no-silent-launch policy.
- WAKE-TROUBLESHOOTING.md updated for current `--deliver` behavior and stale wake skip diagnostics.
- Added critical resume rule: use only `📝 Session registered: <session-id>` value for `--resume`.
