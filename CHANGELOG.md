# Changelog

## 2.0.1 - 2026-07-20

- Removed fallback from default validation and launch examples; explicit Claude/Codex requests now remain single-provider unless cross-provider continuity is explicitly requested.
- Clarified that `run-task.py` is invoked through `python3` and does not require an executable bit.
- Added first-read contract regressions for both rules; the deterministic suite now covers 53 cases.
- Made the selected external provider own the delegated result; validation-only supervisors no longer answer the unlaunched task inline.
- Passed `3/3` consecutive independent production-shaped Iris contexts, including explicit Claude and adversarial native-subagent wording, with zero failed tool calls.

## 2.0.0 - 2026-07-20

- Renamed Claude Code Task to provider-neutral **Worker Relay**.
- Added Codex and Claude Code behind one shared async lifecycle.
- Made Codex/Terra the no-flag default; Claude/Sonnet, Sol, Fable, Fast, custom models, and fallback remain explicit.
- Added durable `run-task.py --detach` through a transient user-systemd service.
- Added exact source-session validation, direct delivery, asynchronous supervisor wake, provider-native resume, and provider-aware registry metadata.
- Added subscription-auth isolation for both providers, including removal of inherited `CODEX_HOME`.
- Added side-effect-aware provider fallback only for unavailability before useful work.
- Added 51 deterministic tests and an ordered real-context E2E protocol.
- Retired the divergent `run-task.sh`, standalone notification helper, Claude-only instructions, and handwritten `nohup` launch flow.
- Made every explicit request to invoke Claude/Codex trigger Worker Relay regardless of task size; raw CLI now requires an explicit bypass request.

## Earlier releases

Versions through 1.2.x were published as `openclaw-skill-claude-code` / `claude-code-task` and supported Claude Code only. The old GitHub URL redirects to this repository.
