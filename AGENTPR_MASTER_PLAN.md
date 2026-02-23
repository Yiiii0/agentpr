# AgentPR 总体方案与执行手册

> 更新时间：2026-02-23  
> 目标：把现有 Forge OSS PR 提交流程，从“手动驱动”升级为“可持续运行的轻量编排系统”。

## 1. 背景与目标

我们已经有可用的执行层资产：
- `prompt_template.md` / `claude_code_prompt.md`
- `scripts/prepare.sh` / `scripts/finish.sh`
- `workflow.md`（包含真实踩坑和规则）
- `repos.md`（当前是手工跟踪）

现状不是“不会改代码”，而是“多仓库持续运营成本高”：  
首次读仓库、识别改动点、遵守 CONTRIBUTING/PR 规则、跟踪 CI/review、远程管理都很耗精力。

## 2. 核心价值观与判断框架

采用三条原则：

1. 实事求是  
遵循客观条件（时间、空间、约束），不按主观愿望设计系统。
2. 抓主要矛盾  
优先解决“仓库理解与合规成本高、状态追踪断裂”，而非过早追求大平台化。
3. 长期理性  
先做可验证的小系统，保留升级路径，不一次性堆满重框架。

## 3. 主要矛盾与非主要矛盾

### 主要矛盾（必须优先解决）

1. 状态管理手工化：`repos.md` 难以支撑规模化
2. 事件闭环断裂：CI/review 反馈需要人工盯 GitHub
3. 首次阅读成本高：每仓库规则和架构差异大
4. 远程控制缺失：不能随时暂停/重试/跳过/查看详细状态

### 非主要矛盾（先不优先）

1. 立刻更换执行引擎（现有执行层已可产出）
2. 一开始引入重型工作流平台（Temporal/K8s 级别复杂度）
3. 多渠道复杂控制网关（先用最小 bot 控制面）

## 4. 当前最优方案（阶段性结论）

**自建轻量 orchestrator（业务逻辑） + 成熟基础库（鲁棒能力） + 现有执行层（已验证）**

组合如下：

1. Orchestrator（自写）
- Python 状态机（repo 级）
- 幂等与重试策略
- 人工介入点（pause/retry/skip/override）

2. 存储层
- MVP：SQLite
- 扩展：Postgres

3. GitHub 事实源
- 优先 Webhook（pr/check_run/workflow_run/issue_comment/review）
- 过渡可轮询（低频兜底）

4. 执行层
- 继续使用当前 prompt + shell 脚本 + 代码代理

5. 远程控制
- Telegram/Slack 轻量 bot

## 5. 为什么不是“全框架接管”

1. OpenClaw  
更适合控制面/会话入口，不是 PR 生命周期编排核心。

2. OpenHands/SWE-agent  
可作为可替换执行器或 A/B worker，但不是你当前瓶颈本体。

3. LangGraph/Temporal  
并非不能用，而是当前规模和阶段下性价比不高。  
应在“事件量、并发、恢复需求”超过轻编排能力后再引入。

## 6. 目标架构（3 层）

### 6.1 Control Plane（控制面）

- Bot/API 命令：
  - `status`
  - `detail <repo>`
  - `pause <repo|all>`
  - `resume <repo|all>`
  - `retry <repo>`
  - `skip <repo>`
  - `set-prompt <version>`

### 6.2 Orchestration Plane（编排面）

- 统一状态机（每 repo 一个 workflow）
- 事件消费与去重
- 失败分类（infra / policy / code / flaky）
- 重试与超时控制

### 6.3 Execution Plane（执行面）

- `prepare.sh`：fork/clone/branch 同步
- 代理执行：按 prompt 完成分析、实现、验证
- `finish.sh`：commit/push（受状态机 gate 控制）

## 7. Repo 状态机（建议）

`QUEUED -> DISCOVERY -> PLAN -> IMPLEMENT -> LOCAL_VALIDATE -> PUSH -> PR_OPEN -> CI_WAIT -> REVIEW_WAIT -> ITERATE -> DONE`

