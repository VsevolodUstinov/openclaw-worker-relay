#!/usr/bin/env python3
"""
Run a Claude Code or Codex task in background and notify the originating session.
Zero OpenClaw tokens while the external coding agent works.

Usage:
  python3 run-task.py --detach -t "Build X" -p ~/projects/x -s "SESSION_KEY"

Resume previous session:
  python3 run-task.py --detach -t "Continue with Y" -p ~/projects/x -s "SESSION_KEY" --resume <session-id>

Features:
  - Optional provider-unavailability fallback to a second engine
  - Session resumption: continue previous provider sessions/threads
  - Session registry: automatic tracking in ~/.openclaw/claude_sessions.json
  - Session labels: human-readable names for easier tracking
  - Heartbeat pings every 60s to WhatsApp group (extracted from session key)
  - Timeout with graceful kill + notification
  - PID file for tracking running tasks
  - Crash-safe: notify on any failure
  - Stale process cleanup
"""

import argparse
import contextlib
import fcntl
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import time
import threading
from collections import deque
from datetime import datetime
from pathlib import Path

import uuid
import hashlib
from typing import Optional
from urllib import request as urllib_request, error as urllib_error

# Import session registry
try:
    from session_registry import peek_session, register_session, update_session
except ImportError:
    # Fallback if not in same directory
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "session_registry",
        Path(__file__).parent / "session_registry.py"
    )
    session_registry = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(session_registry)
    peek_session = session_registry.peek_session
    register_session = session_registry.register_session
    update_session = session_registry.update_session

CONFIG_PATH = Path.home() / ".openclaw" / "openclaw.json"
GW_URL = "http://localhost:18789"
# PID files stored next to this script (in pids/ subdirectory)
PID_DIR = Path(__file__).parent / "pids"
DEFAULT_TIMEOUT = 7200  # 2 hours
STALL_IDLE_TIMEOUT = 1200  # 20 min without semantic progress
STALL_POST_RESULT_GRACE = int(os.environ.get("CC_STALL_POST_RESULT_GRACE", "30"))  # 30s after result seen
RESUME_FAILED_EXIT_CODE = 42
ORCHESTRATION_FAILED_EXIT_CODE = 75
CRASH_EXIT_CODE = 70
TIMEOUT_EXIT_CODE = 124
POLL_INTERVAL = float(os.environ.get("CC_POLL_INTERVAL", "5"))
MAX_STREAM_LINES = int(os.environ.get("CC_MAX_STREAM_LINES", "50000"))


def fmt_duration(seconds: int) -> str:
    """Format seconds as human-readable duration."""
    if seconds < 60:
        return f"{seconds}s"
    m = seconds // 60
    return f"{m}min"


def get_token():
    return json.loads(CONFIG_PATH.read_text())["gateway"]["auth"]["token"]


BG_PREFIX = "📡 "  # Visual marker for background (non-agent-waking) messages

# Notification overrides
# If not set, channel/target are auto-detected from session key and session metadata.
NOTIFY_CHANNEL_OVERRIDE = None
NOTIFY_TARGET_OVERRIDE = None


def extract_group_jid(session_key: str) -> Optional[str]:
    """Extract WhatsApp group JID from session key (e.g. agent:main:whatsapp:group:123@g.us)."""
    if not session_key:
        return None
    for part in session_key.split(":"):
        if "@g.us" in part:
            return part
    return None


def extract_thread_id(session_key: str) -> Optional[str]:
    """Extract Telegram thread ID from simple and composite session keys.

    OpenClaw has emitted both ``*:thread:<thread_id>`` and
    ``*:thread:<sender_id>:<thread_id>`` forms. The routable thread is the last
    segment in either representation.
    """
    if not session_key:
        return None
    parts = session_key.split(":")
    for i, part in enumerate(parts):
        if part == "thread" and i + 1 < len(parts):
            return parts[-1]
    return None


def detect_channel(session_key: str):
    """Return (channel, target) for notifications based on session key or CLI overrides."""
    # Explicit internally-resolved overrides take priority
    if NOTIFY_CHANNEL_OVERRIDE and NOTIFY_TARGET_OVERRIDE:
        return NOTIFY_CHANNEL_OVERRIDE, NOTIFY_TARGET_OVERRIDE
    # WhatsApp: extract JID from session key
    jid = extract_group_jid(session_key or "")
    if jid:
        return "whatsapp", jid
    # Default: no notification target known
    return None, None


def build_whatsapp_group_session_key(base_session_key: str, group_jid: str) -> Optional[str]:
    """Build whatsapp group session key preserving agent id from base session.

    Example:
      base:  agent:assistant:main
      group: 120...@g.us
      ->     agent:assistant:whatsapp:group:120...@g.us
    """
    if not base_session_key or not group_jid:
        return None
    parts = base_session_key.split(":")
    if len(parts) < 2:
        return None
    if parts[0] != "agent":
        return None
    agent_id = parts[1]
    return f"agent:{agent_id}:whatsapp:group:{group_jid}"


class _SimpleResponse:
    def __init__(self, status_code: int, text: str, headers: Optional[dict] = None):
        self.status_code = status_code
        self.text = text
        self.headers = headers or {}

    def json(self):
        return json.loads(self.text)


def http_post(url: str, *, headers: Optional[dict] = None, json_body=None, timeout: int = 20) -> _SimpleResponse:
    data = None
    final_headers = dict(headers or {})
    if json_body is not None:
        data = json.dumps(json_body).encode("utf-8")
        final_headers.setdefault("Content-Type", "application/json")
    req = urllib_request.Request(url, data=data, headers=final_headers, method="POST")
    try:
        with urllib_request.urlopen(req, timeout=timeout) as resp:
            body = resp.read().decode("utf-8", errors="replace")
            return _SimpleResponse(resp.status, body, dict(resp.headers))
    except urllib_error.HTTPError as e:
        body = e.read().decode("utf-8", errors="replace")
        return _SimpleResponse(e.code, body, dict(getattr(e, "headers", {}) or {}))


def _invoke_tool(token: str, tool: str, args: dict, timeout: int = 20) -> Optional[dict]:
    """Invoke OpenClaw tool via gateway; return parsed JSON or None."""
    try:
        resp = http_post(
            f"{GW_URL}/tools/invoke",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json_body={"tool": tool, "args": args},
            timeout=timeout,
        )
        if resp.status_code != 200:
            return None
        return resp.json()
    except Exception:
        return None


def tool_response_ok(resp: _SimpleResponse) -> bool:
    """Return True only when the gateway and invoked tool both succeeded."""
    if resp.status_code != 200:
        return False
    try:
        payload = resp.json()
    except (json.JSONDecodeError, TypeError):
        return False
    if payload.get("ok") is not True or payload.get("error"):
        return False
    result = payload.get("result")
    if isinstance(result, dict) and result.get("isError") is True:
        return False
    status = None
    if isinstance(result, dict):
        details = result.get("details")
        if isinstance(details, dict):
            status = details.get("status")
        if not status:
            content = result.get("content")
            if isinstance(content, list) and content and isinstance(content[0], dict):
                text_payload = content[0].get("text")
                if isinstance(text_payload, str):
                    try:
                        decoded = json.loads(text_payload)
                    except json.JSONDecodeError:
                        decoded = None
                    if isinstance(decoded, dict):
                        status = decoded.get("status")
    if isinstance(status, str) and status.lower() in {
        "error", "forbidden", "timeout", "failed",
    }:
        return False
    return True


def resolve_session_meta(token: str, session_key: str) -> Optional[dict]:
    """Resolve session metadata from sessions_list by exact session key.

    Returns: {"sessionId": str|None, "telegramTarget": str|None,
              "deliveryChannel": str|None, "deliveryTarget": str|None, "key": str}
    """
    if not token or not session_key:
        return None

    data = _invoke_tool(token, "sessions_list", {"limit": 200})
    if not data:
        return None

    try:
        # Tool responses are wrapped as text JSON in result.content[0].text
        txt = data.get("result", {}).get("content", [{}])[0].get("text", "")
        payload = json.loads(txt) if txt else {}
        for s in payload.get("sessions", []):
            if s.get("key") == session_key:
                dc = s.get("deliveryContext", {}) or {}
                channel = dc.get("channel") or s.get("channel")
                to = dc.get("to") or s.get("displayName")
                tg_target = None
                delivery_target = None
                if isinstance(to, str) and to.startswith("telegram:"):
                    tg_target = to.split(":", 1)[1]
                    delivery_target = tg_target
                elif isinstance(to, str) and to:
                    delivery_target = to
                return {
                    "sessionId": s.get("sessionId"),
                    "telegramTarget": tg_target,
                    "deliveryChannel": channel,
                    "deliveryTarget": delivery_target,
                    "key": s.get("key"),
                }
    except Exception:
        return None

    return None


def resolve_session_meta_from_local_registry(session_key: str) -> Optional[dict]:
    """Resolve an exact OpenClaw session from per-agent local registries."""
    if not session_key:
        return None
    agents_dir = Path.home() / ".openclaw" / "agents"
    for registry in agents_dir.glob("*/sessions/sessions.json"):
        try:
            payload = json.loads(registry.read_text())
            session = payload.get(session_key) if isinstance(payload, dict) else None
            if not isinstance(session, dict):
                continue
            dc = session.get("deliveryContext", {}) or {}
            channel = dc.get("channel") or session.get("channel")
            to = dc.get("to") or session.get("displayName")
            telegram_target = None
            delivery_target = None
            if isinstance(to, str) and to.startswith("telegram:"):
                telegram_target = to.split(":", 1)[1]
                delivery_target = telegram_target
            elif isinstance(to, str) and to:
                delivery_target = to
            return {
                "sessionId": session.get("sessionId") or session.get("session_id"),
                "telegramTarget": telegram_target,
                "deliveryChannel": channel,
                "deliveryTarget": delivery_target,
                "key": session_key,
            }
        except (OSError, json.JSONDecodeError, TypeError):
            continue
    return None


