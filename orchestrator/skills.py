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
AGENTPR_SKILLS_MODES: set[str] = {"agentpr", "agentpr_autonomous"}

OPTIONAL_CURATED_CI_SKILLS: tuple[str, ...] = (
    "gh-fix-ci",
    "gh-address-comments",
)

STAGE_SKILLS: dict[RunState, tuple[str, ...]] = {
    RunState.EXECUTING: (
        AGENTPR_SKILL_REPO_PREFLIGHT,
        AGENTPR_SKILL_IMPLEMENT_VALIDATE,
    ),
    RunState.ITERATING: (AGENTPR_SKILL_CI_REVIEW_FIX,),
    RunState.CI_WAIT: (AGENTPR_SKILL_CI_REVIEW_FIX,),
    RunState.REVIEW_WAIT: (AGENTPR_SKILL_CI_REVIEW_FIX,),
}

GOVERNANCE_SCAN_SKIP_DIRS: set[str] = {
    ".git",
    ".agentpr_runtime",
    ".venv",
    ".tox",
    "node_modules",
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".idea",
    ".vscode",
    ".next",
    ".nuxt",
    "dist",
    "build",
}

GOVERNANCE_PRIMARY_ROOTS: set[str] = {".github", "docs"}

GOVERNANCE_TEXT_EXTENSIONS: set[str] = {
    ".md",
    ".markdown",
    ".mdx",
    ".rst",
    ".txt",
    ".adoc",
}

DEV_SETUP_PREFIXES: tuple[str, ...] = (
    "develop",
    "development",
    "setup",
    "hacking",
    "install",
)

GOVERNANCE_SCAN_GROUPS: tuple[str, ...] = (
    "agents",
    "contributing",
    "pr_templates",
    "ci_workflows",
    "readme",
    "dev_setup",
    "codeowners",
    "code_of_conduct",
    "style_configs",
    "governance_candidates",
)

SECONDARY_GOVERNANCE_SEARCH_HINTS: tuple[str, ...] = (
    "rg --files --hidden -g 'CONTRIBUT*' -g 'CODE_OF_CONDUCT*' -g 'CODEOWNERS' -g 'AGENTS.md'",
    "rg --files --hidden -g '.github/PULL_REQUEST_TEMPLATE*' -g '.github/pull_request_template*' -g '.github/PULL_REQUEST_TEMPLATE/**' -g '.github/pull_request_template/**'",
    "find .github -maxdepth 4 -type f \\( -iname '*pull_request_template*' -o -iname '*pr_template*' \\)",
    "rg --files --hidden -g 'README*' -g 'DEVELOP*' -g 'DEVELOPMENT*' -g 'SETUP*' -g 'HACKING*' -g 'INSTALL*' -g 'docs/**'",
    "rg -n --hidden -i 'contribut|development|setup|testing|pull request|pr template' README* docs/**",
)

KEYWORD_GOVERNANCE_TOKENS: tuple[str, ...] = (
    "contrib",
    "govern",
    "maintain",
    "develop",
    "setup",
    "install",
    "workflow",
    "testing",
    "test",
    "pull_request",
    "pr_template",
)

PR_TEMPLATE_PREFERRED_PATHS: tuple[str, ...] = (
    ".github/pull_request_template.md",
    ".github/pull_request_template.txt",
    ".github/pull_request_template.rst",
    ".github/PULL_REQUEST_TEMPLATE.md",
    ".github/PULL_REQUEST_TEMPLATE.txt",
    ".github/PULL_REQUEST_TEMPLATE.rst",
)


@dataclass(frozen=True)
class SkillPlan:
    mode: str
    run_state: RunState
    control_model: str
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
            "control_model": self.control_model,
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


def _dedupe_path_list(values: list[str], *, limit: int) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for raw in values:
        item = str(raw).strip().replace("\\", "/")
        if not item or item in seen:
            continue
        seen.add(item)
        out.append(item)
        if len(out) >= max(int(limit), 1):
            break
    return out


def _governance_scope_rank(relative_path: Path) -> int:
    parts = [part.lower() for part in relative_path.parts]
    if not parts:
        return 99
    if len(parts) == 1:
        return 0
    first = parts[0]
    if first == ".github":
        return 1
    if first == "docs":
        return 2
    return 3