失败出口：
- `NEEDS_HUMAN_REVIEW`
- `FAILED_RETRYABLE`
- `FAILED_TERMINAL`
- `SKIPPED`

关键规则：
1. 每一步必须可重入（幂等）
2. 每一步必须落审计日志（输入、输出、错误、artifact）
3. 只有通过 gate 才能进入 `PUSH/PR_OPEN`

## 8. 分阶段执行计划

### Phase 0（1-2 天）：规则固化

1. 定义状态机与失败分类
2. 明确 repo contract 模型（见下节）
3. 定义最小命令集与权限边界

### Phase 1（3-7 天）：MVP

1. 实现 Python orchestrator（单进程）
2. SQLite 状态存储
3. GitHub 轮询 + 基础 webhook（二选一先落地）
4. Telegram/Slack 基础命令
5. 接入现有 `prepare.sh/finish.sh`

验收标准：
1. 连续跑 10+ repo 不丢状态
2. 失败可从断点重试
3. 可远程查看和控制

### Phase 2（按需）：增强可靠性

1. 存储切 Postgres
2. 全量 webhook 化 + 去重队列
3. 并发调度与配额限制
4. 审计看板（通过率/重试率/耗时）

### Phase 3（按需）：执行器 A/B

1. 增加 OpenHands 或其他 worker 通道
2. 在同类 repo 上做质量与成本对比
3. 优胜者保留，失败者只做备选

## 9. Repo Contract（首次阅读产物，必须结构化）

每个 repo 首次分析后生成一份结构化记录（JSON/YAML），至少包含：

1. `base_branch_policy`（默认分支 vs CONTRIBUTING 指定分支）
2. `required_checks`（必跑测试、lint、tox、docs）
3. `toolchain`（rye/poetry/hatch/uv/bun）
4. `integration_scenario`（A/B/C/D）
5. `expected_files`（预计改动文件清单）
6. `pr_rules`（commit/PR 模板和禁忌）
7. `risk_flags`（100% coverage、docs test、CI 特殊约束）

这一步是降低后续返工的核心抓手。

## 10. Prompt 迭代与实验机制

必须建立 `prompt_version` 追踪，不然无法科学优化。

每次运行记录：
1. repo
2. prompt_version
3. 首次 CI 通过率
4. 平均迭代轮次
5. merge 成功率
6. token/时间成本
7. 失败原因标签

每两周做一次回顾：只保留能提升指标的 prompt 变更。

## 11. Insights（本轮讨论沉淀）

1. 你当前执行层已具备生产力，替换执行器不是第一优先。
2. 真正成本在“读懂仓库 + 合规落地”，不是 patch 行数。
3. 系统要以 GitHub 事件为真相，不要维护手工影子状态。
4. 远程控制应先轻后重，先解决能控，再追求花哨入口。
5. 先解决 80% 痛点（状态闭环、重试、审计），再考虑重平台。

## 12. 警惕点（高优先级）

1. 过度工程化  
小规模阶段堆太多框架，复杂度超过收益。

2. 权限模型错误  
应优先 GitHub App 安装令牌；避免长期高权限 PAT。

3. 事件与幂等缺失  
Webhook 至少一次投递，未去重会导致重复执行。

4. CI 触发误判  
不同 token/事件触发链路不同，可能出现“看似 push 但 CI 不跑”。

5. 合并前陈旧绿灯  
必须考虑 merge queue 或 rebase 后复验，防止主干回归。

6. 未分类失败  
不区分 infra/policy/code/flaky，会让重试策略失效。

7. 审计缺失  
没有可回放日志就无法定位系统性失败。

## 13. 成功标准（季度视角）

1. 首次 CI 通过率持续提升
2. `NEEDS_HUMAN_REVIEW` 比例下降
3. 每 repo 平均人工介入时长下降
4. 从触发到 PR ready 的中位耗时下降
5. Prompt 迭代带来可测量收益（不是主观“感觉更好”）

## 14. 立即行动清单（下一步）

