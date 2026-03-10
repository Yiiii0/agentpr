from __future__ import annotations

import json
import subprocess
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .service import OrchestratorService


# ── Skill Improvement Proposal Patterns ──────────────────────────
# Each pattern: (reason_code_match, proposal_key, skill_name, lesson, suggestion)
# When a grading result matches reason_code_match, a proposal is generated.

SKILL_IMPROVEMENT_PATTERNS: list[dict[str, str]] = [
    {
        "reason_code": "missing_test_evidence",
        "proposal_key": "validation_resilience",
        "skill_name": "agentpr-implement-and-validate",
        "target_file": "references/validation_requirements.md",
        "lesson": "Worker gave up validation after install failure. Zero test/lint commands detected.",
        "suggestion": "Strengthen fallback validation: if install fails, still try pytest/ruff/pre-commit. A failed attempt is better than no attempt.",
    },
    {
        "reason_code": "timeout_with_partial_changes",
        "proposal_key": "timeout_handling",
        "skill_name": "agentpr-implement-and-validate",
        "target_file": "references/self_review_checklist.md",
        "lesson": "Worker timed out but had partial code changes. Work should not be discarded.",
        "suggestion": "Add timeout awareness: if running long, prioritize committing partial work over running more validation.",
    },
    {
        "reason_code": "no_changes_detected",
        "proposal_key": "early_stop_prevention",
        "skill_name": "agentpr-implement-and-validate",
        "target_file": "references/forge_rules.md",
        "lesson": "Worker completed with exit_code=0 but made no code changes.",
        "suggestion": "Worker may have analyzed the repo and stopped before implementing. Ensure Phase 2 (implementation) always produces code changes.",
    },
    {
        "reason_code": "diff_budget_exceeded",
        "proposal_key": "minimal_diff_discipline",
        "skill_name": "agentpr-implement-and-validate",
        "target_file": "references/forge_rules.md",
        "lesson": "Worker exceeded the diff budget (too many files or lines changed).",
        "suggestion": "Strengthen minimal-diff contract: limit touched files, prefer targeted patch over broad refactor.",
    },
]


