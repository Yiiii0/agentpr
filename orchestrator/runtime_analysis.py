from __future__ import annotations

import hashlib
import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .manager_llm import ManagerLLMClient, ManagerLLMError
from .models import AgentRuntimeGrade, RunState
from .service import OrchestratorService

PROJECT_ROOT = Path(__file__).resolve().parents[1]

TEST_COMMAND_PATTERNS: tuple[str, ...] = (
    r"\bpytest\b",
    r"\btox\b",
    r"\bmake\s+test\b",
    r"\bbun\s+test\b",
    r"\bnpm\s+test\b",
    r"\bpnpm\s+test\b",
    r"\byarn\s+test\b",
    r"\bhatch\s+run\s+.*\btest\b",
)

LINT_OR_VALIDATION_COMMAND_PATTERNS: tuple[str, ...] = (
    r"\bmake\s+lint\b",
    r"\bruff\b",
    r"\beslint\b",
    r"\bflake8\b",
    r"\bmypy\b",
    r"\bpyright\b",
    r"\btypecheck\b",
    r"\bpre-commit\b",
)

TEST_INFRA_DEPENDENCY_PATTERNS: tuple[str, ...] = (
    r"\bpytest\b",
    r"\btox\b",
    r"\bjest\b",
    r"\bvitest\b",
    r"\bunittest\b",
    r"\bava\b",
    r"\bmocha\b",
    r"\bcypress\b",
    r"\bplaywright\b",
)

TEST_INFRA_WORKFLOW_PATTERNS: tuple[str, ...] = (
    r"\bpytest\b",
    r"\btox\b",
    r"\bmake\s+test\b",
    r"\bnpm\s+test\b",
    r"\bpnpm\s+test\b",
    r"\byarn\s+test\b",
    r"\bbun\s+test\b",
    r"\bgo\s+test\b",
    r"\bcargo\s+test\b",
    r"\bunit\s*test\b",
)

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


def _safe_dict(obj: Any) -> dict[str, Any]:
    return obj if isinstance(obj, dict) else {}


def _write_report(run_id: str, suffix: str, content: str, *, ext: str = "json") -> Path:
    reports_dir = PROJECT_ROOT / "orchestrator" / "data" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = reports_dir / f"{run_id}_{suffix}_{stamp}.{ext}"
    path.write_text(content, encoding="utf-8")
    return path


def contains_any_pattern(text: str, patterns: tuple[str, ...]) -> bool:
    return any(re.search(pattern, text, flags=re.IGNORECASE) for pattern in patterns)


def summarize_command_categories(commands: list[str]) -> dict[str, int]:
    counts = {
        "dependency_install": 0,
        "tests": 0,
        "lint_or_typecheck": 0,
        "git_ops": 0,
        "repo_reading": 0,
        "other": 0,
    }
    install_patterns = (
        r"\bpip\s+install\b",
        r"\buv\s+sync\b",
        r"\buv\s+pip\b",
        r"\bpoetry\s+install\b",
        r"\brye\s+sync\b",
        r"\bhatch\s+run\s+.*\bpip\s+install\b",
        r"\bnpm\s+(ci|install)\b",
        r"\bpnpm\s+install\b",
        r"\bbun\s+install\b",
        r"\byarn\s+install\b",
    )
    test_patterns = TEST_COMMAND_PATTERNS
    lint_patterns = LINT_OR_VALIDATION_COMMAND_PATTERNS
    git_patterns = (
        r"\bgit\s+status\b",
        r"\bgit\s+diff\b",
        r"\bgit\s+log\b",
        r"\bgit\s+add\b",
        r"\bgit\s+commit\b",
        r"\bgit\s+push\b",
        r"\bgit\s+fetch\b",
    )
    read_patterns = (
        r"\brg\b",
        r"\bfind\b",
        r"\bls\b",
        r"\bcat\b",
        r"\bsed\b",
        r"\bawk\b",
        r"\bhead\b",
        r"\btail\b",
    )
    for command in commands:
        normalized = str(command).strip()
        if not normalized:
            continue
        if contains_any_pattern(normalized, install_patterns):
            counts["dependency_install"] += 1
            continue
        if contains_any_pattern(normalized, test_patterns):
            counts["tests"] += 1
            continue
        if contains_any_pattern(normalized, lint_patterns):
            counts["lint_or_typecheck"] += 1
            continue
        if contains_any_pattern(normalized, git_patterns):
            counts["git_ops"] += 1
            continue
        if contains_any_pattern(normalized, read_patterns):
            counts["repo_reading"] += 1
            continue
        counts["other"] += 1
    return counts


def parse_optional_int(value: Any) -> int | None:
    if value is None:
        return None
    if isinstance(value, bool):
        return int(value)
    if isinstance(value, int):
        return value
    if isinstance(value, float):
        return int(value)
    text = str(value).strip()
    if not text:
        return None
    try:
        return int(float(text))
    except ValueError:
        return None


def dedupe_strings(values: list[str], *, limit: int | None = None) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    max_items = int(limit) if limit is not None else 0
    for value in values:
        text = str(value).strip()
        if not text or text in seen:
            continue
        seen.add(text)
        out.append(text)
        if max_items > 0 and len(out) >= max_items:
            break
    return out


def detect_commands_by_patterns(
    *,
    commands: list[str],
    patterns: tuple[str, ...],
    limit: int = 40,
) -> list[str]:
    matched = [
        command
        for command in commands
        if str(command).strip() and contains_any_pattern(str(command), patterns)
    ]
    return dedupe_strings(matched, limit=limit)


def _read_text_file(path: Path, *, max_bytes: int = 200_000) -> str:
    if not path.exists() or not path.is_file():
        return ""
    try:
        data = path.read_bytes()
    except OSError:
        return ""
    clipped = data[:max(int(max_bytes), 1)]
    return clipped.decode("utf-8", errors="replace")


