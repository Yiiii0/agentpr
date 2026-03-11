# Manager Agent 重构：架构设计文档

> 创建时间：2026-03-10
> 状态：设计阶段，待对齐认知
> 目的：将 Manager 从 "9 x 1-shot LLM + Python 规则引擎" 升级为 "真正的 Agent"

---

## 0. 为什么要做这个

### 从数据看

- 22 repos 测试，19/22 PASS (86%)。Worker 层没问题。
- 17 PRs 提交，1 merged (5.9%)。Manager 层的智能是瓶颈。
- 9 个 LLM 方法中只有 3 个高价值（#7 #8 #9），其余是分类器，rules 已够用。
- Manager 每次 tick 从零开始，无跨 tick 推理，无对话能力，无策略调整。

### 从架构看

- 20K 行 Python，其中 ~40% 是 "plumbing"（tool parsing, decision routing, prompt templating），已被 2026 年的 agent framework 和 API 能力 commoditize。
- 9 x 1-shot 是 "the worst middle ground"（研究结论）：花了 LLM 的钱但没得到 LLM 的智能。
- 当前 Manager 是 "每次从零开始的顾问"，不是 "持续跟进的大脑"（insight #57）。

### 从历史看

这不是新想法。2026-02-27 的目标架构（0228 archive Section 4）就是 Manager Agent with tools：

```
Manager Agent (LLM with tools — 系统大脑)
  |-- tool: create_run / execute_worker / analyze_worker_output
  |-- tool: triage_review_comment / suggest_retry_strategy / notify_user
Orchestrator (薄层：状态持久化 + gate 执法 + 事件日志)
Worker Agent (codex exec — 自主执行，内部管理阶段)
```

当时妥协为 9 x 1-shot 的原因（Section 5）：
1. "Manager 需要低延迟、低成本、可控 schema" — 2026 年 2 月 Agent SDK 不成熟
2. "采用中间态最优：LLM 决策 + 硬约束执行 + 人工 gate" — 先验证 Worker 能不能干活
3. "不是缺多 agent 并发，而是缺单 run 高质量稳定完成率" — 优先级正确

**中间态完成了它的使命**（86% PASS），但也变成了永久态。现在 Worker 已验证，Agent 生态已成熟，是时候回到原始设计了。

---

## 1. 第一性原理

### 1.1 AgentPR 在做什么

AgentPR 是一个 **自主代码贡献系统**：接收 repo 列表 → 分析 repo → 写集成代码 → 测试 → 审查 → 提交 PR → 处理反馈。

这个流程有两个本质不同的阶段：

| 阶段 | 性质 | 需要的能力 | 当前解决方案 |
|------|------|-----------|-------------|
| **代码执行**（分析 repo、写代码、跑测试） | 深度、单次、需要完整 repo context | 代码理解 + 生成 + 执行 | Worker (codex exec + skills) ✅ 已验证 |
| **策略协调**（决策、审查、交互、学习） | 广度、持续、需要全局 context | 推理 + 判断 + 沟通 | Manager (9 x 1-shot) ❌ 不够智能 |

Worker 已经是一个真正的 agent（在 codex exec sandbox 里自主运行）。Manager 需要成为与之匹配的另一个 agent。

### 1.2 Manager 的本质

Manager 是一个 **决策者**。它的输入是：
- 事件（webhook、Telegram 消息、定时器）
- 状态（DB 里的 run 信息、artifacts、evidence）
- 策略（安全规则、质量标准）

它的输出是：
- 行动（启动 Worker、创建 PR、通知人类、重试）
- 判断（代码质量、PR 就绪度、repo 可集成性）
- 沟通（自然语言回复、进度汇报）

**这就是一个 agent with tools。** 不需要更复杂的架构。

### 1.3 设计哲学

从 64 条 insights + 外部研究中提炼的核心原则：

**P1: 简单即正确。** "不要过度设计分层。没有成功的 agent 系统用意图抽象层。单 agent + 好工具 + 安全拦截器。"（insight #25）。Agent loop 是 30 行代码。复杂度应该在工具里，不在架构里。

**P2: 安全在工具层，不在 agent 层。** Agent 可以尝试任何操作，工具层拒绝非法操作并返回清晰错误。这和 OpenHands SecurityAnalyzer、SWE-agent guardrails 一致。Agent 看到 error 后自行调整策略——这比 rules engine 预先限制选项更聪明。

**P3: 确定性基础设施 + 智能决策。** "AI centric ≠ all LLM。基础设施应该是确定性的，Agent 应该是智能的。"（insight #46）。状态持久化、事件幂等、diff budget 是确定性的。质量判断、策略制定、人类沟通是智能的。

**P4: Workflow-Constrained Agent Loop。** 宏观阶段（QUEUED → EXECUTING → PUSHED → ...）是确定性的。每个阶段内的决策是智能的。这是 2026 年验证的最优模式。

**P5: 工具接口设计 > 模型能力。** "ACI 设计对 agent 性能的影响大于模型选择。"（insight #24, SWE-agent ICLR 2025）。给 agent 好的工具比换更强的模型更有效。

**P6: Context is built, not maintained.** 每次 agent session 从 DB 重建 context。不维护跨 session 的对话状态。这保留了无状态设计的核心优势（可重启、可恢复、failure-safe）（insight #58），同时在 session 内提供完整的推理能力。

---

## 2. 现状盘点

