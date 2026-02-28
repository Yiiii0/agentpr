"""Shared CLI utility functions used across cli submodules."""

from __future__ import annotations

import argparse
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from . import runtime_analysis as rt
from .models import RunState

# ---------------------------------------------------------------------------
# Tiny helpers
# ---------------------------------------------------------------------------


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2))


def tail(text: str, lines: int = 20) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    chunks = stripped.splitlines()
    return "\n".join(chunks[-lines:])


# ---------------------------------------------------------------------------
# Prompt / text loading
# ---------------------------------------------------------------------------


def load_prompt(args: argparse.Namespace) -> str:
    if args.prompt is not None:
        return args.prompt
    prompt_file = args.prompt_file
    if prompt_file is None:
        raise ValueError("Either --prompt or --prompt-file is required.")
    try:
        return prompt_file.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Failed to read prompt file {prompt_file}: {exc}") from exc


def load_optional_text(inline: str | None, file_path: Path | None, *, arg_name: str) -> str:
    if inline is not None:
        return inline
    if file_path is None:
        return ""
    try:
        return file_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"Failed to read {arg_name} file {file_path}: {exc}") from exc


def read_text_if_exists(path: Path) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        return path.read_text(encoding="utf-8")
    except OSError:
        return ""


def normalize_text_block(text: str) -> str:
    return str(text or "").strip()


# ---------------------------------------------------------------------------
# Datetime / PR extraction
# ---------------------------------------------------------------------------


def parse_iso_datetime(raw: str) -> datetime:
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid datetime format in request-file: {raw}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


def parse_optional_iso_datetime(value: str | None) -> datetime | None:
    if value is None:
        return None
    raw = str(value).strip()
    if not raw:
        return None
    try:
        parsed = datetime.fromisoformat(raw)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed.astimezone(UTC)


def extract_pr_url(text: str) -> str | None:
    match = re.search(r"https?://[^\s)]+/pull/\d+", text)
    if not match:
        return None
    return match.group(0).rstrip(".,")


def extract_pr_number(text: str) -> int | None:
    url = extract_pr_url(text)
    if url is None:
        return None
    match = re.search(r"/pull/(\d+)$", url)
    if not match:
        return None
    return int(match.group(1))


# ---------------------------------------------------------------------------
# Numeric / stats
# ---------------------------------------------------------------------------


def percentile_ms(values: list[int], p: float) -> int:
    if not values:
        return 0
    ordered = sorted(int(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(0.0, min(1.0, p)) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    if lo == hi:
        return ordered[lo]
    frac = rank - lo
    return int(ordered[lo] + (ordered[hi] - ordered[lo]) * frac)


# ---------------------------------------------------------------------------
# Repo path normalization
# ---------------------------------------------------------------------------

DIFF_IGNORE_RUNTIME_PREFIXES: tuple[str, ...] = (
    ".agentpr_runtime/",
    ".venv/",
    "node_modules/",
    ".tox/",
    ".pytest_cache/",
    ".mypy_cache/",
    ".ruff_cache/",
)
DIFF_IGNORE_RUNTIME_EXACT: set[str] = {
    ".agentpr_runtime",
    ".venv",
    "node_modules",
    ".tox",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
}


def normalize_repo_relpath(path: str) -> str:
    return path.strip().replace("\\", "/")


def is_ignored_runtime_path(path: str) -> bool:
    normalized = normalize_repo_relpath(path)
    if not normalized:
        return True
    if normalized in DIFF_IGNORE_RUNTIME_EXACT:
        return True
    return normalized.startswith(DIFF_IGNORE_RUNTIME_PREFIXES)


# ---------------------------------------------------------------------------
# Thin wrappers delegating to runtime_analysis
# ---------------------------------------------------------------------------


def summarize_command_categories(commands: list[str]) -> dict[str, int]:
    return rt.summarize_command_categories(commands)


def extract_failed_test_commands(event_summary: dict[str, Any]) -> list[str]:
    return rt.extract_failed_test_commands(event_summary)


# ---------------------------------------------------------------------------
# State-aware recommendations
# ---------------------------------------------------------------------------


def recommended_actions_for_state(state: RunState) -> list[str]:
    state_actions: dict[RunState, list[str]] = {
        RunState.QUEUED: [
            "start-discovery --run-id <run_id>",
            "run-prepare --run-id <run_id>",
        ],
        RunState.EXECUTING: [
            "run-agent-step --run-id <run_id> --prompt-file <path> --success-state EXECUTING",
            "run-finish --run-id <run_id> --changes <summary>",
        ],
        RunState.DISCOVERY: [
            "mark-plan-ready --run-id <run_id> --contract-path <path>",
            "run-agent-step --run-id <run_id> --prompt-file <path>",
        ],
        RunState.PLAN_READY: [
            "start-implementation --run-id <run_id>",
            "run-agent-step --run-id <run_id> --prompt-file <path>",
        ],
        RunState.IMPLEMENTING: [
            "run-agent-step --run-id <run_id> --prompt-file <path> --success-state LOCAL_VALIDATING",
        ],
        RunState.LOCAL_VALIDATING: [
            "run-agent-step --run-id <run_id> --prompt-file <path> --success-state LOCAL_VALIDATING",
            "run-finish --run-id <run_id> --changes <summary>",
        ],
        RunState.PUSHED: [
            "request-open-pr --run-id <run_id> --title <title> [--body-file <path>]",
            "or keep push_only and wait for manual PR decision",
        ],
        RunState.CI_WAIT: [
            "sync-github --run-id <run_id>",
            "run-github-webhook (preferred) + sync-github fallback loop",
        ],
        RunState.REVIEW_WAIT: [
            "sync-github --run-id <run_id>",
            "retry --run-id <run_id> --target-state ITERATING",
        ],
        RunState.ITERATING: [
            "run-agent-step --run-id <run_id> --prompt-file <path> --success-state LOCAL_VALIDATING",
            "retry --run-id <run_id> --target-state IMPLEMENTING",
        ],
        RunState.NEEDS_HUMAN_REVIEW: [
            "inspect-run --run-id <run_id>",
            "retry --run-id <run_id> --target-state IMPLEMENTING",
        ],
        RunState.FAILED_RETRYABLE: [
            "retry --run-id <run_id> --target-state IMPLEMENTING",
            "inspect-run --run-id <run_id>",
        ],
        RunState.FAILED: [
            "retry --run-id <run_id> --target-state EXECUTING",
            "inspect-run --run-id <run_id>",
        ],
        RunState.PAUSED: [
            "resume --run-id <run_id> --target-state <state>",
        ],
        RunState.DONE: [],
        RunState.SKIPPED: [],
        RunState.FAILED_TERMINAL: [],
    }
    return state_actions.get(state, [])
