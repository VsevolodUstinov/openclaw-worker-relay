# Wake / Continue Chain — Troubleshooting Guide

Practical diagnostics for the most common failures in the wake/continue pipeline
(`run-task.py` → channel notification → agent wake → agent turn).

---

## Quick Triage Checklist (60 seconds)

Run top-to-bottom. Stop at the first match and jump to the linked item.

1. **Script exited immediately?**
   `tail /tmp/cc-run.log`
   - `❌ Invalid routing` → **item 6** (session key or routing metadata unresolved)
   - `❌ Unsafe routing blocked` → **item 9** (routing mode mismatch)
   - No log file → re-launch with `nohup ... > /tmp/cc-run.log 2>&1 &`

2. **No result notification at all in the channel?**
   Look for `✅ Claude Code completed` / `❌` / `⏰` in log.
   - Missing → task still running (`ls pids/`) or crashed (check full log)
   - Present but HTTP 400 in log → **item 7** (Markdown bold in finish message)

3. **Result appeared in main chat instead of thread?**
   Search log for `thread_id=None` or `stripping thread_id` → **item 3** (thread extraction failed)

4. **Agent wake attempt missing from log?**
   Search for `🤖 Waking agent session:` in log.
   - Line absent → `--session` key not matched to a wake path; verify key format
   - Present but UUID is `None` → **item 4** (session UUID not resolved)

5. **UUID looks wrong or "not resolved"?**
   `openclaw sessions list | grep <thread_id>`
   - Returns nothing → session expired; see **item 4**
   - Returns a UUID → pass it via `--notify-session-id <uuid>` to override

6. **`openclaw agent` exited 0 but no agent response appeared?**
   Session was locked/busy → **item 5** (expected if result was already delivered)

7. **Double message (raw result + agent repeat)?**
   → **item 2** (`--deliver` is enabled by design; tune wake prompt/output style)

8. **Mid-task `📡 🟢 CC:` updates missing?**
   `grep "cc-notify" /tmp/cc-run.log`
   - Not found → script not written before CC launch; check task prompt → **item 8**
   - Found but CC never called it → add explicit notify instruction to prompt

9. **Iterative loop stopped too early?**
   Read the agent's last turn output — says "done" instead of re-launching?
   → **item 10** (verify wake continuation logic and that the agent intentionally launched a follow-up run)

10. **Heartbeats in wrong chat?**
    Search log for `heartbeat` with `thread_id=0` or `thread_id=None` → **item 12**

11. **`list_recent_sessions` returns nothing?**
    `python3 -c "from session_registry import list_recent_sessions; print(list_recent_sessions(hours=168))"`
    Still empty → **item 11** (timestamp window or status field mismatch)

12. **Wake was expected but did not fire?**
    In `--trace-live` mode check for dedupe skip marker:
    `[TRACE][TECH][TELEGRAM][WAKE][SKIP] duplicate/stale wake ignored ...`

13. **None of the above matched?**
    Validate routing without launching:
    ```bash
    python3 run-task.py --task "probe" --project /tmp/x \
      --session "<your-session-key>" --validate-only
    ```

---

## 1. Agent never wakes after task completion

**Symptom:** Result appears in Telegram/WhatsApp but the agent sends no summary.

**Check:**
- Telegram: `openclaw agent --session-id <uuid>` must be called with the correct UUID.
  Look for `"🤖 Waking agent session: <uuid>"` in the wrapper log (`/tmp/cc-run.log`).
- WhatsApp: `sessions_send` is used instead — verify it's not in the HTTP deny list.
- If session UUID was `None` at wake time, resolution failed — see item 4.

**Fix:** Add `--notify-session-id <uuid>` explicitly if auto-resolution is unreliable.

---

## 2. Double message in Telegram (result + agent repeat)

**Symptom:** Thread shows both the raw result block AND a long/duplicative wake continuation.

**Root cause:** Current default intentionally uses `openclaw agent --deliver` for Telegram wake,
so continuation turns are visible in chat. If wake text is too verbose, it may feel like duplication.

**Fix options (in order):**
1. Keep `--deliver` and shorten/structure wake prompts (preferred).
2. Ensure wake continuation reacts to the completed result instead of restating full payload.
3. If product requirements change, disable `--deliver` and switch back to explicit `message(action=send)` + `NO_REPLY` flow.

---

## 3. Result lands in main chat instead of thread

**Symptom:** Notification appears in the DM main chat, not in the expected thread.

**Common causes:**
- `send_telegram_direct()` called without `thread_id` (extraction failed from session key).
- `replyTo` + `message_thread_id` combined → Telegram rejects → fallback strips `thread_id`.
- Session key was `agent:main:telegram:user:<id>` (user-scope) instead of `:thread:<id>`.

**Check in log:** Look for `"thread_id=None"` or `"stripping thread_id (replyTo conflict)"`.

**Fix:** Verify `extract_thread_id(session_key)` returns non-None. Never combine `replyTo`
with `message_thread_id`.

---

## 4. Session UUID not resolved — wake skipped

**Symptom:** Log shows `"⚠️ Could not resolve session UUID — skipping agent wake"`.

**Resolution order used by `run-task.py`:**
1. `--notify-session-id` flag (explicit override)
2. `sessions_list` API response for the session key
3. Local session files: `~/.openclaw/agents/main/sessions/*-topic-<thread_id>.jsonl`

