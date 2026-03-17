"""Manager Agent tools — the agent's interface to the infrastructure layer.

Design principles (SWE-agent ACI, insight #24):
1. Input: strictly constrained (enum, required fields, JSON schema)
2. Output: includes suggested next action (not just raw data)
3. Errors: actionable (tell agent what to do, not just "failed")
4. Safety: checks embedded in tool (agent cannot bypass)
"""

from __future__ import annotations

import json
import logging
import os
import subprocess
import tempfile
from pathlib import Path
from typing import Any, Callable

from .models import RunState, StepName
from .service import OrchestratorService
from .state_machine import allowed_targets, can_transition, is_terminal

logger = logging.getLogger("agentpr.agent_tools")

# Max retries before escalating to human
_MAX_RETRIES = 3


class AgentToolkit:
    """Collection of tools available to the Manager Agent.

    Each tool method returns a string (the tool result the agent sees).
    Safety checks are embedded — the agent cannot bypass them.
    """

    def __init__(
        self,
        *,
        service: OrchestratorService,
        workspace_root: Path,
        integration_root: Path,
        prompt_file: Path | None = None,
        skills_mode: str | None = "agentpr",
        codex_sandbox: str | None = None,
        telegram_sender: Callable[[str], None] | None = None,
    ) -> None:
        self.service = service
        self.workspace_root = workspace_root
        self.integration_root = integration_root
        self.prompt_file = prompt_file
        self.skills_mode = skills_mode
        self.codex_sandbox = codex_sandbox
        self._telegram_sender = telegram_sender

    def execute(self, tool_name: str, args: dict[str, Any]) -> str:
        """Dispatch a tool call by name. Returns result string."""
        handler = {
            "query_runs": self.query_runs,
            "update_state": self.update_state,
            "read_evidence": self.read_evidence,
            "execute_worker": self.execute_worker,
            "github_api": self.github_api,
            "review_code": self.review_code,
            "generate_pr_body": self.generate_pr_body,
            "notify_human": self.notify_human,
            "reply_human": self.reply_human,
            "update_skill": self.update_skill,
        }.get(tool_name)

        if handler is None:
            return f"ERROR: Unknown tool '{tool_name}'. Available: {', '.join(TOOL_DEFINITIONS.keys())}"

        try:
            return handler(**args)
        except TypeError as exc:
            return f"ERROR: Invalid arguments for {tool_name}: {exc}"
        except Exception as exc:
            logger.exception("tool %s failed", tool_name)
            return f"ERROR: {tool_name} failed: {exc}"

    # ── Tool 1: query_runs ──────────────────────────────────────────────

    def query_runs(
        self,
        run_id: str | None = None,
        state: str | None = None,
        limit: int = 20,
    ) -> str:
        """Query run information from the database."""
        if run_id:
            try:
                snap = self.service.get_run_snapshot(run_id)
            except KeyError:
                return f"ERROR: Run '{run_id}' not found."
            return _format_run_detail(snap)

        runs = self.service.list_runs(limit=min(limit, 50))
        if state:
            state_upper = state.upper()
            runs = [r for r in runs if r.get("current_state", r.get("state", "")).upper() == state_upper]

        if not runs:
            return "No runs found." + (f" (filter: state={state})" if state else "")

        lines = [f"Found {len(runs)} run(s):\n"]
        for r in runs:
            lines.append(_format_run_summary(r))
        return "\n".join(lines)

    # ── Tool 2: update_state ────────────────────────────────────────────

    def update_state(
        self,
        run_id: str,
        to_state: str,
        reason: str = "",
    ) -> str:
        """Transition a run to a new state. Safety checks automatic."""
        # Validate target state
        try:
            target = RunState(to_state.upper())
        except ValueError:
            valid = [s.value for s in RunState]
            return f"ERROR: Invalid state '{to_state}'. Valid states: {valid}"

        # Get current state
        try:
            snap = self.service.get_run_snapshot(run_id)
        except KeyError:
            return f"ERROR: Run '{run_id}' not found."

        current = RunState(snap.get("state", snap.get("run", {}).get("current_state", "")))

        # Same state — no-op
        if current == target:
            return f"OK: {run_id} already in {current.value}."

        # Check transition validity
        if not can_transition(current, target):
            valid = [t.value for t in allowed_targets(current)]
            return (
                f"ERROR: Cannot transition {current.value} → {target.value}. "
                f"Valid transitions from {current.value}: {valid}"
            )

        # Execute via service (which records the event)
        try:
            if target == RunState.PAUSED:
                self.service.pause_run(run_id)
            elif target == RunState.DONE:
                self.service.mark_done(run_id)
            elif is_terminal(target):
                # SKIPPED, FAILED_TERMINAL
                self.service.record_step_failure(
                    run_id,
                    step=StepName.AGENT,
                    reason_code="agent_decision",
                    error_message=reason or f"Agent decided: {target.value}",
                )
            elif target == RunState.FAILED:
                self.service.record_step_failure(
                    run_id,
                    step=StepName.AGENT,
                    reason_code="agent_decision",
                    error_message=reason or "Agent decided to fail",
                )
            elif target == RunState.NEEDS_HUMAN_REVIEW:
                # Direct resume to NEEDS_HUMAN_REVIEW if allowed from current state
                self.service.resume_run(run_id, target_state=target)
            elif target in (RunState.EXECUTING, RunState.ITERATING):
                # Retry-type transitions
                self.service.retry_run(run_id, target_state=target)
            else:
                # Generic resume for other forward transitions
                self.service.resume_run(run_id, target_state=target)
        except Exception as exc:
            return f"ERROR: State transition failed: {exc}"

        logger.info("Agent transitioned %s: %s → %s (reason: %s)", run_id, current.value, target.value, reason)
        return f"OK: {run_id} transitioned {current.value} → {target.value}. Reason: {reason}"

    # ── Tool 3: read_evidence ───────────────────────────────────────────

    def read_evidence(
        self,
        run_id: str,
        include_diff: bool = False,
    ) -> str:
        """Read worker execution evidence: grade, test results, changed files."""
        try:
            snap = self.service.get_run_snapshot(run_id)
        except KeyError:
            return f"ERROR: Run '{run_id}' not found."

        run_data = snap.get("run", snap)
        state = snap.get("state", run_data.get("current_state", "?"))

        parts: list[str] = [f"## Evidence for {run_id}\n"]
        parts.append(f"State: {state}")
        parts.append(f"Repo: {run_data.get('owner', '?')}/{run_data.get('repo', '?')}")

        # Latest digest artifact
        digest = self.service.latest_artifact(run_id, artifact_type="run_digest")
        if digest:
            meta = digest.get("metadata") or {}
            parts.append(f"\n### Worker Output")
            parts.append(f"Grade: {meta.get('grade', 'N/A')}")
            parts.append(f"Reason: {meta.get('reason_code', 'N/A')}")
            parts.append(f"Changed files: {meta.get('changed_files_count', 'N/A')}")
            parts.append(f"Added lines: {meta.get('added_lines', 'N/A')}")
            parts.append(f"Deleted lines: {meta.get('deleted_lines', 'N/A')}")

            test_cmds = meta.get("test_command_count", "N/A")
            failed_tests = meta.get("failed_test_command_count", "N/A")
            parts.append(f"Test commands: {test_cmds} (failed: {failed_tests})")

            if meta.get("changed_files"):
                parts.append(f"Files: {', '.join(meta['changed_files'][:10])}")

            if meta.get("classification"):
                parts.append(f"Classification: {meta['classification']}")
        else:
            parts.append("\nNo worker evidence yet.")

        # Code review artifact
        review = self.service.latest_artifact(run_id, artifact_type="code_review")
        if review:
            rmeta = review.get("metadata") or {}
            parts.append(f"\n### Code Review")
            parts.append(f"Verdict: {rmeta.get('verdict', 'N/A')}")
            parts.append(f"Summary: {rmeta.get('summary', 'N/A')}")
            if rmeta.get("issues"):
                for issue in rmeta["issues"][:5]:
                    if isinstance(issue, dict):
                        parts.append(f"- [{issue.get('severity', '?')}] {issue.get('file', '?')}: {issue.get('description', '?')}")
                    else:
                        parts.append(f"- {issue}")

        # PR info
        if run_data.get("pr_number"):
            parts.append(f"\n### PR")
            parts.append(f"PR: #{run_data['pr_number']}")

        # Step attempts
        attempts = self.service.list_step_attempts(run_id, limit=5)
        if attempts:
            parts.append(f"\n### Recent Attempts ({len(attempts)})")
            for att in attempts[-3:]:
                parts.append(
                    f"- {att.get('step', '?')}: exit={att.get('exit_code', '?')} "
                    f"duration={att.get('duration_ms', 0)}ms"
                )

        # Git diff (if requested and workspace exists)
        if include_diff:
            workspace_dir = _resolve_workspace(run_data, self.workspace_root)
            if workspace_dir and workspace_dir.exists():
                diff = _git_diff(workspace_dir)
                if diff:
                    parts.append(f"\n### Diff (truncated to 4000 chars)")
                    parts.append(diff[:4000])

        return "\n".join(parts)

    # ── Tool 4: execute_worker ──────────────────────────────────────────

    def execute_worker(
        self,
        run_id: str,
        task: str = "integration",
    ) -> str:
        """Launch a codex exec Worker for this run. Blocking — waits for completion."""
        try:
            snap = self.service.get_run_snapshot(run_id)
        except KeyError:
            return f"ERROR: Run '{run_id}' not found."

        run_data = snap.get("run", snap)

        # Safety: check retry count
        retry_count = self.service.count_step_attempts(run_id, step=StepName.AGENT)
        if retry_count >= _MAX_RETRIES:
            return (
                f"ERROR: Max retries ({_MAX_RETRIES}) exceeded for {run_id} "
                f"(attempts: {retry_count}). "
                f"Use notify_human() to escalate, or update_state(to_state='NEEDS_HUMAN_REVIEW')."
            )

        # Safety: must be in a valid state for execution
        current_state = RunState(snap.get("state", run_data.get("current_state", "")))
        valid_exec_states = (
            RunState.EXECUTING, RunState.ITERATING,
            RunState.IMPLEMENTING, RunState.LOCAL_VALIDATING,
        )
        if current_state not in valid_exec_states:
            return (
                f"ERROR: Cannot execute worker in state {current_state.value}. "
                f"Use update_state(to_state='EXECUTING') first. "
                f"Valid states for worker: {[s.value for s in valid_exec_states]}"
            )

        if not self.prompt_file:
            return "ERROR: No prompt_file configured. Cannot launch worker."

        # Build worker command
        argv = [
            "run-agent-step",
            "--run-id", run_id,
            "--prompt-file", str(self.prompt_file),
        ]
        if self.skills_mode:
            argv.extend(["--skills-mode", self.skills_mode])
        if self.codex_sandbox:
            argv.extend(["--codex-sandbox", self.codex_sandbox])

        result = self._run_cli(argv)

        if not result["ok"]:
            return (
                f"Worker failed for {run_id}: {result.get('error', 'unknown')}. "
                f"Output: {result.get('output', '')[:500]}"
            )

        # Check if worker made changes that need commit+push
        # (when allow_agent_push=False, worker leaves changes uncommitted)
        workspace_dir = _resolve_workspace(run_data, self.workspace_root)
        if workspace_dir and workspace_dir.exists():
            try:
                diff_check = subprocess.run(
                    ["git", "diff", "--stat"],
                    cwd=str(workspace_dir), capture_output=True, text=True, timeout=10,
                )
                has_uncommitted = bool(diff_check.stdout.strip())
            except (OSError, subprocess.TimeoutExpired):
                has_uncommitted = False

            if has_uncommitted:
                # Run finish.sh to commit + push
                finish_result = self._run_cli([
                    "run-finish",
                    "--run-id", run_id,
                    "--changes", f"Add Forge as LLM provider for {run_data.get('repo', 'project')}",
                ])
                if not finish_result["ok"]:
                    return (
                        f"Worker completed for {run_id} but commit+push failed: "
                        f"{finish_result.get('error', 'unknown')}. "
                        f"Output: {finish_result.get('output', '')[:500]}"
                    )

        return (
            f"OK: Worker completed for {run_id}. "
            f"Use read_evidence(run_id='{run_id}') to check results."
        )

    # ── Tool 5: github_api ──────────────────────────────────────────────

    def github_api(
        self,
        run_id: str,
        action: str,
        params: dict[str, Any] | None = None,
    ) -> str:
        """Interact with GitHub for this run's repository."""
        params = params or {}

        actions = {
            "create_pr": self._create_pr,
            "read_ci": self._read_ci,
            "read_reviews": self._read_reviews,
            "read_pr_template": self._read_pr_template,
            "post_comment": self._post_comment,
        }
        handler = actions.get(action)
        if not handler:
            return f"ERROR: Unknown action '{action}'. Valid: {', '.join(actions.keys())}"
        return handler(run_id, params)

    def _create_pr(self, run_id: str, params: dict[str, Any]) -> str:
        """Create a PR through the request-open-pr + approve-open-pr pipeline."""
        title = params.get("title", "")
        body = params.get("body", "")

        # Build request-open-pr args
        argv = ["request-open-pr", "--run-id", run_id]
        if title:
            argv.extend(["--title", title])

        # If agent provided body, write to temp file and pass as --body-file
        body_file = None
        if body:
            try:
                body_file = tempfile.NamedTemporaryFile(
                    mode="w", suffix=".md", prefix="pr_body_",
                    dir=str(self.integration_root.parent / "orchestrator" / "data"),
                    delete=False,
                )
                body_file.write(body)
                body_file.close()
                argv.extend(["--body-file", body_file.name, "--skip-llm-body"])
            except OSError as exc:
                return f"ERROR: Failed to write PR body to temp file: {exc}"
        # If no body provided, let the CLI generate it via LLM

        result = self._run_cli(argv)
        if not result["ok"]:
            return f"ERROR: PR request creation failed: {result.get('error', 'unknown')}"

        # Parse request output for confirm_token and request_file
        request_file = None
        confirm_token = None
        try:
            output = json.loads(result.get("output", "{}"))
            request_file = output.get("request_file")
            confirm_token = output.get("confirm_token")
        except (json.JSONDecodeError, TypeError):
            # Try to find from artifacts
            art = self.service.latest_artifact(run_id, artifact_type="pr_open_request")
            if art:
                meta = art.get("metadata") or {}
                request_file = meta.get("request_file") or art.get("uri")
                confirm_token = meta.get("confirm_token")

        if not request_file or not confirm_token:
            return (
                f"PR request created for {run_id}, but could not extract confirm_token. "
                f"Use the CLI to approve manually: approve-open-pr --run-id {run_id}"
            )

        # Auto-approve the PR
        approve_argv = [
            "approve-open-pr",
            "--run-id", run_id,
            "--request-file", str(request_file),
            "--confirm-token", confirm_token,
            "--confirm",
        ]
        approve_result = self._run_cli(approve_argv)

        # Clean up temp file
        if body_file:
            try:
                os.unlink(body_file.name)
            except OSError:
                pass

        if approve_result["ok"]:
            # Try to get PR number from output
            try:
                approve_output = json.loads(approve_result.get("output", "{}"))
                pr_number = approve_output.get("pr_number", "?")
                return f"OK: PR #{pr_number} created for {run_id}."
            except (json.JSONDecodeError, TypeError):
                return f"OK: PR created for {run_id}."
        else:
            return f"ERROR: PR approval failed: {approve_result.get('error', 'unknown')}"

    def _read_ci(self, run_id: str, params: dict[str, Any]) -> str:
        """Sync GitHub CI status for a run."""
        result = self._run_cli(["sync-github", "--run-id", run_id])
        if result["ok"]:
            return f"GitHub sync completed for {run_id}. Use read_evidence() to see updated CI status."
        return f"ERROR: GitHub sync failed: {result.get('error', 'unknown')}"

    def _read_reviews(self, run_id: str, params: dict[str, Any]) -> str:
        """Read PR reviews from GitHub."""
        try:
            snap = self.service.get_run_snapshot(run_id)
        except KeyError:
            return f"ERROR: Run '{run_id}' not found."

        run_data = snap.get("run", snap)
        pr_number = run_data.get("pr_number")
        if not pr_number:
            return f"No PR linked to {run_id}. Create a PR first."

        workspace_dir = _resolve_workspace(run_data, self.workspace_root)
        if not workspace_dir or not workspace_dir.exists():
            return f"ERROR: Workspace not found for {run_id}."

        try:
            proc = subprocess.run(
                ["gh", "pr", "view", str(pr_number), "--json", "reviews,comments,state,statusCheckRollup"],
                capture_output=True, text=True, timeout=30,
                cwd=str(workspace_dir),
            )
            if proc.returncode == 0:
                return f"PR #{pr_number} details:\n{proc.stdout[:3000]}"
            return f"ERROR: gh pr view failed: {proc.stderr[:500]}"
        except FileNotFoundError:
            return "ERROR: 'gh' CLI not found. Install GitHub CLI."
        except Exception as exc:
            return f"ERROR: {exc}"

    def _read_pr_template(self, run_id: str, params: dict[str, Any]) -> str:
        """Read the repo's PR template."""
        try:
            snap = self.service.get_run_snapshot(run_id)
        except KeyError:
            return f"ERROR: Run '{run_id}' not found."

        run_data = snap.get("run", snap)
        workspace_dir = _resolve_workspace(run_data, self.workspace_root)
        if not workspace_dir or not workspace_dir.exists():
            return f"ERROR: Workspace not found for {run_id}."

        template_paths = [
            workspace_dir / ".github" / "PULL_REQUEST_TEMPLATE.md",
            workspace_dir / ".github" / "pull_request_template.md",
            workspace_dir / "PULL_REQUEST_TEMPLATE.md",
        ]
        for p in template_paths:
            if p.exists():
                return f"PR template found at {p.name}:\n{p.read_text()[:3000]}"
        return "No PR template found in this repo."

    def _post_comment(self, run_id: str, params: dict[str, Any]) -> str:
        """Post a comment on the PR."""
        comment_body = params.get("body", "")
        if not comment_body:
            return "ERROR: Comment body is required (params.body)."

        try:
            snap = self.service.get_run_snapshot(run_id)
        except KeyError:
            return f"ERROR: Run '{run_id}' not found."

        run_data = snap.get("run", snap)
        pr_number = run_data.get("pr_number")
        if not pr_number:
            return f"No PR linked to {run_id}."

        workspace_dir = _resolve_workspace(run_data, self.workspace_root)
        if not workspace_dir or not workspace_dir.exists():
            return f"ERROR: Workspace not found for {run_id}."

        try:
            proc = subprocess.run(
                ["gh", "pr", "comment", str(pr_number), "--body", comment_body],
                capture_output=True, text=True, timeout=30,
                cwd=str(workspace_dir),
            )
            if proc.returncode == 0:
                return f"OK: Comment posted on PR #{pr_number}."
            return f"ERROR: gh pr comment failed: {proc.stderr[:500]}"
        except FileNotFoundError:
            return "ERROR: 'gh' CLI not found. Install GitHub CLI."
        except Exception as exc:
            return f"ERROR: {exc}"

    # ── Tool 6: review_code ─────────────────────────────────────────────

    def review_code(self, run_id: str) -> str:
        """Perform deep code review using the existing review pipeline.

        Delegates to the run-code-review CLI which handles diff extraction,
        sibling file loading, checklist, and LLM review call.
        """
        result = self._run_cli(["run-code-review", "--run-id", run_id])
        if result["ok"]:
            # Read the review artifact
            review = self.service.latest_artifact(run_id, artifact_type="code_review")
            if review:
                meta = review.get("metadata") or {}
                lines = [f"Code Review for {run_id}:"]
                lines.append(f"Verdict: {meta.get('verdict', 'N/A')}")
                lines.append(f"Summary: {meta.get('summary', 'N/A')}")
                if meta.get("reference_provider"):
                    lines.append(f"Reference provider: {meta['reference_provider']}")
                if meta.get("issues"):
                    lines.append("Issues:")
                    for issue in meta["issues"]:
                        if isinstance(issue, dict):
                            lines.append(
                                f"  - [{issue.get('severity', '?')}] {issue.get('file', '?')}: "
                                f"{issue.get('description', '?')}"
                            )
                            if issue.get("suggested_fix"):
                                lines.append(f"    Fix: {issue['suggested_fix']}")
                        else:
                            lines.append(f"  - {issue}")
                if meta.get("verdict") == "CLEAN":
                    lines.append("\nCode is clean. Proceed to generate_pr_body() and github_api(create_pr).")
                else:
                    lines.append("\nCode has issues. Consider: update_state(to_state='ITERATING') and execute_worker(task='review_fix').")
                return "\n".join(lines)
            return "Code review completed but no artifact found. Check read_evidence() for details."
        return f"ERROR: Code review failed: {result.get('error', 'unknown')}"

    # ── Tool 7: generate_pr_body ────────────────────────────────────────

    def generate_pr_body(self, run_id: str) -> str:
        """Generate diff-aware PR description via LLM.

        Calls ManagerLLMClient.generate_pr_description() directly to get
        the body text. Returns the generated markdown.
        """
        try:
            snap = self.service.get_run_snapshot(run_id)
        except KeyError:
            return f"ERROR: Run '{run_id}' not found."

        run_data = snap.get("run", snap)
        workspace_dir = _resolve_workspace(run_data, self.workspace_root)
        if not workspace_dir or not workspace_dir.exists():
            return f"ERROR: Workspace not found for {run_id}."

        project_name = run_data.get("repo", "unknown")

        # Get git diff + stat
        try:
            diff_proc = subprocess.run(
                ["git", "diff", "HEAD~1..HEAD"],
                cwd=str(workspace_dir), capture_output=True, text=True, timeout=15,
            )
            diff_text = (diff_proc.stdout or "")[:4000]
            stat_proc = subprocess.run(
                ["git", "show", "--stat", "HEAD"],
                cwd=str(workspace_dir), capture_output=True, text=True, timeout=15,
            )
            diff_stat = (stat_proc.stdout or "")[:2000]
        except (OSError, subprocess.TimeoutExpired) as exc:
            return f"ERROR: Failed to get git diff: {exc}"

        if not diff_text.strip():
            return "ERROR: No diff found. Worker may not have committed changes."

        # Load repo PR template
        pr_template = ""
        for tpl_name in (".github/PULL_REQUEST_TEMPLATE.md", ".github/pull_request_template.md", "PULL_REQUEST_TEMPLATE.md"):
            tpl_path = workspace_dir / tpl_name
            if tpl_path.exists():
                try:
                    pr_template = tpl_path.read_text(encoding="utf-8")[:3000]
                except OSError:
                    pass
                break

        # Load evidence from digest file (not just DB metadata, which is sparse)
        evidence: dict[str, Any] = {}
        digest_art = self.service.latest_artifact(run_id, artifact_type="run_digest")
        if digest_art:
            digest_uri = str(digest_art.get("uri") or "").strip()
            if digest_uri and Path(digest_uri).is_file():
                try:
                    full_digest = json.loads(Path(digest_uri).read_text(encoding="utf-8"))
                    cls = full_digest.get("classification") or {}
                    evidence["grade"] = cls.get("grade", "")
                    evidence["reason_code"] = cls.get("reason_code", "")
                    val = full_digest.get("validation") or {}
                    evidence["test_commands"] = val.get("test_commands", [])
                    evidence["failed_test_commands"] = val.get("failed_test_commands", [])
                    evidence["test_command_count"] = val.get("test_command_count", 0)
                    evidence["failed_test_command_count"] = val.get("failed_test_command_count", 0)
                    chg = full_digest.get("changes") or {}
                    evidence["changed_files"] = chg.get("changed_files", [])
                    evidence["changed_files_count"] = chg.get("changed_files_count", 0)
                    evidence["added_lines"] = chg.get("added_lines", 0)
                except (json.JSONDecodeError, OSError):
                    pass
            if not evidence:
                # Fallback to sparse metadata
                meta = digest_art.get("metadata") or {}
                evidence["grade"] = meta.get("grade", "")
                evidence["reason_code"] = meta.get("reason_code", "")

        # Call LLM
        try:
            from .manager_llm import ManagerLLMClient

            client = ManagerLLMClient.from_runtime(
                api_base=None,
                model=None,
                timeout_sec=30,
                api_key_env="AGENTPR_MANAGER_API_KEY",
            )
            body = client.generate_pr_description(
                project_name=project_name,
                diff_text=diff_text,
                diff_stat=diff_stat,
                evidence=evidence,
                pr_template=pr_template,
            )
            if body:
                return f"Generated PR body:\n\n{body}"
            return "ERROR: LLM returned empty body. Use a manual body with github_api(create_pr, params={{body: '...'}})."
        except Exception as exc:
            logger.warning("LLM PR body generation failed: %s", exc)
            return (
                f"ERROR: LLM body generation failed: {exc}. "
                f"You can provide a manual body to github_api(create_pr)."
            )

    # ── Tool 8: notify_human ────────────────────────────────────────────

    def notify_human(
        self,
        message: str,
        priority: str = "normal",
    ) -> str:
        """Send notification to human operator via Telegram (if configured)."""
        logger.info("HUMAN NOTIFICATION [%s]: %s", priority, message)
        if self._telegram_sender:
            try:
                prefix = "⚠️ " if priority == "high" else ""
                self._telegram_sender(f"{prefix}[AgentPR] {message}")
                return f"OK: Notification sent via Telegram (priority={priority}): {message[:200]}"
            except Exception as exc:
                logger.warning("Telegram send failed: %s", exc)
                return f"OK: Notification logged (Telegram send failed: {exc}): {message[:200]}"
        return f"OK: Notification logged (no Telegram configured): {message[:200]}"

    # ── Tool 8b: reply_human ───────────────────────────────────────────

    def reply_human(
        self,
        message: str,
    ) -> str:
        """Reply to the current Telegram conversation."""
        logger.info("REPLY TO HUMAN: %s", message)
        if self._telegram_sender:
            try:
                self._telegram_sender(message)
                return f"OK: Reply sent via Telegram: {message[:200]}"
            except Exception as exc:
                logger.warning("Telegram reply failed: %s", exc)
                return f"OK: Reply logged (Telegram send failed: {exc}): {message[:200]}"
        return f"OK: Reply logged (no Telegram configured): {message[:200]}"

    # ── Tool 9: update_skill ────────────────────────────────────────────

    def update_skill(
        self,
        file: str,
        action: str,
        content: str,
        reason: str,
    ) -> str:
        """Update skill instruction files based on learned patterns."""
        allowed_files = {
            "forge_rules.md",
            "self_review_checklist.md",
            "code_review_checklist.md",
        }
        if file not in allowed_files:
            return (
                f"ERROR: Cannot modify '{file}'. "
                f"Allowed files: {sorted(allowed_files)}. "
                f"For SKILL.md changes, use notify_human() to request approval."
            )

        if action not in ("append_rule", "modify_rule"):
            return f"ERROR: Invalid action '{action}'. Valid: append_rule, modify_rule"

        # Find the file in skills directory
        skills_dir = self.integration_root.parent / "skills" / "agentpr-implement-and-validate" / "references"
        target_path = skills_dir / file
        if not target_path.exists():
            return f"ERROR: File not found: {target_path}"

        if action == "append_rule":
            with open(target_path, "a") as f:
                f.write(f"\n- {content}\n")
            logger.info("Appended rule to %s: %s (reason: %s)", file, content[:100], reason[:100])
        elif action == "modify_rule":
            return "ERROR: modify_rule not yet implemented. Use append_rule or notify_human() for complex changes."

        return f"OK: Updated {file}. Reason: {reason}"

    # ── Internal helpers ────────────────────────────────────────────────

    def _run_cli(self, argv: list[str]) -> dict[str, Any]:
        """Run an orchestrator CLI command as subprocess."""
        full_cmd = [
            "python3.11", "-m", "orchestrator.cli",
            *argv,
        ]
        logger.debug("Running CLI: %s", " ".join(full_cmd))
        try:
            proc = subprocess.run(
                full_cmd,
                capture_output=True,
                text=True,
                timeout=600,
                cwd=str(self.integration_root.parent),
            )
            return {
                "ok": proc.returncode == 0,
                "command": " ".join(argv),
                "returncode": proc.returncode,
                "output": proc.stdout[:8000] if proc.stdout else "",
                "error": proc.stderr[:4000] if proc.stderr else "",
            }
        except subprocess.TimeoutExpired:
            return {
                "ok": False,
                "command": " ".join(argv),
                "returncode": -1,
                "output": "",
                "error": "Command timed out (600s)",
            }
        except Exception as exc:
            return {
                "ok": False,
                "command": " ".join(argv),
                "returncode": -1,
                "output": "",
                "error": str(exc),
            }


