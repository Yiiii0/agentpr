"""Tests for orchestrator.agent_session — core agent loop."""

from __future__ import annotations

import json
import os
import unittest
from unittest.mock import MagicMock, patch

from orchestrator.agent_session import (
    AgentConfig,
    AgentResult,
    create_config_from_env,
    is_reasoning_model,
    run_agent_session,
)


class TestIsReasoningModel(unittest.TestCase):
    def test_o1_detected(self):
        self.assertTrue(is_reasoning_model("tensorblock/o1"))
        self.assertTrue(is_reasoning_model("o1-preview"))
        self.assertTrue(is_reasoning_model("tensorblock/o1-mini"))

    def test_o3_detected(self):
        self.assertTrue(is_reasoning_model("tensorblock/o3"))
        self.assertTrue(is_reasoning_model("o3-mini"))

    def test_gpt4o_not_reasoning(self):
        self.assertFalse(is_reasoning_model("tensorblock/gpt-4o"))
        self.assertFalse(is_reasoning_model("gpt-4o-mini"))

    def test_claude_not_reasoning(self):
        self.assertFalse(is_reasoning_model("tensorblock/claude-sonnet-4"))

    def test_case_insensitive(self):
        self.assertTrue(is_reasoning_model("TensorBlock/O1"))


class TestCreateConfigFromEnv(unittest.TestCase):
    @patch.dict(os.environ, {
        "AGENTPR_MANAGER_API_KEY": "test-key",
        "AGENTPR_MANAGER_API_BASE": "https://forge.test/v1",
    }, clear=False)
    def test_defaults_to_gpt4o(self):
        # Remove model env vars if set
        for k in ("AGENTPR_AGENT_MODEL", "AGENTPR_MANAGER_MODEL"):
            os.environ.pop(k, None)
        cfg = create_config_from_env()
        self.assertEqual(cfg.model, "tensorblock/gpt-4o")

    @patch.dict(os.environ, {
        "AGENTPR_MANAGER_API_KEY": "test-key",
        "AGENTPR_AGENT_MODEL": "tensorblock/claude-sonnet-4",
    }, clear=False)
    def test_agent_model_takes_precedence(self):
        os.environ.pop("AGENTPR_MANAGER_MODEL", None)
        cfg = create_config_from_env()
        self.assertEqual(cfg.model, "tensorblock/claude-sonnet-4")

    @patch.dict(os.environ, {
        "AGENTPR_MANAGER_API_KEY": "test-key",
        "AGENTPR_MANAGER_MODEL": "tensorblock/o1",
    }, clear=False)
    def test_o1_fallback_to_gpt4o(self):
        os.environ.pop("AGENTPR_AGENT_MODEL", None)
        cfg = create_config_from_env()
        self.assertEqual(cfg.model, "tensorblock/gpt-4o")

    @patch.dict(os.environ, {}, clear=True)
    def test_missing_api_key_raises(self):
        with self.assertRaises(ValueError):
            create_config_from_env()


