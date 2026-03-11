"""Manager Agent session — multi-turn tool-calling loop via Forge /chat/completions.

Replaces 9 x 1-shot LLM calls + rules engine with a single agent session.
Each event triggers a bounded session (max_turns). Session is stateless across
invocations — context rebuilt from DB each time.
"""

from __future__ import annotations

import json
import logging
import os
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable

logger = logging.getLogger("agentpr.agent_session")


@dataclass(frozen=True)
class AgentConfig:
    """Configuration for the agent session."""

    api_base: str
    api_key: str
    model: str
    max_turns: int = 15
    temperature: float = 0
    timeout_sec: int = 120


@dataclass
class AgentResult:
    """Result of a completed agent session."""

    final_text: str
    turns_used: int
    tool_calls_made: list[dict[str, Any]] = field(default_factory=list)
    total_input_tokens: int = 0
    total_output_tokens: int = 0
    error: str | None = None


def create_config_from_env(
    *,
    max_turns: int = 15,
    temperature: float = 0,
    timeout_sec: int = 120,
) -> AgentConfig:
    """Create AgentConfig from environment variables (same as existing ManagerLLMClient)."""
    api_key = os.environ.get("AGENTPR_MANAGER_API_KEY", "").strip()
    if not api_key:
        raise ValueError("AGENTPR_MANAGER_API_KEY not set")
    api_base = os.environ.get("AGENTPR_MANAGER_API_BASE", "https://api.openai.com/v1").rstrip("/")
    model = os.environ.get("AGENTPR_MANAGER_MODEL", "tensorblock/gpt-4o").strip()
    return AgentConfig(
        api_base=api_base,
        api_key=api_key,
        model=model,
        max_turns=max_turns,
        temperature=temperature,
        timeout_sec=timeout_sec,
    )


def run_agent_session(
    *,
    config: AgentConfig,
    system_prompt: str,
    context: str,
    tools: list[dict[str, Any]],
    tool_executor: Callable[[str, dict[str, Any]], str],
) -> AgentResult:
    """Run a bounded agent session. Returns when agent stops calling tools or max_turns reached.

    Uses OpenAI /chat/completions format through Forge — model-agnostic.
    """
    messages: list[dict[str, Any]] = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": context},
    ]

    result = AgentResult(final_text="", turns_used=0)

    for turn in range(config.max_turns):
        result.turns_used = turn + 1

        # Call LLM
        payload: dict[str, Any] = {
            "model": config.model,
            "messages": messages,
            "temperature": config.temperature,
        }
        if tools:
            payload["tools"] = tools

        try:
            data = _request_chat_completion(config, payload)
        except Exception as exc:
            result.error = str(exc)
            result.final_text = f"LLM request failed: {exc}"
            return result

        # Track token usage
        usage = data.get("usage") or {}
        result.total_input_tokens += usage.get("prompt_tokens", 0)
        result.total_output_tokens += usage.get("completion_tokens", 0)

        # Extract message
        choices = data.get("choices") or []
        if not choices:
            result.error = "No choices in response"
            result.final_text = "LLM returned no choices."
            return result

        message = choices[0].get("message", {})
        messages.append(message)

        # Check for tool calls
        tool_calls = message.get("tool_calls")
        if not tool_calls:
            # No tool calls — agent is done
            result.final_text = message.get("content") or ""
            return result

        # Execute each tool call
        for tc in tool_calls:
            fn = tc.get("function", {})
            name = fn.get("name", "")
            try:
                args = json.loads(fn.get("arguments", "{}"))
            except json.JSONDecodeError:
                args = {}

            result.tool_calls_made.append({"name": name, "args": args, "turn": turn})

            logger.info("agent tool call: %s(%s)", name, json.dumps(args, ensure_ascii=False)[:200])

            try:
                tool_result = tool_executor(name, args)
            except Exception as exc:
                tool_result = f"ERROR: Tool execution failed: {exc}"
                logger.error("tool %s failed: %s", name, exc)

            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "content": tool_result,
            })

    # Max turns reached
    result.final_text = "Max turns reached. Escalating to human."
    result.error = "max_turns_exceeded"
    return result


def _request_chat_completion(config: AgentConfig, payload: dict[str, Any]) -> dict[str, Any]:
    """Make a raw HTTP request to /chat/completions. Same pattern as ManagerLLMClient."""
    body = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(
        url=f"{config.api_base}/chat/completions",
        data=body,
        headers={
            "Authorization": f"Bearer {config.api_key}",
            "Content-Type": "application/json",
        },
        method="POST",
    )
    t0 = time.monotonic()
    try:
        with urllib.request.urlopen(req, timeout=config.timeout_sec) as resp:
            raw = resp.read().decode("utf-8")
    except urllib.error.HTTPError as exc:
        raw_error = ""
        try:
            raw_error = exc.read().decode("utf-8", errors="replace")[:600]
        except Exception:
            pass
        raise RuntimeError(f"HTTP {exc.code}: {raw_error}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"Request failed: {exc}") from exc

    elapsed = time.monotonic() - t0
    logger.debug("chat/completions took %.1fs", elapsed)

    try:
        return json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Invalid JSON response: {raw[:400]}") from exc