def has_recent_thread_session(token: str, telegram_target: str, max_age_hours: int = 24) -> bool:
    """Return True if there is a recent thread session for this Telegram target.

    Used to prevent accidental non-thread launches when the user is actively using thread mode.
    Checks both sessions_list and local topic session files.
    """
    if not token or not telegram_target:
        return False
    data = _invoke_tool(token, "sessions_list", {"limit": 300})
    if not data:
        return False
    try:
        txt = data.get("result", {}).get("content", [{}])[0].get("text", "")
        payload = json.loads(txt) if txt else {}
        now_ms = int(time.time() * 1000)
        max_age_ms = max_age_hours * 3600 * 1000
        for s in payload.get("sessions", []):
            key = s.get("key", "")
            if ":thread:" not in key:
                continue
            dc = s.get("deliveryContext", {}) or {}
            to = dc.get("to", "")
            if to == f"telegram:{telegram_target}":
                updated = int(s.get("updatedAt", 0) or 0)
                if updated and (now_ms - updated) <= max_age_ms:
                    return True
    except Exception:
        pass

    # Fallback: local topic session files (handles cases where sessions_list only shows current session)
    try:
        base = Path.home() / ".openclaw" / "agents" / "main" / "sessions"
        if base.exists():
            now = time.time()
            max_age_sec = max_age_hours * 3600
            files = sorted(base.glob("*-topic-*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
            for p in files[:200]:
                if (now - p.stat().st_mtime) > max_age_sec:
                    continue
                with p.open("r", encoding="utf-8") as f:
                    for i, line in enumerate(f):
                        if i > 120:
                            break
                        obj = json.loads(line)
                        if obj.get("type") != "message":
                            continue
                        msg = (obj.get("message") or {})
                        if msg.get("role") != "user":
                            continue
                        for b in msg.get("content") or []:
                            txt = b.get("text", "") if isinstance(b, dict) else ""
                            if "sender_id" in txt:
                                start = txt.find("{")
                                end = txt.rfind("}")
                                if start != -1 and end != -1 and end > start:
                                    meta = json.loads(txt[start:end + 1])
                                    sid = str(meta.get("sender_id", ""))
                                    if sid and sid == str(telegram_target):
                                        return True
    except Exception:
        pass

    return False


def resolve_thread_meta_from_local_files(thread_id: str) -> Optional[dict]:
    """Resolve {sessionId, telegramTarget} from local session jsonl files.

    Useful when sessions_list doesn't return inactive thread sessions.
    Looks for newest: ~/.openclaw/agents/main/sessions/*-topic-<thread_id>.jsonl
    """
    if not thread_id:
        return None
    base = Path.home() / ".openclaw" / "agents" / "main" / "sessions"
    if not base.exists():
        return None

    candidates = sorted(
        base.glob(f"*-topic-{thread_id}.jsonl"),
        key=lambda p: p.stat().st_mtime,
        reverse=True,
    )
    if not candidates:
        return None

    p = candidates[0]
    session_id = p.name.rsplit("-topic-", 1)[0]
    telegram_target = None

    # Try to extract sender_id from early user envelope messages
    try:
        with p.open("r", encoding="utf-8") as f:
            for i, line in enumerate(f):
                if i > 150:
                    break
                obj = json.loads(line)
                if obj.get("type") != "message":
                    continue
                msg = (obj.get("message") or {})
                if msg.get("role") != "user":
                    continue
                blocks = msg.get("content") or []
                for b in blocks:
                    txt = b.get("text", "") if isinstance(b, dict) else ""
                    if "sender_id" in txt:
                        # Robust extraction: message may contain multiple JSON blocks,
                        # so wide {..} parsing can fail. Prefer direct regex first.
                        m = re.search(r'"sender_id"\s*:\s*"?(\d+)"?', txt)
                        if m:
                            telegram_target = m.group(1)
                            break
                        try:
                            # Fallback: envelope is embedded json in markdown fence
                            start = txt.find("{")
                            end = txt.rfind("}")
                            if start != -1 and end != -1 and end > start:
                                meta = json.loads(txt[start:end + 1])
                                sid = meta.get("sender_id")
                                if sid:
                                    telegram_target = str(sid)
                                    break
                        except Exception:
                            pass
                if telegram_target:
                    break
    except Exception:
        pass

    return {"sessionId": session_id, "telegramTarget": telegram_target, "key": f"agent:main:main:thread:{thread_id}"}


def get_telegram_bot_token() -> Optional[str]:
    """Read the Telegram bot token from openclaw.json config."""
    try:
        cfg_data = json.loads(CONFIG_PATH.read_text())
        tg = cfg_data.get("channels", {}).get("telegram", {})
        token = tg.get("botToken") or tg.get("token")
        if token:
            return token
        for acct in tg.get("accounts", {}).values():
            if isinstance(acct, dict) and acct.get("botToken"):
                return acct["botToken"]
    except Exception:
        pass
    return None


ENGINE_CONFIG = {
    "claude": {
        "label": "Claude Code",
        "binary": "claude",
        "override": "CC_CLAUDE_BIN",
        "progress_prefix": "CC",
        "auth_label": "subscription/OAuth (ANTHROPIC_API_KEY stripped)",
        "stripped_keys": ("ANTHROPIC_API_KEY",),
    },
    "codex": {
        "label": "Codex",
        "binary": "codex",
        "override": "CC_CODEX_BIN",
        "progress_prefix": "Codex",
        "auth_label": "ChatGPT subscription (API credentials stripped)",
        "stripped_keys": ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "CODEX_HOME"),
    },
}
DEFAULT_ENGINE = "codex"
DEFAULT_ENGINE_MODELS = {
    "claude": ("sonnet", "sonnet"),
    "codex": ("terra", "gpt-5.6-terra"),
}
EXPLICIT_MODES = {
    "fable": ("claude", "fable"),
    "sol": ("codex", "gpt-5.6-sol"),
}


def python_string_literal(value: str) -> str:
    """Return a UTF-8 source literal for generated helper scripts."""
    return json.dumps(value, ensure_ascii=False)


def code_agent_child_env(engine: str) -> dict:
    """Build a child environment that forces the selected subscription login."""
    config = ENGINE_CONFIG[engine]
    env = os.environ.copy()
    for key in config["stripped_keys"]:
        env.pop(key, None)
    return env


def claude_code_child_env() -> dict:
    """Backward-compatible helper for the Claude subscription environment.

    Hard safety invariant: worker-relay must NEVER pass ANTHROPIC_API_KEY to
    Claude Code. This keeps runs on the logged-in Claude Code subscription/OAuth
    path instead of accidentally burning API credits.
    """
    return code_agent_child_env("claude")


def resolve_code_agent_bin(engine: str, env: dict) -> str:
    """Resolve a coding-agent CLI in interactive and detached environments."""
    config = ENGINE_CONFIG[engine]
    binary = config["binary"]
    override_name = config["override"]
    override = env.get(override_name)
    if override:
        path = Path(override).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
        raise FileNotFoundError(f"{override_name} is not executable: {path}")

    on_path = shutil.which(binary, path=env.get("PATH"))
    if on_path:
        return on_path

    candidates = [Path.home() / f".local/bin/{binary}"]
    nvm_root = Path.home() / ".nvm/versions/node"
    candidates.extend(
        sorted(
            nvm_root.glob(f"*/bin/{binary}"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        )
    )
    candidates.append(Path(f"/usr/local/bin/{binary}"))
    for path in candidates:
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise FileNotFoundError(
        f"{config['label']} executable not found in PATH, ~/.local/bin, "
        f"/usr/local/bin, or ~/.nvm/versions/node/*/bin. Set {override_name} explicitly."
    )


def resolve_claude_bin(env: dict) -> str:
    """Resolve Claude Code in interactive and detached/non-login environments."""
    return resolve_code_agent_bin("claude", env)


def prepare_cli_env(env: dict, cli_bin: str) -> dict:
    """Make Node-backed CLI shims work when a detached shell has a minimal PATH."""
    prepared = env.copy()
    # Keep the symlink's directory: NVM puts both the CLI symlink and `node` there,
    # while resolving the symlink lands inside node_modules where `node` is absent.
    bin_dir = str(Path(cli_bin).expanduser().absolute().parent)
    path_parts = [part for part in prepared.get("PATH", "").split(os.pathsep) if part]
    prepend_parts = [bin_dir]
    if not (Path(bin_dir) / "node").is_file():
        nvm_root = Path.home() / ".nvm/versions/node"
        for node_bin in sorted(
            nvm_root.glob("*/bin/node"),
            key=lambda p: p.stat().st_mtime,
            reverse=True,
        ):
            node_dir = str(node_bin.parent)
            if node_dir not in prepend_parts:
                prepend_parts.append(node_dir)
            break
    prepared["PATH"] = os.pathsep.join(
        [part for part in prepend_parts if part not in path_parts] + path_parts
    )
    return prepared


def build_agent_command(engine: str, cli_bin: str, args, task_prompt: str, env: dict) -> list:
    """Build the provider-specific CLI command behind the shared lifecycle."""
    selected_model = provider_model(engine, args)
    if engine == "claude":
        cmd = [cli_bin, "-p"]
        if selected_model:
            cmd.extend(["--model", selected_model])
        if args.fast:
            help_probe = subprocess.run(
                [cli_bin, "-p", "--help"],
                capture_output=True,
                text=True,
                timeout=10,
                env=env,
            )
            help_text = (help_probe.stdout or "") + (help_probe.stderr or "")
            if "--fast" in help_text:
                cmd.append("--fast")
            else:
                cmd.extend(["--settings", '{"fastMode":true}'])
        cmd.extend([
            task_prompt,
            "--dangerously-skip-permissions",
            "--verbose", "--output-format", "stream-json",
            "--include-partial-messages",
        ])
        if args.resume:
            cmd.extend(["--resume", args.resume])
        return cmd

    if args.resume:
        cmd = [
            cli_bin, "exec", "resume", "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
        ]
    else:
        cmd = [
            cli_bin, "exec", "--json",
            "--dangerously-bypass-approvals-and-sandbox",
            "--skip-git-repo-check",
        ]
    if selected_model:
        cmd.extend(["--model", selected_model])
    if args.fast:
        cmd.extend([
            "-c", "features.fast_mode=true",
            "-c", 'service_tier="fast"',
        ])
    if args.resume:
        cmd.append(args.resume)
    cmd.append(task_prompt)
    return cmd


def provider_model(engine: str, args) -> Optional[str]:
    """Resolve the explicit model pin for the selected semantic mode."""
    if args.model:
        return args.model
    if args.mode:
        return EXPLICIT_MODES[args.mode][1]
    if args.fast:
        return None
    return DEFAULT_ENGINE_MODELS[engine][1]


def mode_label(engine: str, args) -> str:
    """Return a stable user-facing label for notifications and validation."""
    if args.fast:
        return "fast"
    if args.model:
        return f"model:{args.model}"
    if args.mode:
        return args.mode
    return f"{DEFAULT_ENGINE_MODELS[engine][0]} (default)"


def send_telegram_direct(
    chat_id: str,
    text: str,
    thread_id: Optional[str] = None,
    reply_to: Optional[str] = None,
    silent: bool = False,
    parse_mode: Optional[str] = None,
) -> Optional[int]:
    """Send a message directly via Telegram Bot API, bypassing the OpenClaw message tool.

    Required when sending to DM threads from outside a session context:
    the message tool's target resolver doesn't accept 'chatId:topic:threadId' format,
    but the Telegram API accepts message_thread_id directly.

    parse_mode: None (default) = plain text; "HTML" = HTML tags; avoid "Markdown" —
    the finish notification uses **text** (CommonMark) which Telegram MarkdownV1 rejects.

    Returns Telegram message_id on success, None on failure (logs warning to stderr).
    """
    bot_token = get_telegram_bot_token()
    if not bot_token:
        print("⚠ send_telegram_direct: no bot token found", file=sys.stderr)
        return None
    try:
        payload: dict = {
            "chat_id": chat_id,
            "text": text,
            "disable_notification": silent,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        if thread_id:
            payload["message_thread_id"] = int(thread_id)
        if reply_to:
            payload["reply_to_message_id"] = int(reply_to)
        resp = http_post(
            f"https://api.telegram.org/bot{bot_token}/sendMessage",
            json_body=payload,
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"⚠ send_telegram_direct: HTTP {resp.status_code} — {resp.text[:200]}", file=sys.stderr)
            return None
        data = resp.json()
        return ((data.get("result") or {}).get("message_id"))
    except Exception as e:
        print(f"⚠ send_telegram_direct: exception — {e}", file=sys.stderr)
        return None


def edit_telegram_direct(
    chat_id: str,
    message_id: int,
    text: str,
    parse_mode: Optional[str] = None,
) -> bool:
    """Edit an existing Telegram message directly via Bot API."""
    bot_token = get_telegram_bot_token()
    if not bot_token:
        print("⚠ edit_telegram_direct: no bot token found", file=sys.stderr)
        return False
    try:
        payload: dict = {
            "chat_id": chat_id,
            "message_id": int(message_id),
            "text": text,
        }
        if parse_mode:
            payload["parse_mode"] = parse_mode
        resp = http_post(
            f"https://api.telegram.org/bot{bot_token}/editMessageText",
            json_body=payload,
            timeout=15,
        )
        if resp.status_code != 200:
            print(f"⚠ edit_telegram_direct: HTTP {resp.status_code} — {resp.text[:200]}", file=sys.stderr)
            return False
        return True
    except Exception as e:
        print(f"⚠ edit_telegram_direct: exception — {e}", file=sys.stderr)
        return False


def send_channel(token: str, session_key: str, text: str, bg_prefix: bool = True, silent: bool = False, thread_id: Optional[str] = None, reply_to: Optional[str] = None) -> bool:
    """Send a notification message to the appropriate channel.

    bg_prefix=True: prepend 📡 (background/informational messages)
    silent=True: Telegram silent mode (no notification sound) — heartbeats use this
    thread_id: Telegram thread ID (message_thread_id) — works for both Forum group topics
               and DM threads (e.g. Saved Messages threads).
    reply_to: Telegram message ID to reply to (reply_to_message_id for DM thread routing).
              Takes priority over thread_id for Telegram channel.
    """
    channel, target = detect_channel(session_key)
    if not channel or not target or not token:
        return False
    try:
        msg = f"{BG_PREFIX}{text}" if bg_prefix else text
        args = {
            "action": "send",
            "channel": channel,
            "target": target,
            "message": msg,
        }
        # Telegram supports silent notifications; WhatsApp does not
        if silent and channel == "telegram":
            args["silent"] = True
        # Telegram DM thread routing:
        # - With thread_id: call Telegram Bot API directly (message tool doesn't accept chatId:topic:threadId format)
        # - With reply_to only: use message tool with replyTo arg (works without thread_id)
        if thread_id and channel == "telegram":
            # Bypass message tool — send directly via Bot API with message_thread_id
            return send_telegram_direct(
                target, msg, thread_id=thread_id, reply_to=reply_to, silent=silent
            ) is not None
        if reply_to and channel == "telegram":
            args["replyTo"] = str(reply_to)
        resp = http_post(
            f"{GW_URL}/tools/invoke",
            headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
            json_body={"tool": "message", "args": args},
            timeout=15,
        )
        if not tool_response_ok(resp):
            print(
                f"⚠ send_channel: gateway/tool failure "
                f"(HTTP {resp.status_code}) — {resp.text[:200]}",
                file=sys.stderr,
            )
            return False
        return True
    except Exception as e:
        print(f"⚠ send_channel: exception — {e}", file=sys.stderr)
        return False


def trace_live(token: Optional[str], session_key: Optional[str], enabled: bool, tag: str, text: str,
               thread_id: Optional[str] = None, reply_to: Optional[str] = None):
    """Send live technical trace events to the same chat/thread."""
    if not enabled or not token or not session_key:
        return
    send_channel(token, session_key, f"[TRACE][TECH]{tag} {text}", bg_prefix=False, silent=True,
                 thread_id=thread_id, reply_to=reply_to)


def state_file_for_project(project_name: str) -> Path:
    """State file path for per-project wake dedupe."""
    h = hashlib.sha1(project_name.encode("utf-8")).hexdigest()[:12]
    return Path(f"/tmp/cc-orchestrator-state-{h}.json")


def load_state(path: Path) -> dict:
    try:
        if path.exists():
            return json.loads(path.read_text())
    except Exception:
        pass
    return {}


def save_state(path: Path, state: dict):
    try:
        tmp = path.with_name(f".{path.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp")
        tmp.write_text(json.dumps(state, indent=2, ensure_ascii=False))
        os.replace(tmp, path)
    except Exception:
        pass


@contextlib.contextmanager
def locked_state(state_path: Path):
    lock_path = state_path.with_suffix(state_path.suffix + ".lock")
    with lock_path.open("a+") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def claim_wake_dispatch(state_path: Optional[Path], output_file_path: str, wake_id: str) -> bool:
    """Atomically claim a wake while rejecting delivered or active duplicates."""
    if not state_path:
        return True
    with locked_state(state_path):
        st = load_state(state_path)
        if st.get("last_dispatched_wake_id") == wake_id:
            return False
        if st.get("last_dispatched_output") == output_file_path:
            return False
        inflight_at = int(st.get("inflight_at", 0) or 0)
        inflight_fresh = inflight_at and (int(time.time()) - inflight_at) < 900
        if inflight_fresh and (
            st.get("inflight_wake_id") == wake_id
            or st.get("inflight_output") == output_file_path
        ):
            return False
        st["inflight_wake_id"] = wake_id
        st["inflight_output"] = output_file_path
        st["inflight_at"] = int(time.time())
        save_state(state_path, st)
        return True


def mark_wake_dispatched(state_path: Optional[Path], output_file_path: str, wake_id: str):
    """Record a wake as dispatched only after successful agent delivery."""
    if not state_path:
        return
    with locked_state(state_path):
        st = load_state(state_path)
        st["last_dispatched_wake_id"] = wake_id
        st["last_dispatched_output"] = output_file_path
        st["last_dispatch_at"] = int(time.time())
        st.pop("inflight_wake_id", None)
        st.pop("inflight_output", None)
        st.pop("inflight_at", None)
        save_state(state_path, st)


def release_wake_claim(state_path: Optional[Path], wake_id: str):
    """Release this run's in-flight claim after a failed delivery."""
    if not state_path:
        return
    with locked_state(state_path):
        st = load_state(state_path)
        if st.get("inflight_wake_id") == wake_id:
            st.pop("inflight_wake_id", None)
            st.pop("inflight_output", None)
            st.pop("inflight_at", None)
            save_state(state_path, st)


def notify_session(token: str, session_key: str, group_jid: Optional[str], message: str,
                   thread_id: Optional[str] = None, notify_session_id: Optional[str] = None,
                   reply_to: Optional[str] = None, html_msg: Optional[str] = None,
                   exit_code: int = 0, project_name: str = "", output_file_path: str = "",
                   trace_enabled: bool = False,
                   run_id: str = "",
                   wake_id: str = "",
                   state_path: Optional[Path] = None,
                   engine: str = "claude") -> bool:
    """Send CC result to the appropriate channel and wake the agent.

    WhatsApp: sends to group + attempts sessions_send to wake agent.
    Telegram: sends direct message, wakes agent via `openclaw agent --json`, then posts the extracted supervisor reply directly to the thread.
    Note: sessions_send is blocked in HTTP API deny list, so we use CLI for Telegram.

    thread_id: Telegram thread ID for Forum group topic notifications.
    notify_session_id: OpenClaw session UUID for precise agent wake in threads.
    reply_to: Telegram message ID to reply to (for DM thread routing).
    """
    channel, target = detect_channel(session_key)
    engine_label = ENGINE_CONFIG[engine]["label"]
    result_marker = "CLAUDE_CODE_RESULT" if engine == "claude" else "CODEX_RESULT"
    if not channel or not target:
        print("⚠ notify_session: no resolved channel target", file=sys.stderr)
        return False

    # Channel-specific delivery strategy:
    # - WhatsApp: send full result directly (human sees it) + sessions_send wakes agent
    # - Telegram: agent wakes and sends one clean response; skip raw dump to avoid double messages
    if channel == "whatsapp":
        # Send direct message (human sees result immediately)
        result_delivered = send_channel(
            token, session_key, message, bg_prefix=False,
            thread_id=thread_id, reply_to=reply_to,
        )
    else:
        result_delivered = False

    # Wake the agent based on channel
    if channel == "whatsapp" and session_key:
        # WhatsApp: sessions_send puts result in session queue.
        # If explicit notify target points to a different group than source session,
        # wake that group session directly to avoid cross-session NO_REPLY artifacts.
        wake_session_key = session_key
        if target:
            maybe_group_session = build_whatsapp_group_session_key(session_key, target)
            if maybe_group_session and maybe_group_session != session_key:
                wake_session_key = maybe_group_session

        trace_live(token, session_key, trace_enabled, "[WHATSAPP][WAKE]", "sending sessions_send wake", thread_id, reply_to)
        agent_msg = (
            f"[{result_marker}]\n{message}\n\n"
            f"---\n"
            f"⚠️ INSTRUCTION: You received a {engine_label} result. "
            f"Process it, then send your response to the WhatsApp group using "
            f"message(action=send, channel=whatsapp, target={target or 'GROUP_JID'}, message=YOUR_SUMMARY). "
            f"Then reply NO_REPLY to avoid duplicate. Do NOT rely on announce step."
        )
        wake_delivered = False
        try:
            resp = http_post(
                f"{GW_URL}/tools/invoke",
                headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"},
                json_body={"tool": "sessions_send",
                      "args": {
                          "sessionKey": wake_session_key,
                          "message": agent_msg,
                          "timeoutSeconds": 0,
                      }},
                timeout=20,
            )
            if tool_response_ok(resp):
                print(f"✓ Agent wake accepted via sessions_send -> {wake_session_key}", file=sys.stderr)
                wake_delivered = True
            else:
                print(
                    f"⚠ sessions_send gateway/tool failure "
                    f"(HTTP {resp.status_code}): {resp.text[:200]}",
                    file=sys.stderr,
                )
        except Exception as e:
            print(f"⚠ Session notify error: {e}", file=sys.stderr)
        if not wake_delivered:
            send_channel(
                token, session_key,
                f"⚠ {engine_label} finished, but the supervisor wake was not confirmed. "
                f"Result is above; output: {output_file_path}",
                bg_prefix=False,
            )
        return result_delivered and wake_delivered

    elif channel == "telegram" and target:
        # For thread sessions: always send result directly to thread first so user always sees it.
        # Agent wake is an additional continuation step; run-task delivers the extracted final text to chat directly.
        already_sent = False
        if thread_id or reply_to:
            if html_msg and thread_id:
                # HTML version available — use expandable blockquote formatting
                _, tgt = detect_channel(session_key)
                result_delivered = send_telegram_direct(
                    tgt, html_msg, thread_id=thread_id,
                    reply_to=reply_to, silent=True, parse_mode="HTML",
                ) is not None
            else:
                result_delivered = send_channel(
                    token, session_key, message, bg_prefix=False,
                    silent=True, thread_id=thread_id, reply_to=reply_to,
                )
            already_sent = result_delivered

        # Wake the agent and deliver continuation response into chat.
        # Fallback: if agent wake fails AND we haven't already sent, send full result directly.
        # NOTE: target:topic:thread format is not supported by message tool for Telegram.
        tg_target = target
        # IMPORTANT: keep wake payload clean and non-leaky.
        # Do NOT include internal markers like [CLAUDE_CODE_RESULT] / ⚠️ INSTRUCTION,
        # because in failure modes they may surface to the user chat.
        resume_failed_hint = ""
        if exit_code == RESUME_FAILED_EXIT_CODE:
            resume_failed_hint = (
                f"\n\nОсобый случай: RESUME_FAILED. {engine_label} не начал работу, потому что переданный --resume session id не найден/протух. "
                "Прочитай output file, проверь исходную цель, и если пользователь не требовал строго продолжать именно эту старую сессию, "
                "перезапусти тот же task fresh/new session без --resume, сохранив DoD/self-verification. "
                "Не выдавай это как выполненную работу."
            )

        wake_text = (
            f"[CODE_AGENT:single] engine={engine} exit={exit_code} project={project_name} output={output_file_path} limit=1 run_id={run_id} wake_id={wake_id}\n"
            f"status={'resume_failed' if exit_code == RESUME_FAILED_EXIT_CODE else 'result'}\n\n"
            f"Это системный wake-вызов после результата или recoverable launch failure {engine_label} по твоему же последнему запуску. "
            "Считай это обычным продолжением того же диалога: та же личность агента, та же сессия, та же история.\n\n"
            f"КРИТИЧЕСКОЕ ТЕХНИЧЕСКОЕ ПРАВИЛО: если во время этого wake ты видишь активный `run-task.py`/{engine_label} wrapper, "
            "который соответствует этому же output path / run_id / wake_id, НЕ УБИВАЙ ЕГО. Он может оставаться живым именно потому, "
            "что ждёт завершения этого supervisor wake, чтобы доставить твой финальный ответ в Telegram. Убийство такого процесса ломает доставку ответа. "
            "Убивать можно только явно другой/устаревший процесс после независимой проверки, что он не является текущим notifier wrapper.\n\n"
            f"Ключевое правило continuity: ориентируйся на ИСХОДНУЮ пользовательскую цель, ради которой запускался {engine_label}, "
            "а не только на последний технический подшаг.\n"
            "Если видишь, что это устаревший wake по уже закрытой более новой итерации — коротко пометь его как stale и заверши без новых действий.\n\n"
            "Сделай естественное продолжение переписки. Для отладки в начале сообщения используй префикс: "
            "[TRACE][AGENT][WAKE_RECEIVED].\n"
            "ЖЁСТКОЕ ПРАВИЛО: silent launch запрещён. Дай отдельный видимый turn с анализом и явным решением ([TRACE][AGENT][DECISION]).\n"
            "1) коротко отреагируй на результат,\n"
            "2) оцени, закрыта ли исходная цель пользователя целиком,\n"
            "3) если нужна безопасная follow-up/retry итерация — запусти её; иначе сообщи итог пользователю и остановись."
            f"{resume_failed_hint}"
        )

        def _send_wake_failure(failure_tag: str, detail: str = ""):
            """Send visible wake failure notification to thread/chat.

            When already_sent=True (thread sessions), direct result was posted but
            the supervisor agent never processed it. Without this notification the
            user has no signal that the iterative workflow stalled.
            """
            short = (
                f"⚠️ [WAKE:{failure_tag}] Supervisor agent did not process CC result.\n"
                f"Output: {output_file_path}"
            )
            if detail:
                short += f"\n{detail}"
            if thread_id and tg_target:
                send_telegram_direct(tg_target, short, thread_id=thread_id,
                                     reply_to=reply_to, silent=False)
            elif token and session_key:
                send_channel(token, session_key, short, bg_prefix=False,
                             thread_id=thread_id, reply_to=reply_to)

        def _extract_agent_payload_text(stdout: str) -> str:
            """Extract final assistant text from `openclaw agent --json` output."""
            raw = (stdout or "").strip()
            if not raw:
                return ""
            try:
                data = json.loads(raw)
            except Exception:
                # Defensive fallback for older CLI builds or embedded fallback logs.
                return raw
            payloads = (((data or {}).get("result") or {}).get("payloads") or [])
            parts = []
            for payload in payloads:
                if not isinstance(payload, dict):
                    continue
                text_part = (payload.get("text") or "").strip()
                if text_part:
                    parts.append(text_part)
                media_urls = payload.get("mediaUrls") or []
                if isinstance(media_urls, list):
                    for url in media_urls:
                        if isinstance(url, str) and url.strip():
                            parts.append(f"MEDIA:{url.strip()}")
                media_url = payload.get("mediaUrl")
                if isinstance(media_url, str) and media_url.strip():
                    parts.append(f"MEDIA:{media_url.strip()}")
            return "\n".join(parts).strip()

        try:
            # Supervisor wake turns often read output, verify files, and launch a
            # follow-up Claude Code run. A short timeout can kill the visible final
            # agent message after the launch notification has already gone out.
            agent_timeout_seconds = 300
            subprocess_timeout_seconds = agent_timeout_seconds + 30
            if notify_session_id:
                cmd = ["openclaw", "agent",
                       "--session-id", notify_session_id,
                       "--message", wake_text,
                       "--json",
                       "--timeout", str(agent_timeout_seconds)]
            else:
                cmd = ["openclaw", "agent",
                       "--channel", "telegram",
                       "--to", target,
                       "--message", wake_text,
                       "--json",
                       "--timeout", str(agent_timeout_seconds)]
            if not claim_wake_dispatch(state_path, output_file_path, wake_id):
                trace_live(token, session_key, trace_enabled, "[TELEGRAM][WAKE][SKIP]",
                           f"duplicate/stale wake ignored (project={project_name}, output={output_file_path}, wake_id={wake_id})",
                           thread_id, reply_to)
                return result_delivered
            trace_live(token, session_key, trace_enabled, "[TELEGRAM][WAKE]",
                       f"dispatching openclaw agent wake (project={project_name}, output={output_file_path}, wake_id={wake_id})",
                       thread_id, reply_to)
            result = subprocess.run(cmd, capture_output=True, text=True, timeout=subprocess_timeout_seconds)
            if result.returncode == 0:
                supervisor_text = _extract_agent_payload_text(result.stdout)
                if not supervisor_text:
                    print("⚠ Agent wake returned success but no payload text", file=sys.stderr)
                    _send_wake_failure("EMPTY", "agent returned no visible final text")
                    release_wake_claim(state_path, wake_id)
                    return False
                # Do deterministic Telegram thread delivery ourselves. In practice,
                # `openclaw agent --deliver --session-id ...` can complete the
                # supervisor turn in the transcript without posting to the original
                # Telegram topic. The universe, as usual, finds the least convenient
                # possible edge case.
                if thread_id and tg_target:
                    supervisor_delivered = send_telegram_direct(
                        tg_target, supervisor_text, thread_id=thread_id,
                        reply_to=reply_to, silent=False,
                    ) is not None
                elif token and session_key:
                    supervisor_delivered = send_channel(
                        token, session_key, supervisor_text, bg_prefix=False,
                        thread_id=thread_id, reply_to=reply_to,
                    )
                else:
                    supervisor_delivered = False
                if not supervisor_delivered:
                    release_wake_claim(state_path, wake_id)
                    _send_wake_failure("DELIVERY", "supervisor reply was generated but not delivered")
                    return False
                mark_wake_dispatched(state_path, output_file_path, wake_id)
                print(f"✓ Agent woken via openclaw agent and delivered by run-task", file=sys.stderr)
                direct_result_required = bool(thread_id or reply_to)
                return supervisor_delivered and (
                    result_delivered if direct_result_required else True
                )
            else:
                stderr_snip = (result.stderr or "")[:300]
                stdout_snip = (result.stdout or "")[:300]
                print(f"⚠ Agent wake failed (exit {result.returncode}): {stderr_snip}", file=sys.stderr)
                if stdout_snip:
                    print(f"  stdout: {stdout_snip}", file=sys.stderr)
                if not already_sent:
                    result_delivered = send_channel(
                        token, session_key, message, bg_prefix=False,
                        thread_id=thread_id, reply_to=reply_to,
                    )
                else:
                    _send_wake_failure("FAIL", f"exit {result.returncode}")
                release_wake_claim(state_path, wake_id)
                return False
        except subprocess.TimeoutExpired:
            print(f"⚠ Telegram agent wake timeout ({subprocess_timeout_seconds}s)", file=sys.stderr)
            if not already_sent:
                result_delivered = send_channel(
                    token, session_key, message, bg_prefix=False,
                    thread_id=thread_id, reply_to=reply_to,
                )
            else:
                _send_wake_failure("TIMEOUT", f"after {subprocess_timeout_seconds}s")
            release_wake_claim(state_path, wake_id)
            return False
        except Exception as e:
            print(f"⚠ Telegram agent wake error: {e}", file=sys.stderr)
            if not already_sent:
                result_delivered = send_channel(
                    token, session_key, message, bg_prefix=False,
                    thread_id=thread_id, reply_to=reply_to,
                )
            else:
                _send_wake_failure("ERROR", str(e)[:200])
            release_wake_claim(state_path, wake_id)
            return False

    return False


def cleanup_stale_pids():
    """Remove PID files for processes that no longer exist."""
    if not PID_DIR.exists():
        return
    for pid_file in PID_DIR.glob("*.pid"):
        try:
            pid = int(pid_file.read_text().strip().split("\n")[0])
            os.kill(pid, 0)  # Check if alive
        except (ProcessLookupError, ValueError):
            pid_file.unlink(missing_ok=True)
        except PermissionError:
            pass  # Process exists but we can't signal it


def write_pid_file(task_short: str) -> Path:
    """Write PID file for this task."""
    PID_DIR.mkdir(parents=True, exist_ok=True)
    cleanup_stale_pids()
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    # Sanitize task name for filename
    safe_name = "".join(c if c.isalnum() or c in "-_" else "-" for c in task_short[:40])
    pid_file = PID_DIR / f"{ts}-{os.getpid()}-{safe_name}.pid"
    pid_file.write_text(f"{os.getpid()}\n{task_short}\n{datetime.now().isoformat()}")
    return pid_file


def kill_process_graceful(proc: subprocess.Popen, timeout_grace: int = 10):
    """SIGTERM → wait → SIGKILL for the Claude process group."""
    try:
        pgid = os.getpgid(proc.pid)
    except Exception:
        pgid = None

    try:
        if pgid is not None:
            os.killpg(pgid, signal.SIGTERM)
        else:
            proc.terminate()
        try:
            proc.wait(timeout=timeout_grace)
        except subprocess.TimeoutExpired:
            if pgid is not None:
                os.killpg(pgid, signal.SIGKILL)
            else:
                proc.kill()
            proc.wait(timeout=5)
    except Exception:
        pass


def format_tokens(n: int) -> str:
    """Format token count: 1234 → '1.2K', 12345 → '12K'."""
    if n < 1000:
        return str(n)
    elif n < 10000:
        return f"{n/1000:.1f}K"
    else:
        return f"{n//1000}K"


def format_cost_summary(state: dict) -> str:
    """Format completion-only cost/usage summary.

    Billing label rules:
    - Standard subscription task path: show subscription.
    - Fast mode / explicit token-billed paths: show estimated dollar cost when present.
    - If no result/usage data exists, return empty string.
    """
    parts = []
    cost = state.get("result_cost_usd")
    usage = state.get("result_usage") or {}
    in_tok = usage.get("input_tokens", 0) or 0
    out_tok = usage.get("output_tokens", 0) or 0
    cache_read = usage.get("cache_read_input_tokens", 0) or 0
    cache_write = usage.get("cache_creation_input_tokens", 0) or 0
    num_turns = state.get("result_num_turns")

    has_usage = bool(in_tok or out_tok or cache_write or cache_read or num_turns)
    billing_label = state.get("billing_label")
    if billing_label == "subscription":
        if has_usage or state.get("result_seen"):
            parts.append("subscription")
    elif cost is not None:
        parts.append(f"~${cost:.4f} est.")
    elif billing_label == "fast":
        if has_usage or state.get("result_seen"):
            parts.append("fast extra usage (cost unavailable)")
    elif has_usage or state.get("result_seen"):
        parts.append("subscription")

    if has_usage:
        tok_str = f"in:{format_tokens(in_tok)} out:{format_tokens(out_tok)}"
        if cache_read:
            tok_str += f" cache↩:{format_tokens(cache_read)}"
        if cache_write:
            tok_str += f" cache↑:{format_tokens(cache_write)}"
        parts.append(tok_str)
    if num_turns:
        parts.append(f"turns:{num_turns}")
    return " | ".join(parts) if parts else ""


def parse_codex_stream_line(line: str, state: dict):
    """Parse `codex exec --json` events into the shared activity state."""
    try:
        data = json.loads(line)
    except (json.JSONDecodeError, TypeError):
        return

    event_type = data.get("type", "")
    state["last_event_time"] = time.time()
    semantic_progress = event_type not in ("thread.started", "turn.started")

    if event_type == "thread.started":
        state["session_id"] = data.get("thread_id") or state.get("session_id")
        state["last_activity"] = "Starting..."
    elif event_type == "turn.started":
        state["last_activity"] = "Thinking..."
    elif event_type in ("item.started", "item.updated", "item.completed"):
        item = data.get("item") or {}
        item_type = item.get("type", "")
        if event_type == "item.started":
            state["chunks_since_heartbeat"] += 1
        if item_type in ("command_execution", "mcp_tool_call", "web_search"):
            if event_type == "item.started":
                state["tool_calls"] += 1
            command = item.get("command") or item.get("name") or item_type
            if isinstance(command, list):
                command = " ".join(str(part) for part in command)
            state["last_activity"] = f"Running: {str(command)[:80]}"
        elif item_type in ("file_change", "file_write", "file_edit"):
            state["last_activity"] = "Editing files..."
            changes = item.get("changes") or []
            if isinstance(changes, list):
                for change in changes:
                    if isinstance(change, dict) and change.get("path"):
                        state["files_written"].append(Path(change["path"]).name)
        elif item_type == "agent_message":
            text = (item.get("text") or "").strip()
            if text:
                state["last_agent_message"] = text
                state["last_activity"] = "Writing..."
        elif item_type in ("reasoning", "analysis"):
            state["last_activity"] = "Thinking..."
        else:
            state["last_activity"] = item_type or "Working..."
    elif event_type == "turn.completed":
        semantic_progress = True
        state["result_seen"] = True
        state["result_seen_time"] = time.time()
        state["last_activity"] = "finishing..."
        usage = data.get("usage") or {}
        if usage:
            normalized = {
                "input_tokens": usage.get("input_tokens", 0),
                "output_tokens": usage.get("output_tokens", 0),
                "cache_read_input_tokens": usage.get("cached_input_tokens", 0),
                "reasoning_output_tokens": usage.get("reasoning_output_tokens", 0),
            }
            state["result_usage"] = normalized
            state["output_tokens"] = normalized["output_tokens"]
    elif event_type == "turn.failed":
        semantic_progress = True
        state["last_activity"] = "failed"
        error = data.get("error")
        if isinstance(error, dict):
            error = error.get("message") or json.dumps(error, ensure_ascii=False)
        state["terminal_error"] = str(error or "Codex turn failed")
    elif event_type == "error":
        semantic_progress = True
        state["last_activity"] = "error"

    if semantic_progress:
        state["last_semantic_progress_time"] = time.time()


def parse_stream_line(line: str, state: dict, engine: str = "claude"):
    """Parse stream-json line for activity tracking, session ID capture, and active subagent count."""
    if engine == "codex":
        parse_codex_stream_line(line, state)
        return
    try:
        data = json.loads(line)
        msg_type = data.get("type", "")
        semantic_progress = False

        # Update liveness timestamp on ANY event
        state["last_event_time"] = time.time()

        # Unwrap stream_event envelope if present
        inner = data
        inner_type = msg_type
        if msg_type == "stream_event":
            inner = data.get("event", {})
            inner_type = inner.get("type", "")

        # Capture session_id and resolved model from init event
        if msg_type == "system" and data.get("subtype") == "init":
            session_id = data.get("session_id")
            if session_id:
                state["session_id"] = session_id
            model = data.get("model")
            if model:
                state["resolved_model"] = model

        # Content block events (from --include-partial-messages)
        # Can arrive as top-level OR inside stream_event envelope
        if inner_type == "content_block_start":
            cb = inner.get("content_block", {})
            if cb.get("type") == "tool_use":
                state["last_activity"] = f"▶️ {cb.get('name', '?')} starting..."
                semantic_progress = True
            elif cb.get("type") == "thinking":
                state["last_activity"] = "🧠 Thinking..."
        elif inner_type == "content_block_delta":
            state["chunks_since_heartbeat"] += 1
            semantic_progress = True
            delta = inner.get("delta", {})
            if delta.get("type") == "thinking_delta":
                state["last_activity"] = "🧠 Thinking..."
            elif delta.get("type") == "text_delta":
                state["last_activity"] = "✍️ Writing..."
        elif inner_type == "content_block_stop":
            pass  # last_event_time already updated
        elif inner_type == "message_delta":
            usage = inner.get("usage", {})
            if "output_tokens" in usage:
                state["output_tokens"] += usage["output_tokens"]
                semantic_progress = True

        if msg_type == "assistant" and "message" in data:
            semantic_progress = True
            # Extract usage from assistant message — aggregate across main + subagents
            usage = data.get("message", {}).get("usage", {})
            if "output_tokens" in usage:
                state["output_tokens"] += usage["output_tokens"]

            content = data["message"].get("content", [])
            for block in content:
                if block.get("type") == "tool_use":
                    state["tool_calls"] += 1
                    tool_name = block.get("name", "?")
                    tool_input = block.get("input", {})

                    # Track active subagents by Task/Agent tool_use id
                    if tool_name.lower() in ("task", "agent"):
                        tid = block.get("id")
                        if tid:
                            state["active_subagent_ids"].add(tid)

                    if tool_name.lower() in ("write", "edit"):
                        fp = tool_input.get("file_path", "?")
                        state["files_written"].append(fp.split("/")[-1])
                        state["last_activity"] = f"📝 {tool_name}: {fp.split('/')[-1]}"
                    elif tool_name.lower() == "read":
                        fp = tool_input.get("file_path", "?")
                        state["last_activity"] = f"👁 read: {fp.split('/')[-1]}"
                    elif tool_name.lower() == "bash":
                        cmd = tool_input.get("command", "?")[:50]
                        state["last_activity"] = f"💻 bash: {cmd}"
                    elif "search" in tool_name.lower() or "grep" in tool_name.lower():
                        state["last_activity"] = f"🔍 {tool_name}"
                    else:
                        state["last_activity"] = f"🔧 {tool_name}"

        elif msg_type == "user" and "message" in data:
            # Mark subagent completion when Task/Agent tool_result returns to parent
            for block in data.get("message", {}).get("content", []):
                if block.get("type") == "tool_result":
                    semantic_progress = True
                    tid = block.get("tool_use_id")
                    if tid and tid in state["active_subagent_ids"]:
                        state["active_subagent_ids"].discard(tid)

        elif msg_type == "system" and data.get("subtype") == "task_notification":
            # Background task completion/failure path
            semantic_progress = True
            tid = data.get("tool_use_id")
            if tid and tid in state["active_subagent_ids"]:
                state["active_subagent_ids"].discard(tid)

        elif msg_type == "result":
            semantic_progress = True
            state["result_seen"] = True
            state["result_seen_time"] = time.time()
            state["last_activity"] = "✅ finishing..."
            # Capture cost and usage from result event
            cost = data.get("total_cost_usd") or data.get("cost_usd")
            if cost is not None and state.get("result_cost_usd") is None:
                state["result_cost_usd"] = cost
            usage = data.get("usage")
            if usage and state.get("result_usage") is None:
                state["result_usage"] = usage
            dur = data.get("duration_ms")
            if dur is not None and state.get("result_duration_ms") is None:
                state["result_duration_ms"] = dur
            turns = data.get("num_turns")
            if turns is not None and state.get("result_num_turns") is None:
                state["result_num_turns"] = turns
            model = data.get("model")
            if model and state.get("resolved_model") is None:
                state["resolved_model"] = model

        if semantic_progress:
            state["last_semantic_progress_time"] = time.time()

    except (json.JSONDecodeError, KeyError):
        pass


def resume_failure_detected(engine: str, resume_id: Optional[str], stderr_output: str) -> bool:
    """Recognize a provider-specific missing/expired session without masking other failures."""
    if not resume_id or not stderr_output:
        return False
    lowered = stderr_output.lower()
    if engine == "claude":
        return (
            "no conversation found" in lowered
            or "resume failed" in lowered
            or "conversation not found" in lowered
        )
    return (
        "no rollout found for thread id" in lowered
        or ("thread/resume" in lowered and "not found" in lowered)
    )


def extract_final_text(engine: str, output_lines, state: dict, stderr_output: str) -> str:
    """Extract the final assistant message from either provider's JSONL schema."""
    if engine == "codex":
        if state.get("terminal_error"):
            return state["terminal_error"]
        final_text = (state.get("last_agent_message") or "").strip()
        if not final_text:
            for line in reversed(output_lines):
                try:
                    data = json.loads(line)
                except (json.JSONDecodeError, TypeError):
                    continue
                item = data.get("item") or {}
                if data.get("type") == "item.completed" and item.get("type") == "agent_message":
                    final_text = (item.get("text") or "").strip()
                    if final_text:
                        break
        return final_text or stderr_output or "(no output captured)"

    final_text = ""
    for line in output_lines:
        try:
            data = json.loads(line)
            if data.get("type") == "result":
                final_text = data.get("result", "")
                if state.get("result_cost_usd") is None:
                    state["result_cost_usd"] = data.get("total_cost_usd") or data.get("cost_usd")
                if state.get("result_usage") is None:
                    state["result_usage"] = data.get("usage")
                if state.get("result_duration_ms") is None:
                    state["result_duration_ms"] = data.get("duration_ms")
                if state.get("result_num_turns") is None:
                    state["result_num_turns"] = data.get("num_turns")
                if state.get("resolved_model") is None:
                    state["resolved_model"] = data.get("model")
                break
        except (json.JSONDecodeError, KeyError):
            pass

    if not final_text:
        for line in output_lines:
            try:
                data = json.loads(line)
                if data.get("type") == "assistant":
                    for block in data.get("message", {}).get("content", []):
                        if block.get("type") == "text":
                            final_text += block.get("text", "") + "\n"
            except (json.JSONDecodeError, KeyError):
                pass
    return final_text or stderr_output or "(no output captured)"


def detached_worker_argv(raw_argv: list[str]) -> list[str]:
    """Remove launcher-only flags before re-executing the canonical runner."""
    worker_argv = []
    skip_next = False
    for arg in raw_argv:
        if skip_next:
            skip_next = False
            continue
        if arg == "--detach":
            continue
        if arg == "--detach-log":
            skip_next = True
            continue
        if arg.startswith("--detach-log="):
            continue
        worker_argv.append(arg)
    return worker_argv


def fallback_worker_argv(raw_argv: list[str], fallback_engine: str) -> list[str]:
    """Build one non-recursive retry using the configured fallback engine."""
    worker_argv = []
    engine_replaced = False
    index = 0
    while index < len(raw_argv):
        arg = raw_argv[index]
        if arg == "--fallback-engine":
            index += 2
            continue
        if arg.startswith("--fallback-engine="):
            index += 1
            continue
        if arg == "--engine":
            worker_argv.extend(["--engine", fallback_engine])
            engine_replaced = True
            index += 2
            continue
        if arg.startswith("--engine="):
            worker_argv.append(f"--engine={fallback_engine}")
            engine_replaced = True
            index += 1
            continue
        worker_argv.append(arg)
        index += 1
    if not engine_replaced:
        worker_argv.extend(["--engine", fallback_engine])
    return worker_argv


PROVIDER_UNAVAILABLE_PATTERNS = (
    r"\b401\b.*\bunauthorized\b",
    r"missing bearer or basic authentication",
    r"authentication (?:failed|required)",
    r"not logged in|login required|token (?:has )?expired",
    r"\b429\b|too many requests|rate limit(?:ed| exceeded)?",
    r"\b(?:500|502|503|504)\b.*(?:server|service|upstream|status)",
    r"service unavailable|temporarily unavailable|server overloaded",
    r"error sending request|failed to connect|connection refused",
    r"network is unreachable|failed to lookup address|dns error",
    r"stream disconnected before completion|upstream connect error",
)


def provider_unavailable_reason(
    engine: str,
    exit_code: int,
    stderr_output: str,
    state: dict,
    *,
    timed_out: bool = False,
) -> Optional[str]:
    """Return a bounded reason only when the provider failed before useful work."""
    if exit_code == 0 or timed_out or state.get("tool_calls", 0) > 0 or state.get("result_seen"):
        return None
    evidence = "\n".join(filter(None, [
        stderr_output,
        str(state.get("terminal_error") or ""),
    ])).strip()
    for pattern in PROVIDER_UNAVAILABLE_PATTERNS:
        match = re.search(pattern, evidence, flags=re.IGNORECASE | re.DOTALL)
        if match:
            compact = " ".join(match.group(0).split())
            return f"{engine} provider unavailable: {compact[:240]}"
    return None


def launch_detached(raw_argv: list[str], log_path: Optional[str] = None) -> int:
    """Launch this runner in a transient user service that survives tool exit."""
    systemd_run = shutil.which("systemd-run")
    if not systemd_run:
        print("Detached launch requires systemd-run on this host", file=sys.stderr)
        return CRASH_EXIT_CODE

    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    unit = f"cc-task-{stamp}-{uuid.uuid4().hex[:10]}"
    detached_log = Path(log_path or f"/tmp/{unit}.log").expanduser().absolute()
    detached_log.parent.mkdir(parents=True, exist_ok=True)

    worker_cmd = [
        sys.executable,
        str(Path(__file__).resolve()),
        *detached_worker_argv(raw_argv),
    ]
    command = [
        systemd_run,
        "--user",
        f"--unit={unit}",
        "--collect",
        "--property=Type=exec",
        f"--property=StandardOutput=append:{detached_log}",
        f"--property=StandardError=append:{detached_log}",
        *worker_cmd,
    ]
    try:
        result = subprocess.run(
            command,
            capture_output=True,
            text=True,
            timeout=20,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        print(f"Detached launch failed: {exc}", file=sys.stderr)
        return CRASH_EXIT_CODE

    if result.returncode != 0:
        detail = (result.stderr or result.stdout or "unknown systemd-run failure").strip()
        print(f"Detached launch failed: {detail}", file=sys.stderr)
        return CRASH_EXIT_CODE

    print("DETACHED_LAUNCH_ACCEPTED")
    print(f"Unit: {unit}.service")
    print(f"Log: {detached_log}")
    if result.stdout.strip():
        print(f"Systemd: {result.stdout.strip()}")
    return 0


def main():
    parser = argparse.ArgumentParser(description="Run Claude Code or Codex task async")
    parser.add_argument(
        "--engine", choices=sorted(ENGINE_CONFIG), default=DEFAULT_ENGINE,
        help=f"Worker engine (default: {DEFAULT_ENGINE}; no implicit fallback)",
    )
    parser.add_argument(
        "--fallback-engine", choices=sorted(ENGINE_CONFIG),
        help="Retry once with this engine only when the primary provider is unavailable before useful work",
    )
    parser.add_argument("--task", "-t", required=True, help="Task description")
    parser.add_argument("--project", "-p", default="/tmp/cc-scratch", help="Project directory")
    parser.add_argument("--session", "-s", help="Session key to notify on completion")
    parser.add_argument("--output", "-o", help="Output file (default: /tmp/cc-<timestamp>.txt)")
    parser.add_argument(
        "--detach", action="store_true",
        help="Launch the runner in a transient user service and return immediately",
    )
    parser.add_argument(
        "--detach-log",
        help="Log path for --detach (default: /tmp/cc-task-<timestamp>-<id>.log)",
    )
    parser.add_argument("--timeout", type=int, default=DEFAULT_TIMEOUT,
                        help=f"Max runtime in seconds (default: {DEFAULT_TIMEOUT}s = {DEFAULT_TIMEOUT//60}min)")

    parser.add_argument("--resume", help="Resume from a previous session/thread ID for the selected engine")
    parser.add_argument("--session-label", help="Human-readable label for this session (e.g., 'Research on X')")
    parser.add_argument(
        "--notify-channel", choices=["telegram", "whatsapp"],
        help="Channel hint; target is resolved from the exact source session",
    )
    parser.add_argument("--notify-thread-id", help="Telegram thread ID for threaded mode (auto-detected from session key)")
    parser.add_argument("--notify-session-id", help="OpenClaw session UUID for precise agent wake in threads")
    parser.add_argument("--reply-to-message-id", help="Telegram message ID to reply to (for DM thread routing)")
    parser.add_argument("--validate-only", action="store_true", help="Resolve routing and exit without launching a worker")
    parser.add_argument("--allow-main-telegram", action="store_true", help="Allow Telegram launch without :thread: session (for non-thread Telegram setups)")
    parser.add_argument("--telegram-routing-mode", choices=["auto", "thread-only", "allow-non-thread"], default="auto", help="Telegram routing policy (default: auto)")
    parser.add_argument("--trace-live", action="store_true",
                        help="Send live technical trace events to chat/thread for debugging")

    parser.add_argument(
        "--fast", action="store_true",
        help="Run the selected engine in its native Fast mode through subscription auth.",
    )
    parser.add_argument(
        "--mode", choices=sorted(EXPLICIT_MODES),
        help="Explicit semantic mode: sol for Codex or fable for Claude. "
             "Omission uses Terra for Codex and Sonnet for Claude.",
    )
    parser.add_argument(
        "--model",
        help="Pin an explicit provider-specific model on the subscription path. "
             "Cannot be combined with --mode or --fast.",
    )
    args = parser.parse_args()

    if args.fast and args.model:
        parser.error("--model cannot be combined with --fast")
    if args.mode and args.model:
        parser.error("--mode cannot be combined with --model")
    if args.mode and args.fast:
        parser.error("--mode cannot be combined with --fast")
    if args.mode and EXPLICIT_MODES[args.mode][0] != args.engine:
        parser.error(
            f"--mode {args.mode} requires --engine {EXPLICIT_MODES[args.mode][0]}"
        )
    if args.fallback_engine == args.engine:
        parser.error("--fallback-engine must differ from --engine")
    if args.fallback_engine and args.resume:
        parser.error("--fallback-engine cannot be combined with --resume")
    if args.fallback_engine and args.model:
        parser.error("--fallback-engine cannot be combined with provider-specific --model")
    if args.fallback_engine and args.mode:
        parser.error("--fallback-engine cannot be combined with provider-specific --mode")
    if args.detach_log and not args.detach:
        parser.error("--detach-log requires --detach")
    if args.detach and args.validate_only:
        parser.error("--detach cannot be combined with --validate-only")
    if args.detach:
        return launch_detached(sys.argv[1:], args.detach_log)

    engine = args.engine
    engine_config = ENGINE_CONFIG[engine]
    engine_label = engine_config["label"]
    fallback_label = (
        ENGINE_CONFIG[args.fallback_engine]["label"]
        if args.fallback_engine else None
    )
    progress_prefix = engine_config["progress_prefix"]
    auth_label = engine_config["auth_label"]
    selected_mode_label = mode_label(engine, args)
    selected_model = provider_model(engine, args)
    agent_env = code_agent_child_env(engine)
    agent_bin = None
    billing_label = "fast" if args.fast else "subscription"

    if args.resume:
        try:
            known_session = peek_session(args.resume)
        except Exception as exc:
            known_session = None
            print(f"⚠ Could not inspect session registry before resume: {exc}", file=sys.stderr)
        if known_session:
            registered_engine = known_session.get("engine", "claude")
            if registered_engine != engine:
                parser.error(
                    f"--resume id is registered for engine {registered_engine}, "
                    f"not {engine}"
                )

    def run_fallback(reason: str) -> int:
        """Switch providers without sending a false final wake for the failed primary."""
        message = (
            f"⚠️ {engine_label} unavailable before useful work; "
            f"switching to {fallback_label}.\nReason: {reason[:300]}"
        )
        print(message, file=sys.stderr)
        if token and args.session:
            send_channel(
                token, args.session, message,
                silent=False, thread_id=thread_id, reply_to=reply_to_msg_id,
            )
        command = [
            sys.executable,
            str(Path(__file__).resolve()),
            *fallback_worker_argv(sys.argv[1:], args.fallback_engine),
        ]
        try:
            result = subprocess.run(command, cwd=str(project))
        except (OSError, subprocess.SubprocessError) as exc:
            print(f"Fallback launch failed: {exc}", file=sys.stderr)
            return CRASH_EXIT_CODE
        return result.returncode

    # Set notification globals (channel hint; target must be resolved deterministically)
    global NOTIFY_CHANNEL_OVERRIDE, NOTIFY_TARGET_OVERRIDE
    if args.notify_channel:
        NOTIFY_CHANNEL_OVERRIDE = args.notify_channel

    # Resolve thread_id: explicit arg takes priority, otherwise auto-detect from session key
    thread_id = args.notify_thread_id or extract_thread_id(args.session or "")
    notify_session_id = args.notify_session_id  # Optional UUID for precise agent wake in threads
    reply_to_msg_id = args.reply_to_message_id  # Optional, for DM thread routing

    # Setup
    project = Path(args.project)
    project.mkdir(parents=True, exist_ok=True)
    run_id = str(uuid.uuid4())
    wake_id = str(uuid.uuid4())
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    output_file = args.output or f"/tmp/cc-{ts}-{run_id[:8]}.txt"
    state_path = state_file_for_project(str(project))
    group_jid = extract_group_jid(args.session or "")  # WhatsApp JID if present
    token = None
    pid_file = None
    proc = None
    notify_script_path = None

    try:
        token = get_token() if args.session else None
    except Exception:
        pass

    # Strict Telegram thread routing: auto-resolve missing IDs and fail fast on mismatch.
    # Goal: make incorrect launches impossible when using thread sessions.
    session_meta = resolve_session_meta(token, args.session) if (token and args.session) else None
    if not session_meta and args.session:
        session_meta = resolve_session_meta_from_local_registry(args.session)
    if not session_meta and thread_id:
        # Fallback for inactive sessions not returned by sessions_list
        session_meta = resolve_thread_meta_from_local_files(thread_id)

    # A syntactically valid WhatsApp JID is not proof that the requested
    # OpenClaw session exists. Without an exact session, direct delivery can
    # work while the supervisor wake silently targets a phantom agent.
    if args.session and group_jid and not session_meta:
        print("❌ Invalid routing: WhatsApp source session does not exist", file=sys.stderr)
        print(f"   session: {args.session}", file=sys.stderr)
        print("   Use the exact live agent/session key from sessions_list.", file=sys.stderr)
        sys.exit(2)

    if not notify_session_id and session_meta:
        notify_session_id = session_meta.get("sessionId")

    # Some WhatsApp DM session keys contain no routable JID. Recover the exact
    # source destination from deliveryContext instead of silently choosing a group.
    if session_meta and not thread_id:
        delivery_channel = session_meta.get("deliveryChannel")
        delivery_target = session_meta.get("deliveryTarget")
        if delivery_channel == "whatsapp" and delivery_target:
            NOTIFY_CHANNEL_OVERRIDE = "whatsapp"
            NOTIFY_TARGET_OVERRIDE = str(delivery_target)

    if thread_id:
        # Thread sessions must always use Telegram notifications
        if args.notify_channel and args.notify_channel != "telegram":
            print("❌ Invalid routing: thread session requires --notify-channel telegram", file=sys.stderr)
            sys.exit(2)

        # Resolve target from session delivery context (mandatory for deterministic thread routing)
        resolved_target = (session_meta or {}).get("telegramTarget")
        if resolved_target:
            NOTIFY_CHANNEL_OVERRIDE = "telegram"
            NOTIFY_TARGET_OVERRIDE = resolved_target

        # If notify-session-id omitted, auto-resolve exact UUID by session key
        resolved_session_id = (session_meta or {}).get("sessionId")
        resolved_target = (session_meta or {}).get("telegramTarget")
        if not notify_session_id and resolved_session_id:
            notify_session_id = resolved_session_id

        # If caller provided notify-session-id but it mismatches the actual session key, fail hard
        if notify_session_id and resolved_session_id and notify_session_id != resolved_session_id:
            print(
                "❌ Invalid routing: --notify-session-id does not match --session key\n"
                f"   session key: {args.session}\n"
                f"   provided:   {notify_session_id}\n"
                f"   expected:   {resolved_session_id}",
                file=sys.stderr,
            )
            sys.exit(2)

        # Hard requirements for thread sessions
        if not NOTIFY_TARGET_OVERRIDE:
            print("❌ Invalid routing: thread session requires resolvable telegram target (auto-resolve failed)", file=sys.stderr)
            print("   Tip: ensure session exists in sessions_list or local thread session files", file=sys.stderr)
            sys.exit(2)
        if not notify_session_id:
            print("❌ Invalid routing: thread session requires --notify-session-id (auto-resolve failed)", file=sys.stderr)
            print("   Tip: pass --notify-session-id <uuid> from sessions_list", file=sys.stderr)
            sys.exit(2)

        # Ensure override is active after auto-resolution
        NOTIFY_CHANNEL_OVERRIDE = "telegram"

    # Safety guard: Telegram launches without explicit thread are error-prone and can drift across threads.
    ch_now, tgt_now = detect_channel(args.session or "")
    is_telegram_route = (ch_now == "telegram") or (args.notify_channel == "telegram")
    if is_telegram_route and not thread_id:
        # Non-thread Telegram is allowed for users/chats that do not use thread mode,
        # but guarded in auto mode if we detect ambiguity.
        tg_target = NOTIFY_TARGET_OVERRIDE or (session_meta or {}).get("telegramTarget")
        user_scope_key = bool(args.session and args.session.startswith("agent:main:telegram:user:"))
        if not tg_target and user_scope_key:
            tg_target = args.session.split(":")[-1]

        if args.telegram_routing_mode == "thread-only":
            print("❌ Unsafe routing blocked: Telegram launch requires thread session (:thread:<id>)", file=sys.stderr)
            print("   Use --session agent:main:main:thread:<id>", file=sys.stderr)
            sys.exit(2)

        if args.telegram_routing_mode == "allow-non-thread":
            pass  # explicitly allowed
        else:
            # auto mode guard #1: synthesized/ambiguous user-scope key must be explicit
            if user_scope_key and not args.allow_main_telegram:
                print("❌ Unsafe routing blocked: session key is non-thread user scope (agent:main:telegram:user:...).", file=sys.stderr)
                print("   For thread chats use --session agent:main:main:thread:<id>.", file=sys.stderr)
                print("   For intentional non-thread Telegram, pass --allow-main-telegram or --telegram-routing-mode allow-non-thread.", file=sys.stderr)
                sys.exit(2)

            # auto mode guard #2: if this target has recent thread sessions, treat non-thread as likely mistake
            if tg_target and has_recent_thread_session(token, str(tg_target), max_age_hours=24):
                if not args.allow_main_telegram:
                    print("❌ Unsafe routing blocked: recent thread session detected for this Telegram target.", file=sys.stderr)
                    print("   Use thread session key (:thread:<id>) or pass --allow-main-telegram to force non-thread.", file=sys.stderr)
                    sys.exit(2)

    # Deterministic Telegram target resolution: no manual --notify-target allowed.
    # If Telegram routing is requested/detected and target cannot be resolved, fail fast.
    if is_telegram_route:
        resolved_tg = NOTIFY_TARGET_OVERRIDE or (session_meta or {}).get("telegramTarget")
        user_scope_key = bool(args.session and args.session.startswith("agent:main:telegram:user:"))
        if not resolved_tg and user_scope_key:
            resolved_tg = args.session.split(":")[-1]
        if not resolved_tg:
            print("❌ Invalid routing: Telegram target could not be resolved from session metadata", file=sys.stderr)
            print("   Provide a valid thread/user session key resolvable via sessions_list/local files.", file=sys.stderr)
            sys.exit(2)
        NOTIFY_CHANNEL_OVERRIDE = "telegram"
        NOTIFY_TARGET_OVERRIDE = str(resolved_tg)

    if args.validate_only:
        ch, tgt = detect_channel(args.session or "")
        if args.session and not (ch and tgt):
            print("❌ Invalid routing: notification target could not be resolved from source session", file=sys.stderr)
            print(f"   session: {args.session}", file=sys.stderr)
            if session_meta:
                print(f"   resolved_session_id: {session_meta.get('sessionId')}", file=sys.stderr)
                print(f"   delivery_channel: {session_meta.get('deliveryChannel')}", file=sys.stderr)
                print(f"   delivery_target: {session_meta.get('deliveryTarget')}", file=sys.stderr)
            sys.exit(2)
        print("✅ Routing validation")
        print(f"   session: {args.session}")
        print(f"   thread_id: {thread_id}")
        print(f"   channel: {ch}")
        print(f"   target: {tgt}")
        print(f"   notify_session_id: {notify_session_id}")
        print(f"   allow_main_telegram: {args.allow_main_telegram}")
        print(f"   mode: {selected_mode_label}")
        if selected_model:
            print(f"   model: {selected_model}")
        print(f"   engine: {engine}")
        print(f"   fallback_engine: {args.fallback_engine}")
        print(f"   auth: {auth_label}")
        if session_meta:
            print(f"   resolved_session_id: {session_meta.get('sessionId')}")
            print(f"   resolved_telegram_target: {session_meta.get('telegramTarget')}")
            print(f"   resolved_delivery_channel: {session_meta.get('deliveryChannel')}")
            print(f"   resolved_delivery_target: {session_meta.get('deliveryTarget')}")
        sys.exit(0)

    exit_code = -1  # default; updated after the worker completes
    try:
        try:
            agent_bin = resolve_code_agent_bin(engine, agent_env)
        except FileNotFoundError as exc:
            if args.fallback_engine:
                return run_fallback(f"{engine} executable unavailable: {exc}")
            raise
        agent_env = prepare_cli_env(agent_env, agent_bin)
        # Write PID file
        pid_file = write_pid_file(args.task[:60])

        print(f"🔧 Starting {engine_label}...", file=sys.stderr)
        print(f"   Task: {args.task[:100]}", file=sys.stderr)
        print(f"   Project: {project}", file=sys.stderr)
        print(f"   Output: {output_file}", file=sys.stderr)
        print(f"   Mode: {selected_mode_label}", file=sys.stderr)
        if selected_model:
            print(f"   Model: {selected_model}", file=sys.stderr)
        print(f"   Engine: {engine}", file=sys.stderr)
        print(f"   CLI: {agent_bin}", file=sys.stderr)
        print(f"   Auth: {auth_label}", file=sys.stderr)
        if fallback_label:
            print(f"   Fallback: {fallback_label} on provider unavailability", file=sys.stderr)
        print(f"   Timeout: {args.timeout}s ({args.timeout//60}min)", file=sys.stderr)
        print(f"   Stall idle timeout: {STALL_IDLE_TIMEOUT}s ({STALL_IDLE_TIMEOUT//60}min)", file=sys.stderr)
        print(f"   Post-result grace: {STALL_POST_RESULT_GRACE}s (terminate after result_seen)", file=sys.stderr)
        print(f"   Stall guard mode: observe for idle stalls; terminate for post-result tail hangs", file=sys.stderr)
        if args.resume:
            print(f"   Resume: {args.resume}", file=sys.stderr)
        if args.session_label:
            print(f"   Label: {args.session_label}", file=sys.stderr)
        print(f"   PID: {os.getpid()}", file=sys.stderr)

        # Resume display in launch message: show session id for resumed runs, otherwise 'new'
        resume_display = args.resume if args.resume else "new"

        # Send launch info (informational)
        _ch, _tgt = detect_channel(args.session or "")
        trace_live(token, args.session, args.trace_live, "[RUN_TASK][START]",
                   f"project={project} run_id={run_id}", thread_id, reply_to_msg_id)
        if _tgt and token:
            launch_parts = [f"🚀 *{engine_label} started*"]
            if args.session_label:
                launch_parts.append(f"*Label:* {args.session_label}")
            launch_parts.append(f"*Project:* {project}")
            launch_parts.append(f"*Mode:* {selected_mode_label}")
            launch_parts.append(f"*Auth:* {auth_label}")
            if fallback_label:
                launch_parts.append(f"*Fallback:* {fallback_label} on provider unavailability")
            launch_parts.append(f"*Timeout:* {fmt_duration(args.timeout)}")
            launch_parts.append(f"*Resume:* {resume_display}")
            launch_parts.append(f"*PID:* {os.getpid()}")
            # Build launch message: use HTML + expandable blockquote for prompt
            def _esc(s: str) -> str:
                return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

            html_parts = [f"🚀 <b>{engine_label} started</b>"]
            if args.session_label:
                html_parts.append(f"<b>Label:</b> {_esc(args.session_label)}")
            html_parts.append(f"<b>Project:</b> {_esc(str(project))}")
            html_parts.append(f"<b>Mode:</b> {_esc(selected_mode_label)}")
            html_parts.append(f"<b>Auth:</b> {_esc(auth_label)}")
            if fallback_label:
                html_parts.append(
                    f"<b>Fallback:</b> {_esc(fallback_label)} on provider unavailability"
                )
            html_parts.append(f"<b>Timeout:</b> {_esc(fmt_duration(args.timeout))}")
            html_parts.append(f"<b>Resume:</b> {_esc(resume_display)}")
            html_parts.append(f"<b>PID:</b> {os.getpid()}")
            prompt_preview = args.task[:3500] + ("…" if len(args.task) > 3500 else "")
            html_parts.append(f"<b>Prompt:</b>\n<blockquote expandable>{_esc(prompt_preview)}</blockquote>")
            launch_html = "\n".join(html_parts)

            if thread_id and _ch == "telegram":
                # Use direct Bot API for thread routing + HTML parse mode
                send_telegram_direct(
                    _tgt, launch_html,
                    thread_id=thread_id, reply_to=reply_to_msg_id,
                    silent=True, parse_mode="HTML"
                )
            else:
                # Fallback: gateway message tool (non-thread sends, other channels)
                task_preview = args.task[:300] + ("…" if len(args.task) > 300 else "")
                launch_parts.append(f"Prompt: {task_preview}")
                send_channel(token, args.session or "", "\n".join(launch_parts),
                             silent=True, thread_id=thread_id, reply_to=reply_to_msg_id)

        # Build claude command
        # Create a progress notification script on disk so Claude Code can send
        # mid-task updates through a single local interface, regardless of channel.
        # This avoids exposing bot tokens or transport details in the task prompt.
        notify_script_path = None
        if _ch == "telegram" and _tgt and thread_id:
            bot_token_for_script = get_telegram_bot_token()
            if bot_token_for_script:
                notify_script_path = f"/tmp/cc-notify-{os.getpid()}.py"
                with open(notify_script_path, "w", encoding="utf-8") as _nf:
                    _nf.write(
                        "#!/usr/bin/env python3\n"
                        "import sys, json\n"
                        "try:\n"
                        "    import urllib.request\n"
                        f"    raw = sys.argv[1] if len(sys.argv) > 1 else 'Progress update'\n"
                        f"    prefix = {python_string_literal(f'📡 🟢 {progress_prefix}: ')}\n"
                        f"    msg = raw if raw.startswith(prefix) else (prefix + raw)\n"
                        f"    payload = json.dumps({{'chat_id': '{_tgt}', 'text': msg, "
                        f"'message_thread_id': {thread_id}, 'disable_notification': True}}).encode()\n"
                        f"    req = urllib.request.Request("
                        f"'https://api.telegram.org/bot{bot_token_for_script}/sendMessage', "
                        f"data=payload, headers={{'Content-Type': 'application/json'}})\n"
                        f"    urllib.request.urlopen(req, timeout=10)\n"
                        "except Exception as e:\n"
                        "    print(f'notify error: {e}', file=sys.stderr)\n"
                    )
                os.chmod(notify_script_path, 0o700)
        elif _ch == "whatsapp" and _tgt and token:
            notify_script_path = f"/tmp/cc-notify-{os.getpid()}.py"
            with open(notify_script_path, "w", encoding="utf-8") as _nf:
                _nf.write(
                    "#!/usr/bin/env python3\n"
                    "import sys, json\n"
                    "from urllib import request as urllib_request, error as urllib_error\n"
                    f"GW_URL = '{GW_URL}'\n"
                    f"TOKEN = {json.dumps(token)}\n"
                    f"TARGET = {json.dumps(_tgt)}\n"
                    "try:\n"
                    "    raw = sys.argv[1] if len(sys.argv) > 1 else 'Progress update'\n"
                    f"    prefix = {python_string_literal(f'📡 🟢 {progress_prefix}: ')}\n"
                    "    msg = raw if raw.startswith(prefix) else (prefix + raw)\n"
                    "    payload = json.dumps({\n"
                    "        'tool': 'message',\n"
                    "        'args': {\n"
                    "            'action': 'send',\n"
                    "            'channel': 'whatsapp',\n"
                    "            'target': TARGET,\n"
                    "            'message': msg,\n"
                    "        }\n"
                    "    }).encode('utf-8')\n"
                    "    req = urllib_request.Request(\n"
                    "        f'{GW_URL}/tools/invoke',\n"
                    "        data=payload,\n"
                    "        headers={\n"
                    "            'Authorization': f'Bearer {TOKEN}',\n"
                    "            'Content-Type': 'application/json',\n"
                    "        },\n"
                    "        method='POST'\n"
                    "    )\n"
                    "    with urllib_request.urlopen(req, timeout=15) as resp:\n"
                    "        body = resp.read().decode('utf-8', errors='replace')\n"
                    "        print(body)\n"
                    "except urllib_error.HTTPError as e:\n"
                    "    body = e.read().decode('utf-8', errors='replace')\n"
                    "    print(f'notify http error: {e.code}: {body}', file=sys.stderr)\n"
                    "    sys.exit(1)\n"
                    "except Exception as e:\n"
                    "    print(f'notify error: {e}', file=sys.stderr)\n"
                    "    sys.exit(1)\n"
                )
            os.chmod(notify_script_path, 0o700)

        # Prepend system context about notification script (avoids prompt-injection warnings)
        base_task = args.task

        # Resume runs already carry prior worker history, so large orchestration
        # prompts can overflow the prompt budget. Collapse to a short continuation form.
        if args.resume:
            collapsed = " ".join(args.task.split())
            if len(collapsed) > 700:
                collapsed = collapsed[:700].rstrip() + "…"
            base_task = (
                f"Continue the existing {engine_label} session from its current state. "
                "Treat prior session context as source of truth. Focus only on this next step:\n\n"
                + collapsed
            )

        task_prompt = base_task
        if notify_script_path:
            task_prompt = (
                f"[Automation context: a progress notification script is available at "
                f"{notify_script_path}. Run it with: "
                f"python3 {notify_script_path} 'your message text' — this sends a "
                f"message to the task owner. Use it once during the task to confirm progress. "
                f"Before starting any deliberate wait/sleep/backoff/rate-limit pause longer than "
                f"60 seconds, you MUST send a progress notification stating how long you are "
                f"about to wait and why, e.g. 'Waiting 15 minutes for X rate limit reset.' "
                f"Then send another progress notification when the wait is over.]\n\n"
                + base_task
            )

        agent_cmd = build_agent_command(engine, agent_bin, args, task_prompt, agent_env)

        # Start the selected coding agent.
        proc = subprocess.Popen(
            agent_cmd,
            cwd=str(project),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            env=agent_env,
            start_new_session=True,
        )

        # Activity tracking state
        state = {
            "tool_calls": 0,
            "files_written": [],
            "last_activity": "",
            "session_id": None,         # Captured from stream-json init event
            "resolved_model": selected_model if engine == "codex" else None,
            "last_event_time": time.time(),
            "last_semantic_progress_time": time.time(),
            "output_tokens": 0,
            "chunks_since_heartbeat": 0,
            "active_subagent_ids": set(),
            "result_seen": False,
            "result_seen_time": None,
            "billing_label": billing_label,
            "last_progress_signature": None,
            "flat_heartbeats": 0,
            "stall_notice_sent": False,
            # Result-level cost and usage (populated from stream-json result event)
            "result_cost_usd": None,
            "result_usage": None,
            "result_duration_ms": None,
            "result_num_turns": None,
            "last_agent_message": "",
            "terminal_error": "",
        }

        start = time.time()
        last_heartbeat = 0
        output_lines = deque(maxlen=max(100, MAX_STREAM_LINES))
        stderr_lines = deque(maxlen=max(100, MAX_STREAM_LINES))
        timed_out = False
        stalled_out = False
        post_result_terminated = False

        # Read stdout in background thread
        def stdout_reader():
            for line in proc.stdout:
                line = line.strip()
                if line:
                    output_lines.append(line)
                    parse_stream_line(line, state, engine=engine)

        def stderr_reader():
            for line in proc.stderr:
                stderr_lines.append(line.rstrip("\n"))

        stdout_thread = threading.Thread(target=stdout_reader, daemon=True)
        stderr_thread = threading.Thread(target=stderr_reader, daemon=True)
        stdout_thread.start()
        stderr_thread.start()

        # Main loop: poll process, send heartbeats, check timeout
        while proc.poll() is None:
            time.sleep(POLL_INTERVAL)
            if proc.poll() is not None:
                break
            elapsed = int(time.time() - start)

            # Timeout check
            if elapsed >= args.timeout:
                timed_out = True
                print(f"⏰ Timeout ({args.timeout}s) reached, killing process...", file=sys.stderr)
                kill_process_graceful(proc)
                break

            # Stall check candidate (semantic progress based)
            idle_secs = time.time() - state["last_event_time"]
            progress_idle_secs = time.time() - state["last_semantic_progress_time"]
            post_result_idle_secs = 0
            if state.get("result_seen_time"):
                post_result_idle_secs = time.time() - state["result_seen_time"]

            stall_reason = None
            stall_mode = None
            if STALL_IDLE_TIMEOUT > 0 and progress_idle_secs >= STALL_IDLE_TIMEOUT and state.get("flat_heartbeats", 0) >= 3:
                stall_mode = "observe"
                stall_reason = f"no semantic progress for {int(progress_idle_secs)}s + flat heartbeats={state.get('flat_heartbeats', 0)}"
            elif state.get("result_seen") and STALL_POST_RESULT_GRACE > 0 and post_result_idle_secs >= STALL_POST_RESULT_GRACE:
                stall_mode = "terminate"
                stall_reason = f"result_seen but process still alive after {int(post_result_idle_secs)}s grace"

            if stall_reason and not state.get("stall_notice_sent") and token:
                state["stall_notice_sent"] = True
                action = "terminating" if stall_mode == "terminate" else "would terminate"
                warn = (
                    f"🧊 Stall guard: {action} this run\n"
                    f"Reason: {stall_reason}\n"
                    f"Mode: {stall_mode}"
                )
                send_channel(token, args.session or "", warn, silent=False, thread_id=thread_id, reply_to=reply_to_msg_id)

            if stall_mode == "terminate":
                post_result_terminated = True
                print(f"🧊 Post-result tail hang detected; terminating child: {stall_reason}", file=sys.stderr)
                kill_process_graceful(proc)
                break

            # Heartbeat every 60s
            _hb_ch, _hb_tgt = detect_channel(args.session or "")
            if elapsed - last_heartbeat >= 60 and _hb_tgt and token:
                last_heartbeat = elapsed
                mins = elapsed // 60

                # Flatline detection (for smart stall guard)
                progress_signature = (
                    state.get("output_tokens", 0),
                    state.get("tool_calls", 0),
                    state.get("last_activity", ""),
                    len(state.get("active_subagent_ids", set())),
                )
                if state.get("last_progress_signature") == progress_signature:
                    state["flat_heartbeats"] = state.get("flat_heartbeats", 0) + 1
                else:
                    state["flat_heartbeats"] = 0
                state["last_progress_signature"] = progress_signature

                # Status emoji based on liveness
                if idle_secs < 30:
                    status = "🟢"
                elif idle_secs < 120:
                    status = "🟡"
                else:
                    status = "🔴"

                parts = [f"{status} {progress_prefix} ({mins}min)"]
                parts.append(f"sub:{len(state['active_subagent_ids'])}")
                if state["output_tokens"] > 0:
                    parts.append(f"{format_tokens(state['output_tokens'])} tok")
                if state["tool_calls"] > 0:
                    parts.append(f"{state['tool_calls']} calls")
                if idle_secs > 120:
                    parts.append(f"🧠 Thinking... ({int(idle_secs)}s)")
                elif idle_secs > 15 and state["chunks_since_heartbeat"] == 0:
                    parts.append(f"🧠 Thinking...")
                elif state["last_activity"]:
                    activity = state["last_activity"]
                    if state["chunks_since_heartbeat"] > 0:
                        activity += " ✍️"
                    parts.append(activity)

                state["chunks_since_heartbeat"] = 0
                send_channel(token, args.session or "", " | ".join(parts), silent=True, thread_id=thread_id, reply_to=reply_to_msg_id)

        stdout_thread.join(timeout=5)
        stderr_thread.join(timeout=5)
        stderr_output = "\n".join(stderr_lines)

        preliminary_exit = proc.returncode if proc.returncode is not None else -1
        fallback_reason = provider_unavailable_reason(
            engine,
            preliminary_exit,
            stderr_output,
            state,
            timed_out=timed_out,
        )
        if args.fallback_engine and fallback_reason:
            if notify_script_path and Path(notify_script_path).exists():
                Path(notify_script_path).unlink(missing_ok=True)
                notify_script_path = None
            return run_fallback(fallback_reason)

        # Check for resume failure. This means the worker did not start the task;
        # wake the supervising agent so it can relaunch fresh when safe.
        resume_failed = resume_failure_detected(engine, args.resume, stderr_output)
        if resume_failed:
            exit_code = RESUME_FAILED_EXIT_CODE
            failure_output = (
                "RESUME_FAILED\n\n"
                f"{engine_label} did not start because --resume session was not found or expired.\n"
                f"Resume session: {args.resume}\n"
                f"Project: {project}\n"
                f"Task: {args.task}\n\n"
                "Recommended supervisor action:\n"
                "- Treat this as a launch failure, not task progress.\n"
                "- If the user did not require this exact old session, relaunch the same task fresh/new session without --resume.\n"
                "- Preserve the original Definition of Done and self-verification requirements.\n\n"
                f"stderr:\n{stderr_output[:4000]}"
            )
            Path(output_file).write_text(failure_output)
            print(f"❌ Resume failed: session {args.resume} not found", file=sys.stderr)
            if args.session and token:
                html_failure = (
                    f"❌ <b>{engine_label} resume failed</b>\n\n"
                    f"<b>Session:</b> <code>{args.resume}</code> not found or expired.\n"
                    "<b>Status:</b> task did not start; waking supervisor agent to decide/relaunch fresh.\n"
                    f"<b>Output:</b> <code>{output_file}</code>"
                )
                notify_session(token, args.session, group_jid,
                    f"❌ {engine_label} resume failed\n\n"
                    f"Session ID `{args.resume}` not found or expired.\n"
                    f"Task did not start; supervisor should relaunch fresh if safe.\n"
                    f"Full output: {output_file}",
                    thread_id=thread_id, notify_session_id=notify_session_id, reply_to=reply_to_msg_id,
                    html_msg=html_failure,
                    exit_code=exit_code, project_name=str(project.name), output_file_path=output_file,
                    trace_enabled=args.trace_live, run_id=run_id, wake_id=wake_id, state_path=state_path,
                    engine=engine)
                print("📨 Resume failure notified", file=sys.stderr)
            return RESUME_FAILED_EXIT_CODE

        final_text = extract_final_text(engine, output_lines, state, stderr_output)

        # Save output
        output = final_text
        Path(output_file).write_text(output)

        exit_code = proc.returncode if proc.returncode is not None else -1
        if post_result_terminated and final_text:
            # The worker already emitted a result; the remaining child
            # process lifetime is a tail hang. Treat the run as completed so
            # the result is saved, registered, and delivered normally.
            exit_code = 0
        output_size = len(output)
        preview = output[:2000]
        elapsed_min = int((time.time() - start) / 60)

        status = "🧊 STALL" if stalled_out else ("⏰ TIMEOUT" if timed_out else ("✅" if exit_code == 0 else "❌"))
        print(f"{status} Done (exit {exit_code}, {output_size} chars, {elapsed_min}min)", file=sys.stderr)

        # Register session in registry
        if state.get("session_id"):
            try:
                session_status = "stalled" if stalled_out else ("timeout" if timed_out else ("completed" if exit_code == 0 else "failed"))
                register_session(
                    session_id=state["session_id"],
                    label=args.session_label,
                    task=args.task,
                    project_dir=str(project),
                    openclaw_session=args.session,
                    output_file=output_file,
                    status=session_status,
                    engine=engine,
                    mode=selected_mode_label,
                    model=state.get("resolved_model") or selected_model,
                )
                print(f"📝 Session registered: {state['session_id']}", file=sys.stderr)
                # Surface the actual provider session ID for future --resume usage.
                if args.session and token:
                    send_channel(
                        token,
                        args.session,
                        f"📝 Session: {state['session_id']}",
                        bg_prefix=False,
                        silent=True,
                        thread_id=thread_id,
                        reply_to=reply_to_msg_id,
                    )
            except Exception as e:
                print(f"⚠️  Failed to register session: {e}", file=sys.stderr)

        # Notify session with result
        if args.session and token:
            trace_live(token, args.session, args.trace_live, "[RUN_TASK][COMPLETE]",
                       f"exit={exit_code} output={output_file} size={output_size} run_id={run_id} wake_id={wake_id}", thread_id, reply_to_msg_id)
            def _e(s: str) -> str:
                return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

            # Mode is always known. Model is shown only if the provider reported it
            # or the caller explicitly pinned it. We never claim a model the wrapper did
            # not actually pass.
            resolved_model = state.get("resolved_model")
            cost_summary = format_cost_summary(state)
            finish_status_parts = [f"Mode: {selected_mode_label}"]
            if post_result_terminated:
                finish_status_parts.append("Post-result tail hang recovered")
            if resolved_model:
                finish_status_parts.append(f"Model: {resolved_model}")
            finish_status_line = " | ".join(finish_status_parts)
            finish_status_html_parts = [f"<b>Mode:</b> {_e(selected_mode_label)}"]
            if post_result_terminated:
                finish_status_html_parts.append("<b>Post-result tail hang:</b> recovered")
            if resolved_model:
                finish_status_html_parts.append(f"<b>Model:</b> {_e(resolved_model)}")
            finish_status_html = " | ".join(finish_status_html_parts)
            finish_cost_html = (
                f"<b>Est. cost:</b> {_e(cost_summary)}\n" if cost_summary else ""
            )

            if stalled_out:
                elapsed = fmt_duration(int(time.time() - start))
                msg = (
                    f"🧊 {engine_label} stalled and was stopped after {elapsed} "
                    f"(idle limit: {fmt_duration(STALL_IDLE_TIMEOUT)})\n\n"
                    f"Task: {args.task[:200]}\n"
                    f"Project: {project}\n"
                    f"{finish_status_line}\n"
                    + (f"Est. cost: {cost_summary}\n" if cost_summary else "")
                    + f"Tool calls: {state['tool_calls']}\n\n"
                    f"Partial result ({output_size} chars):\n\n{preview}\n\n"
                    f"Full output: {output_file}"
                )
                html_msg = (
                    f"🧊 <b>{engine_label} stalled and was stopped</b> after {_e(elapsed)} "
                    f"(idle limit: {_e(fmt_duration(STALL_IDLE_TIMEOUT))})\n\n"
                    f"<b>Task:</b> {_e(args.task[:200])}\n"
                    f"<b>Project:</b> {_e(str(project))}\n"
                    f"{finish_status_html}\n"
                    f"{finish_cost_html}"
                    f"<b>Tool calls:</b> {state['tool_calls']}\n\n"
                    f"<b>Partial result</b> ({output_size} chars):\n"
                    f"<blockquote expandable>{_e(preview)}</blockquote>\n"
                    f"<b>Full output:</b> <code>{_e(str(output_file))}</code>"
                )
            elif timed_out:
                elapsed = fmt_duration(int(time.time() - start))
                msg = (
                    f"⏰ {engine_label} timed out after {elapsed} "
                    f"(limit: {fmt_duration(args.timeout)})\n\n"
                    f"Task: {args.task[:200]}\n"
                    f"Project: {project}\n"
                    f"{finish_status_line}\n"
                    + (f"Est. cost: {cost_summary}\n" if cost_summary else "")
                    + f"Tool calls: {state['tool_calls']}\n\n"
                    f"Partial result ({output_size} chars):\n\n{preview}\n\n"
                    f"Full output: {output_file}"
                )
                html_msg = (
                    f"⏰ <b>{engine_label} timed out</b> after {_e(elapsed)} "
                    f"(limit: {_e(fmt_duration(args.timeout))})\n\n"
                    f"<b>Task:</b> {_e(args.task[:200])}\n"
                    f"<b>Project:</b> {_e(str(project))}\n"
                    f"{finish_status_html}\n"
                    f"{finish_cost_html}"
                    f"<b>Tool calls:</b> {state['tool_calls']}\n\n"
                    f"<b>Partial result</b> ({output_size} chars):\n"
                    f"<blockquote expandable>{_e(preview)}</blockquote>\n"
                    f"<b>Full output:</b> <code>{_e(str(output_file))}</code>"
                )
            elif exit_code == 0:
                trunc = "...(truncated)" if output_size > 2000 else ""
                msg = (
                    f"✅ {engine_label} task complete!\n\n"
                    f"Task: {args.task[:200]}\n"
                    f"Project: {project}\n"
                    f"{finish_status_line}\n"
                    + (f"Est. cost: {cost_summary}\n" if cost_summary else "")
                    + f"Result ({output_size} chars):\n\n{preview}\n{trunc}\n"
                    f"Full output: {output_file}"
                )
                html_msg = (
                    f"✅ <b>{engine_label} task complete!</b>\n\n"
                    f"<b>Task:</b> {_e(args.task[:200])}\n"
                    f"<b>Project:</b> {_e(str(project))}\n"
                    f"{finish_status_html}\n"
                    f"{finish_cost_html}"
                    f"<b>Result</b> ({output_size} chars):\n"
                    f"<blockquote expandable>{_e(preview)}</blockquote>\n"
                    f"{_e(trunc)}"
                    f"<b>Full output:</b> <code>{_e(str(output_file))}</code>"
                )
            else:
                msg = (
                    f"❌ {engine_label} error (exit {exit_code})\n\n"
                    f"Task: {args.task[:200]}\n"
                    f"Project: {project}\n"
                    f"{finish_status_line}\n\n"
                    f"{preview}"
                )
                html_msg = (
                    f"❌ <b>{engine_label} error</b> (exit {exit_code})\n\n"
                    f"<b>Task:</b> {_e(args.task[:200])}\n"
                    f"<b>Project:</b> {_e(str(project))}\n"
                    f"{finish_status_html}\n\n"
                    f"<blockquote expandable>{_e(preview)}</blockquote>"
                )

            notification_ok = notify_session(
                token, args.session, group_jid, msg,
                thread_id=thread_id, notify_session_id=notify_session_id,
                reply_to=reply_to_msg_id, html_msg=html_msg,
                exit_code=exit_code, project_name=str(project.name), output_file_path=output_file,
                trace_enabled=args.trace_live, run_id=run_id, wake_id=wake_id, state_path=state_path,
                engine=engine,
            )
            if notification_ok:
                print("📨 Session notified", file=sys.stderr)
            else:
                print("❌ Final delivery/wake was not confirmed", file=sys.stderr)
                if state.get("session_id"):
                    try:
                        update_session(state["session_id"], status="delivery_failed")
                    except Exception:
                        pass
                if exit_code == 0:
                    exit_code = ORCHESTRATION_FAILED_EXIT_CODE

        if timed_out:
            return TIMEOUT_EXIT_CODE
        return exit_code

    except Exception as e:
        # Crash-safe: always try to notify
        print(f"💥 Crash: {e}", file=sys.stderr)
        import traceback
        traceback.print_exc(file=sys.stderr)

        if proc and proc.poll() is None:
            kill_process_graceful(proc)

        if args.session and token:
            try:
                notify_session(token, args.session, group_jid,
                    f"💥 {engine_label} runner crashed!\n\n"
                    f"**Task:** {args.task[:200]}\n"
                    f"**Error:** {str(e)[:500]}",
                    thread_id=thread_id, notify_session_id=notify_session_id, reply_to=reply_to_msg_id,
                           exit_code=exit_code, project_name=str(project.name), output_file_path=output_file,
                           trace_enabled=args.trace_live, run_id=run_id, wake_id=wake_id, state_path=state_path,
                           engine=engine)
            except Exception:
                pass

        # Fallback: direct channel notification
        _fb_ch, _fb_tgt = detect_channel(args.session or "")
        if _fb_tgt and token and not args.session:
            send_channel(token, args.session or "", f"💥 {engine_label} crash: {str(e)[:200]}", thread_id=thread_id, reply_to=reply_to_msg_id)

        return CRASH_EXIT_CODE

    finally:
        # Cleanup PID file
        if pid_file and pid_file.exists():
            pid_file.unlink(missing_ok=True)
        # Cleanup notification script
        if notify_script_path and Path(notify_script_path).exists():
            Path(notify_script_path).unlink(missing_ok=True)


if __name__ == "__main__":
    raise SystemExit(main())
