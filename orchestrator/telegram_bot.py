from __future__ import annotations

import json
import shlex
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
from .telegram_bot_helpers import (
    BOT_NL_ALLOWED_ACTIONS,
    DECISION_WHY_MODE_HYBRID,
    DECISION_WHY_MODE_OFF,
    DECISION_WHY_MODES,
    DEFAULT_TARGET_STATE,
    NL_MODE_HYBRID,
    NL_MODE_LLM,
    NL_MODE_RULES,
    NL_MODES,
    NOTIFY_TERMINAL_STATES,
    REASON_CODE_EXPLANATIONS,
    authorize_command,
    build_audit_entry,
    build_decision_llm_client_if_enabled,
    build_nl_llm_client_if_enabled,
    clamp_str,
    contains_any,
    extract_prompt_version_from_text,
    extract_repo_ref_text,
    extract_repo_refs_text,
    extract_run_id_from_text,
    extract_target_state_from_text,
    format_bot_response,
    get_last_run_id,
    normalize_target_state,
    parse_bool_env,
    parse_command_name,
    parse_positive_int_env,
    parse_repo_ref,
    resolve_create_autokick,
    resolve_decision_why_mode,
    resolve_default_prompt_version,
    resolve_notification_chat_ids,
    resolve_telegram_nl_mode,
    run_and_render_action,
    run_cli_command,
    safe_send_message,
    set_last_run_id,
    sync_last_run_id_from_text,
)


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
    decision_why_mode = resolve_decision_why_mode()
    decision_llm_client = build_decision_llm_client_if_enabled(
        decision_why_mode=decision_why_mode,
        fallback_client=llm_client,
    )
    notify_enabled = parse_bool_env("AGENTPR_TELEGRAM_NOTIFY_ENABLED", True)
    notify_scan_sec = parse_positive_int_env("AGENTPR_TELEGRAM_NOTIFY_SCAN_SEC", 30)
    notify_scan_limit = parse_positive_int_env("AGENTPR_TELEGRAM_NOTIFY_SCAN_LIMIT", 200)
    notification_chat_ids = resolve_notification_chat_ids(
        allowed_chat_ids=allowed_chat_ids,
        write_chat_ids=write_chat_ids,
        admin_chat_ids=admin_chat_ids,
    )
    last_notify_scan_ts = 0.0

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

        now_scan_ts = time.monotonic()
        if notify_enabled and (now_scan_ts - last_notify_scan_ts) >= float(notify_scan_sec):
            maybe_emit_state_notifications(
                client=client,
                service=service,
                notification_chat_ids=notification_chat_ids,
                scan_limit=notify_scan_limit,
                audit=audit,
            )
            maybe_emit_manager_notifications(
                client=client,
                service=service,
                notification_chat_ids=notification_chat_ids,
                scan_limit=notify_scan_limit,
                audit=audit,
            )
            last_notify_scan_ts = now_scan_ts

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
                        decision_llm_client=decision_llm_client,
                        decision_why_mode=decision_why_mode,
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
                        decision_llm_client=decision_llm_client,
                        decision_why_mode=decision_why_mode,
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


def load_notification_markers(service: OrchestratorService, run_id: str) -> set[str]:
    rows = service.list_artifacts(run_id, artifact_type="bot_state_notify", limit=200)
    markers: set[str] = set()
    for row in rows:
        metadata = row.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        marker = str(metadata.get("marker_key") or "").strip()
        if marker:
            markers.add(marker)
    return markers


def record_notification_marker(
    *,
    service: OrchestratorService,
    run_id: str,
    marker_key: str,
    state: str,
    event_id: int | None = None,
) -> None:
    metadata: dict[str, Any] = {
        "marker_key": marker_key,
        "state": state,
    }
    if event_id is not None:
        metadata["event_id"] = int(event_id)
    service.add_artifact(
        run_id,
        artifact_type="bot_state_notify",
        uri=f"bot://notify/{marker_key}",
        metadata=metadata,
    )