def scan_repo_test_infrastructure(repo_dir: Path) -> dict[str, Any]:
    root = repo_dir.expanduser().resolve()
    test_dir_candidates = ("tests", "test", "spec", "__tests__")
    has_test_directory = any((root / name).is_dir() for name in test_dir_candidates)
    has_test_files = False
    file_globs = (
        "test_*.py",
        "*_test.py",
        "*.spec.ts",
        "*.test.ts",
        "*.spec.js",
        "*.test.js",
    )
    for pattern in file_globs:
        if any(root.glob(pattern)):
            has_test_files = True
            break
        tests_dir = root / "tests"
        if tests_dir.exists() and any(tests_dir.rglob(pattern)):
            has_test_files = True
            break
    dependency_files = (
        "pyproject.toml",
        "requirements.txt",
        "requirements-dev.txt",
        "setup.cfg",
        "setup.py",
        "Pipfile",
        "package.json",
        "pnpm-lock.yaml",
        "bun.lockb",
    )
    dependency_matches: list[str] = []
    scanned_dependency_files: list[str] = []
    for name in dependency_files:
        path = root / name
        text = _read_text_file(path)
        if not text:
            continue
        scanned_dependency_files.append(name)
        if contains_any_pattern(text, TEST_INFRA_DEPENDENCY_PATTERNS):
            dependency_matches.append(name)
    ci_workflows_dir = root / ".github" / "workflows"
    ci_workflows: list[str] = []
    ci_test_workflows: list[str] = []
    if ci_workflows_dir.exists():
        for path in sorted(ci_workflows_dir.glob("*.y*ml")):
            rel = path.relative_to(root).as_posix()
            ci_workflows.append(rel)
            text = _read_text_file(path)
            if text and contains_any_pattern(text, TEST_INFRA_WORKFLOW_PATTERNS):
                ci_test_workflows.append(rel)
    return {
        "has_test_directory": bool(has_test_directory),
        "has_test_files": bool(has_test_files),
        "has_test_dependencies": bool(dependency_matches),
        "has_test_ci_workflow": bool(ci_test_workflows),
        "scanned_dependency_files": scanned_dependency_files[:20],
        "test_dependency_matches": dedupe_strings(dependency_matches, limit=20),
        "ci_workflows": ci_workflows[:24],
        "ci_test_workflows": ci_test_workflows[:24],
    }


def _semantic_override_heuristic(
    *,
    run_state: RunState,
    test_signals: list[str],
    lint_signals: list[str],
    test_infra: dict[str, Any],
    diff_summary: dict[str, Any],
) -> dict[str, Any]:
    has_test_infra = bool(
        test_infra.get("has_test_directory")
        or test_infra.get("has_test_files")
        or test_infra.get("has_test_dependencies")
        or test_infra.get("has_test_ci_workflow")
    )
    has_alternative_validation = bool(lint_signals)
    changed_files_count = int(diff_summary.get("changed_files_count") or 0)
    added_lines = int(diff_summary.get("added_lines") or 0)
    low_risk_diff = changed_files_count <= 8 and added_lines <= 240
    pass_candidate = (
        run_state
        in {RunState.EXECUTING, RunState.ITERATING}
        and not has_test_infra
        and not test_signals
        and has_alternative_validation
        and low_risk_diff
    )
    return {
        "decision": "PASS" if pass_candidate else "NEEDS_REVIEW",
        "reason": (
            "no test infrastructure detected; alternative validation commands observed"
            if pass_candidate
            else "semantic conditions for no-test-infra pass not satisfied"
        ),
        "inputs": {
            "has_test_infrastructure": has_test_infra,
            "has_alternative_validation": has_alternative_validation,
            "low_risk_diff": low_risk_diff,
            "changed_files_count": changed_files_count,
            "added_lines": added_lines,
        },
    }


def _semantic_override_llm(
    *,
    evidence: dict[str, Any],
) -> dict[str, Any]:
    try:
        client = ManagerLLMClient.from_runtime(
            api_base=None,
            model=None,
            timeout_sec=20,
            api_key_env="AGENTPR_MANAGER_API_KEY",
        )
        grade = client.grade_worker_output(evidence=evidence)
    except ManagerLLMError as exc:
        return {
            "available": False,
            "decision": "NEEDS_REVIEW",
            "reason": f"manager llm unavailable: {exc}",
        }
    return {
        "available": True,
        "decision": grade.verdict,
        "reason": grade.reason,
        "confidence": grade.confidence,
    }


def apply_semantic_runtime_grading(
    *,
    run_state: RunState,
    runtime_grading_mode: str,
    rules_classification: dict[str, Any],
    test_signals: list[str],
    lint_signals: list[str],
    failed_test_commands: list[str],
    diff_summary: dict[str, Any],
    test_infra: dict[str, Any],
) -> tuple[dict[str, Any], dict[str, Any]]:
    normalized_mode = str(runtime_grading_mode or "hybrid").strip().lower()
    if normalized_mode not in {"rules", "hybrid", "hybrid_llm"}:
        normalized_mode = "hybrid"
    semantic: dict[str, Any] = {
        "enabled": normalized_mode != "rules",
        "mode": normalized_mode,
        "applied": False,
        "source": "none",
        "decision": "NEEDS_REVIEW",
        "reason": "",
    }
    if normalized_mode == "rules":
        semantic["reason"] = "semantic grading disabled (rules mode)"
        return rules_classification, semantic

    reason_code = str(rules_classification.get("reason_code") or "").strip().lower()
    if reason_code not in {"missing_test_evidence", "insufficient_test_evidence"}:
        semantic["reason"] = "rules classification does not require semantic override"
        return rules_classification, semantic

    evidence = {
        "run_state": run_state.value,
        "rules_classification": {
            "grade": str(rules_classification.get("grade") or ""),
            "reason_code": reason_code,
            "next_action": str(rules_classification.get("next_action") or ""),
        },
        "signals": {
            "test_commands": list(test_signals),
            "lint_or_validation_commands": list(lint_signals),
            "failed_test_commands": list(failed_test_commands),
            "diff": dict(diff_summary),
        },
        "test_infrastructure": dict(test_infra),
    }
    heuristic = _semantic_override_heuristic(
        run_state=run_state,
        test_signals=test_signals,
        lint_signals=lint_signals,
        test_infra=test_infra,
        diff_summary=diff_summary,
    )
    semantic["source"] = "heuristic"
    semantic["decision"] = str(heuristic.get("decision") or "NEEDS_REVIEW")
    semantic["reason"] = str(heuristic.get("reason") or "")
    semantic["heuristic"] = heuristic
    if normalized_mode == "hybrid_llm":
        llm = _semantic_override_llm(evidence=evidence)
        semantic["llm"] = llm
        if bool(llm.get("available")):
            semantic["source"] = "llm"
            semantic["decision"] = str(llm.get("decision") or "NEEDS_REVIEW")
            semantic["reason"] = str(llm.get("reason") or semantic["reason"])

    decision = str(semantic.get("decision") or "NEEDS_REVIEW").upper()
    if decision != "PASS":
        semantic["reason"] = str(semantic.get("reason") or "semantic review rejected override")
        return rules_classification, semantic

    semantic["applied"] = True
    upgraded = dict(rules_classification)
    upgraded["grade"] = AgentRuntimeGrade.PASS.value
    upgraded["reason_code"] = "runtime_success_no_test_infra_with_validation"
    upgraded["next_action"] = "advance"
    evidence_out = dict(upgraded.get("evidence") or {})
    evidence_out.update(
        {
            "semantic_mode": normalized_mode,
            "semantic_source": str(semantic.get("source") or ""),
            "semantic_reason": str(semantic.get("reason") or ""),
            "test_infrastructure": dict(test_infra),
            "lint_or_validation_commands": list(lint_signals)[:20],
            "test_commands": list(test_signals)[:20],
            "failed_test_commands": list(failed_test_commands)[:20],
        }
    )
    upgraded["evidence"] = evidence_out
    return upgraded, semantic


