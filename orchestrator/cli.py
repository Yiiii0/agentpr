from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import statistics
import subprocess
import sys
import time
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .db import Database
from .executor import ScriptExecutor
from .github_webhook import run_github_webhook_server
from .github_sync import build_sync_decision
from .manager_loop import ManagerLoopConfig, ManagerLoopRunner
from .models import AgentRuntimeGrade, RunCreateInput, RunMode, RunState, StepName
from .manager_policy import load_manager_policy, resolve_run_agent_effective_policy
from .preflight import PreflightChecker, RuntimeDoctor
from . import runtime_analysis as rt
from .service import OrchestratorService, RunNotFoundError
from .skills import (
    AGENTPR_REQUIRED_SKILLS,
    OPTIONAL_CURATED_CI_SKILLS,
    build_skill_plan,
    build_task_packet,
    discover_installed_skills,
    install_local_skills,
    list_local_skill_dirs,
    load_user_task_packet,
    render_skill_chain_prompt,
    resolve_codex_home,
    resolve_codex_skills_root,
)
from .state_machine import InvalidTransitionError, allowed_targets
from .telegram_bot import TelegramClient, run_telegram_bot_loop

PROJECT_ROOT = Path(__file__).resolve().parents[1]


def load_dotenv(path: Path) -> None:
    if not path.exists() or not path.is_file():
        return
    raw_lines = path.read_text(encoding="utf-8").splitlines()
    for raw in raw_lines:
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export ") :].strip()
        if "=" not in line:
            continue
        key_raw, value_raw = line.split("=", 1)
        key = key_raw.strip()
        if not key or key.startswith("#"):
            continue
        value = value_raw.strip()
        if value and value[0] in {'"', "'"} and value[-1:] == value[0]:
            value = value[1:-1]
        if key not in os.environ:
            os.environ[key] = value


load_dotenv(PROJECT_ROOT / ".env")

DEFAULT_DB_PATH = PROJECT_ROOT / "orchestrator" / "data" / "agentpr.db"
DEFAULT_WORKSPACE_ROOT = Path(
    os.environ.get("AGENTPR_BASE_DIR", str(PROJECT_ROOT / "workspaces"))
)
DEFAULT_INTEGRATION_ROOT = PROJECT_ROOT / "forge_integration"
DEFAULT_SKILLS_SOURCE_ROOT = PROJECT_ROOT / "skills"
DEFAULT_POLICY_PATH = PROJECT_ROOT / "orchestrator" / "manager_policy.json"


def resolve_default_worker_prompt_file() -> Path:
    raw = str(os.environ.get("AGENTPR_WORKER_PROMPT_FILE") or "").strip()
    if raw:
        candidate = Path(raw).expanduser()
        if not candidate.is_absolute():
            candidate = PROJECT_ROOT / candidate
        return candidate.resolve()
    return (DEFAULT_INTEGRATION_ROOT / "claude_code_prompt.md").resolve()


DEFAULT_WORKER_PROMPT_FILE = resolve_default_worker_prompt_file()
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


def _normalize_repo_relpath(path: str) -> str:
    return path.strip().replace("\\", "/")


def _is_ignored_runtime_path(path: str) -> bool:
    normalized = _normalize_repo_relpath(path)
    if not normalized:
        return True
    if normalized in DIFF_IGNORE_RUNTIME_EXACT:
        return True
    return normalized.startswith(DIFF_IGNORE_RUNTIME_PREFIXES)


def add_idempotency_arg(command_parser: argparse.ArgumentParser) -> None:
    command_parser.add_argument(
        "--idempotency-key",
        help="Optional idempotency key. Provide this for retriable command callers.",
    )