def build_state_change_notification(
    *,
    service: OrchestratorService,
    snapshot: dict[str, Any],
    state: str,
) -> str | None:
    run = snapshot["run"]
    run_id = str(run["run_id"])
    owner = str(run["owner"])
    repo = str(run["repo"])

    if state == RunState.PUSHED.value:
        lines = [
            f"[notify] {run_id}",
            f"{owner}/{repo} is now PUSHED and waiting PR gate.",
            f"- inspect: /show {run_id}",
            f"- pending approvals: /pending_pr",
        ]
        request_payload, request_artifact = load_artifact_payload(
            service=service,
            run_id=run_id,
            artifact_type="pr_open_request",
        )
        if request_artifact is not None:
            expires_at = str(request_artifact.get("metadata", {}).get("expires_at") or "?")
            token = ""
            if isinstance(request_payload, dict):
                token = str(request_payload.get("confirm_token") or "").strip()
            if token:
                lines.append(f"- approve now: /approve_pr {run_id} {token}")
            else:
                lines.append(f"- approve now: /approve_pr {run_id} <confirm_token>")
            lines.append(f"- token_expires_at: {expires_at}")
        return "\n".join(lines)

    if state == RunState.NEEDS_HUMAN_REVIEW.value:
        digest, _ = load_artifact_payload(
            service=service,
            run_id=run_id,
            artifact_type="run_digest",
        )
        reason_code = "unknown"
        if isinstance(digest, dict):
            classification = digest.get("classification")
            classification = classification if isinstance(classification, dict) else {}
            reason_code = str(classification.get("reason_code") or "unknown")
        default_target = DEFAULT_TARGET_STATE
        lines = [
            f"[notify] {run_id}",
            f"{owner}/{repo} needs human review (reason={reason_code}).",
            f"- inspect: /show {run_id}",
            f"- retry: /retry {run_id} {default_target}",
            f"- resume: /resume {run_id} {default_target}",
        ]
        return "\n".join(lines)

    if state == RunState.FAILED.value:
        default_target = DEFAULT_TARGET_STATE
        lines = [
            f"[notify] {run_id}",
            f"{owner}/{repo} moved to FAILED.",
            f"- inspect: /show {run_id}",
            f"- retry: /retry {run_id} {default_target}",
        ]
        return "\n".join(lines)

    if state == RunState.DONE.value:
        lines = [
            f"[notify] {run_id}",
            f"{owner}/{repo} reached DONE.",
            f"- details: /show {run_id}",
        ]
        return "\n".join(lines)

    if state == RunState.ITERATING.value:
        events = service.list_events(run_id, limit=1)
        if not events:
            return None
        latest = events[0]
        event_type = str(latest.get("event_type") or "")
        event_id = int(latest.get("event_id") or 0)
        payload = latest.get("payload")
        payload = payload if isinstance(payload, dict) else {}
        trigger = ""
        if event_type == "github.review.submitted":
            review_state = str(payload.get("state") or "").strip().lower()
            if review_state == "changes_requested":
                trigger = "review changes_requested"
        elif event_type == "github.check.completed":
            conclusion = str(payload.get("conclusion") or "").strip().lower()
            if conclusion and conclusion != "success":
                trigger = f"ci check {conclusion}"
        if not trigger:
            return None
        lines = [
            f"[notify] {run_id}",
            f"{owner}/{repo} moved to ITERATING ({trigger}).",
            "- manager can continue auto-fix in loop mode.",
            f"- inspect: /show {run_id}",
            f"- pause: /pause {run_id}",
        ]
        return "\n".join(lines)

    return None


def maybe_emit_state_notifications(
    *,
    client: TelegramClient,
    service: OrchestratorService,
    notification_chat_ids: list[int],
    scan_limit: int,
    audit: TelegramAuditLogger,
) -> None:
    if not notification_chat_ids:
        return
    rows = service.list_runs(limit=max(int(scan_limit), 1))
    for row in rows:
        run_id = str(row.get("run_id") or "").strip()
        state = str(row.get("display_state") or row.get("current_state") or "").strip()
        if not run_id or not state:
            continue
        should_scan = state in NOTIFY_TERMINAL_STATES or state == RunState.ITERATING.value
        if not should_scan:
            continue
        marker_set = load_notification_markers(service, run_id)
        marker_key = f"state:{state}"
        event_id: int | None = None
        if state == RunState.ITERATING.value:
            events = service.list_events(run_id, limit=1)
            if not events:
                continue
            event_id = int(events[0].get("event_id") or 0)
            event_type = str(events[0].get("event_type") or "")
            if event_type not in {"github.review.submitted", "github.check.completed"}:
                continue
            marker_key = f"iterating_event:{event_id}"
        if marker_key in marker_set:
            continue
        try:
            snapshot = service.get_run_snapshot(run_id)
        except KeyError:
            continue
        text = build_state_change_notification(
            service=service,
            snapshot=snapshot,
            state=state,
        )
        if not text:
            continue
        delivered = False
        for chat_id in notification_chat_ids:
            ok = safe_send_message(client=client, chat_id=chat_id, text=format_bot_response(text))
            delivered = delivered or ok
        if not delivered:
            continue
        record_notification_marker(
            service=service,
            run_id=run_id,
            marker_key=marker_key,
            state=state,
            event_id=event_id,
        )
        audit.append(
            {
                "ts": datetime.now(UTC).isoformat(),
                "kind": "state_notify",
                "run_id": run_id,
                "state": state,
                "marker_key": marker_key,
                "chat_count": len(notification_chat_ids),
            }
        )