### 2.1 已验证的资产（保留）

| 资产 | 验证数据 | 行数 | 去向 |
|------|---------|------|------|
| Worker (codex exec + skills) | 86% PASS, avg 1.0 attempt | ~780 (skills.py) + 3 skills | 不变 |
| 状态持久化 (SQLite) | 22 runs, 0 数据丢失 | ~656 (db.py) + ~667 (service.py) | 不变 |
| 状态机校验 | 0 非法转移 | ~200 (state_machine.py) | 不变，变成 tool safety layer |
| Worker 执行器 | 19/22 成功执行 | ~559 (executor.py) | 不变，变成 agent tool |
| Preflight 检查 | Python+JS+混合项目 | ~495 (preflight.py) | 不变 |
| GitHub webhook | CI/review 事件接收 | ~546 (github_webhook.py) | 保留，连接到 agent event router |
| PR gate 逻辑 | 17 PRs 双确认 | ~285 (cli_pr.py) | 保留核心逻辑，变成 tool safety |
| Safety gates | diff budget, retry limit, sandbox | 分散 | 集中到 tool implementations |
| Skills 管理 | auto-upgrade, self-improvement | ~780 (skills.py) | 保留 |
| 质量链知识 | 7 review principles, 9 systemic issues, forge_rules, self_review_checklist | docs + skills/ | 保留，作为 agent context |

### 2.2 要替换的部分

| 组件 | 行数 | 替换原因 |
|------|------|---------|
| manager_llm.py | 1,637 | 9 个独立 prompt template → agent 内在推理 |
| manager_decision.py | 403 | rules engine → agent 决策 |
| manager_agent.py | 220 | hybrid wrapper → 真正的 agent |
| manager_loop.py | 1,313 | Python tick loop → agent session dispatch |
| manager_tools.py | 473 | analyze_worker_output 等 → agent tools |
| telegram_bot.py 大部分 | ~1,500 | 命令路由+NL路由 → agent 天然理解 |
| runtime_analysis.py 部分 | ~800 | LLM grading 相关 → agent 直接判断 evidence |
| cli.py 部分 | ~1,000 | manager loop CLI → agent session CLI |
| **合计** | **~7,300** | |

### 2.3 新增部分（预估）

| 组件 | 预估行数 | 说明 |
|------|---------|------|
| Agent tool definitions | ~500 | 8-10 个 tools，每个 ~50 行 |
| Agent session launcher | ~300 | 事件 → context → agent loop → result |
| Context builder | ~200 | DB state + artifacts → agent prompt |
| System prompt | ~100 | Agent 的身份、规则、策略 |
| Thin Telegram router | ~200 | 消息 → event → agent |
| **合计** | **~1,300** | |

**净效果：20K → ~14K（删 7.3K + 加 1.3K），核心减少 6K 行 plumbing。**

### 2.4 约束变化（与旧版比）

| 约束 | 旧版（0228） | 当前实际 | 新设计 |
|------|------------|---------|--------|
| PR 创建 | 必须人工双确认 | 双模式（human-confirm / auto with assessment） | **⚡ 待定：见 Decision Point 1** |
| Manager 调用 shell | 禁止 | 禁止 | 禁止（agent 通过 tools 操作） |
| Diff budget | 硬约束 | 硬约束 | 硬约束（在 tool 层） |
| Retry 上限 | 硬约束 | 硬约束 | 硬约束（在 tool 层） |
| Sandbox | danger-full-access + workspace 范围检查 | 同左 | 同左（Worker 不变） |
| Manager 决策模式 | rules / llm / hybrid | hybrid（rules 优先，LLM 可降级） | Agent 自主决策，tools 执行安全校验 |

---

## 3. SDK 选择

### 3.1 候选对比

| 维度 | Anthropic API 直接用 | Claude Agent SDK | Claude Code Headless | LangGraph | OpenAI Agents SDK |
|------|---------------------|-----------------|---------------------|-----------|------------------|
| 实现复杂度 | 最低（30 行 loop） | 低 | 中（MCP server） | 高 | 低 |
| 控制粒度 | 完全控制 | 高 | 低（黑盒） | 高 | 高 |
| 工具接入 | Python functions | Python functions | MCP server | LangChain adapters | Python functions |
| 成本控制 | 精确 | 精确 | 不精确 | 精确 | 精确 |
| Context 管理 | 自己做（简单） | 框架帮做 | 内置 compaction | 内置 checkpointing | 自己做 |
| 启动延迟 | <1s | <1s | ~5s | <1s | <1s |
| 额外依赖 | anthropic SDK（已有） | claude-agent-sdk | claude CLI binary | langchain+langgraph | openai SDK |
| Model 锁定 | Anthropic | Anthropic | Anthropic | 不锁定 | OpenAI |
| 适合场景 | 精确控制的 tool-calling agent | 通用 agent | 需要 shell/file 的 coding agent | 复杂状态图 | 多 agent 协作 |

### 3.2 决定：继续用 OpenAI-compatible `/chat/completions` + tool-calling（现有 API）

**核心发现：不需要换 API。**

当前 `manager_llm.py` 已经在用 OpenAI-compatible `/chat/completions` 格式（line 603），通过 `AGENTPR_MANAGER_API_BASE` 指向 Forge 或其他 provider。Tool-calling（function calling）是 `/chat/completions` 的标准功能。