# ── Tool schemas (OpenAI function-calling format) ──────────────────────

# All agent-addressable states (including terminal states the agent may need)
_AGENT_STATES = [
    "EXECUTING", "PUSHED", "CI_WAIT", "REVIEW_WAIT",
    "ITERATING", "PAUSED", "NEEDS_HUMAN_REVIEW",
    "DONE", "FAILED", "FAILED_TERMINAL",
]

TOOL_DEFINITIONS: dict[str, dict[str, Any]] = {
    "query_runs": {
        "type": "function",
        "function": {
            "name": "query_runs",
            "description": (
                "Query run information from the database. "
                "Returns run state, grade, PR info, and suggested next action."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {
                        "type": "string",
                        "description": "Specific run ID to query. Omit to list all.",
                    },
                    "state": {
                        "type": "string",
                        "description": "Filter by state (e.g. EXECUTING, PUSHED, CI_WAIT).",
                    },
                    "limit": {
                        "type": "integer",
                        "description": "Max runs to return (default 20, max 50).",
                    },
                },
            },
        },
    },
    "update_state": {
        "type": "function",
        "function": {
            "name": "update_state",
            "description": (
                "Transition a run to a new state. Safety checks are automatic — "
                "invalid transitions return an error with valid options."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "Run ID to transition."},
                    "to_state": {
                        "type": "string",
                        "enum": _AGENT_STATES,
                        "description": "Target state.",
                    },
                    "reason": {"type": "string", "description": "Why this transition."},
                },
                "required": ["run_id", "to_state", "reason"],
            },
        },
    },
    "read_evidence": {
        "type": "function",
        "function": {
            "name": "read_evidence",
            "description": (
                "Read worker execution evidence: grade, test results, changed files, "
                "code review results, and step attempts."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "Run ID to read evidence for."},
                    "include_diff": {
                        "type": "boolean",
                        "description": "Include git diff (large, use for code review). Default false.",
                    },
                },
                "required": ["run_id"],
            },
        },
    },
    "execute_worker": {
        "type": "function",
        "function": {
            "name": "execute_worker",
            "description": (
                "Launch a codex exec Worker. Blocks until complete. "
                "Safety: checks retry count (<3) and state (must be EXECUTING or ITERATING). "
                "After completion, use read_evidence() to check results."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "Run ID."},
                    "task": {
                        "type": "string",
                        "description": "Task type: integration (new code), ci_fix (fix CI), review_fix (address review).",
                    },
                },
                "required": ["run_id"],
            },
        },
    },
    "github_api": {
        "type": "function",
        "function": {
            "name": "github_api",
            "description": (
                "Interact with GitHub: create PR, read CI status, read reviews, "
                "read PR template, or post a comment on a PR."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "Run ID."},
                    "action": {
                        "type": "string",
                        "enum": ["create_pr", "read_ci", "read_reviews", "read_pr_template", "post_comment"],
                        "description": "GitHub action to perform.",
                    },
                    "params": {
                        "type": "object",
                        "description": (
                            "Action-specific parameters. "
                            "For create_pr: {title, body}. "
                            "For post_comment: {body}."
                        ),
                    },
                },
                "required": ["run_id", "action"],
            },
        },
    },
    "review_code": {
        "type": "function",
        "function": {
            "name": "review_code",
            "description": (
                "Deep code review on worker's changes. Uses accumulated review knowledge "
                "(7-section checklist from 17 PR reviews). Returns CLEAN or HAS_ISSUES with details."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "Run ID to review."},
                },
                "required": ["run_id"],
            },
        },
    },
    "generate_pr_body": {
        "type": "function",
        "function": {
            "name": "generate_pr_body",
            "description": (
                "Generate diff-aware PR description via LLM. Reads diff, evidence, "
                "and repo PR template. Returns the generated markdown body text. "
                "Use this before github_api(create_pr) to get a quality PR body."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "run_id": {"type": "string", "description": "Run ID."},
                },
                "required": ["run_id"],
            },
        },
    },
    "notify_human": {
        "type": "function",
        "function": {
            "name": "notify_human",
            "description": "Send notification to the human operator via Telegram.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Message to send."},
                    "priority": {
                        "type": "string",
                        "enum": ["low", "normal", "high"],
                        "description": "Priority level. Default: normal.",
                    },
                },
                "required": ["message"],
            },
        },
    },
    "reply_human": {
        "type": "function",
        "function": {
            "name": "reply_human",
            "description": "Reply to the current Telegram conversation.",
            "parameters": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Reply message."},
                },
                "required": ["message"],
            },
        },
    },
    "update_skill": {
        "type": "function",
        "function": {
            "name": "update_skill",
            "description": (
                "Update skill instruction files (forge_rules.md, self_review_checklist.md, "
                "code_review_checklist.md). For SKILL.md changes, use notify_human() instead."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "file": {
                        "type": "string",
                        "enum": ["forge_rules.md", "self_review_checklist.md", "code_review_checklist.md"],
                        "description": "File to update.",
                    },
                    "action": {
                        "type": "string",
                        "enum": ["append_rule"],
                        "description": "Action to perform.",
                    },
                    "content": {"type": "string", "description": "Rule content to add."},
                    "reason": {"type": "string", "description": "Why this rule is needed."},
                },
                "required": ["file", "action", "content", "reason"],
            },
        },
    },
}