def maybe_emit_manager_notifications(
    *,
    client: TelegramClient,
    service: OrchestratorService,
    notification_chat_ids: list[int],
    scan_limit: int,
    audit: TelegramAuditLogger,
) -> None:
    """Push unsent manager_notification artifacts to Telegram."""
    if not notification_chat_ids:
        return
    artifacts = service.list_artifacts_global(
        artifact_type="manager_notification", limit=max(int(scan_limit), 1)
    )
    for artifact in artifacts:
        artifact_id = artifact.get("id")
        run_id = str(artifact.get("run_id") or "").strip()
        if not artifact_id or not run_id:
            continue
        marker_key = f"mgr_notify:{artifact_id}"
        markers = load_notification_markers(service, run_id)
        if marker_key in markers:
            continue
        meta = artifact.get("metadata")
        meta = meta if isinstance(meta, dict) else {}
        message = str(meta.get("message") or "").strip()
        priority = str(meta.get("priority") or "normal").strip()
        if not message:
            continue
        prefix = f"[{priority.upper()}] " if priority in {"high", "urgent"} else ""
        text = f"{prefix}{run_id}: {message}"
        delivered = False
        for chat_id in notification_chat_ids:
            ok = safe_send_message(client=client, chat_id=chat_id, text=format_bot_response(text))
            delivered = delivered or ok
        if not delivered:
            continue
        record_notification_marker(
            service=service, run_id=run_id, marker_key=marker_key, state="manager_notification"
        )
        audit.append(
            {
                "ts": datetime.now(UTC).isoformat(),
                "kind": "mgr_notify",
                "run_id": run_id,
                "artifact_id": artifact_id,
                "priority": priority,
            }
        )


def parse_create_command_args(args: list[str]) -> tuple[list[str], str] | None:
    if not args:
        return None
    prompt_version = resolve_default_prompt_version()
    repo_refs: list[str] = []
    index = 0
    while index < len(args):
        token = str(args[index]).strip()
        if token in {"--prompt-version", "-p"}:
            if index + 1 >= len(args):
                return None
            candidate = str(args[index + 1]).strip()
            if not candidate:
                return None
            prompt_version = candidate
            index += 2
            continue
        repo_refs.append(token)
        index += 1
    if not repo_refs:
        return None
    return repo_refs, prompt_version


def create_runs_from_refs(
    *,
    repo_refs: list[str],
    prompt_version: str,
    service: OrchestratorService,
    db_path: Path,
    workspace_root: Path,
    integration_root: Path,
    project_root: Path,
) -> str:
    unique_refs: list[str] = []
    seen_refs: set[str] = set()
    for item in repo_refs:
        ref = str(item).strip()
        if not ref:
            continue
        if ref in seen_refs:
            continue
        seen_refs.add(ref)
        unique_refs.append(ref)

    created: list[tuple[str, str, str]] = []
    failures: list[str] = []
    for ref in unique_refs:
        parsed = parse_repo_ref(ref)
        if parsed is None:
            failures.append(f"{ref}: invalid repo ref")
            continue
        owner, repo = parsed
        result = run_cli_command(
            [
                "create-run",
                "--owner",
                owner,
                "--repo",
                repo,
                "--prompt-version",
                prompt_version,
            ],
            db_path=db_path,
            workspace_root=workspace_root,
            integration_root=integration_root,
            project_root=project_root,
        )
        if not result["ok"]:
            failures.append(f"{owner}/{repo}: {result['text']}")
            continue
        payload = result.get("payload") or {}
        run_id = str(payload.get("run_id") or "").strip()
        if not run_id:
            failures.append(f"{owner}/{repo}: create-run returned empty run_id")
            continue
        created.append((owner, repo, run_id))

    lines: list[str] = []
    if created:
        lines.append(f"Created {len(created)} run(s):")
        for owner, repo, run_id in created:
            lines.append(f"- {owner}/{repo} -> {run_id}")
        if resolve_create_autokick():
            kicked = 0
            kick_failures: list[str] = []
            for _owner, _repo, run_id in created:
                kick_result = run_cli_command(
                    [
                        "manager-tick",
                        "--run-id",
                        run_id,
                        "--max-actions-per-run",
                        "1",
                    ],
                    db_path=db_path,
                    workspace_root=workspace_root,
                    integration_root=integration_root,
                    project_root=project_root,
                )
                if kick_result["ok"]:
                    kicked += 1
                else:
                    kick_failures.append(f"{run_id}: {kick_result['text']}")
            lines.append(f"Auto-kick: {kicked}/{len(created)} run(s) queued for immediate progression.")
            if kick_failures:
                lines.append("Auto-kick failures:")
                lines.extend([f"- {item}" for item in kick_failures[:5]])
                if len(kick_failures) > 5:
                    lines.append(f"- ... and {len(kick_failures) - 5} more")
        else:
            lines.append("Manager loop will auto-progress them on next interval.")
    if failures:
        if lines:
            lines.append("")
        lines.append(f"Failed {len(failures)} item(s):")
        lines.extend([f"- {item}" for item in failures])
    if not lines:
        return "No runs created."
    return "\n".join(lines)