def extract_string_by_keys(
    payload: dict[str, Any],
    *,
    keys: set[str],
    max_nodes: int = 240,
) -> str | None:
    queue: list[Any] = [payload]
    seen_nodes = 0
    keyset = {key.strip().lower() for key in keys if key.strip()}
    while queue and seen_nodes < max_nodes:
        node = queue.pop(0)
        seen_nodes += 1
        if isinstance(node, dict):
            for key, value in node.items():
                normalized_key = str(key).strip().lower()
                if normalized_key in keyset and isinstance(value, str) and value.strip():
                    return value
                if isinstance(value, (dict, list)):
                    queue.append(value)
            continue
        if isinstance(node, list):
            for item in node:
                if isinstance(item, (dict, list)):
                    queue.append(item)
    return None


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


def top_frequency(values: list[str], *, limit: int) -> list[dict[str, Any]]:
    counts: dict[str, int] = {}
    for value in values:
        key = str(value).strip()
        if not key:
            continue
        counts[key] = counts.get(key, 0) + 1
    ranked = sorted(
        counts.items(),
        key=lambda item: item[1],
        reverse=True,
    )[: max(int(limit), 1)]
    return [{"value": item[0], "count": int(item[1])} for item in ranked]


def summarize_codex_event_stream(
    text: str,
    *,
    line_offsets_ms: list[int] | None = None,
) -> dict[str, Any]:
    lines = text.splitlines()
    parsed_event_count = 0
    parse_error_count = 0
    event_type_counts: dict[str, int] = {}
    command_event_total = 0
    command_events_sample: list[dict[str, Any]] = []
    command_text_raw: list[str] = []
    command_text_sample: list[str] = []
    skill_event_counts: dict[str, int] = {}
    command_started_at: dict[str, int] = {}
    command_durations: list[dict[str, Any]] = []
    usage: dict[str, int] = {}

    for line_idx, raw_line in enumerate(lines):
        line = raw_line.strip()
        if not line:
            continue
        try:
            payload = json.loads(line)
        except json.JSONDecodeError:
            parse_error_count += 1
            continue
        if not isinstance(payload, dict):
            parse_error_count += 1
            continue
        parsed_event_count += 1
        event_type = (
            str(payload.get("type") or payload.get("event") or payload.get("name") or "unknown")
            .strip()
            .lower()
        )
        local_offset_ms = (
            int(line_offsets_ms[line_idx])
            if isinstance(line_offsets_ms, list)
            and line_idx < len(line_offsets_ms)
            and isinstance(line_offsets_ms[line_idx], int)
            else None
        )
        event_type_counts[event_type] = event_type_counts.get(event_type, 0) + 1

        skill_name = extract_string_by_keys(payload, keys={"skill", "skill_name"})
        if skill_name:
            normalized = skill_name.strip()
            skill_event_counts[normalized] = skill_event_counts.get(normalized, 0) + 1

        if event_type == "turn.completed":
            usage_block = payload.get("usage")
            if isinstance(usage_block, dict):
                for key in (
                    "input_tokens",
                    "output_tokens",
                    "cached_input_tokens",
                    "reasoning_tokens",
                    "total_tokens",
                ):
                    parsed = parse_optional_int(usage_block.get(key))
                    if parsed is not None:
                        usage[key] = parsed

        command_text = None
        command_status = ""
        command_exit_code = None
        command_item_id = None
        item = payload.get("item")
        if (
            isinstance(item, dict)
            and str(item.get("type", "")).strip().lower() == "command_execution"
        ):
            command_item_id = str(item.get("id") or "").strip() or None
            command_text = str(item.get("command") or "").strip() or None
            command_status = str(item.get("status") or "").strip()
            command_exit_code = parse_optional_int(item.get("exit_code"))
        if not command_text:
            command_text = extract_string_by_keys(
                payload,
                keys={"command", "cmd", "shell_command", "bash_command"},
            )
        if not command_text:
            continue
        normalized_command = command_text.strip()
        if not normalized_command:
            continue
        command_event_total += 1
        command_text_raw.append(normalized_command)
        if len(command_text_sample) < 200:
            command_text_sample.append(normalized_command)
        duration_ms = None
        if event_type == "item.started" and command_item_id and local_offset_ms is not None:
            command_started_at[command_item_id] = local_offset_ms
        if event_type == "item.completed" and command_item_id:
            started_ms = command_started_at.pop(command_item_id, None)
            if started_ms is not None and local_offset_ms is not None:
                duration_ms = max(local_offset_ms - started_ms, 0)
                command_durations.append(
                    {
                        "command": normalized_command,
                        "duration_ms": duration_ms,
                        "status": command_status,
                        "exit_code": command_exit_code,
                        "item_id": command_item_id,
                    }
                )
        if len(command_events_sample) < 80:
            command_events_sample.append(
                {
                    "event_type": event_type,
                    "local_offset_ms": local_offset_ms,
                    "status": command_status,
                    "duration_ms": duration_ms,
                    "exit_code": command_exit_code,
                    "item_id": command_item_id,
                    "command": normalized_command,
                }
            )

    command_text_sample = dedupe_strings(command_text_sample, limit=200)
    top_commands_by_frequency = top_frequency(command_text_raw, limit=20)
    top_command_durations = sorted(
        command_durations,
        key=lambda row: int(row.get("duration_ms") or 0),
        reverse=True,
    )[:20]
    return {
        "jsonl_line_count": len(lines),
        "parsed_event_count": parsed_event_count,
        "parse_error_count": parse_error_count,
        "event_type_counts": event_type_counts,
        "command_event_count": command_event_total,
        "command_events_sample": command_events_sample,
        "command_text_sample": command_text_sample,
        "top_commands_by_frequency": top_commands_by_frequency,
        "top_commands_by_duration": top_command_durations,
        "skill_event_counts": skill_event_counts,
        "usage": usage,
    }


def extract_failed_test_commands(event_summary: dict[str, Any]) -> list[str]:
    raw_events = event_summary.get("command_events_sample")
    events = raw_events if isinstance(raw_events, list) else []
    patterns = (
        r"\bpytest\b",
        r"\btox\b",
        r"\bmake\s+test\b",
        r"\bmake\s+lint\b",
        r"\bbun\s+test\b",
        r"\bbun\s+run\s+typecheck\b",
        r"\bnpm\s+test\b",
        r"\bpnpm\s+test\b",
        r"\byarn\s+test\b",
        r"\bhatch\s+run\s+.*\btest\b",
    )
    failed: list[str] = []
    for row in events:
        if not isinstance(row, dict):
            continue
        command = str(row.get("command") or "").strip()
        if not command:
            continue
        exit_code = parse_optional_int(row.get("exit_code"))
        if exit_code is None or exit_code == 0:
            continue
        if not contains_any_pattern(command, patterns):
            continue
        failed.append(command)
    return dedupe_strings(failed, limit=40)


