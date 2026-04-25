# TECHNICAL INSIGHTS — claude-code-task

## 0) 2026-04-24 Two-mode wrapper: standard + true Fast mode

The wrapper now exposes only two execution modes:

- **standard** — claude is invoked with no model flag. The CLI picks its current default.
- **fast** — wrapper requests true Claude Code Fast mode via subscription/OAuth auth.

Local finding on Claude Code 2.1.119:
- `claude -p --fast ...` still returns `unknown option '--fast'`.
- `ANTHROPIC_API_KEY=... claude -p --settings '{"fastMode":true}' --model claude-opus-4-6 ...` returns standard speed / fast not on.
- `env -u ANTHROPIC_API_KEY claude -p --settings '{"fastMode":true}' --model claude-opus-4-6 ...` returns `speed: "fast"` and `fast_mode_state: "on"` after extra usage is enabled.

Therefore every wrapper path, not just fast mode, must remove `ANTHROPIC_API_KEY` from the child process environment so Claude Code uses the logged-in OAuth/subscription session. This is a hard billing invariant to prevent accidental API spend. Fast mode should prefer native `claude -p --fast` if a future CLI exposes it; otherwise use the working settings/model fallback.

Earlier insight that remains valid:
- `--effort low` is a different mechanism (deliberation budget on the current model). The wrapper does **not** map `--fast` onto `--effort low`.

## 1) 2026-04-22 memory_search reindex root cause + tail-hang lesson
- A seemingly "hung" OpenClaw session after `read skill -> memory_search` was not model reasoning drift, but blocking local indexing triggered by `agents.defaults.memorySearch.extraPaths` containing `~/SharedWorkspace`.
- In this environment that path was enormous (~15GB / 169K files), so every `memory_search` could trigger a long full reindex instead of a quick lookup.
- The practical operator symptom was deceptive:
  - assistant looked stuck right after `memory_search`,
  - gateway stayed alive,
  - but the tool call could sit for ~16-20 minutes while SharedWorkspace indexing churned.
- Concrete evidence pattern that confirmed it:
  - gateway log showed a `memory_search` call lasting ~16 minutes,
  - `lsof` on the gateway process showed huge `.tmp-*` sqlite-related files open and growing,
  - after removing SharedWorkspace from `extraPaths`, those SharedWorkspace file descriptors disappeared and only small workspace/memory files remained.
- Durable fix in this environment:
  - set `agents.defaults.memorySearch.extraPaths` to `[]` (or keep it strictly limited to small intentional memory corpora),
  - do not point memory search at giant operational workspaces.
- Cleanup lesson:
  - interrupted indexing can strand large orphaned `.tmp-*` files and silently waste tens of GB,
  - cleanup should be part of the incident runbook after fixing the config.
- Separate but related wrapper lesson from the same incident:
  - Claude Code itself may finish substantive work and emit a result, yet `claude -p` can remain alive in a tail-hang state,
  - `run-task.py` currently detects this (`result_seen but process still alive after grace`) but in `observe` mode only warns instead of terminating,
  - this can create a second false impression that "the investigation is still running" even after the root cause is already found.

## 2) 2026-04-14 runtime compatibility lesson
- `run-task.py` must not depend on ambient third-party Python packages for basic transport.
- Real failure observed after runtime/OpenClaw update:
  - wrapper started under Python 3.14,
  - `requests` was no longer installed in that interpreter,
  - process died on import before routing validation or Claude launch.
- This is the wrong failure boundary for an orchestration script.
- Better pattern:
  - use stdlib transport (`urllib.request`) for essential HTTP paths,
  - reserve third-party dependencies for optional features only.
- Practical operator symptom:
  - `--validate-only` fails instantly with `ModuleNotFoundError: No module named 'requests'`,
  - nothing is wrong with routing itself,
  - the wrapper never reaches the routing code.
- Verification after fix:
  - `--validate-only` passes again,
  - full detached E2E launch reaches startup marker and continues normally.

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

