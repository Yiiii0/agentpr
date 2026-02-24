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

from .db import Database
from .executor import ScriptExecutor
from .github_webhook import run_github_webhook_server
from .github_sync import build_sync_decision
from .models import AgentRuntimeGrade, RunCreateInput, RunMode, RunState, StepName
from .preflight import PreflightChecker, RuntimeDoctor
from .service import OrchestratorService, RunNotFoundError
from .state_machine import InvalidTransitionError
from .telegram_bot import TelegramClient, run_telegram_bot_loop

PROJECT_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_DB_PATH = PROJECT_ROOT / "orchestrator" / "data" / "agentpr.db"
DEFAULT_WORKSPACE_ROOT = Path(
    os.environ.get("AGENTPR_BASE_DIR", str(PROJECT_ROOT / "workspaces"))
)
DEFAULT_INTEGRATION_ROOT = PROJECT_ROOT / "forge_integration"


def add_idempotency_arg(command_parser: argparse.ArgumentParser) -> None:
    command_parser.add_argument(
        "--idempotency-key",
        help="Optional idempotency key. Provide this for retriable command callers.",
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
        ],
        help=(
            "Optional state convergence after a successful agent run. "
            "Supported: LOCAL_VALIDATING, NEEDS_HUMAN_REVIEW."
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
        default="danger-full-access",
        help="Codex sandbox mode for this run.",
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
    tb.add_argument("--poll-timeout-sec", type=int, default=30, help="Telegram long-poll timeout.")
    tb.add_argument("--idle-sleep-sec", type=int, default=2, help="Sleep when no updates.")
    tb.add_argument("--list-limit", type=int, default=20, help="Default /list limit.")
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

        enforce_startup_doctor_gate(args)

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
            snapshot = service.get_run_snapshot(args.run_id)
            run = snapshot["run"]
            current_state = RunState(snapshot["state"])
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
            if not args.skip_preflight:
                preflight_report = run_preflight_checks(
                    service,
                    run_id=args.run_id,
                    repo_dir=repo_dir,
                    workspace_root=Path(args.workspace_root),
                    skip_network_check=args.skip_network_check,
                    network_timeout_sec=args.network_timeout_sec,
                    codex_sandbox=args.codex_sandbox,
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
            prompt = load_prompt(args)
            result = executor.run_agent_step(
                prompt=prompt,
                repo_dir=repo_dir,
                codex_sandbox=args.codex_sandbox,
                codex_full_auto=not args.no_codex_full_auto,
                codex_model=args.codex_model,
                extra_args=args.agent_arg,
            )
            service.add_step_attempt(
                args.run_id,
                step=StepName.AGENT,
                exit_code=result.exit_code,
                stdout_log=result.stdout,
                stderr_log=result.stderr,
                duration_ms=result.duration_ms,
            )
            agent_report = build_agent_runtime_report(
                run_id=args.run_id,
                engine="codex",
                result=result,
                run_state=current_state,
                codex_sandbox=args.codex_sandbox,
                codex_model=args.codex_model,
                codex_full_auto=not args.no_codex_full_auto,
                runtime_policy=runtime_policy,
                preflight_report=preflight_report,
            )
            verdict = agent_report["classification"]
            agent_report_path = write_agent_runtime_report(args.run_id, agent_report)
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
                    state_result = service.retry_run(
                        args.run_id,
                        target_state=RunState.NEEDS_HUMAN_REVIEW,
                    )
                print_json(
                    {
                        "ok": False,
                        "engine": "codex",
                        "exit_code": result.exit_code,
                        "state": state_result["state"],
                        "stderr": result.stderr.strip(),
                        "classification": verdict,
                        "agent_report": str(agent_report_path),
                    }
                )
                return result.exit_code
            if verdict["grade"] != AgentRuntimeGrade.PASS.value:
                if verdict["grade"] == AgentRuntimeGrade.HUMAN_REVIEW.value:
                    state_result = service.retry_run(
                        args.run_id,
                        target_state=RunState.NEEDS_HUMAN_REVIEW,
                    )
                else:
                    state_result = service.retry_run(
                        args.run_id,
                        target_state=RunState.FAILED_RETRYABLE,
                    )
                print_json(
                    {
                        "ok": False,
                        "engine": "codex",
                        "exit_code": result.exit_code,
                        "state": state_result["state"],
                        "classification": verdict,
                        "stdout_tail": tail(result.stdout),
                        "agent_report": str(agent_report_path),
                    }
                )
                return 1
            print_json(
                {
                    "ok": True,
                    "engine": "codex",
                    "exit_code": result.exit_code,
                    "state": converge_agent_success_state(service, args),
                    "classification": verdict,
                    "stdout_tail": tail(result.stdout),
                    "agent_report": str(agent_report_path),
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
            client = TelegramClient(token)
            allowed_chat_ids = set(args.allow_chat_id)
            if not allowed_chat_ids and not args.allow_any_chat:
                raise ValueError(
                    "Missing allowlist. Set --allow-chat-id (repeatable) or use --allow-any-chat "
                    "for local development only."
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
                    poll_timeout_sec=max(args.poll_timeout_sec, 1),
                    idle_sleep_sec=max(args.idle_sleep_sec, 1),
                    list_limit=max(args.list_limit, 1),
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
            try:
                run_github_webhook_server(
                    service=service,
                    host=args.host,
                    port=int(args.port),
                    path=args.path,
                    secret=secret,
                    require_signature=require_signature,
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
    except (RunNotFoundError, InvalidTransitionError, ValueError) as exc:
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


def write_pr_open_request(run_id: str, payload: dict[str, Any]) -> Path:
    reports_dir = PROJECT_ROOT / "orchestrator" / "data" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
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
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
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


def build_agent_runtime_report(
    *,
    run_id: str,
    engine: str,
    result: Any,
    run_state: RunState,
    codex_sandbox: str,
    codex_model: str | None,
    codex_full_auto: bool,
    runtime_policy: dict[str, Any],
    preflight_report: dict[str, Any] | None,
) -> dict[str, Any]:
    commands = extract_shell_commands(f"{result.stdout}\n{result.stderr}")
    if not commands:
        commands = [line for line in result.stderr.splitlines() if line.strip()][:20]

    safety_patterns: list[tuple[str, str]] = [
        ("sudo", r"\bsudo\b"),
        ("brew_install", r"\bbrew\s+install\b"),
        ("npm_global", r"\bnpm\b.*\s(-g|--global)\b"),
        ("pnpm_global", r"\bpnpm\b.*\s(-g|--global)\b"),
        ("yarn_global", r"\byarn\s+global\b"),
        ("uv_tool_install", r"\buv\s+tool\s+install\b"),
        ("poetry_self", r"\bpoetry\s+self\b"),
    ]
    violations: list[dict[str, str]] = []
    for command in commands:
        for tag, pattern in safety_patterns:
            if re.search(pattern, command):
                violations.append({"rule": tag, "command": command})

    test_patterns = [
        r"\bmake\s+test\b",
        r"\bmake\s+lint\b",
        r"\bpytest\b",
        r"\btox\b",
        r"\bhatch\s+run\s+test\b",
        r"\bpoetry\s+run\s+(pytest|tox)\b",
        r"\buv\s+run\s+(pytest|tox)\b",
        r"\bbun\s+test\b",
        r"\bbun\s+run\s+typecheck\b",
        r"\bnpm\s+test\b",
        r"\bpnpm\s+test\b",
        r"\byarn\s+test\b",
    ]
    test_signals = sorted(
        {command for command in commands for pattern in test_patterns if re.search(pattern, command)}
    )
    git_signals = sorted(
        {
            command
            for command in commands
            for pattern in (r"\bgit\s+commit\b", r"\bgit\s+push\b", r"\bfinish\.sh\b")
            if re.search(pattern, command)
        }
    )
    classification = classify_agent_runtime(
        run_state=run_state,
        result=result,
        preflight_report=preflight_report,
        safety_violations=violations,
        test_signals=test_signals,
    )

    return {
        "run_id": run_id,
        "created_at": datetime.now(UTC).isoformat(),
        "engine": engine,
        "result": {
            "exit_code": result.exit_code,
            "duration_ms": result.duration_ms,
        },
        "runtime": {
            "codex_sandbox": codex_sandbox,
            "codex_model": codex_model,
            "codex_full_auto": codex_full_auto,
            "policy": runtime_policy,
        },
        "preflight": preflight_report,
        "signals": {
            "commands_sample": commands[:40],
            "test_commands": test_signals,
            "git_commands": git_signals,
        },
        "safety": {
            "violations": violations,
            "violation_count": len(violations),
        },
        "classification": classification,
    }


def classify_agent_runtime(
    *,
    run_state: RunState,
    result: Any,
    preflight_report: dict[str, Any] | None,
    safety_violations: list[dict[str, str]],
    test_signals: list[str],
) -> dict[str, Any]:
    if preflight_report is not None and not preflight_report.get("ok", True):
        failures = [str(item) for item in preflight_report.get("failures", [])]
        failure_text = "\n".join(failures)
        if contains_any_pattern(failure_text, RETRYABLE_FAILURE_PATTERNS):
            return {
                "grade": AgentRuntimeGrade.RETRYABLE.value,
                "reason_code": "preflight_transient_failure",
                "next_action": "retry",
                "evidence": {"failures": failures[:8]},
            }
        return {
            "grade": AgentRuntimeGrade.HUMAN_REVIEW.value,
            "reason_code": "preflight_hard_failure",
            "next_action": "escalate",
            "evidence": {"failures": failures[:8]},
        }

    if safety_violations:
        return {
            "grade": AgentRuntimeGrade.HUMAN_REVIEW.value,
            "reason_code": "safety_violation",
            "next_action": "escalate",
            "evidence": {
                "violations": safety_violations[:8],
            },
        }

    requires_test_evidence = run_state in {
        RunState.IMPLEMENTING,
        RunState.LOCAL_VALIDATING,
        RunState.ITERATING,
    }
    if result.exit_code == 0:
        if requires_test_evidence and not test_signals:
            return {
                "grade": AgentRuntimeGrade.HUMAN_REVIEW.value,
                "reason_code": "missing_test_evidence",
                "next_action": "escalate",
                "evidence": {"expected_state": run_state.value},
            }
        return {
            "grade": AgentRuntimeGrade.PASS.value,
            "reason_code": "runtime_success",
            "next_action": "advance",
            "evidence": {
                "exit_code": result.exit_code,
                "test_commands": test_signals[:12],
            },
        }

    error_text = f"{result.stderr}\n{result.stdout}"
    if contains_any_pattern(error_text, HARD_FAILURE_PATTERNS):
        return {
            "grade": AgentRuntimeGrade.HUMAN_REVIEW.value,
            "reason_code": "runtime_hard_failure",
            "next_action": "escalate",
            "evidence": {"exit_code": result.exit_code},
        }

    if contains_any_pattern(error_text, RETRYABLE_FAILURE_PATTERNS):
        return {
            "grade": AgentRuntimeGrade.RETRYABLE.value,
            "reason_code": "runtime_transient_failure",
            "next_action": "retry",
            "evidence": {"exit_code": result.exit_code},
        }

    return {
        "grade": AgentRuntimeGrade.RETRYABLE.value,
        "reason_code": "runtime_unknown_failure",
        "next_action": "retry",
        "evidence": {"exit_code": result.exit_code},
    }


def contains_any_pattern(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


HARD_FAILURE_PATTERNS: tuple[str, ...] = (
    r"\bpermission denied\b",
    r"\boperation not permitted\b",
    r"\bread-only file system\b",
    r"\bauthentication failed\b",
    r"\bunauthorized\b",
    r"\bforbidden\b",
    r"\bnot a git repository\b",
    r"\brepository not found\b",
    r"\bcommand not found\b",
    r"\bno such file or directory\b",
    r"\bindex\.lock\b",
)


RETRYABLE_FAILURE_PATTERNS: tuple[str, ...] = (
    r"\btimed out\b",
    r"\btimeout\b",
    r"\btemporary failure\b",
    r"\btemporarily unavailable\b",
    r"\bconnection reset\b",
    r"\bconnection aborted\b",
    r"\bconnection refused\b",
    r"\bcould not resolve host\b",
    r"\bnetwork is unreachable\b",
    r"\brate limit\b",
    r"\btoo many requests\b",
    r"\bhttp 429\b",
    r"\bhttp 5\d\d\b",
    r"\bservice unavailable\b",
)


def extract_shell_commands(text: str) -> list[str]:
    commands: list[str] = []
    patterns = [
        r"/bin/zsh -lc '([^']+)'",
        r'/bin/zsh -lc "([^"]+)"',
    ]
    for pattern in patterns:
        commands.extend(re.findall(pattern, text))
    deduped: list[str] = []
    seen: set[str] = set()
    for command in commands:
        stripped = command.strip()
        if not stripped or stripped in seen:
            continue
        seen.add(stripped)
        deduped.append(stripped)
    return deduped


def write_agent_runtime_report(run_id: str, payload: dict[str, Any]) -> Path:
    reports_dir = PROJECT_ROOT / "orchestrator" / "data" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    report_path = reports_dir / f"{run_id}_agent_runtime_{stamp}.json"
    report_path.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return report_path


def converge_agent_success_state(
    service: OrchestratorService,
    args: argparse.Namespace,
) -> str:
    success_state = args.success_state
    if success_state is None:
        return service.get_run_snapshot(args.run_id)["state"]

    target = RunState(success_state)
    if target == RunState.LOCAL_VALIDATING:
        result = service.mark_local_validation_passed(args.run_id)
        return str(result["state"])
    if target == RunState.NEEDS_HUMAN_REVIEW:
        result = service.retry_run(
            args.run_id,
            target_state=RunState.NEEDS_HUMAN_REVIEW,
        )
        return str(result["state"])
    raise ValueError(f"Unsupported success-state target: {success_state}")


if __name__ == "__main__":
    sys.exit(main())
