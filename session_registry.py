#!/usr/bin/env python3
"""
Session registry for Claude Code and Codex tasks.
Stores metadata about completed/running sessions for resumption and tracking.

Registry format: ~/.openclaw/claude_sessions.json
{
  "sessions": {
    "<session-id>": {
      "session_id": "...",
      "engine": "claude|codex",
      "label": "Research on topic X",
      "task_summary": "first 200 chars of task...",
      "project_dir": "/absolute/path",
      "created_at": "ISO timestamp",
      "last_accessed": "ISO timestamp",
      "status": "completed|failed|timeout",
      "openclaw_session": "agent:main:whatsapp:group:...",
      "output_file": "/tmp/cc-YYYYMMDD-HHMMSS.txt",
      "cost_estimate": null
    }
  }
}
"""

import json
import os
import fcntl
import shutil
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Optional, Dict, List


REGISTRY_FILE = Path.home() / ".openclaw" / "claude_sessions.json"


def _lock_file() -> Path:
    return REGISTRY_FILE.with_suffix(REGISTRY_FILE.suffix + ".lock")


@contextmanager
def _registry_lock(exclusive: bool = True):
    REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    with _lock_file().open("a+") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX if exclusive else fcntl.LOCK_SH)
        try:
            yield
        finally:
            fcntl.flock(lock.fileno(), fcntl.LOCK_UN)


def _ensure_registry_unlocked() -> Dict:
    """Ensure registry file exists. Caller must hold the registry lock."""
    if not REGISTRY_FILE.exists():
        data = {"sessions": {}}
        _save_registry_unlocked(data)
        return data

    try:
        data = json.loads(REGISTRY_FILE.read_text())
        if not isinstance(data.get("sessions"), dict):
            raise ValueError("registry sessions must be an object")
        for entry in data["sessions"].values():
            if isinstance(entry, dict):
                entry.setdefault("engine", "claude")
        return data
    except (json.JSONDecodeError, KeyError, TypeError, ValueError):
        # Preserve forensic evidence before recovering with an empty registry.
        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
        backup = REGISTRY_FILE.with_name(
            f"{REGISTRY_FILE.name}.corrupt-{stamp}-{uuid.uuid4().hex[:8]}"
        )
        shutil.copy2(REGISTRY_FILE, backup)
        os.chmod(backup, 0o600)
        data = {"sessions": {}}
        _save_registry_unlocked(data)
        return data


def _save_registry_unlocked(data: Dict):
    """Atomically save registry data. Caller must hold the registry lock."""
    REGISTRY_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = REGISTRY_FILE.with_name(
        f".{REGISTRY_FILE.name}.{os.getpid()}.{uuid.uuid4().hex}.tmp"
    )
    tmp.write_text(json.dumps(data, indent=2))
    os.chmod(tmp, 0o600)
    os.replace(tmp, REGISTRY_FILE)


def _ensure_registry() -> Dict:
    """Return a consistent registry snapshot."""
    with _registry_lock():
        return _ensure_registry_unlocked()


def register_session(
    session_id: str,
    label: Optional[str],
    task: str,
    project_dir: str,
    openclaw_session: Optional[str],
    output_file: str,
    status: str = "running",
    engine: str = "claude",
    mode: Optional[str] = None,
    model: Optional[str] = None,
) -> Dict:
    """
    Register a new session in the registry.

    Args:
        session_id: Provider session/thread ID
        label: Human-readable label for the session
        task: Full task description (will be truncated to 200 chars)
        project_dir: Absolute path to project directory
        openclaw_session: OpenClaw session key that launched this task
        output_file: Path to output file
        status: Session status (running|completed|failed|timeout)
        engine: Worker engine (claude|codex)
        mode: Runner semantic mode label
        model: Resolved or explicitly selected provider model

    Returns:
        The created session entry
    """
    with _registry_lock():
        data = _ensure_registry_unlocked()
        now = datetime.now().isoformat()
        entry = {
            "session_id": session_id,
            "engine": engine,
            "mode": mode,
            "model": model,
            "label": label,
            "task_summary": task[:200],
            "project_dir": str(Path(project_dir).absolute()),
            "created_at": now,
            "last_accessed": now,
            "status": status,
            "openclaw_session": openclaw_session,
            "output_file": output_file,
            "cost_estimate": None
        }
        data["sessions"][session_id] = entry
        _save_registry_unlocked(data)
        return entry


