"""Agent-based manager loop — replaces ManagerLoopRunner with agent sessions.

Each tick builds context from DB state, runs a bounded agent session,
and lets the agent process all active runs via tool calls.

Event-driven via wake file (.wake_manager), same as the existing manager loop.
"""

from __future__ import annotations

import json
import logging
import os
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from .agent_prompts import MANAGER_SYSTEM_PROMPT, build_tick_context
from .agent_session import AgentConfig, AgentResult, create_config_from_env, run_agent_session
from .agent_tools import AgentToolkit, get_tool_schemas, _format_run_summary
from .service import OrchestratorService

logger = logging.getLogger("agentpr.agent_loop")

# Max consecutive idle ticks before exiting (non-persistent mode)
_IDLE_EXIT_TICKS = 3

# Max hibernation sleep when idle (persistent mode)
_MAX_HIBERNATE_SEC = 600


@dataclass
class AgentLoopConfig:
    """Configuration for the agent manager loop."""

    interval_sec: int = 180
    max_loops: int | None = None
    persistent: bool = False
    max_turns_per_tick: int = 15


@dataclass
class AgentTickResult:
    """Result of a single agent tick."""

    ok: bool
    turns_used: int = 0
    tool_calls: int = 0
    tools_used: list[str] = field(default_factory=list)
    active_runs: int = 0
    input_tokens: int = 0
    output_tokens: int = 0
    error: str | None = None


def run_agent_loop(
    *,
    service: OrchestratorService,
    agent_config: AgentConfig,
    loop_config: AgentLoopConfig,
    workspace_root: Path,
    integration_root: Path,
    prompt_file: Path | None = None,
    skills_mode: str = "agentpr",
    codex_sandbox: str | None = None,
    telegram_sender: Callable[[str], None] | None = None,
) -> int:
    """Run the agent manager loop. Returns exit code.

    This is the main entry point, equivalent to the existing run_manager_loop().
    """
    wake_path = service.db.db_path.parent / ".wake_manager"
    consecutive_idle = 0
    loops_run = 0

    logger.info(
        "Agent loop starting (interval=%ds, persistent=%s, max_turns=%d)",
        loop_config.interval_sec,
        loop_config.persistent,
        loop_config.max_turns_per_tick,
    )

    try:
        while True:
            # Check loop limit
            if loop_config.max_loops is not None and loops_run >= loop_config.max_loops:
                logger.info("Max loops (%d) reached, exiting.", loop_config.max_loops)
                return 0

            # Run one tick
            tick_result = agent_tick(
                service=service,
                agent_config=agent_config,
                max_turns=loop_config.max_turns_per_tick,
                workspace_root=workspace_root,
                integration_root=integration_root,
                prompt_file=prompt_file,
                skills_mode=skills_mode,
                codex_sandbox=codex_sandbox,
                telegram_sender=telegram_sender,
            )

            loops_run += 1

            # Log tick result
            if tick_result.error:
                logger.warning(
                    "Tick %d error: %s (turns=%d, tools=%d)",
                    loops_run, tick_result.error,
                    tick_result.turns_used, tick_result.tool_calls,
                )
            else:
                logger.info(
                    "Tick %d: turns=%d, tools=%d, active_runs=%d, tokens=%d/%d",
                    loops_run, tick_result.turns_used, tick_result.tool_calls,
                    tick_result.active_runs,
                    tick_result.input_tokens, tick_result.output_tokens,
                )

            # Record audit
            _record_tick_audit(service, tick_result, loops_run)

            # Idle tracking
            if tick_result.tool_calls == 0 and tick_result.active_runs == 0:
                consecutive_idle += 1
            else:
                consecutive_idle = 0

            # Non-persistent: exit after idle ticks
            if not loop_config.persistent and consecutive_idle >= _IDLE_EXIT_TICKS:
                logger.info("Idle for %d ticks, exiting (non-persistent mode).", consecutive_idle)
                return 0

            # Determine sleep duration
            sleep_sec = loop_config.interval_sec
            if loop_config.persistent and consecutive_idle > 0:
                sleep_sec = min(loop_config.interval_sec * 2, _MAX_HIBERNATE_SEC)
                logger.debug("Hibernating for %ds (idle count: %d)", sleep_sec, consecutive_idle)

            # Sleep with wake file check
            _sleep_with_wake(sleep_sec, wake_path)

    except KeyboardInterrupt:
        logger.info("Agent loop interrupted by user.")
        return 130