def load_artifact_payload(
    *,
    service: OrchestratorService,
    run_id: str,
    artifact_type: str,
) -> tuple[dict[str, Any] | None, dict[str, Any] | None]:
    artifact = service.latest_artifact(run_id, artifact_type=artifact_type)
    if artifact is None:
        return None, None
    uri = str(artifact.get("uri") or "").strip()
    if not uri:
        return None, artifact
    path = Path(uri)
    if not path.exists():
        return None, artifact
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, artifact
    if not isinstance(payload, dict):
        return None, artifact
    return payload, artifact


def render_run_detail(
    *,
    service: OrchestratorService,
    run_id: str,
    snapshot: dict[str, Any],
    decision_llm_client: ManagerLLMClient | None,
    decision_why_mode: str,
) -> str:
    run = snapshot["run"]
    state = str(snapshot["state"])
    lines = [
        f"run_id: {run['run_id']}",
        f"repo: {run['owner']}/{run['repo']}",
        f"state: {state}",
        f"pr_number: {run.get('pr_number')}",
        f"workspace: {run['workspace_dir']}",
    ]

    digest, _digest_artifact = load_artifact_payload(
        service=service,
        run_id=run_id,
        artifact_type="run_digest",
    )
    if digest is None:
        lines.append("")
        lines.append("Decision Card: unavailable (no run_digest artifact yet).")
        return "\n".join(lines)

    classification = digest.get("classification")
    classification = classification if isinstance(classification, dict) else {}
    recommendation = digest.get("manager_recommendation")
    recommendation = recommendation if isinstance(recommendation, dict) else {}
    validation = digest.get("validation")
    validation = validation if isinstance(validation, dict) else {}
    changes = digest.get("changes")
    changes = changes if isinstance(changes, dict) else {}
    attempt = digest.get("attempt")
    attempt = attempt if isinstance(attempt, dict) else {}
    digest_state = digest.get("state")
    digest_state = digest_state if isinstance(digest_state, dict) else {}
    stages = digest.get("stages")
    stages = stages if isinstance(stages, dict) else {}
    skills = digest.get("skills")
    skills = skills if isinstance(skills, dict) else {}

    grade = str(classification.get("grade") or "UNKNOWN")
    reason_code = str(classification.get("reason_code") or "unknown")
    next_action = str(classification.get("next_action") or "unknown")
    reason_detail = REASON_CODE_EXPLANATIONS.get(
        reason_code,
        "Reason code is not mapped yet; inspect runtime artifact for details.",
    )

    action = str(recommendation.get("action") or "unknown")
    priority = str(recommendation.get("priority") or "normal")
    why = str(recommendation.get("why") or "no manager explanation")
    state_before = str(digest_state.get("before") or "?")
    state_after = str(digest_state.get("after") or state)

    test_count = int(validation.get("test_command_count") or 0)
    failed_test_count = int(validation.get("failed_test_command_count") or 0)
    failed_test_commands = [
        clamp_str(str(item), max_len=160)
        for item in (validation.get("failed_test_commands") or [])
        if str(item).strip()
    ][:2]

    changed_files_count = int(changes.get("changed_files_count") or 0)
    added_lines = int(changes.get("added_lines") or 0)
    deleted_lines = int(changes.get("deleted_lines") or 0)
    changed_files = [
        clamp_str(str(item), max_len=120)
        for item in (changes.get("changed_files") or [])
        if str(item).strip()
    ][:6]

    top_step = ""
    top_share = 0.0
    top_duration_ms = 0
    step_totals = stages.get("step_totals")
    step_totals = step_totals if isinstance(step_totals, list) else []
    if step_totals:
        first = step_totals[0]
        if isinstance(first, dict):
            top_step = str(first.get("step") or "")
            top_share = float(first.get("share_of_total_pct") or 0.0)
            top_duration_ms = int(first.get("total_duration_ms") or 0)

    lines.append("")
    why_llm_text = ""
    why_llm_actions: list[str] = []
    if decision_why_mode != DECISION_WHY_MODE_OFF and decision_llm_client is not None:
        try:
            explanation = decision_llm_client.explain_decision_card(
                decision_card={
                    "run_id": run_id,
                    "repo": f"{run['owner']}/{run['repo']}",
                    "state": {"before": state_before, "after": state_after},
                    "classification": {
                        "grade": grade,
                        "reason_code": reason_code,
                        "next_action": next_action,
                    },
                    "recommendation": {
                        "action": action,
                        "priority": priority,
                        "why_machine": why,
                    },
                    "validation": {
                        "test_command_count": test_count,
                        "failed_test_command_count": failed_test_count,
                    },
                    "changes": {
                        "changed_files_count": changed_files_count,
                        "added_lines": added_lines,
                        "deleted_lines": deleted_lines,
                    },
                }
            )
            why_llm_text = clamp_str(explanation.why_llm, max_len=280)
            why_llm_actions = [
                clamp_str(item, max_len=180) for item in explanation.suggested_actions
            ][:3]
        except ManagerLLMError:
            why_llm_text = ""

    lines.append("Decision Card:")
    lines.append(f"- decision: {action} (priority={priority})")
    lines.append(f"- why_machine: {why}")
    if why_llm_text:
        lines.append(f"- why_llm: {why_llm_text}")
        if why_llm_actions:
            lines.append(f"- suggested_actions_llm: {' | '.join(why_llm_actions)}")
    elif decision_why_mode != DECISION_WHY_MODE_OFF:
        lines.append(
            "- why_llm: unavailable (configure manager API key/model for decision explanation)."
        )
    lines.append(f"- runtime: grade={grade} reason={reason_code} next={next_action}")
    lines.append(f"- reason_detail: {reason_detail}")
    lines.append(f"- state_flow: {state_before} -> {state_after}")
    lines.append(f"- skills_mode: {str(skills.get('mode') or 'off')}")
    missing_required = skills.get("missing_required")
    missing_required = missing_required if isinstance(missing_required, list) else []
    if missing_required:
        lines.append(f"- missing_required_skills: {', '.join(str(x) for x in missing_required[:4])}")

    lines.append("Evidence:")
    lines.append(
        "- attempt: "
        f"attempt_no={attempt.get('attempt_no')} "
        f"exit_code={attempt.get('exit_code')} "
        f"duration_ms={attempt.get('duration_ms')}"
    )
    lines.append(f"- tests: observed={test_count} failed_markers={failed_test_count}")
    if failed_test_commands:
        lines.append(f"- failed_markers_sample: {' | '.join(failed_test_commands)}")
    lines.append(f"- diff: files={changed_files_count} +{added_lines}/-{deleted_lines}")
    if changed_files:
        lines.append(f"- changed_files_sample: {', '.join(changed_files)}")
    if top_step:
        lines.append(
            f"- runtime_top_step: {top_step} {top_share:.2f}% ({top_duration_ms}ms)"
        )

    if grade == "PASS" and failed_test_count > 0:
        lines.append(
            "- note: intermediate test failures were observed but this run converged to success."
        )

    pr_request, pr_request_artifact = load_artifact_payload(
        service=service,
        run_id=run_id,
        artifact_type="pr_open_request",
    )
    if state == RunState.PUSHED.value and pr_request_artifact is not None:
        expires_at = str(pr_request_artifact.get("metadata", {}).get("expires_at") or "?")
        confirm_token = ""
        if isinstance(pr_request, dict):
            confirm_token = str(pr_request.get("confirm_token") or "").strip()
        lines.append("")
        lines.append("Human Decision Needed:")
        if confirm_token:
            lines.append(f"- approve_pr_cmd: /approve_pr {run_id} {confirm_token}")
        else:
            lines.append(f"- approve_pr_cmd: /approve_pr {run_id} <confirm_token>")
        lines.append(f"- approve_pr_expires_at: {expires_at}")
    elif state == RunState.NEEDS_HUMAN_REVIEW.value:
        default_target = DEFAULT_TARGET_STATE
        lines.append("")
        lines.append("Human Decision Needed:")
        lines.append(f"- inspect: /show {run_id}")
        lines.append(f"- retry_default: /retry {run_id} {default_target}")
        lines.append(f"- resume_default: /resume {run_id} {default_target}")
    elif state in {RunState.FAILED_RETRYABLE.value, RunState.FAILED.value}:
        default_target = DEFAULT_TARGET_STATE
        lines.append("")
        lines.append("Suggested Action:")
        lines.append(f"- retry: /retry {run_id} {default_target}")

    return "\n".join(lines)


