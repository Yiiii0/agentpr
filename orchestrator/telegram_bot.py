from __future__ import annotations

import json
import os
import re
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from collections import deque
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .manager_llm import BotLLMSelection, ManagerLLMClient, ManagerLLMError
from .models import RunState
from .service import OrchestratorService

READ_COMMANDS: set[str] = {
    "/start",
    "/help",
    "/list",
    "/show",
    "/status",
    "/pending_pr",
}
WRITE_COMMANDS: set[str] = {
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

BOT_RULES_FOOTER = (
    "Rules:\n"
    "1) `/` 开头按命令模式执行（确定性动作）。\n"
    "2) 非 `/` 文本按自然语言模式执行（manager agent 路由）。\n"
    "3) 高风险动作保留显式确认：`/approve_pr <run_id> <confirm_token>`。\n"
    "4) 常用命令：`/list` `/show <run_id>` `/status <run_id>` "
    "`/pause <run_id>` `/resume <run_id> <state>` `/retry <run_id> <state>`。\n"
    "5) 支持的状态值：`DISCOVERY` `PLAN_READY` `IMPLEMENTING` "
    "`LOCAL_VALIDATING` `PUSHED` `CI_WAIT` `REVIEW_WAIT` `ITERATING` "
    "`NEEDS_HUMAN_REVIEW` `FAILED_RETRYABLE` `DONE` `SKIPPED` `FAILED_TERMINAL`."
)

NL_MODE_RULES = "rules"
NL_MODE_LLM = "llm"
NL_MODE_HYBRID = "hybrid"
NL_MODES = {NL_MODE_RULES, NL_MODE_LLM, NL_MODE_HYBRID}

BOT_NL_ALLOWED_ACTIONS = [
    "help",
    "list_runs",
    "show_run",
    "pause_run",
    "resume_run",
    "retry_run",
    "manager_tick",
]


def format_bot_response(message: str) -> str:
    body = str(message).strip() or "(empty response)"
    return f"{body}\n\n---\n{BOT_RULES_FOOTER}"


def resolve_telegram_nl_mode() -> str:
    raw = str(os.environ.get("AGENTPR_TELEGRAM_NL_MODE") or NL_MODE_RULES).strip().lower()
    if raw in NL_MODES:
        return raw
    return NL_MODE_RULES


def parse_positive_int_env(name: str, default: int) -> int:
    value = str(os.environ.get(name) or "").strip()
    if not value:
        return max(int(default), 1)
    try:
        return max(int(value), 1)
    except ValueError:
        return max(int(default), 1)


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


class TelegramApiError(RuntimeError):
    pass


class TelegramClient:
    def __init__(self, token: str) -> None:
        self.base_url = f"https://api.telegram.org/bot{token}"

    def get_updates(self, *, offset: int | None, timeout_sec: int) -> list[dict[str, Any]]:
        payload: dict[str, Any] = {"timeout": timeout_sec}
        if offset is not None:
            payload["offset"] = offset
        data = self._call("getUpdates", payload)
        if not isinstance(data, list):
            return []
        return [item for item in data if isinstance(item, dict)]

    def send_message(self, *, chat_id: int, text: str) -> None:
        self._call(
            "sendMessage",
            {
                "chat_id": chat_id,
                "text": text,
                "disable_web_page_preview": True,
            },
        )

    def _call(self, method: str, payload: dict[str, Any]) -> Any:
        url = f"{self.base_url}/{method}"
        request = urllib.request.Request(
            url=url,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(request, timeout=45) as response:
                body = response.read().decode("utf-8")
        except urllib.error.URLError as exc:
            raise TelegramApiError(f"Telegram API request failed: {exc}") from exc
        try:
            payload_json = json.loads(body)
        except json.JSONDecodeError as exc:
            raise TelegramApiError(f"Invalid Telegram API response: {body[:200]}") from exc
        if not payload_json.get("ok", False):
            raise TelegramApiError(f"Telegram API error: {payload_json}")
        return payload_json.get("result")


class CommandRateLimiter:
    def __init__(
        self,
        *,
        window_sec: int,
        per_chat_limit: int,
        global_limit: int,
    ) -> None:
        self.window_sec = max(int(window_sec), 1)
        self.per_chat_limit = max(int(per_chat_limit), 1)
        self.global_limit = max(int(global_limit), 1)
        self._global: deque[float] = deque()
        self._per_chat: dict[int, deque[float]] = {}

    def allow(self, *, chat_id: int, now_ts: float) -> tuple[bool, str | None]:
        self._evict(now_ts=now_ts)
        if len(self._global) >= self.global_limit:
            return False, "global_rate_limited"
        chat_queue = self._per_chat.setdefault(chat_id, deque())
        if len(chat_queue) >= self.per_chat_limit:
            return False, "chat_rate_limited"
        self._global.append(now_ts)
        chat_queue.append(now_ts)
        return True, None

    def _evict(self, *, now_ts: float) -> None:
        cutoff = now_ts - float(self.window_sec)
        while self._global and self._global[0] < cutoff:
            self._global.popleft()
        stale_chat_ids: list[int] = []
        for chat_id, queue in self._per_chat.items():
            while queue and queue[0] < cutoff:
                queue.popleft()
            if not queue:
                stale_chat_ids.append(chat_id)
        for chat_id in stale_chat_ids:
            self._per_chat.pop(chat_id, None)


class TelegramAuditLogger:
    def __init__(self, path: Path | None) -> None:
        self.path = path
        if self.path is not None:
            self.path.parent.mkdir(parents=True, exist_ok=True)

    def append(self, payload: dict[str, Any]) -> None:
        if self.path is None:
            return
        line = json.dumps(payload, ensure_ascii=True, sort_keys=True)
        with self.path.open("a", encoding="utf-8") as fh:
            fh.write(line)
            fh.write("\n")


def run_telegram_bot_loop(
    *,
    client: TelegramClient,
    service: OrchestratorService,
    db_path: Path,
    workspace_root: Path,
    integration_root: Path,
    project_root: Path,
    allowed_chat_ids: set[int],
    write_chat_ids: set[int],
    admin_chat_ids: set[int],
    poll_timeout_sec: int,
    idle_sleep_sec: int,
    list_limit: int,
    rate_limit_window_sec: int,
    rate_limit_per_chat: int,
    rate_limit_global: int,
    audit_log_file: Path | None,
) -> None:
    offset: int | None = None
    limiter = CommandRateLimiter(
        window_sec=rate_limit_window_sec,
        per_chat_limit=rate_limit_per_chat,
        global_limit=rate_limit_global,
    )
    audit = TelegramAuditLogger(audit_log_file)
    conversation_state: dict[int, dict[str, Any]] = {}
    nl_mode = resolve_telegram_nl_mode()
    llm_client = build_nl_llm_client_if_enabled(nl_mode=nl_mode)

    while True:
        try:
            updates = client.get_updates(offset=offset, timeout_sec=poll_timeout_sec)
        except TelegramApiError as exc:
            audit.append(
                {
                    "ts": datetime.now(UTC).isoformat(),
                    "kind": "poll_error",
                    "error": str(exc),
                }
            )
            time.sleep(max(idle_sleep_sec, 1))
            continue

        if not updates:
            time.sleep(max(idle_sleep_sec, 1))
            continue

        for update in updates:
            update_id = int(update.get("update_id", 0))
            offset = update_id + 1
            message = update.get("message") or update.get("edited_message")
            if not isinstance(message, dict):
                continue
            text = str(message.get("text", "")).strip()
            if not text:
                continue
            chat = message.get("chat")
            if not isinstance(chat, dict) or "id" not in chat:
                continue
            chat_id = int(chat["id"])
            chat_ctx = conversation_state.setdefault(chat_id, {})
            command = parse_command_name(text)
            is_natural_language = command is None
            normalized_command = command or NL_DISPATCH_COMMAND

            now_ts = time.monotonic()
            allow_chat = not allowed_chat_ids or chat_id in allowed_chat_ids
            allowed, reason = authorize_command(
                chat_id=chat_id,
                command=normalized_command,
                allow_chat=allow_chat,
                write_chat_ids=write_chat_ids,
                admin_chat_ids=admin_chat_ids,
            )
            if not allowed:
                response = format_bot_response("Unauthorized command.")
                safe_send_message(client=client, chat_id=chat_id, text=response)
                audit.append(
                    build_audit_entry(
                        update_id=update_id,
                        chat_id=chat_id,
                        command=normalized_command,
                        text=text,
                        outcome="unauthorized",
                        detail=reason or "",
                        response=response,
                    )
                )
                continue

            ok, rate_reason = limiter.allow(chat_id=chat_id, now_ts=now_ts)
            if not ok:
                base_response = (
                    "Rate limited. Please retry later."
                    if rate_reason == "chat_rate_limited"
                    else "System busy. Please retry later."
                )
                response = format_bot_response(base_response)
                safe_send_message(client=client, chat_id=chat_id, text=response)
                audit.append(
                    build_audit_entry(
                        update_id=update_id,
                        chat_id=chat_id,
                        command=normalized_command,
                        text=text,
                        outcome="rate_limited",
                        detail=rate_reason or "",
                        response=response,
                    )
                )
                continue

            try:
                if is_natural_language:
                    response = handle_natural_language(
                        text=text,
                        service=service,
                        db_path=db_path,
                        workspace_root=workspace_root,
                        integration_root=integration_root,
                        project_root=project_root,
                        list_limit=list_limit,
                        conversation_state=chat_ctx,
                        llm_client=llm_client,
                        nl_mode=nl_mode,
                    )
                else:
                    response = handle_bot_command(
                        text=text,
                        service=service,
                        db_path=db_path,
                        workspace_root=workspace_root,
                        integration_root=integration_root,
                        project_root=project_root,
                        list_limit=list_limit,
                    )
                    sync_last_run_id_from_text(chat_ctx, text)
                response = format_bot_response(response)
                outcome = "ok"
                detail = "nl" if is_natural_language else "command"
            except Exception as exc:  # noqa: BLE001
                response = format_bot_response(f"Command failed: {exc}")
                outcome = "error"
                detail = str(exc)
            safe_send_message(client=client, chat_id=chat_id, text=response)
            audit.append(
                build_audit_entry(
                    update_id=update_id,
                    chat_id=chat_id,
                    command=normalized_command,
                    text=text,
                    outcome=outcome,
                    detail=detail,
                    response=response,
                )
            )


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


def truncate_text(value: str, max_len: int) -> str:
    text = str(value)
    if len(text) <= max_len:
        return text
    return text[: max_len - 3] + "..."


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


def safe_send_message(*, client: TelegramClient, chat_id: int, text: str) -> bool:
    try:
        client.send_message(chat_id=chat_id, text=text)
    except TelegramApiError:
        return False
    return True


def handle_bot_command(
    *,
    text: str,
    service: OrchestratorService,
    db_path: Path,
    workspace_root: Path,
    integration_root: Path,
    project_root: Path,
    list_limit: int,
) -> str:
    try:
        parts = shlex.split(text)
    except ValueError:
        return "Invalid command format."
    if not parts:
        return "Empty command."

    command = parts[0].split("@", 1)[0].lower()
    args = parts[1:]

    if command in {"/start", "/help"}:
        return (
            "Commands:\n"
            "/list [N]\n"
            "/show <run_id>\n"
            "/status <run_id>\n"
            "/pending_pr [N]\n"
            "/approve_pr <run_id> <confirm_token>\n"
            "/pause <run_id>\n"
            "/resume <run_id> <target_state>\n"
            "/retry <run_id> <target_state>"
        )

    if command == "/list":
        limit = list_limit
        if args:
            try:
                limit = max(1, min(int(args[0]), 50))
            except ValueError:
                return "Usage: /list [N]"
        runs = service.list_runs(limit=limit)
        if not runs:
            return "No runs."
        lines = ["Latest runs:"]
        for row in runs:
            lines.append(
                f"{row['run_id']} | {row['repo']} | {row['current_state']}"
            )
        return "\n".join(lines)

    if command in {"/show", "/status"}:
        if len(args) != 1:
            return f"Usage: {command} <run_id>"
        run_id = args[0]
        try:
            snapshot = service.get_run_snapshot(run_id)
        except KeyError:
            return f"Run not found: {run_id}"
        run = snapshot["run"]
        return (
            f"run_id: {run['run_id']}\n"
            f"repo: {run['owner']}/{run['repo']}\n"
            f"state: {snapshot['state']}\n"
            f"pr_number: {run.get('pr_number')}\n"
            f"workspace: {run['workspace_dir']}"
        )

    if command == "/pending_pr":
        limit = list_limit
        if args:
            try:
                limit = max(1, min(int(args[0]), 50))
            except ValueError:
                return "Usage: /pending_pr [N]"
        runs = service.list_runs(limit=200)
        lines: list[str] = []
        for row in runs:
            if row.get("current_state") != RunState.PUSHED.value:
                continue
            run_id = str(row["run_id"])
            artifact = service.latest_artifact(run_id, artifact_type="pr_open_request")
            if artifact is None:
                continue
            expires_at = artifact["metadata"].get("expires_at", "?")
            lines.append(f"{run_id} | {row['repo']} | expires={expires_at}")
            if len(lines) >= limit:
                break
        if not lines:
            return "No pending PR approval requests."
        return "Pending PR requests:\n" + "\n".join(lines)

    if command == "/approve_pr":
        if len(args) != 2:
            return "Usage: /approve_pr <run_id> <confirm_token>"
        run_id = args[0]
        confirm_token = args[1]
        artifact = service.latest_artifact(run_id, artifact_type="pr_open_request")
        if artifact is None:
            return f"No pr_open_request found for run: {run_id}"
        request_file = artifact["uri"]
        result = run_cli_command(
            [
                "approve-open-pr",
                "--run-id",
                run_id,
                "--request-file",
                request_file,
                "--confirm-token",
                confirm_token,
                "--confirm",
            ],
            db_path=db_path,
            workspace_root=workspace_root,
            integration_root=integration_root,
            project_root=project_root,
        )
        if not result["ok"]:
            return f"approve-open-pr failed: {result['text']}"
        return f"approve-open-pr done: {result['text']}"

    if command == "/pause":
        if len(args) != 1:
            return "Usage: /pause <run_id>"
        return run_and_render_action(
            ["pause", "--run-id", args[0]],
            db_path=db_path,
            workspace_root=workspace_root,
            integration_root=integration_root,
            project_root=project_root,
        )

    if command == "/resume":
        if len(args) != 2:
            return "Usage: /resume <run_id> <target_state>"
        return run_and_render_action(
            ["resume", "--run-id", args[0], "--target-state", args[1]],
            db_path=db_path,
            workspace_root=workspace_root,
            integration_root=integration_root,
            project_root=project_root,
        )

    if command == "/retry":
        if len(args) != 2:
            return "Usage: /retry <run_id> <target_state>"
        return run_and_render_action(
            ["retry", "--run-id", args[0], "--target-state", args[1]],
            db_path=db_path,
            workspace_root=workspace_root,
            integration_root=integration_root,
            project_root=project_root,
        )

    return "Unknown command. Use /help."


def handle_natural_language(
    *,
    text: str,
    service: OrchestratorService,
    db_path: Path,
    workspace_root: Path,
    integration_root: Path,
    project_root: Path,
    list_limit: int,
    conversation_state: dict[str, Any],
    llm_client: ManagerLLMClient | None,
    nl_mode: str,
) -> str:
    mode = str(nl_mode).strip().lower()
    if mode not in NL_MODES:
        mode = NL_MODE_RULES
    if mode == NL_MODE_RULES:
        return handle_natural_language_rules(
            text=text,
            service=service,
            db_path=db_path,
            workspace_root=workspace_root,
            integration_root=integration_root,
            project_root=project_root,
            list_limit=list_limit,
            conversation_state=conversation_state,
        )
    if llm_client is None:
        if mode == NL_MODE_LLM:
            return (
                "NL LLM router unavailable. "
                "Set AGENTPR_MANAGER_API_KEY (or AGENTPR_TELEGRAM_NL_API_KEY_ENV target) "
                "and optionally AGENTPR_TELEGRAM_NL_MODEL/AGENTPR_TELEGRAM_NL_API_BASE."
            )
        return handle_natural_language_rules(
            text=text,
            service=service,
            db_path=db_path,
            workspace_root=workspace_root,
            integration_root=integration_root,
            project_root=project_root,
            list_limit=list_limit,
            conversation_state=conversation_state,
        )

    normalized = str(text).strip()
    if not normalized:
        return "Empty message."
    explicit_run_id = extract_run_id_from_text(normalized)
    if explicit_run_id:
        set_last_run_id(conversation_state, explicit_run_id)
    llm_context = {
        "last_run_id": get_last_run_id(conversation_state),
        "explicit_run_id": explicit_run_id,
        "states": [state.value for state in RunState],
        "recent_runs": service.list_runs(limit=min(max(int(list_limit), 1), 8)),
        "commands": list(BOT_NL_ALLOWED_ACTIONS),
        "nl_mode": mode,
    }
    try:
        selection = llm_client.decide_bot_action(
            user_text=normalized,
            context=llm_context,
            allowed_actions=BOT_NL_ALLOWED_ACTIONS,
        )
    except ManagerLLMError as exc:
        if mode == NL_MODE_HYBRID:
            return handle_natural_language_rules(
                text=text,
                service=service,
                db_path=db_path,
                workspace_root=workspace_root,
                integration_root=integration_root,
                project_root=project_root,
                list_limit=list_limit,
                conversation_state=conversation_state,
            )
        return f"manager nl routing failed: {exc}"

    if selection.action not in BOT_NL_ALLOWED_ACTIONS:
        if mode == NL_MODE_HYBRID:
            return handle_natural_language_rules(
                text=text,
                service=service,
                db_path=db_path,
                workspace_root=workspace_root,
                integration_root=integration_root,
                project_root=project_root,
                list_limit=list_limit,
                conversation_state=conversation_state,
            )
        return f"manager returned unsupported action: {selection.action}"

    result = execute_nl_selection(
        selection=selection,
        explicit_run_id=explicit_run_id,
        text=normalized,
        service=service,
        db_path=db_path,
        workspace_root=workspace_root,
        integration_root=integration_root,
        project_root=project_root,
        list_limit=list_limit,
        conversation_state=conversation_state,
    )
    return f"[manager:{selection.action}] {result}"


def handle_natural_language_rules(
    *,
    text: str,
    service: OrchestratorService,
    db_path: Path,
    workspace_root: Path,
    integration_root: Path,
    project_root: Path,
    list_limit: int,
    conversation_state: dict[str, Any],
) -> str:
    normalized = str(text).strip()
    if not normalized:
        return "Empty message."
    lowered = normalized.lower()
    explicit_run_id = extract_run_id_from_text(normalized)
    if explicit_run_id:
        set_last_run_id(conversation_state, explicit_run_id)
    run_id = explicit_run_id or get_last_run_id(conversation_state)

    if contains_any(lowered, ["help", "规则", "命令", "commands"]):
        return "自然语言模式已启用。你可以直接描述需求，manager 会路由到对应动作。"

    if contains_any(lowered, ["list", "all runs", "运行列表", "全部运行", "列出"]):
        limit = max(1, min(int(list_limit), 50))
        return handle_bot_command(
            text=f"/list {limit}",
            service=service,
            db_path=db_path,
            workspace_root=workspace_root,
            integration_root=integration_root,
            project_root=project_root,
            list_limit=list_limit,
        )

    if run_id and contains_any(
        lowered,
        ["status", "show", "state", "状态", "进展", "情况"],
    ):
        return handle_bot_command(
            text=f"/show {run_id}",
            service=service,
            db_path=db_path,
            workspace_root=workspace_root,
            integration_root=integration_root,
            project_root=project_root,
            list_limit=list_limit,
        )

    if run_id and contains_any(lowered, ["pause", "暂停"]):
        return handle_bot_command(
            text=f"/pause {run_id}",
            service=service,
            db_path=db_path,
            workspace_root=workspace_root,
            integration_root=integration_root,
            project_root=project_root,
            list_limit=list_limit,
        )

    if run_id and contains_any(lowered, ["resume", "恢复", "继续"]):
        target = normalize_target_state(
            extract_target_state_from_text(normalized),
            default=RunState.IMPLEMENTING.value,
        )
        return handle_bot_command(
            text=f"/resume {run_id} {target}",
            service=service,
            db_path=db_path,
            workspace_root=workspace_root,
            integration_root=integration_root,
            project_root=project_root,
            list_limit=list_limit,
        )

    if run_id and contains_any(lowered, ["retry", "重试"]):
        target = normalize_target_state(
            extract_target_state_from_text(normalized),
            default=RunState.IMPLEMENTING.value,
        )
        return handle_bot_command(
            text=f"/retry {run_id} {target}",
            service=service,
            db_path=db_path,
            workspace_root=workspace_root,
            integration_root=integration_root,
            project_root=project_root,
            list_limit=list_limit,
        )

    if contains_any(lowered, ["manager tick", "推进", "继续跑", "next step", "run tick"]):
        argv = ["manager-tick", "--limit", str(max(1, min(int(list_limit), 50)))]
        if run_id:
            argv.extend(["--run-id", run_id])
        result = run_cli_command(
            argv,
            db_path=db_path,
            workspace_root=workspace_root,
            integration_root=integration_root,
            project_root=project_root,
        )
        if result["ok"]:
            return f"manager-tick done: {result['text']}"
        return f"manager-tick failed: {result['text']}"

    if run_id and contains_any(lowered, ["approve", "批准", "通过pr", "创建 pr", "open pr"]):
        return (
            "出于安全原因，PR 批准仍需显式命令："
            "/approve_pr <run_id> <confirm_token>"
        )

    if run_id:
        return (
            f"收到自然语言请求，已识别 run_id={run_id}。"
            "如果你要执行动作，请说：暂停/恢复/重试/状态/推进。"
        )
    return (
        "收到自然语言请求，但未识别可执行意图。"
        "你可以说：'列出运行'、'查看 <run_id> 状态'、'暂停 <run_id>'、"
        "'恢复 <run_id> 到 IMPLEMENTING'、'重试 <run_id>'、'推进一次'。"
    )


def execute_nl_selection(
    *,
    selection: BotLLMSelection,
    explicit_run_id: str | None,
    text: str,
    service: OrchestratorService,
    db_path: Path,
    workspace_root: Path,
    integration_root: Path,
    project_root: Path,
    list_limit: int,
    conversation_state: dict[str, Any],
) -> str:
    action = selection.action
    run_id = selection.run_id or explicit_run_id or get_last_run_id(conversation_state)
    if run_id:
        set_last_run_id(conversation_state, run_id)
    if action == "help":
        return "自然语言模式已启用。直接说需求，或继续使用 /commands。"
    if action == "list_runs":
        limit = max(1, min(int(selection.limit or list_limit), 50))
        return handle_bot_command(
            text=f"/list {limit}",
            service=service,
            db_path=db_path,
            workspace_root=workspace_root,
            integration_root=integration_root,
            project_root=project_root,
            list_limit=list_limit,
        )
    if action == "show_run":
        if not run_id:
            return "缺少 run_id。请在消息中包含 run_id，或先执行 /list。"
        return handle_bot_command(
            text=f"/show {run_id}",
            service=service,
            db_path=db_path,
            workspace_root=workspace_root,
            integration_root=integration_root,
            project_root=project_root,
            list_limit=list_limit,
        )
    if action == "pause_run":
        if not run_id:
            return "缺少 run_id。请在消息中包含 run_id。"
        return handle_bot_command(
            text=f"/pause {run_id}",
            service=service,
            db_path=db_path,
            workspace_root=workspace_root,
            integration_root=integration_root,
            project_root=project_root,
            list_limit=list_limit,
        )
    if action == "resume_run":
        if not run_id:
            return "缺少 run_id。请在消息中包含 run_id。"
        target = normalize_target_state(
            selection.target_state or extract_target_state_from_text(text),
            default=RunState.IMPLEMENTING.value,
        )
        return handle_bot_command(
            text=f"/resume {run_id} {target}",
            service=service,
            db_path=db_path,
            workspace_root=workspace_root,
            integration_root=integration_root,
            project_root=project_root,
            list_limit=list_limit,
        )
    if action == "retry_run":
        if not run_id:
            return "缺少 run_id。请在消息中包含 run_id。"
        target = normalize_target_state(
            selection.target_state or extract_target_state_from_text(text),
            default=RunState.IMPLEMENTING.value,
        )
        return handle_bot_command(
            text=f"/retry {run_id} {target}",
            service=service,
            db_path=db_path,
            workspace_root=workspace_root,
            integration_root=integration_root,
            project_root=project_root,
            list_limit=list_limit,
        )
    if action == "manager_tick":
        argv = ["manager-tick", "--limit", str(max(1, min(int(list_limit), 50)))]
        if run_id:
            argv.extend(["--run-id", run_id])
        result = run_cli_command(
            argv,
            db_path=db_path,
            workspace_root=workspace_root,
            integration_root=integration_root,
            project_root=project_root,
        )
        if result["ok"]:
            return f"manager-tick done: {result['text']}"
        return f"manager-tick failed: {result['text']}"
    return f"unsupported action: {action}"


def normalize_target_state(target_state: str | None, *, default: str) -> str:
    if isinstance(target_state, str):
        normalized = target_state.strip().upper()
        if normalized in {state.value for state in RunState}:
            return normalized
    return default


def contains_any(text: str, needles: list[str]) -> bool:
    value = str(text).lower()
    return any(item.lower() in value for item in needles if item)


def extract_run_id_from_text(text: str) -> str | None:
    match = RUN_ID_PATTERN.search(str(text))
    if match is None:
        return None
    return str(match.group(0))


def extract_target_state_from_text(text: str) -> str | None:
    raw = str(text).upper()
    for state in RunState:
        if state.value in raw:
            return state.value
    zh_map = {
        "发现": RunState.DISCOVERY.value,
        "计划": RunState.PLAN_READY.value,
        "实现": RunState.IMPLEMENTING.value,
        "本地验证": RunState.LOCAL_VALIDATING.value,
        "推送": RunState.PUSHED.value,
        "等待CI": RunState.CI_WAIT.value,
        "等待REVIEW": RunState.REVIEW_WAIT.value,
        "迭代": RunState.ITERATING.value,
        "人工": RunState.NEEDS_HUMAN_REVIEW.value,
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


def try_parse_json(text: str) -> dict[str, Any] | None:
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return None
    if isinstance(parsed, dict):
        return parsed
    return None