class TestRunAgentSession(unittest.TestCase):
    def _make_config(self, model: str = "tensorblock/gpt-4o") -> AgentConfig:
        return AgentConfig(
            api_base="https://forge.test/v1",
            api_key="test-key",
            model=model,
            max_turns=5,
        )

    def _mock_response(self, content: str = "Done.", tool_calls=None, usage=None):
        msg = {"content": content}
        if tool_calls:
            msg["tool_calls"] = tool_calls
        resp = {"choices": [{"message": msg}]}
        if usage:
            resp["usage"] = usage
        return resp

    @patch("orchestrator.agent_session._request_chat_completion")
    def test_simple_text_response(self, mock_req):
        mock_req.return_value = self._mock_response("All done.", usage={"prompt_tokens": 100, "completion_tokens": 20})
        result = run_agent_session(
            config=self._make_config(),
            system_prompt="You are a test agent.",
            context="Do something.",
            tools=[],
            tool_executor=lambda n, a: "ok",
        )
        self.assertEqual(result.final_text, "All done.")
        self.assertEqual(result.turns_used, 1)
        self.assertEqual(result.total_input_tokens, 100)
        self.assertEqual(result.error, None)

    @patch("orchestrator.agent_session._request_chat_completion")
    def test_tool_call_then_text(self, mock_req):
        tool_call = {
            "id": "tc1",
            "function": {"name": "query_runs", "arguments": "{}"},
        }
        mock_req.side_effect = [
            self._mock_response(tool_calls=[tool_call]),
            self._mock_response("Processed."),
        ]
        executor = MagicMock(return_value="Found 3 runs.")
        result = run_agent_session(
            config=self._make_config(),
            system_prompt="test",
            context="test",
            tools=[{"type": "function", "function": {"name": "query_runs"}}],
            tool_executor=executor,
        )
        executor.assert_called_once_with("query_runs", {})
        self.assertEqual(result.final_text, "Processed.")
        self.assertEqual(result.turns_used, 2)
        self.assertEqual(len(result.tool_calls_made), 1)

    @patch("orchestrator.agent_session._request_chat_completion")
    def test_max_turns_exceeded(self, mock_req):
        # Always return tool calls so we never stop
        tool_call = {
            "id": "tc1",
            "function": {"name": "query_runs", "arguments": "{}"},
        }
        mock_req.return_value = self._mock_response(tool_calls=[tool_call])
        result = run_agent_session(
            config=self._make_config(),
            system_prompt="test",
            context="test",
            tools=[{"type": "function", "function": {"name": "query_runs"}}],
            tool_executor=lambda n, a: "ok",
        )
        self.assertEqual(result.error, "max_turns_exceeded")
        self.assertIn("Max turns", result.final_text)
        self.assertEqual(result.turns_used, 5)

    @patch("orchestrator.agent_session._request_chat_completion")
    def test_llm_error_returns_error_result(self, mock_req):
        mock_req.side_effect = RuntimeError("HTTP 429: rate limited")
        result = run_agent_session(
            config=self._make_config(),
            system_prompt="test",
            context="test",
            tools=[],
            tool_executor=lambda n, a: "ok",
        )
        self.assertIn("rate limited", result.final_text)
        self.assertIsNotNone(result.error)

    @patch("orchestrator.agent_session._request_chat_completion")
    def test_empty_choices_returns_error(self, mock_req):
        mock_req.return_value = {"choices": []}
        result = run_agent_session(
            config=self._make_config(),
            system_prompt="test",
            context="test",
            tools=[],
            tool_executor=lambda n, a: "ok",
        )
        self.assertIn("no choices", result.final_text.lower())
        self.assertIsNotNone(result.error)

    @patch("orchestrator.agent_session._request_chat_completion")
    def test_malformed_tool_args_default_to_empty(self, mock_req):
        tool_call = {
            "id": "tc1",
            "function": {"name": "query_runs", "arguments": "INVALID JSON"},
        }
        mock_req.side_effect = [
            self._mock_response(tool_calls=[tool_call]),
            self._mock_response("Done."),
        ]
        executor = MagicMock(return_value="ok")
        run_agent_session(
            config=self._make_config(),
            system_prompt="test",
            context="test",
            tools=[{"type": "function", "function": {"name": "query_runs"}}],
            tool_executor=executor,
        )
        executor.assert_called_once_with("query_runs", {})

    @patch("orchestrator.agent_session._request_chat_completion")
    def test_tool_executor_exception_returns_error_string(self, mock_req):
        tool_call = {
            "id": "tc1",
            "function": {"name": "query_runs", "arguments": "{}"},
        }
        mock_req.side_effect = [
            self._mock_response(tool_calls=[tool_call]),
            self._mock_response("Handled error."),
        ]
        executor = MagicMock(side_effect=ValueError("DB locked"))
        result = run_agent_session(
            config=self._make_config(),
            system_prompt="test",
            context="test",
            tools=[{"type": "function", "function": {"name": "query_runs"}}],
            tool_executor=executor,
        )
        # The error should be sent back to the LLM as a tool result, not crash
        self.assertEqual(result.final_text, "Handled error.")

    @patch("orchestrator.agent_session._request_chat_completion")
    def test_o1_model_uses_developer_role(self, mock_req):
        mock_req.return_value = self._mock_response("Done.")
        run_agent_session(
            config=self._make_config(model="tensorblock/o1"),
            system_prompt="test",
            context="test",
            tools=[],
            tool_executor=lambda n, a: "ok",
        )
        call_args = mock_req.call_args[0]
        payload = call_args[1]
        self.assertEqual(payload["messages"][0]["role"], "developer")
        self.assertNotIn("temperature", payload)


if __name__ == "__main__":
    unittest.main()