def add_manager_common_args(command_parser: argparse.ArgumentParser) -> None:
    command_parser.add_argument("--run-id", help="Optional single run id target.")
    command_parser.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Max runs scanned when --run-id is omitted (default: 20).",
    )
    command_parser.add_argument(
        "--max-actions-per-run",
        type=int,
        default=4,
        help="Max manager actions executed per run in one tick (default: 4).",
    )
    command_parser.add_argument(
        "--prompt-file",
        type=Path,
        default=DEFAULT_WORKER_PROMPT_FILE,
        help=(
            "Prompt file used by run-agent-step actions "
            f"(default: {DEFAULT_WORKER_PROMPT_FILE}; "
            "env override: AGENTPR_WORKER_PROMPT_FILE)."
        ),
    )
    command_parser.add_argument(
        "--contract-template-file",
        type=Path,
        help="Optional template copied into auto-generated contract files.",
    )
    command_parser.add_argument(
        "--disable-auto-contract",
        action="store_true",
        help="Disable automatic contract materialization when contract artifact is missing.",
    )
    command_parser.add_argument(
        "--changes",
        default="Automated integration updates from manager loop.",
        help="Default --changes value for run-finish.",
    )
    command_parser.add_argument(
        "--commit-title",
        help="Optional default commit title for run-finish.",
    )
    command_parser.add_argument(
        "--skills-mode",
        choices=["off", "agentpr"],
        help="Optional skills mode override passed to run-agent-step.",
    )
    command_parser.add_argument(
        "--codex-sandbox",
        choices=["read-only", "workspace-write", "danger-full-access"],
        help="Optional codex sandbox override passed to run-agent-step.",
    )
    command_parser.add_argument(
        "--agent-arg",
        action="append",
        default=[],
        help="Extra argument forwarded to run-agent-step (repeatable).",
    )
    command_parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Plan actions without executing them.",
    )
    command_parser.add_argument(
        "--decision-mode",
        choices=["rules", "llm", "hybrid"],
        default="rules",
        help="Manager decision strategy (default: rules).",
    )
    command_parser.add_argument(
        "--manager-api-base",
        help=(
            "OpenAI-compatible API base for manager LLM "
            "(default: AGENTPR_MANAGER_API_BASE or https://api.openai.com/v1)."
        ),
    )
    command_parser.add_argument(
        "--manager-model",
        help="Manager LLM model (default: AGENTPR_MANAGER_MODEL or gpt-4o-mini).",
    )
    command_parser.add_argument(
        "--manager-timeout-sec",
        type=int,
        default=20,
        help="Manager LLM HTTP timeout seconds (default: 20).",
    )
    command_parser.add_argument(
        "--manager-api-key-env",
        default="AGENTPR_MANAGER_API_KEY",
        help=(
            "Env var name storing manager API key "
            "(default: AGENTPR_MANAGER_API_KEY)."
        ),
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="AgentPR orchestrator CLI")
    parser.add_argument(
        "--db",
        type=Path,
        default=DEFAULT_DB_PATH,
        help=f"SQLite db path (default: {DEFAULT_DB_PATH})",
    )
    parser.add_argument(
        "--workspace-root",
        type=Path,
        default=DEFAULT_WORKSPACE_ROOT,
        help=f"Workspace root (default: {DEFAULT_WORKSPACE_ROOT})",
    )
    parser.add_argument(
        "--integration-root",
        type=Path,
        default=DEFAULT_INTEGRATION_ROOT,
        help=f"forge_integration root (default: {DEFAULT_INTEGRATION_ROOT})",
    )
    parser.add_argument(
        "--policy-file",
        type=Path,
        default=DEFAULT_POLICY_PATH,
        help=f"Manager policy JSON path (default: {DEFAULT_POLICY_PATH})",
    )
    parser.add_argument(
        "--skip-doctor",
        action="store_true",
        help="Skip automatic startup doctor gate for mutable commands.",
    )

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("init-db", help="Initialize sqlite schema")

    c = sub.add_parser("create-run", help="Create a run in QUEUED state")
    c.add_argument("--owner", required=True)
    c.add_argument("--repo", required=True)
    c.add_argument("--prompt-version", required=True)
    c.add_argument("--run-id")
    c.add_argument(
        "--mode",
        choices=[RunMode.PUSH_ONLY.value],
        default=RunMode.PUSH_ONLY.value,
    )
    c.add_argument(
        "--budget-json",
        default="{}",
        help='JSON object, e.g. \'{"max_run_minutes": 90}\'',
    )

    l = sub.add_parser("list-runs", help="List recent runs")
    l.add_argument("--limit", type=int, default=50)

    s = sub.add_parser("show-run", help="Show single run snapshot")
    s.add_argument("--run-id", required=True)

    d = sub.add_parser("start-discovery", help="Move to DISCOVERY")
    d.add_argument("--run-id", required=True)
    add_idempotency_arg(d)

    p = sub.add_parser("run-prepare", help="Execute prepare.sh")
    p.add_argument("--run-id", required=True)
    p.add_argument("--base-branch")
    p.add_argument("--feature-branch")

    mp = sub.add_parser("mark-plan-ready", help="Mark discovery completed")
    mp.add_argument("--run-id", required=True)
    mp.add_argument("--contract-path", required=True)
    add_idempotency_arg(mp)

    i = sub.add_parser("start-implementation", help="Move to IMPLEMENTING")
    i.add_argument("--run-id", required=True)
    add_idempotency_arg(i)

    lv = sub.add_parser("mark-local-validated", help="Move to LOCAL_VALIDATING")
    lv.add_argument("--run-id", required=True)
    add_idempotency_arg(lv)

    pf = sub.add_parser("run-preflight", help="Run environment preflight checks")
    pf.add_argument("--run-id", required=True)
    pf.add_argument(
        "--skip-network-check",
        action="store_true",
        help="Skip package registry network checks.",
    )
    pf.add_argument(
        "--network-timeout-sec",
        type=int,
        default=5,
        help="Timeout for each network check (seconds).",
    )
    pf.add_argument(
        "--codex-sandbox",
        choices=["read-only", "workspace-write", "danger-full-access"],
        default="danger-full-access",
        help="Sandbox policy to validate in preflight.",
    )

    dc = sub.add_parser("doctor", help="Run startup environment doctor checks")
    dc.add_argument(
        "--skip-network-check",
        action="store_true",
        help="Skip outbound connectivity checks.",
    )
    dc.add_argument(
        "--network-timeout-sec",
        type=int,
        default=5,
        help="Timeout for each doctor network check (seconds).",
    )
    dc.add_argument(
        "--no-require-gh-auth",
        action="store_true",
        help="Do not require gh auth status to pass.",
    )
    dc.add_argument(
        "--require-codex",
        action="store_true",
        help="Require codex binary and package registries reachability.",
    )
    dc.add_argument(
        "--require-telegram-token",
        action="store_true",
        help="Require AGENTPR_TELEGRAM_BOT_TOKEN to be present.",
    )
    dc.add_argument(
        "--require-webhook-secret",
        action="store_true",
        help="Require AGENTPR_GITHUB_WEBHOOK_SECRET to be present.",
    )

    ag = sub.add_parser(
        "run-agent-step",
        help="Execute non-interactive codex command inside repo workspace",
    )
    ag.add_argument("--run-id", required=True)
    prompt_group = ag.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt", help="Prompt text")
    prompt_group.add_argument("--prompt-file", type=Path, help="Prompt file path")
    ag.add_argument(
        "--agent-arg",
        action="append",
        default=[],
        help="Extra argument appended to the engine command. Can be repeated.",
    )
    ag.add_argument(
        "--success-state",
        choices=[
            RunState.LOCAL_VALIDATING.value,
            RunState.NEEDS_HUMAN_REVIEW.value,
            "UNCHANGED",
        ],
        help=(
            "Optional state convergence after a successful agent run. "
            "Supported: LOCAL_VALIDATING, NEEDS_HUMAN_REVIEW, UNCHANGED. "
            "If omitted, uses manager policy default."
        ),
    )
    ag.add_argument(
        "--on-retryable-state",
        choices=[
            RunState.FAILED_RETRYABLE.value,
            RunState.NEEDS_HUMAN_REVIEW.value,
            "UNCHANGED",
        ],
        help=(
            "State convergence when runtime classification is RETRYABLE. "
            "If omitted, uses manager policy default."
        ),
    )
    ag.add_argument(
        "--on-human-review-state",
        choices=[
            RunState.NEEDS_HUMAN_REVIEW.value,
            RunState.FAILED_RETRYABLE.value,
            "UNCHANGED",
        ],
        help=(
            "State convergence when runtime classification is HUMAN_REVIEW. "
            "If omitted, uses manager policy default."
        ),
    )
    ag.add_argument(
        "--skip-preflight",
        action="store_true",
        help="Skip environment preflight checks before launching codex.",
    )
    ag.add_argument(
        "--skip-network-check",
        action="store_true",
        help="Skip package registry network checks during preflight.",
    )
    ag.add_argument(
        "--network-timeout-sec",
        type=int,
        default=5,
        help="Timeout for each preflight network check (seconds).",
    )
    ag.add_argument(
        "--codex-sandbox",
        choices=["read-only", "workspace-write", "danger-full-access"],
        help=(
            "Codex sandbox mode for this run. "
            "If omitted, uses manager policy default."
        ),
    )
    ag.add_argument(
        "--codex-model",
        help="Optional codex model override (e.g., gpt-5.3-codex).",
    )
    ag.add_argument(
        "--no-codex-full-auto",
        action="store_true",
        help="Disable codex --full-auto.",
    )
    ag.add_argument(
        "--max-agent-seconds",
        type=int,
        default=None,
        help=(
            "Hard timeout for a single codex run-agent-step execution. "
            "Set 0 to disable timeout. If omitted, uses manager policy default."
        ),
    )
    ag.add_argument(
        "--allow-agent-push",
        action="store_true",
        help="Allow agent to run commit/push during run-agent-step (default: disabled).",
    )
    ag.add_argument(
        "--allow-read-path",
        action="append",
        default=[],
        help=(
            "Additional external read-only path available to worker during run-agent-step. "
            "Can be repeated."
        ),
    )
    ag.add_argument(
        "--max-changed-files",
        type=int,
        help=(
            "Diff budget: max changed files before forcing HUMAN_REVIEW. "
            "If omitted, uses manager policy default."
        ),
    )
    ag.add_argument(
        "--max-added-lines",
        type=int,
        help=(
            "Diff budget: max added lines before forcing HUMAN_REVIEW. "
            "If omitted, uses manager policy default."
        ),
    )
    ag.add_argument(
        "--max-retryable-attempts",
        type=int,
        help=(
            "Escalate RETRYABLE verdicts to HUMAN_REVIEW when agent attempt_no "
            "exceeds this value. If omitted, uses manager policy default."
        ),
    )
    ag.add_argument(
        "--min-test-commands",
        type=int,
        help=(
            "Minimum number of test/lint evidence commands required in "
            "IMPLEMENTING/LOCAL_VALIDATING/ITERATING on successful exit. "
            "If omitted, uses manager policy default."
        ),
    )
    ag.add_argument(
        "--allow-dirty-worktree",
        action="store_true",
        help=(
            "Allow starting agent step with pre-existing local changes in workspace "
            "(default: blocked in DISCOVERY/IMPLEMENTING)."
        ),
    )
    ag.add_argument(
        "--skills-mode",
        choices=["off", "agentpr"],
        help=(
            "Enable skills-based prompt envelope for this run. "
            "agentpr mode invokes stage-specific AgentPR skills. "
            "If omitted, uses manager policy default."
        ),
    )
    ag.add_argument(
        "--allow-missing-skills",
        action="store_true",
        help=(
            "Do not hard-fail when required skills are missing; "
            "continue with best-effort prompt envelope."
        ),
    )
    ag.add_argument(
        "--task-packet-file",
        type=Path,
        help=(
            "Optional JSON/Markdown payload merged into generated task packet "
            "when skills-mode is enabled."
        ),
    )

    f = sub.add_parser("run-finish", help="Execute finish.sh")
    f.add_argument("--run-id", required=True)
    f.add_argument("--changes", required=True)
    f.add_argument("--project")
    f.add_argument("--commit-title")

    ro = sub.add_parser(
        "request-open-pr",
        help="Create a pending PR-open request with confirmation token",
    )
    ro.add_argument("--run-id", required=True)
    ro.add_argument("--title", required=True)
    ro_body_group = ro.add_mutually_exclusive_group(required=True)
    ro_body_group.add_argument("--body", help="PR body text")
    ro_body_group.add_argument("--body-file", type=Path, help="PR body file path")
    ro.add_argument("--base", help="Base branch (defaults to origin/HEAD)")
    ro.add_argument("--head", help="Head branch (defaults to current branch)")
    ro.add_argument("--draft", action="store_true", help="Create draft PR")
    ro.add_argument(
        "--confirm-ttl-minutes",
        type=int,
        default=30,
        help="Request expiration in minutes for second confirmation (default: 30).",
    )

    ao = sub.add_parser(
        "approve-open-pr",
        help="Approve and execute PR creation from a pending request",
    )
    ao.add_argument("--run-id", required=True)
    ao.add_argument("--request-file", type=Path, required=True)
    ao.add_argument("--confirm-token", required=True)
    ao.add_argument(
        "--confirm",
        action="store_true",
        help="Required second confirmation flag.",
    )
    ao.add_argument(
        "--allow-expired",
        action="store_true",
        help="Allow approving an expired request file.",
    )
    ao.add_argument(
        "--allow-dod-bypass",
        action="store_true",
        help="Bypass PR DoD gate checks (manual emergency use only).",
    )

    sg = sub.add_parser("sync-github", help="Poll GitHub PR checks/reviews and sync run states")
    sg.add_argument("--run-id", help="Optional run id. If omitted, scans active PR runs.")
    sg.add_argument("--limit", type=int, default=50, help="Max runs scanned when --run-id absent.")
    sg.add_argument("--dry-run", action="store_true", help="Compute decisions without mutating run state.")
    sg.add_argument("--loop", action="store_true", help="Continuously poll at interval.")
    sg.add_argument("--interval-sec", type=int, default=120, help="Polling interval in loop mode.")
    sg.add_argument(
        "--max-loops",
        type=int,
        help="Optional max loop count for testing loop mode.",
    )

    tb = sub.add_parser("run-telegram-bot", help="Run Telegram bot control loop")
    tb.add_argument(
        "--telegram-token",
        help="Telegram bot token. Defaults to AGENTPR_TELEGRAM_BOT_TOKEN.",
    )
    tb.add_argument(
        "--allow-chat-id",
        action="append",
        type=int,
        default=[],
        help=(
            "Allowed chat id. Can be repeated. "
            "Required unless --allow-any-chat is used."
        ),
    )
    tb.add_argument(
        "--write-chat-id",
        action="append",
        type=int,
        default=[],
        help=(
            "Chat id allowed to run mutating commands (/pause,/resume,/retry). "
            "Defaults to allowed chat ids when omitted."
        ),
    )
    tb.add_argument(
        "--admin-chat-id",
        action="append",
        type=int,
        default=[],
        help=(
            "Chat id allowed to run privileged commands (/approve_pr). "
            "Defaults to write chat ids when omitted."
        ),
    )
    tb.add_argument(
        "--poll-timeout-sec",
        type=int,
        help="Telegram long-poll timeout. If omitted, uses manager policy default.",
    )
    tb.add_argument(
        "--idle-sleep-sec",
        type=int,
        help="Sleep when no updates. If omitted, uses manager policy default.",
    )
    tb.add_argument(
        "--list-limit",
        type=int,
        help="Default /list limit. If omitted, uses manager policy default.",
    )
    tb.add_argument(
        "--rate-limit-window-sec",
        type=int,
        help="Rate-limit window size in seconds. If omitted, uses manager policy default.",
    )
    tb.add_argument(
        "--rate-limit-per-chat",
        type=int,
        help="Max bot commands per chat in rate-limit window. If omitted, uses manager policy default.",
    )
    tb.add_argument(
        "--rate-limit-global",
        type=int,
        help="Max bot commands globally in rate-limit window. If omitted, uses manager policy default.",
    )
    tb.add_argument(
        "--audit-log-file",
        type=Path,
        help="Audit log JSONL file path. If omitted, uses manager policy default.",
    )
    tb.add_argument(
        "--allow-any-chat",
        action="store_true",
        help="Allow any chat id (development only).",
    )

    wh = sub.add_parser("run-github-webhook", help="Run GitHub webhook HTTP server")
    wh.add_argument("--host", default="127.0.0.1", help="Listen host (default: 127.0.0.1).")
    wh.add_argument("--port", type=int, default=8787, help="Listen port (default: 8787).")
    wh.add_argument(
        "--path",
        default="/github/webhook",
        help="Webhook path (default: /github/webhook).",
    )
    wh.add_argument(
        "--secret",
        help="GitHub webhook secret. Defaults to AGENTPR_GITHUB_WEBHOOK_SECRET.",
    )
    wh.add_argument(
        "--allow-unsigned",
        action="store_true",
        help="Allow unsigned webhook requests (development only).",
    )
    wh.add_argument(
        "--max-payload-bytes",
        type=int,
        help="Max webhook request payload size in bytes. If omitted, uses manager policy default.",
    )
    wh.add_argument(
        "--audit-log-file",
        type=Path,
        help="Webhook audit JSONL file path. If omitted, uses manager policy default.",
    )

    cw = sub.add_parser(
        "cleanup-webhook-deliveries",
        help="Delete old webhook delivery dedup records",
    )
    cw.add_argument(
        "--source",
        default="github",
        help="Webhook source key (default: github).",
    )
    cw.add_argument(
        "--keep-days",
        type=int,
        default=30,
        help="Keep last N days of delivery records (default: 30).",
    )

    ws = sub.add_parser(
        "webhook-audit-summary",
        help="Summarize webhook audit JSONL for observability/alert thresholds",
    )
    ws.add_argument(
        "--audit-log-file",
        type=Path,
        help="Webhook audit JSONL file path. If omitted, uses manager policy default.",
    )
    ws.add_argument(
        "--since-minutes",
        type=int,
        default=60,
        help="Only include entries in the last N minutes (default: 60).",
    )
    ws.add_argument(
        "--max-lines",
        type=int,
        default=5000,
        help="Read at most the latest N lines from audit log (default: 5000).",
    )
    ws.add_argument(
        "--fail-on-retryable-failures",
        type=int,
        help="Return non-zero when retryable failure count exceeds this threshold.",
    )
    ws.add_argument(
        "--fail-on-http5xx-rate",
        type=float,
        help="Return non-zero when HTTP 5xx rate (%%) exceeds this threshold.",
    )

    pr = sub.add_parser("link-pr", help="Link PR number and move to CI_WAIT")
    pr.add_argument("--run-id", required=True)
    pr.add_argument("--pr-number", type=int, required=True)
    add_idempotency_arg(pr)

    md = sub.add_parser("mark-done", help="Manually move run to DONE from allowed states")
    md.add_argument("--run-id", required=True)
    add_idempotency_arg(md)

    ck = sub.add_parser("record-check", help="Record github check conclusion")
    ck.add_argument("--run-id", required=True)
    ck.add_argument("--conclusion", required=True)
    ck.add_argument("--pr-number", type=int)
    add_idempotency_arg(ck)

    rv = sub.add_parser("record-review", help="Record github review state")
    rv.add_argument("--run-id", required=True)
    rv.add_argument("--state", required=True)
    add_idempotency_arg(rv)

    pa = sub.add_parser("pause", help="Pause run")
    pa.add_argument("--run-id", required=True)
    add_idempotency_arg(pa)

    re = sub.add_parser("resume", help="Resume PAUSED run to target state")
    re.add_argument("--run-id", required=True)
    re.add_argument("--target-state", choices=[s.value for s in RunState], required=True)
    add_idempotency_arg(re)

    rt = sub.add_parser("retry", help="Retry run to target state")
    rt.add_argument("--run-id", required=True)
    rt.add_argument("--target-state", choices=[s.value for s in RunState], required=True)
    add_idempotency_arg(rt)

    ss = sub.add_parser("skills-status", help="Show AgentPR skill availability for codex")
    ss.add_argument(
        "--codex-home",
        type=Path,
        help="Optional CODEX_HOME override (defaults to $CODEX_HOME or ~/.codex).",
    )
    ss.add_argument(
        "--skills-root",
        type=Path,
        help="Optional skills root override (defaults to <CODEX_HOME>/skills).",
    )
    ss.add_argument(
        "--local-skills-root",
        type=Path,
        default=DEFAULT_SKILLS_SOURCE_ROOT,
        help=f"Local AgentPR skills source root (default: {DEFAULT_SKILLS_SOURCE_ROOT}).",
    )

    ins = sub.add_parser("install-skills", help="Install local AgentPR skills into codex skills root")
    ins.add_argument(
        "--codex-home",
        type=Path,
        help="Optional CODEX_HOME override (defaults to $CODEX_HOME or ~/.codex).",
    )
    ins.add_argument(
        "--skills-root",
        type=Path,
        help="Optional destination skills root (defaults to <CODEX_HOME>/skills).",
    )
    ins.add_argument(
        "--local-skills-root",
        type=Path,
        default=DEFAULT_SKILLS_SOURCE_ROOT,
        help=f"Local AgentPR skills source root (default: {DEFAULT_SKILLS_SOURCE_ROOT}).",
    )
    ins.add_argument(
        "--name",
        action="append",
        default=[],
        help="Optional skill name to install. Can be repeated.",
    )
    ins.add_argument(
        "--force",
        action="store_true",
        help="Overwrite existing destination skill folders.",
    )
    ins.add_argument(
        "--install-curated-ci",
        action="store_true",
        help=(
            "Also install curated ci/review helper skills from openai/skills "
            "(gh-fix-ci and gh-address-comments)."
        ),
    )

    sm = sub.add_parser(
        "skills-metrics",
        help="Summarize skills-mode runtime quality from agent runtime reports",
    )
    sm.add_argument(
        "--run-id",
        help="Optional run id to scope metrics to one run.",
    )
    sm.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Maximum runtime reports to scan (default: 200).",
    )

    sf = sub.add_parser(
        "skills-feedback",
        help="Build manager-facing prompt/skill iteration actions from skills metrics",
    )
    sf.add_argument(
        "--run-id",
        help="Optional run id to scope feedback to one run.",
    )
    sf.add_argument(
        "--limit",
        type=int,
        default=300,
        help="Maximum runtime reports to scan (default: 300).",
    )
    sf.add_argument(
        "--min-samples",
        type=int,
        default=3,
        help="Minimum per-skill samples before skill-specific actions are emitted (default: 3).",
    )

    ir = sub.add_parser(
        "inspect-run",
        help="Build manager-facing diagnostic snapshot for one run",
    )
    ir.add_argument("--run-id", required=True)
    ir.add_argument(
        "--attempt-limit",
        type=int,
        default=120,
        help="Max step attempts to include (default: 120).",
    )
    ir.add_argument(
        "--event-limit",
        type=int,
        default=60,
        help="Max events to include (default: 60).",
    )
    ir.add_argument(
        "--command-limit",
        type=int,
        default=20,
        help="Max sampled shell commands from runtime report (default: 20).",
    )
    ir.add_argument(
        "--include-log-tails",
        action="store_true",
        help="Include short stdout/stderr tails per step attempt.",
    )

    rb = sub.add_parser(
        "run-bottlenecks",
        help="Summarize runtime bottlenecks across recent runs",
    )
    rb.add_argument(
        "--limit",
        type=int,
        default=20,
        help="Number of recent runs to analyze (default: 20).",
    )
    rb.add_argument(
        "--attempt-limit-per-run",
        type=int,
        default=200,
        help="Step-attempt rows loaded per run (default: 200).",
    )

    mt = sub.add_parser(
        "manager-tick",
        help="Run one manager orchestration tick (rule-based actions)",
    )
    add_manager_common_args(mt)

    ml = sub.add_parser(
        "run-manager-loop",
        help="Run manager orchestration loop continuously",
    )
    add_manager_common_args(ml)
    ml.add_argument(
        "--interval-sec",
        type=int,
        default=300,
        help="Loop interval in seconds (default: 300).",
    )
    ml.add_argument(
        "--max-loops",
        type=int,
        help="Optional max loop count for testing.",
    )

    return parser


def build_service(args: argparse.Namespace) -> OrchestratorService:
    db = Database(args.db)
    service = OrchestratorService(db=db, workspace_root=args.workspace_root)
    service.initialize()
    return service


def resolve_startup_doctor_profile(args: argparse.Namespace) -> dict[str, Any] | None:
    command = str(args.command)
    if command in {"create-run", "start-discovery", "start-implementation"}:
        return {
            "profile": "basic",
            "check_network": False,
            "require_gh_auth": False,
            "require_codex": False,
            "require_telegram_token": False,
            "require_webhook_secret": False,
        }
    if command in {
        "run-prepare",
        "run-finish",
        "request-open-pr",
        "approve-open-pr",
        "sync-github",
    }:
        return {
            "profile": "github",
            "check_network": True,
            "require_gh_auth": True,
            "require_codex": False,
            "require_telegram_token": False,
            "require_webhook_secret": False,
        }
    if command == "run-agent-step":
        return {
            "profile": "agent",
            "check_network": not bool(getattr(args, "skip_network_check", False)),
            "require_gh_auth": True,
            "require_codex": True,
            "require_telegram_token": False,
            "require_webhook_secret": False,
        }
    if command in {"manager-tick", "run-manager-loop"}:
        return {
            "profile": "manager",
            "check_network": True,
            "require_gh_auth": True,
            "require_codex": True,
            "require_telegram_token": False,
            "require_webhook_secret": False,
        }
    if command == "run-telegram-bot":
        return {
            "profile": "telegram",
            "check_network": True,
            "require_gh_auth": False,
            "require_codex": False,
            "require_telegram_token": not bool(getattr(args, "telegram_token", None)),
            "require_webhook_secret": False,
        }
    if command == "run-github-webhook":
        return {
            "profile": "webhook",
            "check_network": False,
            "require_gh_auth": False,
            "require_codex": False,
            "require_telegram_token": False,
            "require_webhook_secret": (
                not bool(getattr(args, "allow_unsigned", False))
                and not bool(getattr(args, "secret", None))
            ),
        }
    return None


def run_startup_doctor(
    args: argparse.Namespace,
    *,
    check_network: bool,
    require_gh_auth: bool,
    require_codex: bool,
    require_telegram_token: bool,
    require_webhook_secret: bool,
) -> dict[str, Any]:
    timeout = max(int(getattr(args, "network_timeout_sec", 5) or 5), 1)
    report = RuntimeDoctor(
        workspace_root=Path(args.workspace_root),
        check_network=check_network,
        network_timeout_sec=timeout,
        require_gh_auth=require_gh_auth,
        require_codex=require_codex,
        require_telegram_token=require_telegram_token,
        require_webhook_secret=require_webhook_secret,
    ).run()
    return report.to_dict()


