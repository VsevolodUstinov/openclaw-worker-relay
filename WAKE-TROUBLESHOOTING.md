# Wake Troubleshooting

Start with `CURRENT-BEHAVIOR.md` and `INCIDENT-INDEX.md`. Do not relaunch blindly: first determine whether Claude failed, delivery failed, or only the supervisor wake failed.

Set the installed skill path once for the commands below:

```bash
SKILL_DIR="${OPENCLAW_WORKSPACE:-$HOME/.openclaw/workspace}/skills/worker-relay"
```

## 1. Identify the run

```bash
ls -lt /tmp/cc-*.txt /tmp/cc-*.log 2>/dev/null | head
find "$SKILL_DIR/pids" \
  -type f -maxdepth 1 -print -exec sed -n '1,3p' {} \;
ps -eo pid,ppid,etime,command | grep -E 'run-task.py|claude -p' | grep -v grep
```

Unique output names contain timestamp plus a run-id prefix. Match the task, project, PID, and output path before touching a process.

## 2. Inspect completion state

```bash
tail -n 120 /tmp/cc-launch.log
cat /tmp/cc-YYYYMMDD-HHMMSS-RUNID.txt
```

Interpretation:

- No startup marker: launch or routing failed before Claude started.
- Output exists and child is gone: inspect wrapper exit/delivery messages.
- Stream result exists but child remains: post-result tail recovery should terminate it after the configured grace.
- Exit `42`: old provider session/thread could not be resumed; relaunch fresh only when exact continuity is not required and keep the same `--engine`.
- Exit `75`: work succeeded but delivery/wake was not confirmed; do not rerun the work until you inspect the output.
- Exit `124`: timeout; inspect partial output before deciding whether to resume or start fresh.

## 3. Validate the exact route

```bash
python3 "$SKILL_DIR/run-task.py" \
  --task probe \
  --project /tmp/cc-probe \
  --session 'EXACT_SESSION_KEY' \
  --validate-only
```

For WhatsApp sessions, preserve the actual agent component instead of substituting `main`. Verify current keys through OpenClaw session metadata; do not repair a failure by changing only the group JID.

## 4. Check gateway and local session evidence

```bash
openclaw status
find ~/.openclaw/agents -path '*/sessions/sessions.json' -type f -print
journalctl --user -u openclaw-gateway --since '30 minutes ago' --no-pager | tail -n 250
```

Look for gateway tool errors, session lookup failure, `sessions_send` timeout, and delivery provider errors. HTTP 200 is not enough; inspect the JSON `ok`, `error`, and `result.isError` fields.

## 5. Safe retry rules

- If only delivery failed, use the existing output; do not pay for the same worker task again.
- If the wake failed before commit, its in-flight claim is released and the wake may be retried.
- If another wake for the same output is currently in flight, wait rather than creating a duplicate.
- Never kill the active `run-task.py` that is waiting for its own supervisor wake to return.
- Resume only with the logged session/thread id from `~/.openclaw/claude_sessions.json`, and require its `engine` to match the launch.

## 6. Registry recovery

```bash
python3 "$SKILL_DIR/session_registry.py" list
ls -l ~/.openclaw/claude_sessions.json*
```

Writes are locked and atomic. If JSON corruption is encountered, the original is retained as `.corrupt-<timestamp>` and a new empty registry is created. Preserve that backup until the incident is understood.

## Escalation evidence

Capture the exact session key, project, runner PID/exit code, output path, wrapper log tail, gateway log window, and whether direct delivery was visible. Add a compact incident-index row only after the root cause is verified.
