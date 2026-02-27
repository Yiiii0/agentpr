from __future__ import annotations

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
                    "review_triage_action": facts.review_triage_action,
                    "retry_should_retry": facts.retry_should_retry,
                    "run_digest": digest_context,
                    "tools": tool_context,
                },
                allowed_actions=allowed,
            )
            kind = ManagerActionKind(selection.action)
            if kind.value not in allowed:
                raise ManagerLLMError(
                    f"selected action not allowed in current state: {kind.value}"
                )
            # Guardrail: keep throughput when LLM is overly conservative.
            if (
                kind == ManagerActionKind.WAIT_HUMAN
                and rules_action.kind
                not in {ManagerActionKind.WAIT_HUMAN, ManagerActionKind.NOOP}
            ):
                return rules_action, "llm_wait_human_overridden_by_rules"
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
            if mode == "llm":
                return (
                    ManagerAction(
                        kind=ManagerActionKind.WAIT_HUMAN,
                        reason=f"manager llm error: {exc}",
                    ),
                    "llm_error",
                )
            return rules_action, "rules_fallback_llm_error"

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
