# AgentPR

AI-powered system that autonomously creates pull requests for open-source repositories. Give it a task in natural language, and it analyzes the repo, writes the code, runs tests, and opens a PR — with humans only needed for approval.

## How It Works

```
Human (Telegram)  -->  Manager (LLM Agent)  -->  Worker (Codex)  -->  GitHub PR
      |                      |                        |
  "Add Forge to             Decides what              Reads repo, writes code,
   pipecat"                 to do next                runs tests, pushes
```

**Manager** is an LLM agent with tools. It understands your task, drives the state machine, monitors progress, and notifies you when something needs attention.

**Worker** is an autonomous coding agent (OpenAI Codex). It clones the repo, analyzes its structure and conventions, implements changes following the repo's own patterns, runs the test suite, and reports results.

**Orchestrator** is a thin layer that persists state, enforces safety gates, and logs every action for auditability.

## Key Features

- **Natural language task dispatch** — describe what you want in Telegram, the system handles the rest
- **Repo-aware implementation** — Worker reads CONTRIBUTING, CI config, and existing patterns before writing code
- **Automatic validation** — runs the repo's own test/lint commands and reports real exit codes
- **Safety by default** — diff budgets, sandbox isolation, double-confirmation PR gate, merge always manual
- **Hybrid decision making** — deterministic rules for hard guardrails, LLM for semantic judgment
- **Auto-recovery** — dirty workspace detection, stale state escalation, consecutive failure protection

## Architecture

```
QUEUED --> EXECUTING --> PUSHED --> CI_WAIT --> REVIEW_WAIT --> DONE
                                      |             |
                                  ITERATING <-------+
+ PAUSED (any state)
+ NEEDS_HUMAN (escalation)
+ FAILED (terminal)
```

The system uses a **skills-based** approach. The Worker has three skills it invokes autonomously:

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

# Create and run
python3.11 -m orchestrator.cli create-run --owner <org> --repo <repo>
python3.11 -m orchestrator.cli run-manager-loop \
  --run-id <run_id> \
  --decision-mode hybrid \
  --skills-mode agentpr_autonomous \
  --codex-sandbox danger-full-access

# When PUSHED, approve PR (double confirmation)
python3.11 -m orchestrator.cli request-open-pr --run-id <run_id> --title "feat: ..."
python3.11 -m orchestrator.cli approve-open-pr --run-id <run_id> --confirm-token <token> --confirm
```

## Full System (3 processes)

```bash
# 1. Human interaction
python3.11 -m orchestrator.cli run-telegram-bot --allow-chat-id <chat_id>

# 2. Autonomous progression
python3.11 -m orchestrator.cli run-manager-loop --decision-mode hybrid

# 3. CI/review feedback
python3.11 -m orchestrator.cli run-github-webhook --port 8787
```

## Requirements

- Python 3.11
- [OpenAI Codex CLI](https://github.com/openai/codex)
- GitHub CLI (`gh`) authenticated
- OpenAI-compatible API key for Manager LLM

## Documentation

- **[Operations Guide](docs/OPERATIONS_GUIDE.md)** — full CLI reference, parameters, environment variables, deployment
- **[Master Plan](AGENTPR_MASTER_PLAN.md)** — architecture decisions, design principles, lessons learned

## Design Principles

- **ACI > Model** — tool interface design matters more than model choice ([SWE-agent, ICLR 2025](https://arxiv.org/abs/2405.15793))
- **Single agent + good tools** — no "CEO/COO" splits or intent abstraction layers
- **Deterministic infrastructure, intelligent agents** — rules for guardrails, LLM for creative work
- **One real test > ten code reviews** — validate with real repos, not hypotheticals

## License

Internal tool — TensorBlock.