**从 1-shot 到 agent loop 的唯一变化：加一个 for 循环。**

```python
# 当前：1-shot
payload = {"model": model, "messages": messages, "tools": tools}
data = _request_chat_completion(payload)  # 一次调用

# 新的：agent loop（同一个 API，同一个函数，加循环）
for turn in range(max_turns):
    payload = {"model": model, "messages": messages, "tools": tools}
    data = _request_chat_completion(payload)  # 同一个函数！
    message = data["choices"][0]["message"]
    messages.append(message)
    if not message.get("tool_calls"):
        return message["content"]  # 没有 tool call → agent 完成
    for tc in message["tool_calls"]:
        result = execute_tool(tc["function"]["name"], tc["function"]["arguments"])
        messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})
```

API 端点、格式、认证、provider 全不变。不需要新 API key。不需要 Anthropic SDK。不需要换 provider。

**已验证（2026-03-10）：Forge `/chat/completions` 多轮 tool-calling 测试通过。** 测试了 3 个模型：

| 模型 | 多轮 tool-calling | 结果 |
|------|-------------------|------|
| `tensorblock/gpt-4o` | ✅ Round 1 tool_call + Round 2 text response | PASS |
| `tensorblock/claude-sonnet-4` | ✅ Round 1 tool_call + Round 2 text response | PASS |
| `tensorblock/gemini-2.5-flash` | ✅ Round 1 tool_call + Round 2 text response | PASS |

模型选择不锁定任何 provider。通过 Forge 路由，统一用 OpenAI `/chat/completions` 格式，Forge 自动处理 Anthropic/Google 等 provider 的格式转换。

**为什么不用 agent 框架？**

| 框架额外能力 | 我们需要吗？ | 原因 |
|-------------|------------|------|
| Graph-based 状态管理 (LangGraph) | 不需要 | 状态在 SQLite，10 个状态的线性流程 |
| Checkpointing / session 恢复 (LangGraph) | 不需要 | Session 短暂（max 15 turns），state 在 DB |
| Shell 执行 + File 编辑 (Claude Code headless) | 不需要 | Manager 通过 Python tool functions 操作（包括 skill 文件更新）|
| Context compaction (Claude Code) | 不需要 | Session 不会撑满 context window |
| Multi-agent handoffs (OpenAI SDK) | 不需要 | 单 agent |

**结论：给 30 行 loop 代码套一个重型框架是 over-engineering（insight #25）。** Chat/completions API 本身已经是 agent 框架——提供 tool schema 校验、tool call 解析、multi-turn conversation。我们只加循环逻辑。

如果将来需求变复杂（graph 分支、long-running sessions、multi-agent），可以迁移到框架。先用最简单的方案验证。

### 3.3 Agent Loop 实现（核心代码，~30 行）

使用 OpenAI `/chat/completions` 格式，通过 Forge 路由到任意模型：

```python
def run_agent_session(
    *,
    system_prompt: str,
    context: str,      # 从 DB 构建的 run/event context
    tools: list[dict], # tool definitions (OpenAI function-calling JSON schema)
    tool_executor: Callable,  # tool name + args → result string
    model: str | None = None,   # 默认从 AGENTPR_MANAGER_MODEL 读取
    max_turns: int = 15,
) -> str:
    """Run a bounded agent session via Forge /chat/completions. Returns final text response."""
    model = model or os.environ.get("AGENTPR_MANAGER_MODEL", "tensorblock/gpt-4o")
    messages = [
        {"role": "system", "content": system_prompt},
        {"role": "user", "content": context},
    ]

    for turn in range(max_turns):
        payload = {"model": model, "messages": messages, "tools": tools, "temperature": 0}
        data = _request_chat_completion(payload)  # 复用现有函数
        message = data["choices"][0]["message"]
        messages.append(message)

        # If no tool calls, agent is done
        if not message.get("tool_calls"):
            return message.get("content", "")

        # Execute tool calls, append results
        for tc in message["tool_calls"]:
            result = tool_executor(tc["function"]["name"], json.loads(tc["function"]["arguments"]))
            messages.append({"role": "tool", "tool_call_id": tc["id"], "content": result})

    return "Max turns reached. Escalating to human."
```

零新依赖。复用现有 `_request_chat_completion()`，通过 Forge 路由到 GPT-4o / Claude / Gemini / 任意模型。

---

## 4. 架构设计

### 4.1 整体架构