def build_manager_loop_config_from_args(args: argparse.Namespace) -> ManagerLoopConfig:
    prompt_file = (
        Path(args.prompt_file).expanduser().resolve()
        if args.prompt_file is not None
        else None
    )
    contract_template_file = (
        Path(args.contract_template_file).expanduser().resolve()
        if args.contract_template_file is not None
        else None
    )
    return ManagerLoopConfig(
        project_root=PROJECT_ROOT,
        db_path=Path(args.db),
        workspace_root=Path(args.workspace_root),
        integration_root=Path(args.integration_root),
        policy_file=Path(args.policy_file),
        run_id=str(args.run_id).strip() if args.run_id else None,
        limit=max(int(args.limit), 1),
        max_actions_per_run=max(int(args.max_actions_per_run), 1),
        prompt_file=prompt_file,
        contract_template_file=contract_template_file,
        auto_contract=not bool(args.disable_auto_contract),
        default_changes=str(args.changes),
        default_commit_title=(str(args.commit_title).strip() if args.commit_title else None),
        codex_sandbox=(
            str(args.codex_sandbox).strip() if args.codex_sandbox is not None else None
        ),
        skills_mode=str(args.skills_mode).strip() if args.skills_mode is not None else None,
        agent_args=tuple(str(item) for item in (args.agent_arg or [])),
        dry_run=bool(args.dry_run),
        decision_mode=str(args.decision_mode).strip().lower(),
        manager_api_base=(
            str(args.manager_api_base).strip() if args.manager_api_base is not None else None
        ),
        manager_model=str(args.manager_model).strip() if args.manager_model is not None else None,
        manager_timeout_sec=max(int(args.manager_timeout_sec), 1),
        manager_api_key_env=str(args.manager_api_key_env).strip() or "AGENTPR_MANAGER_API_KEY",
    )


