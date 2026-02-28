from __future__ import annotations

import argparse
import json
import os
import re
import secrets
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .cli_helpers import (
    extract_pr_number,
    extract_pr_url,
    load_optional_text,
    load_prompt,
    parse_iso_datetime,
    print_json,
    tail,
)
from .cli_inspect import (
    build_skills_feedback_report,
    gather_run_bottlenecks,
    gather_run_inspect,
    gather_skills_metrics,
    summarize_webhook_audit_log,
    write_skills_feedback_json,
    write_skills_feedback_markdown,
)
from .cli_pr import (
    build_request_open_pr_body,
    read_pr_open_request,
    resolve_external_read_only_paths,
    write_pr_open_request,
)
from .cli_worker import (
    apply_nonpass_verdict_state,
    collect_repo_diff_summary,
    compact_diff_summary,
    converge_agent_success_state,
    install_curated_ci_skills,
    prepare_worker_contract_artifact,
    run_github_sync_once,
    run_preflight_checks,
    write_task_packet,
)
from .db import Database
from .executor import ScriptExecutor
from .github_webhook import run_github_webhook_server
from .manager_loop import ManagerLoopConfig, ManagerLoopRunner
from .manager_tools import analyze_worker_output, get_global_stats, notify_user
from .models import (
    AgentRuntimeGrade,
    RunCreateInput,
    RunMode,
    RunState,
    StepName,
)
from .manager_policy import load_manager_policy, resolve_run_agent_effective_policy
from .preflight import RuntimeDoctor
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
    scan_repo_governance_sources,
)
from .state_machine import InvalidTransitionError
from .telegram_bot import (
    TelegramClient,
    build_decision_llm_client_if_enabled,
    build_nl_llm_client_if_enabled,
    handle_bot_command,
    handle_natural_language,
    resolve_decision_why_mode,
    resolve_telegram_nl_mode,
    run_telegram_bot_loop,
    sync_last_run_id_from_text,
)

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
        choices=["off", "agentpr", "agentpr_autonomous"],
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

    d = sub.add_parser("start-discovery", help="Move to DISCOVERY (v1) or EXECUTING (v2)")
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

    i = sub.add_parser(
        "start-implementation",
        help="Move to IMPLEMENTING (v1) or keep EXECUTING (v2)",
    )
    i.add_argument("--run-id", required=True)
    add_idempotency_arg(i)

    lv = sub.add_parser(
        "mark-local-validated",
        help="Move to LOCAL_VALIDATING (v1) or keep EXECUTING (v2)",
    )
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
            RunState.EXECUTING.value,
            RunState.LOCAL_VALIDATING.value,
            RunState.NEEDS_HUMAN_REVIEW.value,
            "UNCHANGED",
        ],
        help=(
            "Optional state convergence after a successful agent run. "
            "Supported: EXECUTING, LOCAL_VALIDATING, NEEDS_HUMAN_REVIEW, UNCHANGED. "
            "If omitted, uses manager policy default."
        ),
    )
    ag.add_argument(
        "--on-retryable-state",
        choices=[
            RunState.FAILED.value,
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
            RunState.FAILED.value,
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
            "EXECUTING/IMPLEMENTING/LOCAL_VALIDATING/ITERATING on successful exit. "
            "If omitted, uses manager policy default."
        ),
    )
    ag.add_argument(
        "--runtime-grading-mode",
        choices=["rules", "hybrid", "hybrid_llm"],
        help=(
            "Runtime grading mode for this run-agent-step. "
            "'rules' keeps deterministic grading only; "
            "'hybrid' enables semantic override for no-test-infra repos; "
            "'hybrid_llm' adds manager-LLM semantic grading when available. "
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
        choices=["off", "agentpr", "agentpr_autonomous"],
        help=(
            "Enable skills-based prompt envelope for this run. "
            "agentpr mode invokes stage-specific AgentPR skills; "
            "agentpr_autonomous lets worker self-orchestrate multi-skill flow in one run. "
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
    ro_body_group = ro.add_mutually_exclusive_group(required=False)
    ro_body_group.add_argument("--body", help="PR body text")
    ro_body_group.add_argument("--body-file", type=Path, help="PR body file path")
    ro.add_argument("--base", help="Base branch (defaults to origin/HEAD)")
    ro.add_argument("--head", help="Head branch (defaults to current branch)")
    ro.add_argument(
        "--project-name",
        help=(
            "Project name used in Forge context text "
            "(defaults to repository name in workspace)."
        ),
    )
    ro.add_argument(
        "--skip-repo-pr-template",
        action="store_true",
        help="Skip auto-prepending repository PR template content.",
    )
    ro.add_argument(
        "--skip-about-forge",
        action="store_true",
        help="Skip appending the shared About Forge section.",
    )
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

    sb = sub.add_parser(
        "simulate-bot-session",
        help="Simulate bot/human message flow locally without Telegram network calls",
    )
    sb.add_argument(
        "--text",
        action="append",
        default=[],
        help="Input message in chronological order. Can be repeated.",
    )
    sb.add_argument(
        "--text-file",
        type=Path,
        help="Optional UTF-8 file with one message per line (appended after --text).",
    )
    sb.add_argument(
        "--list-limit",
        type=int,
        default=8,
        help="Default list/status limit used by bot handlers (default: 8).",
    )
    sb.add_argument(
        "--nl-mode",
        choices=["rules", "llm", "hybrid"],
        help="Override AGENTPR_TELEGRAM_NL_MODE for this simulation.",
    )
    sb.add_argument(
        "--decision-why-mode",
        choices=["off", "hybrid", "llm"],
        help="Override AGENTPR_TELEGRAM_DECISION_WHY_MODE for this simulation.",
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

    awo = sub.add_parser(
        "analyze-worker-output",
        help="Analyze latest worker output artifacts for one run",
    )
    awo.add_argument("--run-id", required=True)

    ggs = sub.add_parser(
        "get-global-stats",
        help="Summarize global run/grade/reason distributions",
    )
    ggs.add_argument(
        "--limit",
        type=int,
        default=200,
        help="Number of recent runs sampled (default: 200).",
    )

    nu = sub.add_parser(
        "notify-user",
        help="Record a manager notification artifact for one run",
    )
    nu.add_argument("--run-id", required=True)
    nu.add_argument("--message", required=True)
    nu.add_argument(
        "--priority",
        choices=["low", "normal", "high", "urgent"],
        default="normal",
    )
    nu.add_argument("--channel", default="manager")

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
    if command == "simulate-bot-session":
        return {
            "profile": "simulate",
            "check_network": False,
            "require_gh_auth": False,
            "require_codex": False,
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

        if args.command == "analyze-worker-output":
            report = analyze_worker_output(
                service=service,
                run_id=str(args.run_id).strip(),
            )
            print_json(report)
            return 0 if bool(report.get("ok", False)) else 1

        if args.command == "get-global-stats":
            report = get_global_stats(
                service=service,
                limit=max(int(args.limit), 1),
            )
            print_json(report)
            return 0

        if args.command == "notify-user":
            report = notify_user(
                service=service,
                run_id=str(args.run_id).strip(),
                message=str(args.message),
                priority=str(args.priority),
                channel=str(args.channel),
            )
            print_json(report)
            return 0

        if args.command == "simulate-bot-session":
            messages = [
                str(item).strip()
                for item in list(args.text or [])
                if str(item).strip()
            ]
            if args.text_file is not None:
                text_file = Path(args.text_file).expanduser()
                if not text_file.exists():
                    raise ValueError(f"Text file not found: {text_file}")
                file_lines = text_file.read_text(encoding="utf-8").splitlines()
                for raw in file_lines:
                    line = str(raw).strip()
                    if line:
                        messages.append(line)
            if not messages:
                raise ValueError("simulate-bot-session requires at least one message.")

            resolved_nl_mode = (
                str(args.nl_mode).strip().lower()
                if args.nl_mode is not None
                else resolve_telegram_nl_mode()
            )
            if resolved_nl_mode not in {"rules", "llm", "hybrid"}:
                resolved_nl_mode = "rules"
            resolved_decision_why_mode = (
                str(args.decision_why_mode).strip().lower()
                if args.decision_why_mode is not None
                else resolve_decision_why_mode()
            )
            if resolved_decision_why_mode not in {"off", "hybrid", "llm"}:
                resolved_decision_why_mode = "hybrid"

            nl_llm_client = build_nl_llm_client_if_enabled(
                nl_mode=resolved_nl_mode,
            )
            decision_llm_client = build_decision_llm_client_if_enabled(
                decision_why_mode=resolved_decision_why_mode,
                fallback_client=nl_llm_client,
            )
            conversation_state: dict[str, Any] = {}
            list_limit = max(int(args.list_limit), 1)
            transcript: list[dict[str, Any]] = []
            for idx, text in enumerate(messages, start=1):
                if text.startswith("/"):
                    response = handle_bot_command(
                        text=text,
                        service=service,
                        db_path=Path(args.db),
                        workspace_root=Path(args.workspace_root),
                        integration_root=Path(args.integration_root),
                        project_root=PROJECT_ROOT,
                        list_limit=list_limit,
                        decision_llm_client=decision_llm_client,
                        decision_why_mode=resolved_decision_why_mode,
                    )
                    sync_last_run_id_from_text(conversation_state, text)
                    input_mode = "command"
                else:
                    response = handle_natural_language(
                        text=text,
                        service=service,
                        db_path=Path(args.db),
                        workspace_root=Path(args.workspace_root),
                        integration_root=Path(args.integration_root),
                        project_root=PROJECT_ROOT,
                        list_limit=list_limit,
                        conversation_state=conversation_state,
                        llm_client=nl_llm_client,
                        nl_mode=resolved_nl_mode,
                        decision_llm_client=decision_llm_client,
                        decision_why_mode=resolved_decision_why_mode,
                    )
                    input_mode = "natural_language"
                transcript.append(
                    {
                        "index": idx,
                        "input_mode": input_mode,
                        "input": text,
                        "response": response,
                    }
                )
            print_json(
                {
                    "ok": True,
                    "message_count": len(messages),
                    "nl_mode": resolved_nl_mode,
                    "decision_why_mode": resolved_decision_why_mode,
                    "llm_clients": {
                        "nl_available": nl_llm_client is not None,
                        "decision_available": decision_llm_client is not None,
                    },
                    "last_run_id": str(conversation_state.get("last_run_id") or ""),
                    "transcript": transcript,
                }
            )
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
            resolved_runtime_grading_mode = (
                str(args.runtime_grading_mode).strip()
                if args.runtime_grading_mode is not None
                else str(effective_policy.get("runtime_grading_mode") or "hybrid")
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
            # Normalize legacy policy defaults to V2 states.
            if (
                args.success_state is None
                and resolved_success_state == RunState.LOCAL_VALIDATING.value
            ):
                resolved_success_state = RunState.EXECUTING.value
            if (
                args.on_retryable_state is None
                and resolved_on_retryable_state == RunState.FAILED_RETRYABLE.value
            ):
                resolved_on_retryable_state = RunState.FAILED.value
            if (
                args.on_human_review_state is None
                and resolved_on_human_review_state == RunState.FAILED_RETRYABLE.value
            ):
                resolved_on_human_review_state = RunState.FAILED.value
            preflight_report: dict[str, Any] | None = None
            if current_state == RunState.QUEUED:
                transitioned = service.start_discovery(args.run_id)
                current_state = RunState(str(transitioned["state"]))
            if current_state not in {
                RunState.EXECUTING,
                RunState.ITERATING,
                # Legacy states for old runs still in-flight:
                RunState.DISCOVERY,
                RunState.PLAN_READY,
                RunState.IMPLEMENTING,
                RunState.LOCAL_VALIDATING,
            }:
                raise ValueError(
                    "run-agent-step is allowed only in "
                    "EXECUTING/ITERATING states."
                )
            repo_dir = Path(run["workspace_dir"])
            if not repo_dir.exists():
                raise ValueError(f"Workspace not found: {repo_dir}")
            if (
                not args.allow_dirty_worktree
                and current_state in {
                    RunState.EXECUTING,
                    RunState.DISCOVERY,
                    RunState.PLAN_READY,
                    RunState.IMPLEMENTING,
                }
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
                include_skills_root=(
                    resolved_skills_mode in {"agentpr", "agentpr_autonomous"}
                ),
                user_paths=[str(item) for item in (args.allow_read_path or [])],
            )
            prompt = load_prompt(args)
            skill_plan_payload: dict[str, Any] | None = None
            task_packet_path: Path | None = None
            governance_scan: dict[str, Any] | None = None
            if resolved_skills_mode in {"agentpr", "agentpr_autonomous"}:
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
                            "error": "required skills are missing for enabled skills-mode",
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
                governance_scan = scan_repo_governance_sources(repo_dir=repo_dir)
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
                    governance_scan=governance_scan,
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
                        "primary_pr_template": str(
                            governance_scan.get("primary_pr_template") or ""
                        )
                        if isinstance(governance_scan, dict)
                        else "",
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
                runtime_grading_mode=resolved_runtime_grading_mode,
                known_test_failure_allowlist=resolved_known_test_failure_allowlist,
                attempt_no=agent_attempt_no,
                skills_mode=resolved_skills_mode,
                skill_plan=skill_plan_payload,
                repo_dir=repo_dir,
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
                        "runtime_grading_mode": policy_agent.runtime_grading_mode,
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
                        "resolved_runtime_grading_mode": resolved_runtime_grading_mode,
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
            user_body = load_optional_text(args.body, args.body_file, arg_name="PR body")
            project_name = (
                str(args.project_name).strip()
                if args.project_name is not None
                else repo_dir.name
            )
            body, body_meta = build_request_open_pr_body(
                repo_dir=repo_dir,
                integration_root=Path(args.integration_root),
                user_body=user_body,
                project_name=project_name,
                prepend_repo_pr_template=not bool(args.skip_repo_pr_template),
                append_about_forge=not bool(args.skip_about_forge),
            )
            if not body.strip():
                raise ValueError(
                    "PR body is empty after composition. "
                    "Pass --body/--body-file or provide a valid repo template."
                )
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
                "body_meta": body_meta,
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
                    "body_meta": body_meta,
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
                        "project_name": project_name,
                        "body_meta": body_meta,
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


if __name__ == "__main__":
    sys.exit(main())
