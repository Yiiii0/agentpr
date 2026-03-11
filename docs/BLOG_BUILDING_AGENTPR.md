# Building AgentPR: 9 Days, 22 Repos, 64 Lessons on AI-Driven Code Contributions

> How we built an autonomous system that analyzes open-source repos, writes integration code, reviews its own work, and submits pull requests — and what we learned about making AI agents actually work.

---

## The Problem

TensorBlock's [Forge](https://github.com/TensorBlock/forge) is an open-source middleware for unified AI model access — one OpenAI-compatible API routing to 40+ providers and thousands of models. To grow adoption, we needed to submit integration PRs to dozens of open-source projects. Each PR means: read the repo, understand conventions, write a new provider class, run tests, write the PR body, submit, and follow up on reviews.

One PR takes 2-4 hours of focused human work. Fifty PRs means 100-200 hours.

We asked: what if an AI system could do this end-to-end, with humans only watching?

---

## What We Built

AgentPR is a three-process system: a Telegram bot for human interaction, a persistent Manager daemon that orchestrates everything, and a GitHub webhook server for event feedback. The Manager is an LLM agent with tools; the Worker is OpenAI Codex running in a sandbox.

```
Human (Telegram)  →  Manager (LLM Agent)  →  Worker (Codex)  →  GitHub PR
```

You type `/create owner/repo1 owner/repo2` in Telegram. The system clones each repo, analyzes its structure and coding patterns, writes minimal integration code following the repo's own conventions, runs the test suite, does a deep code review, generates a context-aware PR description, and opens the PR. Then it monitors CI and review comments, iterating if needed.

**Results after 9 days of development:**
- 22 repos tested, 19/22 PASS (86%)
- 17 PRs submitted to real open-source projects
- 1 merged by a maintainer
- 4 logic bugs caught by our own code review gate (all fixed before PR)
- Average worker attempts per repo: 1.0 (first try success)

---

## The 5 Big Ideas

### 1. Tool Interface Design > Model Choice

This is the single most impactful lesson. It comes from the SWE-agent paper (ICLR 2025): the quality of the tools you give an AI agent matters more than which model you use.

Early on, our Worker was failing ~60% of the time. Not because the model was bad, but because the tool interfaces were ambiguous. The `target_state` parameter accepted free-form strings, so the Worker would write "push the code" instead of `PUSHED`. The feedback on errors was vague ("state transition failed"), so the Worker couldn't self-correct.

We fixed this by:
- Constraining `target_state` to an enum with exactly the valid values
- Auto-deriving fields the Worker shouldn't have to specify
- Making error messages actionable ("Expected PUSHED, got 'push the code'. Valid values: QUEUED, EXECUTING, PUSHED, ...")

Success rate jumped to 86% without changing the model.

### 2. Deterministic Infrastructure, Intelligent Agents

We tried the "let LLM decide everything" approach first. It was fragile. The LLM would sometimes grade a clearly failed run as PASS ("well, it tried hard"). It would skip safety checks because it "understood the intent."

The fix: **rules handle guardrails and evidence extraction, LLM handles semantic judgment.** This hybrid is the right architecture for production AI systems.

Concretely:
- Rules extract evidence: test command count, lint results, diff size, exit code
- Rules enforce hard guardrails: diff over budget → FAIL, period. LLM cannot override.
- LLM handles edge cases: "exit_code=1 but all tests passed — was it just a warning?"

The guardrail principle: **LLM can upgrade UNKNOWN to PASS, but cannot override FAIL.** One-directional safety.

### 3. The Self-Improvement Loop

Every run that fails teaches the system something. But only if you capture the lesson and encode it back into the instructions.

We built a pattern:
1. Grading detects a failure pattern (e.g., `missing_test_evidence`)
2. System matches it against `SKILL_IMPROVEMENT_PATTERNS`
3. Generates a proposal: "Strengthen fallback validation: if install fails, still try pytest"
4. Safety tier check: low-risk files (checklists, rules) → auto-append + git commit; high-risk files (core prompts) → human approval required
5. Next Worker run reads the updated instructions → doesn't repeat the mistake

This is not hypothetical. After discovering that Workers would give up validation after a failed `pip install`, we added a rule to the skill instructions: "A failed attempt is better than no attempt. Always try pytest even if install fails." Subsequent runs stopped exhibiting this behavior.

### 4. PASS Rate ≠ Merge Rate

This was our most expensive lesson.

We spent days optimizing the pipeline: better grading, better prompts, better test detection, better diff budgets. We got to 86% PASS rate. Felt great.