```
┌──────────────────────────────────────────────────┐
│                 Event Sources                     │
│                                                    │
│  Telegram Bot ──→ message event                   │
│  GitHub Webhook ──→ ci/review event               │
│  Timer (cron) ──→ periodic check event            │
│  CLI ──→ manual event                             │
└──────────────┬───────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────┐
│            Event Router (Python, thin)            │
│                                                    │
│  event → determine scope (which run? global?)     │
│  → build context from DB                          │
│  → launch agent session                           │
│  → process agent output (notifications, state)    │
└──────────────┬───────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────┐
│         Manager Agent Session                     │
│  (Forge /chat/completions + tool-calling loop)    │
│                                                    │
│  Input: system prompt + event context             │
│  Loop: reason → call tools → observe → repeat     │
│  Output: text response + tool side effects        │
│  Bounded: max_turns (default 15)                  │
│                                                    │
│  Tools (8-10):                                    │
│  ┌────────────────────────────────────────┐      │
│  │ State Management                       │      │
│  │  query_runs()     — 查询 run 列表/详情  │      │
│  │  update_state()   — 状态转移（含安全校验）│      │
│  │                                        │      │
│  │ Worker Control                         │      │
│  │  execute_worker() — 启动 codex exec    │      │
│  │  read_evidence()  — 读取 worker 产出    │      │
│  │                                        │      │
│  │ GitHub Operations                      │      │
│  │  github_api()     — PR/CI/review 操作   │      │
│  │                                        │      │
│  │ Quality Assessment                     │      │
│  │  review_code()    — 深度 code review   │      │
│  │  generate_pr_body() — 生成 PR 描述     │      │
│  │                                        │      │
│  │ Communication                          │      │
│  │  notify_human()   — Telegram 通知      │      │
│  │  reply_human()    — 回复 Telegram 消息  │      │
│  │                                        │      │
│  │ System                                 │      │
│  │  get_policy()     — 读取安全策略/阈值   │      │
│  └────────────────────────────────────────┘      │
└──────────────┬───────────────────────────────────┘
               │ tool calls (with safety checks)
               ▼
┌──────────────────────────────────────────────────┐
│        Infrastructure Layer (Python)              │
│        (deterministic, safety-enforcing)           │
│                                                    │
│  Tool implementations with embedded safety:       │
│                                                    │
│  update_state():                                  │
│    → state_machine.validate(current → target)     │
│    → if invalid: return ERROR message             │
│    → db.update + event log                        │
│                                                    │
│  execute_worker():                                │
│    → retry_count < MAX_RETRIES check              │
│    → sandbox enforcement                          │
│    → codex exec launch                            │
│    → return: "Worker started. ID=xxx"             │
│                                                    │
│  github_api(action="create_pr"):                  │
│    → pr_gate check (approval status)              │
│    → diff_budget check                            │
│    → DoD check (evidence threshold)               │
│    → if any fail: return ERROR + reason           │
│                                                    │
│  Persistence: SQLite (runs, events, artifacts)    │
│  Audit: decision_log (every agent session)        │
└──────────────┬───────────────────────────────────┘
               │
               ▼
┌──────────────────────────────────────────────────┐
│        Worker (codex exec + skills)               │
│        (unchanged, each execution isolated)       │
│                                                    │
│  Skill-1: repo-preflight-contract                 │
│  Skill-2: implement-and-validate                  │
│  Skill-3: ci-review-fix                           │
└──────────────────────────────────────────────────┘
```

### 4.2 Event-Driven Agent Sessions

Manager 不是 persistent agent。它是 **event-driven**：

```
事件到达 → 构建 context → 启动 agent session (max_turns=15) → session 结束 → 等待下一事件
```

每个 session 是独立的。Session 内 agent 可以多轮推理（和现在的 1-shot 是质的区别），但 session 之间没有对话状态。

**为什么不做 persistent agent？**
- insight #58: "无状态设计的优点不应被低估。可重启、可恢复、failure-safe。"
- 如果 agent 崩溃，不丢失任何状态（因为 state 在 DB）
- 每次 session 可以用最新的 DB 状态，不会有 stale context 问题
- 更容易调试和审计（每个 session 是独立的 transcript）

**Session 内有什么？**
- Agent 可以连续调用多个 tools：先查状态 → 再读 evidence → 再做 code review → 再决定创建 PR
- Agent 可以根据 tool 返回的 error 调整策略："retry limit exceeded → notify human instead"
- Agent 可以自然语言推理："这个 repo 有 factory pattern，Worker 代码质量应该不错，我先看 diff"

这就是 1-shot 做不到但 agent session 做得到的事情。

### 4.3 Context Building

每个 session 开始时，从 DB 构建 context：

```python
def build_context(event) -> str:
    """Build agent context from DB state and event."""
    parts = []

    # 1. Event description
    parts.append(f"## Event\n{event.description}")

    # 2. If run-specific: run state + recent artifacts
    if event.run_id:
        run = db.get_run_snapshot(event.run_id)
        parts.append(f"## Run State\n{format_run(run)}")

        evidence = db.get_latest_evidence(event.run_id)
        if evidence:
            parts.append(f"## Worker Evidence\n{format_evidence(evidence)}")

        recent_decisions = db.get_recent_decisions(event.run_id, limit=5)
        if recent_decisions:
            parts.append(f"## Recent Decisions\n{format_decisions(recent_decisions)}")

    # 3. If Telegram: conversation history
    if event.type == "telegram_message":
        history = db.get_conversation_history(event.chat_id, limit=10)
        parts.append(f"## Conversation\n{format_history(history)}")

    # 4. Global context (if needed)
    if event.needs_global_context:
        stats = db.get_global_stats()
        parts.append(f"## Global Stats\n{format_stats(stats)}")

    return "\n\n".join(parts)
```

**注意 "Recent Decisions"**：这解决了 insight #57 的问题（Manager 无跨 tick 推理）。Agent 虽然没有跨 session 的对话记忆，但它可以读到同一 run 的历史决策记录。"上次选了 X 结果是 Y" 这种推理就成为可能。

### 4.4 System Prompt

