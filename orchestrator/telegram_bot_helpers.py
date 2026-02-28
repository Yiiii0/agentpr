"""Extracted helpers for telegram_bot: constants, config, parsers, utilities."""

from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .manager_llm import ManagerLLMClient, ManagerLLMError
from .models import RunState

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

READ_COMMANDS: set[str] = {
    "/start",
    "/help",
    "/list",
    "/overview",
    "/show",
    "/status",
    "/pending_pr",
}
WRITE_COMMANDS: set[str] = {
    "/create",
    "/pause",
    "/resume",
    "/retry",
}
ADMIN_COMMANDS: set[str] = {
    "/approve_pr",
}
NL_DISPATCH_COMMAND = "/nl"
WRITE_COMMANDS.add(NL_DISPATCH_COMMAND)

RUN_ID_PATTERN = re.compile(r"\b(?:run|baseline|calib|rerun|smoke)_[A-Za-z0-9_.-]+\b")
OWNER_REPO_PATTERN = re.compile(r"\b([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)\b")
GITHUB_REPO_URL_PATTERN = re.compile(
    r"(?:https?://)?(?:www\.)?github\.com/([A-Za-z0-9_.-]+)/([A-Za-z0-9_.-]+)(?:\.git)?(?:/|$)",
    flags=re.IGNORECASE,
)

BOT_RULES_FOOTER = (
    "Rules:\n"
    "1) `/` 开头按命令模式执行（确定性动作）。\n"
    "2) 非 `/` 文本按自然语言模式执行（manager agent 路由）。\n"
    "3) 高风险动作保留显式确认：`/approve_pr <run_id> <confirm_token>`。\n"
    "4) 常用命令：`/create <owner/repo|github_url>... [--prompt-version vX]` "
    "`/overview` "
    "`/list` `/show <run_id>` `/status <run_id>` "
    "`/pause <run_id>` `/resume <run_id> <state>` `/retry <run_id> <state>`。\n"
    "5) 支持的状态值：`EXECUTING` `DISCOVERY` `PLAN_READY` `IMPLEMENTING` "
    "`LOCAL_VALIDATING` `PUSHED` `CI_WAIT` `REVIEW_WAIT` `ITERATING` "
    "`NEEDS_HUMAN_REVIEW` `FAILED` `FAILED_RETRYABLE` `DONE` `SKIPPED` `FAILED_TERMINAL`."
)

REASON_CODE_EXPLANATIONS: dict[str, str] = {
    "runtime_success": "Worker exited cleanly with required test evidence and within diff budget.",
    "runtime_success_recovered_test_failures": (
        "Worker saw intermediate test failures but converged to success in the final attempt."
    ),
    "runtime_success_allowlisted_test_failures": (
        "Observed known baseline failures that matched allowlist and final run converged."
    ),
    "runtime_success_no_test_infra_with_validation": (
        "No repository test infrastructure was detected; semantic grading accepted lint/typecheck validation evidence."
    ),
    "missing_test_evidence": "Required test execution evidence is missing for this state.",
    "insufficient_test_evidence": "Test evidence exists but does not meet configured minimum.",
    "test_command_failed": "At least one test command failed and convergence policy did not recover it.",
    "diff_budget_exceeded": "Changed files/lines exceeded configured minimal-diff budget.",
    "runtime_hard_failure": "Worker failed with non-retryable error signals.",
    "runtime_transient_failure": "Worker failed with retryable transient signals.",
    "runtime_unknown_failure": "Worker failed without clear hard/transient classification.",
}

NL_MODE_RULES = "rules"
NL_MODE_LLM = "llm"
NL_MODE_HYBRID = "hybrid"
NL_MODES = {NL_MODE_RULES, NL_MODE_LLM, NL_MODE_HYBRID}

DECISION_WHY_MODE_OFF = "off"
DECISION_WHY_MODE_HYBRID = "hybrid"
DECISION_WHY_MODE_LLM = "llm"
DECISION_WHY_MODES = {
    DECISION_WHY_MODE_OFF,
    DECISION_WHY_MODE_HYBRID,
    DECISION_WHY_MODE_LLM,
}

BOT_NL_ALLOWED_ACTIONS = [
    "help",
    "create_run",
    "create_runs",
    "status_overview",
    "list_runs",
    "show_run",
    "pause_run",
    "resume_run",
    "retry_run",
    "manager_tick",
]

