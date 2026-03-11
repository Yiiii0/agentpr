# AgentPR 架构设计完全指南

> 假设你是第一次接触 AI Agent 系统，这篇文档会从零开始带你理解 AgentPR 的设计、决策、认知和迭代过程。

---

## 一句话说清楚 AgentPR 是什么

AgentPR 是一个 **AI 驱动的 PR 自动化系统**。你在 Telegram 里输入一行命令（比如 `/create owner/repo1 owner/repo2`），它就会：

1. 自动 clone 这些 repo
2. 分析每个 repo 的结构、代码风格、CI 配置
3. 按照 repo 自己的 pattern 写代码
4. 跑测试、验证
5. AI 自己 review 自己写的代码
6. 生成 PR description
7. 创建 GitHub PR
8. 监控 CI 结果和 maintainer review 评论
9. 遇到问题自动修复重试

你全程只需要在 Telegram 里看通知。

---

## 为什么做这个

我们公司（TensorBlock）有一个产品叫 **Forge**——一个开源的 AI 模型聚合中间件。它兼容 OpenAI API，能把请求路由到 40+ 家 AI provider 的几千个模型。

要让更多开源项目支持 Forge，需要给每个项目提一个 PR：加一个 provider。手动做一次大概需要 2-4 小时（读代码、写集成、跑测试、写 PR body）。如果要做 50 个 repo，那就是 100-200 小时的人工。

**AgentPR 的目标：把这个过程自动化到接近零人工。**

---

## 核心架构：三个角色

```
Human (Telegram)  -->  Manager (LLM)  -->  Worker (Codex)  -->  GitHub PR
      |                    |                     |
  输入命令、看通知     做决策、审代码        写代码、跑测试
```

### 角色 1: Manager（大脑）

Manager 是一个 LLM Agent（大语言模型 + 工具调用）。它：
- **做决策**：这个 run 下一步应该做什么？（执行 worker？创建 PR？等待 CI？重试？）
- **审代码**：Worker 写完代码后，Manager 做深度 code review
- **评估 PR**：PR 创建前，Manager 评估代码质量+PR body 质量是否达标
- **诊断失败**：Worker 失败了，分析原因，生成重试策略
- **通知人类**：关键事件推送到 Telegram

**关键设计决策**：Manager 的所有 LLM 调用都是**无状态的 1-shot**。每次调用都从数据库重建上下文，不依赖对话记忆。这意味着：
- 任何时候重启都不会丢信息
- 但也意味着它无法"记住"自己之前的推理过程

### 角色 2: Worker（手）

Worker 是 OpenAI Codex（一个能在沙箱里自主执行代码的 AI）。它：
- Clone repo 到隔离沙箱
- 读 CONTRIBUTING.md、CI 配置、已有 provider 代码
- 按照 repo 自己的 pattern 写新代码（不是用自己认为"更好"的方式）
- 跑 pytest/ruff/pre-commit 等测试
- 产出代码变更 + 测试证据

**关键设计决策**：Worker 是无状态的。每次执行都是一次独立的 `codex exec` 调用，上下文通过 task packet（指令+skills）传入。

### 角色 3: Orchestrator（骨架）

Orchestrator 不是 LLM，是纯代码。它：
- 管理状态机（QUEUED → EXECUTING → PUSHED → ... → DONE）
- 持久化所有数据到 SQLite
- 执法安全 gate（diff budget、sandbox isolation）
- 记录每一个 action 用于审计

**关键设计决策**：Orchestrator 是"笨"的。它不做任何决策，只执行 Manager 的指令+硬性规则。这个分工很重要——让智能的部分（LLM）做决策，让确定性的部分（代码）做执法。

---

## 三个进程

AgentPR 运行时有三个独立进程，通过 SQLite 数据库 + `.wake_manager` 信号文件连接：

```
进程 1: Telegram Bot (长驻)
    ↓ touch .wake_manager
进程 2: Manager Loop (长驻 daemon)  ←→  SQLite DB
    ↑ touch .wake_manager
进程 3: GitHub Webhook Server (长驻)
```

- **Telegram Bot**：接收用户命令，发送通知
- **Manager Loop**：核心循环。每 tick 从 DB 读状态 → 调 LLM 决策 → 执行 → 更新 DB
- **Webhook Server**：接收 GitHub 事件（CI 结果、review 评论）

