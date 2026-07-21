# Current Behavior

Last synchronized with the canonical Worker Relay implementation on 2026-07-20.

## Identity

- Skill: `worker-relay`
- Entrypoint: `run-task.py`
- OpenClaw is the supervisor; Codex and Claude Code are external workers.
- Native OpenClaw subagents and direct interactive CLI sessions are separate surfaces.
- The old `claude-code-task` name is retired; new callers use Worker Relay.

## Execution

- Omitted engine means Codex only, with no fallback.
- Omitted mode means Terra for Codex and Sonnet for explicitly selected Claude.
- Sol, Fable, Fast, custom model, and provider fallback require explicit compatible flags.
- Durable routed launches use `--detach`; the runner creates the transient user-systemd service.
- The runner creates a missing project directory but never creates a Git repository.
- Codex and Claude use their saved subscription identities; inherited API credentials are removed.

## Routing and completion

- Routed launches resolve the exact source OpenClaw session before starting a worker.
- Direct result delivery and supervisor wake are independently confirmed.
- WhatsApp supervisor wakes use asynchronous `sessions_send` acceptance.
- Provider session/thread ids are stored with engine, semantic mode, and selected model.
- Resume ids cannot cross providers.
- Exit `42` means resume failure, `70` runner crash, `75` delivery/wake failure after successful work, and `124` timeout. Other child exit codes propagate.

## Compatibility state

The registry filename (`~/.openclaw/claude_sessions.json`), `CC_*` environment overrides, `cc-task-*` service names, and `/tmp/cc-*` artifacts remain stable compatibility surfaces despite the product rename.
