# AgentPR

AI-powered system that autonomously creates pull requests for open-source repositories. Give it a list of repos, and it analyzes each one, writes code following repo conventions, runs tests, performs deep code review, and opens PRs — with humans only observing progress via Telegram.

## North Star

1. **Human only observes.** Give 20 repos → system runs autonomously → human watches Telegram notifications.
2. **Manager (LLM) is the brain.** Understands context, makes decisions, reviews code and PRs, notifies when needed.
3. **Worker (Codex) executes autonomously.** Reads repo, writes code, runs tests, produces evidence.
4. **Safety by default.** Merge is always manual. PR creation requires either human confirm or Manager's full assessment. Diff budgets and sandbox isolation enforced.
5. **Self-improving.** Each run's failures are encoded back into skill instructions for the next run.

## How It Works

```
Human (Telegram)  -->  Manager (LLM Agent)  -->  Worker (Codex)  -->  GitHub PR
      |                      |                        |
  "/create repo1           Decides what to do,       Reads repo, writes code,
   repo2 repo3"            reviews code & PR,        runs tests, pushes
                           monitors progress
```

**Manager** is an LLM agent with tools. It drives the state machine, runs deep code review, assesses PR readiness, monitors GitHub events, and notifies you when something needs attention.

**Worker** is an autonomous coding agent (OpenAI Codex). It clones the repo, analyzes its structure and conventions, implements changes following the repo's own patterns, runs the test suite, and reports results.

**Orchestrator** is a thin infrastructure layer that persists state, enforces safety gates, and logs every action for auditability.

## Key Features

- **Batch processing** — `/create repo1 repo2 ... repo20` in Telegram, system handles all of them
- **Repo-aware implementation** — Worker reads CONTRIBUTING, CI config, and existing patterns before writing code
- **Deep code review** — Manager LLM reviews diff + full changed files + sibling reference providers + accumulated checklist from 17 real PR reviews
- **PR readiness assessment** — Manager verifies code quality, PR body quality, and template compliance before auto-creating PRs
- **Two PR modes** — human-confirm (default) or auto-create with Manager assessment (`AGENTPR_AUTO_PR=true`)
- **Event-driven** — GitHub webhooks wake the Manager immediately (no polling delay)
- **Self-improving** — test failures → encode into skill instructions → next run doesn't repeat mistakes
- **Safety by default** — diff budgets, sandbox isolation, merge always manual
- **Global progress notifications** — Telegram batch summaries every 5 minutes

## Architecture

```
Telegram Bot (long-running)  ──touch .wake──╮
                                             ├──→ Manager Loop (persistent daemon)
Webhook Server (long-running) ──touch .wake──╯         ↓
                                              SQLite DB → decide → execute → notify
```

### State Machine

```
QUEUED → EXECUTING → PUSHED → Code Review → PR Gate → CI_WAIT → REVIEW_WAIT → DONE
                        ↑                      |              |
                        └── ITERATING ←────────┘──────────────┘
+ PAUSED (any state)
+ NEEDS_HUMAN (escalation)
+ FAILED (terminal)
```

### Quality Pipeline (PUSHED → PR)

```
1. Code Review (LLM)     — reads diff + files + sibling providers + 7-section checklist
2. If HAS_ISSUES          — auto-retry via Worker (ITERATING)
3. If CLEAN + auto mode   — PR Readiness Assessment (code + body + template + principles)
4. If APPROVE             — auto-create PR
5. If NEEDS_HUMAN         — notify and wait
```

### Skills

The Worker has three skills it invokes autonomously:

1. **Preflight Contract** — analyzes repo structure, CI, contribution rules, provider patterns
2. **Implement & Validate** — writes minimal code changes, installs deps, runs tests
3. **CI Review Fix** — triages CI failures or review comments for iteration

## Quick Start

```bash
# Setup
cp .env.example .env          # fill in required vars
python3.11 -m orchestrator.cli init-db
python3.11 -m orchestrator.cli doctor --require-codex
python3.11 -m orchestrator.cli install-skills --install-curated-ci

# Create and run (single repo)
python3.11 -m orchestrator.cli create-run --owner <org> --repo <repo>
python3.11 -m orchestrator.cli run-manager-loop \
  --decision-mode hybrid \
  --skills-mode agentpr_autonomous \
  --codex-sandbox danger-full-access
```

## Full System (3 processes)

```bash
# 1. Human interaction + notifications
python3.11 -m orchestrator.cli run-telegram-bot --allow-chat-id <chat_id>

# 2. Autonomous progression (persistent daemon mode)
python3.11 -m orchestrator.cli run-manager-loop \
  --persistent \
  --decision-mode hybrid \
  --skills-mode agentpr_autonomous \
  --codex-sandbox danger-full-access

# 3. GitHub event feedback (CI results, review comments)
python3.11 -m orchestrator.cli run-github-webhook --port 8787
```

Then in Telegram:
```
/create owner/repo1 owner/repo2 owner/repo3
```

The system handles everything. You'll receive progress updates every 5 minutes and per-run notifications on key state changes.

## PR Approval Modes

### Mode 1: Human Confirm (default)

```
PUSHED → Code Review → CLEAN → [notify human] → /approve_pr → PR created
```

### Mode 2: Auto with Manager Assessment

```bash
export AGENTPR_AUTO_PR=true
```

```
PUSHED → Code Review → CLEAN → Manager PR Readiness Assessment → APPROVE → PR created
                                                                → NEEDS_HUMAN → [notify]
```

The Manager's assessment uses: code review results, worker grade/evidence, generated PR body, repo PR template, git diff, and accumulated review principles from 17 integration PRs.

## Requirements

- Python 3.11
- [OpenAI Codex CLI](https://github.com/openai/codex)
- GitHub CLI (`gh`) authenticated
- OpenAI-compatible API key for Manager LLM

## Documentation

- **[Operations Guide](docs/OPERATIONS_GUIDE.md)** — full CLI reference, parameters, environment variables, deployment
- **[Master Plan](AGENTPR_MASTER_PLAN.md)** — architecture decisions, design principles, lessons learned

## Design Philosophy

- **ACI > Model** — tool interface design matters more than model choice ([SWE-agent, ICLR 2025](https://arxiv.org/abs/2405.15793))
- **Single agent + good tools** — no multi-agent orchestration frameworks, just a Manager with well-designed tools and safety interceptors
- **Deterministic infrastructure, intelligent agents** — rules for guardrails and evidence extraction, LLM for semantic judgment and creative work
- **One real test > ten code reviews** — validate with real repos, encode failures into instructions, iterate
- **Self-improving pipeline** — every run's learnings are captured in skill instructions and review checklists, so the next run doesn't repeat the same mistakes
- **Minimal diff, maximal alignment** — Worker follows the repo's own patterns (reference provider, factory/registry, naming), not what it thinks is "better"

## Validation

22 repos tested, 19/22 PASS+PUSHED (86%). 17 PRs submitted upstream, 1 merged by maintainer. Deep review of all 17 PRs found 4 logic bugs (all fixed). Lessons encoded into code review checklist and skill instructions.

## License

Internal tool — TensorBlock.