def match_allowlisted_test_failures(
    *,
    text: str,
    patterns: list[str],
) -> list[str]:
    matched: list[str] = []
    haystack = str(text or "")
    for pattern in patterns:
        token = str(pattern).strip()
        if not token:
            continue
        try:
            ok = re.search(token, haystack, flags=re.IGNORECASE) is not None
        except re.error:
            ok = token.lower() in haystack.lower()
        if ok:
            matched.append(token)
    return dedupe_strings(matched, limit=20)


def classify_agent_runtime(
    *,
    run_state: RunState,
    result: Any,
    preflight_report: dict[str, Any] | None,
    safety_violations: list[dict[str, str]],
    test_signals: list[str],
    failed_test_commands: list[str],
    git_signals: list[str],
    diff_summary: dict[str, Any],
    allow_agent_push: bool,
    max_changed_files: int,
    max_added_lines: int,
    max_retryable_attempts: int,
    min_test_commands: int,
    known_test_failure_allowlist: list[str],
    attempt_no: int,
) -> dict[str, Any]:
    if preflight_report is not None and not preflight_report.get("ok", True):
        failures = [str(item) for item in preflight_report.get("failures", [])]
        failure_text = "\n".join(failures)
        if contains_any_pattern(failure_text, RETRYABLE_FAILURE_PATTERNS):
            return apply_retryable_cap(
                {
                    "grade": AgentRuntimeGrade.RETRYABLE.value,
                    "reason_code": "preflight_transient_failure",
                    "next_action": "retry",
                    "evidence": {"failures": failures[:8]},
                },
                attempt_no=attempt_no,
                max_retryable_attempts=max_retryable_attempts,
            )
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
            "evidence": {"violations": safety_violations[:8]},
        }

    if not allow_agent_push and git_signals:
        return {
            "grade": AgentRuntimeGrade.HUMAN_REVIEW.value,
            "reason_code": "agent_push_disallowed",
            "next_action": "escalate",
            "evidence": {"git_commands": git_signals[:8]},
        }

    requires_test_evidence = run_state in {
        RunState.EXECUTING,
        RunState.ITERATING,
    }
    if result.exit_code == 0:
        allowlisted_failure_matches: list[str] = []
        recovered_failed_test_commands: list[str] = []
        if failed_test_commands:
            allowlisted_failure_matches = match_allowlisted_test_failures(
                text=f"{result.stderr}\n{result.stdout}",
                patterns=known_test_failure_allowlist,
            )
            if allowlisted_failure_matches:
                failed_test_commands = []
            else:
                # The run may converge after intermediate failures (e.g. retries / env fixes).
                # Keep this as evidence and let final exit_code + required test evidence decide.
                recovered_failed_test_commands = list(failed_test_commands)
                failed_test_commands = []
        required_tests = max(int(min_test_commands), 0) if requires_test_evidence else 0
        observed_tests = len(test_signals)
        if required_tests > 0 and observed_tests < required_tests:
            reason_code = (
                "missing_test_evidence"
                if observed_tests == 0 and required_tests == 1
                else "insufficient_test_evidence"
            )
            return {
                "grade": AgentRuntimeGrade.HUMAN_REVIEW.value,
                "reason_code": reason_code,
                "next_action": "escalate",
                "evidence": {
                    "expected_state": run_state.value,
                    "required_test_commands": required_tests,
                    "observed_test_commands": observed_tests,
                },
            }
        changed_files_count = int(diff_summary.get("changed_files_count", 0))
        added_lines = int(diff_summary.get("added_lines", 0))
        if (max_changed_files > 0 and changed_files_count > max_changed_files) or (
            max_added_lines > 0 and added_lines > max_added_lines
        ):
            return {
                "grade": AgentRuntimeGrade.HUMAN_REVIEW.value,
                "reason_code": "diff_budget_exceeded",
                "next_action": "escalate",
                "evidence": {
                    "changed_files_count": changed_files_count,
                    "added_lines": added_lines,
                    "max_changed_files": max_changed_files,
                    "max_added_lines": max_added_lines,
                    "changed_files": diff_summary.get("changed_files", [])[:16],
                },
            }
        return {
            "grade": AgentRuntimeGrade.PASS.value,
            "reason_code": (
                "runtime_success_allowlisted_test_failures"
                if allowlisted_failure_matches
                else (
                    "runtime_success_recovered_test_failures"
                    if recovered_failed_test_commands
                    else "runtime_success"
                )
            ),
            "next_action": "advance",
            "evidence": {
                "exit_code": result.exit_code,
                "test_commands": test_signals[:12],
                "changed_files_count": changed_files_count,
                "added_lines": added_lines,
                "allowlisted_test_failure_matches": allowlisted_failure_matches[:12],
                "recovered_failed_test_commands": recovered_failed_test_commands[:12],
                "recovered_failed_test_command_count": len(recovered_failed_test_commands),
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
        return apply_retryable_cap(
            {
                "grade": AgentRuntimeGrade.RETRYABLE.value,
                "reason_code": "runtime_transient_failure",
                "next_action": "retry",
                "evidence": {"exit_code": result.exit_code},
            },
            attempt_no=attempt_no,
            max_retryable_attempts=max_retryable_attempts,
        )

    return apply_retryable_cap(
        {
            "grade": AgentRuntimeGrade.RETRYABLE.value,
            "reason_code": "runtime_unknown_failure",
            "next_action": "retry",
            "evidence": {"exit_code": result.exit_code},
        },
        attempt_no=attempt_no,
        max_retryable_attempts=max_retryable_attempts,
    )


def apply_retryable_cap(
    classification: dict[str, Any],
    *,
    attempt_no: int,
    max_retryable_attempts: int,
) -> dict[str, Any]:
    if classification.get("grade") != AgentRuntimeGrade.RETRYABLE.value:
        return classification
    if max_retryable_attempts <= 0:
        return classification
    if attempt_no <= max_retryable_attempts:
        return classification
    evidence = dict(classification.get("evidence") or {})
    evidence.update(
        {
            "attempt_no": attempt_no,
            "max_retryable_attempts": max_retryable_attempts,
            "original_reason_code": classification.get("reason_code"),
        }
    )
    return {
        "grade": AgentRuntimeGrade.HUMAN_REVIEW.value,
        "reason_code": "retryable_limit_exceeded",
        "next_action": "escalate",
        "evidence": evidence,
    }


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
    diff_summary: dict[str, Any],
    allow_agent_push: bool,
    max_changed_files: int,
    max_added_lines: int,
    max_retryable_attempts: int,
    min_test_commands: int,
    runtime_grading_mode: str,
    known_test_failure_allowlist: list[str],
    attempt_no: int,
    skills_mode: str,
    skill_plan: dict[str, Any] | None,
    repo_dir: Path,
    task_packet_path: str | None,
    event_summary: dict[str, Any] | None,
    event_stream_path: str | None,
    last_message_path: str | None,
    manager_policy: dict[str, Any] | None,
) -> dict[str, Any]:
    resolved_event_summary = (
        event_summary if isinstance(event_summary, dict) else summarize_codex_event_stream(result.stdout)
    )
    command_candidates = [
        str(item).strip()
        for item in (resolved_event_summary.get("command_text_sample") or [])
        if str(item).strip()
    ]
    parsed_event_count = int(resolved_event_summary.get("parsed_event_count") or 0)
    if parsed_event_count > 0:
        commands = dedupe_strings(command_candidates)
    else:
        commands = dedupe_strings(
            [*command_candidates, *extract_shell_commands(f"{result.stdout}\n{result.stderr}")]
        )
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

    test_signals = detect_commands_by_patterns(
        commands=commands,
        patterns=TEST_COMMAND_PATTERNS,
        limit=40,
    )
    lint_signals = detect_commands_by_patterns(
        commands=commands,
        patterns=LINT_OR_VALIDATION_COMMAND_PATTERNS,
        limit=40,
    )
    git_signals = sorted(
        {
            command
            for command in commands
            for pattern in (r"\bgit\s+commit\b", r"\bgit\s+push\b", r"\bfinish\.sh\b")
            if re.search(pattern, command)
        }
    )
    command_categories = summarize_command_categories(commands)
    failed_test_commands = extract_failed_test_commands(resolved_event_summary)
    test_infra = scan_repo_test_infrastructure(repo_dir)
    rules_classification = classify_agent_runtime(
        run_state=run_state,
        result=result,
        preflight_report=preflight_report,
        safety_violations=violations,
        test_signals=test_signals,
        failed_test_commands=failed_test_commands,
        git_signals=git_signals,
        diff_summary=diff_summary,
        allow_agent_push=allow_agent_push,
        max_changed_files=max_changed_files,
        max_added_lines=max_added_lines,
        max_retryable_attempts=max_retryable_attempts,
        min_test_commands=min_test_commands,
        known_test_failure_allowlist=known_test_failure_allowlist,
        attempt_no=attempt_no,
    )
    classification, semantic_grading = apply_semantic_runtime_grading(
        run_state=run_state,
        runtime_grading_mode=runtime_grading_mode,
        rules_classification=rules_classification,
        test_signals=test_signals,
        lint_signals=lint_signals,
        failed_test_commands=failed_test_commands,
        diff_summary=diff_summary,
        test_infra=test_infra,
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
            "attempt_no": attempt_no,
            "max_retryable_attempts": max_retryable_attempts,
            "min_test_commands": min_test_commands,
            "runtime_grading_mode": runtime_grading_mode,
            "known_test_failure_allowlist": list(known_test_failure_allowlist),
            "skills_mode": skills_mode,
            "skill_plan": skill_plan,
            "task_packet_path": task_packet_path,
            "event_stream_path": event_stream_path,
            "last_message_path": last_message_path,
            "manager_policy": manager_policy,
        },
        "preflight": preflight_report,
        "signals": {
            "commands_sample": commands[:40],
            "command_sample_count": len(commands),
            "command_categories": command_categories,
            "test_commands": test_signals,
            "lint_or_validation_commands": lint_signals,
            "failed_test_commands": failed_test_commands,
            "git_commands": git_signals,
            "diff": diff_summary,
            "test_infrastructure": test_infra,
            "agent_event_summary": resolved_event_summary,
        },
        "safety": {
            "violations": violations,
            "violation_count": len(violations),
        },
        "semantic_grading": semantic_grading,
        "classification": classification,
    }