def enforce_startup_doctor_gate(args: argparse.Namespace) -> None:
    if bool(getattr(args, "skip_doctor", False)):
        return
    profile = resolve_startup_doctor_profile(args)
    if profile is None:
        return
    report = run_startup_doctor(
        args,
        check_network=bool(profile["check_network"]),
        require_gh_auth=bool(profile["require_gh_auth"]),
        require_codex=bool(profile["require_codex"]),
        require_telegram_token=bool(profile["require_telegram_token"]),
        require_webhook_secret=bool(profile["require_webhook_secret"]),
    )
    if report["ok"]:
        return
    failures = report.get("failures", [])
    failure_text = "; ".join(failures[:3]) if failures else "unknown failure"
    raise ValueError(
        f"startup doctor gate failed ({profile['profile']}): {failure_text}. "
        "Run `python3.11 -m orchestrator.cli doctor` for full details."
    )


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        service = build_service(args)
        executor = ScriptExecutor(args.integration_root)

        if args.command == "init-db":
            print_json({"ok": True, "db": str(args.db)})
            return 0

        if args.command == "doctor":
            report = run_startup_doctor(
                args,
                check_network=not args.skip_network_check,
                require_gh_auth=not args.no_require_gh_auth,
                require_codex=args.require_codex,
                require_telegram_token=args.require_telegram_token,
                require_webhook_secret=args.require_webhook_secret,
            )
            print_json(report)
            return 0 if report["ok"] else 1

        if args.command == "skills-status":
            codex_home = (
                Path(args.codex_home).expanduser().resolve()
                if args.codex_home is not None
                else resolve_codex_home()
            )
            skills_root = (
                Path(args.skills_root).expanduser().resolve()
                if args.skills_root is not None
                else resolve_codex_skills_root(codex_home=codex_home)
            )
            local_root = Path(args.local_skills_root).expanduser().resolve()
            installed = discover_installed_skills(skills_root=skills_root)
            local_dirs = list_local_skill_dirs(source_root=local_root)
            local_names = [path.name for path in local_dirs]
            missing_required = [
                name for name in AGENTPR_REQUIRED_SKILLS if name not in installed
            ]
            optional_curated = list(OPTIONAL_CURATED_CI_SKILLS)
            missing_optional = [name for name in optional_curated if name not in installed]
            print_json(
                {
                    "ok": True,
                    "codex_home": str(codex_home),
                    "skills_root": str(skills_root),
                    "local_skills_root": str(local_root),
                    "local_skill_names": local_names,
                    "installed_skill_count": len(installed),
                    "installed_required": [
                        name for name in AGENTPR_REQUIRED_SKILLS if name in installed
                    ],
                    "missing_required": missing_required,
                    "installed_optional_ci": [
                        name for name in optional_curated if name in installed
                    ],
                    "missing_optional_ci": missing_optional,
                }
            )
            return 0

        if args.command == "install-skills":
            codex_home = (
                Path(args.codex_home).expanduser().resolve()
                if args.codex_home is not None
                else resolve_codex_home()
            )
            skills_root = (
                Path(args.skills_root).expanduser().resolve()
                if args.skills_root is not None
                else resolve_codex_skills_root(codex_home=codex_home)
            )
            local_root = Path(args.local_skills_root).expanduser().resolve()
            requested_names = [str(item).strip() for item in args.name if str(item).strip()]
            results = install_local_skills(
                source_root=local_root,
                skills_root=skills_root,
                names=requested_names or None,
                force=bool(args.force),
            )
            curated_result: dict[str, Any] | None = None
            if bool(args.install_curated_ci):
                curated_result = install_curated_ci_skills(skills_root=skills_root)
            installed = discover_installed_skills(skills_root=skills_root)
            print_json(
                {
                    "ok": True,
                    "codex_home": str(codex_home),
                    "skills_root": str(skills_root),
                    "local_skills_root": str(local_root),
                    "results": results,
                    "curated_ci_install": curated_result,
                    "missing_required_after_install": [
                        name for name in AGENTPR_REQUIRED_SKILLS if name not in installed
                    ],
                    "missing_optional_ci_after_install": [
                        name for name in OPTIONAL_CURATED_CI_SKILLS if name not in installed
                    ],
                    "note": "Restart codex-managed sessions if skill discovery is cached.",
                }
            )
            return 0

        if args.command == "skills-metrics":
            report = gather_skills_metrics(
                service=service,
                run_id=args.run_id,
                limit=max(int(args.limit), 1),
            )
            print_json(report)
            return 0

        if args.command == "skills-feedback":
            metrics = gather_skills_metrics(
                service=service,
                run_id=args.run_id,
                limit=max(int(args.limit), 1),
            )
            feedback = build_skills_feedback_report(
                metrics=metrics,
                min_samples=max(int(args.min_samples), 1),
            )
            json_path = write_skills_feedback_json(feedback)
            md_path = write_skills_feedback_markdown(feedback)
            feedback["report_path"] = str(json_path)
            feedback["markdown_path"] = str(md_path)
            print_json(feedback)
            return 0

        if args.command == "inspect-run":
            report = gather_run_inspect(
                service=service,
                run_id=args.run_id,
                attempt_limit=max(int(args.attempt_limit), 1),
                event_limit=max(int(args.event_limit), 1),
                command_limit=max(int(args.command_limit), 1),
                include_log_tails=bool(args.include_log_tails),
            )
            print_json(report)
            return 0

        if args.command == "run-bottlenecks":
            report = gather_run_bottlenecks(
                service=service,
                limit=max(int(args.limit), 1),
                attempt_limit_per_run=max(int(args.attempt_limit_per_run), 1),
            )
            print_json(report)
            return 0

        enforce_startup_doctor_gate(args)

        if args.command == "manager-tick":
            config = build_manager_loop_config_from_args(args)
            runner = ManagerLoopRunner(service=service, config=config)
            report = runner.tick()
            print_json(report)
            return 0 if report["ok"] else 1

        if args.command == "run-manager-loop":
            config = build_manager_loop_config_from_args(args)
            runner = ManagerLoopRunner(service=service, config=config)
            loops = 0
            fail_count = 0
            try:
                while True:
                    report = runner.tick()
                    print_json(report)
                    if not bool(report.get("ok", False)):
                        fail_count += 1
                    loops += 1
                    if args.max_loops is not None and loops >= max(int(args.max_loops), 1):
                        break
                    time.sleep(max(int(args.interval_sec), 1))
            except KeyboardInterrupt:
                return 130
            return 0 if fail_count == 0 else 1

        if args.command == "create-run":
            budget = json.loads(args.budget_json)
            run_id = service.create_run(
                RunCreateInput(
                    owner=args.owner,
                    repo=args.repo,
                    prompt_version=args.prompt_version,
                    mode=RunMode(args.mode),
                    budget=budget,
                    run_id=args.run_id,
                )
            )
            print_json({"run_id": run_id})
            return 0

        if args.command == "list-runs":
            print_json({"runs": service.list_runs(limit=args.limit)})
            return 0

        if args.command == "show-run":
            print_json(service.get_run_snapshot(args.run_id))
            return 0

        if args.command == "start-discovery":
            print_json(
                service.start_discovery(
                    args.run_id,
                    idempotency_key=args.idempotency_key,
                )
            )
            return 0

        if args.command == "run-prepare":
            snapshot = service.get_run_snapshot(args.run_id)
            run = snapshot["run"]
            if snapshot["state"] == RunState.QUEUED.value:
                service.start_discovery(args.run_id)
            result = executor.run_prepare(
                owner=run["owner"],
                repo=run["repo"],
                base_branch=args.base_branch,
                feature_branch=args.feature_branch,
            )
            service.add_step_attempt(
                args.run_id,
                step=StepName.PREPARE,
                exit_code=result.exit_code,
                stdout_log=result.stdout,
                stderr_log=result.stderr,
                duration_ms=result.duration_ms,
            )
            if result.exit_code != 0:
                service.record_step_failure(
                    args.run_id,
                    step=StepName.PREPARE,
                    reason_code="script_failed",
                    error_message=result.stderr.strip() or "prepare.sh failed",
                )
                print_json(
                    {
                        "ok": False,
                        "exit_code": result.exit_code,
                        "stderr": result.stderr.strip(),
                    }
                )
                return result.exit_code
            print_json(
                {
                    "ok": True,
                    "exit_code": result.exit_code,
                    "stdout_tail": tail(result.stdout),
                }
            )
            return 0

        if args.command == "mark-plan-ready":
            print_json(
                service.mark_plan_ready(
                    args.run_id,
                    args.contract_path,
                    idempotency_key=args.idempotency_key,
                )
            )
            return 0

        if args.command == "start-implementation":
            print_json(
                service.start_implementation(
                    args.run_id,
                    idempotency_key=args.idempotency_key,
                )
            )
            return 0

        if args.command == "mark-local-validated":
            print_json(
                service.mark_local_validation_passed(
                    args.run_id,
                    idempotency_key=args.idempotency_key,
                )
            )
            return 0

        if args.command == "run-preflight":
            snapshot = service.get_run_snapshot(args.run_id)
            run = snapshot["run"]
            repo_dir = Path(run["workspace_dir"])
            report = run_preflight_checks(
                service,
                run_id=args.run_id,
                repo_dir=repo_dir,
                workspace_root=Path(args.workspace_root),
                skip_network_check=args.skip_network_check,
                network_timeout_sec=args.network_timeout_sec,
                codex_sandbox=args.codex_sandbox,
            )
            print_json(report)
            return 0 if report["ok"] else 1

        if args.command == "run-agent-step":
            manager_policy = load_manager_policy(Path(args.policy_file))
            policy_agent = manager_policy.run_agent_step
            snapshot = service.get_run_snapshot(args.run_id)
            run = snapshot["run"]
            effective_policy = resolve_run_agent_effective_policy(
                policy_agent,
                owner=str(run["owner"]),
                repo=str(run["repo"]),
            )
            resolved_codex_sandbox = args.codex_sandbox or str(effective_policy["codex_sandbox"])
            resolved_skills_mode = args.skills_mode or str(effective_policy["skills_mode"])
            resolved_max_changed_files = (
                max(int(args.max_changed_files), 0)
                if args.max_changed_files is not None
                else int(effective_policy["max_changed_files"])
            )
            resolved_max_added_lines = (
                max(int(args.max_added_lines), 0)
                if args.max_added_lines is not None
                else int(effective_policy["max_added_lines"])
            )
            resolved_max_retryable_attempts = (
                max(int(args.max_retryable_attempts), 0)
                if args.max_retryable_attempts is not None
                else int(effective_policy["max_retryable_attempts"])
            )
            resolved_min_test_commands = (
                max(int(args.min_test_commands), 0)
                if args.min_test_commands is not None
                else int(effective_policy["min_test_commands"])
            )
            resolved_known_test_failure_allowlist = [
                str(item).strip()
                for item in list(effective_policy.get("known_test_failure_allowlist") or [])
                if str(item).strip()
            ]
            resolved_success_event_stream_sample_pct = int(
                effective_policy["success_event_stream_sample_pct"]
            )
            resolved_success_state = (
                str(args.success_state).strip().upper()
                if args.success_state is not None
                else str(effective_policy["success_state"])
            )
            resolved_on_retryable_state = (
                str(args.on_retryable_state).strip().upper()
                if args.on_retryable_state is not None
                else str(effective_policy["on_retryable_state"])
            )
            resolved_on_human_review_state = (
                str(args.on_human_review_state).strip().upper()
                if args.on_human_review_state is not None
                else str(effective_policy["on_human_review_state"])
            )
            current_state = RunState(snapshot["state"])
            if (
                args.success_state is None
                and current_state == RunState.DISCOVERY
                and resolved_success_state == RunState.LOCAL_VALIDATING.value
            ):
                resolved_success_state = "UNCHANGED"
            if current_state == RunState.LOCAL_VALIDATING and resolved_success_state == RunState.LOCAL_VALIDATING.value:
                resolved_success_state = "UNCHANGED"
            preflight_report: dict[str, Any] | None = None
            if current_state == RunState.QUEUED:
                service.start_discovery(args.run_id)
                current_state = RunState.DISCOVERY
            if current_state not in {
                RunState.DISCOVERY,
                RunState.IMPLEMENTING,
                RunState.LOCAL_VALIDATING,
                RunState.ITERATING,
            }:
                raise ValueError(
                    "run-agent-step is allowed only in DISCOVERY/IMPLEMENTING/"
                    "LOCAL_VALIDATING/ITERATING states."
                )
            repo_dir = Path(run["workspace_dir"])
            if not repo_dir.exists():
                raise ValueError(f"Workspace not found: {repo_dir}")
            if (
                not args.allow_dirty_worktree
                and current_state in {RunState.DISCOVERY, RunState.IMPLEMENTING}
            ):
                preexisting_diff = collect_repo_diff_summary(repo_dir=repo_dir)
                if int(preexisting_diff.get("changed_files_count", 0)) > 0:
                    compact_diff = compact_diff_summary(preexisting_diff)
                    service.record_step_failure(
                        args.run_id,
                        step=StepName.AGENT,
                        reason_code="dirty_workspace_before_agent",
                        error_message="workspace has pre-existing local changes",
                    )
                    state_result = service.retry_run(
                        args.run_id,
                        target_state=RunState.NEEDS_HUMAN_REVIEW,
                    )
                    print_json(
                        {
                            "ok": False,
                            "error": "workspace has pre-existing local changes",
                            "state": state_result["state"],
                            "diff": compact_diff,
                        }
                    )
                    return 1
            if not args.skip_preflight:
                preflight_report = run_preflight_checks(
                    service,
                    run_id=args.run_id,
                    repo_dir=repo_dir,
                    workspace_root=Path(args.workspace_root),
                    skip_network_check=args.skip_network_check,
                    network_timeout_sec=args.network_timeout_sec,
                    codex_sandbox=resolved_codex_sandbox,
                )
                if not preflight_report["ok"]:
                    service.record_step_failure(
                        args.run_id,
                        step=StepName.PREFLIGHT,
                        reason_code="preflight_failed",
                        error_message="; ".join(preflight_report["failures"]),
                    )
                    state_result = service.retry_run(
                        args.run_id,
                        target_state=RunState.NEEDS_HUMAN_REVIEW,
                    )
                    print_json(
                        {
                            "ok": False,
                            "error": "preflight failed",
                            "state": state_result["state"],
                            "preflight": preflight_report,
                        }
                    )
                    return 1
            runtime_policy = executor.runtime_policy_summary(repo_dir)
            resolved_max_agent_seconds = (
                max(int(args.max_agent_seconds), 0)
                if args.max_agent_seconds is not None
                else max(int(effective_policy.get("max_agent_seconds", 900)), 0)
            )
            external_read_only_paths = resolve_external_read_only_paths(
                integration_root=Path(args.integration_root),
                include_skills_root=(resolved_skills_mode == "agentpr"),
                user_paths=[str(item) for item in (args.allow_read_path or [])],
            )
            prompt = load_prompt(args)
            skill_plan_payload: dict[str, Any] | None = None
            task_packet_path: Path | None = None
            if resolved_skills_mode == "agentpr":
                installed_skills = discover_installed_skills()
                plan = build_skill_plan(
                    run_state=current_state,
                    mode=resolved_skills_mode,
                    installed_skills=installed_skills,
                )
                skill_plan_payload = plan.to_dict()
                if plan.missing_required and not args.allow_missing_skills:
                    service.record_step_failure(
                        args.run_id,
                        step=StepName.AGENT,
                        reason_code="skills_missing",
                        error_message=(
                            "missing required skills: " + ", ".join(plan.missing_required)
                        ),
                    )
                    state_result = service.retry_run(
                        args.run_id,
                        target_state=RunState.NEEDS_HUMAN_REVIEW,
                    )
                    install_cmd = (
                        "python3.11 -m orchestrator.cli install-skills "
                        f"--local-skills-root {DEFAULT_SKILLS_SOURCE_ROOT}"
                    )
                    print_json(
                        {
                            "ok": False,
                            "error": "required skills are missing for agentpr skills-mode",
                            "missing_required": list(plan.missing_required),
                            "skills_root": str(plan.skills_root),
                            "state": state_result["state"],
                            "next_command": install_cmd,
                        }
                    )
                    return 1

                contract_artifact = service.latest_artifact(
                    args.run_id,
                    artifact_type="contract",
                )
                contract_source_uri = (
                    str(contract_artifact.get("uri"))
                    if contract_artifact is not None and contract_artifact.get("uri")
                    else None
                )
                contract_worker_uri, contract_text = prepare_worker_contract_artifact(
                    repo_dir=repo_dir,
                    contract_source_uri=contract_source_uri,
                )
                user_packet: Any | None = None
                if args.task_packet_file is not None:
                    user_packet = load_user_task_packet(args.task_packet_file)
                task_packet = build_task_packet(
                    run=run,
                    run_state=current_state,
                    repo_dir=repo_dir,
                    contract_uri=contract_worker_uri or contract_source_uri,
                    contract_source_uri=contract_source_uri,
                    contract_text=contract_text,
                    codex_sandbox=resolved_codex_sandbox,
                    allow_agent_push=bool(args.allow_agent_push),
                    max_changed_files=resolved_max_changed_files,
                    max_added_lines=resolved_max_added_lines,
                    integration_root=Path(args.integration_root),
                    skill_plan=plan,
                    user_packet=user_packet,
                )
                task_packet_path = write_task_packet(args.run_id, task_packet)
                service.add_artifact(
                    args.run_id,
                    artifact_type="task_packet",
                    uri=str(task_packet_path),
                    metadata={
                        "skills_mode": resolved_skills_mode,
                        "required_now": list(plan.required_now),
                        "missing_required": list(plan.missing_required),
                    },
                )
                prompt = render_skill_chain_prompt(
                    base_prompt=prompt,
                    task_packet=task_packet,
                    plan=plan,
                )
            result = executor.run_agent_step(
                prompt=prompt,
                repo_dir=repo_dir,
                codex_sandbox=resolved_codex_sandbox,
                codex_full_auto=not args.no_codex_full_auto,
                codex_model=args.codex_model,
                allow_git_push=bool(args.allow_agent_push),
                extra_args=args.agent_arg,
                read_only_paths=external_read_only_paths,
                max_duration_sec=resolved_max_agent_seconds if resolved_max_agent_seconds > 0 else None,
            )
            metadata = result.metadata if isinstance(result.metadata, dict) else {}
            raw_offsets = metadata.get("stdout_line_offsets_ms")
            line_offsets: list[int] | None = None
            if isinstance(raw_offsets, list):
                parsed_offsets: list[int] = []
                for item in raw_offsets:
                    if isinstance(item, int):
                        parsed_offsets.append(item)
                    elif isinstance(item, float):
                        parsed_offsets.append(int(item))
                line_offsets = parsed_offsets
            event_summary = rt.summarize_codex_event_stream(
                result.stdout,
                line_offsets_ms=line_offsets,
            )
            last_message_path: Path | None = None
            raw_last_message_path = str(metadata.get("last_message_path") or "").strip()
            if raw_last_message_path:
                candidate = Path(raw_last_message_path)
                if candidate.exists():
                    last_message_path = candidate
                    service.add_artifact(
                        args.run_id,
                        artifact_type="agent_last_message",
                        uri=str(last_message_path),
                        metadata={"size_bytes": candidate.stat().st_size},
                    )
            service.add_step_attempt(
                args.run_id,
                step=StepName.AGENT,
                exit_code=result.exit_code,
                stdout_log=result.stdout,
                stderr_log=result.stderr,
                duration_ms=result.duration_ms,
            )
            agent_attempt_no = service.count_step_attempts(
                args.run_id,
                step=StepName.AGENT,
            )
            agent_report = rt.build_agent_runtime_report(
                run_id=args.run_id,
                engine="codex",
                result=result,
                run_state=current_state,
                codex_sandbox=resolved_codex_sandbox,
                codex_model=args.codex_model,
                codex_full_auto=not args.no_codex_full_auto,
                runtime_policy=runtime_policy,
                preflight_report=preflight_report,
                diff_summary=collect_repo_diff_summary(repo_dir=repo_dir),
                allow_agent_push=bool(args.allow_agent_push),
                max_changed_files=resolved_max_changed_files,
                max_added_lines=resolved_max_added_lines,
                max_retryable_attempts=resolved_max_retryable_attempts,
                min_test_commands=resolved_min_test_commands,
                known_test_failure_allowlist=resolved_known_test_failure_allowlist,
                attempt_no=agent_attempt_no,
                skills_mode=resolved_skills_mode,
                skill_plan=skill_plan_payload,
                task_packet_path=str(task_packet_path) if task_packet_path else None,
                event_summary=event_summary,
                event_stream_path=None,
                last_message_path=str(last_message_path) if last_message_path else None,
                manager_policy={
                    "path": str(Path(args.policy_file)),
                    "loaded": manager_policy.source_loaded,
                    "run_agent_step": {
                        "codex_sandbox": policy_agent.codex_sandbox,
                        "skills_mode": policy_agent.skills_mode,
                        "max_agent_seconds": policy_agent.max_agent_seconds,
                        "max_changed_files": policy_agent.max_changed_files,
                        "max_added_lines": policy_agent.max_added_lines,
                        "max_retryable_attempts": policy_agent.max_retryable_attempts,
                        "min_test_commands": policy_agent.min_test_commands,
                        "known_test_failure_allowlist": list(policy_agent.known_test_failure_allowlist),
                        "success_event_stream_sample_pct": policy_agent.success_event_stream_sample_pct,
                        "success_state": policy_agent.success_state,
                        "on_retryable_state": policy_agent.on_retryable_state,
                        "on_human_review_state": policy_agent.on_human_review_state,
                        "effective": effective_policy,
                        "resolved_success_state": resolved_success_state,
                        "resolved_on_retryable_state": resolved_on_retryable_state,
                        "resolved_on_human_review_state": resolved_on_human_review_state,
                        "resolved_success_event_stream_sample_pct": resolved_success_event_stream_sample_pct,
                        "resolved_max_agent_seconds": resolved_max_agent_seconds,
                        "resolved_known_test_failure_allowlist": resolved_known_test_failure_allowlist,
                        "resolved_external_read_only_paths": [
                            str(item) for item in external_read_only_paths
                        ],
                    },
                },
            )
            verdict = agent_report["classification"]
            keep_event_stream, keep_event_stream_reason = rt.should_persist_agent_event_stream(
                grade=str(verdict.get("grade") or ""),
                run_id=args.run_id,
                attempt_no=agent_attempt_no,
                success_sample_pct=resolved_success_event_stream_sample_pct,
            )
            event_stream_path: Path | None = None
            if keep_event_stream:
                event_stream_path = rt.write_agent_event_stream(args.run_id, result.stdout)
                service.add_artifact(
                    args.run_id,
                    artifact_type="agent_event_stream",
                    uri=str(event_stream_path),
                    metadata={
                        "parsed_event_count": int(event_summary.get("parsed_event_count") or 0),
                        "parse_error_count": int(event_summary.get("parse_error_count") or 0),
                        "command_event_count": int(event_summary.get("command_event_count") or 0),
                        "retention_reason": keep_event_stream_reason,
                    },
                )
            runtime_payload = agent_report.get("runtime")
            if isinstance(runtime_payload, dict):
                runtime_payload["event_stream_path"] = str(event_stream_path) if event_stream_path else ""
                runtime_payload["event_stream_retained"] = bool(event_stream_path)
                runtime_payload["event_stream_retention_reason"] = keep_event_stream_reason
            signals_payload = agent_report.get("signals")
            if isinstance(signals_payload, dict):
                signals_payload["event_stream_retained"] = bool(event_stream_path)
                signals_payload["event_stream_retention_reason"] = keep_event_stream_reason

            agent_report_path = rt.write_agent_runtime_report(args.run_id, agent_report)
            service.add_artifact(
                args.run_id,
                artifact_type="agent_runtime_report",
                uri=str(agent_report_path),
                metadata={
                    "exit_code": result.exit_code,
                    "violations": len(agent_report["safety"]["violations"]),
                    "grade": verdict["grade"],
                    "reason_code": verdict["reason_code"],
                    "next_action": verdict["next_action"],
                    "parsed_event_count": int(event_summary.get("parsed_event_count") or 0),
                    "command_event_count": int(event_summary.get("command_event_count") or 0),
                    "event_stream_retained": bool(event_stream_path),
                    "event_stream_retention_reason": keep_event_stream_reason,
                },
            )
            if result.exit_code != 0:
                state_result = service.record_step_failure(
                    args.run_id,
                    step=StepName.AGENT,
                    reason_code="codex_agent_failed",
                    error_message=result.stderr.strip() or "agent command failed",
                )
                if verdict["grade"] == AgentRuntimeGrade.HUMAN_REVIEW.value:
                    state_result = apply_nonpass_verdict_state(
                        service,
                        run_id=args.run_id,
                        target_state=resolved_on_human_review_state,
                    )
                elif verdict["grade"] == AgentRuntimeGrade.RETRYABLE.value:
                    state_result = apply_nonpass_verdict_state(
                        service,
                        run_id=args.run_id,
                        target_state=resolved_on_retryable_state,
                    )
                state_after = (
                    str(state_result.get("state"))
                    if isinstance(state_result, dict)
                    else str(state_result)
                )
                analysis_paths = rt.persist_run_analysis_artifacts(
                    service=service,
                    run=run,
                    state_before=current_state.value,
                    state_after=state_after,
                    agent_report=agent_report,
                    agent_report_path=agent_report_path,
                    event_stream_path=event_stream_path,
                    last_message_path=last_message_path,
                )
                print_json(
                    {
                        "ok": False,
                        "engine": "codex",
                        "exit_code": result.exit_code,
                        "state": state_after,
                        "stderr": result.stderr.strip(),
                        "classification": verdict,
                        "agent_report": str(agent_report_path),
                        "agent_event_stream": str(event_stream_path) if event_stream_path else None,
                        "agent_last_message": str(last_message_path) if last_message_path else None,
                        "run_digest": analysis_paths["run_digest"],
                        "manager_insight": analysis_paths["manager_insight"],
                    }
                )
                return result.exit_code
            if verdict["grade"] != AgentRuntimeGrade.PASS.value:
                if verdict["grade"] == AgentRuntimeGrade.HUMAN_REVIEW.value:
                    state_result = apply_nonpass_verdict_state(
                        service,
                        run_id=args.run_id,
                        target_state=resolved_on_human_review_state,
                    )
                else:
                    state_result = apply_nonpass_verdict_state(
                        service,
                        run_id=args.run_id,
                        target_state=resolved_on_retryable_state,
                    )
                state_after = (
                    str(state_result.get("state"))
                    if isinstance(state_result, dict)
                    else str(state_result)
                )
                analysis_paths = rt.persist_run_analysis_artifacts(
                    service=service,
                    run=run,
                    state_before=current_state.value,
                    state_after=state_after,
                    agent_report=agent_report,
                    agent_report_path=agent_report_path,
                    event_stream_path=event_stream_path,
                    last_message_path=last_message_path,
                )
                print_json(
                    {
                        "ok": False,
                        "engine": "codex",
                        "exit_code": result.exit_code,
                        "state": state_after,
                        "classification": verdict,
                        "stdout_tail": tail(result.stdout),
                        "agent_report": str(agent_report_path),
                        "agent_event_stream": str(event_stream_path) if event_stream_path else None,
                        "agent_last_message": str(last_message_path) if last_message_path else None,
                        "run_digest": analysis_paths["run_digest"],
                        "manager_insight": analysis_paths["manager_insight"],
                    }
                )
                return 1
            success_state = converge_agent_success_state(
                service,
                args,
                success_state=resolved_success_state,
            )
            analysis_paths = rt.persist_run_analysis_artifacts(
                service=service,
                run=run,
                state_before=current_state.value,
                state_after=success_state,
                agent_report=agent_report,
                agent_report_path=agent_report_path,
                event_stream_path=event_stream_path,
                last_message_path=last_message_path,
            )
            print_json(
                {
                    "ok": True,
                    "engine": "codex",
                    "exit_code": result.exit_code,
                    "state": success_state,
                    "classification": verdict,
                    "stdout_tail": tail(result.stdout),
                    "agent_report": str(agent_report_path),
                    "agent_event_stream": str(event_stream_path) if event_stream_path else None,
                    "agent_last_message": str(last_message_path) if last_message_path else None,
                    "run_digest": analysis_paths["run_digest"],
                    "manager_insight": analysis_paths["manager_insight"],
                }
            )
            return 0

        if args.command == "run-finish":
            snapshot = service.get_run_snapshot(args.run_id)
            run = snapshot["run"]
            repo_dir = Path(run["workspace_dir"])
            result = executor.run_finish(
                repo_dir=repo_dir,
                changes=args.changes,
                project=args.project,
                commit_title=args.commit_title,
            )
            service.add_step_attempt(
                args.run_id,
                step=StepName.FINISH,
                exit_code=result.exit_code,
                stdout_log=result.stdout,
                stderr_log=result.stderr,
                duration_ms=result.duration_ms,
            )
            if result.exit_code != 0:
                service.record_step_failure(
                    args.run_id,
                    step=StepName.FINISH,
                    reason_code="script_failed",
                    error_message=result.stderr.strip() or "finish.sh failed",
                )
                print_json(
                    {
                        "ok": False,
                        "exit_code": result.exit_code,
                        "stderr": result.stderr.strip(),
                    }
                )
                return result.exit_code

            branch = executor.current_branch(repo_dir)
            state_result = service.record_push_completed(args.run_id, branch=branch)
            print_json(
                {
                    "ok": True,
                    "exit_code": result.exit_code,
                    "branch": branch,
                    "state": state_result["state"],
                    "stdout_tail": tail(result.stdout),
                }
            )
            return 0

        if args.command == "request-open-pr":
            snapshot = service.get_run_snapshot(args.run_id)
            run = snapshot["run"]
            current_state = RunState(snapshot["state"])
            if current_state != RunState.PUSHED:
                raise ValueError(
                    "request-open-pr is allowed only when run state is PUSHED."
                )
            repo_dir = Path(run["workspace_dir"])
            if not repo_dir.exists():
                raise ValueError(f"Workspace not found: {repo_dir}")
            title = args.title.strip()
            if not title:
                raise ValueError("PR title cannot be empty.")
            body = load_optional_text(args.body, args.body_file, arg_name="PR body")
            if not body.strip():
                raise ValueError("PR body cannot be empty.")
            head = (args.head or executor.current_branch(repo_dir)).strip()
            base = (args.base or executor.default_base_branch(repo_dir)).strip()
            if not head:
                raise ValueError("Unable to determine head branch.")
            if not base:
                raise ValueError("Unable to determine base branch.")
            created_at = datetime.now(UTC)
            expires_at = created_at + timedelta(minutes=max(args.confirm_ttl_minutes, 1))
            confirm_token = secrets.token_hex(4).upper()
            request_payload = {
                "run_id": args.run_id,
                "title": title,
                "body": body,
                "base": base,
                "head": head,
                "draft": bool(args.draft),
                "confirm_token": confirm_token,
                "created_at": created_at.isoformat(),
                "expires_at": expires_at.isoformat(),
            }
            request_path = write_pr_open_request(args.run_id, request_payload)
            service.add_artifact(
                args.run_id,
                artifact_type="pr_open_request",
                uri=str(request_path),
                metadata={
                    "base": base,
                    "head": head,
                    "expires_at": expires_at.isoformat(),
                },
            )
            print_json(
                {
                    "ok": True,
                    "run_id": args.run_id,
                    "request_file": str(request_path),
                    "confirm_token": confirm_token,
                    "expires_at": expires_at.isoformat(),
                    "preview": {
                        "title": title,
                        "base": base,
                        "head": head,
                        "draft": bool(args.draft),
                    },
                    "next_command": (
                        "python3.11 -m orchestrator.cli approve-open-pr "
                        f"--run-id {args.run_id} "
                        f"--request-file {request_path} "
                        f"--confirm-token {confirm_token} --confirm"
                    ),
                }
            )
            return 0

        if args.command == "approve-open-pr":
            if not args.confirm:
                raise ValueError(
                    "approve-open-pr requires explicit --confirm for second confirmation."
                )
            snapshot = service.get_run_snapshot(args.run_id)
            run = snapshot["run"]
            current_state = RunState(snapshot["state"])
            repo_dir = Path(run["workspace_dir"])
            if not repo_dir.exists():
                raise ValueError(f"Workspace not found: {repo_dir}")

            request_payload = read_pr_open_request(args.request_file)
            if request_payload["run_id"] != args.run_id:
                raise ValueError("request-file run_id mismatch.")

            token = str(request_payload["confirm_token"]).strip().upper()
            provided = args.confirm_token.strip().upper()
            if provided != token:
                raise ValueError("confirm-token mismatch.")

            expires_at = parse_iso_datetime(str(request_payload["expires_at"]))
            if datetime.now(UTC) > expires_at and not args.allow_expired:
                raise ValueError(
                    "request-file expired. Re-run request-open-pr or pass --allow-expired."
                )

            # Idempotent shortcut when PR is already linked.
            if current_state == RunState.CI_WAIT and run.get("pr_number") is not None:
                print_json(
                    {
                        "ok": True,
                        "already_linked": True,
                        "state": current_state.value,
                        "pr_number": run["pr_number"],
                    }
                )
                return 0

            if current_state != RunState.PUSHED:
                raise ValueError(
                    "approve-open-pr is allowed only when run state is PUSHED."
                )

            policy = load_manager_policy(Path(args.policy_file))
            effective_policy = resolve_run_agent_effective_policy(
                policy.run_agent_step,
                owner=str(run["owner"]),
                repo=str(run["repo"]),
            )
            digest_payload, digest_error = rt.load_digest_artifact_payload(
                service,
                run_id=args.run_id,
            )
            contract_available = (
                service.latest_artifact(args.run_id, artifact_type="contract") is not None
            )
            gate = rt.evaluate_pr_gate_readiness(
                digest=digest_payload,
                expected_policy=effective_policy,
                contract_available=contract_available,
            )
            if digest_error:
                failed_checks = gate.get("failed_checks")
                if isinstance(failed_checks, list):
                    failed_checks.insert(
                        0,
                        {"code": "digest_load_error", "message": digest_error},
                    )
            if not gate.get("ok", False) and not bool(args.allow_dod_bypass):
                failed_checks = gate.get("failed_checks")
                failed_checks = failed_checks if isinstance(failed_checks, list) else []
                top = failed_checks[:4]
                details = "; ".join(
                    f"{item.get('code')}: {item.get('message')}"
                    for item in top
                    if isinstance(item, dict)
                )
                raise ValueError(
                    "PR DoD gate blocked approve-open-pr. "
                    "Fix run quality or pass --allow-dod-bypass for manual emergency. "
                    f"checks={details}"
                )

            result = executor.run_create_pr(
                repo_dir=repo_dir,
                title=str(request_payload["title"]),
                body=str(request_payload["body"]),
                base=str(request_payload["base"]),
                head=str(request_payload["head"]),
                draft=bool(request_payload["draft"]),
            )
            service.add_step_attempt(
                args.run_id,
                step=StepName.PR_CREATE,
                exit_code=result.exit_code,
                stdout_log=result.stdout,
                stderr_log=result.stderr,
                duration_ms=result.duration_ms,
            )

            combined_output = f"{result.stdout}\n{result.stderr}"
            pr_url = extract_pr_url(combined_output)
            pr_number = extract_pr_number(combined_output)

            lower_output = combined_output.lower()
            already_exists = "already exists" in lower_output and pr_number is not None
            if result.exit_code != 0 and not already_exists:
                state_result = service.record_step_failure(
                    args.run_id,
                    step=StepName.PR_CREATE,
                    reason_code="pr_create_failed",
                    error_message=result.stderr.strip() or "gh pr create failed",
                )
                print_json(
                    {
                        "ok": False,
                        "exit_code": result.exit_code,
                        "state": state_result["state"],
                        "stderr": result.stderr.strip(),
                        "stdout_tail": tail(result.stdout),
                        "pr_url": pr_url,
                    }
                )
                return result.exit_code or 1

            if pr_number is None:
                service.record_step_failure(
                    args.run_id,
                    step=StepName.PR_CREATE,
                    reason_code="pr_number_not_found",
                    error_message="PR created but number could not be parsed.",
                )
                print_json(
                    {
                        "ok": False,
                        "error": "PR number not found in gh output.",
                        "pr_url": pr_url,
                        "stdout_tail": tail(result.stdout),
                        "stderr_tail": tail(result.stderr),
                    }
                )
                return 1

            state_result = service.link_pr(args.run_id, pr_number=pr_number)
            if pr_url is not None:
                service.add_artifact(
                    args.run_id,
                    artifact_type="pr_url",
                    uri=pr_url,
                    metadata={"pr_number": pr_number},
                )
            print_json(
                {
                    "ok": True,
                    "mode": "already_exists" if already_exists else "created",
                    "pr_number": pr_number,
                    "pr_url": pr_url,
                    "state": state_result["state"],
                    "dod_gate": {
                        "ok": bool(gate.get("ok", False)),
                        "bypassed": bool(args.allow_dod_bypass) and not bool(gate.get("ok", False)),
                        "snapshot": gate.get("snapshot"),
                    },
                }
            )
            return 0

        if args.command == "sync-github":
            if args.loop:
                loops = 0
                try:
                    while True:
                        payload = run_github_sync_once(
                            service=service,
                            executor=executor,
                            run_id=args.run_id,
                            limit=args.limit,
                            dry_run=args.dry_run,
                        )
                        print_json(payload)
                        loops += 1
                        if args.max_loops is not None and loops >= args.max_loops:
                            break
                        time.sleep(max(args.interval_sec, 1))
                except KeyboardInterrupt:
                    return 130
                return 0

            payload = run_github_sync_once(
                service=service,
                executor=executor,
                run_id=args.run_id,
                limit=args.limit,
                dry_run=args.dry_run,
            )
            print_json(payload)
            return 0

        if args.command == "run-telegram-bot":
            token = args.telegram_token or os.environ.get("AGENTPR_TELEGRAM_BOT_TOKEN")
            if not token:
                raise ValueError(
                    "Missing Telegram token. Set --telegram-token or AGENTPR_TELEGRAM_BOT_TOKEN."
                )
            manager_policy = load_manager_policy(Path(args.policy_file))
            telegram_policy = manager_policy.telegram_bot
            client = TelegramClient(token)
            allowed_chat_ids = set(args.allow_chat_id)
            if not allowed_chat_ids and not args.allow_any_chat:
                raise ValueError(
                    "Missing allowlist. Set --allow-chat-id (repeatable) or use --allow-any-chat "
                    "for local development only."
                )
            write_chat_ids = set(args.write_chat_id)
            admin_chat_ids = set(args.admin_chat_id)
            if not write_chat_ids and allowed_chat_ids:
                write_chat_ids = set(allowed_chat_ids)
            if not admin_chat_ids:
                admin_chat_ids = set(write_chat_ids) if write_chat_ids else set(allowed_chat_ids)
            resolved_poll_timeout_sec = (
                max(int(args.poll_timeout_sec), 1)
                if args.poll_timeout_sec is not None
                else telegram_policy.poll_timeout_sec
            )
            resolved_idle_sleep_sec = (
                max(int(args.idle_sleep_sec), 1)
                if args.idle_sleep_sec is not None
                else telegram_policy.idle_sleep_sec
            )
            resolved_list_limit = (
                max(int(args.list_limit), 1)
                if args.list_limit is not None
                else telegram_policy.list_limit
            )
            resolved_rate_limit_window_sec = (
                max(int(args.rate_limit_window_sec), 1)
                if args.rate_limit_window_sec is not None
                else telegram_policy.rate_limit_window_sec
            )
            resolved_rate_limit_per_chat = (
                max(int(args.rate_limit_per_chat), 1)
                if args.rate_limit_per_chat is not None
                else telegram_policy.rate_limit_per_chat
            )
            resolved_rate_limit_global = (
                max(int(args.rate_limit_global), 1)
                if args.rate_limit_global is not None
                else telegram_policy.rate_limit_global
            )
            audit_log_file = (
                Path(args.audit_log_file)
                if args.audit_log_file is not None
                else PROJECT_ROOT / telegram_policy.audit_log_file
            )
            try:
                run_telegram_bot_loop(
                    client=client,
                    service=service,
                    db_path=Path(args.db),
                    workspace_root=Path(args.workspace_root),
                    integration_root=Path(args.integration_root),
                    project_root=PROJECT_ROOT,
                    allowed_chat_ids=allowed_chat_ids,
                    write_chat_ids=write_chat_ids,
                    admin_chat_ids=admin_chat_ids,
                    poll_timeout_sec=resolved_poll_timeout_sec,
                    idle_sleep_sec=resolved_idle_sleep_sec,
                    list_limit=resolved_list_limit,
                    rate_limit_window_sec=resolved_rate_limit_window_sec,
                    rate_limit_per_chat=resolved_rate_limit_per_chat,
                    rate_limit_global=resolved_rate_limit_global,
                    audit_log_file=audit_log_file,
                )
            except KeyboardInterrupt:
                return 130
            return 0

        if args.command == "run-github-webhook":
            secret = args.secret or os.environ.get("AGENTPR_GITHUB_WEBHOOK_SECRET")
            require_signature = not args.allow_unsigned
            if require_signature and not secret:
                raise ValueError(
                    "Missing GitHub webhook secret. Set --secret or AGENTPR_GITHUB_WEBHOOK_SECRET, "
                    "or use --allow-unsigned for local development."
                )
            manager_policy = load_manager_policy(Path(args.policy_file))
            webhook_policy = manager_policy.github_webhook
            resolved_max_payload_bytes = (
                max(int(args.max_payload_bytes), 1024)
                if args.max_payload_bytes is not None
                else webhook_policy.max_payload_bytes
            )
            audit_log_file = (
                Path(args.audit_log_file)
                if args.audit_log_file is not None
                else PROJECT_ROOT / webhook_policy.audit_log_file
            )
            try:
                run_github_webhook_server(
                    service=service,
                    host=args.host,
                    port=int(args.port),
                    path=args.path,
                    secret=secret,
                    require_signature=require_signature,
                    max_payload_bytes=resolved_max_payload_bytes,
                    audit_log_file=audit_log_file,
                )
            except KeyboardInterrupt:
                return 130
            return 0

        if args.command == "cleanup-webhook-deliveries":
            deleted = service.cleanup_webhook_deliveries(
                source=args.source,
                keep_days=max(args.keep_days, 1),
            )
            print_json(
                {
                    "ok": True,
                    "source": args.source,
                    "keep_days": max(args.keep_days, 1),
                    "deleted": deleted,
                }
            )
            return 0

        if args.command == "webhook-audit-summary":
            manager_policy = load_manager_policy(Path(args.policy_file))
            webhook_policy = manager_policy.github_webhook
            audit_log_file = (
                Path(args.audit_log_file)
                if args.audit_log_file is not None
                else PROJECT_ROOT / webhook_policy.audit_log_file
            )
            report = summarize_webhook_audit_log(
                audit_log_file=audit_log_file,
                since_minutes=max(int(args.since_minutes), 1),
                max_lines=max(int(args.max_lines), 1),
                fail_on_retryable_failures=args.fail_on_retryable_failures,
                fail_on_http5xx_rate=args.fail_on_http5xx_rate,
            )
            print_json(report)
            return 0 if report["ok"] else 1

        if args.command == "link-pr":
            print_json(
                service.link_pr(
                    args.run_id,
                    pr_number=args.pr_number,
                    idempotency_key=args.idempotency_key,
                )
            )
            return 0

        if args.command == "mark-done":
            print_json(
                service.mark_done(
                    args.run_id,
                    idempotency_key=args.idempotency_key,
                )
            )
            return 0

        if args.command == "record-check":
            print_json(
                service.record_github_check(
                    args.run_id,
                    conclusion=args.conclusion,
                    pr_number=args.pr_number,
                    idempotency_key=args.idempotency_key,
                )
            )
            return 0

        if args.command == "record-review":
            print_json(
                service.record_review(
                    args.run_id,
                    review_state=args.state,
                    idempotency_key=args.idempotency_key,
                )
            )
            return 0

        if args.command == "pause":
            print_json(
                service.pause_run(
                    args.run_id,
                    idempotency_key=args.idempotency_key,
                )
            )
            return 0

        if args.command == "resume":
            print_json(
                service.resume_run(
                    args.run_id,
                    target_state=RunState(args.target_state),
                    idempotency_key=args.idempotency_key,
                )
            )
            return 0

        if args.command == "retry":
            print_json(
                service.retry_run(
                    args.run_id,
                    target_state=RunState(args.target_state),
                    idempotency_key=args.idempotency_key,
                )
            )
            return 0

        parser.error(f"Unsupported command: {args.command}")
        return 2
    except json.JSONDecodeError as exc:
        print_json({"ok": False, "error": f"invalid JSON: {exc}"})
        return 2
    except (RunNotFoundError, InvalidTransitionError, ValueError, KeyError) as exc:
        print_json({"ok": False, "error": str(exc)})
        return 1


