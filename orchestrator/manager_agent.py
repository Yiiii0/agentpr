from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

from .manager_decision import (
    ManagerAction,
    ManagerActionKind,
    ManagerRunFacts,
    allowed_action_kinds,
    decide_next_action,
)
from .manager_llm import ManagerLLMClient, ManagerLLMError
from .manager_tools import analyze_worker_output, get_global_stats
from .service import OrchestratorService

logger = logging.getLogger("agentpr.manager_agent")


@dataclass(frozen=True)
class ManagerAgentConfig:
    decision_mode: str
    global_stats_limit: int = 200

    def normalized_mode(self) -> str:
        mode = str(self.decision_mode).strip().lower()
        if mode in {"rules", "llm", "hybrid"}:
            return mode
        return "rules"


class ManagerAgent:
    def __init__(
        self,
        *,
        service: OrchestratorService,
        llm_client: ManagerLLMClient | None,
        config: ManagerAgentConfig,
    ) -> None:
        self.service = service
        self.llm_client = llm_client
        self.config = config

    def compute_global_stats(self, *, limit: int | None = None) -> dict[str, Any]:
        resolved_limit = (
            max(int(limit), 1)
            if limit is not None
            else max(int(self.config.global_stats_limit), 1)
        )
        return get_global_stats(
            service=self.service,
            limit=resolved_limit,
        )

    def decide_action(
        self,
        *,
        facts: ManagerRunFacts,
        digest_context: dict[str, Any],
        global_stats: dict[str, Any] | None,
    ) -> tuple[ManagerAction, str]:
        rules_action = decide_next_action(facts)
        logger.info(
            "rules_action: state=%s kind=%s reason=%s",
            facts.state.value, rules_action.kind.value, rules_action.reason,
        )
        mode = self.config.normalized_mode()
        if mode == "rules":
            return rules_action, "rules"

        allowed = [item.value for item in allowed_action_kinds(facts)]
        tool_context = self._build_tool_context(
            run_id=facts.run_id,
            global_stats=global_stats,
        )
        if self.llm_client is None:
            if mode == "llm":
                return (
                    ManagerAction(
                        kind=ManagerActionKind.WAIT_HUMAN,
                        reason="manager llm unavailable; waiting human",
                    ),
                    "llm_unavailable",
                )
            return rules_action, "rules_fallback_llm_unavailable"

        try:
            selection = self.llm_client.decide_action(
                facts={
                    "run_id": facts.run_id,
                    "owner": facts.owner,
                    "repo": facts.repo,
                    "state": facts.state.value,
                    "prepare_attempts": facts.prepare_attempts,
                    "has_contract": facts.has_contract,
                    "has_prompt": facts.has_prompt,
                    "pr_number": facts.pr_number,
                    "latest_worker_grade": facts.latest_worker_grade,
                    "latest_worker_confidence": facts.latest_worker_confidence,
                    "latest_failure_reason_code": facts.latest_failure_reason_code,
                    "review_triage_action": facts.review_triage_action,
                    "retry_should_retry": facts.retry_should_retry,
                    "run_digest": digest_context,
                    "tools": tool_context,
                },
                allowed_actions=allowed,
            )
            kind = ManagerActionKind(selection.action)
            logger.info(
                "llm_action: kind=%s reason=%s allowed=%s",
                kind.value, selection.reason, allowed,
            )
            if kind.value not in allowed:
                raise ManagerLLMError(
                    f"selected action not allowed in current state: {kind.value}"
                )
            # LLM is allowed to downgrade any active action to WAIT_HUMAN.
            # This is the conservative path — LLM sees context rules can't
            # (global failure rates, missing artifacts, systemic issues).
            # Master Plan Section 8.3: "LLM can downgrade to WAIT_HUMAN".
            if (
                kind == ManagerActionKind.WAIT_HUMAN
                and rules_action.kind
                not in {ManagerActionKind.WAIT_HUMAN, ManagerActionKind.NOOP}
            ):
                logger.info(
                    "llm_downgrade_to_wait_human: rules_wanted=%s llm_reason=%s",
                    rules_action.kind.value, selection.reason,
                )
                return (
                    ManagerAction(
                        kind=ManagerActionKind.WAIT_HUMAN,
                        reason=selection.reason,
                    ),
                    "llm",
                )
            # Guardrail: when both sides pick active actions but disagree,
            # defer to rules (e.g. rules=RUN_FINISH, llm=RUN_AGENT_STEP).
            _PASSIVE = {ManagerActionKind.NOOP, ManagerActionKind.WAIT_HUMAN}
            if (
                kind not in _PASSIVE
                and rules_action.kind not in _PASSIVE
                and kind != rules_action.kind
            ):
                logger.warning(
                    "guardrail: llm_active_overridden_by_rules "
                    "llm_wanted=%s rules_override=%s",
                    kind.value, rules_action.kind.value,
                )
                return rules_action, "llm_active_overridden_by_rules"
            metadata: dict[str, Any] = {}
            if kind == ManagerActionKind.RETRY and selection.target_state:
                metadata["target_state"] = selection.target_state
            return (
                ManagerAction(
                    kind=kind,
                    reason=selection.reason,
                    metadata=metadata,
                ),
                "llm",
            )
        except (ManagerLLMError, ValueError) as exc:
            logger.warning("llm_error_fallback: %s", exc)
            if mode == "llm":
                return (
                    ManagerAction(
                        kind=ManagerActionKind.WAIT_HUMAN,
                        reason=f"manager llm error: {exc}",
                    ),
                    "llm_error",
                )
            return rules_action, "rules_fallback_llm_error"

    @staticmethod
    def _global_stats_show_failures(global_stats: dict[str, Any] | None) -> bool:
        """Check if global stats indicate recent systemic failures."""
        if not isinstance(global_stats, dict):
            return False
        # Check state_counts for NEEDS_HUMAN_REVIEW or FAILED runs
        state_counts = global_stats.get("state_counts")
        if isinstance(state_counts, dict):
            human_review = int(state_counts.get("NEEDS_HUMAN_REVIEW", 0))
            failed = int(state_counts.get("FAILED", 0))
            total = int(global_stats.get("sampled_runs", 0))
            if total > 0 and (human_review + failed) > total * 0.3:
                return True
        return False

    def _build_tool_context(
        self,
        *,
        run_id: str,
        global_stats: dict[str, Any] | None,
    ) -> dict[str, Any]:
        snapshot = self.service.get_run_snapshot(run_id)
        run = snapshot["run"]
        run_status = {
            "ok": True,
            "run_id": run_id,
            "owner": str(run.get("owner") or ""),
            "repo": str(run.get("repo") or ""),
            "state": str(snapshot.get("state") or ""),
            "pr_number": run.get("pr_number"),
            "updated_at": str(run.get("updated_at") or ""),
        }
        worker_analysis = analyze_worker_output(
            service=self.service,
            run_id=run_id,
        )
        return {
            "get_run_status": run_status,
            "analyze_worker_output": worker_analysis,
            "get_global_stats": (
                global_stats
                if isinstance(global_stats, dict)
                else self.compute_global_stats()
            ),
        }
