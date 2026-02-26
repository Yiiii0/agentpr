from __future__ import annotations

from collections import Counter
from typing import Any

from .service import OrchestratorService


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
        return {"ok": False, "run_id": run_id, "error": "missing_worker_runtime_artifact"}
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
        validation = {
            "test_command_count": len(list(signals.get("test_commands") or [])),
            "lint_or_validation_command_count": len(
                list(signals.get("lint_or_validation_commands") or [])
            ),
            "failed_test_command_count": len(
                list(signals.get("failed_test_commands") or [])
            ),
        }
        changes = signals.get("diff")
        changes = changes if isinstance(changes, dict) else {}
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
        },
        "changes": {
            "changed_files_count": int(changes.get("changed_files_count") or 0),
            "added_lines": int(changes.get("added_lines") or 0),
            "deleted_lines": int(changes.get("deleted_lines") or 0),
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
