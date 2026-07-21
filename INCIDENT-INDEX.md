# Incident Index

Compact recognition patterns for failures that materially shaped the current contract.

| Symptom | Cause | Guardrail |
|---|---|---|
| A `nohup ... &` worker disappears when the OpenClaw shell tool returns | Tool-scope cleanup can terminate orphaned descendants | Launch only `run-task.py --detach`; do not recreate service-manager commands |
| Codex works over SSH but a routed OpenClaw launch gets `401 Missing authentication` | The parent agent supplied an alternate `CODEX_HOME` or API credential surface | Strip `CODEX_HOME` and provider API variables so Codex uses the saved user subscription |
| Several provider/model probes fail together | Parallel probes interfere through shared login, rollout, or local state | Run live provider and mode probes sequentially; establish a standard control and retry before declaring a feature gap |
| A worker completed, but the multi-phase workflow stopped after the wake | The supervisor posted a status promise instead of executing the next phase | Treat the completion wake as the next active turn; verify and launch the next ordered phase before releasing it |
| A collected service appears to have exit `0` after an invalid resume | `systemd-run --collect` removed the unit and `systemctl show` returned default-looking values | When `LoadState=not-found`, use correlated wake/output or the journal's process exit line |
| Visible result arrived, but the supervisor did not continue | Direct delivery and wake are separate outcomes | Confirm both; do not rerun successful work until the existing output and wake state are inspected |
| A syntactically plausible session key routes to the wrong owner | Routing identity was inferred from strings instead of resolved session metadata | Validate the exact source session and preserve its real agent id and delivery context |