def write_agent_event_stream(run_id: str, stdout_text: str) -> Path:
    return _write_report(run_id, "agent_events", stdout_text, ext="jsonl")


def write_agent_runtime_report(run_id: str, payload: dict[str, Any]) -> Path:
    return _write_report(
        run_id, "agent_runtime",
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2),
    )


def percentile_ms(values: list[int], p: float) -> int:
    if not values:
        return 0
    ordered = sorted(int(v) for v in values)
    if len(ordered) == 1:
        return ordered[0]
    rank = max(0.0, min(1.0, p)) * (len(ordered) - 1)
    lo = int(rank)
    hi = min(lo + 1, len(ordered) - 1)
    if lo == hi:
        return ordered[lo]
    frac = rank - lo
    return int(ordered[lo] + (ordered[hi] - ordered[lo]) * frac)


def summarize_run_stage_snapshot(
    *,
    service: OrchestratorService,
    run_id: str,
    attempt_limit: int = 600,
) -> dict[str, Any]:
    attempts = service.list_step_attempts(run_id, limit=max(int(attempt_limit), 1))
    attempts = list(reversed(attempts))
    total_duration_ms = 0
    per_step: dict[str, dict[str, Any]] = {}
    for row in attempts:
        step = str(row.get("step") or "unknown")
        duration_ms = max(int(row.get("duration_ms") or 0), 0)
        total_duration_ms += duration_ms
        info = per_step.setdefault(
            step,
            {"attempts": 0, "total_duration_ms": 0, "durations_ms": [], "last_exit_code": 0},
        )
        info["attempts"] += 1
        info["total_duration_ms"] += duration_ms
        info["durations_ms"].append(duration_ms)
        info["last_exit_code"] = int(row.get("exit_code") or 0)

    step_totals: list[dict[str, Any]] = []
    for step, info in per_step.items():
        durations = [int(item) for item in info["durations_ms"]]
        total_ms = int(info["total_duration_ms"])
        step_totals.append(
            {
                "step": step,
                "attempts": int(info["attempts"]),
                "total_duration_ms": total_ms,
                "avg_duration_ms": int(total_ms / max(int(info["attempts"]), 1)),
                "p50_duration_ms": percentile_ms(durations, 0.50),
                "p90_duration_ms": percentile_ms(durations, 0.90),
                "last_exit_code": int(info["last_exit_code"]),
                "share_of_total_pct": round((100.0 * total_ms / total_duration_ms), 2)
                if total_duration_ms > 0
                else 0.0,
            }
        )
    step_totals.sort(key=lambda row: int(row["total_duration_ms"]), reverse=True)

    attempts_recent = [
        {
            "step": str(row.get("step") or ""),
            "attempt_no": int(row.get("attempt_no") or 0),
            "exit_code": int(row.get("exit_code") or 0),
            "duration_ms": max(int(row.get("duration_ms") or 0), 0),
            "created_at": str(row.get("created_at") or ""),
        }
        for row in attempts[-12:]
    ]
    top_step = step_totals[0]["step"] if step_totals else ""
    return {
        "step_attempt_count": len(attempts),
        "total_step_duration_ms": total_duration_ms,
        "top_step": top_step,
        "step_totals": step_totals,
        "attempts_recent": attempts_recent,
    }


