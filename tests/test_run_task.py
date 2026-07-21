import ast
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import textwrap
import unittest
from unittest import mock
from pathlib import Path


SKILL_DIR = Path(__file__).resolve().parents[1]
RUNNER = SKILL_DIR / "run-task.py"


def load_runner():
    spec = importlib.util.spec_from_file_location("claude_code_task_runner", RUNNER)
    module = importlib.util.module_from_spec(spec)
    sys.path.insert(0, str(SKILL_DIR))
    try:
        spec.loader.exec_module(module)
    finally:
        sys.path.pop(0)
    return module


class RunnerFunctionTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.runner = load_runner()

    def test_extract_thread_id_supports_simple_and_composite_keys(self):
        self.assertEqual(
            self.runner.extract_thread_id("agent:main:main:thread:369520"),
            "369520",
        )
        self.assertEqual(
            self.runner.extract_thread_id("agent:main:main:thread:112087171:369520"),
            "369520",
        )

    def test_child_env_removes_api_key(self):
        old = os.environ.get("ANTHROPIC_API_KEY")
        os.environ["ANTHROPIC_API_KEY"] = "must-not-leak"
        try:
            self.assertNotIn("ANTHROPIC_API_KEY", self.runner.claude_code_child_env())
        finally:
            if old is None:
                os.environ.pop("ANTHROPIC_API_KEY", None)
            else:
                os.environ["ANTHROPIC_API_KEY"] = old

    def test_codex_child_env_removes_api_credentials(self):
        keys = ("OPENAI_API_KEY", "CODEX_API_KEY", "CODEX_ACCESS_TOKEN", "CODEX_HOME")
        old = {key: os.environ.get(key) for key in keys}
        try:
            for key in keys:
                os.environ[key] = "must-not-leak"
            child = self.runner.code_agent_child_env("codex")
            for key in keys:
                self.assertNotIn(key, child)
        finally:
            for key, value in old.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value

    def test_generated_progress_prefix_is_real_utf8_not_surrogates(self):
        prefix = "📡 🟢 Codex: "
        literal = self.runner.python_string_literal(prefix)
        self.assertNotIn("\\ud", literal)
        self.assertEqual(ast.literal_eval(literal), prefix)

    def test_tool_response_requires_gateway_and_tool_success(self):
        response = self.runner._SimpleResponse
        self.assertTrue(
            self.runner.tool_response_ok(response(200, '{"ok":true,"result":{}}'))
        )
        self.assertFalse(
            self.runner.tool_response_ok(
                response(200, '{"ok":false,"error":"denied"}')
            )
        )
        self.assertFalse(
            self.runner.tool_response_ok(
                response(200, '{"ok":true,"result":{"isError":true}}')
            )
        )
        self.assertTrue(self.runner.tool_response_ok(response(
            200,
            '{"ok":true,"result":{"details":{"status":"accepted"}}}',
        )))
        self.assertFalse(self.runner.tool_response_ok(response(
            200,
            '{"ok":true,"result":{"content":[{"type":"text",'
            '"text":"{\\"status\\":\\"error\\"}"}]}}',
        )))
        self.assertFalse(self.runner.tool_response_ok(response(500, "{}")))

    def test_local_session_lookup_skips_corrupt_agent_registry(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            bad = root / ".openclaw/agents/a/sessions/sessions.json"
            good = root / ".openclaw/agents/b/sessions/sessions.json"
            bad.parent.mkdir(parents=True)
            good.parent.mkdir(parents=True)
            bad.write_text("not json")
            key = "agent:assistant:whatsapp:group:123@g.us"
            good.write_text(json.dumps({
                key: {
                    "sessionId": "session-123",
                    "deliveryContext": {
                        "channel": "whatsapp",
                        "to": "123@g.us",
                    },
                }
            }))
            old_home = os.environ.get("HOME")
            os.environ["HOME"] = str(root)
            try:
                meta = self.runner.resolve_session_meta_from_local_registry(key)
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home
            self.assertEqual(meta["sessionId"], "session-123")
            self.assertEqual(meta["deliveryTarget"], "123@g.us")

    def test_claude_binary_resolves_from_nvm_without_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            binary = root / ".nvm/versions/node/v22.0.0/bin/claude"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/bin/sh\n")
            binary.chmod(0o755)
            old_home = os.environ.get("HOME")
            os.environ["HOME"] = str(root)
            try:
                resolved = self.runner.resolve_claude_bin({"PATH": "/empty"})
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home
            self.assertEqual(resolved, str(binary))

    def test_codex_binary_resolves_from_nvm_and_prepares_node_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            binary = root / ".nvm/versions/node/v22.0.0/bin/codex"
            binary.parent.mkdir(parents=True)
            binary.write_text("#!/usr/bin/env node\n")
            binary.chmod(0o755)
            old_home = os.environ.get("HOME")
            os.environ["HOME"] = str(root)
            try:
                resolved = self.runner.resolve_code_agent_bin("codex", {"PATH": "/empty"})
                prepared = self.runner.prepare_cli_env({"PATH": "/empty"}, resolved)
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home
            self.assertEqual(resolved, str(binary))
            self.assertEqual(
                Path(prepared["PATH"].split(os.pathsep)[0]),
                binary.parent,
            )

    def test_codex_shim_without_adjacent_node_gets_nvm_node_path(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            shim = root / "bin/codex"
            node = root / ".nvm/versions/node/v22.0.0/bin/node"
            shim.parent.mkdir(parents=True)
            node.parent.mkdir(parents=True)
            shim.write_text("#!/usr/bin/env node\n")
            node.write_text("#!/bin/sh\n")
            shim.chmod(0o755)
            node.chmod(0o755)
            old_home = os.environ.get("HOME")
            os.environ["HOME"] = str(root)
            try:
                prepared = self.runner.prepare_cli_env({"PATH": "/empty"}, str(shim))
            finally:
                if old_home is None:
                    os.environ.pop("HOME", None)
                else:
                    os.environ["HOME"] = old_home
            path_dirs = [Path(p) for p in prepared["PATH"].split(os.pathsep)]
            self.assertEqual(path_dirs[0], shim.parent)
            self.assertEqual(path_dirs[1], node.parent)

    def test_detached_worker_argv_removes_only_launcher_flags(self):
        self.assertEqual(
            self.runner.detached_worker_argv([
                "--detach",
                "--task", "hello",
                "--detach-log", "/tmp/a.log",
                "--engine", "codex",
            ]),
            ["--task", "hello", "--engine", "codex"],
        )
        self.assertEqual(
            self.runner.detached_worker_argv([
                "--task", "hello",
                "--detach-log=/tmp/a.log",
                "--timeout", "10",
            ]),
            ["--task", "hello", "--timeout", "10"],
        )

    def test_detached_launch_uses_transient_user_service(self):
        completed = mock.Mock(returncode=0, stdout="Running as unit", stderr="")
        with tempfile.TemporaryDirectory() as td, mock.patch.object(
            self.runner.shutil, "which", return_value="/usr/bin/systemd-run"
        ), mock.patch.object(
            self.runner.subprocess, "run", return_value=completed
        ) as run:
            log = str(Path(td) / "detach.log")
            result = self.runner.launch_detached(
                ["--detach", "--detach-log", log, "--task", "hello"],
                log,
            )

        self.assertEqual(result, 0)
        command = run.call_args.args[0]
        self.assertEqual(command[0], "/usr/bin/systemd-run")
        self.assertIn("--user", command)
        self.assertIn("--collect", command)
        self.assertIn("--property=Type=exec", command)
        self.assertIn(f"--property=StandardOutput=append:{log}", command)
        self.assertIn("--task", command)
        self.assertIn("hello", command)
        self.assertNotIn("--detach", command)
        self.assertNotIn("--detach-log", command)

    def test_detached_launch_requires_systemd_run(self):
        with mock.patch.object(self.runner.shutil, "which", return_value=None):
            result = self.runner.launch_detached(["--detach", "--task", "hello"])
        self.assertEqual(result, self.runner.CRASH_EXIT_CODE)

    def test_fallback_argv_switches_engine_and_removes_recursion(self):
        self.assertEqual(
            self.runner.fallback_worker_argv([
                "--task", "hello",
                "--engine", "codex",
                "--fallback-engine", "claude",
                "--timeout", "10",
            ], "claude"),
            ["--task", "hello", "--engine", "claude", "--timeout", "10"],
        )

    def test_provider_fallback_requires_unavailability_before_useful_work(self):
        empty_state = {"tool_calls": 0, "result_seen": False, "terminal_error": ""}
        self.assertIn(
            "provider unavailable",
            self.runner.provider_unavailable_reason(
                "codex", 1,
                "unexpected status 401 Unauthorized: Missing bearer or basic authentication",
                empty_state,
            ),
        )
        self.assertIsNone(self.runner.provider_unavailable_reason(
            "codex", 1, "ordinary task failure", empty_state,
        ))
        self.assertIsNone(self.runner.provider_unavailable_reason(
            "codex", 1, "401 Unauthorized",
            {**empty_state, "tool_calls": 1},
        ))
        self.assertIsNone(self.runner.provider_unavailable_reason(
            "codex", 1, "401 Unauthorized", empty_state, timed_out=True,
        ))

    def test_codex_stream_parser_captures_thread_result_and_usage(self):
        state = {
            "last_event_time": 0,
            "last_semantic_progress_time": 0,
            "last_activity": "",
            "session_id": None,
            "chunks_since_heartbeat": 0,
            "tool_calls": 0,
            "files_written": [],
            "last_agent_message": "",
            "result_seen": False,
            "result_seen_time": None,
            "result_usage": None,
            "output_tokens": 0,
        }
        self.runner.parse_stream_line(
            json.dumps({"type": "thread.started", "thread_id": "thread-1"}),
            state,
            engine="codex",
        )
        self.runner.parse_stream_line(
            json.dumps({
                "type": "item.completed",
                "item": {"type": "agent_message", "text": "done"},
            }),
            state,
            engine="codex",
        )
        self.runner.parse_stream_line(
            json.dumps({
                "type": "turn.completed",
                "usage": {"input_tokens": 10, "cached_input_tokens": 6, "output_tokens": 2},
            }),
            state,
            engine="codex",
        )
        self.assertEqual(state["session_id"], "thread-1")
        self.assertEqual(state["last_agent_message"], "done")
        self.assertTrue(state["result_seen"])
        self.assertEqual(state["result_usage"]["cache_read_input_tokens"], 6)

    def test_codex_final_text_falls_back_to_retained_json_line(self):
        state = {"last_agent_message": "", "terminal_error": ""}
        lines = [json.dumps({
            "type": "item.completed",
            "item": {"type": "agent_message", "text": "fallback result"},
        })]
        self.assertEqual(
            self.runner.extract_final_text("codex", lines, state, ""),
            "fallback result",
        )

    def test_codex_turn_failure_wins_over_stale_agent_message(self):
        state = {
            "last_event_time": 0,
            "last_semantic_progress_time": 0,
            "last_activity": "",
            "last_agent_message": "stale text",
            "terminal_error": "",
        }
        self.runner.parse_stream_line(
            json.dumps({
                "type": "turn.failed",
                "error": {"message": "provider failed"},
            }),
            state,
            engine="codex",
        )
        self.assertEqual(
            self.runner.extract_final_text("codex", [], state, "stderr"),
            "provider failed",
        )

    def test_resume_failure_detection_is_specific_for_both_engines(self):
        self.assertTrue(self.runner.resume_failure_detected(
            "codex", "thread", "thread/resume failed: thread not found"
        ))
        self.assertTrue(self.runner.resume_failure_detected(
            "codex", "thread", "no rollout found for thread id thread"
        ))
        self.assertFalse(self.runner.resume_failure_detected(
            "codex", "thread", "network connection failed"
        ))
        self.assertTrue(self.runner.resume_failure_detected(
            "claude", "session", "No conversation found for session"
        ))

    def test_wake_claim_is_released_after_failure(self):
        with tempfile.TemporaryDirectory() as td:
            state = Path(td) / "state.json"
            self.assertTrue(self.runner.claim_wake_dispatch(state, "/tmp/a", "wake-1"))
            self.assertFalse(self.runner.claim_wake_dispatch(state, "/tmp/a", "wake-1"))
            self.runner.release_wake_claim(state, "wake-1")
            self.assertTrue(self.runner.claim_wake_dispatch(state, "/tmp/a", "wake-1"))

    def test_codex_whatsapp_delivery_uses_engine_marker_and_async_wake(self):
        response = self.runner._SimpleResponse(
            200,
            '{"ok":true,"result":{"details":{"status":"accepted"}}}',
        )
        with mock.patch.object(
            self.runner, "detect_channel", return_value=("whatsapp", "123@g.us")
        ), mock.patch.object(
            self.runner, "send_channel", return_value=True
        ), mock.patch.object(
            self.runner, "http_post", return_value=response
        ) as post:
            delivered = self.runner.notify_session(
                "token",
                "agent:assistant:whatsapp:group:123@g.us",
                "123@g.us",
                "done",
                output_file_path="/tmp/result.txt",
                engine="codex",
            )
        self.assertTrue(delivered)
        payload = post.call_args.kwargs["json_body"]
        self.assertEqual(payload["args"]["timeoutSeconds"], 0)
        self.assertIn("[CODEX_RESULT]", payload["args"]["message"])
        self.assertIn("Codex result", payload["args"]["message"])


class RunnerProcessTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.bin_dir = self.root / "bin"
        self.bin_dir.mkdir()
        self.project = self.root / "project"
        self.project.mkdir()
        self.output = self.root / "output.txt"
        self.argv_file = self.root / "argv.json"
        fake = self.bin_dir / "claude"
        fake.write_text(textwrap.dedent("""\
            #!/usr/bin/env python3
            import json
            import os
            import sys
            import time

            if "--help" in sys.argv:
                print("fake claude help")
                raise SystemExit(0)

            argv_file = os.environ.get("FAKE_ARGV_FILE")
            if argv_file:
                with open(argv_file, "w") as f:
                    json.dump({
                        "argv": sys.argv[1:],
                        "api_key_present": "ANTHROPIC_API_KEY" in os.environ,
                    }, f)

            mode = os.environ.get("FAKE_CLAUDE_MODE", "success")
            if mode == "timeout":
                time.sleep(30)
            if mode == "resume_failure":
                print("No conversation found for session", file=sys.stderr)
                raise SystemExit(1)
            if mode == "stderr_flood":
                sys.stderr.write("x" * 2_000_000)
                sys.stderr.flush()
            if mode == "child_error":
                print("child failed", file=sys.stderr)
                raise SystemExit(7)

            print(json.dumps({
                "type": "system",
                "subtype": "init",
                "session_id": "11111111-1111-1111-1111-111111111111",
                "model": "fake-opus",
            }), flush=True)
            print(json.dumps({
                "type": "result",
                "result": "fake result",
                "usage": {"input_tokens": 10, "output_tokens": 2},
                "num_turns": 1,
            }), flush=True)
        """))
        fake.chmod(0o755)

        fake_codex = self.bin_dir / "codex"
        fake_codex.write_text(textwrap.dedent("""\
            #!/usr/bin/env python3
            import json
            import os
            import sys
            import time

            argv_file = os.environ.get("FAKE_ARGV_FILE")
            if argv_file:
                with open(argv_file, "w") as f:
                    json.dump({
                        "argv": sys.argv[1:],
                        "openai_key_present": "OPENAI_API_KEY" in os.environ,
                        "codex_key_present": "CODEX_API_KEY" in os.environ,
                        "access_token_present": "CODEX_ACCESS_TOKEN" in os.environ,
                    }, f)

            mode = os.environ.get("FAKE_CODEX_MODE", "success")
            if mode == "timeout":
                time.sleep(30)
            if mode == "resume_failure":
                print("Error: thread/resume failed: no rollout found for thread id test", file=sys.stderr)
                raise SystemExit(1)
            if mode == "stderr_flood":
                sys.stderr.write("x" * 2_000_000)
                sys.stderr.flush()
            if mode == "child_error":
                print("child failed", file=sys.stderr)
                raise SystemExit(9)
            if mode == "auth_failure":
                print(json.dumps({
                    "type": "thread.started",
                    "thread_id": "66666666-6666-6666-6666-666666666666",
                }), flush=True)
                print("unexpected status 401 Unauthorized: Missing bearer or basic authentication", file=sys.stderr)
                raise SystemExit(1)

            print(json.dumps({
                "type": "thread.started",
                "thread_id": "33333333-3333-3333-3333-333333333333",
            }), flush=True)
            print(json.dumps({
                "type": "item.started",
                "item": {"id": "item-1", "type": "command_execution", "command": "true"},
            }), flush=True)
            print(json.dumps({
                "type": "item.completed",
                "item": {"id": "item-2", "type": "agent_message", "text": "fake codex result"},
            }), flush=True)
            print(json.dumps({
                "type": "turn.completed",
                "usage": {"input_tokens": 20, "cached_input_tokens": 10, "output_tokens": 3},
            }), flush=True)
        """))
        fake_codex.chmod(0o755)

    def tearDown(self):
        self.tmp.cleanup()

    def run_runner(self, mode="success", extra_args=None, timeout=15, engine="claude"):
        env = os.environ.copy()
        env.update({
            "PATH": f"{self.bin_dir}:{env.get('PATH', '')}",
            "HOME": str(self.root),
            "FAKE_CLAUDE_MODE": mode,
            "FAKE_CODEX_MODE": mode,
            "FAKE_ARGV_FILE": str(self.argv_file),
            "CC_POLL_INTERVAL": "0.05",
            "ANTHROPIC_API_KEY": "must-not-leak",
            "OPENAI_API_KEY": "must-not-leak",
            "CODEX_API_KEY": "must-not-leak",
            "CODEX_ACCESS_TOKEN": "must-not-leak",
        })
        cmd = [
            sys.executable,
            str(RUNNER),
            "--task", "test task",
            "--project", str(self.project),
            "--output", str(self.output),
            "--timeout", "5",
        ]
        if engine is not None:
            cmd.extend(["--engine", engine])
        if extra_args:
            cmd.extend(extra_args)
        return subprocess.run(cmd, env=env, capture_output=True, text=True, timeout=timeout)

    def test_success_propagates_zero_and_does_not_create_git_repo(self):
        result = self.run_runner()
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_text(), "fake result")
        self.assertFalse((self.project / ".git").exists())
        invocation = json.loads(self.argv_file.read_text())
        self.assertFalse(invocation["api_key_present"])
        self.assertEqual(invocation["argv"][0:2], ["-p", "--model"])
        self.assertEqual(invocation["argv"][2], "sonnet")

    def test_child_failure_code_is_preserved(self):
        result = self.run_runner("child_error")
        self.assertEqual(result.returncode, 7, result.stderr)

    def test_resume_failure_returns_documented_code(self):
        result = self.run_runner(
            "resume_failure",
            ["--resume", "22222222-2222-2222-2222-222222222222"],
        )
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertTrue(self.output.read_text().startswith("RESUME_FAILED"))

    def test_timeout_returns_124(self):
        result = self.run_runner("timeout", ["--timeout", "1"])
        self.assertEqual(result.returncode, 124, result.stderr)

    def test_stderr_is_drained_while_child_runs(self):
        result = self.run_runner("stderr_flood")
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(self.output.read_text(), "fake result")

    def test_fast_fallback_does_not_pin_old_model(self):
        result = self.run_runner(extra_args=["--fast"])
        self.assertEqual(result.returncode, 0, result.stderr)
        invocation = json.loads(self.argv_file.read_text())
        argv = invocation["argv"]
        self.assertIn("--settings", argv)
        self.assertIn('{"fastMode":true}', argv)
        self.assertNotIn("claude-opus-4-6", argv)

    def test_codex_success_uses_json_subscription_command_and_registers_thread(self):
        result = self.run_runner(engine="codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_text(), "fake codex result")
        self.assertFalse((self.project / ".git").exists())
        invocation = json.loads(self.argv_file.read_text())
        self.assertFalse(invocation["openai_key_present"])
        self.assertFalse(invocation["codex_key_present"])
        self.assertFalse(invocation["access_token_present"])
        self.assertEqual(invocation["argv"][:2], ["exec", "--json"])
        self.assertIn("--skip-git-repo-check", invocation["argv"])
        model_index = invocation["argv"].index("--model")
        self.assertEqual(invocation["argv"][model_index + 1], "gpt-5.6-terra")
        registry = json.loads((self.root / ".openclaw/claude_sessions.json").read_text())
        entry = registry["sessions"]["33333333-3333-3333-3333-333333333333"]
        self.assertEqual(entry["engine"], "codex")

    def test_no_engine_flag_defaults_to_codex_only(self):
        result = self.run_runner(engine=None)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_text(), "fake codex result")
        invocation = json.loads(self.argv_file.read_text())
        self.assertEqual(invocation["argv"][:2], ["exec", "--json"])
        model_index = invocation["argv"].index("--model")
        self.assertEqual(invocation["argv"][model_index + 1], "gpt-5.6-terra")

    def test_no_engine_flag_does_not_fall_back_to_claude(self):
        result = self.run_runner("auth_failure", engine=None)
        self.assertEqual(result.returncode, 1, result.stderr)
        invocation = json.loads(self.argv_file.read_text())
        self.assertEqual(invocation["argv"][:2], ["exec", "--json"])
        self.assertNotIn("switching to Claude Code", result.stderr)

    def test_codex_resume_uses_thread_id_and_compact_prompt(self):
        thread_id = "44444444-4444-4444-4444-444444444444"
        result = self.run_runner(
            engine="codex",
            extra_args=["--resume", thread_id],
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(self.argv_file.read_text())["argv"]
        self.assertEqual(argv[:3], ["exec", "resume", "--json"])
        self.assertIn(thread_id, argv)
        self.assertIn("--skip-git-repo-check", argv)
        self.assertIn("Continue the existing Codex session", argv[-1])

    def test_codex_resume_failure_returns_documented_code(self):
        result = self.run_runner(
            "resume_failure",
            ["--resume", "55555555-5555-5555-5555-555555555555"],
            engine="codex",
        )
        self.assertEqual(result.returncode, 42, result.stderr)
        self.assertIn("Codex did not start", self.output.read_text())

    def test_resume_rejects_known_session_from_other_engine(self):
        registry = self.root / ".openclaw/claude_sessions.json"
        registry.parent.mkdir(parents=True)
        registry.write_text(json.dumps({
            "sessions": {
                "claude-session": {
                    "session_id": "claude-session",
                    "engine": "claude",
                    "last_accessed": "2026-07-19T00:00:00",
                }
            }
        }))
        result = self.run_runner(
            engine="codex",
            extra_args=["--resume", "claude-session"],
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertIn("registered for engine claude, not codex", result.stderr)

    def test_codex_fast_uses_official_fast_mode_config(self):
        result = self.run_runner(extra_args=["--fast"], engine="codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(self.argv_file.read_text())["argv"]
        self.assertIn("features.fast_mode=true", argv)
        self.assertIn('service_tier="fast"', argv)

    def test_codex_model_is_passed_without_claude_default(self):
        result = self.run_runner(extra_args=["--model", "gpt-test"], engine="codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(self.argv_file.read_text())["argv"]
        self.assertIn("--model", argv)
        self.assertIn("gpt-test", argv)
        self.assertNotIn("opus", argv)

    def test_codex_sol_mode_pins_sol(self):
        result = self.run_runner(extra_args=["--mode", "sol"], engine="codex")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(self.argv_file.read_text())["argv"]
        model_index = argv.index("--model")
        self.assertEqual(argv[model_index + 1], "gpt-5.6-sol")
        self.assertIn("Mode: sol", result.stderr)

    def test_claude_fable_mode_pins_fable(self):
        result = self.run_runner(extra_args=["--mode", "fable"], engine="claude")
        self.assertEqual(result.returncode, 0, result.stderr)
        argv = json.loads(self.argv_file.read_text())["argv"]
        self.assertEqual(argv[0:3], ["-p", "--model", "fable"])
        self.assertIn("Mode: fable", result.stderr)

    def test_provider_specific_modes_reject_wrong_engine(self):
        sol = self.run_runner(extra_args=["--mode", "sol"], engine="claude")
        self.assertEqual(sol.returncode, 2, sol.stderr)
        self.assertIn("--mode sol requires --engine codex", sol.stderr)

        fable = self.run_runner(extra_args=["--mode", "fable"], engine="codex")
        self.assertEqual(fable.returncode, 2, fable.stderr)
        self.assertIn("--mode fable requires --engine claude", fable.stderr)

    def test_mode_rejects_model_fast_and_fallback(self):
        for extra_args in (
            ["--mode", "sol", "--model", "gpt-test"],
            ["--mode", "sol", "--fast"],
            ["--mode", "sol", "--fallback-engine", "claude"],
        ):
            with self.subTest(extra_args=extra_args):
                result = self.run_runner(extra_args=extra_args, engine="codex")
                self.assertEqual(result.returncode, 2, result.stderr)

    def test_codex_child_failure_code_is_preserved(self):
        result = self.run_runner("child_error", engine="codex")
        self.assertEqual(result.returncode, 9, result.stderr)

    def test_codex_provider_failure_falls_back_once_to_claude(self):
        result = self.run_runner(
            "auth_failure",
            extra_args=["--fallback-engine", "claude"],
            engine="codex",
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.output.read_text(), "fake result")
        invocation = json.loads(self.argv_file.read_text())
        self.assertEqual(invocation["argv"][0:2], ["-p", "--model"])
        self.assertIn("switching to Claude Code", result.stderr)

    def test_codex_task_failure_does_not_fallback(self):
        result = self.run_runner(
            "child_error",
            extra_args=["--fallback-engine", "claude"],
            engine="codex",
        )
        self.assertEqual(result.returncode, 9, result.stderr)

    def test_codex_timeout_returns_124(self):
        result = self.run_runner("timeout", ["--timeout", "1"], engine="codex")
        self.assertEqual(result.returncode, 124, result.stderr)

    def test_codex_stderr_is_drained_while_child_runs(self):
        result = self.run_runner("stderr_flood", engine="codex")
        self.assertEqual(result.returncode, 0, result.stderr[-2000:])
        self.assertEqual(self.output.read_text(), "fake codex result")


if __name__ == "__main__":
    unittest.main()