**Debug steps:**
```bash
# Check if API returns the session
openclaw sessions list | grep <thread_id>

# Check local fallback files
ls ~/.openclaw/agents/main/sessions/ | grep topic-<thread_id>
```
If both are empty, the session may be expired or never created. Start a fresh session.

---

## 5. Agent wake fails silently — session locked/busy

**Symptom:** `openclaw agent --session-id <uuid>` exits 0, but no agent response appears.

**Cause:** Session is active/locked (another turn is in progress). The agent was already
running — `already_sent=True` is set after the direct `send_telegram_direct()` call, so
no duplicate is sent.

**Fix:** This is expected behavior. If the result was already delivered via `send_telegram_direct`,
no action needed. If the agent summary is critical, retry after the active turn completes.

---

## 6. `❌ Invalid routing` on launch

**Symptom:** `run-task.py` exits immediately with `❌ Invalid routing` before Claude starts.

**Meaning:** The session key contains `:thread:<id>` but Telegram target + session UUID
could not be resolved (API and local file fallback both failed).

**Checklist:**
- Is `--session` key correct? (e.g., `agent:main:main:thread:369520`)
- Is the session active? Try `openclaw sessions list`
- Does `~/.openclaw/agents/main/sessions/` contain a file matching `topic-<thread_id>`?
- Try `--validate-only` to see what was resolved without launching Claude:
  ```bash
  python3 run-task.py --task "probe" --project /tmp/x \
    --session "agent:main:main:thread:<ID>" --validate-only
  ```

---

## 7. Telegram message silently not delivered (HTTP 400)

**Symptom:** No error in log, but message never appears in thread.

**Most likely cause:** `parse_mode="Markdown"` with `**bold**` syntax.
Telegram MarkdownV1 rejects `**text**` — result is HTTP 400 with no delivery.

**Fix:** Use `parse_mode="HTML"` for finish messages that may contain bold/code formatting.
Heartbeats and mid-task updates should use `parse_mode=None` (plain text).

---

## 8. `📡 🟢 CC:` mid-task updates not arriving

**Symptom:** Claude Code runs but no mid-task progress notifications appear in thread.

**Causes:**
- Claude Code did not call the notify script (task prompt didn't reference it).
- Script file `/tmp/cc-notify-<pid>.py` was cleaned up prematurely.
- Bot token or `thread_id` in the script resolved incorrectly at write time.

**Check:**
```bash
# Confirm script was written before CC started
grep "cc-notify" /tmp/cc-run.log

# Inspect the script directly (written before CC launch)
cat /tmp/cc-notify-<pid>.py
```
**Note:** The `[Automation context: ...]` block is prepended to the task automatically.
If Claude ignored it, add an explicit instruction: `"Use python3 /tmp/cc-notify-*.py 'msg' to send updates"`.

---

## 9. Routing blocked: `❌ Unsafe routing blocked`

**Symptom:** Script exits with `❌ Unsafe routing blocked` even though session key looks correct.

**Cause:** Default `--telegram-routing-mode auto` blocks:
- `agent:main:telegram:user:<id>` (user-scope key for thread tasks)
- Non-thread launch when a recent thread session exists for the same target

**Fix options:**
- Use correct thread key: `agent:main:main:thread:<ID>`
- For non-thread Telegram deployments: `--telegram-routing-mode allow-non-thread`
- Force thread-only strictly: `--telegram-routing-mode thread-only`

---

## 10. Iterative wake loop does not continue

**Symptom:** Agent wakes, produces a summary, but does NOT re-launch a follow-up Claude run when more work was expected.

**Check:**
- Confirm the wake analysis actually concluded that more work remains.
- Confirm the agent was expected to launch a follow-up run, not just summarize and stop.
- Verify continuation turn actually received the wake payload for this run.

**Debug:** Read the continuation turn output — it should contain gap analysis and a
re-launch decision. If it says "done", inspect whether the result actually closed all requirements.

**Fix:** Make the wake prompt explicitly state when a follow-up run is expected, and ensure the
next-step gap explicit in the wake continuation summary.

---

## 11. Session registered but `list_recent_sessions` returns nothing

**Symptom:** `~/.openclaw/claude_sessions.json` has the entry, but `list_recent_sessions(hours=72)`
returns an empty list.

**Cause:** Registry entry `started_at` timestamp is outside the `hours` window, or the status
field is not `completed`/`running` (check for `failed`).

**Fix:**
```python
# Broaden the window
from session_registry import list_recent_sessions
recent = list_recent_sessions(hours=168)  # 7 days

# Or inspect raw JSON
import json, pathlib
data = json.loads(pathlib.Path("~/.openclaw/claude_sessions.json").expanduser().read_text())
print(list(data.values())[-3:])  # last 3 entries
```

---

## 12. Heartbeat pings appear in main chat (not thread)

**Symptom:** `⏳ Claude Code running…` messages land in main Telegram DM, not in thread.

**Cause:** Same as item 3 — `thread_id` not passed to `send_telegram_direct()` for heartbeat calls.

**Verify:** Search log for `heartbeat` lines and confirm `thread_id=<N>` is non-zero.
If zero, `extract_thread_id(session_key)` returned `None` at heartbeat call time.

---