def compute_count_shares(counts: dict[str, Any]) -> dict[str, float]:
    normalized: dict[str, int] = {}
    total = 0
    for key, value in counts.items():
        count = max(int(value or 0), 0)
        normalized[str(key)] = count
        total += count
    if total <= 0:
        return {key: 0.0 for key in normalized.keys()}
    return {key: round((100.0 * value / total), 2) for key, value in normalized.items()}


def derive_manager_recommendation(
    *,
    grade: str,
    reason_code: str,
    state_after: str,
) -> dict[str, Any]:
    normalized_grade = str(grade).upper()
    normalized_reason = str(reason_code).lower()
    if normalized_grade == AgentRuntimeGrade.PASS.value:
        action = "advance"
        if state_after == RunState.NEEDS_HUMAN_REVIEW.value:
            action = "human_review_gate"
        return {
            "action": action,
            "priority": "normal",
            "why": "runtime classified PASS",
        }
    if normalized_grade == AgentRuntimeGrade.RETRYABLE.value:
        return {
            "action": "retry",
            "priority": "high",
            "why": f"retryable failure: {normalized_reason}",
        }
    return {
        "action": "human_review",
        "priority": "high",
        "why": f"needs manual judgment: {normalized_reason}",
    }


def derive_iteration_hints(
    *,
    reason_code: str,
    test_command_count: int,
    failed_test_command_count: int,
    changed_files_count: int,
) -> list[str]:
    code = str(reason_code).strip().lower()
    hints: list[str] = []
    semantic_no_test_infra = (
        code == "runtime_success_no_test_infra_with_validation"
    )
    if (
        not semantic_no_test_infra
        and (
            code in {"missing_test_evidence", "insufficient_test_evidence"}
            or test_command_count == 0
        )
    ):
        hints.append(
            "Strengthen implement/validate prompt to require explicit CI-aligned test command execution and evidence."
        )
    if code == "test_command_failed" or failed_test_command_count > 0:
        hints.append(
            "Separate baseline failing tests from integration-scope failures and require worker to report failing command provenance."
        )
    if code == "diff_budget_exceeded" or changed_files_count > 8:
        hints.append(
            "Strengthen minimal-diff contract: limit touched files, prefer targeted patch over broad refactor."
        )
    if "transient" in code or "retryable" in code:
        hints.append(
            "Add retry policy hint: classify network/package flakiness separately from code-quality failures."
        )
    if "hard_failure" in code or "preflight" in code:
        hints.append(
            "Treat environment/tooling/auth issues as manager remediation tasks before retrying worker."
        )
    if not hints:
        hints.append(
            "Current run looks stable; prioritize preserving constraints and reducing unnecessary command volume."
        )
    return hints[:5]


def render_manager_insight_markdown(digest: dict[str, Any]) -> str:
    d = _safe_dict(digest)
    run = _safe_dict(d.get("run"))
    state = _safe_dict(d.get("state"))
    attempt = _safe_dict(d.get("attempt"))
    classification = _safe_dict(d.get("classification"))
    commands = _safe_dict(d.get("commands"))
    events = _safe_dict(d.get("events"))
    validation = _safe_dict(d.get("validation"))
    changes = _safe_dict(d.get("changes"))
    recommendation = _safe_dict(d.get("manager_recommendation"))
    usage = _safe_dict(d.get("usage"))
    stages = _safe_dict(d.get("stages"))
    stage_rows = list(stages.get("step_totals") or [])[:3]
    stage_lines: list[str] = []
    for row in stage_rows:
        if not isinstance(row, dict):
            continue
        step = str(row.get("step") or "").strip()
        if not step:
            continue
        share = row.get("share_of_total_pct")
        duration_ms = row.get("total_duration_ms")
        stage_lines.append(f"- {step}: {duration_ms}ms ({share}%)")
    if not stage_lines:
        stage_lines.append("- No stage timing data captured.")

    top_duration_rows = list(commands.get("top_by_duration") or [])[:3]
    top_duration_lines: list[str] = []
    for row in top_duration_rows:
        if not isinstance(row, dict):
            continue
        cmd = str(row.get("command") or "").strip()
        ms = row.get("duration_ms")
        if cmd:
            top_duration_lines.append(f"- {ms}ms | `{cmd}`")
    if not top_duration_lines:
        top_duration_lines.append("- No command duration data captured.")

    iteration_hints = derive_iteration_hints(
        reason_code=str(classification.get("reason_code") or ""),
        test_command_count=int(validation.get("test_command_count") or 0),
        failed_test_command_count=int(validation.get("failed_test_command_count") or 0),
        changed_files_count=int(changes.get("changed_files_count") or 0),
    )

    lines = [
        "# Manager Insight",
        "",
        f"- Run: `{run.get('run_id', '')}` ({run.get('owner', '')}/{run.get('repo', '')})",
        f"- Outcome: `{classification.get('grade', '')}` / `{classification.get('reason_code', '')}`",
        f"- State: `{state.get('before', '')}` -> `{state.get('after', '')}`",
        (
            f"- Attempt: `{attempt.get('attempt_no')}` | "
            f"Duration: `{attempt.get('duration_ms')}ms` | Exit: `{attempt.get('exit_code')}`"
        ),
        "",
        "## Evidence Snapshot",
        (
            f"- Parsed events: `{events.get('parsed_event_count')}` "
            f"(parse errors: `{events.get('parse_error_count')}`)"
        ),
        f"- Command events: `{events.get('command_event_count')}`",
        f"- Test commands: `{validation.get('test_command_count')}`",
        f"- Lint/typecheck commands: `{validation.get('lint_or_validation_command_count')}`",
        f"- Failed test commands: `{validation.get('failed_test_command_count')}`",
        (
            f"- Diff: files `{changes.get('changed_files_count')}` | "
            f"`+{changes.get('added_lines')} / -{changes.get('deleted_lines')}`"
        ),
        (
            f"- Tokens: input `{usage.get('input_tokens', 0)}` | "
            f"output `{usage.get('output_tokens', 0)}` | "
            f"cached `{usage.get('cached_input_tokens', 0)}`"
        ),
        "",
        "## Top Command Durations",
        *top_duration_lines,
        "",
        "## Stage Timeline Snapshot",
        *stage_lines,
        "",
        "## Suggested Next Action",
        f"- Action: `{recommendation.get('action', '')}`",
        f"- Priority: `{recommendation.get('priority', '')}`",
        f"- Why: {recommendation.get('why', '')}",
        "",
        "## Prompt/Skill Iteration Hints",
    ]
    lines.extend([f"- {item}" for item in iteration_hints])
    lines.append("")
    lines.append(f"_Generated at: {digest.get('generated_at', '')}_")
    return "\n".join(lines).strip() + "\n"


