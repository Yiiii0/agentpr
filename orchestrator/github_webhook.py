from __future__ import annotations

import hmac
import json
from dataclasses import dataclass
from datetime import UTC, datetime
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from uuid import uuid4

from .github_sync import PENDING_STATES, SUCCESS_CONCLUSIONS
from .service import OrchestratorService
from .state_machine import InvalidTransitionError


@dataclass(frozen=True)
class WebhookOutcome:
    ok: bool
    event: str
    delivery: str
    processed: int
    ignored: int
    retryable_failures: int
    failures: list[dict[str, Any]]
    results: list[dict[str, Any]]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "event": self.event,
            "delivery": self.delivery,
            "processed": self.processed,
            "ignored": self.ignored,
            "retryable_failures": self.retryable_failures,
            "failures": self.failures,
            "results": self.results,
        }


class WebhookAuditLogger:
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


def run_github_webhook_server(
    *,
    service: OrchestratorService,
    host: str,
    port: int,
    path: str,
    secret: str | None,
    require_signature: bool,
    max_payload_bytes: int,
    audit_log_file: Path | None,
) -> None:
    normalized_path = normalize_path(path)
    audit = WebhookAuditLogger(audit_log_file)
    server = ThreadingHTTPServer((host, port), _build_handler_class(
        service=service,
        path=normalized_path,
        secret=secret,
        require_signature=require_signature,
        max_payload_bytes=max_payload_bytes,
        audit=audit,
    ))
    server.serve_forever()


def _build_handler_class(
    *,
    service: OrchestratorService,
    path: str,
    secret: str | None,
    require_signature: bool,
    max_payload_bytes: int,
    audit: WebhookAuditLogger,
) -> type[BaseHTTPRequestHandler]:
    class GitHubWebhookHandler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:  # noqa: N802
            event = self.headers.get("X-GitHub-Event", "").strip()
            delivery = self.headers.get("X-GitHub-Delivery", "").strip() or uuid4().hex

            def respond(status_code: int, payload: dict[str, Any], outcome: str) -> None:
                self._send(status_code, payload)
                audit.append(
                    {
                        "ts": datetime.now(UTC).isoformat(),
                        "method": "POST",
                        "path": normalize_path(self.path),
                        "event": event,
                        "delivery": delivery,
                        "status_code": int(status_code),
                        "outcome": outcome,
                        "processed": int(payload.get("processed", 0)),
                        "ignored": int(payload.get("ignored", 0)),
                        "retryable_failures": int(payload.get("retryable_failures", 0)),
                        "error": str(payload.get("error", "")),
                    }
                )

            if normalize_path(self.path) != path:
                respond(404, {"ok": False, "error": "not found"}, "not_found")
                return

            raw_length = self.headers.get("Content-Length", "0")
            try:
                length = int(raw_length)
            except ValueError:
                respond(
                    400,
                    {
                        "ok": False,
                        "event": event,
                        "delivery": delivery,
                        "error": f"invalid Content-Length: {raw_length}",
                    },
                    "invalid_content_length",
                )
                return
            if length > max_payload_bytes:
                respond(
                    413,
                    {
                        "ok": False,
                        "event": event,
                        "delivery": delivery,
                        "error": (
                            f"payload too large: {length} bytes "
                            f"(max={max_payload_bytes})"
                        ),
                    },
                    "payload_too_large",
                )
                return
            body = self.rfile.read(length)
            if len(body) > max_payload_bytes:
                respond(
                    413,
                    {
                        "ok": False,
                        "event": event,
                        "delivery": delivery,
                        "error": (
                            f"payload too large after read: {len(body)} bytes "
                            f"(max={max_payload_bytes})"
                        ),
                    },
                    "payload_too_large",
                )
                return
            signature = self.headers.get("X-Hub-Signature-256")

            if not event:
                respond(
                    400,
                    {"ok": False, "error": "missing X-GitHub-Event header"},
                    "missing_event",
                )
                return
            if not verify_signature(
                body=body,
                secret=secret,
                signature_header=signature,
                require_signature=require_signature,
            ):
                respond(
                    401,
                    {"ok": False, "error": "invalid webhook signature"},
                    "invalid_signature",
                )
                return
            payload_sha256 = sha256(body).hexdigest()
            accepted = service.reserve_webhook_delivery(
                source="github",
                delivery_id=delivery,
                event_type=event,
                payload_sha256=payload_sha256,
            )
            if not accepted:
                respond(
                    200,
                    {
                        "ok": True,
                        "event": event,
                        "delivery": delivery,
                        "duplicate_delivery": True,
                    },
                    "duplicate_delivery",
                )
                return
            try:
                payload = json.loads(body.decode("utf-8"))
            except json.JSONDecodeError:
                service.release_webhook_delivery(
                    source="github",
                    delivery_id=delivery,
                )
                respond(
                    400,
                    {"ok": False, "error": "invalid JSON payload"},
                    "invalid_json",
                )
                return

            try:
                outcome = process_github_webhook_event(
                    service=service,
                    event=event,
                    delivery=delivery,
                    payload=payload,
                )
            except Exception as exc:  # noqa: BLE001
                service.release_webhook_delivery(
                    source="github",
                    delivery_id=delivery,
                )
                respond(
                    500,
                    {
                        "ok": False,
                        "event": event,
                        "delivery": delivery,
                        "error": f"unhandled webhook error: {exc}",
                    },
                    "unhandled_error",
                )
                return
            if outcome.retryable_failures > 0:
                service.release_webhook_delivery(
                    source="github",
                    delivery_id=delivery,
                )
                respond(500, outcome.to_dict(), "retryable_failure")
                return
            respond(200, outcome.to_dict(), "processed")

        def do_GET(self) -> None:  # noqa: N802
            if normalize_path(self.path) == path:
                self._send(200, {"ok": True, "status": "alive"})
                return
            self._send(404, {"ok": False, "error": "not found"})

        def log_message(self, format: str, *args: Any) -> None:  # noqa: A003
            return

        def _send(self, status_code: int, payload: dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=True, sort_keys=True).encode("utf-8")
            self.send_response(status_code)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

    return GitHubWebhookHandler