def analyze_worker_output(
    *,
    service: OrchestratorService,
    run_id: str,
) -> dict[str, Any]:
    digest_artifact = service.latest_artifact(run_id, artifact_type="run_digest")
    artifact_type = "run_digest"
    if digest_artifact is None:
        digest_artifact = service.latest_artifact(run_id, artifact_type="agent_runtime")
        artifact_type = "agent_runtime"
    if digest_artifact is None:
        return {
            "ok": False,
            "run_id": run_id,
            "error": "no_prior_worker_execution",
            "hint": "Worker has not run yet. This is normal for first attempts — proceed with run_agent_step.",
        }
    uri = str(digest_artifact.get("uri") or "").strip()
    if not uri:
        return {
            "ok": False,
            "run_id": run_id,
            "error": "empty_worker_runtime_artifact_uri",
        }
    try:
        import json
        from pathlib import Path

        payload = json.loads(Path(uri).read_text(encoding="utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {
            "ok": False,
            "run_id": run_id,
            "error": f"worker_runtime_artifact_unreadable:{exc}",
        }
    if not isinstance(payload, dict):
        return {
            "ok": False,
            "run_id": run_id,
            "error": "worker_runtime_artifact_invalid_payload",
        }
    if artifact_type == "agent_runtime":
        classification = payload.get("classification")
        classification = classification if isinstance(classification, dict) else {}
        signals = payload.get("signals")
        signals = signals if isinstance(signals, dict) else {}
        test_cmds = list(signals.get("test_commands") or [])
        lint_cmds = list(signals.get("lint_or_validation_commands") or [])
        failed_cmds = list(signals.get("failed_test_commands") or [])
        validation = {
            "test_command_count": len(test_cmds),
            "lint_or_validation_command_count": len(lint_cmds),
            "failed_test_command_count": len(failed_cmds),
            "test_command_samples": test_cmds[:3],
            "failed_test_samples": failed_cmds[:3],
        }
        changes = signals.get("diff")
        changes = changes if isinstance(changes, dict) else {}
        changed_files = changes.get("changed_files")
        changed_files = list(changed_files) if isinstance(changed_files, list) else []
        return {
            "ok": True,
            "run_id": run_id,
            "artifact_type": artifact_type,
            "artifact_uri": uri,
            "classification": {
                "grade": str(classification.get("grade") or ""),
                "reason_code": str(classification.get("reason_code") or ""),
                "next_action": str(classification.get("next_action") or ""),
                "semantic": payload.get("semantic_grading"),
            },
            "validation": validation,
            "changes": {
                "changed_files_count": int(changes.get("changed_files_count") or 0),
                "added_lines": int(changes.get("added_lines") or 0),
                "deleted_lines": int(changes.get("deleted_lines") or 0),
                "changed_files": changed_files[:10],
            },
            "manager_recommendation": {},
        }
    classification = payload.get("classification")
    classification = classification if isinstance(classification, dict) else {}
    validation = payload.get("validation")
    validation = validation if isinstance(validation, dict) else {}
    changes = payload.get("changes")
    changes = changes if isinstance(changes, dict) else {}
    recommendation = payload.get("manager_recommendation")
    recommendation = recommendation if isinstance(recommendation, dict) else {}
    test_samples = validation.get("test_command_samples")
    test_samples = list(test_samples) if isinstance(test_samples, list) else []
    failed_samples = validation.get("failed_test_samples")
    failed_samples = list(failed_samples) if isinstance(failed_samples, list) else []
    changed_files = changes.get("changed_files")
    changed_files = list(changed_files) if isinstance(changed_files, list) else []
    return {
        "ok": True,
        "run_id": run_id,
        "artifact_type": artifact_type,
        "artifact_uri": uri,
        "classification": {
            "grade": str(classification.get("grade") or ""),
            "reason_code": str(classification.get("reason_code") or ""),
            "next_action": str(classification.get("next_action") or ""),
            "semantic": classification.get("semantic"),
        },
        "validation": {
            "test_command_count": int(validation.get("test_command_count") or 0),
            "lint_or_validation_command_count": int(
                validation.get("lint_or_validation_command_count") or 0
            ),
            "failed_test_command_count": int(
                validation.get("failed_test_command_count") or 0
            ),
            "test_command_samples": test_samples[:3],
            "failed_test_samples": failed_samples[:3],
        },
        "changes": {
            "changed_files_count": int(changes.get("changed_files_count") or 0),
            "added_lines": int(changes.get("added_lines") or 0),
            "deleted_lines": int(changes.get("deleted_lines") or 0),
            "changed_files": changed_files[:10],
        },
        "manager_recommendation": recommendation,
    }


def get_global_stats(
    *,
    service: OrchestratorService,
    limit: int = 200,
) -> dict[str, Any]:
    rows = service.list_runs(limit=max(int(limit), 1))
    state_counter: Counter[str] = Counter()
    grade_counter: Counter[str] = Counter()
    reason_counter: Counter[str] = Counter()
    digest_available = 0
    for row in rows:
        state_counter[str(row.get("display_state") or row.get("current_state") or "UNKNOWN")] += 1
        run_id = str(row.get("run_id") or "").strip()
        if not run_id:
            continue
        analyzed = analyze_worker_output(service=service, run_id=run_id)
        if not analyzed.get("ok"):
            continue
        digest_available += 1
        cls = analyzed.get("classification")
        cls = cls if isinstance(cls, dict) else {}
        grade_counter[str(cls.get("grade") or "UNKNOWN")] += 1
        reason_counter[str(cls.get("reason_code") or "unknown")] += 1
    total = len(rows)
    pass_rate = 0.0
    if digest_available > 0:
        pass_rate = round(
            100.0 * float(grade_counter.get("PASS", 0)) / float(digest_available), 2
        )
    return {
        "ok": True,
        "sampled_runs": total,
        "digest_available_runs": digest_available,
        "pass_rate_pct": pass_rate,
        "state_counts": dict(state_counter),
        "grade_counts": dict(grade_counter),
        "top_reason_codes": reason_counter.most_common(10),
    }


def notify_user(
    *,
    service: OrchestratorService,
    run_id: str,
    message: str,
    priority: str,
    channel: str = "manager",
) -> dict[str, Any]:
    normalized_priority = str(priority).strip().lower() or "normal"
    if normalized_priority not in {"low", "normal", "high", "urgent"}:
        normalized_priority = "normal"
    text = str(message).strip()
    if not text:
        raise ValueError("notify_user message cannot be empty")
    metadata = {
        "channel": str(channel).strip() or "manager",
        "priority": normalized_priority,
    }
    service.add_artifact(
        run_id,
        artifact_type="manager_notification",
        uri=f"inline://notification/{run_id}",
        metadata={"message": text, **metadata},
    )
    return {
        "ok": True,
        "run_id": run_id,
        "channel": metadata["channel"],
        "priority": normalized_priority,
        "message": text,
    }


# ── Skill Improvement Proposals ──────────────────────────────────


def propose_skill_improvement(
    *,
    service: OrchestratorService,
    run_id: str,
    proposal_key: str,
    skill_name: str,
    target_file: str,
    lesson: str,
    suggestion: str,
    evidence: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Store a skill improvement proposal as an artifact for human review."""
    metadata = {
        "proposal_key": str(proposal_key).strip(),
        "skill_name": str(skill_name).strip(),
        "target_file": str(target_file).strip(),
        "lesson": str(lesson).strip(),
        "suggestion": str(suggestion).strip(),
        "evidence": evidence or {},
        "created_at": datetime.now(UTC).isoformat(),
        "status": "pending",
    }
    service.add_artifact(
        run_id,
        artifact_type="skill_improvement_proposal",
        uri=f"inline://skill_proposal/{run_id}/{proposal_key}",
        metadata=metadata,
    )
    return {"ok": True, "run_id": run_id, **metadata}


def detect_skill_improvement_proposals(
    *,
    run_id: str,
    reason_code: str,
    grade: str,
    evidence: dict[str, Any] | None = None,
) -> list[dict[str, Any]]:
    """Detect which skill improvement proposals should be generated based on grading result.

    Returns a list of proposal dicts (without storing them — caller decides).
    """
    proposals: list[dict[str, Any]] = []
    normalized_reason = str(reason_code).strip().lower()
    normalized_grade = str(grade).strip().upper()

    # Only generate proposals for non-PASS grades
    if normalized_grade == "PASS":
        return proposals

    for pattern in SKILL_IMPROVEMENT_PATTERNS:
        if pattern["reason_code"] == normalized_reason:
            proposals.append({
                "run_id": run_id,
                "proposal_key": pattern["proposal_key"],
                "skill_name": pattern["skill_name"],
                "target_file": pattern["target_file"],
                "lesson": pattern["lesson"],
                "suggestion": pattern["suggestion"],
                "evidence": {
                    "reason_code": normalized_reason,
                    "grade": normalized_grade,
                    **(evidence or {}),
                },
            })

    return proposals


def list_skill_proposals(
    *,
    service: OrchestratorService,
    limit: int = 100,
) -> list[dict[str, Any]]:
    """List all pending skill improvement proposals across all runs."""
    runs = service.list_runs(limit=max(limit, 1))
    proposals: list[dict[str, Any]] = []
    seen_keys: set[str] = set()

    for run in runs:
        run_id = str(run.get("run_id") or "").strip()
        if not run_id:
            continue
        artifacts = service.list_artifacts(
            run_id, artifact_type="skill_improvement_proposal"
        )
        for art in artifacts:
            meta = art.get("metadata")
            if not isinstance(meta, dict):
                continue
            key = f"{meta.get('proposal_key')}:{meta.get('skill_name')}"
            if key in seen_keys:
                continue
            seen_keys.add(key)
            proposals.append({
                "run_id": run_id,
                "owner": str(run.get("owner") or ""),
                "repo": str(run.get("repo") or ""),
                **meta,
            })

    return proposals


# ── Self-Iteration: Safety Tiers ──────────────────────────────────

# Low-risk files: auto-apply (additive lesson, recoverable if wrong)
# High-risk files: propose only, require human /approve_skill
SKILL_FILE_SAFETY_TIERS: dict[str, str] = {
    "references/forge_rules.md": "low",
    "references/self_review_checklist.md": "low",
    "references/validation_requirements.md": "low",
    "references/code_review_checklist.md": "low",
    "SKILL.md": "high",
}


def apply_skill_improvement(
    *,
    service: OrchestratorService,
    run_id: str,
    proposal: dict[str, Any],
    skills_source_root: Path,
    project_root: Path,
) -> dict[str, Any]:
    """Apply a skill improvement proposal.

    Low-risk files: auto-append lesson + git commit.
    High-risk files: store as pending proposal + notify.
    """
    target_file = str(proposal.get("target_file", "")).strip()
    skill_name = str(proposal.get("skill_name", "")).strip()
    lesson = str(proposal.get("lesson", "")).strip()
    suggestion = str(proposal.get("suggestion", "")).strip()
    proposal_key = str(proposal.get("proposal_key", "")).strip()

    tier = SKILL_FILE_SAFETY_TIERS.get(target_file, "high")

    if tier == "high":
        # Store as pending proposal, do not auto-apply
        propose_skill_improvement(
            service=service,
            run_id=run_id,
            proposal_key=proposal_key,
            skill_name=skill_name,
            target_file=target_file,
            lesson=lesson,
            suggestion=suggestion,
            evidence=proposal.get("evidence"),
        )
        notify_user(
            service=service,
            run_id=run_id,
            message=(
                f"Skill improvement proposal (HIGH risk, needs approval): "
                f"{proposal_key} → {target_file}. "
                f"Lesson: {lesson[:100]}. "
                f"Use /approve_skill {run_id} {proposal_key}"
            ),
            priority="high",
        )
        return {"ok": True, "action": "proposed", "tier": "high", "proposal_key": proposal_key}

    # Low-risk: auto-apply by appending to the target file
    skill_dir = skills_source_root / skill_name
    file_path = skill_dir / target_file
    if not file_path.is_file():
        return {"ok": False, "error": f"target file not found: {file_path}"}

    # Append lesson as a new rule
    append_text = (
        f"\n\n<!-- auto-improvement: {proposal_key} -->\n"
        f"### Lesson: {lesson}\n"
        f"{suggestion}\n"
    )
    try:
        with open(file_path, "a", encoding="utf-8") as f:
            f.write(append_text)
    except OSError as exc:
        return {"ok": False, "error": f"write failed: {exc}"}

    # Git commit for rollback
    try:
        subprocess.run(
            ["git", "add", str(file_path)],
            cwd=str(project_root),
            capture_output=True,
            timeout=10,
        )
        subprocess.run(
            ["git", "commit", "-m", f"skill-improve({proposal_key}): {lesson[:60]}"],
            cwd=str(project_root),
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        pass  # Git commit is best-effort

    # Mark as applied
    service.add_artifact(
        run_id,
        artifact_type="skill_improvement_applied",
        uri=f"inline://skill_applied/{run_id}/{proposal_key}",
        metadata={
            "proposal_key": proposal_key,
            "skill_name": skill_name,
            "target_file": target_file,
            "tier": "low",
            "applied_at": datetime.now(UTC).isoformat(),
        },
    )

    return {
        "ok": True,
        "action": "applied",
        "tier": "low",
        "proposal_key": proposal_key,
        "file": str(file_path),
    }