def _is_primary_governance_scope(relative_path: Path) -> bool:
    parts = [part.lower() for part in relative_path.parts]
    if not parts:
        return False
    if len(parts) == 1:
        return True
    return parts[0] in GOVERNANCE_PRIMARY_ROOTS


def _has_governance_text_extension(filename: str) -> bool:
    suffix = Path(filename).suffix.lower()
    if suffix in GOVERNANCE_TEXT_EXTENSIONS:
        return True
    # Allow extension-less governance docs (for example `README` or `INSTALL`).
    return "." not in filename


def _is_dev_setup_doc(filename_lower: str) -> bool:
    if not filename_lower.startswith(DEV_SETUP_PREFIXES):
        return False
    return _has_governance_text_extension(filename_lower)


def _governance_path_sort_key(path: str) -> tuple[int, int, str]:
    normalized = str(path).strip().replace("\\", "/")
    rel_path = Path(normalized)
    return (
        _governance_scope_rank(rel_path),
        len(rel_path.parts),
        normalized.lower(),
    )


def _normalize_governance_group(values: list[str], *, limit: int) -> list[str]:
    ordered = sorted(values, key=_governance_path_sort_key)
    return _dedupe_path_list(ordered, limit=limit)


def select_primary_pr_template(pr_templates: list[str]) -> str | None:
    deduped = _dedupe_path_list(pr_templates, limit=80)
    if not deduped:
        return None
    lower_to_original = {item.lower(): item for item in deduped}
    for preferred in PR_TEMPLATE_PREFERRED_PATHS:
        found = lower_to_original.get(preferred.lower())
        if found:
            return found

    def sort_key(item: str) -> tuple[int, str]:
        lower = item.lower()
        if lower.startswith(".github/pull_request_template/"):
            return (1, lower)
        if lower.startswith(".github/") and "pull_request_template" in lower:
            return (2, lower)
        if "pull_request_template" in lower:
            return (3, lower)
        if "pr_template" in lower:
            return (4, lower)
        return (5, lower)

    return sorted(deduped, key=sort_key)[0]


