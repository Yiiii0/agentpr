"""Tests for orchestrator.agent_tools — safety checks and tool dispatch."""

from __future__ import annotations

import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator.agent_tools import AgentToolkit, get_tool_schemas
from orchestrator.models import RunState, StepName


def _make_toolkit(**overrides) -> AgentToolkit:
    defaults = dict(
        service=MagicMock(),
        workspace_root=Path("/tmp/test_workspaces"),
        integration_root=Path("/tmp/test_integration"),
        prompt_file=Path("/tmp/test_prompt.txt"),
        skills_mode="agentpr",
        codex_sandbox=None,
        telegram_sender=None,
    )
    defaults.update(overrides)
    return AgentToolkit(**defaults)


class TestToolDispatch(unittest.TestCase):
    def test_unknown_tool_returns_error(self):
        tk = _make_toolkit()
        result = tk.execute("nonexistent_tool", {})
        self.assertIn("ERROR", result)
        self.assertIn("Unknown tool", result)

    def test_invalid_args_returns_error(self):
        tk = _make_toolkit()
        # update_state requires run_id and to_state
        result = tk.execute("update_state", {"bad_arg": "value"})
        self.assertIn("ERROR", result)

    def test_tool_exception_returns_error(self):
        tk = _make_toolkit()
        tk.service.get_run_snapshot.side_effect = RuntimeError("DB down")
        result = tk.execute("read_evidence", {"run_id": "test123"})
        self.assertIn("ERROR", result)


class TestUpdateState(unittest.TestCase):
    def test_invalid_target_state(self):
        tk = _make_toolkit()
        result = tk.update_state(run_id="r1", to_state="INVALID_STATE", reason="test")
        self.assertIn("ERROR", result)
        self.assertIn("Invalid state", result)

    def test_run_not_found(self):
        tk = _make_toolkit()
        tk.service.get_run_snapshot.side_effect = KeyError("not found")
        result = tk.update_state(run_id="r1", to_state="EXECUTING", reason="test")
        self.assertIn("ERROR", result)
        self.assertIn("not found", result)

    def test_same_state_noop(self):
        tk = _make_toolkit()
        tk.service.get_run_snapshot.return_value = {"state": "EXECUTING", "run": {"current_state": "EXECUTING"}}
        result = tk.update_state(run_id="r1", to_state="EXECUTING", reason="already here")
        self.assertIn("OK", result)
        self.assertIn("already in", result)

    def test_invalid_transition(self):
        tk = _make_toolkit()
        tk.service.get_run_snapshot.return_value = {"state": "QUEUED", "run": {"current_state": "QUEUED"}}
        result = tk.update_state(run_id="r1", to_state="DONE", reason="skip")
        self.assertIn("ERROR", result)
        self.assertIn("Cannot transition", result)

    def test_valid_transition_executing(self):
        tk = _make_toolkit()
        tk.service.get_run_snapshot.return_value = {"state": "QUEUED", "run": {"current_state": "QUEUED"}}
        result = tk.update_state(run_id="r1", to_state="EXECUTING", reason="start work")
        self.assertIn("OK", result)
        self.assertIn("QUEUED → EXECUTING", result)


class TestExecuteWorkerSafety(unittest.TestCase):
    def test_max_retries_exceeded(self):
        tk = _make_toolkit()
        tk.service.get_run_snapshot.return_value = {"state": "EXECUTING", "run": {"current_state": "EXECUTING"}}
        tk.service.count_step_attempts.return_value = 3  # == _MAX_RETRIES
        result = tk.execute_worker(run_id="r1", task="integration")
        self.assertIn("ERROR", result)
        self.assertIn("Max retries", result)

    def test_invalid_state_for_execution(self):
        tk = _make_toolkit()
        tk.service.get_run_snapshot.return_value = {"state": "QUEUED", "run": {"current_state": "QUEUED"}}
        tk.service.count_step_attempts.return_value = 0
        result = tk.execute_worker(run_id="r1", task="integration")
        self.assertIn("ERROR", result)
        self.assertIn("Cannot execute worker", result)

    def test_no_prompt_file(self):
        tk = _make_toolkit(prompt_file=None)
        tk.service.get_run_snapshot.return_value = {"state": "EXECUTING", "run": {"current_state": "EXECUTING"}}
        tk.service.count_step_attempts.return_value = 0
        result = tk.execute_worker(run_id="r1", task="integration")
        self.assertIn("ERROR", result)
        self.assertIn("No prompt_file", result)


class TestCreatePrGate(unittest.TestCase):
    def test_no_code_review_blocks_pr(self):
        tk = _make_toolkit()
        tk.service.latest_artifact.return_value = None  # no code review
        result = tk.github_api(run_id="r1", action="create_pr")
        self.assertIn("ERROR", result)
        self.assertIn("No code review", result)
        self.assertIn("MUST call review_code", result)

    def test_non_clean_review_blocks_pr(self):
        tk = _make_toolkit()
        tk.service.latest_artifact.return_value = {
            "metadata": {"verdict": "HAS_ISSUES"}
        }
        result = tk.github_api(run_id="r1", action="create_pr")
        self.assertIn("ERROR", result)
        self.assertIn("HAS_ISSUES", result)

    def test_state_transition_error_returns_error(self):
        """Fix 3A: state transition failure in _create_pr should return error, not silently pass."""
        tk = _make_toolkit()
        tk.service.latest_artifact.return_value = {
            "metadata": {"verdict": "CLEAN"}
        }
        tk.service.get_run_snapshot.side_effect = RuntimeError("DB locked")
        result = tk.github_api(run_id="r1", action="create_pr")
        self.assertIn("ERROR", result)
        self.assertIn("Cannot prepare state", result)


class TestUpdateSkillSafety(unittest.TestCase):
    def test_disallowed_file(self):
        tk = _make_toolkit()
        result = tk.update_skill(file="SKILL.md", action="append_rule", content="test", reason="test")
        self.assertIn("ERROR", result)
        self.assertIn("Cannot modify", result)

    def test_invalid_action(self):
        tk = _make_toolkit()
        result = tk.update_skill(file="forge_rules.md", action="delete_rule", content="test", reason="test")
        self.assertIn("ERROR", result)
        self.assertIn("Invalid action", result)


class TestNotifyHuman(unittest.TestCase):
    def test_no_telegram_logs_only(self):
        tk = _make_toolkit(telegram_sender=None)
        result = tk.notify_human(message="test notification")
        self.assertIn("OK", result)
        self.assertIn("no Telegram configured", result)

    def test_telegram_success(self):
        sender = MagicMock()
        tk = _make_toolkit(telegram_sender=sender)
        result = tk.notify_human(message="test notification", priority="high")
        sender.assert_called_once()
        self.assertIn("OK", result)
        self.assertIn("Telegram", result)


class TestToolSchemas(unittest.TestCase):
    def test_all_10_tools_have_schemas(self):
        schemas = get_tool_schemas()
        self.assertEqual(len(schemas), 10)
        names = {s["function"]["name"] for s in schemas}
        expected = {
            "query_runs", "update_state", "read_evidence", "execute_worker",
            "github_api", "review_code", "generate_pr_body",
            "notify_human", "reply_human", "update_skill",
        }
        self.assertEqual(names, expected)


if __name__ == "__main__":
    unittest.main()
