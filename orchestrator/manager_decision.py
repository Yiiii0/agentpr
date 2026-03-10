from __future__ import annotations

import os
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from enum import StrEnum
from typing import Any

from .models import RunState


class ManagerActionKind(StrEnum):
    NOOP = "noop"
    WAIT_HUMAN = "wait_human"
    START_DISCOVERY = "start_discovery"
    RUN_PREPARE = "run_prepare"
    MARK_PLAN_READY = "mark_plan_ready"
    START_IMPLEMENTATION = "start_implementation"
    RUN_AGENT_STEP = "run_agent_step"
    RUN_FINISH = "run_finish"
    RETRY = "retry"
    SYNC_GITHUB = "sync_github"
    RUN_CODE_REVIEW = "run_code_review"
    AUTO_CREATE_PR = "auto_create_pr"
    BUMP_PR_COMMENT = "bump_pr_comment"


@dataclass(frozen=True)
class ManagerRunFacts:
    run_id: str
    owner: str
    repo: str
    state: RunState
    prepare_attempts: int
    has_contract: bool
    contract_uri: str | None
    has_prompt: bool
    pr_number: int | None
    worker_autonomous: bool = False
    latest_worker_grade: str | None = None
    latest_worker_confidence: str | None = None
    review_triage_action: str | None = None  # fix_code | reply_explain | ignore
    latest_failure_reason_code: str | None = None
    retry_should_retry: bool | None = None
    retry_target_state: str | None = None
    state_entered_at: str | None = None
    has_code_review: bool = False
    code_review_verdict: str | None = None  # CLEAN | HAS_ISSUES
    auto_pr_enabled: bool = False
    review_triage_confirmed: bool = False
    pr_bump_count: int = 0
    last_pr_bump_at: str | None = None


@dataclass(frozen=True)
class ManagerAction:
    kind: ManagerActionKind
    reason: str
    metadata: dict[str, Any] = field(default_factory=dict)


_TERMINAL: set[RunState] = {
    RunState.DONE,
    RunState.SKIPPED,
    RunState.FAILED_TERMINAL,
}

_VALID_RETRY_TARGETS: set[str] = {
    RunState.QUEUED.value,
    RunState.EXECUTING.value,
    RunState.ITERATING.value,
    RunState.DISCOVERY.value,
    RunState.IMPLEMENTING.value,
}