NOTIFY_TERMINAL_STATES = {
    RunState.PUSHED.value,
    RunState.NEEDS_HUMAN_REVIEW.value,
    RunState.FAILED.value,
    RunState.DONE.value,
}

DEFAULT_TARGET_STATE: str = RunState.EXECUTING.value

# ---------------------------------------------------------------------------
# Config / env resolution
# ---------------------------------------------------------------------------


def parse_positive_int_env(name: str, default: int) -> int:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        return max(int(default), 1)
    try:
        return max(int(value), 1)
    except ValueError:
        return max(int(default), 1)


def parse_bool_env(name: str, default: bool) -> bool:
    raw = str(os.environ.get(name) or "").strip().lower()
    if not raw:
        return bool(default)
    if raw in {"1", "true", "yes", "on"}:
        return True
    if raw in {"0", "false", "no", "off"}:
        return False
    return bool(default)


def resolve_default_prompt_version() -> str:
    raw = str(os.environ.get("AGENTPR_DEFAULT_PROMPT_VERSION") or "").strip()
    return raw or "v1"


def resolve_create_autokick() -> bool:
    raw = str(os.environ.get("AGENTPR_CREATE_AUTOKICK") or "1").strip().lower()
    return raw in {"1", "true", "yes", "on"}


def resolve_telegram_nl_mode() -> str:
    raw = str(os.environ.get("AGENTPR_TELEGRAM_NL_MODE") or NL_MODE_RULES).strip().lower()
    if raw in NL_MODES:
        return raw
    return NL_MODE_RULES


def resolve_decision_why_mode() -> str:
    raw = str(
        os.environ.get("AGENTPR_TELEGRAM_DECISION_WHY_MODE")
        or DECISION_WHY_MODE_HYBRID
    ).strip().lower()
    if raw in DECISION_WHY_MODES:
        return raw
    return DECISION_WHY_MODE_HYBRID


def build_nl_llm_client_if_enabled(*, nl_mode: str) -> ManagerLLMClient | None:
    if nl_mode == NL_MODE_RULES:
        return None
    api_base = str(os.environ.get("AGENTPR_TELEGRAM_NL_API_BASE") or "").strip() or None
    model = str(os.environ.get("AGENTPR_TELEGRAM_NL_MODEL") or "").strip() or None
    api_key_env = str(
        os.environ.get("AGENTPR_TELEGRAM_NL_API_KEY_ENV") or "AGENTPR_MANAGER_API_KEY"
    ).strip() or "AGENTPR_MANAGER_API_KEY"
    timeout_sec = parse_positive_int_env("AGENTPR_TELEGRAM_NL_TIMEOUT_SEC", 20)
    try:
        return ManagerLLMClient.from_runtime(
            api_base=api_base,
            model=model,
            timeout_sec=timeout_sec,
            api_key_env=api_key_env,
        )
    except ManagerLLMError:
        return None


def build_decision_llm_client_if_enabled(
    *,
    decision_why_mode: str,
    fallback_client: ManagerLLMClient | None,
) -> ManagerLLMClient | None:
    if decision_why_mode == DECISION_WHY_MODE_OFF:
        return None
    if fallback_client is not None:
        return fallback_client
    api_base = str(
        os.environ.get("AGENTPR_TELEGRAM_DECISION_API_BASE")
        or os.environ.get("AGENTPR_TELEGRAM_NL_API_BASE")
        or ""
    ).strip() or None
    model = str(
        os.environ.get("AGENTPR_TELEGRAM_DECISION_MODEL")
        or os.environ.get("AGENTPR_TELEGRAM_NL_MODEL")
        or ""
    ).strip() or None
    api_key_env = str(
        os.environ.get("AGENTPR_TELEGRAM_DECISION_API_KEY_ENV")
        or os.environ.get("AGENTPR_TELEGRAM_NL_API_KEY_ENV")
        or "AGENTPR_MANAGER_API_KEY"
    ).strip() or "AGENTPR_MANAGER_API_KEY"
    timeout_sec = parse_positive_int_env("AGENTPR_TELEGRAM_DECISION_TIMEOUT_SEC", 20)
    try:
        return ManagerLLMClient.from_runtime(
            api_base=api_base,
            model=model,
            timeout_sec=timeout_sec,
            api_key_env=api_key_env,
        )
    except ManagerLLMError:
        return None


