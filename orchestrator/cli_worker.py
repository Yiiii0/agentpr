"""Worker execution helpers: preflight, diff analysis, state convergence, GitHub sync."""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .cli_helpers import is_ignored_runtime_path, normalize_repo_relpath
from .executor import ScriptExecutor
from .github_sync import build_sync_decision
from .models import RunState, StepName
from .preflight import PreflightChecker
from .service import OrchestratorService
from .state_machine import InvalidTransitionError

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Curated CI skills installation
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# GitHub sync
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# Diff analysis
# ---------------------------------------------------------------------------


def collect_repo_diff_summary(*, repo_dir: Path) -> dict[str, Any]:
    diff_names = run_git_text(repo_dir, ["diff", "--name-only", "HEAD"])
    diff_numstat = run_git_text(repo_dir, ["diff", "--numstat", "HEAD"])
    untracked = run_git_text(repo_dir, ["ls-files", "--others", "--exclude-standard"])

    changed_files: set[str] = set()
    ignored_files: set[str] = set()
    for line in diff_names.splitlines():
        stripped = line.strip()
        if stripped:
            normalized = normalize_repo_relpath(stripped)
            if is_ignored_runtime_path(normalized):
                ignored_files.add(normalized)
                continue
            changed_files.add(normalized)

    untracked_files: list[str] = []
    for line in untracked.splitlines():
        stripped = line.strip()
        if stripped:
            normalized = normalize_repo_relpath(stripped)
            if is_ignored_runtime_path(normalized):
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
        if is_ignored_runtime_path(path_raw):
            ignored_files.add(normalize_repo_relpath(path_raw))
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


# ---------------------------------------------------------------------------
# Worker contract artifact
# ---------------------------------------------------------------------------


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


# ---------------------------------------------------------------------------
# State convergence
# ---------------------------------------------------------------------------


def converge_agent_success_state(
    service: OrchestratorService,
    args: argparse.Namespace,
    *,
    success_state: str | None = None,
) -> str:
    snapshot = service.get_run_snapshot(args.run_id)
    current_state = RunState(snapshot["state"])
    resolved_success_state = success_state if success_state is not None else args.success_state
    if resolved_success_state is None:
        return current_state.value
    if str(resolved_success_state).strip().upper() == "UNCHANGED":
        return current_state.value

    target = RunState(str(resolved_success_state).strip().upper())
    # Normalize legacy target to V2.
    if target == RunState.LOCAL_VALIDATING:
        target = RunState.EXECUTING

    if target == RunState.EXECUTING:
        if current_state == RunState.EXECUTING:
            return current_state.value
        result = service.retry_run(
            args.run_id,
            target_state=RunState.EXECUTING,
        )
        return str(result["state"])

    if target == RunState.LOCAL_VALIDATING:
        if current_state == RunState.LOCAL_VALIDATING:
            return current_state.value
        if current_state == RunState.DISCOVERY:
            contract_artifact = service.latest_artifact(
                args.run_id,
                artifact_type="contract",
            )
            contract_uri = (
                str(contract_artifact.get("uri"))
                if isinstance(contract_artifact, dict) and contract_artifact.get("uri")
                else f"inline://auto_contract/{args.run_id}"
            )
            service.mark_plan_ready(
                args.run_id,
                contract_path=contract_uri,
            )
            current_state = RunState(service.get_run_snapshot(args.run_id)["state"])
        if current_state == RunState.PLAN_READY:
            service.start_implementation(args.run_id)
            current_state = RunState(service.get_run_snapshot(args.run_id)["state"])
        if current_state in {RunState.IMPLEMENTING, RunState.ITERATING}:
            result = service.mark_local_validation_passed(args.run_id)
            return str(result["state"])
        if current_state == RunState.PAUSED:
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
    # Normalize legacy targets to V2.
    if normalized == RunState.FAILED_RETRYABLE.value:
        normalized = RunState.FAILED.value
    if normalized == RunState.IMPLEMENTING.value:
        normalized = RunState.EXECUTING.value
    target = RunState(normalized)
    return service.retry_run(
        run_id,
        target_state=target,
    )


# ---------------------------------------------------------------------------
# Report writers
# ---------------------------------------------------------------------------


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
