# OpenClaw Worker Relay

Run Codex CLI or Claude Code as a supervised asynchronous worker while OpenClaw keeps ownership of routing, progress, result delivery, wake-up, verification, and multi-turn continuation.

Worker Relay is deliberately distinct from two adjacent surfaces:

| Surface | Meaning |
|---|---|
| Native OpenClaw subagent | An agent created and managed inside OpenClaw |
| Direct CLI session | An explicit request to bypass Worker Relay and run `codex` or `claude` interactively |
| Worker Relay | OpenClaw supervises an external CLI worker through the complete async harness |

Any request to call, run, invoke, use, ask, or delegate to Claude or Codex selects Worker Relay, even for a small task. Native subagents and raw CLI are never inferred as substitutes.

## What it provides

- Codex and Claude Code behind one lifecycle and one `run-task.py` entrypoint
- exact originating-session routing for WhatsApp and Telegram
- launch, worker progress, heartbeat, completion, and failure notifications
- direct result delivery plus supervisor wake for autonomous continuation
- provider-native session/thread capture and resume
- explicit Terra, Sonnet, Sol, Fable, model, and Fast selection
- optional side-effect-aware provider-unavailability fallback
- subscription-auth isolation: inherited API credentials are stripped
- durable Linux detachment through a transient user `systemd` service
- deterministic tests plus a production E2E protocol

## Defaults

No provider, mode, model, Fast, or fallback flags means:

- **worker:** Codex
- **mode/model:** Terra (`gpt-5.6-terra`)
- **auth:** saved ChatGPT subscription login
- **fallback:** none

Claude must be selected explicitly with `--engine claude`; its ordinary default is the forward-compatible `sonnet` alias. Sol and Fable are explicit premium modes. Worker Relay never invents a provider fallback.

## Requirements

- OpenClaw with an addressable source session
- Linux with a working user `systemd` manager for durable `--detach` launches
- Python 3.9+
- Codex CLI and/or Claude Code installed and logged in with the intended subscription
- OpenClaw gateway configuration allowing `sessions_send`
- session visibility configured as `all`

The relevant OpenClaw configuration shape is:

```json
{
  "gateway": {
    "tools": {
      "allow": ["sessions_send"]
    }
  },
  "tools": {
    "sessions": {
      "visibility": "all"
    }
  }
}
```

Keep any existing allowed tools when applying this setting.

## Installation

```bash
git clone https://github.com/VsevolodUstinov/openclaw-worker-relay.git \
  ~/.openclaw/workspace/skills/worker-relay
```

Confirm that OpenClaw reports `worker-relay` as ready:

```bash
openclaw skills list
```

## Routed launch

Always preserve the exact source session key and validate it before launch:

```bash
SKILL_DIR="$HOME/.openclaw/workspace/skills/worker-relay"
SESSION_KEY='agent:your-agent:whatsapp:group:YOUR_GROUP_JID'

python3 "$SKILL_DIR/run-task.py" \
  --task "routing probe" \
  --project /absolute/project/path \
  --session "$SESSION_KEY" \
  --validate-only
```

Then launch the durable worker:

```bash
python3 "$SKILL_DIR/run-task.py" \
  --detach \
  --task "Implement the requested change, verify it, and report files and checks" \
  --project /absolute/project/path \
  --session "$SESSION_KEY" \
  --timeout 7200 \
  --session-label "short workstream label"
```

After `DETACHED_LAUNCH_ACCEPTED`, the supervising OpenClaw turn must end. The worker's routed completion wake starts the next supervisor turn. Do not poll the service from the launch turn; that masks the lifecycle being tested.

## Explicit selections

```bash
# Claude Code / Sonnet
python3 "$SKILL_DIR/run-task.py" --engine claude --task "..." --project /path

# Codex / Sol
python3 "$SKILL_DIR/run-task.py" --mode sol --task "..." --project /path

# Claude Code / Fable
python3 "$SKILL_DIR/run-task.py" --engine claude --mode fable --task "..." --project /path

# Availability fallback for a fresh, side-effect-safe automation
python3 "$SKILL_DIR/run-task.py" \
  --engine codex --fallback-engine claude \
  --task "..." --project /path
```

Fallback applies only when the primary provider is unavailable before useful work. It does not replay timeouts, ordinary task failures, tool-using runs, completed results, or resumed sessions.

## Safety model

Worker CLIs run unattended with their unrestricted automation flags. Use trusted prompts and project directories. The runner itself never initializes a Git repository and never forwards inherited provider API keys to subscription workers.

For exact operating rules, read [`SKILL.md`](SKILL.md). For current mechanics and known failure patterns, see [`CURRENT-BEHAVIOR.md`](CURRENT-BEHAVIOR.md), [`INCIDENT-INDEX.md`](INCIDENT-INDEX.md), and [`TECHNICAL-INSIGHTS.md`](TECHNICAL-INSIGHTS.md).

## Testing

```bash
python3 -m unittest discover -s tests -v
python3 -m py_compile run-task.py session_registry.py
```

Changes to routing, delivery, auth, or process lifetime also require the ordered clean-context and real-parent checks in [`references/testing-protocol.md`](references/testing-protocol.md).

## Migration from Claude Code Task

This project was formerly `openclaw-skill-claude-code`, and the skill was named `claude-code-task`. GitHub redirects the old repository URL, but existing clones should update their remote and folder name:

```bash
git remote set-url origin https://github.com/VsevolodUstinov/openclaw-worker-relay.git
```

Do not install independent old and new copies. `worker-relay` is the sole implementation; provider-specific runners would drift in routing and failure handling.

## License

MIT
