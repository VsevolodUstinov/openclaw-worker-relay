from pathlib import Path
import unittest


SKILL_DIR = Path(__file__).resolve().parents[1]


class InstructionContractTests(unittest.TestCase):
    def test_provider_neutral_identity_and_boundaries(self):
        skill = (SKILL_DIR / "SKILL.md").read_text()
        self.assertIn("name: worker-relay", skill)
        for phrase in (
            "external-agent execution harness",
            "OpenClaw is the **supervisor**",
            "Claude Code/Codex are **external workers**",
            "native OpenClaw subagent",
            "raw interactive CLI invocation",
            "historical name `claude-code-task`",
            "Any explicit request to call, run, invoke, use, ask, or delegate",
            "even when the underlying task is small",
            "Direct CLI is allowed only when the user explicitly asks to bypass Worker Relay",
            "do not solve the delegated task inline as a substitute or supplement",
            "stop after routing validation and do not state or infer the answer",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, skill)

    def test_first_read_preserves_live_e2e_guardrails(self):
        skill = (SKILL_DIR / "SKILL.md").read_text()
        first_read = skill.split("## Mandatory preflight", 1)[0]

        required_phrases = (
            "Non-negotiable operating rules",
            "Launch only `python3 {baseDir}/run-task.py --detach`",
            "Do not handwrite `nohup`, `systemd-run`",
            "Never edit the canonical shared-skill checkout during an E2E",
            "Run provider/mode probes sequentially",
            "an observation, not a durable feature gap",
            "run a standard control",
            "Correlate asynchronous messages",
            "references/testing-protocol.md",
            "One detached worker phase occupies one supervisor turn",
            "Polling after detach is an E2E failure",
            "sole optional exception",
            "A completion-wake turn is the next working supervisor turn",
            "no future trigger exists",
            "a failed E2E attempt",
            "systemd-run --collect",
            "if `LoadState=not-found`, those fields are not exit evidence",
            "never override it with post-collection defaults",
        )
        for phrase in required_phrases:
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, first_read)

    def test_default_examples_do_not_enable_fallback(self):
        skill = (SKILL_DIR / "SKILL.md").read_text()
        preflight_example = skill.split("```bash", 1)[1].split("```", 1)[0]
        launch_example = skill.split("## Launch", 1)[1].split("```bash", 1)[1].split("```", 1)[0]

        self.assertNotIn("--fallback-engine", preflight_example)
        self.assertNotIn("--fallback-engine", launch_example)
        self.assertIn("Fallback is `none` unless", skill)

    def test_python_entrypoint_does_not_require_executable_probe(self):
        skill = (SKILL_DIR / "SKILL.md").read_text()
        self.assertIn("invoked as `python3 {baseDir}/run-task.py`", skill)
        self.assertIn("Do not probe it with `test -x`", skill)

    def test_testing_protocol_requires_sequential_confirmation(self):
        protocol = (SKILL_DIR / "references/testing-protocol.md").read_text()
        for phrase in (
            "Required order for a full Codex E2E",
            "never in parallel",
            "Live-failure confirmation matrix",
            "Retry the failed case once, sequentially",
            "do not list a current feature gap",
            "Continue only from that worker's routed completion wake",
            "syntactically valid and absent",
            "a malformed non-UUID string does not satisfy this gate",
            "A single terminal `sessions_yield` is allowed",
            "launch exactly that next phase in the same wake turn",
            "status-only promise to continue later",
            "the workflow has stalled",
            "post-completion `systemctl show` defaults",
            "A collected unit can misleadingly display",
            "wake `exit=42` or systemd journal `status=42`",
        ):
            with self.subTest(phrase=phrase):
                self.assertIn(phrase, protocol)


if __name__ == "__main__":
    unittest.main()
