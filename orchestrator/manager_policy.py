from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

DEFAULT_MANAGER_POLICY: dict[str, Any] = {
    "run_agent_step": {
        "codex_sandbox": "danger-full-access",
        "skills_mode": "off",
        "max_agent_seconds": 900,
        "max_changed_files": 8,
        "max_added_lines": 150,
        "max_retryable_attempts": 3,
        "min_test_commands": 1,
        "runtime_grading_mode": "hybrid",
        "known_test_failure_allowlist": [],
        "success_event_stream_sample_pct": 15,
        "success_state": "EXECUTING",
        "on_retryable_state": "FAILED",
        "on_human_review_state": "NEEDS_HUMAN_REVIEW",
        "repo_overrides": {},
    },
    "telegram_bot": {
        "poll_timeout_sec": 30,
        "idle_sleep_sec": 2,
        "list_limit": 20,
        "rate_limit_window_sec": 60,
        "rate_limit_per_chat": 12,
        "rate_limit_global": 120,
        "audit_log_file": "orchestrator/data/reports/telegram_audit.jsonl",
    },
    "github_webhook": {
        "max_payload_bytes": 1048576,
        "audit_log_file": "orchestrator/data/reports/github_webhook_audit.jsonl",
    },
}


@dataclass(frozen=True)
class RunAgentPolicy:
    codex_sandbox: str
    skills_mode: str
    max_agent_seconds: int
    max_changed_files: int
    max_added_lines: int
    max_retryable_attempts: int
    min_test_commands: int
    runtime_grading_mode: str
    known_test_failure_allowlist: list[str]
    success_event_stream_sample_pct: int
    success_state: str
    on_retryable_state: str
    on_human_review_state: str
    repo_overrides: dict[str, dict[str, Any]]


@dataclass(frozen=True)
class TelegramBotPolicy:
    poll_timeout_sec: int
    idle_sleep_sec: int
    list_limit: int
    rate_limit_window_sec: int
    rate_limit_per_chat: int
    rate_limit_global: int
    audit_log_file: str


@dataclass(frozen=True)
class GitHubWebhookPolicy:
    max_payload_bytes: int
    audit_log_file: str


@dataclass(frozen=True)
class ManagerPolicy:
    run_agent_step: RunAgentPolicy
    telegram_bot: TelegramBotPolicy
    github_webhook: GitHubWebhookPolicy
    source_path: Path
    source_loaded: bool