1. 在 `agentpr/` 下创建 `orchestrator/` 代码骨架
2. 落 `repo_contract` 数据模型与状态机枚举
3. 实现最小 `status/detail/retry/pause` 控制命令
4. 接入一个 repo 做端到端跑通
5. 用 5-10 个 repo 做稳定性验证，再决定是否进入 Phase 2

## 15. Skills 策略更新（2026-02-23）

### 15.1 Skills 在本项目的真实定位

`Skills` 是执行层能力封装，不是状态系统替代品。  
它擅长把高频、可重复、易出错的步骤标准化；不负责长期状态、事件去重、跨 repo 编排。

因此本项目应采用：
- Skills：保证执行一致性与可复用流程
- Orchestrator：保证状态闭环、重试、审计、人工介入

### 15.2 为什么建议先拆成 5 个 Skill，而不是 1 个

不是“必须 5 个”，而是“先按单一职责拆分”，5 个是当前流程的自然切片。

建议初始切片：
1. `repo-discovery-contract`：首次读仓库并生成 `repo_contract`
2. `integration-plan-minimal-diff`：确定最小改动计划（目标文件、风险点、验证项）
3. `implement-and-local-validate`：实现 + 本地验证（按 repo toolchain）
4. `pr-compliance-gate`：CONTRIBUTING/PR 规则硬门禁（branch、commit、模板、改动范围）
5. `ci-review-iteration`：处理 CI 失败与 review 评论迭代

拆分的客观收益：
1. 触发更精准：减少“单个大 Skill 误触发/漏触发”
2. 上下文更小：避免一个 Skill 塞满所有流程导致 token 膨胀
3. 回归风险更低：改一个 Skill 不会影响全链路
4. 评估更清晰：可按 Skill 统计失败率，知道问题在哪一段
5. 迁移更容易：将来换执行器时可以复用 Skill 契约

什么时候可以先做 1 个 Skill：
1. 只做单 repo PoC
2. 目标是快速验证“可用性”而非“可运营性”
3. 能接受后续拆分重构成本

## 16. Claude 官方创建 Skill 的方式（澄清）

你记忆中的功能是存在的，官方有两条路：

1. 对话创建（推荐起步）
- 在对话中说明“我想创建一个 skill for ...”
- Claude 会引导提问并生成标准化 Skill 包
- 对应官方文档：`How to create a skill with Claude through conversation`

2. 手工创建
- 编写 `SKILL.md`（含 frontmatter：`name` / `description`）
- 打包为 zip
- 在 `Settings > Capabilities > Skills` 上传
- 对应官方文档：`How to create custom Skills`

组织共享：
- Team/Enterprise 可由 Owner 统一 provision 到组织

实践建议：
1. 先用对话创建首版（快）
2. 再把内容迁移到仓库内版本化管理（稳）

## 17. 之后的整体流程（目标形态）

1. 人通过聊天入口下达任务  
示例：`把 Forge 集成到 owner/repo，优先最小改动`

2. 控制面生成 `run_id` 并入队  
写入 repo、prompt_version、优先级、预算与超时

3. 执行 `repo-discovery-contract`  
产出 `repo_contract`（branch/toolchain/rules/risk_flags/expected_files）

4. Orchestrator 做 gate 判断  
若关键条件缺失或高风险，转 `NEEDS_HUMAN_REVIEW`

5. 执行计划与实现 Skill  
按最小改动原则修改代码并完成本地验证

6. 执行 `pr-compliance-gate`  
确保符合 CONTRIBUTING/PR 规则再允许 push

7. push 后进入 `CI_WAIT`  
通过 GitHub 事件更新状态，不再人工盯仓库

8. 若 CI/review 失败，执行 `ci-review-iteration`  
自动迭代到阈值；超过阈值转人工

9. 达标后转 `DONE` + 生成审计记录  
沉淀本次 repo_contract、失败归因和 prompt 表现

10. 周期性复盘  
基于指标迭代 prompt 和 Skill，而不是凭感觉调整

> 说明：若采用“停在 push、人工开 PR”模式，则 `PUSHED` 后先等待 `pr_number` 绑定事件，再进入 `CI_WAIT`。

