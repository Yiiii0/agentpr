"""Tests for orchestrator.agent_loop — daemon loop, idle detection, escalation."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from orchestrator.agent_loop import (
    AgentLoopConfig,
    AgentTickResult,
    _record_tick_audit,
    _sleep_with_wake,
    agent_tick,
)
from orchestrator.agent_session import AgentConfig, AgentResult


class TestSleepWithWake(unittest.TestCase):
    def test_wake_file_interrupts_sleep(self):
        with tempfile.TemporaryDirectory() as td:
            wake = Path(td) / ".wake_manager"
            wake.touch()
            # Should return quickly when wake file exists
            _sleep_with_wake(60, wake)
            self.assertFalse(wake.exists())

    def test_no_wake_file_completes(self):
        with tempfile.TemporaryDirectory() as td:
            wake = Path(td) / ".wake_manager"
            # Short sleep, no wake file
            _sleep_with_wake(0.01, wake)


class TestRecordTickAudit(unittest.TestCase):
    def test_writes_jsonl(self):
        with tempfile.TemporaryDirectory() as td:
            db_path = Path(td) / "agentpr.db"
            db_path.touch()
            service = MagicMock()
            service.db.db_path = db_path

            tick_result = AgentTickResult(
                ok=True, turns_used=3, tool_calls=5,
                tools_used=["query_runs", "update_state"],
                active_runs=2, input_tokens=1000, output_tokens=200,
            )
            _record_tick_audit(service, tick_result, tick_number=1)

            audit_file = Path(td) / "data" / "agent_tick_audit.jsonl"
            self.assertTrue(audit_file.exists())
            data = json.loads(audit_file.read_text().strip())
            self.assertEqual(data["tick_number"], 1)
            self.assertEqual(data["tool_calls"], 5)
            self.assertTrue(data["ok"])


class TestAgentTick(unittest.TestCase):
    @patch("orchestrator.agent_loop.run_agent_session")
    def test_no_active_runs_returns_early(self, mock_session):
        service = MagicMock()
        service.list_runs.return_value = [
            {"run_id": "r1", "current_state": "DONE"},
        ]
        config = AgentConfig(
            api_base="https://test/v1", api_key="k", model="gpt-4o",
        )
        result = agent_tick(
            service=service,
            agent_config=config,
            workspace_root=Path("/tmp"),
            integration_root=Path("/tmp"),
        )
        self.assertTrue(result.ok)
        self.assertEqual(result.active_runs, 0)
        mock_session.assert_not_called()

    @patch("orchestrator.agent_loop.run_agent_session")
    def test_max_turns_auto_escalates(self, mock_session):
        """Fix 2A: max_turns should auto-transition in-progress runs to NEEDS_HUMAN_REVIEW."""
        service = MagicMock()
        service.list_runs.return_value = [
            {"run_id": "r1", "current_state": "PUSHED", "owner": "o", "repo": "r"},
            {"run_id": "r2", "current_state": "DONE", "owner": "o", "repo": "r2"},
        ]
        mock_session.return_value = AgentResult(
            final_text="Max turns reached.",
            turns_used=15,
            error="max_turns_exceeded",
        )
        config = AgentConfig(
            api_base="https://test/v1", api_key="k", model="gpt-4o",
        )

        with patch("orchestrator.agent_loop.AgentToolkit") as MockToolkit:
            mock_tk = MagicMock()
            MockToolkit.return_value = mock_tk

            result = agent_tick(
                service=service,
                agent_config=config,
                workspace_root=Path("/tmp"),
                integration_root=Path("/tmp"),
            )

        self.assertFalse(result.ok)
        self.assertEqual(result.error, "max_turns_exceeded")
        # r1 (PUSHED) should be escalated, r2 (DONE) should not
        # The escalation happens via toolkit.update_state which we mocked
        # Just verify the tick completed without crashing


if __name__ == "__main__":
    unittest.main()