def print_json(payload: dict[str, Any]) -> None:
    print(json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2))


def tail(text: str, lines: int = 20) -> str:
    stripped = text.strip()
    if not stripped:
        return ""
    chunks = stripped.splitlines()
    return "\n".join(chunks[-lines:])


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


def resolve_external_read_only_paths(
    *,
    integration_root: Path,
    include_skills_root: bool,
    user_paths: list[str] | None = None,
) -> list[Path]:
    candidates: list[Path] = []
    resolved_integration_root = integration_root.expanduser().resolve()
    candidates.append(resolved_integration_root)

    forge_root = resolved_integration_root.parent.parent / "forge"
    if forge_root.exists():
        candidates.append(forge_root.resolve())

    if include_skills_root:
        skills_root = resolve_codex_skills_root(codex_home=resolve_codex_home())
        if skills_root.exists():
            candidates.append(skills_root.resolve())

    for raw in user_paths or []:
        value = str(raw).strip()
        if not value:
            continue
        path = Path(value).expanduser()
        if path.exists():
            candidates.append(path.resolve())

    out: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        value = str(path)
        if value in seen:
            continue
        seen.add(value)
        out.append(path)
    return out


def write_pr_open_request(run_id: str, payload: dict[str, Any]) -> Path:
    reports_dir = PROJECT_ROOT / "orchestrator" / "data" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = reports_dir / f"{run_id}_pr_open_request_{stamp}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return path