## 18. 最小可跑 Orchestrator：状态机与事件表

### 18.1 最小状态集合

`QUEUED`  
`DISCOVERY`  
`PLAN_READY`  
`IMPLEMENTING`  
`LOCAL_VALIDATING`  
`PUSHED`  
`CI_WAIT`  
`REVIEW_WAIT`  
`ITERATING`  
`PAUSED`  
`DONE`  
`SKIPPED`  
`NEEDS_HUMAN_REVIEW`  
`FAILED_RETRYABLE`  
`FAILED_TERMINAL`

### 18.2 事件表（MVP）

| event_type | source | 关键字段 | 典型动作 |
|---|---|---|---|
| `command.run.create` | bot/api | run_id, repo, prompt_version | 创建 workflow，状态设为 `QUEUED` |
| `worker.discovery.completed` | executor | run_id, contract_path | 写入 contract，转 `PLAN_READY` |
| `worker.step.failed` | executor | run_id, step, reason_code | 失败分类，决定重试或人工 |
| `worker.push.completed` | executor | run_id, branch | 转 `PUSHED` |
| `command.pr.linked` | bot/api | run_id, pr_number | 人工开 PR 后绑定，转 `CI_WAIT` |
| `github.pr.opened` | github | run_id or branch, pr_number | 自动开 PR 模式下绑定，转 `CI_WAIT` |
| `github.check.completed` | github | pr_number, conclusion | 通过转 `REVIEW_WAIT`，失败转 `ITERATING` |
| `github.review.submitted` | github | pr_number, state | `changes_requested` 转 `ITERATING` |
| `github.comment.created` | github | pr_number, comment_id | 进入评论处理流程 |
| `command.retry` | bot/api | run_id | 增加 attempt 并回到对应 step |
| `command.pause` | bot/api | run_id | 状态置 `PAUSED`（可选） |
| `timer.timeout` | scheduler | run_id, step | 转 `FAILED_RETRYABLE` 或人工 |

### 18.3 事件处理硬规则

1. 每个事件都带 `idempotency_key`，重复事件只处理一次  
2. 状态转移必须校验 `from_state -> to_state` 合法性  
3. 任何写操作都记录 `event_log + step_attempt + artifact`  
4. 超过重试阈值直接转人工，不无限重试  
5. `PUSHED` 之后以 GitHub 事件为准，不以本地推测为准

### 18.4 建议的最小数据表

1. `runs`：一次任务全局信息（repo、prompt_version、owner、budget）  
2. `run_states`：当前状态与最近错误  
3. `events`：原始事件与去重键  
4. `step_attempts`：每一步执行记录（耗时、退出码、stderr 摘要）  
5. `artifacts`：contract、diff、测试结果、PR 链接

## 19. 对标系统（已有实践）与可借鉴机制

### 19.1 已有对标（我们不是第一个做这件事）

1. Renovate  
核心能力：Dashboard、审批门禁、重试闭环、批量规则化运营。

2. Sourcegraph Batch Changes  
核心能力：跨 repo 统一执行、先 preview 后 publish、集中追踪 campaign 状态。

3. multi-gitter  
核心能力：同一脚本批量跑多 repo 并创建 PR，支持 status/merge/close 操作。

4. OpenHands GitHub Action  
核心能力：`fix-me` / `@openhands-agent` 触发迭代修复，基于 issue/PR 评论循环改进。

### 19.2 可直接抄的机制（而不是抄整套产品）

1. 统一看板（借鉴 Renovate Dashboard）  
将 repo 状态、待处理项、失败原因聚合到一个入口。

2. 审批门禁（借鉴 Dashboard Approval）  
高风险仓库或高风险改动必须人工放行。

3. Preview-first（借鉴 Batch Changes）  
先产出 plan/expected_files，再决定是否执行 push。

4. 事件驱动迭代（借鉴 OpenHands）  
CI 失败与 review 评论都应触发自动迭代，而不是人工轮询。

5. 批量控制（借鉴 multi-gitter）  
支持一条命令管理多仓库 run（status/retry/pause/skip）。

