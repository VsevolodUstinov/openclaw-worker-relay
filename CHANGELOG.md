# Changelog

## 2.0.0 - 2026-07-20

- Renamed Claude Code Task to provider-neutral **Worker Relay**.
- Added Codex and Claude Code behind one shared async lifecycle.
- Made Codex/Terra the no-flag default; Claude/Sonnet, Sol, Fable, Fast, custom models, and fallback remain explicit.
- Added durable `run-task.py --detach` through a transient user-systemd service.
- Added exact source-session validation, direct delivery, asynchronous supervisor wake, provider-native resume, and provider-aware registry metadata.
- Added subscription-auth isolation for both providers, including removal of inherited `CODEX_HOME`.
- Added side-effect-aware provider fallback only for unavailability before useful work.
- Added 52 deterministic tests and an ordered real-context E2E protocol.
- Retired the divergent `run-task.sh`, standalone notification helper, Claude-only instructions, and handwritten `nohup` launch flow.

## Earlier releases

Versions through 1.2.x were published as `openclaw-skill-claude-code` / `claude-code-task` and supported Claude Code only. The old GitHub URL redirects to this repository.