def get_session(session_id: str) -> Optional[Dict]:
    """
    Get session entry by ID.

    Returns:
        Session entry dict or None if not found
    """
    with _registry_lock():
        data = _ensure_registry_unlocked()
        entry = data["sessions"].get(session_id)
        if entry:
            entry["last_accessed"] = datetime.now().isoformat()
            data["sessions"][session_id] = entry
            _save_registry_unlocked(data)
        return entry


def peek_session(session_id: str) -> Optional[Dict]:
    """Read a session without updating timestamps or recovering registry state."""
    if not REGISTRY_FILE.exists():
        return None
    with _registry_lock(exclusive=False):
        try:
            data = json.loads(REGISTRY_FILE.read_text())
            sessions = data.get("sessions")
            if not isinstance(sessions, dict):
                return None
            entry = sessions.get(session_id)
            if not isinstance(entry, dict):
                return None
            result = dict(entry)
            result.setdefault("engine", "claude")
            return result
        except (OSError, json.JSONDecodeError, TypeError, ValueError):
            return None


def list_recent_sessions(hours: int = 72) -> List[Dict]:
    """
    List sessions accessed within the last N hours.

    Args:
        hours: Time window in hours (default: 72 = 3 days)

    Returns:
        List of session entries, sorted by last_accessed (newest first)
    """
    with _registry_lock():
        data = _ensure_registry_unlocked()
    cutoff = datetime.now() - timedelta(hours=hours)

    recent = []
    for session_id, entry in data["sessions"].items():
        try:
            last_access = datetime.fromisoformat(entry["last_accessed"])
            if last_access >= cutoff:
                recent.append(entry)
        except (ValueError, KeyError):
            continue

    # Sort by last_accessed, newest first
    recent.sort(key=lambda x: x.get("last_accessed", ""), reverse=True)
    return recent


def find_session_by_label(label: str) -> Optional[Dict]:
    """
    Find session by label (case-insensitive substring match).

    Args:
        label: Label to search for

    Returns:
        First matching session entry or None
    """
    with _registry_lock():
        data = _ensure_registry_unlocked()
    label_lower = label.lower()

    # First try exact match
    for session_id, entry in data["sessions"].items():
        entry_label = (entry.get("label") or "")
        if entry_label.lower() == label_lower:
            return entry

    # Then try substring match
    for session_id, entry in data["sessions"].items():
        entry_label = (entry.get("label") or "")
        if label_lower in entry_label.lower():
            return entry

    return None


def update_session(session_id: str, **kwargs) -> bool:
    """
    Update session entry fields.

    Args:
        session_id: Session ID to update
        **kwargs: Fields to update (status, label, output_file, etc.)

    Returns:
        True if session was found and updated, False otherwise
    """
    with _registry_lock():
        data = _ensure_registry_unlocked()
        if session_id not in data["sessions"]:
            return False
        entry = data["sessions"][session_id]
        entry["last_accessed"] = datetime.now().isoformat()
        for key, value in kwargs.items():
            if key in entry:
                entry[key] = value
        data["sessions"][session_id] = entry
        _save_registry_unlocked(data)
        return True


def cleanup_old_sessions(days: int = 30) -> int:
    """
    Remove sessions older than N days.

    Args:
        days: Age threshold in days (default: 30)

    Returns:
        Number of sessions removed
    """
    with _registry_lock():
        data = _ensure_registry_unlocked()
        cutoff = datetime.now() - timedelta(days=days)
        to_remove = []
        for session_id, entry in data["sessions"].items():
            try:
                last_access = datetime.fromisoformat(entry["last_accessed"])
                if last_access < cutoff:
                    to_remove.append(session_id)
            except (ValueError, KeyError):
                to_remove.append(session_id)
        for session_id in to_remove:
            del data["sessions"][session_id]
        if to_remove:
            _save_registry_unlocked(data)
        return len(to_remove)
