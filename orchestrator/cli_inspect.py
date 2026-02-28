"""Run inspection, skills feedback, and webhook audit functions."""

from __future__ import annotations

import json
import statistics
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

from .cli_helpers import (
    parse_optional_iso_datetime,
    percentile_ms,
    recommended_actions_for_state,
    summarize_command_categories,
    tail,
)
from .models import RunState, StepName
from .service import OrchestratorService
from .state_machine import allowed_targets

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Run inspection
# ---------------------------------------------------------------------------


def gather_run_inspect(
    *,
    service: OrchestratorService,
    run_id: str,
    attempt_limit: int,
    event_limit: int,
    command_limit: int,
    include_log_tails: bool,
) -> dict[str, Any]:
    snapshot = service.get_run_snapshot(run_id)
    run = snapshot["run"]
    current_state = RunState(str(snapshot["state"]))

    attempt_rows = service.list_step_attempts(
        run_id,
        limit=max(int(attempt_limit), 1),
    )
    attempt_rows = list(reversed(attempt_rows))

    total_duration_ms = 0
    per_step: dict[str, dict[str, Any]] = {}
    attempts_out: list[dict[str, Any]] = []
    for row in attempt_rows:
        step = str(row.get("step", "unknown"))
        duration_ms = int(row.get("duration_ms") or 0)
        total_duration_ms += max(duration_ms, 0)
        info = per_step.setdefault(
            step,
            {
                "attempts": 0,
                "total_duration_ms": 0,
                "durations_ms": [],
                "last_exit_code": None,
            },
        )
        info["attempts"] += 1
        info["total_duration_ms"] += max(duration_ms, 0)
        info["durations_ms"].append(max(duration_ms, 0))
        info["last_exit_code"] = int(row.get("exit_code") or 0)

        item: dict[str, Any] = {
            "step": step,
            "attempt_no": int(row.get("attempt_no") or 0),
            "exit_code": int(row.get("exit_code") or 0),
            "duration_ms": duration_ms,
            "created_at": str(row.get("created_at") or ""),
        }
        if include_log_tails:
            item["stdout_tail"] = tail(str(row.get("stdout_log") or ""))
            item["stderr_tail"] = tail(str(row.get("stderr_log") or ""))
        attempts_out.append(item)

    step_totals: list[dict[str, Any]] = []
    for step, info in per_step.items():
        durations = [int(v) for v in info["durations_ms"]]
        total_ms = int(info["total_duration_ms"])
        step_totals.append(
            {
                "step": step,
                "attempts": int(info["attempts"]),
                "total_duration_ms": total_ms,
                "avg_duration_ms": int(total_ms / max(int(info["attempts"]), 1)),
                "p50_duration_ms": percentile_ms(durations, 0.50),
                "p90_duration_ms": percentile_ms(durations, 0.90),
                "last_exit_code": info["last_exit_code"],
                "share_of_total_pct": round((100.0 * total_ms / total_duration_ms), 2)
                if total_duration_ms > 0
                else 0.0,
            }
        )
    step_totals.sort(key=lambda row: int(row["total_duration_ms"]), reverse=True)
    warnings: list[str] = []
    preflight_attempts = next(
        (int(row["attempts"]) for row in step_totals if str(row["step"]) == StepName.PREFLIGHT.value),
        0,
    )
    if preflight_attempts > 1:
        warnings.append(
            "multiple preflight attempts detected; avoid running both standalone "
            "run-preflight and default preflight inside run-agent-step unless required."
        )
    if step_totals and str(step_totals[0]["step"]) == StepName.AGENT.value:
        share_pct = float(step_totals[0]["share_of_total_pct"])
        if share_pct >= 85.0:
            warnings.append(
                f"agent step dominates runtime ({share_pct:.2f}%). focus optimization on "
                "dependency install/test scope and prompt-stage tasking."
            )

    event_rows = service.list_events(
        run_id,
        limit=max(int(event_limit), 1),
    )
    event_rows = list(reversed(event_rows))
    events_out: list[dict[str, Any]] = []
    for row in event_rows:
        payload = row.get("payload")
        payload_obj = payload if isinstance(payload, dict) else {}
        compact_payload = {
            key: payload_obj[key]
            for key in sorted(payload_obj.keys())[:8]
        }
        events_out.append(
            {
                "event_id": int(row.get("event_id") or 0),
                "event_type": str(row.get("event_type") or ""),
                "created_at": str(row.get("created_at") or ""),
                "idempotency_key": str(row.get("idempotency_key") or ""),
                "payload": compact_payload,
            }
        )

    latest_runtime = service.latest_artifact(run_id, artifact_type="agent_runtime_report")
    runtime_summary: dict[str, Any] | None = None
    if latest_runtime is not None:
        path = Path(str(latest_runtime.get("uri", "")))
        if path.exists():
            try:
                payload = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                payload = None
            if isinstance(payload, dict):
                signals = payload.get("signals")
                signals = signals if isinstance(signals, dict) else {}
                classification = payload.get("classification")
                classification = classification if isinstance(classification, dict) else {}
                runtime = payload.get("runtime")
                runtime = runtime if isinstance(runtime, dict) else {}
                result = payload.get("result")
                result = result if isinstance(result, dict) else {}
                commands_sample = [
                    str(item).strip()
                    for item in (signals.get("commands_sample") or [])
                    if str(item).strip()
                ]
                test_commands = [
                    str(item).strip()
                    for item in (signals.get("test_commands") or [])
                    if str(item).strip()
                ]
                failed_test_commands = [
                    str(item).strip()
                    for item in (signals.get("failed_test_commands") or [])
                    if str(item).strip()
                ]
                command_categories = signals.get("command_categories")
                if not isinstance(command_categories, dict):
                    command_categories = summarize_command_categories(commands_sample)
                agent_event_summary = signals.get("agent_event_summary")
                if not isinstance(agent_event_summary, dict):
                    agent_event_summary = {}
                event_stream_path = str(runtime.get("event_stream_path") or "")
                last_message_path = str(runtime.get("last_message_path") or "")
                last_message_preview = ""
                if last_message_path:
                    preview_path = Path(last_message_path)
                    if preview_path.exists():
                        try:
                            last_message_preview = tail(
                                preview_path.read_text(encoding="utf-8"),
                                lines=10,
                            )
                        except OSError:
                            last_message_preview = ""
                runtime_summary = {
                    "report_path": str(path),
                    "created_at": str(payload.get("created_at") or latest_runtime.get("created_at") or ""),
                    "duration_ms": int(result.get("duration_ms") or 0),
                    "exit_code": int(result.get("exit_code") or 0),
                    "grade": str(classification.get("grade") or ""),
                    "reason_code": str(classification.get("reason_code") or ""),
                    "next_action": str(classification.get("next_action") or ""),
                    "skills_mode": str(runtime.get("skills_mode") or "off"),
                    "attempt_no": runtime.get("attempt_no"),
                    "test_commands": test_commands[:20],
                    "test_command_count": len(test_commands),
                    "failed_test_commands": failed_test_commands[:20],
                    "failed_test_command_count": len(failed_test_commands),
                    "command_sample": commands_sample[: max(int(command_limit), 1)],
                    "command_sample_count": len(commands_sample),
                    "command_categories": command_categories,
                    "agent_event_summary": agent_event_summary,
                    "event_stream_path": event_stream_path,
                    "last_message_path": last_message_path,
                    "last_message_preview": last_message_preview,
                    "diff": signals.get("diff"),
                }

    latest_digest_summary: dict[str, Any] | None = None
    latest_digest = service.latest_artifact(run_id, artifact_type="run_digest")
    if latest_digest is not None:
        digest_path = Path(str(latest_digest.get("uri", "")))
        if digest_path.exists():
            try:
                digest_payload = json.loads(digest_path.read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError):
                digest_payload = None
            if isinstance(digest_payload, dict):
                classification = digest_payload.get("classification")
                classification = classification if isinstance(classification, dict) else {}
                state = digest_payload.get("state")
                state = state if isinstance(state, dict) else {}
                recommendation = digest_payload.get("manager_recommendation")
                recommendation = recommendation if isinstance(recommendation, dict) else {}
                stages = digest_payload.get("stages")
                stages = stages if isinstance(stages, dict) else {}
                latest_digest_summary = {
                    "path": str(digest_path),
                    "generated_at": str(digest_payload.get("generated_at") or ""),
                    "grade": str(classification.get("grade") or ""),
                    "reason_code": str(classification.get("reason_code") or ""),
                    "state_before": str(state.get("before") or ""),
                    "state_after": str(state.get("after") or ""),
                    "recommended_action": str(recommendation.get("action") or ""),
                    "top_step": str(stages.get("top_step") or ""),
                    "step_attempt_count": int(stages.get("step_attempt_count") or 0),
                }

    latest_manager_insight: dict[str, Any] | None = None
    insight_artifact = service.latest_artifact(run_id, artifact_type="manager_insight")
    if insight_artifact is not None:
        insight_path = Path(str(insight_artifact.get("uri", "")))
        if insight_path.exists():
            preview = ""
            try:
                preview = tail(insight_path.read_text(encoding="utf-8"), lines=24)
            except OSError:
                preview = ""
            latest_manager_insight = {
                "path": str(insight_path),
                "preview": preview,
            }

    step_start = (
        parse_optional_iso_datetime(str(attempt_rows[0]["created_at"]))
        if attempt_rows
        else None
    )
    step_end = (
        parse_optional_iso_datetime(str(attempt_rows[-1]["created_at"]))
        if attempt_rows
        else None
    )
    run_created = parse_optional_iso_datetime(str(run.get("created_at")))
    run_updated = parse_optional_iso_datetime(str(run.get("updated_at")))

    return {
        "ok": True,
        "run": {
            "run_id": str(run["run_id"]),
            "owner": str(run["owner"]),
            "repo": str(run["repo"]),
            "mode": str(run["mode"]),
            "prompt_version": str(run["prompt_version"]),
            "workspace_dir": str(run["workspace_dir"]),
            "pr_number": run.get("pr_number"),
            "created_at": str(run["created_at"]),
            "updated_at": str(run["updated_at"]),
        },
        "state": current_state.value,
        "allowed_targets": [item.value for item in allowed_targets(current_state)],
        "recommended_actions": recommended_actions_for_state(current_state),
        "warnings": warnings,
        "timing": {
            "step_attempt_count": len(attempt_rows),
            "total_step_duration_ms": total_duration_ms,
            "run_wall_clock_ms": int((run_updated - run_created).total_seconds() * 1000)
            if run_created is not None and run_updated is not None
            else None,
            "step_window_ms": int((step_end - step_start).total_seconds() * 1000)
            if step_start is not None and step_end is not None
            else None,
            "step_totals": step_totals,
            "attempts": attempts_out,
        },
        "events": {
            "count": len(events_out),
            "items": events_out,
        },
        "latest_agent_runtime": runtime_summary,
        "latest_run_digest": latest_digest_summary,
        "latest_manager_insight": latest_manager_insight,
    }