```markdown
You are the Manager of AgentPR, an autonomous code contribution system.

Your job: coordinate Workers (codex exec) to submit high-quality integration PRs
to open-source repositories. You make strategic decisions, assess quality, and
communicate with humans.

## What you do
- Decide what action to take for each run (start worker, review code, create PR, retry, escalate)
- Assess code quality by reading worker evidence and diffs
- Generate PR descriptions based on actual code changes
- Communicate with humans naturally via Telegram
- Learn from outcomes (maintainer feedback, CI results, review comments)

## What you don't do
- You don't write code or execute shell commands (Worker does that)
- You don't bypass safety constraints (tools will return errors if you try)
- You don't fabricate data (use actual evidence from tools)

## Decision principles
1. Read evidence before judging. Always call read_evidence() before deciding quality.
2. Safety tools will stop you if something is wrong. Trust the error messages.
3. When uncertain, escalate to human rather than proceeding.
4. PR quality matters more than speed. A great PR that takes longer beats a mediocre PR that's fast.
5. Use the actual class names, file names, and test results from evidence. Never fabricate.

## Quality standards (from 17 PR reviews)
- Worker code must match the most similar existing provider pattern, not "improve" on it
- PR body must be technical (what the code does), not marketing (what the product is)
- Usage examples must be from the user's perspective, not internal API calls
- About Forge: "open-source middleware service, 40+ providers, thousands of models"
```

### 4.5 工具设计

每个工具的设计原则（SWE-agent ACI, insight #24）：
1. **输入严格约束**（enum, required fields, JSON schema validation）
2. **输出包含下一步建议**（不只是 raw data）
3. **错误信息可操作**（告诉 agent 该怎么办，不只是说 "failed"）
4. **安全检查内嵌**（不需要 agent 自己判断是否安全）

#### Tool 1: query_runs

```python
def query_runs(
    run_id: str | None = None,  # 查特定 run
    state: str | None = None,   # 按状态过滤
    limit: int = 10,
) -> str:
    """Query run information from the database.

    Returns structured summary of runs including state, owner/repo,
    last grade, PR number, and suggested next action.
    """
```

返回示例：
```
Found 3 runs:
1. run_abc123 | owner/repo1 | PUSHED | grade=PASS | No PR yet
   → Suggested: review code, then create PR
2. run_def456 | owner/repo2 | CI_WAIT | PR #42 | CI running
   → Suggested: wait for CI results
3. run_ghi789 | owner/repo3 | FAILED | grade=FAIL | retry_count=2
   → Suggested: analyze failure, consider escalating to human
```

#### Tool 2: update_state

```python
def update_state(
    run_id: str,
    to_state: Literal["EXECUTING", "PUSHED", "CI_WAIT", "REVIEW_WAIT",
                       "ITERATING", "PAUSED", "NEEDS_HUMAN", "FAILED", "DONE"],
    reason: str,  # 为什么做这个转移
) -> str:
    """Transition a run to a new state. Safety checks are automatic."""
```

返回示例（成功）：
```
OK: run_abc123 transitioned PUSHED → CI_WAIT. PR #42 linked.
```

返回示例（被安全层阻止）：
```
ERROR: Cannot transition QUEUED → PUSHED. Must go through EXECUTING first.
Valid transitions from QUEUED: EXECUTING, PAUSED, FAILED.
```

#### Tool 3: execute_worker

```python
def execute_worker(
    run_id: str,
    task: str,  # 给 Worker 的任务描述（integration / CI fix / review fix）
    skills_mode: Literal["agentpr", "off"] = "agentpr",
) -> str:
    """Launch a codex exec Worker for this run. Handles workspace setup automatically.

    Returns immediately. Use read_evidence() to check results after worker completes.
    """
```

安全检查内嵌：
- retry_count < MAX_RETRIES，否则返回 ERROR
- workspace exists，否则自动 prepare
- run state allows execution

#### Tool 4: read_evidence

```python
def read_evidence(
    run_id: str,
    include_diff: bool = False,  # 是否包含 git diff（大，用于 code review）
    include_files: bool = False, # 是否包含 changed file contents
) -> str:
    """Read worker execution evidence: grade, test results, changed files, etc."""
```

返回示例：
```
## Worker Evidence for run_abc123

Grade: PASS (confidence: high)
Changed files: 3 (forge_provider.py +45, config.py +3, README.md +8)
Test commands: pytest (874 passed, 0 failed), mypy (clean)
Test infrastructure: pytest + mypy + pre-commit
Diff stats: +56/-0, all new code

## Assessment hints
- All tests passed including existing tests
- Changes are purely additive (no modification of existing code)
- Repo has factory pattern → Worker likely followed it cleanly
```

#### Tool 5: github_api

```python
def github_api(
    run_id: str,
    action: Literal["create_pr", "read_ci", "read_reviews", "post_comment", "read_pr_template"],
    params: dict | None = None,
) -> str:
    """Interact with GitHub for this run's repository."""
```

`create_pr` 的安全检查：
- PR gate 状态检查
- Diff budget 检查
- DoD（Definition of Done）证据检查
- 如果任何检查失败 → 返回 ERROR + 具体原因 + 建议

#### Tool 6: review_code

```python
def review_code(
    run_id: str,
) -> str:
    """Perform deep code review on the worker's changes.

    Reads the diff, changed files, sibling reference files, and applies
    the 7-section review checklist. Returns CLEAN or HAS_ISSUES with details.
    """
```

这个 tool 内部可以用专门的 review prompt（保留 `review_code_changes()` 的核心逻辑），因为 code review 需要：
- 完整 diff context
- Sibling provider files 作为参照
- 7-section checklist（code_review_checklist.md 积累的 17 PR 经验）

