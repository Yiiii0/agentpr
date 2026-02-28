"""PR body building, request I/O, and external path resolution."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from .cli_helpers import normalize_text_block, read_text_if_exists
from .skills import (
    resolve_codex_home,
    resolve_codex_skills_root,
    scan_repo_governance_sources,
    select_primary_pr_template,
)

PROJECT_ROOT = Path(__file__).resolve().parents[1]

# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _strip_template_preamble(text: str) -> str:
    normalized = normalize_text_block(text)
    if not normalized:
        return ""
    parts = re.split(r"\n-{3,}\n", normalized, maxsplit=1)
    if len(parts) == 2 and (
        "PR Description Template" in parts[0]
        or "创建 PR 时复制以下内容" in parts[0]
    ):
        return normalize_text_block(parts[1])
    return normalized


def _contains_about_forge_section(text: str) -> bool:
    return re.search(
        r"^\s{0,3}#{1,6}\s*about forge\b",
        str(text or ""),
        flags=re.IGNORECASE | re.MULTILINE,
    ) is not None


def _load_repo_pr_template_text(repo_dir: Path) -> tuple[str, str]:
    governance = scan_repo_governance_sources(repo_dir=repo_dir)
    groups = governance.get("groups")
    groups = groups if isinstance(groups, dict) else {}
    candidates = [str(item).strip() for item in list(groups.get("pr_templates") or [])]
    primary = select_primary_pr_template(candidates) or str(
        governance.get("primary_pr_template") or ""
    ).strip()
    if not primary:
        return "", ""
    path = repo_dir / primary
    return primary, normalize_text_block(read_text_if_exists(path))


def _load_about_forge_text(
    *,
    integration_root: Path,
    project_name: str,
) -> tuple[str, str]:
    finish_script = integration_root / "scripts" / "finish.sh"
    finish_text = read_text_if_exists(finish_script)
    if finish_text:
        match = re.search(
            r"## About Forge\n(?P<section>.*?)(?:\nEOF\b)",
            finish_text,
            flags=re.DOTALL,
        )
        if match:
            section = normalize_text_block(
                "## About Forge\n" + str(match.group("section") or "")
            )
            if section:
                section = section.replace("${PROJECT}", project_name)
                return section, "finish.sh"

    pr_template_path = integration_root / "pr_description_template.md"
    pr_template_text = read_text_if_exists(pr_template_path)
    if pr_template_text:
        match = re.search(
            r"## About Forge\n(?P<section>.*)$",
            pr_template_text,
            flags=re.DOTALL,
        )
        if match:
            section = normalize_text_block(
                "## About Forge\n" + str(match.group("section") or "")
            )
            if section:
                section = section.replace("[Project Name]", project_name)
                return section, "pr_description_template.md"
    return "", ""


def _load_default_pr_body(*, integration_root: Path, project_name: str) -> tuple[str, str]:
    template_path = integration_root / "pr_description_template.md"
    body = _strip_template_preamble(read_text_if_exists(template_path))
    if not body:
        return "", ""
    body = body.replace("[Project Name]", project_name)
    body = body.replace("[具体改动描述，按 repo 填写]", "Integrated Forge with minimal-diff changes.")
    return body, str(template_path)


def _build_manager_pr_body_stub(*, project_name: str) -> str:
    return (
        "## AgentPR Draft Summary\n\n"
        f"- Integrated Forge support for {project_name} with minimal-diff changes.\n"
        "- Kept existing provider behavior unchanged (additive integration only).\n"
        "- Validation evidence and run artifacts are attached in AgentPR outputs."
    )


# ---------------------------------------------------------------------------
# Public: build PR body
# ---------------------------------------------------------------------------


def build_request_open_pr_body(
    *,
    repo_dir: Path,
    integration_root: Path,
    user_body: str,
    project_name: str,
    prepend_repo_pr_template: bool,
    append_about_forge: bool,
) -> tuple[str, dict[str, Any]]:
    user_body_text = normalize_text_block(user_body)
    sections: list[str] = []
    metadata: dict[str, Any] = {
        "project_name": project_name,
        "user_body_supplied": bool(user_body_text),
        "repo_pr_template_used": False,
        "repo_pr_template_path": "",
        "manager_stub_appended": False,
        "fallback_body_used": False,
        "fallback_body_source": "",
        "about_forge_appended": False,
        "about_forge_source": "",
    }

    if prepend_repo_pr_template:
        template_relpath, template_text = _load_repo_pr_template_text(repo_dir)
        if template_text:
            sections.append(template_text)
            metadata["repo_pr_template_used"] = True
            metadata["repo_pr_template_path"] = template_relpath

    if user_body_text:
        sections.append(user_body_text)

    if metadata["repo_pr_template_used"] and not user_body_text:
        sections.append(_build_manager_pr_body_stub(project_name=project_name))
        metadata["manager_stub_appended"] = True

    if not sections:
        fallback_body, fallback_source = _load_default_pr_body(
            integration_root=integration_root,
            project_name=project_name,
        )
        if fallback_body:
            sections.append(fallback_body)
            metadata["fallback_body_used"] = True
            metadata["fallback_body_source"] = fallback_source

    composed = "\n\n---\n\n".join(
        section for section in sections if normalize_text_block(section)
    ).strip()

    if append_about_forge:
        about_forge_text, about_source = _load_about_forge_text(
            integration_root=integration_root,
            project_name=project_name,
        )
        if about_forge_text and not _contains_about_forge_section(composed):
            composed = (
                f"{composed}\n\n---\n\n{about_forge_text.strip()}"
                if composed
                else about_forge_text.strip()
            )
            metadata["about_forge_appended"] = True
            metadata["about_forge_source"] = about_source
        elif about_forge_text:
            metadata["about_forge_source"] = about_source

    return composed.strip(), metadata


# ---------------------------------------------------------------------------
# Public: external read-only paths
# ---------------------------------------------------------------------------


def resolve_external_read_only_paths(
    *,
    integration_root: Path,
    include_skills_root: bool,
    user_paths: list[str] | None = None,
) -> list[Path]:
    candidates: list[Path] = []
    resolved_integration_root = integration_root.expanduser().resolve()
    candidates.append(resolved_integration_root)

    forge_root = resolved_integration_root.parent.parent / "forge"
    if forge_root.exists():
        candidates.append(forge_root.resolve())

    if include_skills_root:
        skills_root = resolve_codex_skills_root(codex_home=resolve_codex_home())
        if skills_root.exists():
            candidates.append(skills_root.resolve())

    for raw in user_paths or []:
        value = str(raw).strip()
        if not value:
            continue
        path = Path(value).expanduser()
        if path.exists():
            candidates.append(path.resolve())

    out: list[Path] = []
    seen: set[str] = set()
    for path in candidates:
        value = str(path)
        if value in seen:
            continue
        seen.add(value)
        out.append(path)
    return out


# ---------------------------------------------------------------------------
# Public: PR request I/O
# ---------------------------------------------------------------------------


def write_pr_open_request(run_id: str, payload: dict[str, Any]) -> Path:
    reports_dir = PROJECT_ROOT / "orchestrator" / "data" / "reports"
    reports_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S%fZ")
    path = reports_dir / f"{run_id}_pr_open_request_{stamp}.json"
    path.write_text(
        json.dumps(payload, ensure_ascii=True, sort_keys=True, indent=2),
        encoding="utf-8",
    )
    return path


def read_pr_open_request(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except OSError as exc:
        raise ValueError(f"Failed to read request-file {path}: {exc}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(f"Invalid JSON in request-file {path}: {exc}") from exc
    required = {
        "run_id",
        "title",
        "body",
        "base",
        "head",
        "draft",
        "confirm_token",
        "created_at",
        "expires_at",
    }
    missing = sorted(required - set(payload.keys()))
    if missing:
        raise ValueError(f"request-file missing required fields: {', '.join(missing)}")
    return payload