def get_tool_schemas() -> list[dict[str, Any]]:
    """Return all tool schemas for the agent session."""
    return list(TOOL_DEFINITIONS.values())


# ── Formatting helpers ──────────────────────────────────────────────────

def _resolve_workspace(run_data: dict[str, Any], workspace_root: Path) -> Path | None:
    """Resolve workspace directory from run data."""
    # Prefer explicit workspace_dir from DB
    ws = run_data.get("workspace_dir")
    if ws:
        p = Path(ws)
        if p.exists():
            return p
    # Fallback: workspace_root / repo
    repo = run_data.get("repo", "")
    if repo:
        return workspace_root / repo
    return None


def _format_run_summary(run: dict[str, Any]) -> str:
    """One-line summary of a run."""
    run_id = run.get("run_id", "?")
    state = run.get("current_state", run.get("state", "?"))
    owner = run.get("owner", "?")
    repo = run.get("repo", "?")
    pr = run.get("pr_number")
    pr_str = f" PR #{pr}" if pr else ""

    # Suggest next action
    suggestion = _suggest_action(state, run)
    return f"- {run_id} | {owner}/{repo} | {state}{pr_str}\n  → {suggestion}"


def _format_run_detail(snap: dict[str, Any]) -> str:
    """Detailed run information. Handles both list_runs format and get_run_snapshot format."""
    run_data = snap.get("run", snap)
    state = snap.get("state", run_data.get("current_state", "?"))

    lines = [
        f"Run: {run_data.get('run_id', '?')}",
        f"Repo: {run_data.get('owner', '?')}/{run_data.get('repo', '?')}",
        f"State: {state}",
        f"Mode: {run_data.get('mode', '?')}",
        f"Created: {run_data.get('created_at', '?')}",
        f"Updated: {run_data.get('updated_at', '?')}",
    ]
    if run_data.get("pr_number"):
        lines.append(f"PR: #{run_data['pr_number']}")
    if run_data.get("branch"):
        lines.append(f"Branch: {run_data['branch']}")
    if run_data.get("workspace_dir"):
        lines.append(f"Workspace: {run_data['workspace_dir']}")

    suggestion = _suggest_action(state, run_data)
    lines.append(f"\nSuggested: {suggestion}")
    return "\n".join(lines)


