# CHANGELOG — claude-code-task

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