### 19.3 我们的边界

我们做的是“AgentPR 运营系统”，不是“另一个通用 Git 平台”：  
只做和 Forge 集成场景强相关、可复用且可审计的最小闭环。

## 20. 本轮自检（与 forge_integration 现状对齐）

### 20.1 发现的客观不一致

1. 路径基准不一致（已修复）  
`agentpr/forge_integration/claude_code_prompt.md`、`workflow.md`、`prompt_template.md` 已统一到 `agentpr/forge_integration`。

2. commit 规范冲突风险（已修复）  
`scripts/finish.sh` 已支持第三参数 `COMMIT_TITLE`，不再固定标题，便于按 repo 规范传入。

3. 分支命名策略过于固定（已修复）  
`scripts/prepare.sh` 已支持第四参数 `FEATURE_BRANCH`，默认生成唯一分支名，避免历史叠加。

### 20.2 对应修正方向

1. 引入运行模式配置  
`push_only`（默认）与 `managed_pr` 两种模式，分别决定 `PUSHED` 后的状态流转。

2. commit message 从“固定模板”改为“repo contract 驱动”（已落地）  
由 `pr_rules.commit_format` 控制标题格式，`finish.sh` 接收显式 commit title。

3. 分支命名参数化（已落地）  
分支命名建议：`feature/forge-<run_id|date>`，并在 retry 策略里定义“沿用分支/新分支”规则。

4. 在迁移完成前，统一以当前真实路径为准  
避免执行器在 `agentpr` 与 `oss-integration` 两套路径间混用。

## 21. 存储层决策：SQLite vs Postgres

### 21.1 为什么 MVP 先 SQLite

1. 单机单进程编排场景下，SQLite 足够  
2. 零运维成本，最快落地闭环  
3. 先验证状态机/事件模型，避免在基建上过早投入

### 21.2 什么时候切 Postgres

满足任一条件即可切换：
1. 多 worker 并发消费事件
2. 需要跨机器运行与远程查询
3. 事件量明显增大（轮询+webhook 并发写入）
4. 需要更强的并发控制和运维可观测

### 21.3 执行策略（避免二次重写）

1. 从 Day 1 使用同一 ORM/迁移工具（如 SQLAlchemy + Alembic）  
2. 以 `DATABASE_URL` 切换后端（SQLite/Postgres）  
3. 业务层禁止写方言专属 SQL，保持可迁移

## 22. Agent 调用方式（从人工输入到自动执行）

### 22.1 当前人工模式

人在 terminal 打开 `claude`/`codex`，手工粘贴 prompt，人工观察过程与结果。

### 22.2 目标自动模式

由 orchestrator 的 executor 进程调用 CLI/API：
1. 读取 `repo_contract` + prompt 模板并渲染
2. 以 `run_id` 创建独立工作目录/日志目录
3. 调用 agent（CLI/API）执行
4. 解析结果，写入 `events` / `step_attempts` / `artifacts`
5. 根据结果触发下一状态或重试/人工介入

### 22.3 建议调用原则

1. 优先使用可非交互执行的接口（便于编排）  
2. 每次调用绑定 `run_id`、超时、预算上限  
3. 调用失败必须产出结构化错误码（供重试分类）

## 23. 当前决策（2026-02-23）

1. 运行模式采用 `push_only`  
push 完成后不自动创建对外可见 PR，merge 仍由人工确认。

2. PR 开启采用人工 gate  
理想流程：先 push 到 fork 分支，人工在 loop 中 review diff + PR message，确认后再创建 PR。

3. 可见性认知  
GitHub 上“创建 PR”本质就是在目标仓库创建一个可见条目（即使 Draft 也可见）；  
若不希望目标仓库看到，必须停在 push（仅分支在 fork 可见，不在 upstream PR 列表出现）。

4. Bot 交互采用“LLM 解释 + 命令落地”双层  
状态变更类操作（retry/pause/skip/open_pr）必须走明确命令并二次确认；  
LLM 负责自然语言理解、摘要和建议，不直接绕过命令层执行破坏性动作。