**为什么用信号文件而不是消息队列？** 零依赖。一个 `.touch()` 调用就能唤醒 Manager。Manager idle 时休眠（600s 检查一次），被唤醒后立刻 tick。不需要 Redis、RabbitMQ 或任何外部服务。

---

## 状态机：一个 run 的完整生命周期

```
QUEUED          用户创建了 run，等待执行
    ↓
EXECUTING       Worker 正在沙箱里写代码+跑测试
    ↓
PUSHED          Worker 完成，代码已 push 到分支
    ↓
Code Review     Manager LLM 审查代码（diff + 完整文件 + checklist）
    ↓
  ├─ CLEAN          代码通过审查
  │   ↓
  │  PR Gate        如果是 auto 模式：Manager 评估 PR 就绪度
  │   ├─ APPROVE    → 自动创建 PR → CI_WAIT
  │   └─ NEEDS_HUMAN → 通知人工
  │
  └─ HAS_ISSUES     代码有问题
      ↓
    ITERATING       Worker 再次执行修复问题 → 回到 PUSHED

CI_WAIT         等待 GitHub CI 通过
    ↓
REVIEW_WAIT     等待 maintainer review
    ↓
DONE            PR 被合并或关闭

+ PAUSED         任何状态都可以暂停
+ NEEDS_HUMAN    需要人工干预
+ FAILED         终态：多次重试后放弃
```

---

## 质量链：从代码到 PR 的四道防线

这是 AgentPR 最重要的设计之一。代码不是写完就提 PR，中间有四道质量检查：

```
Worker 写代码 → Hybrid Grading → Code Review → PR Gate → PR
                    ↑                  ↑            ↑
              规则+LLM 评分      LLM 深度审查   LLM 综合评估
```

### 第一道：Worker Self-Review
Worker 在提交前会做自检（self-review checklist）：
- 有没有 mutate `os.environ`？
- 变量有没有在使用前初始化？
- model 检测用的是 prefix match 还是 substring？

### 第二道：Hybrid Grading（混合评分）
**不是纯 LLM，是规则+LLM 混合。** 这是一个关键设计决策。

规则负责：
- 提取证据（test 命令数、lint 命令数、diff 大小、changed files）
- 硬性护栏（diff 超过 budget → FAIL，不管 LLM 怎么说）
- 分类（exit_code 非零 → 失败，zero test commands → 标记）

LLM 负责：
- 语义判断（"这个 exit_code=1 是不是其实只是 warning？"）
- 边界情况（"diff 刚好在 budget 边缘，但只改了一个文件"）

**为什么不全用 LLM？** 因为 LLM 会"同情" Worker——你给它一个明显失败的 run，它可能会说"虽然有问题但总体还行，给 PASS"。硬性规则不会。**护栏只能降级（PASS→FAIL），不能升级（FAIL→PASS）。**

### 第三道：Code Review（LLM 深度审查）
Manager LLM 读取：
- Git diff
- 完整的 changed files
- 相邻的 reference provider 代码（用来对比 pattern）
- 从 17 次真实 PR review 中积累的 7 段 checklist

输出 CLEAN 或 HAS_ISSUES。如果 HAS_ISSUES，自动进入 ITERATING 让 Worker 修复。

### 第四道：PR Readiness Assessment
创建 PR 前的最终检查，综合评估：
- 代码审查结果
- Worker 产出的证据（test results、grade）
- 生成的 PR body
- Repo 的 PR template（如果有）
- Git diff
- 从 17 次 review 中提炼的 7 条原则

---

## LLM 架构：9 个方法，全部 1-shot

Manager 的 LLM 调用都在 `manager_llm.py` 里，共 9 个方法：