def load_manager_policy(path: Path) -> ManagerPolicy:
    payload: dict[str, Any] = {}
    loaded = False
    if path.exists():
        raw = path.read_text(encoding="utf-8")
        parsed = json.loads(raw)
        if not isinstance(parsed, dict):
            raise ValueError(f"Manager policy must be a JSON object: {path}")
        payload = parsed
        loaded = True

    merged = deep_merge(DEFAULT_MANAGER_POLICY, payload)

    run_agent = dict(merged.get("run_agent_step") or {})
    codex_sandbox = str(run_agent.get("codex_sandbox", "danger-full-access"))
    if codex_sandbox not in {"read-only", "workspace-write", "danger-full-access"}:
        raise ValueError(
            "Invalid manager policy run_agent_step.codex_sandbox: "
            f"{codex_sandbox}"
        )

    skills_mode = str(run_agent.get("skills_mode", "off"))
    if skills_mode not in {"off", "agentpr", "agentpr_autonomous"}:
        raise ValueError(
            "Invalid manager policy run_agent_step.skills_mode: "
            f"{skills_mode}"
        )

    max_changed_files = max(int(run_agent.get("max_changed_files", 8)), 0)
    max_added_lines = max(int(run_agent.get("max_added_lines", 150)), 0)
    max_agent_seconds = max(int(run_agent.get("max_agent_seconds", 900)), 0)
    max_retryable_attempts = max(int(run_agent.get("max_retryable_attempts", 3)), 0)
    min_test_commands = max(int(run_agent.get("min_test_commands", 1)), 0)
    runtime_grading_mode = str(
        run_agent.get("runtime_grading_mode", "hybrid")
    ).strip()
    if runtime_grading_mode not in {"rules", "hybrid", "hybrid_llm"}:
        raise ValueError(
            "Invalid manager policy run_agent_step.runtime_grading_mode: "
            f"{runtime_grading_mode}"
        )
    known_test_failure_allowlist = parse_string_list(
        run_agent.get("known_test_failure_allowlist", []),
        field_name="run_agent_step.known_test_failure_allowlist",
    )
    success_event_stream_sample_pct = min(
        max(int(run_agent.get("success_event_stream_sample_pct", 15)), 0),
        100,
    )

    success_state = normalize_target_state(
        run_agent.get("success_state", "EXECUTING"),
        allowed={"EXECUTING", "NEEDS_HUMAN_REVIEW", "UNCHANGED"},
        name="run_agent_step.success_state",
    )
    on_retryable_state = normalize_target_state(
        run_agent.get("on_retryable_state", "FAILED"),
        allowed={"FAILED", "NEEDS_HUMAN_REVIEW", "UNCHANGED"},
        name="run_agent_step.on_retryable_state",
    )
    on_human_review_state = normalize_target_state(
        run_agent.get("on_human_review_state", "NEEDS_HUMAN_REVIEW"),
        allowed={"FAILED", "NEEDS_HUMAN_REVIEW", "UNCHANGED"},
        name="run_agent_step.on_human_review_state",
    )
    repo_overrides = parse_repo_overrides(run_agent.get("repo_overrides", {}))

    telegram = dict(merged.get("telegram_bot") or {})
    poll_timeout_sec = max(int(telegram.get("poll_timeout_sec", 30)), 1)
    idle_sleep_sec = max(int(telegram.get("idle_sleep_sec", 2)), 1)
    list_limit = max(int(telegram.get("list_limit", 20)), 1)
    rate_limit_window_sec = max(int(telegram.get("rate_limit_window_sec", 60)), 1)
    rate_limit_per_chat = max(int(telegram.get("rate_limit_per_chat", 12)), 1)
    rate_limit_global = max(int(telegram.get("rate_limit_global", 120)), 1)
    audit_log_file = str(telegram.get("audit_log_file", "orchestrator/data/reports/telegram_audit.jsonl")).strip()
    if not audit_log_file:
        raise ValueError("Invalid manager policy telegram_bot.audit_log_file: empty value")

    webhook = dict(merged.get("github_webhook") or {})
    webhook_max_payload_bytes = max(int(webhook.get("max_payload_bytes", 1048576)), 1024)
    webhook_audit_log_file = str(
        webhook.get("audit_log_file", "orchestrator/data/reports/github_webhook_audit.jsonl")
    ).strip()
    if not webhook_audit_log_file:
        raise ValueError("Invalid manager policy github_webhook.audit_log_file: empty value")

    return ManagerPolicy(
        run_agent_step=RunAgentPolicy(
            codex_sandbox=codex_sandbox,
            skills_mode=skills_mode,
            max_agent_seconds=max_agent_seconds,
            max_changed_files=max_changed_files,
            max_added_lines=max_added_lines,
            max_retryable_attempts=max_retryable_attempts,
            min_test_commands=min_test_commands,
            runtime_grading_mode=runtime_grading_mode,
            known_test_failure_allowlist=known_test_failure_allowlist,
            success_event_stream_sample_pct=success_event_stream_sample_pct,
            success_state=success_state,
            on_retryable_state=on_retryable_state,
            on_human_review_state=on_human_review_state,
            repo_overrides=repo_overrides,
        ),
        telegram_bot=TelegramBotPolicy(
            poll_timeout_sec=poll_timeout_sec,
            idle_sleep_sec=idle_sleep_sec,
            list_limit=list_limit,
            rate_limit_window_sec=rate_limit_window_sec,
            rate_limit_per_chat=rate_limit_per_chat,
            rate_limit_global=rate_limit_global,
            audit_log_file=audit_log_file,
        ),
        github_webhook=GitHubWebhookPolicy(
            max_payload_bytes=webhook_max_payload_bytes,
            audit_log_file=webhook_audit_log_file,
        ),
        source_path=path,
        source_loaded=loaded,
    )


def normalize_target_state(value: Any, *, allowed: set[str], name: str) -> str:
    state = str(value or "").strip().upper()
    if state not in allowed:
        allowed_text = ", ".join(sorted(allowed))
        raise ValueError(f"Invalid manager policy {name}: {state} (allowed: {allowed_text})")
    return state


