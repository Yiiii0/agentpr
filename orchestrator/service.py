from __future__ import annotations

import hashlib
import json
import sqlite3
from pathlib import Path
from typing import Any

from .db import Database
from .models import EventInput, EventType, RunCreateInput, RunState, StepName
from .state_machine import InvalidTransitionError, assert_transition, is_terminal


class RunNotFoundError(KeyError):
    pass


class OrchestratorService:
    def __init__(self, db: Database, workspace_root: Path) -> None:
        self.db = db
        self.workspace_root = workspace_root
        self.workspace_root.mkdir(parents=True, exist_ok=True)

    def initialize(self) -> None:
        self.db.initialize()

    def create_run(self, run_input: RunCreateInput) -> str:
        run_id = run_input.resolved_run_id()
        workspace_dir = str(self.workspace_root / run_input.repo)
        try:
            with self.db.transaction() as conn:
                self.db.insert_run(
                    conn,
                    run_id=run_id,
                    owner=run_input.owner,
                    repo=run_input.repo,
                    prompt_version=run_input.prompt_version,
                    mode=run_input.mode,
                    budget=run_input.budget,
                    workspace_dir=workspace_dir,
                )
                self.db.insert_event(
                    conn,
                    run_id=run_id,
                    event_type=EventType.COMMAND_RUN_CREATE.value,
                    idempotency_key=f"run-create:{run_id}",
                    payload={
                        "run_id": run_id,
                        "owner": run_input.owner,
                        "repo": run_input.repo,
                        "prompt_version": run_input.prompt_version,
                        "mode": run_input.mode.value,
                        "budget": run_input.budget,
                        "workspace_dir": workspace_dir,
                    },
                )
        except sqlite3.IntegrityError as exc:
            raise ValueError(f"Run already exists: {run_id}") from exc
        return run_id

    def start_discovery(self, run_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        event = EventInput(
            run_id=run_id,
            event_type=EventType.COMMAND_START_DISCOVERY,
            payload=payload,
            idempotency_key=idempotency_key
            or self._key(EventType.COMMAND_START_DISCOVERY, run_id, payload),
        )
        return self._apply_event(event)

    def mark_plan_ready(
        self,
        run_id: str,
        contract_path: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload = {"contract_path": contract_path}
        event = EventInput(
            run_id=run_id,
            event_type=EventType.WORKER_DISCOVERY_COMPLETED,
            payload=payload,
            idempotency_key=idempotency_key
            or self._key(EventType.WORKER_DISCOVERY_COMPLETED, run_id, payload),
        )
        return self._apply_event(event)

    def start_implementation(
        self, run_id: str, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        event = EventInput(
            run_id=run_id,
            event_type=EventType.COMMAND_START_IMPLEMENTATION,
            payload=payload,
            idempotency_key=idempotency_key
            or self._key(EventType.COMMAND_START_IMPLEMENTATION, run_id, payload),
        )
        return self._apply_event(event)

    def mark_local_validation_passed(
        self, run_id: str, idempotency_key: str | None = None
    ) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        event = EventInput(
            run_id=run_id,
            event_type=EventType.COMMAND_LOCAL_VALIDATION_PASSED,
            payload=payload,
            idempotency_key=idempotency_key
            or self._key(EventType.COMMAND_LOCAL_VALIDATION_PASSED, run_id, payload),
        )
        return self._apply_event(event)

    def link_pr(
        self,
        run_id: str,
        pr_number: int,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload = {"pr_number": pr_number}
        event = EventInput(
            run_id=run_id,
            event_type=EventType.COMMAND_PR_LINKED,
            payload=payload,
            idempotency_key=idempotency_key
            or self._key(EventType.COMMAND_PR_LINKED, run_id, payload),
        )
        return self._apply_event(event)

    def record_push_completed(
        self,
        run_id: str,
        branch: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload = {"branch": branch}
        event = EventInput(
            run_id=run_id,
            event_type=EventType.WORKER_PUSH_COMPLETED,
            payload=payload,
            idempotency_key=idempotency_key
            or self._key(EventType.WORKER_PUSH_COMPLETED, run_id, payload),
        )
        return self._apply_event(event)

    def record_step_failure(
        self,
        run_id: str,
        *,
        step: StepName | str,
        reason_code: str,
        error_message: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload = {
            "step": str(step),
            "reason_code": reason_code,
            "error_message": error_message,
        }
        event = EventInput(
            run_id=run_id,
            event_type=EventType.WORKER_STEP_FAILED,
            payload=payload,
            idempotency_key=idempotency_key
            or self._key(EventType.WORKER_STEP_FAILED, run_id, payload),
        )
        return self._apply_event(event)

    def record_github_check(
        self,
        run_id: str,
        *,
        conclusion: str,
        pr_number: int | None = None,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload = {"conclusion": conclusion, "pr_number": pr_number}
        event = EventInput(
            run_id=run_id,
            event_type=EventType.GITHUB_CHECK_COMPLETED,
            payload=payload,
            idempotency_key=idempotency_key
            or self._key(EventType.GITHUB_CHECK_COMPLETED, run_id, payload),
        )
        return self._apply_event(event)

    def record_review(
        self,
        run_id: str,
        *,
        review_state: str,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload = {"state": review_state}
        event = EventInput(
            run_id=run_id,
            event_type=EventType.GITHUB_REVIEW_SUBMITTED,
            payload=payload,
            idempotency_key=idempotency_key
            or self._key(EventType.GITHUB_REVIEW_SUBMITTED, run_id, payload),
        )
        return self._apply_event(event)

    def pause_run(self, run_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        event = EventInput(
            run_id=run_id,
            event_type=EventType.COMMAND_PAUSE,
            payload=payload,
            idempotency_key=idempotency_key or self._key(EventType.COMMAND_PAUSE, run_id, payload),
        )
        return self._apply_event(event)

    def resume_run(
        self,
        run_id: str,
        *,
        target_state: RunState,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload = {"target_state": target_state.value}
        event = EventInput(
            run_id=run_id,
            event_type=EventType.COMMAND_RESUME,
            payload=payload,
            idempotency_key=idempotency_key
            or self._key(EventType.COMMAND_RESUME, run_id, payload),
        )
        return self._apply_event(event)

    def retry_run(
        self,
        run_id: str,
        *,
        target_state: RunState,
        idempotency_key: str | None = None,
    ) -> dict[str, Any]:
        payload = {"target_state": target_state.value}
        event = EventInput(
            run_id=run_id,
            event_type=EventType.COMMAND_RETRY,
            payload=payload,
            idempotency_key=idempotency_key
            or self._key(EventType.COMMAND_RETRY, run_id, payload),
        )
        return self._apply_event(event)

    def mark_done(self, run_id: str, idempotency_key: str | None = None) -> dict[str, Any]:
        payload: dict[str, Any] = {}
        event = EventInput(
            run_id=run_id,
            event_type=EventType.COMMAND_MARK_DONE,
            payload=payload,
            idempotency_key=idempotency_key
            or self._key(EventType.COMMAND_MARK_DONE, run_id, payload),
        )
        return self._apply_event(event)

    def list_runs(self, limit: int = 50) -> list[dict[str, Any]]:
        with self.db.transaction() as conn:
            return self.db.list_runs(conn, limit=limit)

    def get_run_snapshot(self, run_id: str) -> dict[str, Any]:
        return self.db.get_run_snapshot(run_id)

    def add_step_attempt(
        self,
        run_id: str,
        *,
        step: StepName,
        exit_code: int,
        stdout_log: str,
        stderr_log: str,
        duration_ms: int,
    ) -> None:
        with self.db.transaction() as conn:
            self._require_run(conn, run_id)
            self.db.insert_step_attempt(
                conn,
                run_id=run_id,
                step=step.value,
                exit_code=exit_code,
                stdout_log=stdout_log,
                stderr_log=stderr_log,
                duration_ms=duration_ms,
            )

    def add_artifact(
        self, run_id: str, *, artifact_type: str, uri: str, metadata: dict[str, Any] | None = None
    ) -> None:
        with self.db.transaction() as conn:
            self._require_run(conn, run_id)
            self.db.insert_artifact(
                conn,
                run_id=run_id,
                artifact_type=artifact_type,
                uri=uri,
                metadata=metadata,
            )

    def _apply_event(self, event: EventInput) -> dict[str, Any]:
        with self.db.transaction() as conn:
            self._require_run(conn, event.run_id)
            inserted = self.db.insert_event(
                conn,
                run_id=event.run_id,
                event_type=event.event_type.value,
                idempotency_key=event.idempotency_key,
                payload=event.payload,
            )
            current_state = self.db.get_state(conn, event.run_id)
            if not inserted:
                return {
                    "duplicate": True,
                    "run_id": event.run_id,
                    "state": current_state.value,
                    "event_type": event.event_type.value,
                }

            target, last_error = self._resolve_target(
                current_state=current_state,
                event=event,
            )
            if target is None and event.event_type in _REQUIRES_TRANSITION:
                raise InvalidTransitionError(
                    f"No valid transition for {event.event_type.value} from {current_state.value}"
                )
            if target is not None:
                assert_transition(current_state, target)
                self.db.set_state(
                    conn,
                    run_id=event.run_id,
                    target=target,
                    last_error=last_error,
                )
                current_state = target

            if event.event_type == EventType.COMMAND_PR_LINKED:
                pr_number = int(event.payload["pr_number"])
                self.db.set_pr_number(conn, run_id=event.run_id, pr_number=pr_number)

            if event.event_type == EventType.WORKER_DISCOVERY_COMPLETED:
                self.db.insert_artifact(
                    conn,
                    run_id=event.run_id,
                    artifact_type="contract",
                    uri=event.payload["contract_path"],
                    metadata={},
                )

            if event.event_type == EventType.WORKER_PUSH_COMPLETED:
                self.db.insert_artifact(
                    conn,
                    run_id=event.run_id,
                    artifact_type="branch",
                    uri=event.payload["branch"],
                    metadata={},
                )

            return {
                "duplicate": False,
                "run_id": event.run_id,
                "state": current_state.value,
                "event_type": event.event_type.value,
            }

    def _resolve_target(
        self,
        *,
        current_state: RunState,
        event: EventInput,
    ) -> tuple[RunState | None, str | None]:
        event_type = event.event_type
        payload = event.payload

        if event_type == EventType.COMMAND_START_DISCOVERY:
            if current_state in {RunState.QUEUED, RunState.PAUSED, RunState.FAILED_RETRYABLE}:
                return RunState.DISCOVERY, None
            return None, None

        if event_type == EventType.WORKER_DISCOVERY_COMPLETED:
            if current_state == RunState.QUEUED:
                raise InvalidTransitionError(
                    "Discovery cannot complete from QUEUED; start discovery first."
                )
            return RunState.PLAN_READY, None

        if event_type == EventType.COMMAND_START_IMPLEMENTATION:
            if current_state in {RunState.PLAN_READY, RunState.ITERATING, RunState.PAUSED}:
                return RunState.IMPLEMENTING, None
            return None, None

        if event_type == EventType.COMMAND_LOCAL_VALIDATION_PASSED:
            if current_state in {RunState.IMPLEMENTING, RunState.ITERATING, RunState.PAUSED}:
                return RunState.LOCAL_VALIDATING, None
            return None, None

        if event_type == EventType.WORKER_PUSH_COMPLETED:
            return RunState.PUSHED, None

        if event_type == EventType.COMMAND_PR_LINKED:
            return RunState.CI_WAIT, None

        if event_type == EventType.WORKER_STEP_FAILED:
            step = payload.get("step", "unknown")
            reason_code = payload.get("reason_code", "unknown")
            message = payload.get("error_message", "")
            return RunState.FAILED_RETRYABLE, f"{step}:{reason_code}:{message}"

        if event_type == EventType.GITHUB_CHECK_COMPLETED:
            conclusion = str(payload.get("conclusion", "")).lower()
            success = {"success", "neutral", "skipped"}
            if conclusion in success:
                return RunState.REVIEW_WAIT, None
            return RunState.ITERATING, None

        if event_type == EventType.GITHUB_REVIEW_SUBMITTED:
            review_state = str(payload.get("state", "")).lower()
            if review_state == "changes_requested":
                return RunState.ITERATING, None
            return None, None

        if event_type == EventType.COMMAND_MARK_DONE:
            if current_state in {
                RunState.PUSHED,
                RunState.REVIEW_WAIT,
                RunState.NEEDS_HUMAN_REVIEW,
            }:
                return RunState.DONE, None
            return None, None

        if event_type == EventType.COMMAND_PAUSE:
            if is_terminal(current_state):
                raise InvalidTransitionError(f"Cannot pause terminal state: {current_state}")
            return RunState.PAUSED, None

        if event_type in {EventType.COMMAND_RESUME, EventType.COMMAND_RETRY}:
            target = RunState(payload["target_state"])
            return target, None

        if event_type == EventType.TIMER_TIMEOUT:
            step = payload.get("step", "unknown")
            return RunState.FAILED_RETRYABLE, f"timeout:{step}"

        return None, None

    def _require_run(self, conn: Any, run_id: str) -> dict[str, Any]:
        run = self.db.get_run(conn, run_id)
        if run is None:
            raise RunNotFoundError(f"Run not found: {run_id}")
        return run

    @staticmethod
    def _key(event_type: EventType, run_id: str, payload: dict[str, Any]) -> str:
        canonical_payload = json.dumps(payload, sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha1(canonical_payload.encode("utf-8")).hexdigest()[:12]  # noqa: S324
        return f"{event_type.value}:{run_id}:{digest}"


_REQUIRES_TRANSITION: set[EventType] = {
    EventType.COMMAND_START_DISCOVERY,
    EventType.COMMAND_START_IMPLEMENTATION,
    EventType.COMMAND_LOCAL_VALIDATION_PASSED,
    EventType.COMMAND_MARK_DONE,
    EventType.COMMAND_PR_LINKED,
    EventType.COMMAND_PAUSE,
    EventType.COMMAND_RESUME,
    EventType.COMMAND_RETRY,
    EventType.WORKER_DISCOVERY_COMPLETED,
    EventType.WORKER_STEP_FAILED,
    EventType.WORKER_PUSH_COMPLETED,
    EventType.TIMER_TIMEOUT,
}
