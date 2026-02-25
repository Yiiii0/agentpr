from __future__ import annotations

import json
import os
import shutil
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .models import RunState

AGENTPR_SKILL_REPO_PREFLIGHT = "agentpr-repo-preflight-contract"
AGENTPR_SKILL_IMPLEMENT_VALIDATE = "agentpr-implement-and-validate"
AGENTPR_SKILL_CI_REVIEW_FIX = "agentpr-ci-review-fix"

AGENTPR_REQUIRED_SKILLS: tuple[str, ...] = (
    AGENTPR_SKILL_REPO_PREFLIGHT,
    AGENTPR_SKILL_IMPLEMENT_VALIDATE,
    AGENTPR_SKILL_CI_REVIEW_FIX,
)

OPTIONAL_CURATED_CI_SKILLS: tuple[str, ...] = (
    "gh-fix-ci",
    "gh-address-comments",
)

STAGE_SKILLS: dict[RunState, tuple[str, ...]] = {
    RunState.DISCOVERY: (AGENTPR_SKILL_REPO_PREFLIGHT,),
    RunState.PLAN_READY: (AGENTPR_SKILL_REPO_PREFLIGHT,),
    RunState.IMPLEMENTING: (AGENTPR_SKILL_IMPLEMENT_VALIDATE,),
    RunState.LOCAL_VALIDATING: (AGENTPR_SKILL_IMPLEMENT_VALIDATE,),
    RunState.ITERATING: (AGENTPR_SKILL_CI_REVIEW_FIX,),
    RunState.CI_WAIT: (AGENTPR_SKILL_CI_REVIEW_FIX,),
    RunState.REVIEW_WAIT: (AGENTPR_SKILL_CI_REVIEW_FIX,),
}


@dataclass(frozen=True)
class SkillPlan:
    mode: str
    run_state: RunState
    required_now: tuple[str, ...]
    optional_now: tuple[str, ...]
    missing_required: tuple[str, ...]
    available_optional: tuple[str, ...]
    missing_optional: tuple[str, ...]
    skills_root: Path

    def to_dict(self) -> dict[str, Any]:
        return {
            "mode": self.mode,
            "run_state": self.run_state.value,
            "required_now": list(self.required_now),
            "optional_now": list(self.optional_now),
            "missing_required": list(self.missing_required),
            "available_optional": list(self.available_optional),
            "missing_optional": list(self.missing_optional),
            "skills_root": str(self.skills_root),
        }


def resolve_codex_home() -> Path:
    env_home = str(os.environ.get("CODEX_HOME", "")).strip()
    if env_home:
        return Path(env_home).expanduser().resolve()
    return (Path.home() / ".codex").resolve()


def resolve_codex_skills_root(codex_home: Path | None = None) -> Path:
    home = codex_home or resolve_codex_home()
    return home / "skills"


def discover_installed_skills(*, skills_root: Path | None = None) -> set[str]:
    root = skills_root or resolve_codex_skills_root()
    if not root.exists() or not root.is_dir():
        return set()
    names: set[str] = set()
    for child in root.iterdir():
        if not child.is_dir():
            continue
        if child.name.startswith("."):
            continue
        if (child / "SKILL.md").exists():
            names.add(child.name)
    return names


def build_skill_plan(
    *,
    run_state: RunState,
    mode: str,
    installed_skills: set[str],
    skills_root: Path | None = None,
) -> SkillPlan:
    if mode != "agentpr":
        return SkillPlan(
            mode=mode,
            run_state=run_state,
            required_now=tuple(),
            optional_now=tuple(),
            missing_required=tuple(),
            available_optional=tuple(),
            missing_optional=tuple(),
            skills_root=skills_root or resolve_codex_skills_root(),
        )

    required_now = STAGE_SKILLS.get(run_state, (AGENTPR_SKILL_IMPLEMENT_VALIDATE,))
    optional_now: tuple[str, ...] = (
        OPTIONAL_CURATED_CI_SKILLS if run_state in {RunState.ITERATING, RunState.CI_WAIT, RunState.REVIEW_WAIT} else tuple()
    )

    missing_required = tuple(name for name in required_now if name not in installed_skills)
    available_optional = tuple(name for name in optional_now if name in installed_skills)
    missing_optional = tuple(name for name in optional_now if name not in installed_skills)

    return SkillPlan(
        mode=mode,
        run_state=run_state,
        required_now=required_now,
        optional_now=optional_now,
        missing_required=missing_required,
        available_optional=available_optional,
        missing_optional=missing_optional,
        skills_root=skills_root or resolve_codex_skills_root(),
    )


