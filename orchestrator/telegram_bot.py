from __future__ import annotations

import json
import shlex
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .models import RunState
from .service import OrchestratorService


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


def run_telegram_bot_loop(
    *,
    client: TelegramClient,
    service: OrchestratorService,
    db_path: Path,
    workspace_root: Path,
    integration_root: Path,
    project_root: Path,
    allowed_chat_ids: set[int],
    poll_timeout_sec: int,
    idle_sleep_sec: int,
    list_limit: int,
) -> None:
    offset: int | None = None
    while True:
        try:
            updates = client.get_updates(offset=offset, timeout_sec=poll_timeout_sec)
        except TelegramApiError:
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
            chat = message.get("chat")
            if not isinstance(chat, dict) or "id" not in chat:
                continue
            chat_id = int(chat["id"])
            if allowed_chat_ids and chat_id not in allowed_chat_ids:
                safe_send_message(
                    client=client,
                    chat_id=chat_id,
                    text="Unauthorized chat id.",
                )
                continue
            if not text.startswith("/"):
                continue
            try:
                response = handle_bot_command(
                    text=text,
                    service=service,
                    db_path=db_path,
                    workspace_root=workspace_root,
                    integration_root=integration_root,
                    project_root=project_root,
                    list_limit=list_limit,
                )
            except Exception as exc:  # noqa: BLE001
                response = f"Command failed: {exc}"
            safe_send_message(client=client, chat_id=chat_id, text=response)


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
