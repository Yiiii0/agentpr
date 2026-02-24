from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path
from typing import Any

from .db import Database
from .executor import ScriptExecutor
from .models import RunCreateInput, RunMode, RunState, StepName
from .service import OrchestratorService, RunNotFoundError
from .state_machine import InvalidTransitionError

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

    ag = sub.add_parser(
        "run-agent-step",
        help="Execute non-interactive agent command (codex/claude) inside repo workspace",
    )
    ag.add_argument("--run-id", required=True)
    ag.add_argument("--engine", choices=["codex", "claude"], default="codex")
    prompt_group = ag.add_mutually_exclusive_group(required=True)
    prompt_group.add_argument("--prompt", help="Prompt text")
    prompt_group.add_argument("--prompt-file", type=Path, help="Prompt file path")
    ag.add_argument(
        "--agent-arg",
        action="append",
        default=[],
        help="Extra argument appended to the engine command. Can be repeated.",
    )

    f = sub.add_parser("run-finish", help="Execute finish.sh")
    f.add_argument("--run-id", required=True)
    f.add_argument("--changes", required=True)
    f.add_argument("--project")
    f.add_argument("--commit-title")

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


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()

    try:
        service = build_service(args)
        executor = ScriptExecutor(args.integration_root)

        if args.command == "init-db":
            print_json({"ok": True, "db": str(args.db)})
            return 0

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

        if args.command == "run-agent-step":
            snapshot = service.get_run_snapshot(args.run_id)
            run = snapshot["run"]
            current_state = RunState(snapshot["state"])
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
            prompt = load_prompt(args)
            result = executor.run_agent_step(
                engine=args.engine,
                prompt=prompt,
                repo_dir=repo_dir,
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
            if result.exit_code != 0:
                service.record_step_failure(
                    args.run_id,
                    step=StepName.AGENT,
                    reason_code=f"{args.engine}_agent_failed",
                    error_message=result.stderr.strip() or "agent command failed",
                )
                print_json(
                    {
                        "ok": False,
                        "engine": args.engine,
                        "exit_code": result.exit_code,
                        "stderr": result.stderr.strip(),
                    }
                )
                return result.exit_code
            print_json(
                {
                    "ok": True,
                    "engine": args.engine,
                    "exit_code": result.exit_code,
                    "stdout_tail": tail(result.stdout),
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


if __name__ == "__main__":
    sys.exit(main())
