from __future__ import annotations

import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .manager_decision import ManagerAction, ManagerActionKind, ManagerRunFacts, decide_next_action
from .models import RunState, StepName
from .service import OrchestratorService


@dataclass(frozen=True)
class ManagerLoopConfig:
    project_root: Path
    db_path: Path
    workspace_root: Path
    integration_root: Path
    policy_file: Path
    run_id: str | None
    limit: int
    max_actions_per_run: int
    prompt_file: Path | None
    contract_template_file: Path | None
    auto_contract: bool
    default_changes: str
    default_commit_title: str | None
    codex_sandbox: str | None
    skills_mode: str | None
    agent_args: tuple[str, ...]
    dry_run: bool
    skip_doctor_for_inner_commands: bool = True


class ManagerLoopRunner:
    def __init__(self, *, service: OrchestratorService, config: ManagerLoopConfig) -> None:
        self.service = service
        self.config = config

    def tick(self) -> dict[str, Any]:
        started_at = datetime.now(UTC)
        run_ids = self._resolve_run_ids()
        results: list[dict[str, Any]] = []

        for run_id in run_ids:
            results.append(self._process_run(run_id))

        failed = sum(1 for item in results if not bool(item.get("ok", False)))
        progressed = sum(1 for item in results if int(item.get("actions_executed", 0)) > 0)
        waiting = sum(1 for item in results if int(item.get("actions_executed", 0)) == 0)

        return {
            "ok": failed == 0,
            "tick_started_at": started_at.isoformat(),
            "tick_finished_at": datetime.now(UTC).isoformat(),
            "run_count": len(results),
            "progressed_count": progressed,
            "waiting_count": waiting,
            "failed_count": failed,
            "results": results,
        }

    def _resolve_run_ids(self) -> list[str]:
        if self.config.run_id:
            return [self.config.run_id]
        rows = self.service.list_runs(limit=max(self.config.limit, 1))
        out: list[str] = []
        for row in rows:
            run_id = str(row.get("run_id") or "").strip()
            if not run_id:
                continue
            out.append(run_id)
        return out

    def _process_run(self, run_id: str) -> dict[str, Any]:
        actions: list[dict[str, Any]] = []
        errors: list[str] = []
        attempts = 0
        seen_state_action: set[tuple[str, str]] = set()

        while attempts < max(self.config.max_actions_per_run, 1):
            facts = self._build_run_facts(run_id)
            action = decide_next_action(facts)
            action_record: dict[str, Any] = {
                "state": facts.state.value,
                "action": action.kind.value,
                "reason": action.reason,
            }

            signature = (facts.state.value, action.kind.value)
            if signature in seen_state_action:
                action_record["result"] = "loop_guard_break"
                actions.append(action_record)
                break
            seen_state_action.add(signature)

            if action.kind in {ManagerActionKind.NOOP, ManagerActionKind.WAIT_HUMAN}:
                action_record["result"] = "no_execution"
                actions.append(action_record)
                break

            if self.config.dry_run:
                action_record["result"] = "planned"
                actions.append(action_record)
                break

            outcome = self._execute_action(run_id=run_id, facts=facts, action=action)
            action_record["command"] = outcome["command"]
            action_record["returncode"] = outcome["returncode"]
            action_record["ok"] = outcome["ok"]
            action_record["output"] = outcome["output"]
            actions.append(action_record)

            attempts += 1
            if not outcome["ok"]:
                errors.append(str(outcome.get("error") or "action failed"))
                break

        final_snapshot = self.service.get_run_snapshot(run_id)
        executed_count = sum(1 for item in actions if "command" in item)
        return {
            "ok": not errors,
            "run_id": run_id,
            "owner": final_snapshot["run"]["owner"],
            "repo": final_snapshot["run"]["repo"],
            "state": final_snapshot["state"],
            "actions_executed": executed_count,
            "actions": actions,
            "errors": errors,
        }

    def _build_run_facts(self, run_id: str) -> ManagerRunFacts:
        snapshot = self.service.get_run_snapshot(run_id)
        run = snapshot["run"]
        state = RunState(snapshot["state"])
        prepare_attempts = self.service.count_step_attempts(run_id, step=StepName.PREPARE)
        contract = self.service.latest_artifact(run_id, artifact_type="contract")
        contract_uri = str(contract.get("uri")) if contract and contract.get("uri") else None
        prompt_ok = self.config.prompt_file is not None and self.config.prompt_file.exists()
        pr_number = run.get("pr_number")
        parsed_pr_number = int(pr_number) if isinstance(pr_number, int) else None

        return ManagerRunFacts(
            run_id=run_id,
            owner=str(run["owner"]),
            repo=str(run["repo"]),
            state=state,
            prepare_attempts=prepare_attempts,
            has_contract=contract_uri is not None,
            contract_uri=contract_uri,
            has_prompt=prompt_ok,
            pr_number=parsed_pr_number,
        )

    def _execute_action(
        self,
        *,
        run_id: str,
        facts: ManagerRunFacts,
        action: ManagerAction,
    ) -> dict[str, Any]:
        if action.kind == ManagerActionKind.START_DISCOVERY:
            return self._run_cli(["start-discovery", "--run-id", run_id])

        if action.kind == ManagerActionKind.RUN_PREPARE:
            return self._run_cli(["run-prepare", "--run-id", run_id])

        if action.kind == ManagerActionKind.MARK_PLAN_READY:
            contract_uri = str(action.metadata.get("contract_uri") or "").strip()
            if not contract_uri:
                contract_uri = self._materialize_auto_contract(facts)
            if not contract_uri:
                return {
                    "ok": False,
                    "command": "mark-plan-ready",
                    "returncode": 1,
                    "output": "",
                    "error": "missing contract and auto-contract is disabled",
                }
            return self._run_cli(
                [
                    "mark-plan-ready",
                    "--run-id",
                    run_id,
                    "--contract-path",
                    contract_uri,
                ]
            )

        if action.kind == ManagerActionKind.START_IMPLEMENTATION:
            return self._run_cli(["start-implementation", "--run-id", run_id])

        if action.kind == ManagerActionKind.RUN_AGENT_STEP:
            if self.config.prompt_file is None:
                return {
                    "ok": False,
                    "command": "run-agent-step",
                    "returncode": 1,
                    "output": "",
                    "error": "prompt_file is required for run-agent-step",
                }
            argv = [
                "run-agent-step",
                "--run-id",
                run_id,
                "--prompt-file",
                str(self.config.prompt_file),
            ]
            if self.config.skills_mode:
                argv.extend(["--skills-mode", self.config.skills_mode])
            if self.config.codex_sandbox:
                argv.extend(["--codex-sandbox", self.config.codex_sandbox])
            for arg in self.config.agent_args:
                argv.extend(["--agent-arg", arg])
            return self._run_cli(argv)

        if action.kind == ManagerActionKind.RUN_FINISH:
            argv = [
                "run-finish",
                "--run-id",
                run_id,
                "--changes",
                self.config.default_changes,
            ]
            if self.config.default_commit_title:
                argv.extend(["--commit-title", self.config.default_commit_title])
            return self._run_cli(argv)

        if action.kind == ManagerActionKind.RETRY:
            target_state = str(action.metadata.get("target_state") or RunState.IMPLEMENTING.value)
            return self._run_cli(
                [
                    "retry",
                    "--run-id",
                    run_id,
                    "--target-state",
                    target_state,
                ]
            )

        if action.kind == ManagerActionKind.SYNC_GITHUB:
            return self._run_cli(["sync-github", "--run-id", run_id])

        return {
            "ok": False,
            "command": action.kind.value,
            "returncode": 2,
            "output": "",
            "error": f"unsupported manager action: {action.kind.value}",
        }

    def _materialize_auto_contract(self, facts: ManagerRunFacts) -> str:
        if not self.config.auto_contract:
            return ""
        contracts_dir = self.config.project_root / "orchestrator" / "data" / "contracts"
        contracts_dir.mkdir(parents=True, exist_ok=True)
        out_path = contracts_dir / f"{facts.run_id}_auto_contract.md"
        if out_path.exists():
            return str(out_path)

        if self.config.contract_template_file is not None:
            try:
                template = self.config.contract_template_file.read_text(encoding="utf-8")
            except OSError:
                template = ""
            if template.strip():
                out_path.write_text(template, encoding="utf-8")
                return str(out_path)

        content = (
            f"# Auto Contract ({facts.owner}/{facts.repo})\n\n"
            f"run_id: {facts.run_id}\n"
            "status: bootstrap\n\n"
            "## Required Rules\n"
            "1. Read AGENTS/CONTRIBUTING/PR template and CI workflow before edits.\n"
            "2. Keep minimal diff and avoid unrelated file changes.\n"
            "3. Follow repository toolchain and run required tests/lint commands.\n"
            "4. Update docs when integration behavior changes.\n"
            "5. If any mandatory evidence is missing, stop and return NEEDS REVIEW.\n"
        )
        out_path.write_text(content, encoding="utf-8")
        return str(out_path)

    def _run_cli(self, argv: list[str]) -> dict[str, Any]:
        cmd = [
            sys.executable,
            "-m",
            "orchestrator.cli",
            "--db",
            str(self.config.db_path),
            "--workspace-root",
            str(self.config.workspace_root),
            "--integration-root",
            str(self.config.integration_root),
            "--policy-file",
            str(self.config.policy_file),
        ]
        if self.config.skip_doctor_for_inner_commands:
            cmd.append("--skip-doctor")
        cmd.extend(argv)

        completed = subprocess.run(  # noqa: S603
            cmd,
            cwd=self.config.project_root,
            text=True,
            capture_output=True,
            check=False,
        )
        output = completed.stdout.strip() or completed.stderr.strip() or "(no output)"
        payload = self._try_parse_json(output)

        if payload is not None:
            output = json.dumps(payload, ensure_ascii=True, sort_keys=True)

        ok = completed.returncode == 0
        err = ""
        if not ok:
            if payload is not None and isinstance(payload.get("error"), str):
                err = str(payload["error"]).strip()
            if not err:
                err = output

        return {
            "ok": ok,
            "command": " ".join(argv),
            "returncode": int(completed.returncode),
            "payload": payload,
            "output": output,
            "error": err,
        }

    @staticmethod
    def _try_parse_json(text: str) -> dict[str, Any] | None:
        try:
            parsed = json.loads(text)
        except json.JSONDecodeError:
            return None
        return parsed if isinstance(parsed, dict) else None