def scan_repo_governance_sources(
    *,
    repo_dir: Path,
    max_files_scanned: int = 20000,
    max_paths_per_group: int = 60,
) -> dict[str, Any]:
    root = repo_dir.expanduser().resolve()
    groups: dict[str, list[str]] = {name: [] for name in GOVERNANCE_SCAN_GROUPS}
    scanned_files = 0
    truncated = False

    for walk_root, dirnames, filenames in os.walk(root):
        dirnames[:] = [
            name
            for name in sorted(dirnames)
            if name not in GOVERNANCE_SCAN_SKIP_DIRS
        ]
        base = Path(walk_root)
        for filename in sorted(filenames):
            scanned_files += 1
            if scanned_files > max(int(max_files_scanned), 1):
                truncated = True
                break
            absolute_path = base / filename
            try:
                rel_path = absolute_path.relative_to(root)
            except ValueError:
                continue
            rel = rel_path.as_posix()
            rel_lower = rel.lower()
            name_lower = filename.lower()
            in_primary_scope = _is_primary_governance_scope(rel_path)

            if name_lower == "agents.md" and in_primary_scope:
                groups["agents"].append(rel)
            if (
                in_primary_scope
                and name_lower.startswith("contributing")
                and _has_governance_text_extension(filename)
            ):
                groups["contributing"].append(rel)
            if name_lower in {"codeowners"} and in_primary_scope:
                groups["codeowners"].append(rel)
            if (
                in_primary_scope
                and name_lower.startswith("code_of_conduct")
                and _has_governance_text_extension(filename)
            ):
                groups["code_of_conduct"].append(rel)
            if (
                in_primary_scope
                and _has_governance_text_extension(filename)
                and (
                    "pull_request_template" in rel_lower
                    or "pr_template" in rel_lower
                )
            ):
                groups["pr_templates"].append(rel)
            if rel_lower.startswith(".github/workflows/") and (
                name_lower.endswith(".yml") or name_lower.endswith(".yaml")
            ):
                groups["ci_workflows"].append(rel)
            if (
                in_primary_scope
                and name_lower.startswith("readme")
                and _has_governance_text_extension(filename)
            ):
                groups["readme"].append(rel)
            if in_primary_scope and _is_dev_setup_doc(name_lower):
                groups["dev_setup"].append(rel)
            if (
                in_primary_scope
                and (
                    name_lower == ".editorconfig"
                    or name_lower == "ruff.toml"
                    or name_lower.startswith(".eslintrc")
                    or name_lower.startswith(".prettierrc")
                )
            ):
                groups["style_configs"].append(rel)
            if (
                in_primary_scope
                and _has_governance_text_extension(filename)
                and any(token in name_lower for token in KEYWORD_GOVERNANCE_TOKENS)
            ):
                groups["governance_candidates"].append(rel)
        if truncated:
            break

    normalized_groups = {
        key: _normalize_governance_group(values, limit=max_paths_per_group)
        for key, values in groups.items()
    }
    primary_pr_template = select_primary_pr_template(
        normalized_groups["pr_templates"]
    )
    missing_expected_groups: list[str] = []
    if not normalized_groups["contributing"]:
        missing_expected_groups.append("contributing")
    if not normalized_groups["pr_templates"]:
        missing_expected_groups.append("pr_templates")
    if not normalized_groups["ci_workflows"]:
        missing_expected_groups.append("ci_workflows")
    if not normalized_groups["readme"]:
        missing_expected_groups.append("readme")

    return {
        "scan_version": "agentpr.governance_scan.v1",
        "generated_at": datetime.now(UTC).isoformat(),
        "repo_root": str(root),
        "scanned_files": scanned_files,
        "truncated": truncated,
        "groups": normalized_groups,
        "primary_pr_template": primary_pr_template,
        "coverage": {
            "missing_expected_groups": missing_expected_groups,
            "requires_secondary_search": bool(missing_expected_groups),
        },
        "secondary_search_hints": list(SECONDARY_GOVERNANCE_SEARCH_HINTS),
    }


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
    if mode not in AGENTPR_SKILLS_MODES:
        return SkillPlan(
            mode=mode,
            run_state=run_state,
            control_model="disabled",
            required_now=tuple(),
            optional_now=tuple(),
            missing_required=tuple(),
            available_optional=tuple(),
            missing_optional=tuple(),
            skills_root=skills_root or resolve_codex_skills_root(),
        )

    if mode == "agentpr_autonomous":
        if run_state in {RunState.ITERATING, RunState.CI_WAIT, RunState.REVIEW_WAIT}:
            required_now = (AGENTPR_SKILL_IMPLEMENT_VALIDATE, AGENTPR_SKILL_CI_REVIEW_FIX)
        else:
            required_now = (AGENTPR_SKILL_REPO_PREFLIGHT, AGENTPR_SKILL_IMPLEMENT_VALIDATE)
    else:
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
        control_model=("worker_autonomous" if mode == "agentpr_autonomous" else "orchestrator_staged"),
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
    if plan.mode not in AGENTPR_SKILLS_MODES:
        return base_prompt

    required_lines = [f"{idx + 1}. ${name}" for idx, name in enumerate(plan.required_now)]
    optional_lines = [f"- ${name}" for name in plan.available_optional]

    optional_block = ""
    if optional_lines:
        optional_block = (
            "\nOptional helper skills currently installed (use when helpful):\n"
            + "\n".join(optional_lines)
            + "\n"
        )

    task_packet_json = json.dumps(task_packet, ensure_ascii=True, sort_keys=True, indent=2)

    if plan.mode == "agentpr_autonomous":
        return (
            "AgentPR manager instruction: skills-mode is enabled (worker-autonomous).\n"
            "Use the installed AgentPR skills below as reusable tools; worker decides invocation order.\n"
            "Suggested internal flow: analyze contract/governance -> implement+validate -> optional ci/review fix.\n"
            "\n"
            "Installed core skills for this run:\n"
            f"{chr(10).join(required_lines)}\n"
            f"{optional_block}"
            "Autonomous execution rules:\n"
            "- Keep minimal diff and follow repository contribution rules.\n"
            "- Reuse task packet governance evidence first, then do secondary search only when needed.\n"
            "- Governance paths in task packet are direct file paths; read them explicitly even under hidden dirs like .github/.\n"
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
        "- Governance paths in task packet are direct file paths; read them explicitly even under hidden dirs like .github/.\n"
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
    governance_scan: dict[str, Any] | None = None,
    user_packet: Any | None = None,
) -> dict[str, Any]:
    resolved_governance_scan = (
        governance_scan
        if isinstance(governance_scan, dict)
        else scan_repo_governance_sources(repo_dir=repo_dir)
    )
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
            "governance_scan": resolved_governance_scan,
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