def decide_next_action(facts: ManagerRunFacts) -> ManagerAction:
    state = facts.state

    if state in _TERMINAL:
        return ManagerAction(
            kind=ManagerActionKind.NOOP,
            reason="run is terminal",
        )

    if state == RunState.PAUSED:
        return ManagerAction(
            kind=ManagerActionKind.WAIT_HUMAN,
            reason="run is paused",
        )

    if state == RunState.PUSHED:
        if not facts.has_code_review:
            return ManagerAction(
                kind=ManagerActionKind.RUN_CODE_REVIEW,
                reason="code pushed, running deep code review before PR creation",
            )
        if facts.code_review_verdict == "HAS_ISSUES":
            return ManagerAction(
                kind=ManagerActionKind.RETRY,
                reason="code review found issues, worker should fix them",
                metadata={"target_state": "ITERATING"},
            )
        # Code review CLEAN — two modes
        if facts.auto_pr_enabled:
            return ManagerAction(
                kind=ManagerActionKind.AUTO_CREATE_PR,
                reason="code review passed, auto-creating PR (manager will do final assessment)",
            )
        return ManagerAction(
            kind=ManagerActionKind.WAIT_HUMAN,
            reason="code review passed, awaiting human PR gate decision",
        )

    if state == RunState.NEEDS_HUMAN_REVIEW:
        return ManagerAction(
            kind=ManagerActionKind.WAIT_HUMAN,
            reason="run escalated to human review",
        )

    if state == RunState.QUEUED:
        return ManagerAction(
            kind=ManagerActionKind.START_DISCOVERY,
            reason="queued run should enter discovery",
        )

    if state == RunState.DISCOVERY:
        if facts.worker_autonomous:
            if not facts.has_prompt:
                return ManagerAction(
                    kind=ManagerActionKind.WAIT_HUMAN,
                    reason="manager prompt file is missing",
                )
            return ManagerAction(
                kind=ManagerActionKind.RUN_AGENT_STEP,
                reason=(
                    "autonomous worker should execute discovery→implementation"
                    " in one agent step"
                ),
            )
        if facts.prepare_attempts <= 0:
            return ManagerAction(
                kind=ManagerActionKind.RUN_PREPARE,
                reason="prepare has not run in discovery",
            )
        return ManagerAction(
            kind=ManagerActionKind.MARK_PLAN_READY,
            reason="prepare completed; advance to plan ready",
            metadata={"contract_uri": facts.contract_uri},
        )

    if state == RunState.PLAN_READY:
        if facts.worker_autonomous:
            if not facts.has_prompt:
                return ManagerAction(
                    kind=ManagerActionKind.WAIT_HUMAN,
                    reason="manager prompt file is missing",
                )
            return ManagerAction(
                kind=ManagerActionKind.RUN_AGENT_STEP,
                reason=(
                    "autonomous worker should execute plan→implementation"
                    " in one agent step"
                ),
            )
        return ManagerAction(
            kind=ManagerActionKind.START_IMPLEMENTATION,
            reason="plan is ready; start implementation",
        )

    if state == RunState.EXECUTING:
        if facts.latest_worker_grade == "PASS":
            if facts.latest_worker_confidence == "low":
                return ManagerAction(
                    kind=ManagerActionKind.WAIT_HUMAN,
                    reason="worker PASS but low confidence; escalating for human review",
                )
            return ManagerAction(
                kind=ManagerActionKind.RUN_FINISH,
                reason="worker pass evidence found; execute finish/push",
            )
        if facts.latest_worker_grade == "NEEDS_REVIEW":
            return ManagerAction(
                kind=ManagerActionKind.WAIT_HUMAN,
                reason="worker output needs human review",
            )
        if not facts.has_prompt:
            return ManagerAction(
                kind=ManagerActionKind.WAIT_HUMAN,
                reason="manager prompt file is missing",
            )
        return ManagerAction(
            kind=ManagerActionKind.RUN_AGENT_STEP,
            reason="executing stage requires worker execution",
        )

    if state == RunState.ITERATING:
        if facts.review_triage_action == "ignore":
            return ManagerAction(
                kind=ManagerActionKind.WAIT_HUMAN,
                reason="review comment triaged as ignorable; no action needed",
            )
        if facts.review_triage_action == "reply_explain":
            return ManagerAction(
                kind=ManagerActionKind.WAIT_HUMAN,
                reason="review comment needs human reply (not a code fix)",
            )
        if facts.review_triage_action == "fix_code" and not facts.review_triage_confirmed:
            return ManagerAction(
                kind=ManagerActionKind.WAIT_HUMAN,
                reason="triage says fix_code, awaiting human confirmation (/approve_triage)",
            )
        if not facts.has_prompt:
            return ManagerAction(
                kind=ManagerActionKind.WAIT_HUMAN,
                reason="manager prompt file is missing",
            )
        return ManagerAction(
            kind=ManagerActionKind.RUN_AGENT_STEP,
            reason="iterating: review comment requires code fix (confirmed)",
        )

    if state == RunState.IMPLEMENTING:
        if not facts.has_prompt:
            return ManagerAction(
                kind=ManagerActionKind.WAIT_HUMAN,
                reason="manager prompt file is missing",
            )
        return ManagerAction(
            kind=ManagerActionKind.RUN_AGENT_STEP,
            reason="implementation stage requires worker execution",
        )

    if state == RunState.LOCAL_VALIDATING:
        return ManagerAction(
            kind=ManagerActionKind.RUN_FINISH,
            reason="local validation stage should converge via finish/push",
        )

    if state == RunState.FAILED_RETRYABLE:
        target = (
            RunState.DISCOVERY
            if facts.prepare_attempts <= 0 or not facts.has_contract
            else RunState.IMPLEMENTING
        )
        return ManagerAction(
            kind=ManagerActionKind.RETRY,
            reason="retryable failure should be retried",
            metadata={"target_state": target.value},
        )

    if state == RunState.FAILED:
        if facts.retry_should_retry is False:
            return ManagerAction(
                kind=ManagerActionKind.WAIT_HUMAN,
                reason="LLM diagnosis: retry not worthwhile",
            )
        # Validate LLM-provided target_state; default to EXECUTING
        target_raw = (facts.retry_target_state or "").strip().upper()
        if target_raw not in _VALID_RETRY_TARGETS:
            target_raw = RunState.EXECUTING.value
        return ManagerAction(
            kind=ManagerActionKind.RETRY,
            reason="failed run should retry",
            metadata={"target_state": target_raw},
        )

    if state in {RunState.CI_WAIT, RunState.REVIEW_WAIT}:
        # Stale state detection + PR bump automation
        if facts.state_entered_at:
            try:
                entered = datetime.fromisoformat(facts.state_entered_at)
                if entered.tzinfo is None:
                    entered = entered.replace(tzinfo=UTC)
                age = datetime.now(UTC) - entered

                # CI_WAIT: escalate after 24h
                if state == RunState.CI_WAIT and age > timedelta(hours=24):
                    return ManagerAction(
                        kind=ManagerActionKind.WAIT_HUMAN,
                        reason=(
                            f"stale CI_WAIT: no progress for "
                            f"{age.total_seconds() / 3600:.1f}h"
                        ),
                    )

                # REVIEW_WAIT: bump after 3 days, escalate after 7 days
                if state == RunState.REVIEW_WAIT:
                    bump_days = int(
                        os.environ.get("AGENTPR_PR_BUMP_DAYS", "3")
                    )
                    if (
                        age > timedelta(days=bump_days)
                        and facts.pr_number is not None
                        and facts.pr_bump_count == 0
                    ):
                        return ManagerAction(
                            kind=ManagerActionKind.BUMP_PR_COMMENT,
                            reason=(
                                f"PR has no response for {age.days}d, "
                                f"posting polite bump comment"
                            ),
                        )
                    if age > timedelta(days=7):
                        return ManagerAction(
                            kind=ManagerActionKind.WAIT_HUMAN,
                            reason=(
                                f"stale REVIEW_WAIT: no progress for "
                                f"{age.total_seconds() / 3600:.1f}h"
                            ),
                        )
            except (ValueError, TypeError):
                pass  # Unparseable timestamp — fall through to sync
        return ManagerAction(
            kind=ManagerActionKind.SYNC_GITHUB,
            reason="ci/review waiting states should sync github",
        )

    return ManagerAction(
        kind=ManagerActionKind.WAIT_HUMAN,
        reason=f"unsupported manager state: {state.value}",
    )


