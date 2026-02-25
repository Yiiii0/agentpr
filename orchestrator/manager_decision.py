from __future__ import annotations

from dataclasses import dataclass, field
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
        return ManagerAction(
            kind=ManagerActionKind.WAIT_HUMAN,
            reason="awaiting PR gate decision",
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
        return ManagerAction(
            kind=ManagerActionKind.START_IMPLEMENTATION,
            reason="plan is ready; start implementation",
        )

    if state in {RunState.IMPLEMENTING, RunState.ITERATING}:
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

    if state in {RunState.CI_WAIT, RunState.REVIEW_WAIT}:
        return ManagerAction(
            kind=ManagerActionKind.SYNC_GITHUB,
            reason="ci/review waiting states should sync github",
        )

    return ManagerAction(
        kind=ManagerActionKind.WAIT_HUMAN,
        reason=f"unsupported manager state: {state.value}",
    )