Then we looked at merge rate: 1 out of 17 PRs merged. 5.9%.

The realization: **we were optimizing the wrong metric.** A technically perfect PR that adds a provider nobody asked for to a project whose maintainer is inactive... is still zero value. The bottleneck shifted from "can the pipeline produce good code" to "will a human merge it."

This is a product-value problem, not an engineering problem. More pipeline features won't fix it. What would fix it: better repo targeting (only submit to repos with active maintainers and clear extension points), better PR framing (solve a problem the maintainer cares about), and follow-up automation (polite bumps when there's no response).

### 5. The "Factory Worker" Anti-Pattern

Our initial instinct was to build a sophisticated orchestrator that micromanages the Worker: inject skills at specific steps, validate intermediate outputs, control the execution flow.

This was wrong. The better model is: **lightweight production line + autonomous worker + critical checkpoints.** Give the Worker good instructions (skills) and let it figure out the execution. Check the output at key points (grading, code review, PR gate). Don't try to control every step.

Why? Because the Worker's context window is precious. Every instruction you inject reduces the space for actual code understanding. Skills work better as self-contained references the Worker can consult when needed, not as step-by-step scripts the orchestrator feeds in sequence.

---

## Architecture Deep Dive

### The 9 LLM Methods

All LLM calls in the system are stateless 1-shot invocations. No conversation memory across ticks. Context is rebuilt from the database each time.

| Method | Purpose | Value |
|--------|---------|-------|
| `decide_action` | State machine: what to do next | Medium (rules handle 90%) |
| `grade_worker_output` | Semantic grading of worker results | Medium |
| `explain_decision_card` | Human-readable Telegram notifications | Low |
| `decide_bot_action` | Natural language → command routing | Low |
| `triage_review_comment` | Classify maintainer review comments | Medium |
| `suggest_retry_strategy` | Diagnose failures, plan retries | Medium |
| **`generate_pr_description`** | **Diff-aware PR body generation** | **High** |
| **`review_code_changes`** | **Deep code review** | **High** |
| **`assess_pr_readiness`** | **PR quality final assessment** | **High** |

Key insight: **only 3 of 9 methods directly impact PR quality.** The rest are classifiers where rules+heuristics already work fine. If you're building an AI agent system, figure out which LLM calls actually create value and invest there.

The stateless design has real trade-offs. Pro: any crash is recoverable, no state corruption, ticks are independent. Con: the Manager can't reason across ticks ("I tried X last time and it failed, so let me try Y"). We mitigate this with artifacts — structured data stored in the DB that gets included in the next tick's context.

### Quality Pipeline

The path from Worker output to PR has four gates:

1. **Worker Self-Review** — checklist items encoded in skill instructions
2. **Hybrid Grading** — rules extract evidence + hard guardrails; LLM handles edge cases
3. **Code Review** — Manager LLM reads diff + full files + reference providers + 7-section checklist accumulated from 17 real PR reviews
4. **PR Readiness Assessment** — final check before auto-creating PR

This pipeline caught 4 real logic bugs in 17 PRs:
- **DAMO-ConvAI**: Worker changed `Literal[...]` to `Union[Literal[...], str]`, breaking `get_args()` validation
- **pipecat**: Unnecessary `os.getenv()` fallback that deviated from OpenRouter pattern
- **octotools**: Missing factory routing — Forge models with certain prefixes would ValueError
- **weave**: elif chain ordering bug that would silently break 937 existing models (66% of the model registry)

The weave bug is instructive. The Worker added a Forge detection branch (`elif "/" in model_name:`) in the middle of an elif chain. Because elif branches are mutually exclusive, this intercepted all 937 models with `/` in their name before they could reach the `model_info` branch. A single `elif` in the wrong position, affecting 66% of an existing model registry. Our code review caught it; the Worker could not fix it (code structure rearrangement is beyond current Worker capability).

### The Worker's Blind Spot

Workers (LLM coding agents) have a consistent behavioral pattern: they are excellent at **adding new code** and terrible at **restructuring existing code.**

When a repo has a clean extension point (registry, factory, config entry), the Worker just fills in parameters — no room for error. When the Worker needs to modify existing code (add an elif branch, change a function signature), it starts "improving" — adding validation that doesn't exist elsewhere, changing types, inserting unnecessary error handling.

We found this correlates strongly with repo architecture:

| Repo Pattern | Worker Behavior | Code Quality |
|-------------|----------------|--------------|
| Registry/factory (fill parameters) | Follows pattern exactly | Clean |
| New class (independent code) | Starts "creating" | Mixed |
| Modify existing function (add elif) | Starts "improving" | Bugs |