def resolve_notification_chat_ids(
    *,
    allowed_chat_ids: set[int],
    write_chat_ids: set[int],
    admin_chat_ids: set[int],
) -> list[int]:
    if admin_chat_ids:
        return sorted(admin_chat_ids)
    if write_chat_ids:
        return sorted(write_chat_ids)
    return sorted(allowed_chat_ids)


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------


def get_last_run_id(conversation_state: dict[str, Any]) -> str | None:
    value = conversation_state.get("last_run_id")
    if not isinstance(value, str):
        return None
    run_id = value.strip()
    if not run_id:
        return None
    return run_id


def set_last_run_id(conversation_state: dict[str, Any], run_id: str | None) -> None:
    if not run_id:
        return
    conversation_state["last_run_id"] = str(run_id).strip()
    conversation_state["updated_at"] = datetime.now(UTC).isoformat()


def sync_last_run_id_from_text(conversation_state: dict[str, Any], text: str) -> None:
    set_last_run_id(conversation_state, extract_run_id_from_text(text))


# ---------------------------------------------------------------------------
# Parsers
# ---------------------------------------------------------------------------


def extract_run_id_from_text(text: str) -> str | None:
    match = RUN_ID_PATTERN.search(str(text))
    if match is None:
        return None
    return str(match.group(0))


def extract_repo_refs_text(text: str) -> list[str]:
    raw_text = str(text)
    found: list[str] = []
    seen: set[str] = set()
    for token in re.split(r"[\s,;]+", raw_text):
        normalized = str(token).strip().strip("()[]{}<>\"'`")
        if not normalized:
            continue
        parsed = parse_repo_ref(normalized)
        if parsed is None:
            continue
        owner, repo = parsed
        repo_ref = f"{owner}/{repo}"
        if repo_ref in seen:
            continue
        seen.add(repo_ref)
        found.append(repo_ref)
    return found


def extract_repo_ref_text(text: str) -> str | None:
    refs = extract_repo_refs_text(text)
    if not refs:
        return None
    return refs[0]


def parse_repo_ref(value: str) -> tuple[str, str] | None:
    raw = str(value).strip()
    if not raw:
        return None
    match = GITHUB_REPO_URL_PATTERN.fullmatch(raw)
    if match is not None:
        owner = str(match.group(1)).strip()
        repo = str(match.group(2)).strip()
        return (owner, repo) if owner and repo else None
    if raw.startswith("git@github.com:"):
        payload = raw.split(":", 1)[1].strip()
        if payload.endswith(".git"):
            payload = payload[:-4]
        if "/" in payload:
            owner, repo = payload.split("/", 1)
            owner = owner.strip()
            repo = repo.strip()
            if owner and repo:
                return owner, repo
    if "/" not in raw:
        return None
    owner, repo = raw.split("/", 1)
    owner = owner.strip()
    repo = repo.strip()
    if repo.endswith(".git"):
        repo = repo[:-4].strip()
    if not owner or not repo:
        return None
    return owner, repo


def extract_prompt_version_from_text(text: str) -> str | None:
    value = str(text)
    match = re.search(r"(?:prompt[_\s-]*version|版本)\s*[:=]?\s*([A-Za-z0-9_.-]+)", value, flags=re.IGNORECASE)
    if match is None:
        return None
    parsed = str(match.group(1)).strip()
    return parsed or None


def extract_target_state_from_text(text: str) -> str | None:
    raw = str(text).upper()
    for state in RunState:
        if state.value in raw:
            return state.value
    zh_map = {
        "执行": RunState.EXECUTING.value,
        "发现": RunState.DISCOVERY.value,
        "计划": RunState.PLAN_READY.value,
        "实现": RunState.IMPLEMENTING.value,
        "本地验证": RunState.LOCAL_VALIDATING.value,
        "推送": RunState.PUSHED.value,
        "等待CI": RunState.CI_WAIT.value,
        "等待REVIEW": RunState.REVIEW_WAIT.value,
        "迭代": RunState.ITERATING.value,
        "人工": RunState.NEEDS_HUMAN_REVIEW.value,
        "失败": RunState.FAILED.value,
        "重试失败": RunState.FAILED_RETRYABLE.value,
        "完成": RunState.DONE.value,
        "跳过": RunState.SKIPPED.value,
        "终止": RunState.FAILED_TERMINAL.value,
    }
    lowered = str(text).replace(" ", "").lower()
    for key, state in zh_map.items():
        if key.lower() in lowered:
            return state
    return None