| # | 方法 | 干什么 | 价值 |
|---|------|--------|------|
| 1 | `decide_action` | 状态机决策："下一步做什么？" | 中（rules 能做大部分） |
| 2 | `grade_worker_output` | 对 Worker 产出做语义评分 | 中 |
| 3 | `explain_decision_card` | 把决策翻译成人话发 Telegram | 低 |
| 4 | `decide_bot_action` | 把用户自然语言路由到具体命令 | 低 |
| 5 | `triage_review_comment` | 分析 maintainer 的 review 评论：要改代码？要改 PR body？还是只需要回复？ | 中 |
| 6 | `suggest_retry_strategy` | Worker 失败了，分析原因并生成重试策略 | 中 |
| **7** | **`generate_pr_description`** | **基于 diff 生成 PR body** | **高** |
| **8** | **`review_code_changes`** | **深度代码审查** | **高** |
| **9** | **`assess_pr_readiness`** | **PR 就绪度评估** | **高** |

**核心洞察：只有 #7、#8、#9 直接影响 PR 质量。** 其余都是分类器，用规则+启发式已经够用。如果要优化系统，优先投资在这三个方法上。

---

## Skills：Worker 的"说明书"

Worker 执行时不是裸跑，它有三个 skill（技能），每个 skill 是一组 markdown 指令文件：

### Skill 1: Preflight Contract（分析阶段）
- 自动检测项目类型（Python? TypeScript? 混合?）
- 读 CONTRIBUTING.md, CI 配置
- 找到最相似的 reference provider 代码
- 产出分析结果供后续使用

### Skill 2: Implement & Validate（实施+验证阶段）
- 两阶段 prompt：分析（内部不输出） → 实施（唯一产出）
- 按照 repo 的 pattern 写代码（factory 注册、registry 声明、class 继承）
- 跑 pytest/ruff/pre-commit
- Self-review checklist
- **关键规则**：永远不要放弃验证。安装失败？还是试着跑 pytest。测试失败？记录证据继续。

### Skill 3: CI Review Fix（迭代修复阶段）
- 解读 CI 失败日志
- 解读 maintainer review 评论
- 做最小改动修复问题
- 区分"初始集成"和"迭代修复"（prompt 不一样）

**Skills 的特殊设计**：
- Skills 是 Worker **自主调用**的，不是 Orchestrator 注入的。这保护了 Worker 的上下文窗口
- Skills 有**自动升级**机制：source 文件的 mtime 比已安装的新 → 自动覆盖
- Skills 有**自我改进**：每次 run 的失败经验可以编码回 skill 的 reference 文件

---

## 自我改进闭环

这是 AgentPR 的一个有意思的设计。失败不只是失败，它是学习机会：

```
Run 失败（比如 Worker 没跑测试）
    ↓
Grading 检测到 reason_code = "missing_test_evidence"
    ↓
匹配 SKILL_IMPROVEMENT_PATTERNS
    ↓
生成 proposal: "强化 fallback 验证：安装失败也要尝试 pytest"
    ↓
安全分层判断：
  - 目标文件是 references/validation_requirements.md（低风险）→ 自动追加 + git commit
  - 目标文件是 SKILL.md（高风险）→ 存为 proposal，通知人类，等 /approve_skill
    ↓
下次 run 时 Worker 读到了更新后的 skill instructions → 不再犯同样的错
```

**安全分层**很重要：
- **低风险文件**（checklist、rules、validation requirements）→ 自动修改 + git commit（方便回滚）
- **高风险文件**（SKILL.md 主提示、核心 prompt 逻辑）→ 只提建议，人工批准后才修改

---

## 安全设计

1. **Worker 沙箱隔离**：写权限限定在 repo 目录 + `.agentpr_runtime` + `/tmp`
2. **Diff budget**：限制 Worker 能改的文件数和行数，防止大规模重构
3. **Commit 白名单**：只允许特定扩展名的新文件（.py .ts .md 等），永远不 commit lock files
4. **双确认 PR 创建**：人工确认（默认）或 Manager AI 评估
5. **Merge 永远人工**：系统永远不会自己合并 PR
6. **审计日志**：每个 action、每个 decision 都记录在 SQLite 里

---

## 从数据中学到的认知

以下是从 22 个真实 repo 测试 + 17 个真实 PR review 中总结的关键认知：

### 关于 AI Agent 设计

**"精密工厂管理聪明工人"是反模式。** 正确的模型是"轻量生产线 + 自主工人 + 关键检查点"。不要试图用复杂的 orchestration 框架去控制每一步，让 Worker 自主执行，在关键节点做检查。

