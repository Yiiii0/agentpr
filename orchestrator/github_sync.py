from __future__ import annotations

from dataclasses import dataclass
from typing import Any


FAILURE_CONCLUSIONS: set[str] = {
    "failure",
    "timed_out",
    "cancelled",
    "action_required",
    "startup_failure",
    "stale",
}
SUCCESS_CONCLUSIONS: set[str] = {"success", "neutral", "skipped"}
PENDING_STATES: set[str] = {"queued", "in_progress", "pending", "waiting", "requested"}
FAILURE_STATES: set[str] = {"failure", "error"}


@dataclass(frozen=True)
class CheckSummary:
    total: int
    successes: int
    failures: int
    pending: int
    unknown: int


@dataclass(frozen=True)
class GitHubSyncDecision:
    check_conclusion: str | None
    review_state: str | None
    check_summary: CheckSummary


def build_sync_decision(pr_view_payload: dict[str, Any]) -> GitHubSyncDecision:
    check_summary = summarize_status_checks(pr_view_payload.get("statusCheckRollup"))
    check_conclusion = decide_check_conclusion(check_summary)
    review_state = decide_review_state(pr_view_payload)
    return GitHubSyncDecision(
        check_conclusion=check_conclusion,
        review_state=review_state,
        check_summary=check_summary,
    )


def summarize_status_checks(raw_rollup: Any) -> CheckSummary:
    rollup = raw_rollup if isinstance(raw_rollup, list) else []
    successes = 0
    failures = 0
    pending = 0
    unknown = 0

    for item in rollup:
        if not isinstance(item, dict):
            unknown += 1
            continue
        conclusion = normalize_token(item.get("conclusion"))
        state = normalize_token(item.get("state"))
        if conclusion in FAILURE_CONCLUSIONS or state in FAILURE_STATES:
            failures += 1
            continue
        if conclusion in SUCCESS_CONCLUSIONS:
            successes += 1
            continue
        if state in PENDING_STATES:
            pending += 1
            continue
        unknown += 1

    return CheckSummary(
        total=len(rollup),
        successes=successes,
        failures=failures,
        pending=pending,
        unknown=unknown,
    )


def decide_check_conclusion(summary: CheckSummary) -> str | None:
    if summary.failures > 0:
        return "failure"
    if summary.pending > 0:
        return None
    if summary.total > 0 and summary.successes + summary.unknown == summary.total:
        return "success"
    return None


def decide_review_state(pr_view_payload: dict[str, Any]) -> str | None:
    review_decision = normalize_token(pr_view_payload.get("reviewDecision"))
    if review_decision == "changes_requested":
        return "changes_requested"

    reviews = pr_view_payload.get("reviews")
    if not isinstance(reviews, list):
        return None
    for item in reversed(reviews):
        if not isinstance(item, dict):
            continue
        state = normalize_token(item.get("state"))
        if state == "changes_requested":
            return "changes_requested"
        if state in {"approved", "commented", "dismissed"}:
            return state
    return None


def normalize_token(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip().lower()

