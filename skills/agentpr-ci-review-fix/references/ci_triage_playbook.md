# CI Triage Playbook

1. Identify failed check names and URLs.
2. Separate transient infra issues from deterministic code/test failures.
3. Patch one root cause at a time.
4. Re-run targeted checks first, then broader suite if required.
5. Keep changes incremental and scoped to failing signal.
6. Escalate to human when evidence is incomplete or policy conflicts appear.