def render_overview(*, service: OrchestratorService, list_limit: int) -> str:
    runs = service.list_runs(limit=max(1, min(int(list_limit), 50)))
    if not runs:
        return "No runs."
    lines: list[str] = []
    lines.append(f"Total recent runs: {len(runs)}")
    latest = runs[0]
    latest_state = str(latest.get("display_state") or latest.get("current_state") or "UNKNOWN")
    lines.append(
        "Latest: "
        f"{latest['run_id']} | {latest['owner']}/{latest['repo']} | {latest_state}"
    )

    state_counts: dict[str, int] = {}
    for row in runs:
        state = str(row.get("display_state") or row.get("current_state") or "UNKNOWN")
        state_counts[state] = int(state_counts.get(state, 0)) + 1
    top_states = sorted(state_counts.items(), key=lambda item: (-item[1], item[0]))[:6]
    if top_states:
        lines.append("States: " + ", ".join([f"{name}={count}" for name, count in top_states]))

    attention_states = {
        RunState.PUSHED.value,
        RunState.NEEDS_HUMAN_REVIEW.value,
        RunState.FAILED.value,
        RunState.FAILED_RETRYABLE.value,
        RunState.FAILED_TERMINAL.value,
        RunState.PAUSED.value,
    }
    attention = [
        row
        for row in runs
        if str(row.get("display_state") or row.get("current_state")) in attention_states
    ][:8]
    if attention:
        lines.append("Need attention:")
        for row in attention:
            state = str(row.get("display_state") or row.get("current_state") or "UNKNOWN")
            lines.append(
                f"- {row['run_id']} | {row['owner']}/{row['repo']} | {state}"
            )

    pending_pr_lines: list[str] = []
    for row in runs:
        if row.get("current_state") != RunState.PUSHED.value:
            continue
        run_id = str(row["run_id"])
        artifact = service.latest_artifact(run_id, artifact_type="pr_open_request")
        if artifact is None:
            continue
        expires_at = artifact["metadata"].get("expires_at", "?")
        pending_pr_lines.append(
            f"- {run_id} | {row['owner']}/{row['repo']} | expires={expires_at}"
        )
        if len(pending_pr_lines) >= 5:
            break
    if pending_pr_lines:
        lines.append("Pending PR approvals:")
        lines.extend(pending_pr_lines)

    try:
        from .manager_tools import get_global_stats

        stats = get_global_stats(service=service, limit=max(1, min(int(list_limit), 50)))
        if stats.get("ok") and stats.get("digest_available_runs", 0) > 0:
            lines.append(
                f"Pass rate: {stats['pass_rate_pct']}%"
                f" ({stats['digest_available_runs']} runs graded)"
            )
            grades = stats.get("grade_counts") or {}
            if grades:
                grade_parts = [f"{k}={v}" for k, v in sorted(grades.items(), key=lambda x: -x[1])]
                lines.append("Grades: " + ", ".join(grade_parts))
            top_reasons = stats.get("top_reason_codes") or []
            if top_reasons:
                reason_parts = [f"{code}({n})" for code, n in top_reasons[:3]]
                lines.append("Top reasons: " + ", ".join(reason_parts))
    except Exception:  # noqa: BLE001
        pass  # Stats are best-effort

    return "\n".join(lines)


