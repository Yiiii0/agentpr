from __future__ import annotations

import json
import os
import subprocess
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from .codex_bin import resolve_codex_binary

@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int
    metadata: dict[str, Any] | None = None


DEFAULT_RUNTIME_ENV_TEMPLATES: dict[str, str] = {
    "AGENTPR_RUNTIME_DIR": "{runtime_dir}",
    "AGENTPR_WORKSPACE_DIR": "{repo_dir}",
    "XDG_CACHE_HOME": "{cache_dir}",
    "XDG_DATA_HOME": "{data_dir}",
    "TMPDIR": "{tmp_dir}",
    "PIP_CACHE_DIR": "{cache_dir}/pip",
    "PIP_DISABLE_PIP_VERSION_CHECK": "1",
    "PIP_REQUIRE_VIRTUALENV": "true",
    "UV_CACHE_DIR": "{cache_dir}/uv",
    "UV_TOOL_DIR": "{runtime_dir}/uv-tools",
    "POETRY_CACHE_DIR": "{cache_dir}/poetry",
    "POETRY_DATA_DIR": "{data_dir}/poetry",
    "POETRY_VIRTUALENVS_IN_PROJECT": "true",
    "HATCH_CACHE_DIR": "{cache_dir}/hatch",
    "HATCH_DATA_DIR": "{data_dir}/hatch",
    "RYE_HOME": "{runtime_dir}/rye-home",
    "RYE_CACHE_DIR": "{cache_dir}/rye",
    "TOX_WORK_DIR": "{runtime_dir}/tox",
    "NPM_CONFIG_CACHE": "{cache_dir}/npm",
    "NPM_CONFIG_PREFIX": "{runtime_dir}/npm-global",
    "BUN_INSTALL_CACHE_DIR": "{cache_dir}/bun",
    "BUN_INSTALL_GLOBAL_DIR": "{runtime_dir}/bun-global",
    "YARN_CACHE_FOLDER": "{cache_dir}/yarn",
    "PNPM_HOME": "{runtime_dir}/pnpm-home",
    "RUFF_CACHE_DIR": "{cache_dir}/ruff",
    "MYPY_CACHE_DIR": "{cache_dir}/mypy",
    "PYTHONPYCACHEPREFIX": "{runtime_dir}/pycache",
    "COVERAGE_FILE": "{runtime_dir}/coverage/.coverage",
}

PATH_ENV_KEYS: set[str] = {
    "AGENTPR_RUNTIME_DIR",
    "AGENTPR_WORKSPACE_DIR",
    "XDG_CACHE_HOME",
    "XDG_DATA_HOME",
    "TMPDIR",
    "PIP_CACHE_DIR",
    "UV_CACHE_DIR",
    "UV_TOOL_DIR",
    "POETRY_CACHE_DIR",
    "POETRY_DATA_DIR",
    "HATCH_CACHE_DIR",
    "HATCH_DATA_DIR",
    "RYE_HOME",
    "RYE_CACHE_DIR",
    "TOX_WORK_DIR",
    "NPM_CONFIG_CACHE",
    "NPM_CONFIG_PREFIX",
    "BUN_INSTALL_CACHE_DIR",
    "BUN_INSTALL_GLOBAL_DIR",
    "YARN_CACHE_FOLDER",
    "PNPM_HOME",
    "RUFF_CACHE_DIR",
    "MYPY_CACHE_DIR",
    "PYTHONPYCACHEPREFIX",
}

FILE_PATH_ENV_KEYS: set[str] = {"COVERAGE_FILE"}


