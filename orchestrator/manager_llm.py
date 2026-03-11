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


@dataclass(frozen=True)
class WorkerOutputGrade:
    verdict: str
    reason: str
    confidence: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class DecisionCardExplanation:
    why_llm: str
    suggested_actions: list[str]
    confidence: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class ReviewCommentTriage:
    action: str  # fix_code | reply_explain | ignore
    reason: str
    confidence: str
    reply_draft: str | None
    raw: dict[str, Any]


@dataclass(frozen=True)
class RetryStrategy:
    should_retry: bool
    target_state: str
    modified_instructions: str
    reason: str
    confidence: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class CodeReviewIssue:
    severity: str  # HIGH | MEDIUM | LOW
    file: str
    description: str
    suggested_fix: str


@dataclass(frozen=True)
class CodeReviewResult:
    verdict: str  # CLEAN | HAS_ISSUES
    issues: list[CodeReviewIssue]
    reference_provider: str
    summary: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class PRReadinessResult:
    verdict: str  # APPROVE | NEEDS_HUMAN
    code_ok: bool
    body_ok: bool
    reasons: list[str]
    summary: str
    raw: dict[str, Any]


@dataclass(frozen=True)
class ChatResponse:
    """Response from the conversational chat agent."""

    reply: str | None
    tool_calls: list[dict[str, Any]]
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
                            "enum": [
                                "QUEUED",
                                "EXECUTING",
                                "ITERATING",
                                "DISCOVERY",
                                "IMPLEMENTING",
                            ],
                            "description": "Required only for retry action. Which state to retry from.",
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
                    "When latest_worker_grade is null and state is EXECUTING, the worker has not run yet — "
                    "this is normal for first attempts, proceed with run_agent_step. "
                    "Choose WAIT_HUMAN only for explicit blockers that require human input (not missing artifacts from unstarted runs)."
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
            return self._selection_from_payload(self._extract_tool_call_payload(data), data)
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
                            "enum": [
                                "QUEUED",
                                "EXECUTING",
                                "ITERATING",
                                "DISCOVERY",
                                "IMPLEMENTING",
                            ],
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
                    "Select exactly one action. Be conservative and safe. "
                    "The context may include 'recent_messages' — the last few "
                    "messages in this conversation. Use them to resolve ambiguous "
                    "references like 'that repo', 'retry it', 'the last one'."
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
            return self._bot_selection_from_payload(self._extract_tool_call_payload(data), data)
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

    def grade_worker_output(
        self,
        *,
        evidence: dict[str, Any],
    ) -> WorkerOutputGrade:
        tool_schema = {
            "type": "function",
            "function": {
                "name": "grade_worker_output",
                "description": (
                    "Grade worker output semantics for runtime classification."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "verdict": {
                            "type": "string",
                            "enum": ["PASS", "NEEDS_REVIEW", "FAIL"],
                        },
                        "reason": {
                            "type": "string",
                            "description": "One-sentence explanation grounded in evidence.",
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                        },
                    },
                    "required": ["verdict", "reason", "confidence"],
                    "additionalProperties": False,
                },
            },
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "You are AgentPR runtime semantic grader. "
                    "Use only provided evidence. Apply these fixed criteria: "
                    "(1) test infrastructure exists? "
                    "(2) if exists, required tests executed? "
                    "(3) if absent, alternative validation sufficient? "
                    "(4) change scope matches risk? "
                    "(5) PR-template testing expectations satisfied or not applicable? "
                    "(6) worker self-report aligns with evidence? "
                    "Output PASS only when criteria are clearly satisfied."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"worker_output_evidence": evidence},
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
                "function": {"name": "grade_worker_output"},
            },
        }
        try:
            data = self._request_chat_completion(payload)
            return self._worker_output_grade_from_payload(
                self._extract_tool_call_payload(data), data
            )
        except ManagerLLMError as exc:
            if not self._should_try_json_fallback(exc):
                raise
            parsed = self._request_json_fallback(
                messages=messages,
                schema_instruction=(
                    "Return ONLY one compact JSON object with fields: "
                    "verdict (PASS|NEEDS_REVIEW|FAIL), reason (string), "
                    "confidence (low|medium|high)."
                ),
            )
            return self._worker_output_grade_from_payload(
                parsed,
                {
                    "fallback_mode": "json_no_tools",
                    "fallback_reason": str(exc),
                },
            )

    def explain_decision_card(
        self,
        *,
        decision_card: dict[str, Any],
    ) -> DecisionCardExplanation:
        tool_schema = {
            "type": "function",
            "function": {
                "name": "explain_decision_card",
                "description": "Generate human-readable decision explanation and next steps.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "why_llm": {
                            "type": "string",
                            "description": "2-3 sentence explanation in operator language.",
                        },
                        "suggested_actions": {
                            "type": "array",
                            "items": {"type": "string"},
                            "description": "Concrete next actions for operator.",
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                        },
                    },
                    "required": ["why_llm", "suggested_actions", "confidence"],
                    "additionalProperties": False,
                },
            },
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "You are AgentPR operations manager. "
                    "Explain deterministic decision-card evidence in plain actionable terms. "
                    "Do not invent facts. Keep suggestions concrete and safe."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"decision_card": decision_card},
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
                "function": {"name": "explain_decision_card"},
            },
        }
        try:
            data = self._request_chat_completion(payload)
            return self._decision_card_explanation_from_payload(
                self._extract_tool_call_payload(data), data
            )
        except ManagerLLMError as exc:
            if not self._should_try_json_fallback(exc):
                raise
            parsed = self._request_json_fallback(
                messages=messages,
                schema_instruction=(
                    "Return ONLY one compact JSON object with fields: "
                    "why_llm (string), suggested_actions (string array), "
                    "confidence (low|medium|high)."
                ),
            )
            return self._decision_card_explanation_from_payload(
                parsed,
                {
                    "fallback_mode": "json_no_tools",
                    "fallback_reason": str(exc),
                },
            )

    def chat_with_human(
        self,
        *,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]] | None = None,
    ) -> ChatResponse:
        """Conversational agent for human interaction. Returns text and/or tool calls."""
        payload: dict[str, Any] = {
            "model": self.config.model,
            "temperature": 0.3,
            "messages": messages,
        }
        if tools:
            payload["tools"] = tools

        data = self._request_chat_completion(payload)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ManagerLLMError("manager llm missing choices")
        message = (choices[0] or {}).get("message")
        if not isinstance(message, dict):
            raise ManagerLLMError("manager llm missing message")

        reply = self._extract_text_content(message.get("content"))

        tool_calls: list[dict[str, Any]] = []
        tool_calls_raw = message.get("tool_calls")
        if isinstance(tool_calls_raw, list):
            for tc in tool_calls_raw:
                if not isinstance(tc, dict):
                    continue
                tc_id = str(tc.get("id") or "")
                fn = tc.get("function")
                if not isinstance(fn, dict):
                    continue
                name = str(fn.get("name") or "")
                args_str = str(fn.get("arguments") or "{}")
                try:
                    parsed_args = json.loads(args_str)
                except json.JSONDecodeError:
                    parsed_args = {}
                tool_calls.append({
                    "id": tc_id,
                    "type": "function",
                    "function": {"name": name, "arguments": args_str},
                    "parsed_args": parsed_args,
                    "name": name,
                })

        return ChatResponse(
            reply=reply or None,
            tool_calls=tool_calls,
            raw=data,
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

    def _extract_tool_call_payload(self, raw: dict[str, Any]) -> dict[str, Any]:
        """Extract parsed JSON payload from a tool-call response (or content fallback)."""
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
            if isinstance(parsed, dict):
                return parsed
        return self._parse_json_content_payload(raw)

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
        target_state = str(target_state_raw).strip().upper() if isinstance(target_state_raw, str) and target_state_raw.strip() else None
        # Validate target_state against allowed retry targets
        if target_state is not None and target_state not in ManagerLLMClient._VALID_RETRY_TARGETS:
            target_state = "EXECUTING"
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

    @staticmethod
    def _worker_output_grade_from_payload(
        payload: Any,
        raw: dict[str, Any],
    ) -> WorkerOutputGrade:
        if not isinstance(payload, dict):
            raise ManagerLLMError("manager llm grading payload must be object")
        verdict = str(payload.get("verdict") or "").strip().upper()
        reason = str(payload.get("reason") or "").strip()
        confidence = str(payload.get("confidence") or "").strip().lower()
        if verdict not in {"PASS", "NEEDS_REVIEW", "FAIL"}:
            raise ManagerLLMError(
                f"manager llm invalid grading verdict: {verdict}"
            )
        if confidence not in {"low", "medium", "high"}:
            confidence = "medium"
        if not reason:
            reason = "semantic grade inferred from worker evidence"
        return WorkerOutputGrade(
            verdict=verdict,
            reason=reason,
            confidence=confidence,
            raw=raw,
        )

    @staticmethod
    def _decision_card_explanation_from_payload(
        payload: Any,
        raw: dict[str, Any],
    ) -> DecisionCardExplanation:
        if not isinstance(payload, dict):
            raise ManagerLLMError("manager llm decision-card payload must be object")
        why_llm = str(payload.get("why_llm") or "").strip()
        suggested_actions_raw = payload.get("suggested_actions")
        confidence = str(payload.get("confidence") or "").strip().lower()
        if confidence not in {"low", "medium", "high"}:
            confidence = "medium"
        if not why_llm:
            why_llm = "LLM explanation unavailable; use deterministic decision-card evidence."
        suggested_actions: list[str] = []
        if isinstance(suggested_actions_raw, list):
            for item in suggested_actions_raw:
                text = str(item).strip()
                if not text:
                    continue
                suggested_actions.append(text)
        if not suggested_actions:
            suggested_actions = ["Review deterministic evidence and apply the suggested machine action."]
        return DecisionCardExplanation(
            why_llm=why_llm,
            suggested_actions=suggested_actions[:4],
            confidence=confidence,
            raw=raw,
        )

    # ------------------------------------------------------------------
    # generate_pr_description
    # ------------------------------------------------------------------

    _PR_DESCRIPTION_SYSTEM_PROMPT_WITH_TEMPLATE = (
        "You are writing a PR description for an open-source integration contribution.\n"
        "This PR adds Forge (an open-source LLM inference middleware) as a new provider to {project_name}.\n\n"
        "The repository provides a PR template (shown below). You MUST follow its structure:\n"
        "- Fill in every section the template defines, using the commit diff and test evidence.\n"
        "- If the template has checkboxes (e.g. `- [ ] Tests pass`), check them (`- [x]`) or leave unchecked based on evidence.\n"
        "- If the template has placeholder text (e.g. `<!-- describe changes -->`), replace it with real content.\n"
        "- Preserve the template's heading hierarchy and section order.\n"
        "- If a template section is not applicable, write 'N/A' rather than omitting it.\n\n"
        "After filling the template, append this exact line at the end:\n"
        "I work at TensorBlock and will help maintain this integration.\n\n"
        "Rules:\n"
        "- Be technical, not marketing. No superlatives.\n"
        "- Lead with what the code does, not what Forge is.\n"
        "- Use the actual class names, function names, and file paths from the diff.\n"
        '- Do NOT include "About Forge", "Motivation", or "Why Forge" sections — those are appended separately.\n'
        "- Usage section MUST be from a USER's perspective: environment variables to set and "
        "how to invoke/configure the integration (CLI command, config file entry, or constructor call). "
        "Do NOT show internal implementation functions that users never call directly.\n"
        "- Test Evidence: describe what test commands actually ran and their results "
        "(e.g. 'pytest ran 874 tests, all passed'). Do NOT output system internal codes or grading metadata.\n"
        "- Keep total output under 500 words.\n\n"
        "--- PR TEMPLATE ---\n{pr_template}\n--- END PR TEMPLATE ---"
    )

    _PR_DESCRIPTION_SYSTEM_PROMPT_DEFAULT = (
        "You are writing a PR description for an open-source integration contribution.\n"
        "This PR adds Forge (an open-source LLM inference middleware) as a new provider to {project_name}.\n\n"
        "Based on the commit diff and test evidence, generate ONLY these sections in markdown:\n\n"
        "## Summary\n"
        "1-2 sentences. Lead with what the code does technically.\n"
        'Example: "Adds ForgeAPI class extending OpenAICompatibleAPI, routing requests '
        "through Forge's OpenAI-compatible API endpoint.\"\n\n"
        "## Changes\n"
        "Per-file, one line each. Use actual filenames from the diff.\n"
        "Example:\n"
        "- `src/services/forge/llm.py`: New ForgeAPI class with settings and model resolution\n"
        "- `env.example`: Added FORGE_API_KEY and FORGE_API_BASE entries\n\n"
        "## Usage\n"
        "Show how a USER configures and uses Forge with this project. This means:\n"
        "- Environment variables to set (FORGE_API_KEY, optionally FORGE_API_BASE)\n"
        "- The user-facing command, config entry, or constructor call to select Forge\n"
        "- Example model name format if applicable (e.g. `Provider/model-name`)\n"
        "Do NOT show internal implementation functions, routing logic, or private methods. "
        "If the project is a CLI tool, show the CLI command. If it's a library, show the import + instantiation. "
        "If it's a GUI app, describe the configuration steps.\n\n"
        "## Test Evidence\n"
        "Describe what test/validation commands were actually executed and their results.\n"
        "Example: 'Ran `pytest tests/` — 874 tests passed, 0 failed.'\n"
        "Do NOT output internal grading codes, reason codes, or system metadata. "
        "Only describe what a developer would see when running the tests.\n\n"
        "Then add this exact line:\n"
        "I work at TensorBlock and will help maintain this integration.\n\n"
        "Rules:\n"
        "- Be technical, not marketing. No superlatives.\n"
        "- Lead with what the code does, not what Forge is.\n"
        "- Use the actual class names, function names, and file paths from the diff.\n"
        '- Do NOT include "About Forge", "Motivation", or "Why Forge" sections.\n'
        "- Keep total output under 400 words."
    )

    def generate_pr_description(
        self,
        *,
        project_name: str,
        diff_text: str,
        diff_stat: str,
        evidence: dict[str, Any],
        pr_template: str = "",
    ) -> str:
        """Generate diff-aware PR description sections via LLM.

        When *pr_template* is provided the LLM follows its structure and fills
        in sections / checkboxes.  Otherwise it falls back to a default
        4-section layout (Summary / Changes / Usage / Test Evidence).
        """
        pr_template_text = (pr_template or "").strip()
        if pr_template_text:
            system_prompt = self._PR_DESCRIPTION_SYSTEM_PROMPT_WITH_TEMPLATE.format(
                project_name=project_name,
                pr_template=pr_template_text,
            )
        else:
            system_prompt = self._PR_DESCRIPTION_SYSTEM_PROMPT_DEFAULT.format(
                project_name=project_name,
            )
        # Filter evidence to only include user-facing information.
        # Strip internal grading codes and system metadata that the LLM
        # would otherwise echo into the PR body.
        filtered_evidence: dict[str, Any] = {}
        _internal_keys = {
            "reason_code", "next_action", "semantic", "grade",
            "artifact_type", "artifact_uri", "ok", "run_id",
        }
        for k, v in evidence.items():
            if k in _internal_keys:
                continue
            if isinstance(v, dict):
                filtered_evidence[k] = {
                    sk: sv for sk, sv in v.items() if sk not in _internal_keys
                }
            else:
                filtered_evidence[k] = v
        user_content = json.dumps(
            {
                "diff_stat": diff_stat[:2000],
                "diff": diff_text[:4000],
                "evidence": filtered_evidence,
            },
            ensure_ascii=True,
        )
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]
        payload = {
            "model": self.config.model,
            "temperature": 0.3,
            "messages": messages,
        }
        data = self._request_chat_completion(payload)
        choices = data.get("choices")
        if not isinstance(choices, list) or not choices:
            raise ManagerLLMError("pr description: missing choices")
        message = (choices[0] or {}).get("message")
        if not isinstance(message, dict):
            raise ManagerLLMError("pr description: missing message")
        text = self._extract_text_content(message.get("content"))
        if not text:
            raise ManagerLLMError("pr description: empty content")
        return text

    # ------------------------------------------------------------------
    # triage_review_comment
    # ------------------------------------------------------------------

    def triage_review_comment(
        self,
        *,
        comment_body: str,
        run_context: dict[str, Any],
    ) -> ReviewCommentTriage:
        tool_schema = {
            "type": "function",
            "function": {
                "name": "triage_review_comment",
                "description": "Triage a PR review comment into an action.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "action": {
                            "type": "string",
                            "enum": ["fix_code", "reply_explain", "ignore"],
                        },
                        "reason": {
                            "type": "string",
                            "description": "One-sentence justification.",
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                        },
                        "reply_draft": {
                            "type": "string",
                            "description": "Draft reply if action is reply_explain.",
                        },
                    },
                    "required": ["action", "reason", "confidence"],
                    "additionalProperties": False,
                },
            },
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "You are AgentPR review-comment triage agent. "
                    "Decide the best action for each review comment. Criteria: "
                    "(1) changes_requested with concrete code suggestions → fix_code. "
                    "(2) Questions about design choices → reply_explain. "
                    "(3) Nitpicks, style-only, praise, or approvals → ignore. "
                    "(4) If uncertain, prefer fix_code over ignore."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"comment": comment_body, "run_context": run_context},
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
            "tool_choice": {"type": "function", "function": {"name": "triage_review_comment"}},
        }
        try:
            data = self._request_chat_completion(payload)
            return self._review_triage_from_payload(self._extract_tool_call_payload(data), data)
        except ManagerLLMError as exc:
            if not self._should_try_json_fallback(exc):
                raise
            parsed = self._request_json_fallback(
                messages=messages,
                schema_instruction=(
                    "Return ONLY one compact JSON object with fields: "
                    "action (fix_code|reply_explain|ignore), reason (string), "
                    "confidence (low|medium|high), reply_draft (string|null)."
                ),
            )
            return self._review_triage_from_payload(parsed, {"fallback_mode": "json_no_tools"})

    @staticmethod
    def _review_triage_from_payload(
        payload: Any,
        raw: dict[str, Any],
    ) -> ReviewCommentTriage:
        if not isinstance(payload, dict):
            raise ManagerLLMError("review triage payload must be object")
        action = str(payload.get("action") or "").strip().lower()
        if action not in {"fix_code", "reply_explain", "ignore"}:
            raise ManagerLLMError(f"invalid triage action: {action}")
        reason = str(payload.get("reason") or "").strip()
        confidence = str(payload.get("confidence") or "medium").strip().lower()
        if confidence not in {"low", "medium", "high"}:
            confidence = "medium"
        reply_draft = payload.get("reply_draft")
        reply_draft = str(reply_draft).strip() if reply_draft else None
        return ReviewCommentTriage(
            action=action,
            reason=reason or "triage decision",
            confidence=confidence,
            reply_draft=reply_draft,
            raw=raw,
        )

    # ------------------------------------------------------------------
    # suggest_retry_strategy
    # ------------------------------------------------------------------

    def suggest_retry_strategy(
        self,
        *,
        failure_evidence: dict[str, Any],
    ) -> RetryStrategy:
        tool_schema = {
            "type": "function",
            "function": {
                "name": "suggest_retry_strategy",
                "description": "Analyze failure and recommend retry strategy.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "should_retry": {
                            "type": "boolean",
                            "description": "Whether retrying is worthwhile.",
                        },
                        "target_state": {
                            "type": "string",
                            "enum": [
                                "QUEUED",
                                "EXECUTING",
                                "ITERATING",
                                "DISCOVERY",
                                "IMPLEMENTING",
                            ],
                            "description": "State to retry from.",
                        },
                        "modified_instructions": {
                            "type": "string",
                            "description": "Adjusted instructions for the retry attempt.",
                        },
                        "reason": {
                            "type": "string",
                            "description": "One-sentence explanation.",
                        },
                        "confidence": {
                            "type": "string",
                            "enum": ["low", "medium", "high"],
                        },
                    },
                    "required": ["should_retry", "reason", "confidence"],
                    "additionalProperties": False,
                },
            },
        }
        messages = [
            {
                "role": "system",
                "content": (
                    "You are AgentPR failure-diagnosis agent. "
                    "Given failure evidence, decide: "
                    "(1) Is retrying worthwhile or will it repeat the same error? "
                    "(2) What target state to retry from? "
                    "(3) What instructions should change for the retry? "
                    "Criteria: environment/transient errors → retry. "
                    "Fundamental misunderstanding of task → do not retry. "
                    "Test failures with clear fix path → retry with specific guidance. "
                    "If uncertain, recommend retry with low confidence. "
                    "The evidence may include 'previous_attempts' (number of prior retries). "
                    "If previous_attempts >= 2, be more skeptical about retrying — "
                    "the same approach is likely to fail again."
                ),
            },
            {
                "role": "user",
                "content": json.dumps(
                    {"failure_evidence": failure_evidence},
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
            "tool_choice": {"type": "function", "function": {"name": "suggest_retry_strategy"}},
        }
        try:
            data = self._request_chat_completion(payload)
            return self._retry_strategy_from_payload(self._extract_tool_call_payload(data), data)
        except ManagerLLMError as exc:
            if not self._should_try_json_fallback(exc):
                raise
            parsed = self._request_json_fallback(
                messages=messages,
                schema_instruction=(
                    "Return ONLY one compact JSON object with fields: "
                    "should_retry (boolean), target_state (string), "
                    "modified_instructions (string), reason (string), "
                    "confidence (low|medium|high)."
                ),
            )
            return self._retry_strategy_from_payload(parsed, {"fallback_mode": "json_no_tools"})

    _VALID_RETRY_TARGETS: set[str] = {
        "QUEUED",
        "EXECUTING",
        "ITERATING",
        "DISCOVERY",
        "IMPLEMENTING",
    }

    @staticmethod
    def _retry_strategy_from_payload(
        payload: Any,
        raw: dict[str, Any],
    ) -> RetryStrategy:
        if not isinstance(payload, dict):
            raise ManagerLLMError("retry strategy payload must be object")
        should_retry = bool(payload.get("should_retry", True))
        target_state_raw = str(payload.get("target_state") or "").strip().upper()
        # Validate against allowed retry targets; default to EXECUTING
        if target_state_raw not in ManagerLLMClient._VALID_RETRY_TARGETS:
            target_state_raw = "EXECUTING"
        target_state = target_state_raw
        modified_instructions = str(payload.get("modified_instructions") or "").strip()
        reason = str(payload.get("reason") or "").strip()
        confidence = str(payload.get("confidence") or "medium").strip().lower()
        if confidence not in {"low", "medium", "high"}:
            confidence = "medium"
        return RetryStrategy(
            should_retry=should_retry,
            target_state=target_state,
            modified_instructions=modified_instructions,
            reason=reason or "retry strategy decision",
            confidence=confidence,
            raw=raw,
        )

    # ------------------------------------------------------------------
    # review_code_changes — deep code review before PR creation
    # ------------------------------------------------------------------

    _CODE_REVIEW_SYSTEM_PROMPT = (
        "You are a senior code reviewer evaluating a Forge integration PR for {project_name}.\n\n"
        "Your job is to find bugs, pattern violations, and downstream breakage that automated "
        "tests cannot catch. You have access to:\n"
        "1. The git diff (what changed)\n"
        "2. The full content of each changed file (for context)\n"
        "3. Sibling files in the same directories (potential reference providers)\n"
        "4. A review checklist encoding lessons from 17 previous integration reviews\n\n"
        "IMPORTANT REVIEW APPROACH:\n"
        "- For each changed file, identify the MOST SIMILAR existing provider in the repo "
        "and compare line-by-line.\n"
        "- Check if the Forge code inserts into any dispatch chain (if/elif/match). If so, "
        "analyze whether it shadows existing branches.\n"
        "- Check if a factory, router, or registry needs to be updated but wasn't.\n"
        "- Search for downstream callers of any modified functions/types.\n"
        "- Check environment variable handling against the reference provider.\n\n"
        "--- REVIEW CHECKLIST ---\n{checklist}\n--- END CHECKLIST ---\n\n"
        "Return a JSON object with:\n"
        "- verdict: 'CLEAN' or 'HAS_ISSUES'\n"
        "- issues: array of {{severity: 'HIGH'|'MEDIUM'|'LOW', file: string, "
        "description: string, suggested_fix: string}}\n"
        "- reference_provider: which existing provider you compared against\n"
        "- summary: 1-2 sentence overall assessment\n\n"
        "Only flag real issues. Do NOT flag pre-existing repo patterns that are not "
        "introduced by this PR. Focus on: does the Forge code break anything? "
        "Does it match the repo's patterns?"
    )

    def review_code_changes(
        self,
        *,
        project_name: str,
        diff_text: str,
        changed_files_content: dict[str, str],
        sibling_files_content: dict[str, str],
        checklist: str,
        previous_review: dict[str, Any] | None = None,
    ) -> CodeReviewResult:
        """Deep code review of worker-produced changes.

        Args:
            project_name: The repository name.
            diff_text: Full git diff output.
            changed_files_content: {filepath: content} for each changed file.
            sibling_files_content: {filepath: content} for reference provider files.
            checklist: The code review checklist markdown.

        Returns:
            CodeReviewResult with verdict, issues, and summary.
        """
        system_prompt = self._CODE_REVIEW_SYSTEM_PROMPT.format(
            project_name=project_name,
            checklist=checklist,
        )

        # Build user content with all context
        user_parts: list[str] = []
        user_parts.append("=== GIT DIFF ===")
        user_parts.append(diff_text[:8000])
        user_parts.append("\n=== CHANGED FILES (full content) ===")
        for fpath, content in changed_files_content.items():
            user_parts.append(f"\n--- {fpath} ---")
            user_parts.append(content[:6000])
        user_parts.append("\n=== SIBLING FILES (reference providers) ===")
        for fpath, content in sibling_files_content.items():
            user_parts.append(f"\n--- {fpath} ---")
            user_parts.append(content[:6000])

        if previous_review:
            user_parts.append("\n=== PREVIOUS REVIEW (this is a re-review after fixes) ===")
            user_parts.append(json.dumps(previous_review, indent=2, default=str)[:3000])
            user_parts.append(
                "\nFocus on whether the previous issues were fixed. "
                "Do not re-flag issues that have been resolved."
            )

        user_content = "\n".join(user_parts)
        # Truncate to stay within reasonable token limits
        if len(user_content) > 60000:
            user_content = user_content[:60000] + "\n... (truncated)"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        tool_schema = {
            "type": "function",
            "function": {
                "name": "submit_code_review",
                "description": "Submit the code review results.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "verdict": {
                            "type": "string",
                            "enum": ["CLEAN", "HAS_ISSUES"],
                            "description": "Overall review verdict.",
                        },
                        "issues": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "properties": {
                                    "severity": {
                                        "type": "string",
                                        "enum": ["HIGH", "MEDIUM", "LOW"],
                                    },
                                    "file": {"type": "string"},
                                    "description": {"type": "string"},
                                    "suggested_fix": {"type": "string"},
                                },
                                "required": ["severity", "file", "description", "suggested_fix"],
                            },
                            "description": "List of issues found. Empty if CLEAN.",
                        },
                        "reference_provider": {
                            "type": "string",
                            "description": "Which existing provider was used as reference.",
                        },
                        "summary": {
                            "type": "string",
                            "description": "1-2 sentence overall assessment.",
                        },
                    },
                    "required": ["verdict", "issues", "reference_provider", "summary"],
                },
            },
        }

        payload = {
            "model": self.config.model,
            "temperature": 0,
            "messages": messages,
            "tools": [tool_schema],
            "tool_choice": {"type": "function", "function": {"name": "submit_code_review"}},
        }

        try:
            data = self._request_chat_completion(payload)
            return self._code_review_from_payload(self._extract_tool_call_payload(data), data)
        except ManagerLLMError as exc:
            if not self._should_try_json_fallback(exc):
                raise
            parsed = self._request_json_fallback(
                messages=messages,
                schema_instruction=(
                    "Return ONLY one compact JSON object with fields: "
                    "verdict ('CLEAN' or 'HAS_ISSUES'), "
                    "issues (array of {severity, file, description, suggested_fix}), "
                    "reference_provider (string), summary (string)."
                ),
            )
            return self._code_review_from_payload(parsed, {"fallback_mode": "json_no_tools"})

    @staticmethod
    def _code_review_from_payload(
        payload: Any,
        raw: dict[str, Any],
    ) -> CodeReviewResult:
        if not isinstance(payload, dict):
            raise ManagerLLMError("code review payload must be object")
        verdict = str(payload.get("verdict") or "CLEAN").strip().upper()
        if verdict not in {"CLEAN", "HAS_ISSUES"}:
            verdict = "HAS_ISSUES"  # err on the side of caution
        raw_issues = payload.get("issues")
        issues: list[CodeReviewIssue] = []
        if isinstance(raw_issues, list):
            for item in raw_issues:
                if not isinstance(item, dict):
                    continue
                severity = str(item.get("severity") or "MEDIUM").strip().upper()
                if severity not in {"HIGH", "MEDIUM", "LOW"}:
                    severity = "MEDIUM"
                issues.append(CodeReviewIssue(
                    severity=severity,
                    file=str(item.get("file") or "unknown"),
                    description=str(item.get("description") or ""),
                    suggested_fix=str(item.get("suggested_fix") or ""),
                ))
        # If we have issues but verdict says CLEAN, override
        high_issues = [i for i in issues if i.severity == "HIGH"]
        if high_issues and verdict == "CLEAN":
            verdict = "HAS_ISSUES"
        reference_provider = str(payload.get("reference_provider") or "unknown").strip()
        summary = str(payload.get("summary") or "").strip()
        return CodeReviewResult(
            verdict=verdict,
            issues=issues,
            reference_provider=reference_provider,
            summary=summary or "Review completed.",
            raw=raw,
        )

    # ------------------------------------------------------------------
    # PR Readiness Assessment (final review before auto-creating PR)
    # ------------------------------------------------------------------

    _PR_READINESS_SYSTEM_PROMPT = (
        "You are the final reviewer before a Forge integration PR is posted to {project_name}.\n\n"
        "You have access to:\n"
        "1. The code review result (from a prior deep code review)\n"
        "2. The worker's grading evidence (test results, grade, changed files)\n"
        "3. The generated PR body that will be posted\n"
        "4. The repo's PR template (if any)\n"
        "5. The git diff\n"
        "6. Accumulated review principles from 17 previous integration PRs\n\n"
        "YOUR JOB: Decide if this PR is ready to be posted as-is, or if a human should review first.\n\n"
        "--- REVIEW PRINCIPLES (from 17 real PR reviews) ---\n"
        "1. REFERENCE PROVIDER: The code must match the MOST SIMILAR existing provider "
        "(Forge is an aggregator → reference OpenRouter/LiteLLM/AiHubMix, not Groq/Together).\n"
        "2. MINIMAL DIFF: Only necessary files modified. No unnecessary refactors, no lock files, "
        "no CI changes unless required.\n"
        "3. NO GLOBAL POLLUTION: No os.environ mutation. API key via explicit kwargs.\n"
        "4. DISPATCH INTEGRITY: If inserted into elif/match chain, no shadowing of existing branches. "
        "If factory/registry exists, Forge is registered.\n"
        "5. PR BODY QUALITY:\n"
        "   - Summary must describe actual code changes (not generic 'adds Forge support')\n"
        "   - Usage must be USER-FACING (env vars, CLI, config) not internal API functions\n"
        "   - Test Evidence must be REAL commands and results, not grading system codes\n"
        "   - About Forge must use verified facts (40+ providers, thousands of models, "
        "open-source middleware service)\n"
        "   - No marketing text (no Motivation/Why Forge/Key Benefits sections)\n"
        "6. PR TEMPLATE COMPLIANCE: If the repo has a PR template, all required sections/checkboxes "
        "must be addressed. Missing sections = NEEDS_HUMAN.\n"
        "7. WORKER 'CREATIVITY' RULE: Additive improvements that don't break anything (extra DRY "
        "helpers, env var fallback) are acceptable. Changes that alter types, break callers, or "
        "shadow branches are NOT.\n"
        "--- END PRINCIPLES ---\n\n"
        "Evaluate and return:\n"
        "- verdict: 'APPROVE' (safe to auto-post) or 'NEEDS_HUMAN' (human should review)\n"
        "- code_ok: true/false — is the code quality acceptable?\n"
        "- body_ok: true/false — is the PR body quality acceptable?\n"
        "- reasons: array of strings explaining your decision\n"
        "- summary: 1-2 sentence overall assessment\n\n"
        "IMPORTANT: Err on the side of APPROVE when the code review was CLEAN and the PR body "
        "covers all required sections. Only flag NEEDS_HUMAN for concrete issues, not hypothetical ones.\n"
        "If the code review found no HIGH issues and the PR body is reasonable, APPROVE."
    )

    def assess_pr_readiness(
        self,
        *,
        project_name: str,
        code_review_summary: str,
        code_review_verdict: str,
        code_review_issues: list[dict[str, str]],
        worker_evidence: dict[str, Any],
        pr_body: str,
        pr_template: str,
        diff_text: str,
    ) -> PRReadinessResult:
        """Final PR readiness assessment using all accumulated context.

        Args:
            project_name: Repository name.
            code_review_summary: Summary from the code review step.
            code_review_verdict: CLEAN or HAS_ISSUES.
            code_review_issues: List of issues from code review.
            worker_evidence: Grading evidence (grade, test_commands, changed_files, etc.).
            pr_body: The composed PR body text.
            pr_template: The repo's PR template (empty string if none).
            diff_text: The git diff.

        Returns:
            PRReadinessResult with verdict and detailed reasoning.
        """
        system_prompt = self._PR_READINESS_SYSTEM_PROMPT.format(
            project_name=project_name,
        )

        user_parts: list[str] = []

        user_parts.append("=== CODE REVIEW RESULT ===")
        user_parts.append(f"Verdict: {code_review_verdict}")
        user_parts.append(f"Summary: {code_review_summary}")
        if code_review_issues:
            user_parts.append("Issues:")
            for issue in code_review_issues[:10]:
                user_parts.append(
                    f"  - [{issue.get('severity', 'MEDIUM')}] {issue.get('file', '?')}: "
                    f"{issue.get('description', '')}"
                )

        user_parts.append("\n=== WORKER EVIDENCE ===")
        # Filter out internal grading keys
        _internal_keys = {
            "reason_code", "next_action", "semantic", "grade",
            "artifact_type", "artifact_uri", "ok", "run_id",
        }
        filtered_evidence = {
            k: v for k, v in worker_evidence.items()
            if k not in _internal_keys
        }
        user_parts.append(json.dumps(filtered_evidence, indent=2, default=str)[:3000])

        user_parts.append("\n=== PR BODY (will be posted) ===")
        user_parts.append(pr_body[:6000])

        if pr_template.strip():
            user_parts.append("\n=== REPO PR TEMPLATE (must comply) ===")
            user_parts.append(pr_template[:3000])

        user_parts.append("\n=== GIT DIFF ===")
        user_parts.append(diff_text[:6000])

        user_content = "\n".join(user_parts)
        if len(user_content) > 40000:
            user_content = user_content[:40000] + "\n... (truncated)"

        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_content},
        ]

        tool_schema = {
            "type": "function",
            "function": {
                "name": "submit_pr_readiness",
                "description": "Submit the PR readiness assessment.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "verdict": {
                            "type": "string",
                            "enum": ["APPROVE", "NEEDS_HUMAN"],
                        },
                        "code_ok": {"type": "boolean"},
                        "body_ok": {"type": "boolean"},
                        "reasons": {
                            "type": "array",
                            "items": {"type": "string"},
                        },
                        "summary": {"type": "string"},
                    },
                    "required": ["verdict", "code_ok", "body_ok", "reasons", "summary"],
                },
            },
        }

        payload = {
            "model": self.config.model,
            "temperature": 0,
            "messages": messages,
            "tools": [tool_schema],
            "tool_choice": {"type": "function", "function": {"name": "submit_pr_readiness"}},
        }

        try:
            data = self._request_chat_completion(payload)
            return self._pr_readiness_from_payload(self._extract_tool_call_payload(data), data)
        except ManagerLLMError as exc:
            if not self._should_try_json_fallback(exc):
                raise
            parsed = self._request_json_fallback(
                messages=messages,
                schema_instruction=(
                    "Return ONLY one compact JSON object with fields: "
                    "verdict ('APPROVE' or 'NEEDS_HUMAN'), "
                    "code_ok (boolean), body_ok (boolean), "
                    "reasons (array of strings), summary (string)."
                ),
            )
            return self._pr_readiness_from_payload(parsed, {"fallback_mode": "json_no_tools"})

    @staticmethod
    def _pr_readiness_from_payload(
        payload: Any,
        raw: dict[str, Any],
    ) -> PRReadinessResult:
        if not isinstance(payload, dict):
            raise ManagerLLMError("PR readiness payload must be object")
        verdict = str(payload.get("verdict") or "NEEDS_HUMAN").strip().upper()
        if verdict not in {"APPROVE", "NEEDS_HUMAN"}:
            verdict = "NEEDS_HUMAN"
        code_ok = bool(payload.get("code_ok", True))
        body_ok = bool(payload.get("body_ok", True))
        reasons = []
        raw_reasons = payload.get("reasons")
        if isinstance(raw_reasons, list):
            reasons = [str(r) for r in raw_reasons if r]
        # Safety: if code or body not ok, force NEEDS_HUMAN
        if not code_ok or not body_ok:
            verdict = "NEEDS_HUMAN"
        summary = str(payload.get("summary") or "").strip()
        return PRReadinessResult(
            verdict=verdict,
            code_ok=code_ok,
            body_ok=body_ok,
            reasons=reasons,
            summary=summary or "Assessment completed.",
            raw=raw,
        )