Agent 决定 **什么时候** review，tool 决定 **怎么** review。这是 Hub-and-Spoke 模式。

#### Tool 7: generate_pr_body

```python
def generate_pr_body(
    run_id: str,
) -> str:
    """Generate a diff-aware PR description. Reads diff, evidence, and repo PR template.

    Returns markdown PR body following the quality standards:
    - Technical summary (what code does, not what product is)
    - Per-file changes
    - User-perspective usage example
    - Actual test evidence
    - Maintenance disclosure
    - About Forge (standardized)
    """
```

同样内部用专门的 generation prompt。保留 `generate_pr_description()` 的核心逻辑。

#### Tool 8: notify_human / reply_human

```python
def notify_human(
    message: str,
    priority: Literal["low", "normal", "high"] = "normal",
) -> str:
    """Send a notification to the human operator via Telegram."""

def reply_human(
    message: str,
) -> str:
    """Reply to the current Telegram conversation."""
```

#### Tool 9: update_skill（自我迭代）

```python
def update_skill(
    file: Literal["forge_rules.md", "self_review_checklist.md",
                   "code_review_checklist.md"],  # 只允许低风险文件
    action: Literal["append_rule", "modify_rule"],
    content: str,
    reason: str,
) -> str:
    """Update skill instruction files based on learned failure patterns.

    Safety: only low-risk reference files allowed (rules, checklists).
    Core SKILL.md files require human approval — use notify_human() instead.
    Auto-commits changes with reason as commit message.
    """
```

自我迭代在新架构下更强：
- 当前系统：匹配 `SKILL_IMPROVEMENT_PATTERNS`（预定义固定列表），只能识别已知失败模式
- 新架构：Agent 读 evidence → 多轮推理 → 识别任何失败模式（包括新的）→ 调用 update_skill
- Agent 可以跨 run 分析："最近 3 个没有 factory 的 repo 都有 Worker creativity 问题 → 加一条 forge_rules"

安全分层不变：低风险文件（rules, checklist）→ auto-apply + git commit；高风险文件（SKILL.md）→ notify_human 请求审批。约束在 tool 的 `file` enum 里。

### 4.6 9 个 LLM Methods 的归宿

| # | 旧 Method | 新归宿 | 说明 |
|---|-----------|--------|------|
| 1 | `decide_action` | **删除** → Agent 自主决策 | Agent 有 context + tools，自己推理下一步 |
| 2 | `grade_worker_output` | **简化** → `read_evidence()` tool 内的 rules 提取 | Agent 看 evidence 自己判断，rules 仍做证据提取 |
| 3 | `explain_decision_card` | **已删除** → Agent 直接用自然语言 | 上一轮已完成 |
| 4 | `decide_bot_action` | **删除** → Agent 天然理解 NL | Agent 就是对话者 |
| 5 | `triage_review_comment` | **删除** → Agent 读 review comment 自己判断 | Agent 有完整 run context |
| 6 | `suggest_retry_strategy` | **删除** → Agent 分析失败后自己制定策略 | Agent 可以多轮推理 |
| 7 | `generate_pr_description` | **保留为 tool** → `generate_pr_body()` | 专门的生成 prompt，Agent 决定何时调用 |
| 8 | `review_code_changes` | **保留为 tool** → `review_code()` | 专门的审查 prompt，Agent 决定何时调用 |
| 9 | `assess_pr_readiness` | **删除** → Agent 综合 review 结果自己判断 | Agent 做完 review 后自然知道是否 ready |

**9 → 2 specialized tools + agent 内在推理。** 省去了 7 个 prompt template + 7 个 response parser。

### 4.7 Telegram 交互模型

旧模型：
```
用户输入 → decide_bot_action(1-shot) → 选命令 → 执行 → explain_decision_card(1-shot) → 回复
```

新模型：
```
用户输入 → agent session(with conversation history) → agent 调用 tools → 回复
```

Agent 就是对话者。不需要 NL→command 路由。

示例：
```
Human: "dexter 怎么样了？"
Agent: [calls query_runs(state=None, limit=5)]
       [sees dexter in CI_WAIT, last CI failed]
Agent: "dexter 在等 CI 结果。最近一次 CI 失败了——lint error in forge_client.py:42。
        我可以启动 Worker 去修这个问题，你觉得呢？"
Human: "修吧"
Agent: [calls execute_worker(run_id="xxx", task="Fix lint error in forge_client.py:42")]
Agent: "Worker 已启动修复。我会在完成后通知你结果。"
```

不需要 `/retry run_xxx ITERATING`。不需要 decide_bot_action 解析意图。

### 4.8 Safety Model

**原则：Agent 自由行动，Tools 执法。**

```
Agent 想做一个操作
  ↓
调用 Tool（带参数）
  ↓
Tool 内部安全检查：
  - 状态转移合法？ → state_machine.validate()
  - Diff budget 内？ → diff_stats.check()
  - Retry limit 内？ → retry_count < MAX
  - PR gate 通过？ → approval_status.check()
  - DoD 满足？ → evidence_threshold.check()
  ↓
  通过 → 执行操作，返回成功
  失败 → 不执行，返回 ERROR + 原因 + 建议
  ↓
Agent 看到 error → 调整策略
  - "retry limit exceeded" → 通知人类
  - "need human approval" → 发 Telegram 请求
  - "invalid transition" → 选择正确的转移
```