def handle_bot_command(
    *,
    text: str,
    service: OrchestratorService,
    db_path: Path,
    workspace_root: Path,
    integration_root: Path,
    project_root: Path,
    list_limit: int,
    decision_llm_client: ManagerLLMClient | None = None,
    decision_why_mode: str | None = None,
) -> str:
    try:
        parts = shlex.split(text)
    except ValueError:
        return "Invalid command format."
    if not parts:
        return "Empty command."
    resolved_why_mode = (
        str(decision_why_mode).strip().lower()
        if decision_why_mode is not None
        else resolve_decision_why_mode()
    )
    if resolved_why_mode not in DECISION_WHY_MODES:
        resolved_why_mode = DECISION_WHY_MODE_HYBRID
    resolved_decision_llm_client = decision_llm_client
    if resolved_decision_llm_client is None and resolved_why_mode != DECISION_WHY_MODE_OFF:
        resolved_decision_llm_client = build_decision_llm_client_if_enabled(
            decision_why_mode=resolved_why_mode,
            fallback_client=None,
        )

    command = parts[0].split("@", 1)[0].lower()
    args = parts[1:]

    if command in {"/start", "/help"}:
        return (
            "Commands:\n"
            "/create <owner/repo|github_url>... [--prompt-version vX]\n"
            "/overview\n"
            "/list [N]\n"
            "/show <run_id>\n"
            "/status <run_id>\n"
            "/pending_pr [N]\n"
            "/approve_pr <run_id> <confirm_token>\n"
            "/pause <run_id>\n"
            "/resume <run_id> <target_state>\n"
            "/retry <run_id> <target_state>"
        )

    if command == "/create":
        parsed = parse_create_command_args(args)
        if parsed is None:
            return "Usage: /create <owner/repo|github_url>... [--prompt-version vX]"
        repo_refs, prompt_version = parsed
        if not repo_refs:
            return "At least one repo ref is required."
        return create_runs_from_refs(
            repo_refs=repo_refs,
            prompt_version=prompt_version,
            service=service,
            db_path=db_path,
            workspace_root=workspace_root,
            integration_root=integration_root,
            project_root=project_root,
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
            state = str(row.get("display_state") or row.get("current_state") or "UNKNOWN")
            lines.append(
                f"{row['run_id']} | {row['repo']} | {state}"
            )
        return "\n".join(lines)

    if command == "/overview":
        return render_overview(service=service, list_limit=list_limit)

    if command in {"/show", "/status"}:
        if len(args) != 1:
            return f"Usage: {command} <run_id>"
        run_id = args[0]
        try:
            snapshot = service.get_run_snapshot(run_id)
        except KeyError:
            return f"Run not found: {run_id}"
        return render_run_detail(
            service=service,
            run_id=run_id,
            snapshot=snapshot,
            decision_llm_client=resolved_decision_llm_client,
            decision_why_mode=resolved_why_mode,
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
    decision_llm_client: ManagerLLMClient | None,
    decision_why_mode: str | None,
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
            decision_llm_client=decision_llm_client,
            decision_why_mode=decision_why_mode,
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
            decision_llm_client=decision_llm_client,
            decision_why_mode=decision_why_mode,
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
            fallback = handle_natural_language_rules(
                text=text,
                service=service,
                db_path=db_path,
                workspace_root=workspace_root,
                integration_root=integration_root,
                project_root=project_root,
                list_limit=list_limit,
                conversation_state=conversation_state,
                decision_llm_client=decision_llm_client,
                decision_why_mode=decision_why_mode,
            )
            return f"[manager:rules_fallback] {fallback}"
        return f"manager nl routing failed: {exc}"

    if selection.action not in BOT_NL_ALLOWED_ACTIONS:
        if mode == NL_MODE_HYBRID:
            fallback = handle_natural_language_rules(
                text=text,
                service=service,
                db_path=db_path,
                workspace_root=workspace_root,
                integration_root=integration_root,
                project_root=project_root,
                list_limit=list_limit,
                conversation_state=conversation_state,
                decision_llm_client=decision_llm_client,
                decision_why_mode=decision_why_mode,
            )
            return f"[manager:rules_fallback] {fallback}"
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
        decision_llm_client=decision_llm_client,
        decision_why_mode=decision_why_mode,
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
    decision_llm_client: ManagerLLMClient | None,
    decision_why_mode: str | None,
) -> str:
    def dispatch_bot(command_text: str) -> str:
        return handle_bot_command(
            text=command_text,
            service=service,
            db_path=db_path,
            workspace_root=workspace_root,
            integration_root=integration_root,
            project_root=project_root,
            list_limit=list_limit,
            decision_llm_client=decision_llm_client,
            decision_why_mode=decision_why_mode,
        )

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

    if contains_any(lowered, ["create", "new run", "创建", "新建", "跑这个repo", "跑这个仓库"]):
        repo_refs = extract_repo_refs_text(normalized)
        if repo_refs:
            prompt_version = extract_prompt_version_from_text(normalized) or resolve_default_prompt_version()
            argv = ["/create", *repo_refs, "--prompt-version", prompt_version]
            return dispatch_bot(" ".join(argv))

    if contains_any(lowered, ["list", "all runs", "运行列表", "全部运行", "列出"]):
        limit = max(1, min(int(list_limit), 50))
        return dispatch_bot(f"/list {limit}")

    if run_id and contains_any(
        lowered,
        ["status", "show", "state", "状态", "进展", "情况"],
    ):
        return dispatch_bot(f"/show {run_id}")

    if contains_any(lowered, ["status", "overall", "overview", "目前", "整体", "全局", "近况", "情况"]):
        return dispatch_bot("/overview")

    if run_id and contains_any(lowered, ["pause", "暂停"]):
        return dispatch_bot(f"/pause {run_id}")

    if run_id and contains_any(lowered, ["resume", "恢复", "继续"]):
        target = normalize_target_state(
            extract_target_state_from_text(normalized),
            default=DEFAULT_TARGET_STATE,
        )
        return dispatch_bot(f"/resume {run_id} {target}")

    if run_id and contains_any(lowered, ["retry", "重试"]):
        target = normalize_target_state(
            extract_target_state_from_text(normalized),
            default=DEFAULT_TARGET_STATE,
        )
        return dispatch_bot(f"/retry {run_id} {target}")

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
        "你可以说：'创建 run mem0ai/mem0'、'create https://github.com/a/b https://github.com/c/d'、"
        "'目前什么情况'、'列出运行'、'查看 <run_id> 状态'、"
        "'暂停 <run_id>'、'恢复 <run_id> 到 EXECUTING/IMPLEMENTING'、'重试 <run_id>'、'推进一次'。"
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
    decision_llm_client: ManagerLLMClient | None,
    decision_why_mode: str | None,
) -> str:
    def dispatch_bot(command_text: str) -> str:
        return handle_bot_command(
            text=command_text,
            service=service,
            db_path=db_path,
            workspace_root=workspace_root,
            integration_root=integration_root,
            project_root=project_root,
            list_limit=list_limit,
            decision_llm_client=decision_llm_client,
            decision_why_mode=decision_why_mode,
        )

    action = selection.action
    run_id = selection.run_id or explicit_run_id or get_last_run_id(conversation_state)
    if run_id:
        set_last_run_id(conversation_state, run_id)
    if action == "help":
        return "自然语言模式已启用。直接说需求，或继续使用 /commands。"
    if action == "status_overview":
        return dispatch_bot("/overview")
    if action == "create_run":
        repo_ref = selection.repo_ref or extract_repo_ref_text(text)
        if not repo_ref:
            return "缺少 repo。请使用 owner/repo 或 github URL。"
        parsed_repo = parse_repo_ref(repo_ref)
        if parsed_repo is None:
            return "repo 格式无效。请使用 owner/repo 或 github URL。"
        owner, repo = parsed_repo
        prompt_version = selection.prompt_version or extract_prompt_version_from_text(text) or resolve_default_prompt_version()
        return dispatch_bot(f"/create {owner}/{repo} --prompt-version {prompt_version}")
    if action == "create_runs":
        repo_refs = selection.repo_refs or extract_repo_refs_text(text)
        if not repo_refs:
            return "缺少 repo 列表。请提供 owner/repo 或 github URL。"
        prompt_version = selection.prompt_version or extract_prompt_version_from_text(text) or resolve_default_prompt_version()
        argv = ["/create", *repo_refs, "--prompt-version", prompt_version]
        return dispatch_bot(" ".join(argv))
    if action == "list_runs":
        limit = max(1, min(int(selection.limit or list_limit), 50))
        return dispatch_bot(f"/list {limit}")
    if action == "show_run":
        if not run_id:
            recent = service.list_runs(limit=1)
            if recent:
                run_id = str(recent[0]["run_id"])
                set_last_run_id(conversation_state, run_id)
            else:
                return "缺少 run_id。请在消息中包含 run_id，或先执行 /list。"
        return dispatch_bot(f"/show {run_id}")
    if action == "pause_run":
        if not run_id:
            return "缺少 run_id。请在消息中包含 run_id。"
        return dispatch_bot(f"/pause {run_id}")
    if action == "resume_run":
        if not run_id:
            return "缺少 run_id。请在消息中包含 run_id。"
        target = normalize_target_state(
            selection.target_state or extract_target_state_from_text(text),
            default=DEFAULT_TARGET_STATE,
        )
        return dispatch_bot(f"/resume {run_id} {target}")
    if action == "retry_run":
        if not run_id:
            return "缺少 run_id。请在消息中包含 run_id。"
        target = normalize_target_state(
            selection.target_state or extract_target_state_from_text(text),
            default=DEFAULT_TARGET_STATE,
        )
        return dispatch_bot(f"/retry {run_id} {target}")
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


