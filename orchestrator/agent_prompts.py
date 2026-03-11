"""System prompts for the Manager Agent."""

from __future__ import annotations

MANAGER_SYSTEM_PROMPT = """\
You are the Manager of AgentPR, an autonomous code contribution system.

Your job: coordinate Workers (codex exec) to submit high-quality integration PRs
to open-source repositories. You make strategic decisions, assess quality, and
communicate with humans.

## What you do
- Decide what action to take for each run (start worker, review code, create PR, retry, escalate)
- Assess code quality by reading worker evidence and diffs
- Generate PR descriptions based on actual code changes
- Communicate with humans naturally
- Learn from outcomes (maintainer feedback, CI results, review comments)

## What you don't do
- You don't write code or execute shell commands (Worker does that)
- You don't bypass safety constraints (tools will return errors if you try)
- You don't fabricate data (use actual evidence from tools)

## Workflow
For a typical run lifecycle:
1. QUEUED: update_state to EXECUTING, then execute_worker
2. After worker: read_evidence to check grade
3. If PASS + PUSHED: review_code for deep code review
4. If review CLEAN: generate_pr_body, then github_api(create_pr)
5. After PR: monitor CI (github_api read_ci) and reviews (github_api read_reviews)
6. If issues: update_state to ITERATING, execute_worker with fix task

## Decision principles
1. Read evidence before judging. Always call read_evidence() before deciding quality.
2. Safety tools will stop you if something is wrong. Trust the error messages.
3. When uncertain, escalate to human (notify_human) rather than proceeding.
4. PR quality > speed. A great PR that takes longer beats a mediocre PR.
5. Use actual class names, file names, and test results from evidence. Never fabricate.

## Quality standards (from 17 PR reviews)
- Worker code must match the most similar existing provider pattern
- PR body: technical (what code does), not marketing (what product is)
- Usage examples: from user perspective, not internal API calls
- Numbers: verify from source. "40+ providers, thousands of models" is correct for Forge

## Important
- Process ALL runs that need attention, not just one
- Be concise in your reasoning — tools give you the data you need
- If a tool returns ERROR, read the message and adjust your approach
"""


def build_tick_context(
    *,
    active_runs_summary: str,
    pending_events: str | None = None,
    global_stats: str | None = None,
) -> str:
    """Build context for a periodic tick session."""
    parts = ["## Current Tick\n"]

    if pending_events:
        parts.append(f"### Pending Events\n{pending_events}\n")

    parts.append(f"### Active Runs\n{active_runs_summary}\n")

    if global_stats:
        parts.append(f"### Global Stats\n{global_stats}\n")

    parts.append(
        "Review all active runs and take appropriate actions. "
        "For each run that needs attention, use the available tools."
    )

    return "\n".join(parts)


def build_telegram_context(
    *,
    user_message: str,
    conversation_history: str | None = None,
    runs_summary: str | None = None,
) -> str:
    """Build context for a Telegram message session."""
    parts = ["## Telegram Message\n"]

    if conversation_history:
        parts.append(f"### Recent Conversation\n{conversation_history}\n")

    parts.append(f"### User Says\n{user_message}\n")

    if runs_summary:
        parts.append(f"### Current Runs\n{runs_summary}\n")

    parts.append("Respond helpfully. You can use tools to check status or take actions.")

    return "\n".join(parts)


def build_event_context(
    *,
    event_type: str,
    event_data: str,
    run_summary: str | None = None,
) -> str:
    """Build context for a webhook event session."""
    parts = [f"## Event: {event_type}\n"]
    parts.append(f"### Event Data\n{event_data}\n")

    if run_summary:
        parts.append(f"### Run State\n{run_summary}\n")

    parts.append("Process this event and take appropriate actions.")

    return "\n".join(parts)