def process_github_webhook_event(
    *,
    service: OrchestratorService,
    event: str,
    delivery: str,
    payload: dict[str, Any],
) -> WebhookOutcome:
    repo_identity = extract_repo_identity(payload)
    if repo_identity is None:
        return WebhookOutcome(
            ok=True,
            event=event,
            delivery=delivery,
            processed=0,
            ignored=1,
            retryable_failures=0,
            failures=[],
            results=[{"message": "missing repository identity in payload"}],
        )
    owner, repo = repo_identity

    pr_numbers = extract_pr_numbers(event=event, payload=payload)
    if not pr_numbers:
        return WebhookOutcome(
            ok=True,
            event=event,
            delivery=delivery,
            processed=0,
            ignored=1,
            retryable_failures=0,
            failures=[],
            results=[{"message": "no PR association in payload"}],
        )

    failures: list[dict[str, Any]] = []
    results: list[dict[str, Any]] = []
    processed = 0
    ignored = 0
    for index, pr_number in enumerate(pr_numbers):
        snapshot = service.get_run_snapshot_by_repo_and_pr_number(
            owner=owner,
            repo=repo,
            pr_number=pr_number,
        )
        if snapshot is None:
            ignored += 1
            results.append(
                {
                    "repo": f"{owner}/{repo}",
                    "pr_number": pr_number,
                    "message": "no run found for pr_number",
                }
            )
            continue
        run_id = snapshot["run"]["run_id"]
        outcome = apply_event_to_run(
            service=service,
            run_id=run_id,
            pr_number=pr_number,
            event=event,
            payload=payload,
            delivery=delivery,
            index=index,
        )
        if outcome.get("ok"):
            processed += 1
            results.append(outcome)
        elif outcome.get("ignored"):
            ignored += 1
            results.append(outcome)
        else:
            failures.append(outcome)

    return WebhookOutcome(
        ok=len(failures) == 0,
        event=event,
        delivery=delivery,
        processed=processed,
        ignored=ignored,
        retryable_failures=sum(1 for item in failures if item.get("retryable")),
        failures=failures,
        results=results,
    )


