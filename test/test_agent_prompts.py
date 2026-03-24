"""Tests for orchestrator.agent_prompts — prompt/context builders."""

from __future__ import annotations

import unittest

from orchestrator.agent_prompts import (
    MANAGER_SYSTEM_PROMPT,
    build_single_run_context,
    build_tick_context,
)


class TestManagerSystemPrompt(unittest.TestCase):
    def test_prompt_not_empty(self):
        self.assertTrue(len(MANAGER_SYSTEM_PROMPT) > 100)

    def test_prompt_mentions_critical_rules(self):
        self.assertIn("CRITICAL RULES", MANAGER_SYSTEM_PROMPT)
        self.assertIn("review_code", MANAGER_SYSTEM_PROMPT)

    def test_prompt_has_workflow(self):
        self.assertIn("QUEUED", MANAGER_SYSTEM_PROMPT)
        self.assertIn("PUSHED", MANAGER_SYSTEM_PROMPT)
        self.assertIn("ITERATING", MANAGER_SYSTEM_PROMPT)


class TestBuildTickContext(unittest.TestCase):
    def test_basic_output(self):
        ctx = build_tick_context(
            active_runs_summary="- r1 | owner/repo | PUSHED",
        )
        self.assertIn("Active Runs", ctx)
        self.assertIn("r1", ctx)

    def test_with_all_fields(self):
        ctx = build_tick_context(
            active_runs_summary="- r1 | o/r | PUSHED",
            pending_events="webhook: CI failed",
            global_stats="Total: 5, Active: 2",
            recent_decisions="r1: decided to review",
        )
        self.assertIn("Pending Events", ctx)
        self.assertIn("Global Stats", ctx)
        self.assertIn("Recent Decision", ctx)

    def test_priority_instruction(self):
        ctx = build_tick_context(active_runs_summary="test")
        self.assertIn("PUSHED > ITERATING", ctx)


class TestBuildSingleRunContext(unittest.TestCase):
    def test_basic_output(self):
        ctx = build_single_run_context(
            run_summary="r1 | PUSHED | owner/repo",
        )
        self.assertIn("Run State", ctx)
        self.assertIn("r1", ctx)

    def test_with_event(self):
        ctx = build_single_run_context(
            run_summary="r1 | PUSHED",
            event_description="CI failed for PR #42",
        )
        self.assertIn("Event", ctx)
        self.assertIn("CI failed", ctx)


if __name__ == "__main__":
    unittest.main()