# ---------------------------------------------------------------------------
# Run bottlenecks
# ---------------------------------------------------------------------------


def gather_run_bottlenecks(
    *,
    service: OrchestratorService,
    limit: int,
    attempt_limit_per_run: int,
) -> dict[str, Any]:
    runs = service.list_runs(limit=max(int(limit), 1))
    per_step_samples: dict[str, list[int]] = defaultdict(list)
    run_totals: list[dict[str, Any]] = []
    analyzed_runs = 0
    for run in runs:
        run_id = str(run["run_id"])
        attempts = service.list_step_attempts(
            run_id,
            limit=max(int(attempt_limit_per_run), 1),
        )
        if not attempts:
            continue
        analyzed_runs += 1
        total_ms = 0
        by_step: dict[str, int] = defaultdict(int)
        for row in attempts:
            step = str(row.get("step") or "unknown")
            duration_ms = max(int(row.get("duration_ms") or 0), 0)
            total_ms += duration_ms
            by_step[step] += duration_ms
            per_step_samples[step].append(duration_ms)
        run_totals.append(
            {
                "run_id": run_id,
                "repo": str(run.get("repo") or ""),
                "owner": str(run.get("owner") or ""),
                "state": str(run.get("current_state") or ""),
                "prompt_version": str(run.get("prompt_version") or ""),
                "total_step_duration_ms": total_ms,
                "top_step": max(by_step.items(), key=lambda x: x[1])[0] if by_step else None,
                "top_step_duration_ms": max(by_step.values()) if by_step else 0,
                "updated_at": str(run.get("updated_at") or ""),
            }
        )

    step_stats: list[dict[str, Any]] = []
    for step, samples in per_step_samples.items():
        ordered = sorted(samples)
        step_stats.append(
            {
                "step": step,
                "samples": len(ordered),
                "avg_duration_ms": int(sum(ordered) / max(len(ordered), 1)),
                "median_duration_ms": int(statistics.median(ordered)),
                "p90_duration_ms": percentile_ms(ordered, 0.90),
                "max_duration_ms": int(max(ordered)),
            }
        )
    step_stats.sort(key=lambda row: int(row["avg_duration_ms"]), reverse=True)
    run_totals.sort(key=lambda row: int(row["total_step_duration_ms"]), reverse=True)

    return {
        "ok": True,
        "scanned_runs": len(runs),
        "analyzed_runs": analyzed_runs,
        "step_bottlenecks": step_stats,
        "slowest_runs": run_totals[:20],
    }