def build_run_digest(
    *,
    run: dict[str, Any],
    state_before: str,
    state_after: str,
    agent_report: dict[str, Any],
    agent_report_path: Path,
    event_stream_path: Path | None,
    last_message_path: Path | None,
    stage_summary: dict[str, Any] | None,
) -> dict[str, Any]:
    classification = _safe_dict(agent_report.get("classification"))
    runtime = _safe_dict(agent_report.get("runtime"))
    result = _safe_dict(agent_report.get("result"))
    signals = _safe_dict(agent_report.get("signals"))
    safety = _safe_dict(agent_report.get("safety"))
    semantic_grading = _safe_dict(agent_report.get("semantic_grading"))
    preflight = _safe_dict(agent_report.get("preflight"))
    event_summary = _safe_dict(signals.get("agent_event_summary"))
    skill_plan = _safe_dict(runtime.get("skill_plan"))
    usage = _safe_dict(event_summary.get("usage"))
    command_categories = _safe_dict(signals.get("command_categories"))
    category_share_pct = compute_count_shares(command_categories)
    resolved_stage_summary = _safe_dict(stage_summary)

    grade = str(classification.get("grade") or "UNKNOWN")
    reason_code = str(classification.get("reason_code") or "unknown")
    next_action = str(classification.get("next_action") or "")
    recommendation = derive_manager_recommendation(
        grade=grade,
        reason_code=reason_code,
        state_after=state_after,
    )

    return {
        "run": {
            "run_id": str(run.get("run_id") or ""),
            "owner": str(run.get("owner") or ""),
            "repo": str(run.get("repo") or ""),
            "mode": str(run.get("mode") or ""),
            "prompt_version": str(run.get("prompt_version") or ""),
            "workspace_dir": str(run.get("workspace_dir") or ""),
        },
        "state": {
            "before": str(state_before),
            "after": str(state_after),
        },
        "attempt": {
            "attempt_no": runtime.get("attempt_no"),
            "duration_ms": int(result.get("duration_ms") or 0),
            "exit_code": int(result.get("exit_code") or 0),
        },
        "classification": {
            "grade": grade,
            "reason_code": reason_code,
            "next_action": next_action,
            "evidence": classification.get("evidence"),
            "semantic": semantic_grading,
        },
        "skills": {
            "mode": str(runtime.get("skills_mode") or "off"),
            "required_now": list(skill_plan.get("required_now") or []),
            "missing_required": list(skill_plan.get("missing_required") or []),
            "available_optional": list(skill_plan.get("available_optional") or []),
        },
        "validation": {
            "test_command_count": len(list(signals.get("test_commands") or [])),
            "test_commands": list(signals.get("test_commands") or []),
            "lint_or_validation_command_count": len(
                list(signals.get("lint_or_validation_commands") or [])
            ),
            "lint_or_validation_commands": list(
                signals.get("lint_or_validation_commands") or []
            ),
            "failed_test_command_count": len(list(signals.get("failed_test_commands") or [])),
            "failed_test_commands": list(signals.get("failed_test_commands") or []),
        },
        "changes": signals.get("diff"),
        "commands": {
            "sample_count": int(signals.get("command_sample_count") or 0),
            "sample": list(signals.get("commands_sample") or []),
            "categories": command_categories,
            "category_share_pct": category_share_pct,
            "top_by_duration": list(event_summary.get("top_commands_by_duration") or []),
            "top_by_frequency": list(event_summary.get("top_commands_by_frequency") or []),
        },
        "stages": resolved_stage_summary,
        "events": {
            "parsed_event_count": int(event_summary.get("parsed_event_count") or 0),
            "parse_error_count": int(event_summary.get("parse_error_count") or 0),
            "event_type_counts": event_summary.get("event_type_counts") or {},
            "command_event_count": int(event_summary.get("command_event_count") or 0),
        },
        "usage": usage,
        "safety": {
            "violation_count": int(safety.get("violation_count") or 0),
            "violations": list(safety.get("violations") or []),
        },
        "preflight": {
            "ok": bool(preflight.get("ok", True)),
            "failures": list(preflight.get("failures") or []),
        },
        "manager_recommendation": recommendation,
        "artifacts": {
            "agent_report_path": str(agent_report_path),
            "event_stream_path": str(event_stream_path) if event_stream_path else "",
            "last_message_path": str(last_message_path) if last_message_path else "",
        },
        "generated_at": datetime.now(UTC).isoformat(),
    }


def write_run_digest(run_id: str, payload: dict[str, Any]) -> Path:
    return _write_report(
        run_id, "run_digest",
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2),
    )


def write_manager_insight(run_id: str, markdown: str) -> Path:
    return _write_report(run_id, "manager_insight", markdown, ext="md")


def persist_run_analysis_artifacts(
    *,
    service: OrchestratorService,
    run: dict[str, Any],
    state_before: str,
    state_after: str,
    agent_report: dict[str, Any],
    agent_report_path: Path,
    event_stream_path: Path | None,
    last_message_path: Path | None,
) -> dict[str, str]:
    run_id = str(run["run_id"])
    stage_summary = summarize_run_stage_snapshot(service=service, run_id=run_id)
    digest = build_run_digest(
        run=run,
        state_before=state_before,
        state_after=state_after,
        agent_report=agent_report,
        agent_report_path=agent_report_path,
        event_stream_path=event_stream_path,
        last_message_path=last_message_path,
        stage_summary=stage_summary,
    )
    digest_path = write_run_digest(run_id, digest)
    service.add_artifact(
        run_id,
        artifact_type="run_digest",
        uri=str(digest_path),
        metadata={
            "grade": str(digest["classification"]["grade"]),
            "reason_code": str(digest["classification"]["reason_code"]),
            "state_after": str(digest["state"]["after"]),
            "attempt_no": digest["attempt"]["attempt_no"],
        },
    )

    insight_text = render_manager_insight_markdown(digest)
    insight_path = write_manager_insight(run_id, insight_text)
    service.add_artifact(
        run_id,
        artifact_type="manager_insight",
        uri=str(insight_path),
        metadata={
            "grade": str(digest["classification"]["grade"]),
            "reason_code": str(digest["classification"]["reason_code"]),
            "state_after": str(digest["state"]["after"]),
        },
    )
    return {
        "run_digest": str(digest_path),
        "manager_insight": str(insight_path),
    }