def agent_tick(
    *,
    service: OrchestratorService,
    agent_config: AgentConfig,
    max_turns: int = 15,
    workspace_root: Path,
    integration_root: Path,
    prompt_file: Path | None = None,
    skills_mode: str = "agentpr",
    codex_sandbox: str | None = None,
    telegram_sender: Callable[[str], None] | None = None,
    run_id: str | None = None,
) -> AgentTickResult:
    """Run a single agent tick. Can target a specific run or process all active runs."""
    # Build toolkit
    toolkit = AgentToolkit(
        service=service,
        workspace_root=workspace_root,
        integration_root=integration_root,
        prompt_file=prompt_file,
        skills_mode=skills_mode,
        codex_sandbox=codex_sandbox,
        telegram_sender=telegram_sender,
    )

    # List active runs
    all_runs = service.list_runs(limit=50)
    _TERMINAL = {"DONE", "SKIPPED", "FAILED_TERMINAL"}
    active_runs = [
        r for r in all_runs
        if r.get("current_state", r.get("state", "")).upper() not in _TERMINAL
    ]

    if run_id:
        active_runs = [r for r in all_runs if r.get("run_id") == run_id]

    if not active_runs:
        return AgentTickResult(ok=True, active_runs=0)

    # Build context
    runs_summary = "\n".join(_format_run_summary(r) for r in active_runs)

    # Build global stats
    total = len(all_runs)
    active = len(active_runs)
    by_state: dict[str, int] = {}
    for r in active_runs:
        s = r.get("current_state", r.get("state", "?"))
        by_state[s] = by_state.get(s, 0) + 1
    stats = f"Total runs: {total}, Active: {active}. By state: {by_state}"

    context = build_tick_context(
        active_runs_summary=runs_summary,
        global_stats=stats,
    )

    # Override max_turns in config for this tick
    tick_config = AgentConfig(
        api_base=agent_config.api_base,
        api_key=agent_config.api_key,
        model=agent_config.model,
        max_turns=max_turns,
        temperature=agent_config.temperature,
        timeout_sec=agent_config.timeout_sec,
    )

    # Run agent session
    result: AgentResult = run_agent_session(
        config=tick_config,
        system_prompt=MANAGER_SYSTEM_PROMPT,
        context=context,
        tools=get_tool_schemas(),
        tool_executor=toolkit.execute,
    )

    return AgentTickResult(
        ok=result.error is None,
        turns_used=result.turns_used,
        tool_calls=len(result.tool_calls_made),
        tools_used=[tc["name"] for tc in result.tool_calls_made],
        active_runs=len(active_runs),
        input_tokens=result.total_input_tokens,
        output_tokens=result.total_output_tokens,
        error=result.error,
    )


def _record_tick_audit(
    service: OrchestratorService,
    tick_result: AgentTickResult,
    tick_number: int,
) -> None:
    """Record tick result as audit artifact for debugging and analysis."""
    try:
        # Store as a global artifact (not run-specific)
        # Use a special "system" run_id convention
        audit_data = {
            "tick_number": tick_number,
            "ok": tick_result.ok,
            "turns_used": tick_result.turns_used,
            "tool_calls": tick_result.tool_calls,
            "tools_used": tick_result.tools_used,
            "active_runs": tick_result.active_runs,
            "input_tokens": tick_result.input_tokens,
            "output_tokens": tick_result.output_tokens,
            "error": tick_result.error,
            "timestamp": time.time(),
        }
        logger.debug("Tick audit: %s", json.dumps(audit_data, ensure_ascii=False)[:500])
    except Exception:
        pass  # Best-effort audit logging


def _sleep_with_wake(sleep_sec: int, wake_path: Path) -> None:
    """Sleep with periodic wake file checks."""
    check_interval = min(sleep_sec, 5)
    elapsed = 0.0
    while elapsed < sleep_sec:
        # Check wake file
        if wake_path.exists():
            try:
                wake_path.unlink(missing_ok=True)
                logger.info("Wake file detected, interrupting sleep.")
            except OSError:
                pass
            return
        time.sleep(check_interval)
        elapsed += check_interval