def _suggest_action(state: str, run: dict[str, Any]) -> str:
    """Suggest next action based on state."""
    s = state.upper()
    if s == "QUEUED":
        return "update_state(to_state='EXECUTING'), then execute_worker()"
    elif s == "EXECUTING":
        return "execute_worker(), then read_evidence() to check results"
    elif s == "PUSHED":
        return "review_code() first. If CLEAN: generate_pr_body() → github_api(create_pr). If HAS_ISSUES: update_state(to_state='ITERATING') → execute_worker(task='fix: <issues>')"
    elif s == "CI_WAIT":
        return "github_api(action='read_ci') to check CI status"
    elif s == "REVIEW_WAIT":
        return "github_api(action='read_reviews') to check maintainer feedback"
    elif s == "ITERATING":
        return "execute_worker(task='ci_fix' or 'review_fix'), then read_evidence()"
    elif s == "NEEDS_HUMAN_REVIEW":
        return "Wait for human input or notify_human() with update"
    elif s in ("DONE", "SKIPPED", "FAILED_TERMINAL"):
        return "Terminal state — no action needed"
    elif s == "FAILED":
        return "Analyze failure via read_evidence(), then retry or escalate"
    return "read_evidence() and decide"


def _git_diff(workspace_dir: Path) -> str:
    """Get git diff from workspace."""
    try:
        proc = subprocess.run(
            ["git", "diff", "HEAD~1..HEAD", "--stat"],
            capture_output=True, text=True, timeout=10,
            cwd=str(workspace_dir),
        )
        stat = proc.stdout.strip() if proc.returncode == 0 else ""

        proc2 = subprocess.run(
            ["git", "diff", "HEAD~1..HEAD"],
            capture_output=True, text=True, timeout=10,
            cwd=str(workspace_dir),
        )
        diff = proc2.stdout.strip() if proc2.returncode == 0 else ""

        if stat or diff:
            return f"Stat:\n{stat}\n\nDiff:\n{diff}"
        return ""
    except Exception:
        return ""