def render_skill_chain_prompt(
    *,
    base_prompt: str,
    task_packet: dict[str, Any],
    plan: SkillPlan,
) -> str:
    if plan.mode != "agentpr":
        return base_prompt

    required_lines = [
        f"{idx + 1}. ${name}"
        for idx, name in enumerate(plan.required_now)
    ]
    optional_lines = [f"- ${name}" for name in plan.available_optional]

    optional_block = ""
    if optional_lines:
        optional_block = (
            "\nOptional helper skills currently installed (use when helpful):\n"
            + "\n".join(optional_lines)
            + "\n"
        )

    task_packet_json = json.dumps(task_packet, ensure_ascii=True, sort_keys=True, indent=2)

    return (
        "AgentPR manager instruction: skills-mode is enabled.\n"
        "Execute one stage for the current run state using the required skill(s) below.\n"
        "Do not skip required skill(s).\n"
        "\n"
        "Required skill invocation order for this stage:\n"
        f"{chr(10).join(required_lines)}\n"
        f"{optional_block}"
        "Stage execution rules:\n"
        "- Keep minimal diff and follow repository contribution rules.\n"
        "- Run required install/test/lint commands exactly as repo docs/CI require.\n"
        "- If blocked by environment or policy, stop and report NEEDS REVIEW with concrete evidence.\n"
        "- Respect manager push policy from task packet.\n"
        "\n"
        "Task packet (JSON):\n"
        "```json\n"
        f"{task_packet_json}\n"
        "```\n"
        "\n"
        "Base integration prompt (must still be followed):\n"
        "---\n"
        f"{base_prompt.strip()}\n"
    )


def build_task_packet(
    *,
    run: dict[str, Any],
    run_state: RunState,
    repo_dir: Path,
    contract_uri: str | None,
    contract_source_uri: str | None = None,
    contract_text: str | None = None,
    codex_sandbox: str,
    allow_agent_push: bool,
    max_changed_files: int,
    max_added_lines: int,
    integration_root: Path,
    skill_plan: SkillPlan,
    user_packet: Any | None = None,
) -> dict[str, Any]:
    packet: dict[str, Any] = {
        "version": "agentpr.task_packet.v1",
        "created_at": datetime.now(UTC).isoformat(),
        "run": {
            "run_id": str(run.get("run_id", "")),
            "owner": str(run.get("owner", "")),
            "repo": str(run.get("repo", "")),
            "prompt_version": str(run.get("prompt_version", "")),
            "mode": str(run.get("mode", "")),
            "state": run_state.value,
        },
        "repo": {
            "path": str(repo_dir),
        },
        "policy": {
            "codex_sandbox": codex_sandbox,
            "allow_agent_push": bool(allow_agent_push),
            "max_changed_files": int(max_changed_files),
            "max_added_lines": int(max_added_lines),
        },
        "artifacts": {
            "contract_uri": contract_uri,
            "contract_source_uri": contract_source_uri,
            "contract_text": contract_text,
        },
        "docs": {
            "workflow": str(integration_root / "workflow.md"),
            "prompt_template": str(integration_root / "prompt_template.md"),
            "pr_template": str(integration_root / "pr_description_template.md"),
        },
        "skills": skill_plan.to_dict(),
        "required_output": {
            "status": "PASS | NEEDS REVIEW | FAIL | SKIP",
            "files_changed": "list of changed files",
            "test_results": "exact commands and outcomes",
            "notes": "risks/blockers and next action",
        },
    }
    if user_packet is not None:
        packet["user_packet"] = user_packet
    return packet


def load_user_task_packet(path: Path) -> Any:
    raw = path.read_text(encoding="utf-8")
    stripped = raw.strip()
    if not stripped:
        return {}
    if path.suffix.lower() == ".json":
        return json.loads(stripped)
    try:
        return json.loads(stripped)
    except json.JSONDecodeError:
        return {"text": raw}


def list_local_skill_dirs(*, source_root: Path) -> list[Path]:
    if not source_root.exists() or not source_root.is_dir():
        return []
    out: list[Path] = []
    for child in sorted(source_root.iterdir()):
        if not child.is_dir():
            continue
        if (child / "SKILL.md").exists():
            out.append(child)
    return out


def install_local_skills(
    *,
    source_root: Path,
    skills_root: Path,
    names: list[str] | None = None,
    force: bool = False,
) -> list[dict[str, Any]]:
    source_dirs = {path.name: path for path in list_local_skill_dirs(source_root=source_root)}
    selected_names = names or sorted(source_dirs.keys())
    if not selected_names:
        return []

    skills_root.mkdir(parents=True, exist_ok=True)
    results: list[dict[str, Any]] = []

    for name in selected_names:
        src = source_dirs.get(name)
        if src is None:
            results.append(
                {
                    "name": name,
                    "status": "missing_source",
                    "source": None,
                    "dest": str(skills_root / name),
                }
            )
            continue

        dest = skills_root / name
        if dest.exists():
            if not force:
                results.append(
                    {
                        "name": name,
                        "status": "already_exists",
                        "source": str(src),
                        "dest": str(dest),
                    }
                )
                continue
            shutil.rmtree(dest)

        shutil.copytree(src, dest)
        results.append(
            {
                "name": name,
                "status": "installed",
                "source": str(src),
                "dest": str(dest),
            }
        )

    return results