class ScriptExecutor:
    def __init__(self, integration_root: Path) -> None:
        self.integration_root = integration_root
        self.prepare_script = integration_root / "scripts" / "prepare.sh"
        self.finish_script = integration_root / "scripts" / "finish.sh"
        self.runtime_env_overrides_path = (
            integration_root.parent / "orchestrator" / "runtime_env_overrides.json"
        )
        self.codex_bin, self.codex_bin_source = resolve_codex_binary()

    def run_prepare(
        self,
        *,
        owner: str,
        repo: str,
        base_branch: str | None = None,
        feature_branch: str | None = None,
    ) -> CommandResult:
        cmd = [str(self.prepare_script), owner, repo]
        if base_branch:
            cmd.append(base_branch)
        if feature_branch:
            if not base_branch:
                cmd.append("")
            cmd.append(feature_branch)
        return self._run(cmd, cwd=self.integration_root)

    def run_finish(
        self,
        *,
        repo_dir: Path,
        changes: str,
        project: str | None = None,
        commit_title: str | None = None,
    ) -> CommandResult:
        cmd = [str(self.finish_script), changes]
        if project:
            cmd.append(project)
        if commit_title:
            if not project:
                cmd.append(repo_dir.name)
            cmd.append(commit_title)
        return self._run(cmd, cwd=repo_dir)

    def current_branch(self, repo_dir: Path) -> str:
        result = self._run(["git", "branch", "--show-current"], cwd=repo_dir)
        if result.exit_code != 0:
            raise RuntimeError(
                f"Failed to determine branch in {repo_dir}: {result.stderr.strip()}"
            )
        return result.stdout.strip()

    def default_base_branch(self, repo_dir: Path) -> str:
        result = self._run(
            ["git", "symbolic-ref", "--short", "refs/remotes/origin/HEAD"],
            cwd=repo_dir,
        )
        if result.exit_code != 0:
            return "main"
        ref = result.stdout.strip()
        if "/" not in ref:
            return "main"
        return ref.split("/", 1)[1]

    def run_create_pr(
        self,
        *,
        repo_dir: Path,
        title: str,
        body: str,
        base: str,
        head: str,
        draft: bool = False,
    ) -> CommandResult:
        cmd = [
            "gh",
            "pr",
            "create",
            "--title",
            title,
            "--body",
            body,
            "--base",
            base,
            "--head",
            head,
        ]
        if draft:
            cmd.append("--draft")
        return self._run(cmd, cwd=repo_dir)

    def run_gh_pr_view(
        self,
        *,
        repo_dir: Path,
        pr_number: int,
    ) -> CommandResult:
        cmd = [
            "gh",
            "pr",
            "view",
            str(pr_number),
            "--json",
            "number,url,reviewDecision,reviews,statusCheckRollup,headRefName,baseRefName",
        ]
        return self._run(cmd, cwd=repo_dir)

    def run_agent_step(
        self,
        *,
        prompt: str,
        repo_dir: Path,
        codex_sandbox: str = "workspace-write",
        codex_full_auto: bool = True,
        codex_model: str | None = None,
        allow_git_push: bool = False,
        extra_args: list[str] | None = None,
        read_only_paths: list[Path] | None = None,
        max_duration_sec: int | None = None,
    ) -> CommandResult:
        if not self.codex_bin:
            return CommandResult(
                exit_code=127,
                stdout="",
                stderr=(
                    "codex executable not found. "
                    "Set AGENTPR_CODEX_BIN or add codex to PATH."
                ),
                duration_ms=0,
            )
        runtime_policy = self._build_runtime_policy(repo_dir=repo_dir)
        guarded_prompt = self._with_safety_contract(
            prompt=prompt,
            repo_dir=repo_dir,
            runtime_dir=runtime_policy["runtime_dir"],
            allow_git_push=allow_git_push,
            read_only_paths=read_only_paths or [],
        )
        last_message_path = runtime_policy["runtime_dir"] / "codex_last_message.txt"
        cmd = [self.codex_bin, "exec", "--sandbox", codex_sandbox]
        if codex_full_auto and codex_sandbox == "workspace-write":
            cmd.append("--full-auto")
        if codex_model:
            cmd.extend(["--model", codex_model])
        cmd.extend(
            [
                "--json",
                "--output-last-message",
                str(last_message_path),
            ]
        )
        cmd.append(guarded_prompt)
        cmd.extend(extra_args or [])
        result = self._run_with_stdout_timeline(
            cmd,
            cwd=repo_dir,
            env=runtime_policy["env"],
            max_duration_sec=max_duration_sec,
        )
        metadata = {
            "codex_jsonl": True,
            "last_message_path": str(last_message_path),
            "runtime_dir": str(runtime_policy["runtime_dir"]),
            "max_duration_sec": max_duration_sec,
        }
        if isinstance(result.metadata, dict):
            metadata.update(result.metadata)
        return CommandResult(
            exit_code=result.exit_code,
            stdout=result.stdout,
            stderr=result.stderr,
            duration_ms=result.duration_ms,
            metadata=metadata,
        )

    @staticmethod
    def _with_safety_contract(
        *,
        prompt: str,
        repo_dir: Path,
        runtime_dir: Path,
        allow_git_push: bool,
        read_only_paths: list[Path],
    ) -> str:
        push_rule = (
            "- Do NOT run git commit/git push/finish.sh. "
            "Manager owns commit+push gate for this run.\n"
        )
        if allow_git_push:
            push_rule = (
                "- git commit/push is allowed only after all required tests/lint pass, "
                "and only with minimal intended diff.\n"
            )
        read_only_lines: list[str] = []
        seen: set[str] = set()
        for item in read_only_paths:
            resolved = item.expanduser().resolve()
            value = str(resolved)
            if value in seen:
                continue
            seen.add(value)
            read_only_lines.append(f"  - {value}")
        read_only_block = ""
        if read_only_lines:
            read_only_block = (
                "External read-only context paths allowed:\n"
                + "\n".join(read_only_lines)
                + "\n"
            )
        contract = (
            "Execution safety contract (mandatory):\n"
            f"- Operate only inside repository: {repo_dir}\n"
            f"- Runtime writable scratch/cache area: {runtime_dir}\n"
            "- Do NOT write outside repository, runtime scratch/cache area, or /tmp.\n"
            "- Do NOT read paths outside repository unless listed in External read-only context paths.\n"
            f"{read_only_block}"
            "- Do NOT run sudo.\n"
            "- Do NOT install global packages (brew install, npm -g, pip --user/global, uv tool install, poetry self).\n"
            "- Use only project-local environments and dependencies (.venv/node_modules/.agentpr_runtime).\n"
            f"{push_rule}"
            "- If any required step needs out-of-repo writes or global changes, stop and report NEEDS REVIEW.\n"
        )
        return f"{contract}\n---\n\n{prompt}"

    def runtime_policy_summary(self, repo_dir: Path) -> dict[str, Any]:
        policy = self._build_runtime_policy(repo_dir=repo_dir)
        return {
            "runtime_dir": str(policy["runtime_dir"]),
            "policy_file": str(self.runtime_env_overrides_path),
            "policy_file_loaded": policy["policy_file_loaded"],
            "env_keys": sorted(policy["env_overrides"].keys()),
            "codex_bin": self.codex_bin,
            "codex_bin_source": self.codex_bin_source,
        }

    def _build_runtime_policy(self, *, repo_dir: Path) -> dict[str, Any]:
        env = dict(os.environ)
        runtime_dir = repo_dir / ".agentpr_runtime"
        cache_dir = runtime_dir / "cache"
        data_dir = runtime_dir / "data"
        tmp_dir = runtime_dir / "tmp"

        templates, loaded = self._load_runtime_env_templates()
        context = {
            "repo_dir": str(repo_dir),
            "runtime_dir": str(runtime_dir),
            "cache_dir": str(cache_dir),
            "data_dir": str(data_dir),
            "tmp_dir": str(tmp_dir),
        }

        env_overrides: dict[str, str] = {}
        for key, template in templates.items():
            try:
                env_overrides[key] = template.format(**context)
            except KeyError as exc:
                missing = exc.args[0]
                raise ValueError(
                    f"Invalid runtime env template for {key}: unknown token {missing}"
                ) from exc

        for key in PATH_ENV_KEYS:
            value = env_overrides.get(key)
            if not value:
                continue
            Path(value).mkdir(parents=True, exist_ok=True)
        for key in FILE_PATH_ENV_KEYS:
            value = env_overrides.get(key)
            if not value:
                continue
            Path(value).parent.mkdir(parents=True, exist_ok=True)

        env.update(env_overrides)
        return {
            "env": env,
            "env_overrides": env_overrides,
            "runtime_dir": runtime_dir,
            "policy_file_loaded": loaded,
        }

    def _load_runtime_env_templates(self) -> tuple[dict[str, str], bool]:
        templates = dict(DEFAULT_RUNTIME_ENV_TEMPLATES)
        path = self.runtime_env_overrides_path
        if not path.exists():
            return templates, False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            raise ValueError(f"Failed to load runtime env overrides {path}: {exc}") from exc
        if not isinstance(payload, dict):
            raise ValueError(f"Runtime env overrides must be a JSON object: {path}")
        for key, value in payload.items():
            if not isinstance(key, str) or not isinstance(value, str):
                raise ValueError(
                    f"Runtime env override entries must be string:string in {path}"
                )
        templates.update(payload)
        return templates, True

    @staticmethod
    def _run(
        cmd: list[str],
        cwd: Path,
        env: dict[str, str] | None = None,
    ) -> CommandResult:
        start = time.monotonic()
        try:
            completed = subprocess.run(  # noqa: S603
                cmd,
                cwd=cwd,
                env=env,
                capture_output=True,
                text=True,
                check=False,
            )
            exit_code = completed.returncode
            stdout = completed.stdout
            stderr = completed.stderr
        except OSError as exc:
            exit_code = 127
            stdout = ""
            stderr = str(exc)
        duration_ms = int((time.monotonic() - start) * 1000)
        return CommandResult(
            exit_code=exit_code,
            stdout=stdout,
            stderr=stderr,
            duration_ms=duration_ms,
        )

    @staticmethod
    def _run_with_stdout_timeline(
        cmd: list[str],
        cwd: Path,
        env: dict[str, str] | None = None,
        max_duration_sec: int | None = None,
    ) -> CommandResult:
        start = time.monotonic()
        try:
            process = subprocess.Popen(  # noqa: S603
                cmd,
                cwd=cwd,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                bufsize=1,
            )
        except OSError as exc:
            return CommandResult(
                exit_code=127,
                stdout="",
                stderr=str(exc),
                duration_ms=0,
                metadata={"stdout_line_offsets_ms": []},
            )

        stdout_lines: list[str] = []
        stderr_lines: list[str] = []
        stdout_line_offsets_ms: list[int] = []

        def read_stdout() -> None:
            if process.stdout is None:
                return
            for line in process.stdout:
                stdout_lines.append(line)
                stdout_line_offsets_ms.append(int((time.monotonic() - start) * 1000))

        def read_stderr() -> None:
            if process.stderr is None:
                return
            for line in process.stderr:
                stderr_lines.append(line)

        stdout_thread = threading.Thread(target=read_stdout, daemon=True)
        stderr_thread = threading.Thread(target=read_stderr, daemon=True)
        stdout_thread.start()
        stderr_thread.start()
        timed_out = False
        try:
            if max_duration_sec is not None and max_duration_sec > 0:
                exit_code = process.wait(timeout=max_duration_sec)
            else:
                exit_code = process.wait()
        except subprocess.TimeoutExpired:
            timed_out = True
            process.kill()
            exit_code = 124
            stderr_lines.append(
                f"agent_execution_timeout: exceeded max_duration_sec={max_duration_sec}\n"
            )
        stdout_thread.join()
        stderr_thread.join()

        duration_ms = int((time.monotonic() - start) * 1000)
        return CommandResult(
            exit_code=exit_code,
            stdout="".join(stdout_lines),
            stderr="".join(stderr_lines),
            duration_ms=duration_ms,
            metadata={
                "stdout_line_offsets_ms": stdout_line_offsets_ms,
                "timed_out": timed_out,
            },
        )