def apply_event_to_run(
    *,
    service: OrchestratorService,
    run_id: str,
    pr_number: int,
    event: str,
    payload: dict[str, Any],
    delivery: str,
    index: int,
) -> dict[str, Any]:
    idempotency_prefix = f"gh-webhook:{delivery}:{event}:{pr_number}:{index}"
    try:
        if event == "pull_request_review":
            review_state = normalize_token(payload.get("review", {}).get("state"))
            if review_state != "changes_requested":
                return {
                    "ok": True,
                    "ignored": True,
                    "run_id": run_id,
                    "pr_number": pr_number,
                    "message": f"review state ignored: {review_state or 'unknown'}",
                }
            result = service.record_review(
                run_id,
                review_state="changes_requested",
                idempotency_key=f"{idempotency_prefix}:review:{review_state}",
            )
            return {
                "ok": True,
                "run_id": run_id,
                "pr_number": pr_number,
                "event": result["event_type"],
                "state": result["state"],
            }

        check_conclusion = resolve_check_conclusion(event=event, payload=payload)
        if check_conclusion is None:
            return {
                "ok": True,
                "ignored": True,
                "run_id": run_id,
                "pr_number": pr_number,
                "message": "no actionable check conclusion",
            }
        result = service.record_github_check(
            run_id,
            conclusion=check_conclusion,
            pr_number=pr_number,
            idempotency_key=f"{idempotency_prefix}:check:{check_conclusion}",
        )
        return {
            "ok": True,
            "run_id": run_id,
            "pr_number": pr_number,
            "event": result["event_type"],
            "state": result["state"],
            "conclusion": check_conclusion,
        }
    except InvalidTransitionError as exc:
        return {
            "ok": True,
            "ignored": True,
            "run_id": run_id,
            "pr_number": pr_number,
            "message": f"ignored invalid transition: {exc}",
        }
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "retryable": True,
            "run_id": run_id,
            "pr_number": pr_number,
            "error": f"unexpected error: {exc}",
        }


def extract_pr_numbers(*, event: str, payload: dict[str, Any]) -> list[int]:
    if event in {"pull_request", "pull_request_review", "issue_comment"}:
        pr = payload.get("pull_request")
        if isinstance(pr, dict):
            number = pr.get("number")
            if isinstance(number, int):
                return [number]
        issue = payload.get("issue")
        if isinstance(issue, dict) and isinstance(issue.get("pull_request"), dict):
            number = issue.get("number")
            if isinstance(number, int):
                return [number]

    if event in {"check_suite", "check_run"}:
        root = payload.get(event)
        if isinstance(root, dict):
            numbers = extract_pr_numbers_from_list(root.get("pull_requests"))
            if numbers:
                return numbers
    return []


def extract_pr_numbers_from_list(value: Any) -> list[int]:
    if not isinstance(value, list):
        return []
    numbers: list[int] = []
    for item in value:
        if not isinstance(item, dict):
            continue
        number = item.get("number")
        if isinstance(number, int):
            numbers.append(number)
    return numbers


def resolve_check_conclusion(*, event: str, payload: dict[str, Any]) -> str | None:
    if event == "pull_request":
        action = normalize_token(payload.get("action"))
        if action == "synchronize":
            return None
        return None

    if event in {"check_suite", "check_run"}:
        root = payload.get(event)
        if not isinstance(root, dict):
            return None
        conclusion = normalize_token(root.get("conclusion"))
        status = normalize_token(root.get("status"))
        if conclusion in SUCCESS_CONCLUSIONS:
            return "success"
        if conclusion in {"failure", "timed_out", "cancelled", "action_required", "startup_failure"}:
            return "failure"
        if status in PENDING_STATES:
            return None
        return None

    return None


def verify_signature(
    *,
    body: bytes,
    secret: str | None,
    signature_header: str | None,
    require_signature: bool,
) -> bool:
    if not require_signature and not secret:
        return True
    if not secret:
        return False
    if not signature_header:
        return False
    if not signature_header.startswith("sha256="):
        return False
    expected = "sha256=" + hmac.new(
        secret.encode("utf-8"),
        body,
        sha256,
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


def normalize_path(path: str) -> str:
    if not path:
        return "/"
    if not path.startswith("/"):
        path = "/" + path
    return path.split("?", 1)[0]


def normalize_token(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()


def extract_repo_identity(payload: dict[str, Any]) -> tuple[str, str] | None:
    repository = payload.get("repository")
    if not isinstance(repository, dict):
        return None
    owner_block = repository.get("owner")
    owner = ""
    if isinstance(owner_block, dict):
        owner = normalize_token(owner_block.get("login") or owner_block.get("name"))
    if not owner:
        owner = normalize_token(repository.get("owner"))
    repo = normalize_token(repository.get("name"))
    if not owner or not repo:
        return None
    return owner, repo
