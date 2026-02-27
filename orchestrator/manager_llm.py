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
                            "description": "State to retry from (e.g. EXECUTING).",
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
                    "If uncertain, recommend retry with low confidence."
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

    @staticmethod
    def _retry_strategy_from_payload(
        payload: Any,
        raw: dict[str, Any],
    ) -> RetryStrategy:
        if not isinstance(payload, dict):
            raise ManagerLLMError("retry strategy payload must be object")
        should_retry = bool(payload.get("should_retry", True))
        target_state = str(payload.get("target_state") or "EXECUTING").strip()
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