def allowed_action_kinds(facts: ManagerRunFacts) -> tuple[ManagerActionKind, ...]:
    state = facts.state

    if state in _TERMINAL:
        return (ManagerActionKind.NOOP,)

    if state in {RunState.PAUSED, RunState.NEEDS_HUMAN_REVIEW}:
        return (ManagerActionKind.WAIT_HUMAN,)

    if state == RunState.PUSHED:
        if not facts.has_code_review:
            return (ManagerActionKind.RUN_CODE_REVIEW, ManagerActionKind.WAIT_HUMAN)
        if facts.code_review_verdict == "HAS_ISSUES":
            return (ManagerActionKind.RETRY, ManagerActionKind.WAIT_HUMAN)
        if facts.auto_pr_enabled:
            return (ManagerActionKind.AUTO_CREATE_PR, ManagerActionKind.WAIT_HUMAN)
        return (ManagerActionKind.WAIT_HUMAN,)

    if state == RunState.QUEUED:
        return (ManagerActionKind.START_DISCOVERY, ManagerActionKind.WAIT_HUMAN)

    if state == RunState.DISCOVERY:
        if facts.worker_autonomous:
            if not facts.has_prompt:
                return (ManagerActionKind.WAIT_HUMAN,)
            return (ManagerActionKind.RUN_AGENT_STEP, ManagerActionKind.WAIT_HUMAN)
        if facts.prepare_attempts <= 0:
            return (ManagerActionKind.RUN_PREPARE, ManagerActionKind.WAIT_HUMAN)
        return (ManagerActionKind.MARK_PLAN_READY, ManagerActionKind.WAIT_HUMAN)

    if state == RunState.PLAN_READY:
        if facts.worker_autonomous:
            if not facts.has_prompt:
                return (ManagerActionKind.WAIT_HUMAN,)
            return (ManagerActionKind.RUN_AGENT_STEP, ManagerActionKind.WAIT_HUMAN)
        return (ManagerActionKind.START_IMPLEMENTATION, ManagerActionKind.WAIT_HUMAN)

    if state == RunState.EXECUTING:
        if facts.latest_worker_grade == "PASS":
            if facts.latest_worker_confidence == "low":
                return (ManagerActionKind.RUN_FINISH, ManagerActionKind.WAIT_HUMAN)
            return (ManagerActionKind.RUN_FINISH, ManagerActionKind.WAIT_HUMAN)
        if facts.latest_worker_grade == "NEEDS_REVIEW":
            return (ManagerActionKind.WAIT_HUMAN, ManagerActionKind.RUN_AGENT_STEP)
        if not facts.has_prompt:
            return (ManagerActionKind.WAIT_HUMAN,)
        return (ManagerActionKind.RUN_AGENT_STEP, ManagerActionKind.WAIT_HUMAN)

    if state == RunState.ITERATING:
        if facts.review_triage_action in {"ignore", "reply_explain"}:
            return (ManagerActionKind.WAIT_HUMAN, ManagerActionKind.RUN_AGENT_STEP)
        if not facts.has_prompt:
            return (ManagerActionKind.WAIT_HUMAN,)
        return (ManagerActionKind.RUN_AGENT_STEP, ManagerActionKind.WAIT_HUMAN)

    if state == RunState.IMPLEMENTING:
        if not facts.has_prompt:
            return (ManagerActionKind.WAIT_HUMAN,)
        return (ManagerActionKind.RUN_AGENT_STEP, ManagerActionKind.WAIT_HUMAN)

    if state == RunState.LOCAL_VALIDATING:
        return (ManagerActionKind.RUN_FINISH, ManagerActionKind.WAIT_HUMAN)

    if state == RunState.FAILED_RETRYABLE:
        return (ManagerActionKind.RETRY, ManagerActionKind.WAIT_HUMAN)

    if state == RunState.FAILED:
        if facts.retry_should_retry is False:
            return (ManagerActionKind.WAIT_HUMAN, ManagerActionKind.RETRY)
        return (ManagerActionKind.RETRY, ManagerActionKind.WAIT_HUMAN)

    if state in {RunState.CI_WAIT, RunState.REVIEW_WAIT}:
        return (
            ManagerActionKind.SYNC_GITHUB,
            ManagerActionKind.BUMP_PR_COMMENT,
            ManagerActionKind.WAIT_HUMAN,
        )

    return (ManagerActionKind.WAIT_HUMAN,)