def read_pr_open_request(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Failed to read request-file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in request-file {path}: {exc}") from exc
    required = {
        "run_id",
        "title",
        "body",
        "base",
        "head",
        "draft",
        "confirm_token",
        "created_at",
        "expires_at",
    }
    missing = sorted(required - set(payload.keys()))
    if missing:
        raise ValueError(f"request-file missing required fields: {', '.join(missing)}")
    return payload


def parse_iso_datetime(raw: str) -> datetime:
    try:
        dt = datetime.fromisoformat(raw)
    except ValueError as exc:
        raise ValueError(f"Invalid datetime format in request-file: {raw}") from exc
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=UTC)
    return dt.astimezone(UTC)


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


def run_preflight_checks(
    service: OrchestratorService,
    *,
    run_id: str,
    repo_dir: Path,
    workspace_root: Path,
    skip_network_check: bool,
    network_timeout_sec: int,
    codex_sandbox: str,
) -> dict[str, Any]:
    checker = PreflightChecker(
        repo_dir=repo_dir,
        workspace_root=workspace_root,
        check_network=not skip_network_check,
        network_timeout_sec=network_timeout_sec,
        codex_sandbox=codex_sandbox,
    )
    report = checker.run()
    report_payload = report.to_dict()
    report_json = json.dumps(report_payload, ensure_ascii=True, sort_keys=True, indent=2)
    report_path = write_preflight_report(run_id, report_json)

    service.add_step_attempt(
        run_id,
        step=StepName.PREFLIGHT,
        exit_code=0 if report.ok else 1,
        stdout_log=report_json,
        stderr_log="\n".join(report.failures),
        duration_ms=report.duration_ms,
    )
    service.add_artifact(
        run_id,
        artifact_type="preflight_report",
        uri=str(report_path),
        metadata={"ok": report.ok},
    )
    return report_payload


def install_curated_ci_skills(*, skills_root: Path) -> dict[str, Any]:
    installer_script = (
        Path.home()
        / ".codex"
        / "skills"
        / ".system"
        / "skill-installer"
        / "scripts"
        / "install-skill-from-github.py"
    )
    if not installer_script.exists():
        return {
            "ok": False,
            "error": f"installer script not found: {installer_script}",
        }
    targets = [
        ("gh-fix-ci", "skills/.curated/gh-fix-ci"),
        ("gh-address-comments", "skills/.curated/gh-address-comments"),
    ]
    results: list[dict[str, Any]] = []
    all_ok = True
    for name, remote_path in targets:
        dest = skills_root / name
        if dest.exists():
            results.append(
                {
                    "name": name,
                    "status": "already_exists",
                    "dest": str(dest),
                    "command": None,
                }
            )
            continue
        cmd = [
            sys.executable,
            str(installer_script),
            "--repo",
            "openai/skills",
            "--path",
            remote_path,
            "--dest",
            str(skills_root),
        ]
        completed = subprocess.run(  # noqa: S603
            cmd,
            text=True,
            capture_output=True,
            check=False,
        )
        stderr = completed.stderr.strip()
        stdout = completed.stdout.strip()
        if completed.returncode == 0:
            status = "installed"
        elif "Destination already exists" in stderr and dest.exists():
            status = "already_exists"
        else:
            status = "failed"
            all_ok = False
        results.append(
            {
                "name": name,
                "status": status,
                "dest": str(dest),
                "command": cmd,
                "exit_code": completed.returncode,
                "stdout": stdout,
                "stderr": stderr,
            }
        )

    return {
        "ok": all_ok,
        "results": results,
    }


def run_github_sync_once(
    *,
    service: OrchestratorService,
    executor: ScriptExecutor,
    run_id: str | None,
    limit: int,
    dry_run: bool,
) -> dict[str, Any]:
    candidates = resolve_github_sync_candidates(service=service, run_id=run_id, limit=limit)
    results: list[dict[str, Any]] = []
    failures: list[dict[str, Any]] = []
    for candidate in candidates:
        rid = str(candidate["run_id"])
        try:
            snapshot = service.get_run_snapshot(rid)
            run = snapshot["run"]
            pr_number = run.get("pr_number")
            if pr_number is None:
                continue
            repo_dir = Path(run["workspace_dir"])
            if not repo_dir.exists():
                failures.append(
                    {
                        "run_id": rid,
                        "error": f"workspace not found: {repo_dir}",
                    }
                )
                continue

            gh_result = executor.run_gh_pr_view(repo_dir=repo_dir, pr_number=int(pr_number))
            service.add_step_attempt(
                rid,
                step=StepName.GITHUB_SYNC,
                exit_code=gh_result.exit_code,
                stdout_log=gh_result.stdout,
                stderr_log=gh_result.stderr,
                duration_ms=gh_result.duration_ms,
            )
            if gh_result.exit_code != 0:
                failures.append(
                    {
                        "run_id": rid,
                        "pr_number": pr_number,
                        "error": gh_result.stderr.strip() or "gh pr view failed",
                    }
                )
                continue
            try:
                payload = json.loads(gh_result.stdout)
            except json.JSONDecodeError:
                failures.append(
                    {
                        "run_id": rid,
                        "pr_number": pr_number,
                        "error": "invalid JSON from gh pr view",
                    }
                )
                continue

            decision = build_sync_decision(payload)
            applied_events: list[dict[str, Any]] = []
            if not dry_run and decision.check_conclusion is not None:
                applied_events.append(
                    service.record_github_check(
                        rid,
                        conclusion=decision.check_conclusion,
                        pr_number=int(pr_number),
                    )
                )
            if not dry_run and decision.review_state == "changes_requested":
                applied_events.append(
                    service.record_review(
                        rid,
                        review_state="changes_requested",
                    )
                )

            results.append(
                {
                    "run_id": rid,
                    "repo": f"{run['owner']}/{run['repo']}",
                    "state_before": snapshot["state"],
                    "pr_number": pr_number,
                    "decision": {
                        "check_conclusion": decision.check_conclusion,
                        "review_state": decision.review_state,
                        "check_summary": {
                            "total": decision.check_summary.total,
                            "successes": decision.check_summary.successes,
                            "failures": decision.check_summary.failures,
                            "pending": decision.check_summary.pending,
                            "unknown": decision.check_summary.unknown,
                        },
                    },
                    "events": applied_events,
                }
            )
        except InvalidTransitionError as exc:
            failures.append(
                {
                    "run_id": rid,
                    "error": f"state transition error during sync: {exc}",
                }
            )
        except Exception as exc:  # noqa: BLE001
            failures.append(
                {
                    "run_id": rid,
                    "error": f"unexpected sync error: {exc}",
                }
            )

    report_payload = {
        "ok": len(failures) == 0,
        "dry_run": dry_run,
        "scanned": len(candidates),
        "synced": len(results),
        "failures": failures,
        "results": results,
    }
    report_path = write_github_sync_report(report_payload)
    report_payload["report_path"] = str(report_path)
    return report_payload