**工具接口设计比模型能力更重要。** SWE-agent（ICLR 2025）的核心结论。我们通过修改工具接口（enum 约束、自动推导、显式反馈）就把 Worker 成功率从 ~40% 提升到 86%，没有换更强的模型。

**给 LLM 精简的信息比给它更多信息效果好。** 堆上下文会稀释重要信息。用 slim entry + skill references 比 everything-in-one-prompt 好。

### 关于混合策略（Rules + LLM）

**"AI centric" 不等于 "all LLM"。** 基础设施应该是确定性的（规则、护栏、状态机），Agent 的部分才应该是智能的（决策、审查、生成）。

**护栏只能降级，不能升级。** LLM 可以把 UNKNOWN 判定为 PASS，但不能把 FAIL 覆盖为 PASS。这是安全的核心。

**单信号 false positive 是分类问题通病。** 用 ≥2/4 信号阈值代替单信号判断，消除误判。

### 关于 Worker 行为

**Worker 的"改进"冲动是最大风险。** LLM 天生倾向于"做得更好"而不是"做得一样"。你说"follow existing patterns exactly"，它理解成"参考但改进"。必须在 skill instructions 里反复强调"不添加参照没有的东西"。

**Worker 代码质量和 repo 架构强相关。** 好的 repo 有 registry/factory pattern → Worker 只需填参数 → 没有空间犯错。差的抽象 → Worker 开始"创作" → bug。

**Worker 无法执行代码结构重排。** 擅长"添加新代码"，不擅长"重组已有逻辑"（比如 elif 顺序调整）。这类问题需要人工介入。

### 关于验证

**一次真实测试胜过十次代码审查。** dirty workspace bug 纯静态分析不可能发现。只有跑了真实 repo 才会暴露。

**PASS rate ≠ merge rate。** 我们 86% 的 PASS 率，但只有 5.9% 的 merge 率。优化错误的指标是最大的浪费。技术管道再完美，maintainer 不 merge 就是零。

**Grade=PASS 不等于产出质量达标。** 量化 grading（test pass、diff size）和定性 review（逻辑正确性）是两回事。Pipeline 需要 code review gate。

### 关于 PR 质量

**PR body 不应包含营销文本。** 被 maintainer 视为低质量贡献信号。写 PR 就像写技术报告，不是写推销信。

**Usage 必须是用户视角。** 不要展示内部 API 调用，要展示用户实际怎么用。

**数据必须回源核实。** 我们曾经"纠正"一个近似正确的数字（"40+ providers"）为一个错误的数字（"77 providers"），因为没有回源验证。

---

## 迭代历程

| 阶段 | 日期 | 做了什么 | 关键产出 |
|------|------|---------|---------|
| D1 | 03-02 | 工具接口优化（ACI） | target_state enum、自动推导、显式反馈 |
| D1.6 | 03-04 | Pipeline 安全网 | 10 个安全机制（audit、guardrail、mutex 等） |
| D2 | 03-05 | 第一次真实验证 | 5/5 PASS。发现 codex 断点 bug、git add 白名单 |
| D2.5 | 03-06 | Commit 质量修复 | 白名单 stage、self-review checklist |
| D3 | 03-07 | 边界修复 + 批量运行 | 19/22 PASS (86%)、17 PRs |
| D3.5 | 03-07 | 自我改进闭环 | Skills 自动升级、skill proposal 系统 |
| D3.6 | 03-07 | LLM PR Description | diff-aware 智能生成（不是模板拼接） |
| D3.8 | 03-08 | Review 经验编码 | forge_rules +4、self_review +4，从真实 review 中学习 |
| D3.9 | 03-09 | Code Review Gate | 17/17 深度 review、4 逻辑 bug 修复 |
| D4 | 03-10 | 运维化 | persistent daemon、wake file、双 PR 模式、auto-title |

**从 D1 到 D4，总共 9 天。** 核心洞察是：不要花太多时间设计，快速跑真实测试，从失败中学习，编码回指令，再跑。

---

## 验证数据