def parse_repo_overrides(value: Any) -> dict[str, dict[str, Any]]:
    if value is None:
        return {}
    if not isinstance(value, dict):
        raise ValueError("Invalid manager policy run_agent_step.repo_overrides: expected object")

    allowed_int_fields = {
        "max_agent_seconds",
        "max_changed_files",
        "max_added_lines",
        "max_retryable_attempts",
        "min_test_commands",
        "success_event_stream_sample_pct",
    }
    allowed_list_fields = {"known_test_failure_allowlist"}
    allowed_str_fields = {"skills_mode", "runtime_grading_mode"}
    out: dict[str, dict[str, Any]] = {}
    for raw_key, raw_override in value.items():
        key = normalize_repo_override_key(raw_key)
        if not isinstance(raw_override, dict):
            raise ValueError(
                "Invalid manager policy run_agent_step.repo_overrides."
                f"{raw_key}: expected object"
            )
        parsed: dict[str, Any] = {}
        for field, raw_field_value in raw_override.items():
            field_name = str(field).strip()
            if (
                field_name not in allowed_int_fields
                and field_name not in allowed_str_fields
                and field_name not in allowed_list_fields
            ):
                allowed_text = ", ".join(
                    sorted(allowed_int_fields | allowed_str_fields | allowed_list_fields)
                )
                raise ValueError(
                    "Invalid manager policy run_agent_step.repo_overrides."
                    f"{raw_key}.{field_name} (allowed: {allowed_text})"
                )
            if field_name in allowed_int_fields:
                value_int = max(int(raw_field_value), 0)
                if field_name == "success_event_stream_sample_pct":
                    value_int = min(value_int, 100)
                parsed[field_name] = value_int
                continue
            if field_name in allowed_list_fields:
                parsed[field_name] = parse_string_list(
                    raw_field_value,
                    field_name=(
                        "run_agent_step.repo_overrides."
                        f"{raw_key}.{field_name}"
                    ),
                )
                continue
            field_value = str(raw_field_value).strip()
            if field_name == "skills_mode":
                if field_value not in {"off", "agentpr", "agentpr_autonomous"}:
                    raise ValueError(
                        "Invalid manager policy run_agent_step.repo_overrides."
                        f"{raw_key}.skills_mode: {field_value}"
                    )
                parsed[field_name] = field_value
                continue
            if field_name == "runtime_grading_mode":
                if field_value not in {"rules", "hybrid", "hybrid_llm"}:
                    raise ValueError(
                        "Invalid manager policy run_agent_step.repo_overrides."
                        f"{raw_key}.runtime_grading_mode: {field_value}"
                    )
                parsed[field_name] = field_value
                continue
        out[key] = parsed
    return out


def normalize_repo_override_key(value: Any) -> str:
    key = str(value or "").strip().lower()
    if not key:
        raise ValueError("Invalid manager policy run_agent_step.repo_overrides key: empty")
    return key


def resolve_run_agent_effective_policy(
    policy: RunAgentPolicy,
    *,
    owner: str,
    repo: str,
) -> dict[str, Any]:
    effective: dict[str, Any] = {
        "codex_sandbox": policy.codex_sandbox,
        "skills_mode": policy.skills_mode,
        "max_agent_seconds": policy.max_agent_seconds,
        "max_changed_files": policy.max_changed_files,
        "max_added_lines": policy.max_added_lines,
        "max_retryable_attempts": policy.max_retryable_attempts,
        "min_test_commands": policy.min_test_commands,
        "runtime_grading_mode": policy.runtime_grading_mode,
        "known_test_failure_allowlist": list(policy.known_test_failure_allowlist),
        "success_event_stream_sample_pct": policy.success_event_stream_sample_pct,
        "success_state": policy.success_state,
        "on_retryable_state": policy.on_retryable_state,
        "on_human_review_state": policy.on_human_review_state,
    }
    candidates = [
        normalize_repo_override_key(f"{owner}/{repo}"),
        normalize_repo_override_key(repo),
    ]
    for key in candidates:
        override = policy.repo_overrides.get(key)
        if not isinstance(override, dict):
            continue
        for field in (
            "max_agent_seconds",
            "max_changed_files",
            "max_added_lines",
            "max_retryable_attempts",
            "min_test_commands",
            "runtime_grading_mode",
            "success_event_stream_sample_pct",
        ):
            if field not in override:
                continue
            if field == "runtime_grading_mode":
                value = str(override[field]).strip()
                if value in {"rules", "hybrid", "hybrid_llm"}:
                    effective[field] = value
                continue
            effective[field] = max(int(override[field]), 0)
        if "known_test_failure_allowlist" in override:
            merged = [
                *list(effective.get("known_test_failure_allowlist") or []),
                *list(override["known_test_failure_allowlist"] or []),
            ]
            effective["known_test_failure_allowlist"] = dedupe_string_list(merged)
        if "skills_mode" in override:
            value = str(override["skills_mode"]).strip()
            if value in {"off", "agentpr", "agentpr_autonomous"}:
                effective["skills_mode"] = value
    return effective


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = dict(base)
    for key, value in override.items():
        base_value = out.get(key)
        if isinstance(base_value, dict) and isinstance(value, dict):
            out[key] = deep_merge(base_value, value)
            continue
        out[key] = value
    return out


def parse_string_list(value: Any, *, field_name: str) -> list[str]:
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError(f"Invalid manager policy {field_name}: expected array")
    out: list[str] = []
    for item in value:
        text = str(item).strip()
        if not text:
            continue
        out.append(text)
    return dedupe_string_list(out)


def dedupe_string_list(values: list[str]) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for item in values:
        text = str(item).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
    return out
