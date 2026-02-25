from __future__ import annotations

import json
import os
import shutil
import subprocess
import time
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.error import URLError
from urllib.request import Request, urlopen

from .codex_bin import resolve_codex_binary


@dataclass(frozen=True)
class CheckResult:
    name: str
    ok: bool
    detail: str


@dataclass(frozen=True)
class PreflightReport:
    ok: bool
    failures: list[str]
    warnings: list[str]
    checks: list[CheckResult]
    duration_ms: int
    context: dict[str, Any]

    def to_dict(self) -> dict[str, Any]:
        return {
            "ok": self.ok,
            "failures": self.failures,
            "warnings": self.warnings,
            "duration_ms": self.duration_ms,
            "context": self.context,
            "checks": [
                {"name": c.name, "ok": c.ok, "detail": c.detail} for c in self.checks
            ],
        }


class PreflightChecker:
    def __init__(
        self,
        repo_dir: Path,
        *,
        workspace_root: Path | None = None,
        check_network: bool = True,
        network_timeout_sec: int = 5,
        codex_sandbox: str = "workspace-write",
    ) -> None:
        self.repo_dir = repo_dir
        self.workspace_root = workspace_root
        self.check_network = check_network
        self.network_timeout_sec = network_timeout_sec
        self.codex_sandbox = codex_sandbox

    def run(self) -> PreflightReport:
        start = time.monotonic()
        checks: list[CheckResult] = []
        failures: list[str] = []
        warnings: list[str] = []

        uses_python = self._detect_python_project()
        python_tools = self._detect_python_toolchain_commands()
        js_tools = self._detect_js_toolchain_commands()
        uses_js = self._detect_js_project()
        uses_bun = "bun" in js_tools

        checks.append(self._check_repo_exists())
        checks.append(self._check_workspace_scope())
        checks.append(self._check_git_write())
        checks.append(self._check_command("git"))
        checks.append(self._check_codex_sandbox())

        if uses_python:
            checks.append(self._check_command("python3.11"))
        for cmd in sorted(python_tools):
            checks.append(self._check_command(cmd))
        for cmd in sorted(js_tools):
            checks.append(self._check_command(cmd))

        if self.check_network:
            if uses_python:
                checks.append(self._check_url("https://pypi.org/simple/"))
            if uses_js:
                checks.append(self._check_url("https://registry.npmjs.org/"))

        for check in checks:
            if not check.ok:
                failures.append(f"{check.name}: {check.detail}")

        if not uses_python and not uses_js:
            warnings.append(
                "Project type detection found neither python nor javascript markers; "
                "network/tooling checks may be incomplete."
            )

        duration_ms = int((time.monotonic() - start) * 1000)
        return PreflightReport(
            ok=len(failures) == 0,
            failures=failures,
            warnings=warnings,
            checks=checks,
            duration_ms=duration_ms,
            context={
                "repo_dir": str(self.repo_dir),
                "workspace_root": str(self.workspace_root) if self.workspace_root else None,
                "uses_python": uses_python,
                "python_tools": sorted(python_tools),
                "uses_js": uses_js,
                "js_tools": sorted(js_tools),
                "uses_bun": uses_bun,
                "check_network": self.check_network,
                "network_timeout_sec": self.network_timeout_sec,
                "codex_sandbox": self.codex_sandbox,
            },
        )

    def _detect_python_project(self) -> bool:
        return any(
            (self.repo_dir / marker).exists()
            for marker in (
                "pyproject.toml",
                "requirements.txt",
                "requirements-dev.txt",
                "setup.py",
            )
        )

    def _detect_js_project(self) -> bool:
        return any(
            (self.repo_dir / marker).exists()
            for marker in (
                "package.json",
                "bun.lock",
                "pnpm-lock.yaml",
                "yarn.lock",
                "package-lock.json",
                "npm-shrinkwrap.json",
            )
        )

    def _detect_python_toolchain_commands(self) -> set[str]:
        tools: set[str] = set()
        pyproject = self._load_pyproject()
        tool_section = pyproject.get("tool", {}) if isinstance(pyproject, dict) else {}
        if isinstance(tool_section, dict):
            if "rye" in tool_section:
                tools.add("rye")
            if "poetry" in tool_section:
                tools.add("poetry")
            if "hatch" in tool_section:
                tools.add("hatch")
            if "uv" in tool_section:
                tools.add("uv")
            if "tox" in tool_section:
                tools.add("tox")

        if (self.repo_dir / "tox.ini").exists() or (self.repo_dir / ".tox").exists():
            tools.add("tox")
        if (self.repo_dir / "poetry.lock").exists():
            tools.add("poetry")
        return tools

    def _detect_js_toolchain_commands(self) -> set[str]:
        tools: set[str] = set()
        if not self._detect_js_project():
            return tools

        package_manager = self._detect_package_manager()
        if package_manager:
            tools.add(package_manager)
        if (self.repo_dir / "package.json").exists():
            tools.add("node")
        return tools

    def _detect_package_manager(self) -> str | None:
        if (self.repo_dir / "bun.lock").exists():
            return "bun"
        if (self.repo_dir / "pnpm-lock.yaml").exists():
            return "pnpm"
        if (self.repo_dir / "yarn.lock").exists():
            return "yarn"
        if (self.repo_dir / "package-lock.json").exists() or (
            self.repo_dir / "npm-shrinkwrap.json"
        ).exists():
            return "npm"

        package_json = self.repo_dir / "package.json"
        if not package_json.exists():
            return None
        try:
            data = json.loads(package_json.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return "npm"
        package_manager = str(data.get("packageManager", ""))
        if package_manager.startswith("bun@"):
            return "bun"
        if package_manager.startswith("pnpm@"):
            return "pnpm"
        if package_manager.startswith("yarn@"):
            return "yarn"
        if package_manager.startswith("npm@"):
            return "npm"
        return "npm"

    def _load_pyproject(self) -> dict[str, Any]:
        pyproject_path = self.repo_dir / "pyproject.toml"
        if not pyproject_path.exists():
            return {}
        try:
            content = pyproject_path.read_bytes()
            parsed = tomllib.loads(content.decode("utf-8"))
            if isinstance(parsed, dict):
                return parsed
        except (OSError, UnicodeDecodeError, tomllib.TOMLDecodeError):
            return {}
        return {}

    def _check_repo_exists(self) -> CheckResult:
        if self.repo_dir.exists():
            return CheckResult("repo.exists", True, str(self.repo_dir))
        return CheckResult("repo.exists", False, f"Missing repo dir: {self.repo_dir}")

    def _check_workspace_scope(self) -> CheckResult:
        if self.workspace_root is None:
            return CheckResult("repo.scope", True, "workspace scope check skipped")
        try:
            repo_resolved = self.repo_dir.resolve(strict=True)
            root_resolved = self.workspace_root.resolve(strict=True)
        except OSError as exc:
            return CheckResult("repo.scope", False, f"Resolve failed: {exc}")
        if repo_resolved.is_relative_to(root_resolved):
            return CheckResult("repo.scope", True, f"{repo_resolved} is within {root_resolved}")
        return CheckResult(
            "repo.scope",
            False,
            f"{repo_resolved} is outside workspace root {root_resolved}",
        )

    def _check_git_write(self) -> CheckResult:
        git_dir = self.repo_dir / ".git"
        if not git_dir.exists():
            return CheckResult("git.dir", False, f"Missing git dir: {git_dir}")

        probe = git_dir / ".agentpr_write_probe"
        try:
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return CheckResult("git.write", True, f"Writable: {git_dir}")
        except OSError as exc:
            return CheckResult("git.write", False, f"Cannot write in .git: {exc}")

    @staticmethod
    def _check_command(command: str) -> CheckResult:
        path = shutil.which(command)
        if path:
            return CheckResult(f"cmd.{command}", True, path)
        return CheckResult(f"cmd.{command}", False, "Not found in PATH")

    def _check_url(self, url: str) -> CheckResult:
        request = Request(url=url, method="HEAD")
        try:
            with urlopen(request, timeout=self.network_timeout_sec) as response:  # noqa: S310
                code = getattr(response, "status", None) or response.getcode()
                if 200 <= int(code) < 500:
                    return CheckResult(f"net.{url}", True, f"HTTP {code}")
                return CheckResult(f"net.{url}", False, f"HTTP {code}")
        except URLError as exc:
            return CheckResult(f"net.{url}", False, str(exc))

    def _check_codex_sandbox(self) -> CheckResult:
        if self.codex_sandbox == "read-only":
            return CheckResult(
                "policy.codex_sandbox",
                False,
                "read-only blocks environment creation and test runs",
            )
        return CheckResult(
            "policy.codex_sandbox",
            True,
            f"mode={self.codex_sandbox}",
        )


class RuntimeDoctor:
    def __init__(
        self,
        *,
        workspace_root: Path,
        check_network: bool = True,
        network_timeout_sec: int = 5,
        require_gh_auth: bool = True,
        require_codex: bool = False,
        require_telegram_token: bool = False,
        require_webhook_secret: bool = False,
    ) -> None:
        self.workspace_root = workspace_root
        self.check_network = check_network
        self.network_timeout_sec = network_timeout_sec
        self.require_gh_auth = require_gh_auth
        self.require_codex = require_codex
        self.require_telegram_token = require_telegram_token
        self.require_webhook_secret = require_webhook_secret

    def run(self) -> PreflightReport:
        start = time.monotonic()
        checks: list[CheckResult] = []
        failures: list[str] = []
        warnings: list[str] = []

        checks.append(self._check_workspace_write())
        checks.append(self._check_command("git"))
        checks.append(self._check_command("python3.11"))
        checks.append(self._check_command("gh"))
        if self.require_codex:
            checks.append(self._check_codex_binary())
        if self.require_gh_auth:
            checks.append(self._check_gh_auth())
        if self.require_telegram_token:
            checks.append(
                self._check_secret_env(
                    "AGENTPR_TELEGRAM_BOT_TOKEN",
                    required=True,
                )
            )
        if self.require_webhook_secret:
            checks.append(
                self._check_secret_env(
                    "AGENTPR_GITHUB_WEBHOOK_SECRET",
                    required=True,
                )
            )

        if self.check_network:
            checks.append(self._check_url("https://github.com/"))
            checks.append(self._check_url("https://api.github.com/"))
            if self.require_codex:
                checks.append(self._check_url("https://pypi.org/simple/"))
                checks.append(self._check_url("https://registry.npmjs.org/"))

        for check in checks:
            if not check.ok:
                failures.append(f"{check.name}: {check.detail}")

        if self.check_network and not self.require_codex:
            warnings.append("Package registry checks skipped (require_codex=false).")

        duration_ms = int((time.monotonic() - start) * 1000)
        return PreflightReport(
            ok=len(failures) == 0,
            failures=failures,
            warnings=warnings,
            checks=checks,
            duration_ms=duration_ms,
            context={
                "workspace_root": str(self.workspace_root),
                "check_network": self.check_network,
                "network_timeout_sec": self.network_timeout_sec,
                "require_gh_auth": self.require_gh_auth,
                "require_codex": self.require_codex,
                "require_telegram_token": self.require_telegram_token,
                "require_webhook_secret": self.require_webhook_secret,
            },
        )

    @staticmethod
    def _check_command(command: str) -> CheckResult:
        path = shutil.which(command)
        if path:
            return CheckResult(f"cmd.{command}", True, path)
        return CheckResult(f"cmd.{command}", False, "Not found in PATH")

    @staticmethod
    def _check_codex_binary() -> CheckResult:
        resolved, source = resolve_codex_binary()
        if resolved:
            return CheckResult("cmd.codex", True, f"{resolved} ({source})")
        return CheckResult("cmd.codex", False, source)

    def _check_workspace_write(self) -> CheckResult:
        try:
            self.workspace_root.mkdir(parents=True, exist_ok=True)
            probe = self.workspace_root / (
                f".agentpr_doctor_probe_{os.getpid()}_{time.time_ns()}"
            )
            probe.write_text("ok", encoding="utf-8")
            probe.unlink()
            return CheckResult("workspace.write", True, f"Writable: {self.workspace_root}")
        except OSError as exc:
            return CheckResult("workspace.write", False, f"Cannot write: {exc}")

    def _check_gh_auth(self) -> CheckResult:
        try:
            completed = subprocess.run(  # noqa: S603
                ["gh", "auth", "status"],
                capture_output=True,
                text=True,
                check=False,
            )
        except OSError as exc:
            return CheckResult("gh.auth", False, str(exc))
        if completed.returncode == 0:
            return CheckResult("gh.auth", True, "authenticated")
        detail = completed.stderr.strip() or completed.stdout.strip() or "gh auth status failed"
        lines = [line.strip() for line in detail.splitlines() if line.strip()]
        meaningful = " | ".join(lines[:3]) if lines else "gh auth status failed"
        return CheckResult("gh.auth", False, meaningful[:240])

    @staticmethod
    def _check_secret_env(env_name: str, *, required: bool) -> CheckResult:
        value = str(os.environ.get(env_name, "")).strip()
        if value:
            return CheckResult(f"env.{env_name}", True, "present")
        if required:
            return CheckResult(f"env.{env_name}", False, "missing")
        return CheckResult(f"env.{env_name}", True, "optional and missing")

    def _check_url(self, url: str) -> CheckResult:
        request = Request(url=url, method="HEAD")
        try:
            with urlopen(request, timeout=self.network_timeout_sec) as response:  # noqa: S310
                code = getattr(response, "status", None) or response.getcode()
                if 200 <= int(code) < 500:
                    return CheckResult(f"net.{url}", True, f"HTTP {code}")
                return CheckResult(f"net.{url}", False, f"HTTP {code}")
        except URLError as exc:
            return CheckResult(f"net.{url}", False, str(exc))
