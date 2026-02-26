from __future__ import annotations

from dataclasses import dataclass

from .models import RunState


class InvalidTransitionError(ValueError):
    pass


_ALLOWED_TRANSITIONS: dict[RunState, set[RunState]] = {
    RunState.QUEUED: {
        RunState.EXECUTING,
        RunState.DISCOVERY,
        RunState.PAUSED,
        RunState.SKIPPED,
        RunState.FAILED,
        RunState.FAILED_TERMINAL,
    },
    RunState.EXECUTING: {
        RunState.PUSHED,
        RunState.PAUSED,
        RunState.FAILED,
        RunState.NEEDS_HUMAN_REVIEW,
    },
    RunState.DISCOVERY: {
        RunState.PLAN_READY,
        RunState.PAUSED,
        RunState.SKIPPED,
        RunState.FAILED,
        RunState.FAILED_RETRYABLE,
        RunState.FAILED_TERMINAL,
        RunState.NEEDS_HUMAN_REVIEW,
    },
    RunState.PLAN_READY: {
        RunState.IMPLEMENTING,
        RunState.PAUSED,
        RunState.SKIPPED,
        RunState.FAILED,
        RunState.NEEDS_HUMAN_REVIEW,
    },
    RunState.IMPLEMENTING: {
        RunState.LOCAL_VALIDATING,
        RunState.PAUSED,
        RunState.FAILED,
        RunState.FAILED_RETRYABLE,
        RunState.FAILED_TERMINAL,
        RunState.NEEDS_HUMAN_REVIEW,
    },
    RunState.LOCAL_VALIDATING: {
        RunState.PUSHED,
        RunState.PAUSED,
        RunState.FAILED,
        RunState.FAILED_RETRYABLE,
        RunState.FAILED_TERMINAL,
        RunState.NEEDS_HUMAN_REVIEW,
    },
    RunState.PUSHED: {
        RunState.CI_WAIT,
        RunState.PAUSED,
        RunState.FAILED,
        RunState.NEEDS_HUMAN_REVIEW,
        RunState.DONE,
    },
    RunState.CI_WAIT: {
        RunState.REVIEW_WAIT,
        RunState.ITERATING,
        RunState.PAUSED,
        RunState.FAILED,
        RunState.FAILED_RETRYABLE,
        RunState.FAILED_TERMINAL,
        RunState.NEEDS_HUMAN_REVIEW,
    },
    RunState.REVIEW_WAIT: {
        RunState.ITERATING,
        RunState.PAUSED,
        RunState.DONE,
        RunState.FAILED,
        RunState.FAILED_RETRYABLE,
        RunState.NEEDS_HUMAN_REVIEW,
    },
    RunState.ITERATING: {
        RunState.IMPLEMENTING,
        RunState.LOCAL_VALIDATING,
        RunState.PAUSED,
        RunState.FAILED,
        RunState.FAILED_RETRYABLE,
        RunState.FAILED_TERMINAL,
        RunState.NEEDS_HUMAN_REVIEW,
    },
    RunState.PAUSED: {
        RunState.DISCOVERY,
        RunState.PLAN_READY,
        RunState.IMPLEMENTING,
        RunState.LOCAL_VALIDATING,
        RunState.PUSHED,
        RunState.CI_WAIT,
        RunState.REVIEW_WAIT,
        RunState.ITERATING,
        RunState.EXECUTING,
        RunState.NEEDS_HUMAN_REVIEW,
        RunState.FAILED,
        RunState.SKIPPED,
        RunState.FAILED_TERMINAL,
    },
    RunState.NEEDS_HUMAN_REVIEW: {
        RunState.EXECUTING,
        RunState.IMPLEMENTING,
        RunState.ITERATING,
        RunState.PAUSED,
        RunState.FAILED,
        RunState.SKIPPED,
        RunState.DONE,
        RunState.FAILED_TERMINAL,
    },
    RunState.FAILED: {
        RunState.EXECUTING,
        RunState.PAUSED,
        RunState.NEEDS_HUMAN_REVIEW,
        RunState.FAILED_TERMINAL,
    },
    RunState.FAILED_RETRYABLE: {
        RunState.EXECUTING,
        RunState.DISCOVERY,
        RunState.IMPLEMENTING,
        RunState.LOCAL_VALIDATING,
        RunState.ITERATING,
        RunState.FAILED,
        RunState.NEEDS_HUMAN_REVIEW,
        RunState.SKIPPED,
        RunState.FAILED_TERMINAL,
    },
    RunState.DONE: set(),
    RunState.SKIPPED: set(),
    RunState.FAILED_TERMINAL: set(),
}

_TERMINAL_STATES: set[RunState] = {
    RunState.DONE,
    RunState.SKIPPED,
    RunState.FAILED_TERMINAL,
}


def can_transition(source: RunState, target: RunState) -> bool:
    return target in _ALLOWED_TRANSITIONS[source]


def assert_transition(source: RunState, target: RunState) -> None:
    if source == target:
        return
    if not can_transition(source, target):
        raise InvalidTransitionError(
            f"Illegal state transition: {source.value} -> {target.value}"
        )


def is_terminal(state: RunState) -> bool:
    return state in _TERMINAL_STATES


def allowed_targets(state: RunState) -> list[RunState]:
    return sorted(_ALLOWED_TRANSITIONS[state], key=lambda s: s.value)


@dataclass(frozen=True)
class Transition:
    source: RunState
    target: RunState
