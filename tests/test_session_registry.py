import importlib.util
import json
import tempfile
import threading
import unittest
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
REGISTRY_MODULE = SKILL_DIR / "session_registry.py"


def load_registry():
    spec = importlib.util.spec_from_file_location("claude_session_registry_test", REGISTRY_MODULE)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class SessionRegistryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.registry = load_registry()
        self.registry.REGISTRY_FILE = Path(self.tmp.name) / "claude_sessions.json"

    def tearDown(self):
        self.tmp.cleanup()

    def register(self, session_id, engine="claude", mode=None, model=None):
        self.registry.register_session(
            session_id=session_id,
            label=session_id,
            task="task",
            project_dir=self.tmp.name,
            openclaw_session=None,
            output_file=f"/tmp/{session_id}.txt",
            status="completed",
            engine=engine,
            mode=mode,
            model=model,
        )

    def test_concurrent_registrations_are_not_lost(self):
        threads = [threading.Thread(target=self.register, args=(f"session-{i}",)) for i in range(30)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        data = json.loads(self.registry.REGISTRY_FILE.read_text())
        self.assertEqual(len(data["sessions"]), 30)

    def test_corrupt_registry_is_preserved_before_recovery(self):
        self.registry.REGISTRY_FILE.write_text("not-json")
        self.register("session-ok")
        backups = list(self.registry.REGISTRY_FILE.parent.glob("*.corrupt-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "not-json")
        data = json.loads(self.registry.REGISTRY_FILE.read_text())
        self.assertIn("session-ok", data["sessions"])

    def test_update_session_changes_status(self):
        self.register("session-update")
        self.assertTrue(self.registry.update_session("session-update", status="delivery_failed"))
        self.assertEqual(
            self.registry.get_session("session-update")["status"],
            "delivery_failed",
        )

    def test_registry_records_engine_and_defaults_legacy_entries_to_claude(self):
        self.register(
            "codex-thread",
            engine="codex",
            mode="sol",
            model="gpt-5.6-sol",
        )
        entry = self.registry.get_session("codex-thread")
        self.assertEqual(entry["engine"], "codex")
        self.assertEqual(entry["mode"], "sol")
        self.assertEqual(entry["model"], "gpt-5.6-sol")

        data = json.loads(self.registry.REGISTRY_FILE.read_text())
        data["sessions"]["legacy"] = {
            "session_id": "legacy",
            "last_accessed": data["sessions"]["codex-thread"]["last_accessed"],
        }
        self.registry.REGISTRY_FILE.write_text(json.dumps(data))
        self.assertEqual(self.registry.get_session("legacy")["engine"], "claude")

    def test_peek_session_does_not_change_last_accessed(self):
        self.register("peek", engine="codex")
        before = json.loads(self.registry.REGISTRY_FILE.read_text())["sessions"]["peek"]
        peeked = self.registry.peek_session("peek")
        after = json.loads(self.registry.REGISTRY_FILE.read_text())["sessions"]["peek"]
        self.assertEqual(peeked["engine"], "codex")
        self.assertEqual(after["last_accessed"], before["last_accessed"])


if __name__ == "__main__":
    unittest.main()
