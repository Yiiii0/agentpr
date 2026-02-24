from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any
from uuid import uuid4


class RunMode(StrEnum):
    PUSH_ONLY = "push_only"


class RunState(StrEnum):
    QUEUED = "QUEUED"
    DISCOVERY = "DISCOVERY"
    PLAN_READY = "PLAN_READY"
    IMPLEMENTING = "IMPLEMENTING"
    LOCAL_VALIDATING = "LOCAL_VALIDATING"
    PUSHED = "PUSHED"
    CI_WAIT = "CI_WAIT"
    REVIEW_WAIT = "REVIEW_WAIT"
    ITERATING = "ITERATING"
    PAUSED = "PAUSED"
    DONE = "DONE"
    SKIPPED = "SKIPPED"
    NEEDS_HUMAN_REVIEW = "NEEDS_HUMAN_REVIEW"
    FAILED_RETRYABLE = "FAILED_RETRYABLE"
    FAILED_TERMINAL = "FAILED_TERMINAL"


class EventType(StrEnum):
    COMMAND_RUN_CREATE = "command.run.create"
    COMMAND_START_DISCOVERY = "command.start.discovery"
    COMMAND_START_IMPLEMENTATION = "command.start.implementation"
    COMMAND_LOCAL_VALIDATION_PASSED = "command.local.validation.passed"
    COMMAND_PR_CREATE = "command.pr.create"
    COMMAND_PR_LINKED = "command.pr.linked"
    COMMAND_MARK_DONE = "command.mark.done"
    COMMAND_RETRY = "command.retry"
    COMMAND_PAUSE = "command.pause"
    COMMAND_RESUME = "command.resume"
    WORKER_DISCOVERY_COMPLETED = "worker.discovery.completed"
    WORKER_STEP_FAILED = "worker.step.failed"
    WORKER_PUSH_COMPLETED = "worker.push.completed"
    GITHUB_CHECK_COMPLETED = "github.check.completed"
    GITHUB_REVIEW_SUBMITTED = "github.review.submitted"
    GITHUB_COMMENT_CREATED = "github.comment.created"
    TIMER_TIMEOUT = "timer.timeout"


class StepName(StrEnum):
    PREPARE = "prepare"
    FINISH = "finish"
    AGENT = "agent"


@dataclass(frozen=True)
class RunCreateInput:
    owner: str
    repo: str
    prompt_version: str
    mode: RunMode = RunMode.PUSH_ONLY
    budget: dict[str, Any] = field(default_factory=dict)
    run_id: str | None = None

    def resolved_run_id(self) -> str:
        if self.run_id:
            return self.run_id
        return f"run_{uuid4().hex[:12]}"


@dataclass(frozen=True)
class EventInput:
    run_id: str
    event_type: EventType
    payload: dict[str, Any]
    idempotency_key: str
