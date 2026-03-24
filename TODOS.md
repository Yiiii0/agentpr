# TODOS

## TODO-1: Add DB error handling in agent_tick()
**Priority:** High
**What:** Wrap `agent_tick()` DB calls (`service.list_runs()`) in try/except so a transient SQLite lock doesn't crash the daemon tick.
**Why:** The daemon runs 24/7. An unhandled DB exception at `agent_loop.py:175` kills the tick and may crash the loop. Only `KeyboardInterrupt` is caught today.
**Context:** `agent_loop.py:175` — `service.list_runs(limit=50)` has no error handling. Fix: wrap in try/except, return `AgentTickResult(ok=False, error=str(exc))`, let the loop continue to next tick.
**Effort:** human: ~10min / CC: ~2min
**Depends on:** Nothing
**Added:** 2026-03-24 (eng review)

## TODO-2: Async worker execution for 50+ repo scale
**Priority:** Medium (D5 phase)
**What:** Make `execute_worker` non-blocking: fire-and-forget + poll via `read_evidence()`. Let agent process other runs while workers execute.
**Why:** At 2-3 workers/tick, blocking is fine. At 50+ repos, blocking workers will exhaust the 15-turn budget on a single run.
**Context:** Explicitly deferred in eng review issue #9C. The current `subprocess.run(..., timeout=600)` at `agent_tools.py:831` blocks the entire agent session.
**Effort:** human: ~3 days / CC: ~30min
**Depends on:** D5 scale testing to validate the need
**Added:** 2026-03-24 (eng review)