def gather_skills_metrics(
    *,
    service: OrchestratorService,
    run_id: str | None,
    limit: int,
) -> dict[str, Any]:
    if run_id:
        artifacts = service.list_artifacts(
            run_id,
            artifact_type="agent_runtime_report",
            limit=limit,
        )
    else:
        artifacts = service.list_artifacts_global(
            artifact_type="agent_runtime_report",
            limit=limit,
        )

    mode_counts: dict[str, int] = {}
    grade_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    state_counts: dict[str, int] = {}
    per_skill: dict[str, dict[str, Any]] = {}
    missing_required_counts: dict[str, int] = {}
    failures: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    for artifact in artifacts:
        uri = Path(str(artifact.get("uri", "")))
        if not uri.exists():
            failures.append(
                {
                    "run_id": artifact.get("run_id"),
                    "artifact_id": artifact.get("id"),
                    "error": f"missing runtime report: {uri}",
                }
            )
            continue
        try:
            payload = json.loads(uri.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(
                {
                    "run_id": artifact.get("run_id"),
                    "artifact_id": artifact.get("id"),
                    "error": f"invalid runtime report {uri}: {exc}",
                }
            )
            continue

        runtime = payload.get("runtime") if isinstance(payload, dict) else {}
        classification = payload.get("classification") if isinstance(payload, dict) else {}
        runtime = runtime if isinstance(runtime, dict) else {}
        classification = classification if isinstance(classification, dict) else {}
        skills_mode = str(runtime.get("skills_mode", "off"))
        grade = str(classification.get("grade", "UNKNOWN"))
        reason_code = str(classification.get("reason_code", "unknown"))
        skill_plan = runtime.get("skill_plan")
        run_state = None
        if isinstance(skill_plan, dict) and skill_plan.get("run_state") is not None:
            run_state = str(skill_plan.get("run_state"))

        mode_counts[skills_mode] = mode_counts.get(skills_mode, 0) + 1
        grade_counts[grade] = grade_counts.get(grade, 0) + 1
        reason_counts[reason_code] = reason_counts.get(reason_code, 0) + 1
        if run_state:
            state_counts[run_state] = state_counts.get(run_state, 0) + 1

        required_now: list[str] = []
        available_optional: list[str] = []
        missing_required: list[str] = []
        if isinstance(skill_plan, dict):
            required_now = [str(item) for item in skill_plan.get("required_now", []) if str(item).strip()]
            available_optional = [str(item) for item in skill_plan.get("available_optional", []) if str(item).strip()]
            missing_required = [str(item) for item in skill_plan.get("missing_required", []) if str(item).strip()]
        for skill_name in missing_required:
            missing_required_counts[skill_name] = missing_required_counts.get(skill_name, 0) + 1

        seen_skills = sorted(set(required_now + available_optional))
        for skill_name in seen_skills:
            stats = per_skill.setdefault(
                skill_name,
                {
                    "skill": skill_name,
                    "samples": 0,
                    "grades": {},
                    "reasons": {},
                    "states": {},
                    "runs": [],
                },
            )
            stats["samples"] += 1
            stats["grades"][grade] = stats["grades"].get(grade, 0) + 1
            stats["reasons"][reason_code] = stats["reasons"].get(reason_code, 0) + 1
            if run_state:
                stats["states"][run_state] = stats["states"].get(run_state, 0) + 1
            if len(stats["runs"]) < 20:
                stats["runs"].append(str(payload.get("run_id") or artifact.get("run_id") or ""))

        rows.append(
            {
                "run_id": str(payload.get("run_id") or artifact.get("run_id") or ""),
                "created_at": str(payload.get("created_at") or artifact.get("created_at") or ""),
                "skills_mode": skills_mode,
                "run_state": run_state,
                "grade": grade,
                "reason_code": reason_code,
                "attempt_no": runtime.get("attempt_no"),
                "max_retryable_attempts": runtime.get("max_retryable_attempts"),
                "required_now": required_now,
                "available_optional": available_optional,
                "missing_required": missing_required,
                "report_path": str(uri),
            }
        )

    return {
        "ok": True,
        "scope_run_id": run_id,
        "scanned_artifacts": len(artifacts),
        "parsed_reports": len(rows),
        "mode_counts": mode_counts,
        "grade_counts": grade_counts,
        "reason_code_counts": reason_counts,
        "state_counts": state_counts,
        "per_skill": sorted(per_skill.values(), key=lambda row: int(row.get("samples", 0)), reverse=True),
        "missing_required_counts": missing_required_counts,
        "latest": rows[:20],
        "failures": failures[:20],
    }


def build_skills_feedback_report(
    *,
    metrics: dict[str, Any],
    min_samples: int,
) -> dict[str, Any]:
    parsed_reports = int(metrics.get("parsed_reports") or 0)
    grade_counts_raw = metrics.get("grade_counts")
    grade_counts = grade_counts_raw if isinstance(grade_counts_raw, dict) else {}
    reason_counts_raw = metrics.get("reason_code_counts")
    reason_counts = reason_counts_raw if isinstance(reason_counts_raw, dict) else {}
    per_skill_raw = metrics.get("per_skill")
    per_skill = per_skill_raw if isinstance(per_skill_raw, list) else []
    missing_required_raw = metrics.get("missing_required_counts")
    missing_required_counts = (
        missing_required_raw if isinstance(missing_required_raw, dict) else {}
    )

    pass_count = int(grade_counts.get("PASS") or 0)
    retryable_count = int(grade_counts.get("RETRYABLE") or 0)
    human_review_count = int(grade_counts.get("HUMAN_REVIEW") or 0)
    total = max(parsed_reports, 0)

    def pct(value: int) -> float:
        if total <= 0:
            return 0.0
        return round(100.0 * float(value) / float(total), 2)

    top_reasons = sorted(
        (
            {"reason_code": str(key), "count": int(value)}
            for key, value in reason_counts.items()
        ),
        key=lambda row: int(row["count"]),
        reverse=True,
    )[:10]

    actions: list[dict[str, Any]] = []

    def add_action(
        *,
        action_id: str,
        priority: str,
        target: str,
        trigger: str,
        recommendation: str,
        acceptance: str,
    ) -> None:
        actions.append(
            {
                "id": action_id,
                "priority": priority,
                "target": target,
                "trigger": trigger,
                "recommendation": recommendation,
                "acceptance_check": acceptance,
            }
        )

    evidence_gap_count = int(reason_counts.get("missing_test_evidence") or 0) + int(
        reason_counts.get("insufficient_test_evidence") or 0
    )
    if evidence_gap_count > 0:
        add_action(
            action_id="prompt-impl-test-evidence",
            priority="high",
            target="agentpr-implement-and-validate + prompt_template",
            trigger=f"evidence_gap_count={evidence_gap_count}",
            recommendation=(
                "Strengthen implement/validate stage to require CI-aligned test commands "
                "with explicit command outputs in final report."
            ),
            acceptance=(
                "reason_code missing/insufficient_test_evidence decreases over next 10 runs."
            ),
        )

    failed_test_count = int(reason_counts.get("test_command_failed") or 0)
    if failed_test_count > 0:
        add_action(
            action_id="policy-known-baseline-failures",
            priority="high",
            target="manager_policy.run_agent_step.known_test_failure_allowlist",
            trigger=f"test_command_failed={failed_test_count}",
            recommendation=(
                "Split baseline failures from integration failures; add only verified baseline "
                "signatures to allowlist per repo, keep unknown failures as HUMAN_REVIEW."
            ),
            acceptance=(
                "reason_code test_command_failed decreases without increasing post-review regressions."
            ),
        )

    diff_budget_count = int(reason_counts.get("diff_budget_exceeded") or 0)
    if diff_budget_count > 0:
        add_action(
            action_id="policy-minimal-diff-tighten",
            priority="medium",
            target="manager_policy.run_agent_step.repo_overrides",
            trigger=f"diff_budget_exceeded={diff_budget_count}",
            recommendation=(
                "Tighten max_changed_files/max_added_lines for affected repos and reinforce "
                "minimal patch instructions in implement skill."
            ),
            acceptance=(
                "median changed_files_count and added_lines decrease while PASS rate stays stable."
            ),
        )

    transient_count = int(reason_counts.get("runtime_transient_failure") or 0) + int(
        reason_counts.get("runtime_unknown_failure") or 0
    )
    if transient_count > 0:
        add_action(
            action_id="policy-retry-and-timeout-calibration",
            priority="medium",
            target="manager_policy.run_agent_step.max_agent_seconds/retry caps",
            trigger=f"transient_like_failures={transient_count}",
            recommendation=(
                "Calibrate timeout and retry caps per repo; separate network/tooling failures "
                "from code-quality failures before re-dispatch."
            ),
            acceptance=(
                "RETRYABLE share decreases and retryable_limit_exceeded remains low."
            ),
        )

    if missing_required_counts:
        missing_names = ", ".join(sorted(str(key) for key in missing_required_counts.keys()))
        add_action(
            action_id="skills-install-integrity",
            priority="high",
            target="skills installation + bootstrap checks",
            trigger=f"missing_required_counts={missing_names}",
            recommendation=(
                "Enforce required skill presence in manager startup and fail fast before dispatch."
            ),
            acceptance=(
                "missing_required_counts remains empty for all new runs."
            ),
        )

    low_pass_skills: list[dict[str, Any]] = []
    for row in per_skill:
        if not isinstance(row, dict):
            continue
        skill_name = str(row.get("skill") or "").strip()
        if not skill_name:
            continue
        samples = max(int(row.get("samples") or 0), 0)
        if samples < max(int(min_samples), 1):
            continue
        grades_raw = row.get("grades")
        grades = grades_raw if isinstance(grades_raw, dict) else {}
        pass_samples = int(grades.get("PASS") or 0)
        pass_rate_pct = round(100.0 * pass_samples / max(samples, 1), 2)
        if pass_rate_pct >= 60.0:
            continue
        reasons_raw = row.get("reasons")
        reasons = reasons_raw if isinstance(reasons_raw, dict) else {}
        dominant_reason = ""
        dominant_count = 0
        for key, value in reasons.items():
            count = int(value or 0)
            if count > dominant_count:
                dominant_reason = str(key)
                dominant_count = count
        low_pass_skills.append(
            {
                "skill": skill_name,
                "samples": samples,
                "pass_rate_pct": pass_rate_pct,
                "dominant_reason": dominant_reason,
                "dominant_reason_count": dominant_count,
            }
        )
    low_pass_skills.sort(key=lambda row: float(row["pass_rate_pct"]))
    for row in low_pass_skills[:5]:
        add_action(
            action_id=f"skill-tune-{str(row['skill']).replace('/', '-')}",
            priority="medium",
            target=str(row["skill"]),
            trigger=(
                f"samples={row['samples']},pass_rate_pct={row['pass_rate_pct']},"
                f"dominant_reason={row['dominant_reason']}"
            ),
            recommendation=(
                "Update skill checklist/exit criteria for dominant failure mode and "
                "re-run A/B on at least 3 repos."
            ),
            acceptance=(
                f"{row['skill']} pass_rate_pct reaches >= 70 over next {max(int(min_samples), 1)}+ samples."
            ),
        )

    stamp = datetime.now(UTC).strftime("%Y%m%d")
    return {
        "ok": True,
        "generated_at": datetime.now(UTC).isoformat(),
        "scope_run_id": metrics.get("scope_run_id"),
        "input_summary": {
            "parsed_reports": parsed_reports,
            "pass_count": pass_count,
            "retryable_count": retryable_count,
            "human_review_count": human_review_count,
            "pass_rate_pct": pct(pass_count),
            "retryable_rate_pct": pct(retryable_count),
            "human_review_rate_pct": pct(human_review_count),
        },
        "top_reasons": top_reasons,
        "low_pass_skills": low_pass_skills[:10],
        "actions": actions,
        "suggested_versions": {
            "prompt_version": f"auto-prompt-{stamp}",
            "skills_bundle_version": f"auto-skills-{stamp}",
        },
        "metrics_snapshot": {
            "grade_counts": grade_counts,
            "reason_code_counts": reason_counts,
            "missing_required_counts": missing_required_counts,
        },
    }


def write_skills_feedback_json(payload: dict[str, Any]) -> Path:
    reports_dir = PROJECT_ROOT / "orchestrator" / "data" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    out_path = reports_dir / f"skills_feedback_{stamp}.json"
    out_path.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return out_path


def render_skills_feedback_markdown(payload: dict[str, Any]) -> str:
    summary_raw = payload.get("input_summary")
    summary = summary_raw if isinstance(summary_raw, dict) else {}
    top_reasons_raw = payload.get("top_reasons")
    top_reasons = top_reasons_raw if isinstance(top_reasons_raw, list) else []
    actions_raw = payload.get("actions")
    actions = actions_raw if isinstance(actions_raw, list) else []
    suggested_versions_raw = payload.get("suggested_versions")
    suggested_versions = (
        suggested_versions_raw if isinstance(suggested_versions_raw, dict) else {}
    )

    lines = [
        "# Skills Feedback",
        "",
        f"- Generated at: {payload.get('generated_at', '')}",
        f"- Scope run_id: {payload.get('scope_run_id', '') or 'global'}",
        "",
        "## Summary",
        f"- Parsed reports: {summary.get('parsed_reports', 0)}",
        f"- PASS rate: {summary.get('pass_rate_pct', 0)}%",
        f"- RETRYABLE rate: {summary.get('retryable_rate_pct', 0)}%",
        f"- HUMAN_REVIEW rate: {summary.get('human_review_rate_pct', 0)}%",
        "",
        "## Top Reasons",
    ]
    if top_reasons:
        for row in top_reasons[:10]:
            if not isinstance(row, dict):
                continue
            lines.append(f"- {row.get('reason_code', '')}: {row.get('count', 0)}")
    else:
        lines.append("- No reason-code data.")

    lines.extend(["", "## Actions"])
    if actions:
        for idx, row in enumerate(actions, start=1):
            if not isinstance(row, dict):
                continue
            lines.append(
                f"{idx}. [{row.get('priority', 'normal')}] {row.get('id', '')} -> {row.get('target', '')}"
            )
            lines.append(f"   Trigger: {row.get('trigger', '')}")
            lines.append(f"   Change: {row.get('recommendation', '')}")
            lines.append(f"   Accept: {row.get('acceptance_check', '')}")
    else:
        lines.append("1. No actions generated; keep current policy/prompt and continue sampling.")

    lines.extend(
        [
            "",
            "## Suggested Versions",
            f"- prompt_version: {suggested_versions.get('prompt_version', '')}",
            f"- skills_bundle_version: {suggested_versions.get('skills_bundle_version', '')}",
            "",
        ]
    )
    return "\n".join(lines)


def write_skills_feedback_markdown(payload: dict[str, Any]) -> Path:
    reports_dir = PROJECT_ROOT / "orchestrator" / "data" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    out_path = reports_dir / f"skills_feedback_{stamp}.md"
    out_path.write_text(
        render_skills_feedback_markdown(payload),
        encoding="utf-8",
    )
    return out_path


def summarize_webhook_audit_log(
    *,
    audit_log_file: Path,
    since_minutes: int,
    max_lines: int,
    fail_on_retryable_failures: int | None,
    fail_on_http5xx_rate: float | None,
) -> dict[str, Any]:
    if not audit_log_file.exists():
        return {
            "ok": False,
            "error": f"audit log file not found: {audit_log_file}",
            "audit_log_file": str(audit_log_file),
            "since_minutes": since_minutes,
            "max_lines": max_lines,
        }

    try:
        lines = audit_log_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return {
            "ok": False,
            "error": f"failed to read audit log: {exc}",
            "audit_log_file": str(audit_log_file),
            "since_minutes": since_minutes,
            "max_lines": max_lines,
        }

    tail_lines = lines[-max_lines:]
    now = datetime.now(UTC)
    window_start = now - timedelta(minutes=max(since_minutes, 1))
    outcome_counts: dict[str, int] = {}
    status_code_counts: dict[str, int] = {}
    error_counts: dict[str, int] = {}
    retryable_failures = 0
    http_5xx_count = 0
    parse_errors = 0
    considered = 0

    for line in tail_lines:
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        if not isinstance(payload, dict):
            parse_errors += 1
            continue
        raw_ts = payload.get("ts")
        if raw_ts is None:
            parse_errors += 1
            continue
        try:
            ts = datetime.fromisoformat(str(raw_ts))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            ts = ts.astimezone(UTC)
        except ValueError:
            parse_errors += 1
            continue
        if ts < window_start:
            continue
        considered += 1
        outcome = str(payload.get("outcome", "unknown"))
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1

        status_code = int(payload.get("status_code", 0))
        status_key = str(status_code)
        status_code_counts[status_key] = status_code_counts.get(status_key, 0) + 1
        if status_code >= 500:
            http_5xx_count += 1

        retryable_value = int(payload.get("retryable_failures", 0))
        retryable_failures += max(retryable_value, 0)

        error_text = str(payload.get("error", "")).strip()
        if error_text:
            error_counts[error_text] = error_counts.get(error_text, 0) + 1

    http_5xx_rate_pct = (
        round((http_5xx_count / considered) * 100.0, 2) if considered > 0 else 0.0
    )
    alerts: list[str] = []
    ok = True
    if (
        fail_on_retryable_failures is not None
        and retryable_failures > int(fail_on_retryable_failures)
    ):
        ok = False
        alerts.append(
            "retryable_failures_exceeded:"
            f"{retryable_failures}>{int(fail_on_retryable_failures)}"
        )
    if fail_on_http5xx_rate is not None and http_5xx_rate_pct > float(fail_on_http5xx_rate):
        ok = False
        alerts.append(
            "http_5xx_rate_exceeded:"
            f"{http_5xx_rate_pct}>{float(fail_on_http5xx_rate)}"
        )

    top_errors = sorted(error_counts.items(), key=lambda item: item[1], reverse=True)[:10]
    return {
        "ok": ok,
        "audit_log_file": str(audit_log_file),
        "since_minutes": since_minutes,
        "window_start": window_start.isoformat(),
        "window_end": now.isoformat(),
        "max_lines": max_lines,
        "total_lines_read": len(tail_lines),
        "parse_errors": parse_errors,
        "considered_entries": considered,
        "outcome_counts": outcome_counts,
        "status_code_counts": status_code_counts,
        "retryable_failures": retryable_failures,
        "http_5xx_count": http_5xx_count,
        "http_5xx_rate_pct": http_5xx_rate_pct,
        "top_errors": [{"error": text, "count": count} for text, count in top_errors],
        "alerts": alerts,
    }


def summarize_command_categories(commands: list[str]) -> dict[str, int]:
    return rt.summarize_command_categories(commands)


def extract_failed_test_commands(event_summary: dict[str, Any]) -> list[str]:
    return rt.extract_failed_test_commands(event_summary)


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


def recommended_actions_for_state(state: RunState) -> list[str]:
    state_actions: dict[RunState, list[str]] = {
        RunState.QUEUED: [
            "start-discovery --run-id <run_id>",
            "run-prepare --run-id <run_id>",
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
            "request-open-pr --run-id <run_id> --title <title> --body-file <path>",
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
        RunState.PAUSED: [
            "resume --run-id <run_id> --target-state <state>",
        ],
        RunState.DONE: [],
        RunState.SKIPPED: [],
        RunState.FAILED_TERMINAL: [],
    }
    return state_actions.get(state, [])


def gather_run_inspect(
    *,
    service: OrchestratorService,
    run_id: str,
    attempt_limit: int,
    event_limit: int,
    command_limit: int,
    include_log_tails: bool,
) -> dict[str, Any]:
    snapshot = service.get_run_snapshot(run_id)
    run = snapshot["run"]
    current_state = RunState(str(snapshot["state"]))

    attempt_rows = service.list_step_attempts(
        run_id,
        limit=max(int(attempt_limit), 1),
    )
    # DB method returns newest first; manager view prefers chronological timeline.
    attempt_rows = list(reversed(attempt_rows))

    total_duration_ms = 0
    per_step: dict[str, dict[str, Any]] = {}
    attempts_out: list[dict[str, Any]] = []
    for row in attempt_rows:
        step = str(row.get("step", "unknown"))
        duration_ms = int(row.get("duration_ms") or 0)
        total_duration_ms += max(duration_ms, 0)
        info = per_step.setdefault(
            step,
            {
                "attempts": 0,
                "total_duration_ms": 0,
                "durations_ms": [],
                "last_exit_code": None,
            },
        )
        info["attempts"] += 1
        info["total_duration_ms"] += max(duration_ms, 0)
        info["durations_ms"].append(max(duration_ms, 0))
        info["last_exit_code"] = int(row.get("exit_code") or 0)

        item = {
            "step": step,
            "attempt_no": int(row.get("attempt_no") or 0),
            "exit_code": int(row.get("exit_code") or 0),
            "duration_ms": duration_ms,
            "created_at": str(row.get("created_at") or ""),
        }
        if include_log_tails:
            item["stdout_tail"] = tail(str(row.get("stdout_log") or ""))
            item["stderr_tail"] = tail(str(row.get("stderr_log") or ""))
        attempts_out.append(item)

    step_totals: list[dict[str, Any]] = []
    for step, info in per_step.items():
        durations = [int(v) for v in info["durations_ms"]]
        total_ms = int(info["total_duration_ms"])
        step_totals.append(
            {
                "step": step,
                "attempts": int(info["attempts"]),
                "total_duration_ms": total_ms,
                "avg_duration_ms": int(total_ms / max(int(info["attempts"]), 1)),
                "p50_duration_ms": percentile_ms(durations, 0.50),
                "p90_duration_ms": percentile_ms(durations, 0.90),
                "last_exit_code": info["last_exit_code"],
                "share_of_total_pct": round((100.0 * total_ms / total_duration_ms), 2)
                if total_duration_ms > 0
                else 0.0,
            }
        )
    step_totals.sort(key=lambda row: int(row["total_duration_ms"]), reverse=True)
    warnings: list[str] = []
    preflight_attempts = next(
        (int(row["attempts"]) for row in step_totals if str(row["step"]) == StepName.PREFLIGHT.value),
        0,
    )
    if preflight_attempts > 1:
        warnings.append(
            "multiple preflight attempts detected; avoid running both standalone "
            "run-preflight and default preflight inside run-agent-step unless required."
        )
    if step_totals and str(step_totals[0]["step"]) == StepName.AGENT.value:
        share_pct = float(step_totals[0]["share_of_total_pct"])
        if share_pct >= 85.0:
            warnings.append(
                f"agent step dominates runtime ({share_pct:.2f}%). focus optimization on "
                "dependency install/test scope and prompt-stage tasking."
            )

    event_rows = service.list_events(
        run_id,
        limit=max(int(event_limit), 1),
    )
    event_rows = list(reversed(event_rows))
    events_out: list[dict[str, Any]] = []
    for row in event_rows:
        payload = row.get("payload")
        payload_obj = payload if isinstance(payload, dict) else {}
        compact_payload = {
            key: payload_obj[key]
            for key in sorted(payload_obj.keys())[:8]
        }
        events_out.append(
            {
                "event_id": int(row.get("event_id") or 0),
                "event_type": str(row.get("event_type") or ""),
                "created_at": str(row.get("created_at") or ""),
                "idempotency_key": str(row.get("idempotency_key") or ""),
                "payload": compact_payload,
            }
        )

    latest_runtime = service.latest_artifact(run_id, artifact_type="agent_runtime_report")
    runtime_summary: dict[str, Any] | None = None
    if latest_runtime is not None:
        path = Path(str(latest_runtime.get("uri", "")))
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                signals = payload.get("signals")
                signals = signals if isinstance(signals, dict) else {}
                classification = payload.get("classification")
                classification = classification if isinstance(classification, dict) else {}
                runtime = payload.get("runtime")
                runtime = runtime if isinstance(runtime, dict) else {}
                result = payload.get("result")
                result = result if isinstance(result, dict) else {}
                commands_sample = [
                    str(item).strip()
                    for item in (signals.get("commands_sample") or [])
                    if str(item).strip()
                ]
                test_commands = [
                    str(item).strip()
                    for item in (signals.get("test_commands") or [])
                    if str(item).strip()
                ]
                failed_test_commands = [
                    str(item).strip()
                    for item in (signals.get("failed_test_commands") or [])
                    if str(item).strip()
                ]
                command_categories = signals.get("command_categories")
                if not isinstance(command_categories, dict):
                    command_categories = summarize_command_categories(commands_sample)
                agent_event_summary = signals.get("agent_event_summary")
                if not isinstance(agent_event_summary, dict):
                    agent_event_summary = {}
                event_stream_path = str(runtime.get("event_stream_path") or "")
                last_message_path = str(runtime.get("last_message_path") or "")
                last_message_preview = ""
                if last_message_path:
                    preview_path = Path(last_message_path)
                    if preview_path.exists():
                        try:
                            last_message_preview = tail(
                                preview_path.read_text(encoding="utf-8"),
                                lines=10,
                            )
                        except OSError:
                            last_message_preview = ""
                runtime_summary = {
                    "report_path": str(path),
                    "created_at": str(payload.get("created_at") or latest_runtime.get("created_at") or ""),
                    "duration_ms": int(result.get("duration_ms") or 0),
                    "exit_code": int(result.get("exit_code") or 0),
                    "grade": str(classification.get("grade") or ""),
                    "reason_code": str(classification.get("reason_code") or ""),
                    "next_action": str(classification.get("next_action") or ""),
                    "skills_mode": str(runtime.get("skills_mode") or "off"),
                    "attempt_no": runtime.get("attempt_no"),
                    "test_commands": test_commands[:20],
                    "test_command_count": len(test_commands),
                    "failed_test_commands": failed_test_commands[:20],
                    "failed_test_command_count": len(failed_test_commands),
                    "command_sample": commands_sample[: max(int(command_limit), 1)],
                    "command_sample_count": len(commands_sample),
                    "command_categories": command_categories,
                    "agent_event_summary": agent_event_summary,
                    "event_stream_path": event_stream_path,
                    "last_message_path": last_message_path,
                    "last_message_preview": last_message_preview,
                    "diff": signals.get("diff"),
                }

    latest_digest_summary: dict[str, Any] | None = None
    latest_digest = service.latest_artifact(run_id, artifact_type="run_digest")
    if latest_digest is not None:
        digest_path = Path(str(latest_digest.get("uri", "")))
        if digest_path.exists():
            try:
                digest_payload = json.loads(digest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                digest_payload = None
            if isinstance(digest_payload, dict):
                classification = digest_payload.get("classification")
                classification = classification if isinstance(classification, dict) else {}
                state = digest_payload.get("state")
                state = state if isinstance(state, dict) else {}
                recommendation = digest_payload.get("manager_recommendation")
                recommendation = recommendation if isinstance(recommendation, dict) else {}
                stages = digest_payload.get("stages")
                stages = stages if isinstance(stages, dict) else {}
                latest_digest_summary = {
                    "path": str(digest_path),
                    "generated_at": str(digest_payload.get("generated_at") or ""),
                    "grade": str(classification.get("grade") or ""),
                    "reason_code": str(classification.get("reason_code") or ""),
                    "state_before": str(state.get("before") or ""),
                    "state_after": str(state.get("after") or ""),
                    "recommended_action": str(recommendation.get("action") or ""),
                    "top_step": str(stages.get("top_step") or ""),
                    "step_attempt_count": int(stages.get("step_attempt_count") or 0),
                }

    latest_manager_insight: dict[str, Any] | None = None
    insight_artifact = service.latest_artifact(run_id, artifact_type="manager_insight")
    if insight_artifact is not None:
        insight_path = Path(str(insight_artifact.get("uri", "")))
        if insight_path.exists():
            preview = ""
            try:
                preview = tail(insight_path.read_text(encoding="utf-8"), lines=24)
            except OSError:
                preview = ""
            latest_manager_insight = {
                "path": str(insight_path),
                "preview": preview,
            }

    step_start = (
        parse_optional_iso_datetime(str(attempt_rows[0]["created_at"]))
        if attempt_rows
        else None
    )
    step_end = (
        parse_optional_iso_datetime(str(attempt_rows[-1]["created_at"]))
        if attempt_rows
        else None
    )
    run_created = parse_optional_iso_datetime(str(run.get("created_at")))
    run_updated = parse_optional_iso_datetime(str(run.get("updated_at")))

    return {
        "ok": True,
        "run": {
            "run_id": str(run["run_id"]),
            "owner": str(run["owner"]),
            "repo": str(run["repo"]),
            "mode": str(run["mode"]),
            "prompt_version": str(run["prompt_version"]),
            "workspace_dir": str(run["workspace_dir"]),
            "pr_number": run.get("pr_number"),
            "created_at": str(run["created_at"]),
            "updated_at": str(run["updated_at"]),
        },
        "state": current_state.value,
        "allowed_targets": [item.value for item in allowed_targets(current_state)],
        "recommended_actions": recommended_actions_for_state(current_state),
        "warnings": warnings,
        "timing": {
            "step_attempt_count": len(attempt_rows),
            "total_step_duration_ms": total_duration_ms,
            "run_wall_clock_ms": int((run_updated - run_created).total_seconds() * 1000)
            if run_created is not None and run_updated is not None
            else None,
            "step_window_ms": int((step_end - step_start).total_seconds() * 1000)
            if step_start is not None and step_end is not None
            else None,
            "step_totals": step_totals,
            "attempts": attempts_out,
        },
        "events": {
            "count": len(events_out),
            "items": events_out,
        },
        "latest_agent_runtime": runtime_summary,
        "latest_run_digest": latest_digest_summary,
        "latest_manager_insight": latest_manager_insight,
    }


def gather_run_bottlenecks(
    *,
    service: OrchestratorService,
    limit: int,
    attempt_limit_per_run: int,
) -> dict[str, Any]:
    runs = service.list_runs(limit=max(int(limit), 1))
    per_step_samples: dict[str, list[int]] = defaultdict(list)
    run_totals: list[dict[str, Any]] = []
    analyzed_runs = 0
    for run in runs:
        run_id = str(run["run_id"])
        attempts = service.list_step_attempts(
            run_id,
            limit=max(int(attempt_limit_per_run), 1),
        )
        if not attempts:
            continue
        analyzed_runs += 1
        total_ms = 0
        by_step: dict[str, int] = defaultdict(int)
        for row in attempts:
            step = str(row.get("step") or "unknown")
            duration_ms = max(int(row.get("duration_ms") or 0), 0)
            total_ms += duration_ms
            by_step[step] += duration_ms
            per_step_samples[step].append(duration_ms)
        run_totals.append(
            {
                "run_id": run_id,
                "repo": str(run.get("repo") or ""),
                "owner": str(run.get("owner") or ""),
                "state": str(run.get("current_state") or ""),
                "prompt_version": str(run.get("prompt_version") or ""),
                "total_step_duration_ms": total_ms,
                "top_step": max(by_step.items(), key=lambda x: x[1])[0] if by_step else None,
                "top_step_duration_ms": max(by_step.values()) if by_step else 0,
                "updated_at": str(run.get("updated_at") or ""),
            }
        )

    step_stats: list[dict[str, Any]] = []
    for step, samples in per_step_samples.items():
        ordered = sorted(samples)
        step_stats.append(
            {
                "step": step,
                "samples": len(ordered),
                "avg_duration_ms": int(sum(ordered) / max(len(ordered), 1)),
                "median_duration_ms": int(statistics.median(ordered)),
                "p90_duration_ms": percentile_ms(ordered, 0.90),
                "max_duration_ms": int(max(ordered)),
            }
        )
    step_stats.sort(key=lambda row: int(row["avg_duration_ms"]), reverse=True)
    run_totals.sort(key=lambda row: int(row["total_step_duration_ms"]), reverse=True)

    return {
        "ok": True,
        "scanned_runs": len(runs),
        "analyzed_runs": analyzed_runs,
        "step_bottlenecks": step_stats,
        "slowest_runs": run_totals[:20],
    }


def resolve_github_sync_candidates(
    *,
    service: OrchestratorService,
    run_id: str | None,
    limit: int,
) -> list[dict[str, Any]]:
    if run_id is not None:
        snapshot = service.get_run_snapshot(run_id)
        run = snapshot["run"]
        return [
            {
                "run_id": run["run_id"],
                "current_state": snapshot["state"],
                "pr_number": run.get("pr_number"),
            }
        ]
    active_states = {
        RunState.CI_WAIT.value,
        RunState.REVIEW_WAIT.value,
        RunState.ITERATING.value,
    }
    rows = service.list_runs(limit=max(limit, 1))
    return [
        row
        for row in rows
        if row.get("pr_number") is not None and row.get("current_state") in active_states
    ]


def write_github_sync_report(payload: dict[str, Any]) -> Path:
    reports_dir = PROJECT_ROOT / "orchestrator" / "data" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    report_path = reports_dir / f"github_sync_{stamp}.json"
    report_path.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return report_path


def write_preflight_report(run_id: str, report_json: str) -> Path:
    reports_dir = PROJECT_ROOT / "orchestrator" / "data" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    report_path = reports_dir / f"{run_id}_preflight.json"
    report_path.write_text(report_json, encoding="utf-8")
    return report_path


def write_task_packet(run_id: str, payload: dict[str, Any]) -> Path:
    packets_dir = PROJECT_ROOT / "orchestrator" / "data" / "task_packets"
    packets_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    packet_path = packets_dir / f"{run_id}_task_packet_{stamp}.json"
    packet_path.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return packet_path


def prepare_worker_contract_artifact(
    *,
    repo_dir: Path,
    contract_source_uri: str | None,
    max_chars: int = 20000,
) -> tuple[str | None, str | None]:
    source = str(contract_source_uri or "").strip()
    if not source:
        return None, None
    source_path = Path(source)
    if not source_path.exists() or not source_path.is_file():
        return None, None

    try:
        text = source_path.read_text(encoding="utf-8")
    except OSError:
        text = ""
    normalized_text = text[: max(int(max_chars), 0)] if text else None

    dest_dir = repo_dir / ".agentpr_runtime" / "contracts"
    dest_dir.mkdir(parents=True, exist_ok=True)
    dest_path = dest_dir / source_path.name
    try:
        dest_path.write_text(text, encoding="utf-8")
    except OSError:
        return None, normalized_text
    return str(dest_path), normalized_text


def collect_repo_diff_summary(*, repo_dir: Path) -> dict[str, Any]:
    diff_names = run_git_text(repo_dir, ["diff", "--name-only", "HEAD"])
    diff_numstat = run_git_text(repo_dir, ["diff", "--numstat", "HEAD"])
    untracked = run_git_text(repo_dir, ["ls-files", "--others", "--exclude-standard"])

    changed_files: set[str] = set()
    ignored_files: set[str] = set()
    for line in diff_names.splitlines():
        stripped = line.strip()
        if stripped:
            normalized = _normalize_repo_relpath(stripped)
            if _is_ignored_runtime_path(normalized):
                ignored_files.add(normalized)
                continue
            changed_files.add(normalized)

    untracked_files: list[str] = []
    for line in untracked.splitlines():
        stripped = line.strip()
        if stripped:
            normalized = _normalize_repo_relpath(stripped)
            if _is_ignored_runtime_path(normalized):
                ignored_files.add(normalized)
                continue
            untracked_files.append(normalized)
            changed_files.add(normalized)

    added_lines = 0
    deleted_lines = 0
    for line in diff_numstat.splitlines():
        parts = line.split("\t")
        if len(parts) < 3:
            continue
        add_raw, del_raw, path_raw = (
            parts[0].strip(),
            parts[1].strip(),
            parts[2].strip(),
        )
        if _is_ignored_runtime_path(path_raw):
            ignored_files.add(_normalize_repo_relpath(path_raw))
            continue
        if add_raw.isdigit():
            added_lines += int(add_raw)
        if del_raw.isdigit():
            deleted_lines += int(del_raw)

    return {
        "changed_files": sorted(changed_files),
        "changed_files_count": len(changed_files),
        "untracked_files": sorted(untracked_files),
        "untracked_files_count": len(untracked_files),
        "ignored_files": sorted(ignored_files),
        "ignored_files_count": len(ignored_files),
        "added_lines": added_lines,
        "deleted_lines": deleted_lines,
    }


def compact_diff_summary(
    summary: dict[str, Any],
    *,
    sample_limit: int = 8,
) -> dict[str, Any]:
    limit = max(int(sample_limit), 1)

    def _sample(name: str) -> list[str]:
        raw = summary.get(name)
        if not isinstance(raw, list):
            return []
        out: list[str] = []
        for item in raw[:limit]:
            text = str(item).strip()
            if text:
                out.append(text)
        return out

    changed_sample = _sample("changed_files")
    untracked_sample = _sample("untracked_files")
    ignored_sample = _sample("ignored_files")
    changed_total = int(summary.get("changed_files_count") or 0)
    untracked_total = int(summary.get("untracked_files_count") or 0)
    ignored_total = int(summary.get("ignored_files_count") or 0)

    return {
        "changed_files_count": changed_total,
        "changed_files_sample": changed_sample,
        "changed_files_sample_truncated": max(changed_total - len(changed_sample), 0),
        "untracked_files_count": untracked_total,
        "untracked_files_sample": untracked_sample,
        "untracked_files_sample_truncated": max(untracked_total - len(untracked_sample), 0),
        "ignored_files_count": ignored_total,
        "ignored_files_sample": ignored_sample,
        "ignored_files_sample_truncated": max(ignored_total - len(ignored_sample), 0),
        "added_lines": int(summary.get("added_lines") or 0),
        "deleted_lines": int(summary.get("deleted_lines") or 0),
    }


def run_git_text(repo_dir: Path, args: list[str]) -> str:
    completed = subprocess.run(  # noqa: S603
        ["git", *args],
        cwd=repo_dir,
        text=True,
        capture_output=True,
        check=False,
    )
    if completed.returncode != 0:
        return ""
    return completed.stdout


def converge_agent_success_state(
    service: OrchestratorService,
    args: argparse.Namespace,
    *,
    success_state: str | None = None,
) -> str:
    resolved_success_state = success_state if success_state is not None else args.success_state
    if resolved_success_state is None:
        return service.get_run_snapshot(args.run_id)["state"]
    if str(resolved_success_state).strip().upper() == "UNCHANGED":
        return service.get_run_snapshot(args.run_id)["state"]

    target = RunState(str(resolved_success_state))
    if target == RunState.LOCAL_VALIDATING:
        current_state = RunState(service.get_run_snapshot(args.run_id)["state"])
        if current_state in {RunState.LOCAL_VALIDATING, RunState.DISCOVERY, RunState.PLAN_READY}:
            return current_state.value
        result = service.mark_local_validation_passed(args.run_id)
        return str(result["state"])
    if target == RunState.NEEDS_HUMAN_REVIEW:
        result = service.retry_run(
            args.run_id,
            target_state=RunState.NEEDS_HUMAN_REVIEW,
        )
        return str(result["state"])
    raise ValueError(f"Unsupported success-state target: {resolved_success_state}")


def apply_nonpass_verdict_state(
    service: OrchestratorService,
    *,
    run_id: str,
    target_state: str | None,
) -> dict[str, Any]:
    if target_state is None:
        return service.get_run_snapshot(run_id)
    normalized = str(target_state).strip().upper()
    if normalized == "UNCHANGED":
        return service.get_run_snapshot(run_id)
    target = RunState(normalized)
    return service.retry_run(
        run_id,
        target_state=target,
    )


if __name__ == "__main__":
    sys.exit(main())