def normalize_target_state(target_state: str | None, *, default: str) -> str:
    if isinstance(target_state, str):
        normalized = target_state.strip().upper()
        if normalized in {state.value for state in RunState}:
            return normalized
    return default


# ---------------------------------------------------------------------------
# Format / text utilities
# ---------------------------------------------------------------------------


def format_bot_response(message: str) -> str:
    body = str(message).strip() or "(empty response)"
    return f"{body}\n\n---\n{BOT_RULES_FOOTER}"


def truncate_text(value: str, max_len: int) -> str:
    text = str(value)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def clamp_str(value: str, *, max_len: int) -> str:
    text = str(value).strip()
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


def contains_any(text: str, needles: list[str]) -> bool:
    value = str(text).lower()
    return any(item.lower() in value for item in needles if item)


def try_parse_json(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None


# ---------------------------------------------------------------------------
# Command access / auth
# ---------------------------------------------------------------------------


def parse_command_name(text: str) -> str | None:
    if not text.startswith("/"):
        return None
    try:
        parts = shlex.split(text)
    except ValueError:
        return None
    if not parts:
        return None
    return parts[0].split("@", 1)[0].lower()


def command_access_level(command: str) -> str:
    if command in ADMIN_COMMANDS:
        return "admin"
    if command in WRITE_COMMANDS:
        return "write"
    if command in READ_COMMANDS:
        return "read"
    return "unknown"


def authorize_command(
    *,
    chat_id: int,
    command: str,
    allow_chat: bool,
    write_chat_ids: set[int],
    admin_chat_ids: set[int],
) -> tuple[bool, str | None]:
    if not allow_chat:
        return False, "chat_not_allowlisted"
    level = command_access_level(command)
    if level == "read":
        return True, None
    if level == "write":
        if allow_chat and not write_chat_ids and not admin_chat_ids:
            return True, None
        if chat_id in admin_chat_ids or chat_id in write_chat_ids:
            return True, None
        return False, "write_permission_required"
    if level == "admin":
        if allow_chat and not admin_chat_ids and not write_chat_ids:
            return True, None
        if chat_id in admin_chat_ids:
            return True, None
        return False, "admin_permission_required"
    return True, None


# ---------------------------------------------------------------------------
# CLI execution
# ---------------------------------------------------------------------------


def run_cli_command(
    argv: list[str],
    *,
    db_path: Path,
    workspace_root: Path,
    integration_root: Path,
    project_root: Path,
) -> dict[str, Any]:
    cmd = [
        sys.executable,
        "-m",
        "orchestrator.cli",
        "--db",
        str(db_path),
        "--workspace-root",
        str(workspace_root),
        "--integration-root",
        str(integration_root),
        *argv,
    ]
    completed = subprocess.run(  # noqa: S603
        cmd,
        cwd=project_root,
        text=True,
        capture_output=True,
        check=False,
    )
    text = completed.stdout.strip() or completed.stderr.strip() or "(no output)"
    payload = try_parse_json(text)
    if payload is not None:
        return {
            "ok": completed.returncode == 0,
            "payload": payload,
            "text": json.dumps(payload, ensure_ascii=True),
        }
    return {"ok": completed.returncode == 0, "payload": None, "text": text}


def run_and_render_action(
    argv: list[str],
    *,
    db_path: Path,
    workspace_root: Path,
    integration_root: Path,
    project_root: Path,
) -> str:
    result = run_cli_command(
        argv,
        db_path=db_path,
        workspace_root=workspace_root,
        integration_root=integration_root,
        project_root=project_root,
    )
    if result["ok"]:
        return result["text"]
    return f"Failed: {result['text']}"


def build_audit_entry(
    *,
    update_id: int,
    chat_id: int,
    command: str,
    text: str,
    outcome: str,
    detail: str,
    response: str,
) -> dict[str, Any]:
    return {
        "ts": datetime.now(UTC).isoformat(),
        "update_id": int(update_id),
        "chat_id": int(chat_id),
        "command": command,
        "outcome": outcome,
        "detail": detail,
        "request_text": truncate_text(text, 400),
        "response_text": truncate_text(response, 400),
    }


def safe_send_message(*, client: Any, chat_id: int, text: str) -> bool:
    try:
        client.send_message(chat_id=chat_id, text=text)
    except Exception:  # noqa: BLE001
        return False
    return True