This has direct implications for repo targeting: prefer repos with clear extension mechanisms.

---

## What Surprised Us

### Hybrid Grading Was the Right Call From Day One

We debated whether to use pure LLM grading or pure rules. The hybrid approach — rules for evidence extraction and hard guardrails, LLM for semantic judgment — turned out to be exactly right. LLMs are too "sympathetic" for enforcement (they'll rationalize a FAIL into a PASS), but rules can't handle edge cases ("exit code 1 but it was just a deprecation warning").

### The Self-Review Checklist Was the Cheapest Quality Win

Adding a 10-item self-review checklist to the Worker's skill instructions — things like "did you mutate os.environ?" and "did you initialize all variables before conditionals?" — was perhaps 20 lines of markdown and eliminated an entire class of bugs. The ROI of structured self-checking is enormous.

### PR Bodies Matter More Than Code

Multiple maintainers never looked at the code because the PR body put them off. Marketing language ("revolutionary gateway"), fabricated usage examples (showing internal API functions as user-facing), and inflated statistics ("200+ models" when it's 4,990) all signal "this is spam, not a real contribution."

The fix: generate PR bodies from actual diff content, use maintainer-facing language, and verify every number against source documentation. Lead with what the code does, not what the product is.

### Our "Correction" Made Things Worse

We once "corrected" a PR body that said "40+ providers" to "77 providers" — because we thought 40+ was wrong. We never verified 77 against the source. Turns out the original "40+" was approximately correct (README lists 42, API docs show 38 with model IDs). Our correction introduced a factual error that we then propagated to multiple PRs.

Lesson: don't correct something unless you've verified your correction against the primary source.

---

## The Numbers

| Metric | Value |
|--------|-------|
| Development time | 9 days |
| Repos tested | 22 |
| PASS rate | 86% (19/22) |
| PRs submitted | 17 |
| PRs merged | 1 (5.9%) |
| Logic bugs caught by code review | 4 |
| Logic bugs caught by Worker | 0 |
| Average worker attempts | 1.0 |
| LLM methods | 9 (3 high-value) |
| Skills | 3 |
| Accumulated insights | 64 |
| Lines of Python | ~5,000 |

---

## What We'd Do Differently

1. **Start with repo targeting.** We optimized the pipeline before asking "which repos would actually merge our PRs?" Should have done repo assessment first — active maintainers, clear extension points, recent provider additions.

2. **Code review gate from day one.** We added it at D3.9 after finding bugs in already-submitted PRs. Should have been there from the start. Quantitative grading (tests passed, diff size) is not qualitative review (logic correctness).

3. **Less time on classifiers, more on generators.** 6 of our 9 LLM methods are classifiers where rules already work. We should have spent that time improving the 3 high-value methods (PR description, code review, readiness assessment).

4. **PR body is a product, not an afterthought.** We treated it as template concatenation until D3.6. The PR body is often the only thing a maintainer reads before deciding whether to engage. It deserves first-class treatment.

---

## Current Limitations and What's Next

**Limitations:**
- Manager has no cross-tick memory (stateless 1-shot design)
- Worker can't restructure existing code
- Only suitable for structured, repetitive integration work (not creative feature development)
- Merge rate (5.9%) is a product-value problem, not solvable with more engineering

**Next steps:**
- Scale to 50 repos with better targeting (registry/factory repos only, active maintainers)
- Add repo integrability assessment (new LLM method: "should we even try this repo?")
- Harvest existing 17 PRs (sign CLAs, bump stale PRs, respond to reviews)
- Publish case studies from successful merges

---

## The Meta-Lesson

Building an AI agent system is not primarily about the AI. It's about:

1. **Observability** — you need to see exactly what the agent did, why, and what evidence it produced
2. **Safety boundaries** — decide upfront what the agent can and cannot do, enforce with rules not trust
3. **Fast iteration on real data** — one real test is worth ten code reviews; encode every failure into instructions
4. **Knowing your metric** — we chased PASS rate when merge rate was the real goal; measure what matters

The AI is the easy part. The system around it — the state machine, the safety gates, the quality pipeline, the self-improvement loop, the observability layer — that's what makes it work in production.

---

*Built at TensorBlock. March 2026.*
*Architecture: Python 3.11 + OpenAI Codex + SQLite + Telegram + GitHub Webhooks.*
*References: [SWE-agent (ICLR 2025)](https://arxiv.org/abs/2405.15793), [OpenHands V1 SDK](https://arxiv.org/abs/2511.03690)*