**关键：Agent 无法绕过安全检查。** Tools 是唯一的行动接口，安全检查嵌入在 tool 实现中。

这和旧架构的区别：
- 旧：rules engine 预先限制 allowed_actions → LLM 只能从有限列表选
- 新：Agent 可以尝试任何 tool call → tool 层决定是否允许

**新模型更聪明**：Agent 可以理解 WHY 某操作被拒绝（error message），并制定替代策略。旧模型里 LLM 只看到 "allowed_actions: [RUN_FINISH, WAIT_HUMAN]"，不知道 WHY 其他选项不可用。

---

## 5. Decision Points（已确认）

### DP1: PR 创建的 gate 策略 → **Agent 自主创建 + 通知人，保留 human-confirm 选项**

Agent review 通过后自主创建 PR，同时 Telegram 通知 "已为 repo X 创建 PR #Y"。可通过配置切换为 human-confirm 模式（Agent 建议，人 approve 后创建）。

实现：`github_api(action="create_pr")` 内部检查配置 `pr_gate_mode`：
- `"auto"`: Agent 可直接创建，tool 执行后发通知
- `"human_confirm"`: tool 返回 "PR ready, waiting for human approval. Token: xxx"

### DP2: Model 选择 → **模型无关，通过 Forge 路由，默认 GPT-4o，数据驱动切换**

通过 Forge 统一接口，`AGENTPR_MANAGER_MODEL` 可指向任意模型（`tensorblock/gpt-4o`、`tensorblock/claude-sonnet-4`、`tensorblock/gemini-2.5-flash` 等）。不锁定 provider。如果数据显示 code review 或 PR 生成质量不够，可随时切换模型，无需改代码。

### DP3: Session 触发范围 → **全局 tick + Telegram 即时响应**

两种 session 触发方式：

**定时 tick（默认 180s / 3 分钟，可配置 `AGENTPR_TICK_INTERVAL_SEC`）**：收集所有 pending 事件 + 所有 active runs 状态 → 一个 Agent session 处理全局。Agent 看到全局视角，可以做优先级判断和交叉分析。加上已有的 wake file 机制：webhook 事件到达 → touch `.wake_manager` → 立即触发 tick（不等定时器）。所以有事件时几乎实时，无事件时 3 分钟巡检。

```
Tick session context:
  "当前 3 个 active runs:
   - repo1: CI 失败（第 2 次），lint error
   - repo2: Worker 完成，PASS，等待 review
   - repo3: maintainer 回复了 review comment

   Pending events: CI_FAILED(repo1), WORKER_DONE(repo2), REVIEW_COMMENT(repo3)"

→ Agent 推理并处理所有 pending 事项
```

**Telegram 消息 → 立即启动 session**：人发消息期望快速回复，不等 tick。

```
Telegram session context:
  "用户说：'现在什么情况？'
   全局状态摘要：3 active runs, 1 PASS waiting review, ..."

→ Agent 立即回复
```

### DP4: Forge 专用 vs 通用 → **先 Forge 专用，架构不 hardcode**

当前 skills 和 review checklist 是 Forge-specific。system prompt 控制任务范围。架构本身（tools, agent loop, safety gates）不 hardcode "Forge"——未来换 system prompt + skills 即可通用化。

### DP5: 迁移策略 → **新 branch，全部重写 Manager 层**

创建 `feature/manager-agent` branch。Worker 层、DB 层、safety gate 逻辑直接复用。旧 Manager 代码结构和新架构根本不同，增量替换更复杂。新 branch 快速验证，不影响 main。

---

## 6. Implementation Plan

### Phase E0: Spike（1-2 天）

目标：验证 "Agent 替代 9 x 1-shot" 的可行性。

1. 创建 `feature/manager-agent` branch
2. 实现 agent loop（30 行）
3. 实现 3 个核心 tools：query_runs, execute_worker, read_evidence
4. 硬编码 system prompt
5. 在 1-2 个 repo 上跑：对比 Agent session 的决策质量 vs 旧 manager 的决策

验收：Agent 能正确推进一个 run 从 QUEUED 到 PUSHED。

### Phase E1: Tool Layer（2-3 天）

1. 实现全部 8-10 个 tools（含安全检查）
2. 实现 context builder
3. 实现 review_code + generate_pr_body specialized tools
4. 单元测试 tools 的安全行为

### Phase E2: Integration（2-3 天）

1. 替换 manager_loop.py → agent session dispatch
2. 接入 GitHub webhook → agent event
3. 接入 Telegram → agent conversation
4. 实现 decision audit logging

### Phase E3: Validation（1-2 天）

1. 在 5+ repos 上测试完整流程
2. 对比成本（token usage）
3. 对比质量（Agent 决策 vs 旧 Manager 决策）
4. 收集数据驱动 DP 决定

---

## 7. 成本估算

### 当前成本（9 x 1-shot per tick）

| Method | 调用频率 | 估算 tokens/call | 成本/call |
|--------|---------|-----------------|----------|
| decide_action | 每 tick | ~2K in + ~200 out | ~$0.01 |
| grade_worker_output | 每 run 1-2 次 | ~3K in + ~500 out | ~$0.02 |
| 其他 6 个 | 按需 | ~2K avg | ~$0.01 each |
| **Total per run** | | | **~$0.10-0.30** |

### 新成本（Agent session）