# ---------------------------------------------------------------------------
# Skills metrics
# ---------------------------------------------------------------------------


def gather_skills_metrics(
    *,
    service: OrchestratorService,
    run_id: str | None,
    limit: int,
) -> dict[str, Any]:
    if run_id:
        artifacts = service.list_artifacts(
            run_id,
            artifact_type="agent_runtime_report",
            limit=limit,
        )
    else:
        artifacts = service.list_artifacts_global(
            artifact_type="agent_runtime_report",
            limit=limit,
        )

    mode_counts: dict[str, int] = {}
    grade_counts: dict[str, int] = {}
    reason_counts: dict[str, int] = {}
    state_counts: dict[str, int] = {}
    per_skill: dict[str, dict[str, Any]] = {}
    missing_required_counts: dict[str, int] = {}
    failures: list[dict[str, Any]] = []
    rows: list[dict[str, Any]] = []

    for artifact in artifacts:
        uri = Path(str(artifact.get("uri", "")))
        if not uri.exists():
            failures.append(
                {
                    "run_id": artifact.get("run_id"),
                    "artifact_id": artifact.get("id"),
                    "error": f"missing runtime report: {uri}",
                }
            )
            continue
        try:
            payload = json.loads(uri.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            failures.append(
                {
                    "run_id": artifact.get("run_id"),
                    "artifact_id": artifact.get("id"),
                    "error": f"invalid runtime report {uri}: {exc}",
                }
            )
            continue

        runtime = payload.get("runtime") if isinstance(payload, dict) else {}
        classification = payload.get("classification") if isinstance(payload, dict) else {}
        runtime = runtime if isinstance(runtime, dict) else {}
        classification = classification if isinstance(classification, dict) else {}
        skills_mode = str(runtime.get("skills_mode", "off"))
        grade = str(classification.get("grade", "UNKNOWN"))
        reason_code = str(classification.get("reason_code", "unknown"))
        skill_plan = runtime.get("skill_plan")
        run_state = None
        if isinstance(skill_plan, dict) and skill_plan.get("run_state") is not None:
            run_state = str(skill_plan.get("run_state"))

        mode_counts[skills_mode] = mode_counts.get(skills_mode, 0) + 1
        grade_counts[grade] = grade_counts.get(grade, 0) + 1
        reason_counts[reason_code] = reason_counts.get(reason_code, 0) + 1
        if run_state:
            state_counts[run_state] = state_counts.get(run_state, 0) + 1

        required_now: list[str] = []
        available_optional: list[str] = []
        missing_required: list[str] = []
        if isinstance(skill_plan, dict):
            required_now = [str(item) for item in skill_plan.get("required_now", []) if str(item).strip()]
            available_optional = [str(item) for item in skill_plan.get("available_optional", []) if str(item).strip()]
            missing_required = [str(item) for item in skill_plan.get("missing_required", []) if str(item).strip()]
        for skill_name in missing_required:
            missing_required_counts[skill_name] = missing_required_counts.get(skill_name, 0) + 1

        seen_skills = sorted(set(required_now + available_optional))
        for skill_name in seen_skills:
            stats = per_skill.setdefault(
                skill_name,
                {
                    "skill": skill_name,
                    "samples": 0,
                    "grades": {},
                    "reasons": {},
                    "states": {},
                    "runs": [],
                },
            )
            stats["samples"] += 1
            stats["grades"][grade] = stats["grades"].get(grade, 0) + 1
            stats["reasons"][reason_code] = stats["reasons"].get(reason_code, 0) + 1
            if run_state:
                stats["states"][run_state] = stats["states"].get(run_state, 0) + 1
            if len(stats["runs"]) < 20:
                stats["runs"].append(str(payload.get("run_id") or artifact.get("run_id") or ""))

        rows.append(
            {
                "run_id": str(payload.get("run_id") or artifact.get("run_id") or ""),
                "created_at": str(payload.get("created_at") or artifact.get("created_at") or ""),
                "skills_mode": skills_mode,
                "run_state": run_state,
                "grade": grade,
                "reason_code": reason_code,
                "attempt_no": runtime.get("attempt_no"),
                "max_retryable_attempts": runtime.get("max_retryable_attempts"),
                "required_now": required_now,
                "available_optional": available_optional,
                "missing_required": missing_required,
                "report_path": str(uri),
            }
        )

    return {
        "ok": True,
        "scope_run_id": run_id,
        "scanned_artifacts": len(artifacts),
        "parsed_reports": len(rows),
        "mode_counts": mode_counts,
        "grade_counts": grade_counts,
        "reason_code_counts": reason_counts,
        "state_counts": state_counts,
        "per_skill": sorted(per_skill.values(), key=lambda row: int(row.get("samples", 0)), reverse=True),
        "missing_required_counts": missing_required_counts,
        "latest": rows[:20],
        "failures": failures[:20],
    }


# ---------------------------------------------------------------------------
# Skills feedback report
# ---------------------------------------------------------------------------


def build_skills_feedback_report(
    *,
    metrics: dict[str, Any],
    min_samples: int,
) -> dict[str, Any]:
    parsed_reports = int(metrics.get("parsed_reports") or 0)
    grade_counts_raw = metrics.get("grade_counts")
    grade_counts = grade_counts_raw if isinstance(grade_counts_raw, dict) else {}
    reason_counts_raw = metrics.get("reason_code_counts")
    reason_counts = reason_counts_raw if isinstance(reason_counts_raw, dict) else {}
    per_skill_raw = metrics.get("per_skill")
    per_skill = per_skill_raw if isinstance(per_skill_raw, list) else []
    missing_required_raw = metrics.get("missing_required_counts")
    missing_required_counts = (
        missing_required_raw if isinstance(missing_required_raw, dict) else {}
    )

    pass_count = int(grade_counts.get("PASS") or 0)
    retryable_count = int(grade_counts.get("RETRYABLE") or 0)
    human_review_count = int(grade_counts.get("HUMAN_REVIEW") or 0)
    total = max(parsed_reports, 0)

    def pct(value: int) -> float:
        if total <= 0:
            return 0.0
        return round(100.0 * float(value) / float(total), 2)

    top_reasons = sorted(
        (
            {"reason_code": str(key), "count": int(value)}
            for key, value in reason_counts.items()
        ),
        key=lambda row: int(row["count"]),
        reverse=True,
    )[:10]

    actions: list[dict[str, Any]] = []

    def add_action(
        *,
        action_id: str,
        priority: str,
        target: str,
        trigger: str,
        recommendation: str,
        acceptance: str,
    ) -> None:
        actions.append(
            {
                "id": action_id,
                "priority": priority,
                "target": target,
                "trigger": trigger,
                "recommendation": recommendation,
                "acceptance_check": acceptance,
            }
        )

    evidence_gap_count = int(reason_counts.get("missing_test_evidence") or 0) + int(
        reason_counts.get("insufficient_test_evidence") or 0
    )
    if evidence_gap_count > 0:
        add_action(
            action_id="prompt-impl-test-evidence",
            priority="high",
            target="agentpr-implement-and-validate + prompt_template",
            trigger=f"evidence_gap_count={evidence_gap_count}",
            recommendation=(
                "Strengthen implement/validate stage to require CI-aligned test commands "
                "with explicit command outputs in final report."
            ),
            acceptance=(
                "reason_code missing/insufficient_test_evidence decreases over next 10 runs."
            ),
        )

    failed_test_count = int(reason_counts.get("test_command_failed") or 0)
    if failed_test_count > 0:
        add_action(
            action_id="policy-known-baseline-failures",
            priority="high",
            target="manager_policy.run_agent_step.known_test_failure_allowlist",
            trigger=f"test_command_failed={failed_test_count}",
            recommendation=(
                "Split baseline failures from integration failures; add only verified baseline "
                "signatures to allowlist per repo, keep unknown failures as HUMAN_REVIEW."
            ),
            acceptance=(
                "reason_code test_command_failed decreases without increasing post-review regressions."
            ),
        )

    diff_budget_count = int(reason_counts.get("diff_budget_exceeded") or 0)
    if diff_budget_count > 0:
        add_action(
            action_id="policy-minimal-diff-tighten",
            priority="medium",
            target="manager_policy.run_agent_step.repo_overrides",
            trigger=f"diff_budget_exceeded={diff_budget_count}",
            recommendation=(
                "Tighten max_changed_files/max_added_lines for affected repos and reinforce "
                "minimal patch instructions in implement skill."
            ),
            acceptance=(
                "median changed_files_count and added_lines decrease while PASS rate stays stable."
            ),
        )

    transient_count = int(reason_counts.get("runtime_transient_failure") or 0) + int(
        reason_counts.get("runtime_unknown_failure") or 0
    )
    if transient_count > 0:
        add_action(
            action_id="policy-retry-and-timeout-calibration",
            priority="medium",
            target="manager_policy.run_agent_step.max_agent_seconds/retry caps",
            trigger=f"transient_like_failures={transient_count}",
            recommendation=(
                "Calibrate timeout and retry caps per repo; separate network/tooling failures "
                "from code-quality failures before re-dispatch."
            ),
            acceptance=(
                "RETRYABLE share decreases and retryable_limit_exceeded remains low."
            ),
        )

    if missing_required_counts:
        missing_names = ", ".join(sorted(str(key) for key in missing_required_counts.keys()))
        add_action(
            action_id="skills-install-integrity",
            priority="high",
            target="skills installation + bootstrap checks",
            trigger=f"missing_required_counts={missing_names}",
            recommendation=(
                "Enforce required skill presence in manager startup and fail fast before dispatch."
            ),
            acceptance=(
                "missing_required_counts remains empty for all new runs."
            ),
        )

    low_pass_skills: list[dict[str, Any]] = []
    for row in per_skill:
        if not isinstance(row, dict):
            continue
        skill_name = str(row.get("skill") or "").strip()
        if not skill_name:
            continue
        samples = max(int(row.get("samples") or 0), 0)
        if samples < max(int(min_samples), 1):
            continue
        grades_raw = row.get("grades")
        grades = grades_raw if isinstance(grades_raw, dict) else {}
        pass_samples = int(grades.get("PASS") or 0)
        pass_rate_pct = round(100.0 * pass_samples / max(samples, 1), 2)
        if pass_rate_pct >= 60.0:
            continue
        reasons_raw = row.get("reasons")
        reasons = reasons_raw if isinstance(reasons_raw, dict) else {}
        dominant_reason = ""
        dominant_count = 0
        for key, value in reasons.items():
            count = int(value or 0)
            if count > dominant_count:
                dominant_reason = str(key)
                dominant_count = count
        low_pass_skills.append(
            {
                "skill": skill_name,
                "samples": samples,
                "pass_rate_pct": pass_rate_pct,
                "dominant_reason": dominant_reason,
                "dominant_reason_count": dominant_count,
            }
        )
    low_pass_skills.sort(key=lambda row: float(row["pass_rate_pct"]))
    for row in low_pass_skills[:5]:
        add_action(
            action_id=f"skill-tune-{str(row['skill']).replace('/', '-')}",
            priority="medium",
            target=str(row["skill"]),
            trigger=(
                f"samples={row['samples']},pass_rate_pct={row['pass_rate_pct']},"
                f"dominant_reason={row['dominant_reason']}"
            ),
            recommendation=(
                "Update skill checklist/exit criteria for dominant failure mode and "
                "re-run A/B on at least 3 repos."
            ),
            acceptance=(
                f"{row['skill']} pass_rate_pct reaches >= 70 over next {max(int(min_samples), 1)}+ samples."
            ),
        )

    stamp = datetime.now(UTC).strftime("%Y%m%d")
    return {
        "ok": True,
        "generated_at": datetime.now(UTC).isoformat(),
        "scope_run_id": metrics.get("scope_run_id"),
        "input_summary": {
            "parsed_reports": parsed_reports,
            "pass_count": pass_count,
            "retryable_count": retryable_count,
            "human_review_count": human_review_count,
            "pass_rate_pct": pct(pass_count),
            "retryable_rate_pct": pct(retryable_count),
            "human_review_rate_pct": pct(human_review_count),
        },
        "top_reasons": top_reasons,
        "low_pass_skills": low_pass_skills[:10],
        "actions": actions,
        "suggested_versions": {
            "prompt_version": f"auto-prompt-{stamp}",
            "skills_bundle_version": f"auto-skills-{stamp}",
        },
        "metrics_snapshot": {
            "grade_counts": grade_counts,
            "reason_code_counts": reason_counts,
            "missing_required_counts": missing_required_counts,
        },
    }


# ---------------------------------------------------------------------------
# Skills feedback I/O
# ---------------------------------------------------------------------------


def write_skills_feedback_json(payload: dict[str, Any]) -> Path:
    reports_dir = PROJECT_ROOT / "orchestrator" / "data" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    out_path = reports_dir / f"skills_feedback_{stamp}.json"
    out_path.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return out_path


def render_skills_feedback_markdown(payload: dict[str, Any]) -> str:
    summary_raw = payload.get("input_summary")
    summary = summary_raw if isinstance(summary_raw, dict) else {}
    top_reasons_raw = payload.get("top_reasons")
    top_reasons = top_reasons_raw if isinstance(top_reasons_raw, list) else []
    actions_raw = payload.get("actions")
    actions = actions_raw if isinstance(actions_raw, list) else []
    suggested_versions_raw = payload.get("suggested_versions")
    suggested_versions = (
        suggested_versions_raw if isinstance(suggested_versions_raw, dict) else {}
    )

    lines = [
        "# Skills Feedback",
        "",
        f"- Generated at: {payload.get('generated_at', '')}",
        f"- Scope run_id: {payload.get('scope_run_id', '') or 'global'}",
        "",
        "## Summary",
        f"- Parsed reports: {summary.get('parsed_reports', 0)}",
        f"- PASS rate: {summary.get('pass_rate_pct', 0)}%",
        f"- RETRYABLE rate: {summary.get('retryable_rate_pct', 0)}%",
        f"- HUMAN_REVIEW rate: {summary.get('human_review_rate_pct', 0)}%",
        "",
        "## Top Reasons",
    ]
    if top_reasons:
        for row in top_reasons[:10]:
            if not isinstance(row, dict):
                continue
            lines.append(f"- {row.get('reason_code', '')}: {row.get('count', 0)}")
    else:
        lines.append("- No reason-code data.")

    lines.extend(["", "## Actions"])
    if actions:
        for idx, row in enumerate(actions, start=1):
            if not isinstance(row, dict):
                continue
            lines.append(
                f"{idx}. [{row.get('priority', 'normal')}] {row.get('id', '')} -> {row.get('target', '')}"
            )
            lines.append(f"   Trigger: {row.get('trigger', '')}")
            lines.append(f"   Change: {row.get('recommendation', '')}")
            lines.append(f"   Accept: {row.get('acceptance_check', '')}")
    else:
        lines.append("1. No actions generated; keep current policy/prompt and continue sampling.")

    lines.extend(
        [
            "",
            "## Suggested Versions",
            f"- prompt_version: {suggested_versions.get('prompt_version', '')}",
            f"- skills_bundle_version: {suggested_versions.get('skills_bundle_version', '')}",
            "",
        ]
    )
    return "\n".join(lines)


def write_skills_feedback_markdown(payload: dict[str, Any]) -> Path:
    reports_dir = PROJECT_ROOT / "orchestrator" / "data" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    out_path = reports_dir / f"skills_feedback_{stamp}.md"
    out_path.write_text(
        render_skills_feedback_markdown(payload),
        encoding="utf-8",
    )
    return out_path


# ---------------------------------------------------------------------------
# Webhook audit
# ---------------------------------------------------------------------------


def summarize_webhook_audit_log(
    *,
    audit_log_file: Path,
    since_minutes: int,
    max_lines: int,
    fail_on_retryable_failures: int | None,
    fail_on_http5xx_rate: float | None,
) -> dict[str, Any]:
    if not audit_log_file.exists():
        return {
            "ok": False,
            "error": f"audit log file not found: {audit_log_file}",
            "audit_log_file": str(audit_log_file),
            "since_minutes": since_minutes,
            "max_lines": max_lines,
        }

    try:
        lines = audit_log_file.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return {
            "ok": False,
            "error": f"failed to read audit log: {exc}",
            "audit_log_file": str(audit_log_file),
            "since_minutes": since_minutes,
            "max_lines": max_lines,
        }

    tail_lines = lines[-max_lines:]
    now = datetime.now(UTC)
    window_start = now - timedelta(minutes=max(since_minutes, 1))
    outcome_counts: dict[str, int] = {}
    status_code_counts: dict[str, int] = {}
    error_counts: dict[str, int] = {}
    retryable_failures = 0
    http_5xx_count = 0
    parse_errors = 0
    considered = 0

    for line in tail_lines:
        text = line.strip()
        if not text:
            continue
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            parse_errors += 1
            continue
        if not isinstance(payload, dict):
            parse_errors += 1
            continue
        raw_ts = payload.get("ts")
        if raw_ts is None:
            parse_errors += 1
            continue
        try:
            ts = datetime.fromisoformat(str(raw_ts))
            if ts.tzinfo is None:
                ts = ts.replace(tzinfo=UTC)
            ts = ts.astimezone(UTC)
        except ValueError:
            parse_errors += 1
            continue
        if ts < window_start:
            continue
        considered += 1
        outcome = str(payload.get("outcome", "unknown"))
        outcome_counts[outcome] = outcome_counts.get(outcome, 0) + 1

        status_code = int(payload.get("status_code", 0))
        status_key = str(status_code)
        status_code_counts[status_key] = status_code_counts.get(status_key, 0) + 1
        if status_code >= 500:
            http_5xx_count += 1

        retryable_value = int(payload.get("retryable_failures", 0))
        retryable_failures += max(retryable_value, 0)

        error_text = str(payload.get("error", "")).strip()
        if error_text:
            error_counts[error_text] = error_counts.get(error_text, 0) + 1

    http_5xx_rate_pct = (
        round((http_5xx_count / considered) * 100.0, 2) if considered > 0 else 0.0
    )
    alerts: list[str] = []
    ok = True
    if (
        fail_on_retryable_failures is not None
        and retryable_failures > int(fail_on_retryable_failures)
    ):
        ok = False
        alerts.append(
            "retryable_failures_exceeded:"
            f"{retryable_failures}>{int(fail_on_retryable_failures)}"
        )
    if fail_on_http5xx_rate is not None and http_5xx_rate_pct > float(fail_on_http5xx_rate):
        ok = False
        alerts.append(
            "http_5xx_rate_exceeded:"
            f"{http_5xx_rate_pct}>{float(fail_on_http5xx_rate)}"
        )

    top_errors = sorted(error_counts.items(), key=lambda item: item[1], reverse=True)[:10]
    return {
        "ok": ok,
        "audit_log_file": str(audit_log_file),
        "since_minutes": since_minutes,
        "window_start": window_start.isoformat(),
        "window_end": now.isoformat(),
        "max_lines": max_lines,
        "total_lines_read": len(tail_lines),
        "parse_errors": parse_errors,
        "considered_entries": considered,
        "outcome_counts": outcome_counts,
        "status_code_counts": status_code_counts,
        "retryable_failures": retryable_failures,
        "http_5xx_count": http_5xx_count,
        "http_5xx_rate_pct": http_5xx_rate_pct,
        "top_errors": [{"error": text, "count": count} for text, count in top_errors],
        "alerts": alerts,
    }
