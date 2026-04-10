# TECHNICAL INSIGHTS — claude-code-task

## 1) What caused "extra" wake/agent replies
- Root cause was event overlap + manual mid-cycle interventions.
- With `--deliver`, every wake turn becomes visible, so delayed/stale wakes are now observable.
- Fix: dedupe dispatch by `wake_id` and output file in per-project state.

## 2) Why visibility and continuity both matter
- If wake is hidden, user loses chain-of-thought of orchestration decisions.
- If wake is visible but unstructured, chat gets noisy and confusing.
- Current compromise:
  - technical events via `[TRACE][TECH]...`
  - semantic decision via `[TRACE][AGENT][WAKE_RECEIVED]` + decision turn.

## 3) No-silent-launch is critical
- Without a mandatory visible decision turn, step2 can appear to start "magically".
- Policy now requires:
  1. visible interpretation/decision,
  2. only then launch next iteration.

## 4) Resume pitfalls (most frequent operator error)
- `run_id` and `wake_id` are not resume ids.
- `--resume` must use the Claude session id logged as:
  `📝 Session registered: <session-id>`.

## 5) Known practical pattern
- Keep run-task as deterministic transport/orchestrator.
- Keep planning/decision in the main agent session.
- Let Claude runs be short and explicit, one iteration at a time.

## 6) Recommended live-debug setup
- Turn on reasoning stream in chat.
- Launch with `--trace-live`.
- Watch for strict sequence:
  - `RUN_TASK START`
  - Claude complete
  - `WAKE`
  - `WAKE_RECEIVED` + decision
  - optional next launch

## 7) 2026-04-01 additions (legacy cleanup + completion-tail failure)
- Removing legacy launch fields is not enough; all post-completion notify paths must be audited too.
- Failure pattern observed in production:
  - Claude run completed successfully,
  - output file was written,
  - then `run-task.py` crashed on a stale `args.completion_mode` reference,
  - resulting in heartbeat stopping and apparent "Claude stalled" confusion even though Claude had already finished.
- Practical lesson:
  - launch-surface cleanup must be paired with tail-path cleanup (`normal completion`, `resume failure`, `crash notify`).
- Operator symptom to recognize:
  - heartbeats suddenly stop,
  - completion output file already exists,
  - log shows success line followed by Python exception in notification/wake path.
- Verification that fixed it:
  - smoke run completed end-to-end,
  - wake delivered normally,
  - no post-completion AttributeError remained.
- Resume-specific lesson:
  - `--resume` should not reuse a full, verbose orchestration prompt.
  - prior Claude session history is already present, so adding another long control prompt can trigger `Prompt is too long` before useful work starts.
  - resumed launches should use a compact continuation instruction instead of replaying the entire task framing.

## 8) 2026-03-03 additions (operator behavior + routing reliability)
- E2E intent must be explicit in SKILL docs:
  - "run tests" for this skill means operator E2E flow (routing→launch→heartbeat→completion), not `pytest`/`unittest` discovery.
- Async boundary must be explicit:
  - after detached `nohup` launch, agent should acknowledge and stop turn; continuation should happen on wake.
  - same-turn waiting causes false negatives (agent "didn't see" completion until later wake).
- Session key format reality:
  - thread keys may appear as `agent:main:main:thread:<sender_id>:<thread_id>`.
  - parsing must tolerate composite form and use last segment as thread/topic id.
- Wake delivery is flaky under tight timeout budget:
  - 40s was too short in practice; increased timeout and one retry reduced transient wake failures.
- Practical testing insight:
  - >4500-char prompt requirement can be used for useful creative output while still validating Telegram quote truncation/collapse behavior.