- **22 个 repo 测试**：19 PASS+PUSHED (86%)，3 需要人工
- **17 个 PR 提交**：1 merged（octotools #53），13 代码干净，2 CLA blocked
- **Code review 发现**：4 个逻辑 bug（DAMO-ConvAI Union type、pipecat os.getenv、octotools factory routing、weave elif reorder）
- **平均 Worker attempt**：1.0（首次执行即 PASS）
- **代码影响**：~10 个文件、~5000 行 Python、9 个 LLM 方法、3 个 Skills

---

## 技术栈

| 组件 | 技术 |
|------|------|
| 语言 | Python 3.11 |
| Worker | OpenAI Codex CLI (`codex exec`) |
| Manager LLM | OpenAI-compatible API（gpt-4o） |
| 数据库 | SQLite（零依赖） |
| 进程通信 | 信号文件 `.wake_manager`（零依赖） |
| GitHub 交互 | `gh` CLI |
| 人机交互 | Telegram Bot API |
| CI 监控 | GitHub Webhooks |

**设计原则：最少外部依赖。** 不用 Redis、不用消息队列、不用容器编排。SQLite + 信号文件 + gh CLI 就够了。

---

## 目前的局限

1. **Manager 无跨 tick 记忆**：每次决策都是 1-shot，无法"记住"上次尝试过什么。当前通过 artifacts 作为间接记忆缓解
2. **Worker 不擅长结构重排**：只能添加新代码，不能重新组织已有代码的逻辑
3. **Merge rate 低 (5.9%)**：技术管道完美但 maintainer merge 是产品价值问题，不是工程问题
4. **仅适用于"添加 provider"类型的 PR**：结构化、重复性的集成工作。不适用于 bug fix、feature development 等创造性工作
5. **CLA 无法自动化**：部分 repo 要求签署 CLA，必须人工完成

---

## 文件结构

```
agentpr/
├── orchestrator/
│   ├── cli.py              # 所有 CLI 命令入口
│   ├── cli_pr.py           # PR 相关的辅助函数
│   ├── manager_loop.py     # Manager 核心循环
│   ├── manager_llm.py      # 9 个 LLM 方法
│   ├── manager_decision.py # 状态机决策逻辑 + ManagerRunFacts
│   ├── manager_tools.py    # Manager 的分析工具
│   ├── executor.py         # 执行层（codex exec, gh pr create 等）
│   ├── runtime_analysis.py # Hybrid grading（规则+LLM）
│   ├── preflight.py        # 环境检查（codex, gh, python）
│   ├── skills.py           # Skill 安装+渲染+升级
│   ├── service.py          # OrchestratorService（DB 操作封装）
│   ├── db.py               # SQLite schema + 低级 DB 操作
│   ├── telegram_bot.py     # Telegram Bot 进程
│   ├── telegram_bot_helpers.py  # Bot 格式化+命令定义
│   └── github_webhook.py   # Webhook Server 进程
├── skills/
│   ├── agentpr-preflight-contract/    # Skill 1: 分析
│   ├── agentpr-implement-and-validate/ # Skill 2: 实施+验证
│   └── agentpr-ci-review-fix/         # Skill 3: 迭代修复
├── forge_integration/
│   └── pr_description_template.md     # About Forge 模板
├── AGENTPR_MASTER_PLAN.md             # 精简版 master plan (64 条认知)
└── docs/
    ├── OPERATIONS_GUIDE.md            # 运维指南
    ├── pr_review_findings.md          # 17 PR 深度 review 记录
    └── ARCHITECTURE_GUIDE.md          # 本文档
```

---

## 总结

AgentPR 不是一个"用 AI 写代码"的工具。它是一个**完整的 AI 工作流系统**，从任务创建到代码编写到质量审查到 PR 提交到后续跟进，全链路自动化。

它的核心设计哲学是：
- **让 LLM 做它擅长的事**（理解上下文、做判断、写代码、生成文本）
- **让规则做规则擅长的事**（硬性护栏、状态管理、安全执法）
- **从真实数据中学习**（跑测试 → 发现问题 → 编码回指令 → 下次不犯）
- **安全默认**（merge 人工、PR 需确认、diff 有预算、每步可回滚）

9 天时间，从零到 86% PASS rate、17 个真实 PR、1 个被 merge、64 条沉淀认知。这不是因为工程量大，而是因为**每次迭代都基于真实反馈**。