| Session type | 估算 turns | 估算 tokens | 成本/session |
|-------------|-----------|------------|-------------|
| Routine tick（看状态，决定下一步） | 3-5 | ~10K in + ~2K out | ~$0.05-0.10 |
| Worker 完成后（read evidence + review） | 5-8 | ~20K in + ~5K out | ~$0.15-0.30 |
| PR 创建（review + generate body + create） | 8-12 | ~30K in + ~8K out | ~$0.25-0.50 |
| Telegram 对话 | 2-4 | ~5K in + ~1K out | ~$0.03-0.05 |
| **Total per run (full lifecycle)** | | | **~$0.50-1.00** |

**成本增加 ~3-5x**，但换来了：
- Agent 可以多轮推理（质量上升）
- 省去了 7 个 prompt template 的维护
- 省去了 rules engine 的维护
- Telegram 交互质量大幅提升

50 repos × $1/run = $50，可接受。

---

## 8. 风险和缓解

| 风险 | 影响 | 缓解 |
|------|------|------|
| Agent 做出错误决策 | PR 质量下降 | Safety 在 tools 层，Agent 无法绕过。Error 后 Agent 自行调整 |
| Agent 无限循环调用 tools | 成本失控 | max_turns 硬限制（15）。超出自动 escalate to human |
| Agent 回复质量不稳定 | Telegram UX 差 | System prompt 规范输出格式。人可以随时 /override |
| 新架构 bug 多 | 流程中断 | 新 branch 独立开发。E0 spike 快速验证。旧 main 不受影响 |
| Token 成本超预期 | 预算问题 | 每个 session audit token usage。超阈值自动降级到 cheaper model |
| Code review 质量不如专门 prompt | 漏掉 bug | review_code 保留为专门 tool（内部仍用优化的 review prompt） |

---

## 9. 不做的事情

1. **不改 Worker。** codex exec + skills 已验证。保持不变。
2. **不改 DB schema。** SQLite + 现有 tables 足够。可能加 decision_log 表。
3. **不改状态机。** 10 个状态保持。状态转移校验保持确定性。
4. **不引入新框架。** 直接用 Forge `/chat/completions` + 30 行 agent loop。模型无关。
5. **不做多 agent。** 单 Manager agent + Worker (codex exec)。Hub-and-Spoke 足够。
6. **不追求 persistent conversation。** Event-driven sessions，从 DB 重建 context。
7. **不做 Web UI。** Telegram 足够（insight: "过早追求 Web 控制台"）。

---

## 附录 A: 历史决策链

```
Phase A (0225): 纯基础设施
  14-state 手动 CLI → 建立了状态持久化、安全 gate
  insight #7: "worker 专注执行，manager 专注状态管理"

Phase B/C (0228): Vision + Compromise
  Vision: Manager Agent (LLM with tools) — Section 4
  Compromise: "中间态最优：LLM 决策 + 硬约束执行 + 人工 gate" — Section 5
  原因: framework 不成熟，先验证 Worker

Phase D (D1-D4): 验证成功
  86% PASS, 17 PRs, 64 insights
  发现: Worker OK, Manager 是瓶颈
  发现: 9/9 LLM 是 1-shot, 6/9 是 rules 够用的分类器
  发现: PASS ≠ merge, 需要 Manager 更聪明（repo targeting, PR quality, follow-up）

Phase E (now): 回到原始 Vision
  把 Manager 从 9 x 1-shot 升级为真正的 Agent
  保留所有已验证的资产（Worker, DB, safety, skills, review knowledge）
  删除 plumbing，保留 domain knowledge
```

## 附录 B: 从 64 条 Insights 中与此重构直接相关的

- #7: "精密工厂管理聪明工人" 是反模式 → 轻量生产线 + 自主工人 + 关键检查点
- #9: LLM 是大脑 with tools，不是规则引擎附属品。但"大脑" ≠ "all LLM"
- #24: 工具接口设计 > 模型能力（SWE-agent ICLR 2025）
- #25: 不要过度设计分层。单 agent + 好工具 + 安全拦截器
- #46: AI centric ≠ all LLM。基础设施确定性，Agent 智能
- #57: Manager 无记忆的 1-shot 是最大结构性限制
- #58: 无状态设计优点不应被低估。改进方向：更多 artifacts 作为 context
- #63: 9 个 LLM 方法中只有 3 个高价值
- #64: "给现有方法加上下文"(低 ROI) ≠ "让 Manager 做新事情"(高 ROI)

## 附录 C: 外部研究关键结论

来源：`log.txt` (Gemini Deep Research, 2026-03-10)

1. **Workflow-Constrained Agent Loop** 是最优模式：宏观阶段确定性 + 微观阶段 agent loop (max_turns=5)
2. **Hub-and-Spoke** 胜过多 agent：单 frontier model + 按需 spawn 轻量子 agent
3. **SCAFFOLD-CEGIS** 安全范式：Correctness Gate → Safety Gate → Anchor Integrity → Diff Budget
4. **Irreducible complexity**：只有 domain-specific rules + guardrails + evaluation heuristics 需要自建。其余 commoditized
5. **每次 CI 失败降低 merge 概率 15%** → Build-Before-Push 是关键
6. **80%+ merge rate** 在有足够约束的系统中可达 → 我们 5.9% 不是技术问题是产品问题
7. **Diff budget** 是最关键操作约束 → 已有，保留
