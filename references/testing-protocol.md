# Testing Protocol (E2E, operator-facing)

For this skill, "run tests" means validating end-to-end routing + launch + delivery behavior.

## 1) Routing precheck (Telegram thread)

Run:

```bash
python3 {baseDir}/run-task.py \
  --task "probe" \
  --project /tmp/cc-probe \
  --session "agent:main:main:thread:<THREAD_ID>" \
  --validate-only
```

Pass: `✅ Routing validation` and resolved target/session are correct.

## 2) Launch smoke run

Launch detached with prompt file + nohup.

## 3) Launch confirmation gate (mandatory)

Do not report "launched" until all checks pass:
- PID returned,
- process alive (`ps -p <PID>`),
- log contains startup marker (`🔧 Starting Claude Code...`),
- Telegram routing precheck already passed.

## 4) Runtime signals

Expect in source thread:
- launch notification,
- heartbeat for >60s runs,
- optional `📡` mid-task update,
- completion message.

## 5) Completion semantics

`run-task.py` is async orchestration:
- after confirmed launch, acknowledge briefly and stop turn,
- continue only on wake/completion event.

## PASS criteria

PASS only if routing, launch confirmation gate, and completion delivery all succeed in the same session/thread.
