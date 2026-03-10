from __future__ import annotations

import fcntl
import json
import logging
import os
import subprocess
import sys
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Iterator

from .manager_agent import ManagerAgent, ManagerAgentConfig
from .manager_decision import ManagerAction, ManagerActionKind, ManagerRunFacts
from .manager_llm import ManagerLLMClient, ManagerLLMError, RetryStrategy
from .manager_policy import RunAgentPolicy, load_manager_policy, resolve_run_agent_effective_policy
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
    decision_mode: str
    manager_api_base: str | None
    manager_model: str | None
    manager_timeout_sec: int
    manager_api_key_env: str
    skip_doctor_for_inner_commands: bool = True


logger = logging.getLogger("agentpr.manager_loop")

_CONSECUTIVE_FAIL_LIMIT = 3


class ManagerLoopRunner:
    def __init__(self, *, service: OrchestratorService, config: ManagerLoopConfig) -> None:
        self.service = service
        self.config = config
        self._llm_client: ManagerLLMClient | None = None
        self._run_agent_policy: RunAgentPolicy | None = None
        self._global_stats_cache: dict[str, Any] | None = None
        try:
            self._run_agent_policy = load_manager_policy(self.config.policy_file).run_agent_step
        except (OSError, ValueError):
            self._run_agent_policy = None
        if self.config.decision_mode in {"llm", "hybrid"}:
            try:
                self._llm_client = ManagerLLMClient.from_runtime(
                    api_base=self.config.manager_api_base,
                    model=self.config.manager_model,
                    timeout_sec=self.config.manager_timeout_sec,
                    api_key_env=self.config.manager_api_key_env,
                )
            except ManagerLLMError:
                self._llm_client = None
        self._manager_agent = ManagerAgent(
            service=self.service,
            llm_client=self._llm_client,
            config=ManagerAgentConfig(
                decision_mode=self.config.decision_mode,
                global_stats_limit=max(self.config.limit, 1),
            ),
        )

    def _get_failure_count(self, run_id: str) -> int:
        artifact = self.service.latest_artifact(run_id, artifact_type="manager_failure_count")
        if artifact is None:
            return 0
        metadata = artifact.get("metadata")
        if not isinstance(metadata, dict):
            return 0
        return int(metadata.get("count") or 0)

    def _set_failure_count(self, run_id: str, count: int) -> None:
        self.service.add_artifact(
            run_id,
            artifact_type="manager_failure_count",
            uri=f"inline://failure_count/{run_id}",
            metadata={"count": count},
        )

    def _reset_failure_count(self, run_id: str) -> None:
        self._set_failure_count(run_id, 0)

    def tick(self) -> dict[str, Any]:
        started_at = datetime.now(UTC)
        run_ids = self._resolve_run_ids()
        self._global_stats_cache = self._manager_agent.compute_global_stats(
            limit=max(self.config.limit, 1),
        )
        results: list[dict[str, Any]] = []

        for run_id in run_ids:
            result = self._process_run(run_id)
            results.append(result)

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

    @contextmanager
    def _run_lock(self, run_id: str) -> Iterator[bool]:
        """File-based per-run lock. Yields True if acquired, False if busy."""
        lock_dir = self.config.db_path.parent / ".locks"
        lock_dir.mkdir(parents=True, exist_ok=True)
        lock_path = lock_dir / f"run_{run_id}.lock"
        fd = None
        try:
            fd = open(lock_path, "w")  # noqa: SIM115
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            yield True
        except OSError:
            if fd is not None:
                fd.close()
                fd = None
            yield False
        finally:
            if fd is not None:
                try:
                    fcntl.flock(fd, fcntl.LOCK_UN)
                except OSError:
                    pass
                fd.close()

    def _process_run(self, run_id: str) -> dict[str, Any]:
        with self._run_lock(run_id) as acquired:
            if not acquired:
                logger.warning("run=%s already locked by another process, skipping", run_id)
                return {
                    "ok": True,
                    "run_id": run_id,
                    "owner": "",
                    "repo": "",
                    "state": "",
                    "actions_executed": 0,
                    "actions": [{"action": "skip", "reason": "run locked by another process"}],
                    "errors": [],
                }
            return self._process_run_inner(run_id)

    def _process_run_inner(self, run_id: str) -> dict[str, Any]:
        actions: list[dict[str, Any]] = []
        errors: list[str] = []
        attempts = 0
        seen_state_action: set[tuple[str, str]] = set()

        # Check consecutive failure count before doing anything
        fail_count = self._get_failure_count(run_id)
        if fail_count >= _CONSECUTIVE_FAIL_LIMIT:
            # Auto-escalate: transition to NEEDS_HUMAN and stop retrying
            escalation_msg = (
                f"Auto-escalated after {fail_count} consecutive failures. "
                "Manual intervention required."
            )
            try:
                self.service.pause_run(run_id)
            except Exception:  # noqa: BLE001
                pass  # Best-effort pause
            try:
                from .manager_tools import notify_user
                notify_user(
                    service=self.service,
                    run_id=run_id,
                    message=escalation_msg,
                    priority="high",
                )
            except Exception:  # noqa: BLE001
                pass
            self._reset_failure_count(run_id)
            actions.append({
                "state": "ESCALATED",
                "action": "auto_pause",
                "reason": escalation_msg,
                "result": "consecutive_failure_limit",
            })
            errors.append(escalation_msg)
            final_snapshot = self.service.get_run_snapshot(run_id)
            return {
                "ok": False,
                "run_id": run_id,
                "owner": final_snapshot["run"]["owner"],
                "repo": final_snapshot["run"]["repo"],
                "state": final_snapshot["state"],
                "actions_executed": 0,
                "actions": actions,
                "errors": errors,
            }

        while attempts < max(self.config.max_actions_per_run, 1):
            facts = self._build_run_facts(run_id)
            action, decision_source = self._decide_action(facts)
            action_record: dict[str, Any] = {
                "state": facts.state.value,
                "action": action.kind.value,
                "reason": action.reason,
                "decision_source": decision_source,
                "facts_snapshot": {
                    "state": facts.state.value,
                    "latest_worker_grade": facts.latest_worker_grade,
                    "latest_worker_confidence": facts.latest_worker_confidence,
                    "latest_failure_reason_code": facts.latest_failure_reason_code,
                    "has_prompt": facts.has_prompt,
                    "worker_autonomous": facts.worker_autonomous,
                },
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

            self._notify_after_action(
                run_id=run_id, action=action, ok=outcome["ok"]
            )

            attempts += 1
            if not outcome["ok"]:
                # Auto-recovery: dirty workspace with PASS grade → run-finish
                payload = outcome.get("payload")
                reason_code = (
                    str(payload.get("reason_code") or "")
                    if isinstance(payload, dict)
                    else ""
                )
                if (
                    action.kind == ManagerActionKind.RUN_AGENT_STEP
                    and reason_code == "dirty_workspace_pass_grade"
                ):
                    logger.info(
                        "auto_recovery: dirty_workspace_pass_grade for run=%s, "
                        "executing run-finish instead",
                        run_id,
                    )
                    recovery_action = ManagerAction(
                        kind=ManagerActionKind.RUN_FINISH,
                        reason="auto-recovery: dirty workspace with PASS grade",
                    )
                    recovery_outcome = self._execute_action(
                        run_id=run_id, facts=facts, action=recovery_action,
                    )
                    recovery_record: dict[str, Any] = {
                        "state": facts.state.value,
                        "action": recovery_action.kind.value,
                        "reason": recovery_action.reason,
                        "decision_source": "auto_recovery_dirty_workspace",
                        "command": recovery_outcome["command"],
                        "returncode": recovery_outcome["returncode"],
                        "ok": recovery_outcome["ok"],
                        "output": recovery_outcome["output"],
                    }
                    actions.append(recovery_record)
                    if recovery_outcome["ok"]:
                        self._reset_failure_count(run_id)
                        continue
                    else:
                        errors.append(
                            str(recovery_outcome.get("error") or "recovery failed")
                        )
                        break

                self._set_failure_count(run_id, fail_count + 1)
                errors.append(str(outcome.get("error") or "action failed"))
                break
            else:
                # Reset on success
                self._reset_failure_count(run_id)

        final_snapshot = self.service.get_run_snapshot(run_id)
        executed_count = sum(1 for item in actions if "command" in item)

        # Post-processing: detect skill improvement proposals from grading results
        self._check_skill_proposals(run_id, actions)

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

    def _decide_action(self, facts: ManagerRunFacts) -> tuple[ManagerAction, str]:
        digest_context = self._load_digest_context(facts.run_id)
        return self._manager_agent.decide_action(
            facts=facts,
            digest_context=digest_context,
            global_stats=self._global_stats_cache,
        )

    def _load_digest_context(self, run_id: str) -> dict[str, Any]:
        artifact = self.service.latest_artifact(run_id, artifact_type="run_digest")
        if artifact is None:
            return {"available": False, "reason": "missing_artifact"}
        path_raw = str(artifact.get("uri") or "").strip()
        if not path_raw:
            return {"available": False, "reason": "empty_artifact_uri"}
        path = Path(path_raw)
        if not path.exists():
            return {
                "available": False,
                "reason": "artifact_not_found",
                "path": str(path),
            }
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {
                "available": False,
                "reason": "artifact_unreadable",
                "path": str(path),
            }
        if not isinstance(payload, dict):
            return {
                "available": False,
                "reason": "artifact_invalid_payload",
                "path": str(path),
            }

        classification = payload.get("classification")
        classification = classification if isinstance(classification, dict) else {}
        validation = payload.get("validation")
        validation = validation if isinstance(validation, dict) else {}
        changes = payload.get("changes")
        changes = changes if isinstance(changes, dict) else {}
        attempt = payload.get("attempt")
        attempt = attempt if isinstance(attempt, dict) else {}
        recommendation = payload.get("manager_recommendation")
        recommendation = recommendation if isinstance(recommendation, dict) else {}
        skills = payload.get("skills")
        skills = skills if isinstance(skills, dict) else {}
        state = payload.get("state")
        state = state if isinstance(state, dict) else {}

        evidence_fields = [
            "classification.grade",
            "classification.reason_code",
            "validation.test_command_count",
            "validation.failed_test_command_count",
            "changes.changed_files_count",
            "changes.added_lines",
            "attempt.exit_code",
            "attempt.duration_ms",
            "skills.mode",
            "manager_recommendation.action",
        ]
        return {
            "available": True,
            "path": str(path),
            "generated_at": str(payload.get("generated_at") or ""),
            "state_after": str(state.get("after") or ""),
            "classification": {
                "grade": str(classification.get("grade") or ""),
                "reason_code": str(classification.get("reason_code") or ""),
                "next_action": str(classification.get("next_action") or ""),
            },
            "validation": {
                "test_command_count": int(validation.get("test_command_count") or 0),
                "failed_test_command_count": int(
                    validation.get("failed_test_command_count") or 0
                ),
            },
            "changes": {
                "changed_files_count": int(changes.get("changed_files_count") or 0),
                "added_lines": int(changes.get("added_lines") or 0),
                "deleted_lines": int(changes.get("deleted_lines") or 0),
            },
            "attempt": {
                "attempt_no": attempt.get("attempt_no"),
                "exit_code": int(attempt.get("exit_code") or 0),
                "duration_ms": int(attempt.get("duration_ms") or 0),
            },
            "skills": {
                "mode": str(skills.get("mode") or ""),
                "missing_required_count": len(list(skills.get("missing_required") or [])),
            },
            "manager_recommendation": {
                "action": str(recommendation.get("action") or ""),
                "priority": str(recommendation.get("priority") or ""),
                "why": str(recommendation.get("why") or ""),
            },
            "evidence_fields": evidence_fields,
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
        resolved_skills_mode = (
            str(self.config.skills_mode).strip()
            if self.config.skills_mode is not None
            else self._resolve_policy_skills_mode(
                owner=str(run["owner"]),
                repo=str(run["repo"]),
            )
        )

        grade, confidence, failure_reason_code = self._latest_worker_grade(run_id)

        # When LLM is available and we have a grade but no confidence,
        # ask the LLM for a semantic assessment.
        if (
            grade is not None
            and confidence is None
            and self._llm_client is not None
            and self.config.decision_mode in {"llm", "hybrid"}
        ):
            try:
                from .manager_tools import analyze_worker_output

                evidence = analyze_worker_output(
                    service=self.service, run_id=run_id
                )
                if evidence.get("ok"):
                    llm_grade = self._llm_client.grade_worker_output(
                        evidence=evidence
                    )
                    grade = llm_grade.verdict.strip().upper() or grade
                    confidence = llm_grade.confidence.strip().lower() or None
            except (ManagerLLMError, Exception):  # noqa: BLE001
                pass  # Keep original grade; LLM grading is best-effort

        # Review triage for ITERATING runs
        review_triage_action: str | None = None
        if state == RunState.ITERATING and self._llm_client is not None:
            review_triage_action = self._triage_iterating_review(run_id)

        # Retry strategy for FAILED runs
        retry_should_retry: bool | None = None
        retry_target_state: str | None = None
        if state == RunState.FAILED and self._llm_client is not None:
            strategy = self._diagnose_failure(run_id)
            if strategy is not None:
                retry_should_retry = strategy.should_retry
                retry_target_state = strategy.target_state or None

        # Use run updated_at as proxy for state_entered_at
        state_entered_at = str(run.get("updated_at") or "") or None

        # Check for code review artifact
        code_review_art = self.service.latest_artifact(run_id, artifact_type="code_review")
        has_code_review = code_review_art is not None
        code_review_verdict: str | None = None
        if has_code_review and isinstance(code_review_art, dict):
            meta = code_review_art.get("metadata")
            if isinstance(meta, dict):
                code_review_verdict = str(meta.get("verdict") or "").strip().upper() or None

        # Auto PR mode: controlled by env var AGENTPR_AUTO_PR (default: false)
        auto_pr_enabled = os.environ.get("AGENTPR_AUTO_PR", "").strip().lower() in {
            "1", "true", "yes",
        }

        # Check for PR bump artifacts
        pr_bump_count = 0
        last_pr_bump_at: str | None = None
        bump_arts = self.service.list_artifacts(run_id, artifact_type="pr_bump_comment", limit=10)
        if bump_arts:
            pr_bump_count = len(bump_arts)
            last_art_meta = bump_arts[-1].get("metadata")
            if isinstance(last_art_meta, dict):
                last_pr_bump_at = last_art_meta.get("bumped_at")

        # Check for triage confirmation
        review_triage_confirmed = False
        if review_triage_action == "fix_code":
            triage_confirm_art = self.service.latest_artifact(
                run_id, artifact_type="review_triage_confirmation"
            )
            review_triage_confirmed = triage_confirm_art is not None

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
            worker_autonomous=(resolved_skills_mode == "agentpr_autonomous"),
            latest_worker_grade=grade,
            latest_worker_confidence=confidence,
            latest_failure_reason_code=failure_reason_code,
            review_triage_action=review_triage_action,
            retry_should_retry=retry_should_retry,
            retry_target_state=retry_target_state,
            state_entered_at=state_entered_at,
            has_code_review=has_code_review,
            code_review_verdict=code_review_verdict,
            auto_pr_enabled=auto_pr_enabled,
            review_triage_confirmed=review_triage_confirmed,
            pr_bump_count=pr_bump_count,
            last_pr_bump_at=last_pr_bump_at,
        )

    def _latest_worker_grade(
        self, run_id: str
    ) -> tuple[str | None, str | None, str | None]:
        """Return (grade, confidence, reason_code) from the latest worker digest."""
        artifact = self.service.latest_artifact(run_id, artifact_type="run_digest")
        if artifact is None:
            artifact = self.service.latest_artifact(run_id, artifact_type="agent_runtime_report")
        if artifact is None:
            return None, None, None
        raw_path = str(artifact.get("uri") or "").strip()
        if not raw_path:
            return None, None, None
        path = Path(raw_path)
        if not path.exists():
            return None, None, None
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return None, None, None
        if not isinstance(payload, dict):
            return None, None, None
        classification = payload.get("classification")
        classification = classification if isinstance(classification, dict) else {}
        grade = str(classification.get("grade") or "").strip().upper() or None
        reason_code = str(classification.get("reason_code") or "").strip() or None
        # Confidence from semantic grading (hybrid_llm mode) or classification
        confidence: str | None = None
        semantic = classification.get("semantic")
        if isinstance(semantic, dict):
            confidence = str(semantic.get("confidence") or "").strip().lower() or None
        if confidence is None:
            confidence = str(classification.get("confidence") or "").strip().lower() or None
        return grade, confidence, reason_code

    def _triage_iterating_review(self, run_id: str) -> str | None:
        """Best-effort LLM triage of the latest review comment. Returns action or None."""
        if self._llm_client is None:
            return None
        try:
            events = self.service.list_events(run_id, limit=1)
            if not events:
                return None
            latest = events[0]
            event_type = str(latest.get("event_type") or "")
            if event_type != "github.review.submitted":
                return None  # Only triage review comments, not CI checks
            event_payload = latest.get("payload")
            event_payload = event_payload if isinstance(event_payload, dict) else {}
            comment_body = str(event_payload.get("body") or "").strip()
            if not comment_body:
                return "fix_code"  # No body = changes_requested without detail, default to fix
            snapshot = self.service.get_run_snapshot(run_id)
            run = snapshot["run"]
            triage = self._llm_client.triage_review_comment(
                comment_body=comment_body,
                run_context={
                    "run_id": run_id,
                    "owner": str(run.get("owner") or ""),
                    "repo": str(run.get("repo") or ""),
                    "state": str(snapshot.get("state") or ""),
                },
            )
            # Store triage result as artifact for audit + confirmation flow
            self.service.add_artifact(
                run_id,
                artifact_type="review_triage",
                uri=f"inline://review_triage/{run_id}",
                metadata={
                    "action": triage.action,
                    "reason": getattr(triage, "reason", ""),
                    "confidence": getattr(triage, "confidence", ""),
                    "comment_body": comment_body[:500],
                    "created_at": datetime.now(UTC).isoformat(),
                },
            )
            # Notify human if triage says fix_code (needs confirmation)
            if triage.action == "fix_code":
                from .manager_tools import notify_user
                notify_user(
                    service=self.service,
                    run_id=run_id,
                    message=(
                        f"Review triage: {triage.action} — "
                        f"{getattr(triage, 'reason', 'reviewer requests code change')}. "
                        f"Approve with /approve_triage {run_id}"
                    ),
                    priority="high",
                )
            return triage.action
        except (ManagerLLMError, Exception):  # noqa: BLE001
            return None  # Triage is best-effort

    def _diagnose_failure(self, run_id: str) -> RetryStrategy | None:
        """Best-effort LLM failure diagnosis. Returns strategy or None."""
        if self._llm_client is None:
            return None
        try:
            from .manager_tools import analyze_worker_output

            evidence = analyze_worker_output(service=self.service, run_id=run_id)
            if not evidence.get("ok"):
                return None
            failure_count = self._get_failure_count(run_id)
            evidence["previous_attempts"] = failure_count
            return self._llm_client.suggest_retry_strategy(failure_evidence=evidence)
        except (ManagerLLMError, Exception):  # noqa: BLE001
            return None  # Diagnosis is best-effort

    def _resolve_policy_skills_mode(self, *, owner: str, repo: str) -> str:
        if self._run_agent_policy is None:
            return "off"
        effective = resolve_run_agent_effective_policy(
            self._run_agent_policy,
            owner=owner,
            repo=repo,
        )
        return str(effective.get("skills_mode") or "off")

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
            # Auto-prepare: if workspace doesn't exist, run-prepare first
            workspace_dir = self.config.workspace_root / facts.repo
            if not workspace_dir.exists():
                prep = self._run_cli(["run-prepare", "--run-id", run_id])
                if not prep["ok"]:
                    return prep
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
            # run-finish needs workspace (git commit/push)
            workspace_dir = self.config.workspace_root / facts.repo
            if not workspace_dir.exists():
                return {
                    "ok": False,
                    "command": "run-finish",
                    "returncode": 1,
                    "output": "",
                    "error": f"Workspace not found: {workspace_dir}. Run run-prepare first.",
                }
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
            target_state = str(
                action.metadata.get("target_state") or RunState.EXECUTING.value
            ).strip().upper()
            # Validate target_state before executing
            valid_targets = {"QUEUED", "EXECUTING", "ITERATING", "DISCOVERY", "IMPLEMENTING"}
            if target_state not in valid_targets:
                return {
                    "ok": False,
                    "command": "retry",
                    "returncode": 1,
                    "output": "",
                    "error": (
                        f"Invalid retry target_state '{target_state}'. "
                        f"Valid values: {sorted(valid_targets)}. "
                        f"Defaulting is handled by rules; this indicates a bug in action metadata."
                    ),
                }
            return self._run_cli(
                [
                    "retry",
                    "--run-id",
                    run_id,
                    "--target-state",
                    target_state,
                ]
            )

        if action.kind == ManagerActionKind.RUN_CODE_REVIEW:
            return self._run_cli(["run-code-review", "--run-id", run_id])

        if action.kind == ManagerActionKind.AUTO_CREATE_PR:
            return self._auto_create_pr(run_id=run_id, facts=facts)

        if action.kind == ManagerActionKind.BUMP_PR_COMMENT:
            return self._bump_pr_comment(run_id=run_id, facts=facts)

        if action.kind == ManagerActionKind.SYNC_GITHUB:
            return self._run_cli(["sync-github", "--run-id", run_id])

        return {
            "ok": False,
            "command": action.kind.value,
            "returncode": 2,
            "output": "",
            "error": f"unsupported manager action: {action.kind.value}",
        }

    def _bump_pr_comment(
        self, *, run_id: str, facts: ManagerRunFacts
    ) -> dict[str, Any]:
        """Post a polite follow-up comment on a PR with no maintainer response."""
        if facts.pr_number is None:
            return {"ok": False, "error": "no pr_number for bump"}

        bump_msg = os.environ.get(
            "AGENTPR_PR_BUMP_MESSAGE",
            "Hi! Just checking in on this PR. Let me know if there's any "
            "feedback or changes you'd like to see. Happy to iterate!",
        )

        workspace_dir = Path(
            self.service.get_run(run_id).get("workspace_dir", "")
        )
        if not workspace_dir.is_dir():
            return {"ok": False, "error": f"workspace not found: {workspace_dir}"}

        result = self.executor.run_gh_pr_comment(
            repo_dir=workspace_dir,
            pr_number=facts.pr_number,
            body=bump_msg,
        )

        if result.returncode == 0:
            self.service.add_artifact(
                run_id,
                artifact_type="pr_bump_comment",
                uri=f"inline://pr_bump/{run_id}/{facts.pr_bump_count + 1}",
                metadata={
                    "pr_number": facts.pr_number,
                    "bumped_at": datetime.now(UTC).isoformat(),
                    "bump_count": facts.pr_bump_count + 1,
                    "message": bump_msg,
                },
            )

        return {
            "ok": result.returncode == 0,
            "command": "bump_pr_comment",
            "returncode": result.returncode,
            "output": result.stdout[:500] if result.stdout else "",
            "error": result.stderr[:500] if result.stderr else "",
        }

    def _auto_create_pr(
        self, *, run_id: str, facts: ManagerRunFacts
    ) -> dict[str, Any]:
        """Auto-create PR: request → assess readiness → approve.

        Steps:
        1. request-open-pr (generates PR body via LLM, title auto-generated)
        2. Manager LLM assesses PR readiness (code + body + principles)
        3. If APPROVE → approve-open-pr
        4. If NEEDS_HUMAN → store result, return not-ok
        """
        # Step 1: Request PR creation
        request_result = self._run_cli(["request-open-pr", "--run-id", run_id])
        if not request_result["ok"]:
            return request_result
        payload = request_result.get("payload") or {}
        confirm_token = str(payload.get("confirm_token") or "").strip()
        request_file = str(payload.get("request_file") or "").strip()
        if not confirm_token or not request_file:
            return {
                "ok": False,
                "command": "auto-create-pr",
                "returncode": 1,
                "output": "",
                "error": "request-open-pr did not return confirm_token or request_file",
            }

        # Step 2: PR readiness assessment (if LLM available)
        if self._llm_client is not None:
            try:
                readiness = self._run_pr_readiness_assessment(
                    run_id=run_id,
                    facts=facts,
                    request_file=request_file,
                )
                # Store result as artifact
                self.service.add_artifact(
                    run_id,
                    artifact_type="pr_readiness",
                    uri=f"inline://pr_readiness/{run_id}",
                    metadata={
                        "verdict": readiness.verdict,
                        "code_ok": readiness.code_ok,
                        "body_ok": readiness.body_ok,
                        "reasons": readiness.reasons,
                        "summary": readiness.summary,
                    },
                )
                if readiness.verdict != "APPROVE":
                    # Notify human and don't approve
                    try:
                        from .manager_tools import notify_user
                        notify_user(
                            service=self.service,
                            run_id=run_id,
                            message=(
                                f"PR readiness: NEEDS_HUMAN. {readiness.summary} "
                                f"Reasons: {'; '.join(readiness.reasons[:3])}"
                            ),
                            priority="high",
                        )
                    except Exception:  # noqa: BLE001
                        pass
                    return {
                        "ok": False,
                        "command": "auto-create-pr (readiness check)",
                        "returncode": 1,
                        "output": json.dumps({
                            "verdict": readiness.verdict,
                            "summary": readiness.summary,
                            "reasons": readiness.reasons,
                        }),
                        "error": f"PR readiness: {readiness.verdict} — {readiness.summary}",
                    }
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "PR readiness assessment failed for run=%s: %s. Proceeding with approval.",
                    run_id,
                    exc,
                )

        # Step 3: Approve PR
        approve_result = self._run_cli([
            "approve-open-pr",
            "--run-id", run_id,
            "--request-file", request_file,
            "--confirm-token", confirm_token,
            "--confirm",
            "--allow-dod-bypass",
        ])

        # Notify about auto-creation
        if approve_result["ok"]:
            try:
                from .manager_tools import notify_user
                approve_payload = approve_result.get("payload") or {}
                pr_url = str(approve_payload.get("pr_url") or "")
                notify_user(
                    service=self.service,
                    run_id=run_id,
                    message=f"PR auto-created: {pr_url}" if pr_url else "PR auto-created",
                    priority="normal",
                )
            except Exception:  # noqa: BLE001
                pass

        return approve_result

    def _run_pr_readiness_assessment(
        self,
        *,
        run_id: str,
        facts: ManagerRunFacts,
        request_file: str,
    ) -> "PRReadinessResult":
        """Run the Manager LLM PR readiness assessment."""
        from .manager_llm import PRReadinessResult  # noqa: F811

        # Load PR body from request file
        pr_body = ""
        try:
            request_data = json.loads(Path(request_file).read_text(encoding="utf-8"))
            pr_body = str(request_data.get("body") or "")
        except (OSError, json.JSONDecodeError):
            pass

        # Load code review artifact
        code_review_art = self.service.latest_artifact(run_id, artifact_type="code_review")
        cr_summary = ""
        cr_verdict = "CLEAN"
        cr_issues: list[dict[str, str]] = []
        if code_review_art and isinstance(code_review_art, dict):
            meta = code_review_art.get("metadata")
            if isinstance(meta, dict):
                cr_verdict = str(meta.get("verdict") or "CLEAN")
                cr_summary = str(meta.get("summary") or "")
                raw_issues = meta.get("issues")
                if isinstance(raw_issues, list):
                    cr_issues = [i for i in raw_issues if isinstance(i, dict)]

        # Load worker evidence
        evidence: dict[str, Any] = {}
        try:
            from .manager_tools import analyze_worker_output
            evidence = analyze_worker_output(service=self.service, run_id=run_id)
        except Exception:  # noqa: BLE001
            pass

        # Load repo PR template
        pr_template = ""
        workspace_dir = self.config.workspace_root / facts.repo
        template_paths = [
            workspace_dir / ".github" / "PULL_REQUEST_TEMPLATE.md",
            workspace_dir / ".github" / "pull_request_template.md",
            workspace_dir / "PULL_REQUEST_TEMPLATE.md",
        ]
        for tp in template_paths:
            if tp.exists():
                try:
                    pr_template = tp.read_text(encoding="utf-8")[:3000]
                except OSError:
                    pass
                break

        # Load diff
        diff_text = ""
        if workspace_dir.exists():
            try:
                import subprocess as _sp
                result = _sp.run(
                    ["git", "diff", "HEAD~1..HEAD"],
                    cwd=workspace_dir,
                    capture_output=True,
                    text=True,
                    check=False,
                )
                diff_text = result.stdout[:6000]
            except Exception:  # noqa: BLE001
                pass

        assert self._llm_client is not None
        return self._llm_client.assess_pr_readiness(
            project_name=facts.repo,
            code_review_summary=cr_summary,
            code_review_verdict=cr_verdict,
            code_review_issues=cr_issues,
            worker_evidence=evidence,
            pr_body=pr_body,
            pr_template=pr_template,
            diff_text=diff_text,
        )

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

    def _notify_after_action(
        self,
        *,
        run_id: str,
        action: ManagerAction,
        ok: bool,
    ) -> None:
        """Best-effort notification after significant manager actions."""
        _NOTIFY_KINDS = {
            ManagerActionKind.RUN_FINISH,
            ManagerActionKind.RETRY,
            ManagerActionKind.AUTO_CREATE_PR,
        }
        if action.kind not in _NOTIFY_KINDS and ok:
            return
        try:
            from .manager_tools import notify_user

            if not ok:
                notify_user(
                    service=self.service,
                    run_id=run_id,
                    message=f"action {action.kind.value} failed: {action.reason}",
                    priority="high",
                )
            elif action.kind == ManagerActionKind.RUN_FINISH:
                notify_user(
                    service=self.service,
                    run_id=run_id,
                    message="run finish/push completed",
                    priority="normal",
                )
            elif action.kind == ManagerActionKind.RETRY:
                target = action.metadata.get("target_state", "")
                notify_user(
                    service=self.service,
                    run_id=run_id,
                    message=f"retrying run → {target}",
                    priority="normal",
                )
        except Exception:  # noqa: BLE001
            pass  # Notification is best-effort; never block the loop

    def _check_skill_proposals(
        self, run_id: str, actions: list[dict[str, Any]]
    ) -> None:
        """Detect and store skill improvement proposals based on run grading results.

        This is the self-improvement loop: each run's failures are analyzed for
        patterns that should be encoded back into skill instructions.
        """
        try:
            from .manager_tools import (
                apply_skill_improvement,
                detect_skill_improvement_proposals,
            )

            # Extract grading info from the latest action that has classification data
            reason_code = ""
            grade = ""
            evidence: dict[str, Any] = {}
            for action in reversed(actions):
                output_raw = action.get("output", "")
                if not output_raw:
                    continue
                try:
                    output = json.loads(output_raw) if isinstance(output_raw, str) else output_raw
                except (json.JSONDecodeError, TypeError):
                    continue
                if not isinstance(output, dict):
                    continue
                classification = output.get("classification")
                if isinstance(classification, dict):
                    reason_code = str(classification.get("reason_code") or "")
                    grade = str(classification.get("grade") or "")
                    evidence = classification.get("evidence") or {}
                    break

            if not reason_code:
                return

            proposals = detect_skill_improvement_proposals(
                run_id=run_id,
                reason_code=reason_code,
                grade=grade,
                evidence=evidence,
            )
            # Resolve skills source root and project root for auto-apply
            skills_source_root = Path(__file__).resolve().parent.parent / "skills"
            project_root = Path(__file__).resolve().parent.parent

            for proposal in proposals:
                result = apply_skill_improvement(
                    service=self.service,
                    run_id=proposal["run_id"],
                    proposal=proposal,
                    skills_source_root=skills_source_root,
                    project_root=project_root,
                )
                logger.info(
                    "skill_improvement: run=%s key=%s action=%s tier=%s",
                    run_id,
                    proposal["proposal_key"],
                    result.get("action", "?"),
                    result.get("tier", "?"),
                )
        except Exception:  # noqa: BLE001
            pass  # Proposals are best-effort; never block the loop

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
            output = json.dumps(
                self._compact_payload_for_output(payload),
                ensure_ascii=True,
                sort_keys=True,
            )

        ok = completed.returncode == 0
        err = ""
        if not ok:
            if payload is not None and isinstance(payload.get("error"), str):
                err = str(payload["error"]).strip()
            if not err:
                err = output
            # Enrich error with actionable context (ACI explicit feedback)
            if payload is not None:
                if payload.get("duplicate"):
                    err = (
                        f"Duplicate action blocked by idempotency. "
                        f"Current state: {payload.get('state', 'unknown')}. "
                        f"No action needed — the previous request is already in effect."
                    )
                elif payload.get("invalid_transition"):
                    err = (
                        f"State transition not allowed: {err}. "
                        f"Current state: {payload.get('state', 'unknown')}. "
                        f"Check allowed transitions for this state."
                    )

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

    @staticmethod
    def _compact_payload_for_output(
        value: Any,
        *,
        list_limit: int = 5,
        str_limit: int = 500,
        dict_key_limit: int = 20,
    ) -> Any:
        if isinstance(value, dict):
            keys = list(value.keys())
            out: dict[str, Any] = {}
            for key in keys[:dict_key_limit]:
                out[str(key)] = ManagerLoopRunner._compact_payload_for_output(
                    value[key],
                    list_limit=list_limit,
                    str_limit=str_limit,
                    dict_key_limit=dict_key_limit,
                )
            if len(keys) > dict_key_limit:
                out["..."] = f"({len(keys) - dict_key_limit} more keys)"
            return out
        if isinstance(value, list):
            items = [
                ManagerLoopRunner._compact_payload_for_output(
                    item,
                    list_limit=list_limit,
                    str_limit=str_limit,
                    dict_key_limit=dict_key_limit,
                )
                for item in value[:list_limit]
            ]
            if len(value) > list_limit:
                items.append(f"...({len(value) - list_limit} more)")
            return items
        if isinstance(value, str) and len(value) > str_limit:
            return f"{value[:str_limit]}...(truncated {len(value) - str_limit} chars)"
        return value
