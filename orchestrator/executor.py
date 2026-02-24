from __future__ import annotations

import subprocess
import time
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class CommandResult:
    exit_code: int
    stdout: str
    stderr: str
    duration_ms: int


class ScriptExecutor:
    def __init__(self, integration_root: Path) -> None:
        self.integration_root = integration_root
        self.prepare_script = integration_root / "scripts" / "prepare.sh"
        self.finish_script = integration_root / "scripts" / "finish.sh"

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

    def run_agent_step(
        self,
        *,
        engine: str,
        prompt: str,
        repo_dir: Path,
        extra_args: list[str] | None = None,
    ) -> CommandResult:
        cmd = self._agent_cmd(
            engine=engine,
            prompt=prompt,
            extra_args=extra_args or [],
        )
        return self._run(cmd, cwd=repo_dir)

    @staticmethod
    def _agent_cmd(*, engine: str, prompt: str, extra_args: list[str]) -> list[str]:
        normalized = engine.strip().lower()
        if normalized == "codex":
            return ["codex", "exec", prompt, *extra_args]
        if normalized == "claude":
            return ["claude", "-p", prompt, *extra_args]
        raise ValueError(
            f"Unsupported agent engine: {engine}. Use one of: codex, claude."
        )

    @staticmethod
    def _run(cmd: list[str], cwd: Path) -> CommandResult:
        start = time.monotonic()
        try:
            completed = subprocess.run(  # noqa: S603
                cmd,
                cwd=cwd,
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
