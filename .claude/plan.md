# P1 + P2 Implementation Plan

## P1: Notification Last Mile + Global Stats in /overview

### P1a: Manager Notification → Telegram Bridge

**Goal**: `manager_notification` artifacts (produced by `_notify_after_action` in manager_loop) get pushed to Telegram.

**File: `orchestrator/telegram_bot.py`**

1. Add `maybe_emit_manager_notifications()` function (~40 lines):
   - Query `service.list_artifacts_global(artifact_type="manager_notification", limit=50)`
   - For each artifact, check if already delivered via marker: `load_notification_markers()` with marker_key `f"mgr_notify:{artifact_id}"`
   - Build message from artifact metadata: `[{priority}] {run_id}: {message}`
   - Send to `notification_chat_ids`
   - Record marker via `record_notification_marker()` with artifact_type `"bot_mgr_notify"`

2. Wire into `run_telegram_bot_loop()` — call alongside existing `maybe_emit_state_notifications()` in the same scan interval block.

### P1b: Global Stats in /overview

**File: `orchestrator/telegram_bot.py`**

1. In `render_overview()`, add a "Global Stats" section:
   - Import and call `get_global_stats(service=service, limit=list_limit)` from `manager_tools`
   - Display: pass_rate_pct, grade_counts (top grades), top 3 reason_codes
   - ~15 lines added to render_overview

---

## P2: Review Comment Triage + Failure Diagnosis

### P2a: `triage_review_comment` LLM Tool

**File: `orchestrator/manager_llm.py`**

1. Add `ReviewCommentTriage` dataclass: `action` (fix_code/reply_explain/ignore), `reason`, `confidence`, `raw`
2. Add `triage_review_comment()` method (~50 lines):
   - Tool schema: `action` enum, `reason` string, `confidence` enum, `reply_draft` optional string
   - System prompt: fixed criteria for triage (changes_requested → likely fix, nitpick → ignore, question → reply)
   - Input: review comment body + diff summary + run context
   - Uses existing `_extract_tool_call_payload` + `_request_chat_completion`

### P2b: `suggest_retry_strategy` LLM Tool

**File: `orchestrator/manager_llm.py`**

1. Add `RetryStrategy` dataclass: `should_retry`, `target_state`, `modified_instructions`, `reason`, `confidence`, `raw`
2. Add `suggest_retry_strategy()` method (~50 lines):
   - Tool schema: `should_retry` bool, `target_state` string, `modified_instructions` string, `reason` string, `confidence` enum
   - System prompt: analyze failure evidence, decide if retry is worthwhile and with what adjustments
   - Input: reason_code, classification, worker output evidence, attempt count

### P2c: Wire Triage into ITERATING Decision

**File: `orchestrator/manager_loop.py`**

1. Add `_triage_iterating_run()` method (~30 lines):
   - Load latest event from `service.list_events(run_id, limit=1)`
   - If review event: extract comment body from event payload
   - Call `llm_client.triage_review_comment()` if LLM available
   - Return triage result (or None if LLM unavailable)

2. Add `latest_review_triage` field to `ManagerRunFacts` in `manager_decision.py`

3. In `_build_run_facts()`: call `_triage_iterating_run()` when state is ITERATING and LLM available

**File: `orchestrator/manager_decision.py`**

4. Update ITERATING decision: if `facts.latest_review_triage == "ignore"` → NOOP/WAIT_HUMAN; if `"fix_code"` → RUN_AGENT_STEP; if `"reply_explain"` → WAIT_HUMAN

### P2d: Wire Retry Strategy into FAILED Decision

**File: `orchestrator/manager_loop.py`**

1. In `_build_run_facts()`: when state is FAILED and LLM available, call `llm_client.suggest_retry_strategy()` with failure evidence
2. Add `retry_strategy` field to `ManagerRunFacts`

**File: `orchestrator/manager_decision.py`**

3. Update FAILED decision: if `facts.retry_strategy` exists and `should_retry=False` → WAIT_HUMAN; if `should_retry=True` → RETRY with strategy's target_state

---

## File Change Summary

| File | Changes |
|------|---------|
| `manager_llm.py` | +2 dataclasses, +2 methods (~100 lines) |
| `manager_decision.py` | +2 fields on ManagerRunFacts, update ITERATING + FAILED decisions (~30 lines) |
| `manager_loop.py` | +1 triage method, update _build_run_facts for triage + retry strategy (~40 lines) |
| `telegram_bot.py` | +1 notification bridge function, update render_overview + bot loop (~60 lines) |

Total: ~230 lines new code across 4 files.