def should_persist_agent_event_stream(
    *,
    grade: str,
    run_id: str,
    attempt_no: int,
    success_sample_pct: int,
) -> tuple[bool, str]:
    normalized_grade = str(grade).upper()
    if normalized_grade != AgentRuntimeGrade.PASS.value:
        return True, "non_pass"
    pct = min(max(int(success_sample_pct), 0), 100)
    if pct >= 100:
        return True, "success_sample_always"
    if pct <= 0:
        return False, "success_sample_disabled"
    digest = hashlib.sha1(f"{run_id}:{attempt_no}".encode("utf-8")).hexdigest()
    bucket = int(digest[:8], 16) % 100
    keep = bucket < pct
    return keep, f"success_sample_{pct}pct"


def load_digest_artifact_payload(
    service: OrchestratorService,
    *,
    run_id: str,
) -> tuple[dict[str, Any] | None, str]:
    artifact = service.latest_artifact(run_id, artifact_type="run_digest")
    if artifact is None:
        return None, "missing_run_digest_artifact"
    uri = str(artifact.get("uri") or "").strip()
    if not uri:
        return None, "empty_run_digest_uri"
    path = Path(uri)
    if not path.exists():
        return None, "run_digest_file_not_found"
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None, "run_digest_unreadable"
    if not isinstance(payload, dict):
        return None, "run_digest_invalid_payload"
    return payload, ""


def evaluate_pr_gate_readiness(
    *,
    digest: dict[str, Any] | None,
    expected_policy: dict[str, Any],
    contract_available: bool,
) -> dict[str, Any]:
    failed_checks: list[dict[str, str]] = []
    warnings: list[dict[str, str]] = []
    accepted_runtime_reason_codes = {
        "runtime_success",
        "runtime_success_allowlisted_test_failures",
        "runtime_success_recovered_test_failures",
        "runtime_success_no_test_infra_with_validation",
    }
    if not contract_available:
        failed_checks.append(
            {"code": "missing_contract", "message": "contract artifact is required for PR gate"}
        )
    if not isinstance(digest, dict):
        failed_checks.append(
            {"code": "missing_digest", "message": "latest run_digest is required for PR gate"}
        )
        return {"ok": False, "failed_checks": failed_checks}

    classification = _safe_dict(digest.get("classification"))
    grade = str(classification.get("grade") or "")
    reason_code = str(classification.get("reason_code") or "")
    if grade != AgentRuntimeGrade.PASS.value:
        failed_checks.append(
            {"code": "runtime_not_pass", "message": f"classification grade={grade}"}
        )
    if reason_code not in accepted_runtime_reason_codes:
        failed_checks.append(
            {
                "code": "runtime_not_runtime_success",
                "message": f"classification reason_code={reason_code}",
            }
        )

    preflight = _safe_dict(digest.get("preflight"))
    if not bool(preflight.get("ok", False)):
        failed_checks.append(
            {
                "code": "preflight_not_ok",
                "message": "preflight must be ok in latest run_digest",
            }
        )

    safety = _safe_dict(digest.get("safety"))
    safety_count = int(safety.get("violation_count") or 0)
    if safety_count > 0:
        failed_checks.append(
            {
                "code": "safety_violation_present",
                "message": f"violation_count={safety_count}",
            }
        )

    validation = _safe_dict(digest.get("validation"))
    required_tests = max(int(expected_policy.get("min_test_commands") or 0), 0)
    runtime_grading_mode = str(
        expected_policy.get("runtime_grading_mode") or "hybrid"
    ).strip()
    test_count = int(validation.get("test_command_count") or 0)
    failed_test_count = int(validation.get("failed_test_command_count") or 0)
    no_test_infra_semantic_pass = (
        grade == AgentRuntimeGrade.PASS.value
        and reason_code == "runtime_success_no_test_infra_with_validation"
    )
    if required_tests > 0 and test_count < required_tests:
        if no_test_infra_semantic_pass:
            warnings.append(
                {
                    "code": "semantic_no_test_infra_override",
                    "message": (
                        f"required={required_tests}, observed={test_count}, "
                        f"runtime_grading_mode={runtime_grading_mode}"
                    ),
                }
            )
        else:
            failed_checks.append(
                {
                    "code": "insufficient_test_evidence",
                    "message": f"required={required_tests}, observed={test_count}",
                }
            )
    if failed_test_count > 0:
        if grade == AgentRuntimeGrade.PASS.value and reason_code in accepted_runtime_reason_codes:
            warnings.append(
                {
                    "code": "failed_test_commands_observed_but_converged",
                    "message": f"failed_test_command_count={failed_test_count}",
                }
            )
        else:
            failed_checks.append(
                {
                    "code": "failed_test_commands_present",
                    "message": f"failed_test_command_count={failed_test_count}",
                }
            )

    changes = _safe_dict(digest.get("changes"))
    changed_files = int(changes.get("changed_files_count") or 0)
    added_lines = int(changes.get("added_lines") or 0)
    max_changed_files = max(int(expected_policy.get("max_changed_files") or 0), 0)
    max_added_lines = max(int(expected_policy.get("max_added_lines") or 0), 0)
    if max_changed_files > 0 and changed_files > max_changed_files:
        failed_checks.append(
            {
                "code": "changed_files_budget_exceeded",
                "message": f"max={max_changed_files}, observed={changed_files}",
            }
        )
    if max_added_lines > 0 and added_lines > max_added_lines:
        failed_checks.append(
            {
                "code": "added_lines_budget_exceeded",
                "message": f"max={max_added_lines}, observed={added_lines}",
            }
        )

    expected_mode = str(expected_policy.get("skills_mode") or "").strip()
    skills = _safe_dict(digest.get("skills"))
    actual_mode = str(skills.get("mode") or "").strip()
    missing_required = list(skills.get("missing_required") or [])
    if expected_mode and actual_mode and expected_mode != actual_mode:
        failed_checks.append(
            {
                "code": "skills_mode_mismatch",
                "message": f"expected={expected_mode}, observed={actual_mode}",
            }
        )
    if actual_mode in {"agentpr", "agentpr_autonomous"} and missing_required:
        failed_checks.append(
            {
                "code": "missing_required_skills",
                "message": ", ".join(str(item) for item in missing_required[:5]),
            }
        )

    return {
        "ok": len(failed_checks) == 0,
        "failed_checks": failed_checks,
        "warnings": warnings,
        "snapshot": {
            "grade": grade,
            "reason_code": reason_code,
            "test_command_count": test_count,
            "failed_test_command_count": failed_test_count,
            "changed_files_count": changed_files,
            "added_lines": added_lines,
            "skills_mode": actual_mode,
            "runtime_grading_mode": runtime_grading_mode,
        },
    }
