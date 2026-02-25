from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any


class ManagerLLMError(RuntimeError):
    pass


@dataclass(frozen=True)
class ManagerLLMConfig:
    api_base: str
    api_key: str
    model: str
    timeout_sec: int


@dataclass(frozen=True)
class ManagerLLMSelection:
    action: str
    reason: str
    target_state: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class BotLLMSelection:
    action: str
    reason: str
    run_id: str | None
    repo_ref: str | None
    repo_refs: list[str] | None
    prompt_version: str | None
    target_state: str | None
    limit: int | None
    raw: dict[str, Any]


class ManagerLLMClient:
    def __init__(self, config: ManagerLLMConfig) -> None:
        self.config = config

    @classmethod
    def from_runtime(
        cls,
        *,
        api_base: str | None,
        model: str | None,
        timeout_sec: int,
        api_key_env: str,
    ) -> "ManagerLLMClient":
        key_env = str(api_key_env or "AGENTPR_MANAGER_API_KEY").strip() or "AGENTPR_MANAGER_API_KEY"
        api_key = str(os.environ.get(key_env) or "").strip()
        if not api_key:
            raise ManagerLLMError(f"missing manager api key env: {key_env}")
        resolved_base = str(api_base or os.environ.get("AGENTPR_MANAGER_API_BASE") or "https://api.openai.com/v1").rstrip("/")
        resolved_model = str(model or os.environ.get("AGENTPR_MANAGER_MODEL") or "gpt-4o-mini").strip()
        if not resolved_model:
            raise ManagerLLMError("missing manager model")
        return cls(
            ManagerLLMConfig(
                api_base=resolved_base,
                api_key=api_key,
                model=resolved_model,
                timeout_sec=max(int(timeout_sec), 1),
            )
        )

    def decide_action(
        self,
        *,
        facts: dict[str, Any],
        allowed_actions: list[str],
    ) -> ManagerLLMSelection:
        if not allowed_actions:
            raise ManagerLLMError("allowed_actions is empty")

        tool_schema = {
            "type": "function",
            "function": {
                "name": "select_action",
                "description": "Select one next manager action for this run.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": allowed_actions,
                        },
                        "reason": {
                            "type": "string",
                            "description": "One-sentence rationale.",
                        },
                        "target_state": {
                            "type": "string",
                            "description": "Required only for retry action.",
                        },
                    },
                    "required": ["action", "reason"],
                    "additionalProperties": False,
                },
            },
        }

        messages = [
            {
                "role": "system",
                "content": (
                    "You are AgentPR manager. Pick exactly one next action. "
                    "Use deterministic run_digest evidence when available. "
                    "Prefer progressing the workflow when an executable action is allowed. "
                    "Choose WAIT_HUMAN only for explicit blockers that require human input."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "run_facts": facts,
                        "allowed_actions": allowed_actions,
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                ),
            },
        ]

        payload = {
            "model": self.config.model,
            "temperature": 0,
            "messages": messages,
            "tools": [tool_schema],
            "tool_choice": {
                "type": "function",
                "function": {"name": "select_action"},
            },
        }
        try:
            data = self._request_chat_completion(payload)
            return self._parse_manager_selection_from_response(data)
        except ManagerLLMError as exc:
            if not self._should_try_json_fallback(exc):
                raise
            parsed = self._request_json_fallback(
                messages=messages,
                schema_instruction=(
                    "Return ONLY one compact JSON object with fields: "
                    "action (enum), reason (string), target_state (optional string). "
                    f"Allowed action values: {allowed_actions}."
                ),
            )
            return self._selection_from_payload(
                parsed,
                {
                    "fallback_mode": "json_no_tools",
                    "fallback_reason": str(exc),
                },
            )

    def decide_bot_action(
        self,
        *,
        user_text: str,
        context: dict[str, Any],
        allowed_actions: list[str],
    ) -> BotLLMSelection:
        if not allowed_actions:
            raise ManagerLLMError("allowed_actions is empty")

        tool_schema = {
            "type": "function",
            "function": {
                "name": "select_bot_action",
                "description": "Select one bot action from user natural-language request.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": allowed_actions,
                        },
                        "reason": {
                            "type": "string",
                            "description": "Short rationale.",
                        },
                        "run_id": {
                            "type": "string",
                            "description": "Target run id when action needs a run.",
                        },
                        "repo_ref": {
                            "type": "string",
                            "description": "Repo ref for create_run action: owner/repo or github URL.",
                        },
                        "repo_refs": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Repo refs for create_runs action: owner/repo or github URL.",
                        },
                        "prompt_version": {
                            "type": "string",
                            "description": "Optional prompt version for create_run.",
                        },
                        "target_state": {
                            "type": "string",
                            "description": "Target state for resume/retry actions.",
                        },
                        "limit": {
                            "type": "integer",
                            "description": "Optional list size for list action.",
                        },
                    },
                    "required": ["action", "reason"],
                    "additionalProperties": False,
                },
            },
        }

        messages = [
            {
                "role": "system",
                "content": (
                    "You are AgentPR Telegram manager router. "
                    "Select exactly one action. Be conservative and safe."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {
                        "text": user_text,
                        "context": context,
                        "allowed_actions": allowed_actions,
                    },
                    ensure_ascii=True,
                    sort_keys=True,
                ),
            },
        ]

        payload = {
            "model": self.config.model,
            "temperature": 0,
            "messages": messages,
            "tools": [tool_schema],
            "tool_choice": {
                "type": "function",
                "function": {"name": "select_bot_action"},
            },
        }
        try:
            data = self._request_chat_completion(payload)
            return self._parse_bot_selection_from_response(data)
        except ManagerLLMError as exc:
            if not self._should_try_json_fallback(exc):
                raise
            parsed = self._request_json_fallback(
                messages=messages,
                schema_instruction=(
                    "Return ONLY one compact JSON object with fields: "
                    "action (enum), reason (string), run_id (optional string), "
                    "repo_ref (optional string), repo_refs (optional string array), "
                    "prompt_version (optional string), "
                    "target_state (optional string), limit (optional integer). "
                    f"Allowed action values: {allowed_actions}."
                ),
            )
            return self._bot_selection_from_payload(
                parsed,
                {
                    "fallback_mode": "json_no_tools",
                    "fallback_reason": str(exc),
                },
            )

    def _request_chat_completion(self, payload: dict[str, Any]) -> dict[str, Any]:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(
            url=f"{self.config.api_base}/chat/completions",
            data=body,
            headers={
                "Authorization": f"Bearer {self.config.api_key}",
                "Content-Type": "application/json",
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.config.timeout_sec) as resp:
                raw = resp.read().decode("utf-8")
        except urllib.error.HTTPError as exc:
            raw_error = ""
            try:
                raw_error = exc.read().decode("utf-8", errors="replace")
            except Exception:  # noqa: BLE001
                raw_error = ""
            detail = f" | response: {raw_error[:600]}" if raw_error else ""
            raise ManagerLLMError(
                f"manager llm request failed: HTTP {exc.code} {exc.reason}{detail}"
            ) from exc
        except urllib.error.URLError as exc:
            raise ManagerLLMError(f"manager llm request failed: {exc}") from exc
        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ManagerLLMError(
                f"manager llm invalid response: {raw[:400]}"
            ) from exc
        return data

    def _request_json_fallback(
        self,
        *,
        messages: list[dict[str, Any]],
        schema_instruction: str,
    ) -> dict[str, Any]:
        fallback_messages = [
            *messages,
            {
                "role": "system",
                "content": schema_instruction,
            },
        ]
        payload = {
            "model": self.config.model,
            "temperature": 0,
            "messages": fallback_messages,
        }
        data = self._request_chat_completion(payload)
        return self._parse_json_content_payload(data)

    def _parse_json_content_payload(self, raw: dict[str, Any]) -> dict[str, Any]:
        choices = raw.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ManagerLLMError("manager llm missing choices")
        message = (choices[0] or {}).get("message")
        if not isinstance(message, dict):
            raise ManagerLLMError("manager llm missing message")
        content = self._extract_text_content(message.get("content"))
        if not content:
            raise ManagerLLMError("manager llm content is empty")
        try:
            parsed = json.loads(content)
        except json.JSONDecodeError as exc:
            raise ManagerLLMError(
                f"manager llm content is not json: {content[:400]}"
            ) from exc
        if not isinstance(parsed, dict):
            raise ManagerLLMError("manager llm content json must be object")
        return parsed

    def _parse_manager_selection_from_response(
        self, raw: dict[str, Any]
    ) -> ManagerLLMSelection:
        choices = raw.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ManagerLLMError("manager llm missing choices")
        message = (choices[0] or {}).get("message")
        if not isinstance(message, dict):
            raise ManagerLLMError("manager llm missing message")

        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            first_call = tool_calls[0] if isinstance(tool_calls[0], dict) else {}
            fn_payload = (
                first_call.get("function")
                if isinstance(first_call.get("function"), dict)
                else {}
            )
            arguments = str(fn_payload.get("arguments") or "{}").strip()
            try:
                parsed = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ManagerLLMError(
                    f"manager llm invalid tool arguments: {arguments[:400]}"
                ) from exc
            return self._selection_from_payload(parsed, raw)

        parsed = self._parse_json_content_payload(raw)
        return self._selection_from_payload(parsed, raw)

    def _parse_bot_selection_from_response(self, raw: dict[str, Any]) -> BotLLMSelection:
        choices = raw.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ManagerLLMError("manager llm missing choices")
        message = (choices[0] or {}).get("message")
        if not isinstance(message, dict):
            raise ManagerLLMError("manager llm missing message")

        tool_calls = message.get("tool_calls")
        if isinstance(tool_calls, list) and tool_calls:
            first_call = tool_calls[0] if isinstance(tool_calls[0], dict) else {}
            fn_payload = (
                first_call.get("function")
                if isinstance(first_call.get("function"), dict)
                else {}
            )
            arguments = str(fn_payload.get("arguments") or "{}").strip()
            try:
                parsed = json.loads(arguments)
            except json.JSONDecodeError as exc:
                raise ManagerLLMError(
                    f"manager llm invalid bot tool arguments: {arguments[:400]}"
                ) from exc
            return self._bot_selection_from_payload(parsed, raw)

        parsed = self._parse_json_content_payload(raw)
        return self._bot_selection_from_payload(parsed, raw)

    @staticmethod
    def _extract_text_content(content: Any) -> str:
        if isinstance(content, str):
            return content.strip()
        if isinstance(content, list):
            out: list[str] = []
            for item in content:
                if not isinstance(item, dict):
                    continue
                text = item.get("text")
                if isinstance(text, str) and text.strip():
                    out.append(text.strip())
            return "\n".join(out).strip()
        return ""

    @staticmethod
    def _should_try_json_fallback(exc: ManagerLLMError) -> bool:
        text = str(exc).lower()
        return "http 400" in text or "bad request" in text

    @staticmethod
    def _selection_from_payload(payload: Any, raw: dict[str, Any]) -> ManagerLLMSelection:
        if not isinstance(payload, dict):
            raise ManagerLLMError("manager llm payload must be object")
        action = str(payload.get("action") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        target_state_raw = payload.get("target_state")
        target_state = str(target_state_raw).strip() if isinstance(target_state_raw, str) and target_state_raw.strip() else None
        if not action:
            raise ManagerLLMError("manager llm payload missing action")
        if not reason:
            reason = "llm selected next action"
        return ManagerLLMSelection(
            action=action,
            reason=reason,
            target_state=target_state,
            raw=raw,
        )

    @staticmethod
    def _bot_selection_from_payload(payload: Any, raw: dict[str, Any]) -> BotLLMSelection:
        if not isinstance(payload, dict):
            raise ManagerLLMError("manager llm bot payload must be object")
        action = str(payload.get("action") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        run_id_raw = payload.get("run_id")
        repo_ref_raw = payload.get("repo_ref")
        repo_refs_raw = payload.get("repo_refs")
        prompt_version_raw = payload.get("prompt_version")
        target_state_raw = payload.get("target_state")
        limit_raw = payload.get("limit")
        run_id = (
            str(run_id_raw).strip()
            if isinstance(run_id_raw, str) and run_id_raw.strip()
            else None
        )
        repo_ref = (
            str(repo_ref_raw).strip()
            if isinstance(repo_ref_raw, str) and repo_ref_raw.strip()
            else None
        )
        repo_refs: list[str] | None = None
        if isinstance(repo_refs_raw, list):
            parsed_refs = [
                str(item).strip()
                for item in repo_refs_raw
                if isinstance(item, str) and str(item).strip()
            ]
            if parsed_refs:
                repo_refs = parsed_refs
        prompt_version = (
            str(prompt_version_raw).strip()
            if isinstance(prompt_version_raw, str) and prompt_version_raw.strip()
            else None
        )
        target_state = (
            str(target_state_raw).strip()
            if isinstance(target_state_raw, str) and target_state_raw.strip()
            else None
        )
        limit: int | None = None
        if isinstance(limit_raw, int):
            limit = limit_raw
        elif isinstance(limit_raw, float):
            limit = int(limit_raw)
        if not action:
            raise ManagerLLMError("manager llm bot payload missing action")
        if not reason:
            reason = "llm selected bot action"
        return BotLLMSelection(
            action=action,
            reason=reason,
            run_id=run_id,
            repo_ref=repo_ref,
            repo_refs=repo_refs,
            prompt_version=prompt_version,
            target_state=target_state,
            limit=limit,
            raw=raw,
        )
