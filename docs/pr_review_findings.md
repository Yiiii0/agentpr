# PR Review Findings — 2026-03-09

## 目的

逐个审查 17 个已提交 PR 的代码质量和 PR body 质量，记录所有问题，最终用于改进 pipeline。

---

## 已发现的系统性问题

### 问题 1: Worker "创作"而非"复制"

Worker 倾向于在添加 Forge 支持时做额外的"改进"，而不是严格复制最近 provider 的 pattern。

**DAMO-ConvAI 实例：**
- 把 `LLM_Name = Literal[...]` 改成 `Union[Literal[...], str]` — 完全不需要（`obtain_chain` 参数已经是 `str`），且破坏了 `get_args(LLM_Name)` 验证逻辑
- 加了 `FORGE_API_KEY` 的 `ValueError` validation — together_ai 和 groq 都不做验证，这是自作主张
- 用了 `.lower()` 在 `startswith` 检查中 — 其他 provider 不用

**根因：** LLM 本性倾向于"做得更好"而不是"做得一样"。skill instructions 说 "follow existing patterns exactly"，但 LLM 理解的"follow"是"参考但改进"。

### 问题 2: PR body LLM 生成质量差

`generate_pr_description` 产生的 PR body 有多处问题：
- **Usage 编造内部 API 调用**：LLM 看到 diff 里的函数名就编了个调用示例，但这些是内部路由函数，用户不会直接调用
- **Test Evidence 输出 grading code**：`runtime_success_recovered_test_failures` 是系统内部 reason code，对 reviewer 无意义
- **About Forge 数据不准确**：写 "40+ providers" 或 "200+ models"，实际 Forge README 说的是 77 个 providers

**根因：** prompt 说 "copy-paste ready code example using actual class/function names from the diff"，但没有区分"用户面向 API"和"内部实现函数"。

### 问题 3: 三层审查全部失效

- **Worker（codex exec）**：不会自查代码是否跟 repo pattern 一致，不搜索改动的下游影响
- **Grading（runtime_analysis）**：只检查量化指标（文件数、测试通过、diff 大小），不做定性审查
- **人工 review（我）**：两次 review 都只看 diff 本身，没有搜索被修改类型/函数的其他使用位置

**根因：** 优化吞吐量（12 repos 批量）导致没有做深度 review。真正的 code review 需要：看到改动 → 搜索所有下游使用 → 判断影响。

### 问题 4: About Forge 信息不准确

PR body 中关于 Forge 的描述存在事实错误：
- 来源：部分从旧的 `pr_description_template.md` 模板，部分 LLM 自己编
- 实际：Forge 是 "open-source middleware service"（README 原话）
- **核实后的数据**：README 列出约 42 个 providers，API docs model IDs 页有 38 个 provider、~4,990 个 model ID
- "40+ providers" 实际上是对的（38-42），"200+ models" 严重低估（实际 ~4,990）
- "gateway" 不准确，应为 "middleware service"
- **注意**：之前我们自己错误地 "纠正" 成 "77 个 providers"，反而引入了新的错误数字

### 问题 5: "最近 provider" 选择错误

Worker（和 reviewer）倾向于选同目录下随便一个 provider 对比，而不是选**最相似**的 provider。

**pipecat 实例：**
- Forge 是聚合 router，最相似的 provider 是 OpenRouter（同为聚合 router）
- 但 Worker 参照了 Groq/Together（直连 provider），导致 `api_key` 和 `base_url` 的处理方式不一致
- reviewer（我）也犯了同样错误 — 一开始参照 Groq 把 `api_key` 改成 required，被用户叫停后才去看 OpenRouter

**根因：** "follow existing patterns exactly" 的前提是找对参照对象。没有明确要求 "先看所有相关 provider，选最相似的"。

### 问题 6: Reviewer 自身的过度修正

修 PR 时不够谨慎，从一个极端（Worker 乱加东西）跳到另一个极端（过度删减/修改）。

**pipecat 实例：**
- 发现 Worker 加了不该有的 `os.getenv()` → 正确
- 但同时把 `api_key: Optional[str] = None` 改成 `api_key: str` → 错误，应该保持 Optional（OpenRouter 就是 Optional）
- 如果不是用户要求停下来看 OpenRouter，这个过度修正就会被 force push

**根因：** 急于修复，没有先看完所有参照再动手。正确流程：发现问题 → 看所有相关 provider → 确定最小改动 → 再动手。

### 问题 7: Forge 事实数据未核实

之前写 "77 个 providers" 并记录在文档里，但从未回源核实。实际 README 列出约 42 个，API docs 有 38 个有 model ID 的 provider，~4,990 个 model ID。

讽刺的是：原始 PR body 写的 "40+ providers" 其实是近似正确的，我们"纠正"成 "77" 反而引入了错误。

**根因：** 某个环节产生了 "77" 这个数字，后续所有引用都没有回源验证。教训：任何需要写进 PR body 的数据都要回源核实，不要轻易"纠正"没有证据表明错误的数字。

### 问题 8: PR body 营销文本来自模板，不是 LLM

所有 17 个 PR 的 Motivation / Why Forge / Key Benefits 文本**完全一致**，因为它们来自 `forge_integration/pr_description_template.md` 的静态模板，不是 LLM 生成的。

**数据流追踪：**
1. `cli_pr.py _load_about_forge_text()` 从 `pr_description_template.md` 提取 `## About Forge` 到文件末尾的所有内容
2. 这包含了 About Forge + Motivation + Why Forge + Key Benefits + References — 全部是营销文本
3. 拼接到每个 PR body 的末尾

**根因在 pipeline：** `pr_description_template.md` 本身包含不该出现在 PR body 里的营销内容。不是 LLM 的问题。

**Pipeline 修复（后续）：**
- `pr_description_template.md`: 删除 Motivation/Why Forge/Key Benefits，只保留 About Forge（用核实后的标准描述）+ References
- 或者：`_load_about_forge_text()` 改为只提取 `## About Forge` 到下一个 `## ` 之间的内容，不提取后续 sections

### 问题 9: Worker 代码质量与 repo 扩展性强相关

跨 4 个 repo 的对比发现，Worker 代码质量不取决于 Worker 本身，而取决于 repo 是否有好的扩展机制：

| Repo | Worker 做法 | 代码质量 | 原因 |
|---|---|---|---|
| ai-gradio | `functools.partial` 复用 registry | ✅ 干净 | 只填参数，无发挥空间 |
| DeepCode | registry 加 ProviderSpec | ✅ 干净 | 声明式配置，无发挥空间 |
| pipecat | 写新 class | ⚠️ os.getenv 多余 | 独立代码 → 开始"创作" |
| DAMO-ConvAI | 改现有函数加 elif | ❌ Union type 等 | 改现有代码 → 开始"改进" |

**规律：** 好的 repo 抽象 → Worker 只需填参数 → 没有空间犯错。差的抽象 / 独立代码 → Worker 开始"做得更好"。

**Skill 改进方向：** "优先使用 repo 已有的扩展机制（registry, factory, partial, config entry），不要写独立实现。如果必须写独立代码，逐行对比参照 provider，不添加参照没有的东西。"

### PR body 问题的完整分层

| 问题 | 来源 | Pipeline 根因 | 已提交 PR 的修复方式 |
|---|---|---|---|
| 营销文本 | 模板 `pr_description_template.md` | 模板包含不该有的 sections | 手动删除 |
| 编造 Usage | LLM `generate_pr_description()` | prompt 没区分用户 API vs 内部函数 | 手动重写 |
| grading code 作为 Test Evidence | LLM `generate_pr_description()` | evidence dict 包含系统内部 reason code | 手动重写 |
| About Forge 称 "gateway" | 模板 | 模板用词不准确 | 换标准描述 |
| About Forge 数据错误 | 模板 + LLM | 模板数据未核实，LLM 自己编 | 换标准描述 |

---

## 逐 PR 审查记录

### 1. DAMO-ConvAI #226 ✅ 已修复

**代码问题（已修复）：**
- `LLM_Name` 改成 `Union[Literal, str]` — 破坏 `get_args()` 验证，已 revert
- `ValueError` validation — 不符合 repo pattern，已删除
- `.lower()` in startswith — 不符合 repo pattern，已删除

**PR body 问题（已修复）：**
- Usage 编造 `obtain_chain()` 内部调用 — 已改为 env var + model prefix
- Test Evidence 写 grading code — 已改为实际描述
- About Forge 过长营销文 — 已精简

**最终状态：** 1 commit, 1 file, +18/-1, body 已更新

### 2. pipecat #3955 ✅ 已修复

**代码问题（已修复）：**
- `api_key: Optional[str] = None` + `os.getenv("FORGE_API_KEY")` 内部 fallback — OpenRouter 不做 env var fallback，直接传给 parent
- `base_url: Optional[str] = None` + `os.getenv("FORGE_API_BASE")` fallback — OpenRouter 用 `str = "url"` 有默认值
- `import os` + `DEFAULT_FORGE_BASE_URL` 常量 — OpenRouter 不需要
- 参数顺序 `api_key, base_url, model` — OpenRouter 是 `api_key, model, base_url`

**PR body 问题（已修复）：**
- 写 "ForgeLLM" class — 实际是 `ForgeLLMService`
- 写 "FORGE_API_URL" — 实际是 `FORGE_API_KEY`
- "40+ upstream providers" — 实际核实后 40+ 是准确的（README 42 个，API docs 38 个）
- Motivation/Why Forge 营销文本 — 已删除
- "tested locally" 无具体信息 — 已改为 "874 tests passed"

**review 过程中的教训：**
- 一开始错误地参照 Groq/Together 把 `api_key` 改成 required `str` — 后来发现 OpenRouter（最相似的聚合 provider）用的是 `Optional[str] = None`
- 教训：**"最近 provider" 不等于 "同目录随便选一个"，要选最相似的**。Forge 和 OpenRouter 都是聚合 router，应该参照 OpenRouter
- 教训：**改之前先看完所有相关 provider，不要看完两个就下结论**

**最终状态：** 1 commit, 5 files (2 new + 3 modified), body 已更新

### 3. DeepCode #116 ✅ 已修复

**代码：无问题。** 4 files, +26/-1. 严格匹配 AiHubMix（最相似 gateway）pattern。env_extras 的 `os.environ` 改动是 repo 既有 pattern 的正确扩展，不是 Worker 自加。

**PR body 问题（已修复）：**
- Motivation/Why Forge/Key Benefits 营销文本 — 已删除
- About Forge 用了旧模板 — 已更新为标准描述
- Usage 改为用户视角的 yaml config 示例

**最终状态：** 1 commit, 4 files, code 无需改动, body 已更新

### 4. ai-gradio #28 ✅ 已修复

**代码：无问题。** 4 files. Worker 用 `functools.partial(openai_registry, ...)` 复用已有 registry — 没有空间犯错。model names `"OpenAI/gpt-4o-mini"` 正确（发给 Forge API，期望 Provider/model 格式）。

**PR body 问题（已修复）：**
- Usage 编造 `custom_load()` 内部 API — 改为 `gr.load(name="forge:...", src=ai_gradio.registry)` 用户 API
- Test Evidence 输出 grading code `runtime_success_recovered_test_failures` — 改为实际描述
- 营销文本 — 已删除
- About Forge — 已更新标准描述

**此 PR 验证了问题 9（repo 扩展性与代码质量的关系）：** repo 有 `functools.partial` + 参数化 `registry()` → Worker 只填参数 → 代码干净。

**最终状态：** 1 commit, 4 files, code 无需改动, body 已更新

### 5. openlit #1040 ✅ 已修复

**代码：无问题。** 6 files (Python 2 + TS 2 + docs 2). Worker 在现有 `llm_response_openai()` 函数里加 Forge 检测逻辑，不新建 provider class。`api_key` 通过 constructor kwargs 传递（不 mutate os.environ），model name `"OpenAI/gpt-4o-mini"` 正确。

**review 教训：** 自动化 agent 标记了 12 个 "issues"，但逐个验证后：
- 5 个是 pre-existing repo patterns（os.environ mutation in `setup_provider()`, code duplication across 4 files, etc.）— 不是我们的代码
- 3 个是正确行为（Forge model naming, domain detection, control flow）
- 4 个是 nice-to-have（docstring, env.example, TS types, default model consistency）— 不在我们的改动范围内

**教训：** 不要过度 review。我们的 review 范围是 "Forge 代码是否破坏了什么 + 是否匹配 repo pattern"，不是 "repo 本身应该怎么改进"。

**PR body 问题（已修复）：** 营销文本删除，About Forge 标准描述，Usage 改为用户视角。

**最终状态：** 1 commit, 6 files, code 无需改动, body 已更新

### 6. Absolute-Zero-Reasoner #34 ✅ 已修复

**代码：无问题。** 2 files, +20/-4. Worker 把 class-level `client` 重构为 instance-level `_build_client()`，使不同 instance 可以用不同 provider。结构性改动合理，不 mutate os.environ，Forge 不存在时行为一致。

**PR body（已修复）：** 营销删除 + 标准描述 + 准确 Usage

**最终状态：** 1 commit, 2 files, code 无需改动, body 已更新

### 7. PRIME #65 ✅ 已修复

**代码：无问题。** 2 files, +10/-1. 修改现有 `main.py` 和 `oai_runner.py`，无新建文件。

**参照分析：** PRIME 没有 provider registry，代码直接在 `oai_runner.py` class-level 初始化 `OpenAI` client。Worker 用 `if os.getenv("FORGE_API_KEY")` 做 class-level 分支，**匹配**现有 class-level `OpenAI(api_key=os.getenv("OPENAI_KEY"))` pattern。

**代码详情：**
- `oai_runner.py`: Class-level `if os.getenv("FORGE_API_KEY")` → 用 Forge credentials 初始化 client。matches existing class-level pattern
- `main.py`: `"/" in args.model` 检测 Forge model naming → 设为 `OpenAIChat` style（原来 unknown models fallback 到 `Qwen25Math` → `VLLMRunner`）。必要改动：没有这个检测，Forge models 会路由到本地 vLLM
- `o1-` prefix 检测：routing reasoning models 到 `OpenAIReason` style（不同 kwargs）。必要，否则 o1 models 会收到错误参数

**为什么不修代码：**
- 代码与 repo 现有 pattern 一致（class-level client 初始化）
- `os.getenv()` 是唯一的 env var 读取方式（repo 没有 config 系统）
- 修改项全部由 `FORGE_API_KEY` guard — 不影响现有行为

**PR body 问题（已修复）：** Usage 编造了 Python import 示例（PRIME 是 CLI 工具不是库）→ 改为 bash CLI 用法。营销文本 + 标准描述。

**最终状态：** 2 files, code 无需改动, body 已更新

### 8. inspect_ai #3439 ✅ 已修复

**代码：无问题。** 4 files (1 new + 1 modified + 2 docs). `ForgeAPI(OpenAICompatibleAPI)` 严格匹配 OpenRouter 和 Together 的 pattern。

**参照分析（OpenRouter 和 Together in inspect_ai）：**
- OpenRouter: `OpenAICompatibleAPI.__init__(service="OpenRouter", service_base_url="https://openrouter.ai/api/v1")`
- Together: `OpenAICompatibleAPI.__init__(service="Together", service_base_url="https://api.together.xyz/v1")`
- Forge: `OpenAICompatibleAPI.__init__(service="Forge", service_base_url="https://api.forge.tensorblock.co/v1")` ✅
- `canonical_name()`: 返回 `self.service_model_name()` — 与 Together 一致 ✅
- `model_base_url()` 解析 env var — Together 也用这个 utility ✅
- 注册方式 `@modelapi(name="forge")` — 与所有 provider 一致 ✅

**唯一细节：** 定义了两个 env var (`FORGE_API_BASE`, `FORGE_BASE_URL`) 通过 `model_base_url()` list 传入。其他 provider 只有一个。这是 Worker 加的小冗余，但不影响功能。不值得为此改代码。

**此 PR 进一步验证了问题 9：** inspect_ai 的 `OpenAICompatibleAPI` 提供了强抽象，Worker 只需填 service name + base URL + 注册 → 几乎没有犯错空间。

**PR body 问题（已修复）：** 保留了 repo 的 PR template（checkboxes），替换了 current/new behavior 中的营销文本，About Forge 标准描述。

**最终状态：** 4 files, code 无需改动, body 已更新

### 9. py-gpt #173 ✅ 已修复

**代码：有 Worker "创作" 但不改。** 6 files (1 new + 5 modified). `ForgeLLM(BaseLLM)` 整体匹配 OpenRouter pattern，但有几处偏差。

**参照分析（OpenRouter in py-gpt）：**
- OpenRouter: 直接在 `llama()` 和 `get_embeddings_model()` 里 inline auth：`if "api_key" not in args: args["api_key"] = window.core.config.get(...)`
- Forge: 提取了 `_apply_auth()` helper 方法，DRY 但 OpenRouter 没有这个抽象 — Worker "改进"
- OpenRouter: 只从 `window.core.config.get()` 读 API key
- Forge: `os.environ.get("FORGE_API_KEY") or window.core.config.get(...)` — 加了 env var fallback，OpenRouter 没有
- OpenRouter: `if "api_key" not in args`
- Forge: `if "api_key" not in args or args["api_key"] == ""` — 加了空字符串检查
- `prepare_client_args()` 的 forge 分支也加了 `os.environ.get()` fallback，OpenRouter 的分支只用 `cfg.get()`

**为什么不修代码（重要决策）：**
1. `os.environ.get()` fallback 虽然 OpenRouter 没有，但**不破坏任何东西** — 只是多提供了一种配置方式
2. `_apply_auth()` 提取是合理的 DRY refactor，maintainer 不太可能反对
3. 如果强行改成 OpenRouter pattern（inline auth、删 env var fallback），改动量反而更大 — 违反"最小改动"原则
4. 删 `os.environ.get()` 可能**破坏**已经用 env var 配置的用户

**教训：** Worker "创作" 不总是需要修复。当"创作"是 additive（加了功能但没破坏什么）且修复的成本（改动量 + 风险）大于收益时，应该保留。这与 pipecat 案例不同 — pipecat 的 `os.getenv()` fallback 导致了参数类型和默认值的连锁偏差。

**PR body 问题（已修复）：** Usage 编造了 Python import 示例（py-gpt 是桌面应用，用 GUI 配置）→ 改为 GUI 步骤。营销文本 + 标准描述。

**最终状态：** 6 files, code 无需改动, body 已更新

### 10. Controllable-RAG-Agent #26 ✅ 已修复

**代码：无问题。** 3 files, +56/-10. Worker 创建了 `create_chat_llm()` / `create_embeddings()` factory functions，替换 13 处直接 `ChatOpenAI()` 调用。

**亮点：** 原代码有 `os.environ["OPENAI_API_KEY"] = os.getenv('OPENAI_API_KEY')`（读 env 再写回 env），Worker 移除了这个不必要的 mutation，改为本地变量。这是 Worker "改进" 但实际上是正确的修复。

**为什么不修代码：** factory function pattern 集中了 Forge 逻辑，避免 13 处重复修改。`_forge_model_name()` auto-prefix 合理。Repo 没有 provider registry，factory 是最佳方案。

**最终状态：** 3 files, code 无需改动, body 已更新

### 11. AgentFlow #37 ✅ 已修复

**代码：无问题。** 5 files (2 code + 3 docs). `forge-` 前缀检测匹配 repo 惯例（`together-`, `vllm-`, `litellm-`, `ollama-`）。

**参照分析：** `factory.py` 的 prefix-based dispatch 是 repo 的核心 pattern。Worker 在 `openai.py` 的 `ChatOpenAI.__init__` 加了 `api_key`/`base_url` 参数 — 必要改动，其他 provider（如 Azure）也有类似参数化。

**细节：** factory 设了默认 temperature=0.7, frequency_penalty=0.5 等 — OpenAI path 不设默认值。这是 Worker "creativity" 但 kwargs.get() 允许 caller 覆盖，不破坏现有行为。

**最终状态：** 5 files, code 无需改动, body 已更新

### 12. octotools #53 ✅ 已修复（深度 review 发现 bug）

**代码：发现 factory 路由 bug，已修复。** 3 files (1 code + 2 docs) + 我们修了 2 files。

**深度 review 发现的问题：**
Worker 在 `openai.py` 加了 `forge/` prefix 检测，但**忘了修改 `factory.py`**。factory 根据模型名关键词路由（`"gpt" in model_string` → ChatOpenAI, `"claude" in model_string` → ChatAnthropic）。结果：
- `forge/OpenAI/gpt-4o-mini` → 恰好能工作（因为 "gpt" 在字符串中）
- `forge/Anthropic/claude-3-haiku` → **错误路由到 ChatAnthropic**（用 Anthropic SDK 直连，不走 Forge）
- `forge/Meta/llama-3-70b` → **ValueError**（无匹配 pattern）

AgentFlow #37 正确处理了这个 — 在 factory 最前面加了 `forge-` 前缀检测。octotools 的 Worker 漏了。

**README 文档也不一致：** README 说 `OpenAI/gpt-4o-mini`（无 forge/ 前缀），但代码要求 `forge/OpenAI/gpt-4o-mini`。已修复。

**修复内容：**
1. `factory.py`: 在顶部加 `if model_string.startswith("forge/"):` → 路由到 ChatOpenAI
2. `README.md`: 统一用 `forge/Provider/model-name` 格式

**openai.py 本身没问题：**
- `self.is_forge` flag + prefix stripping — 干净 ✅
- Cache path `model_string.replace("/", "_")` — 必要修复 ✅
- `is_pro_reasoning_model = False` for Forge — 正确 ✅
- 帮助性错误消息 — 有用 ✅

**为什么之前浅看没发现：** 只看了 diff 文件列表和 openai.py 代码，没想到去检查 factory.py 是否需要同步修改。必须理解代码的完整路径（factory → engine），不能只看被改动的文件。

**最终状态：** 代码已修复 + pushed, body 已更新

### 13. kit #191 ✅ 已修复

**代码：无问题。** 2 files (1 code + 1 docs). 最小改动 — 把 Forge map 到内部的 `LLMProvider.OPENAI`。

**实现：** `is_forge_provider` flag → 设 Forge-specific defaults (model, api_key, base_url) → 内部走 OpenAI path。这是正确的架构选择 — Forge 就是 OpenAI-compatible，不需要新 provider type。

**最终状态：** 2 files, code 无需改动, body 已更新

### 14. ScaleCUA #18 ✅ 已修复

**代码：功能正常，较大改动。** 6 files 跨 evaluation/ 和 playground/ 两个目录（repo 既有的代码重复 pattern）。

**实现：** `_resolve_openai_config()` helper 检测 `FORGE_API_KEY` → 如果有则优先用 Forge。auto-prefix model name with provider if no `/`。

**注意：** FORGE_API_KEY 设了就总是用 Forge（即使同时有 OPENAI_API_KEY）。这可能 surprise 同时设两个 key 的用户，但 Worker 选择了 "Forge-first" 逻辑，行为清晰。

**最终状态：** 6 files, code 无需改动, body 已更新

### 15. elasticsearch-labs #528 ✅ 已修复（深度 review 确认）

**代码：非常干净。** 3 files (1 code + 1 docs + 1 env.example). 严格匹配 repo 的 factory pattern。

**深度 review（参照 init_openai_chat）：**
- `init_forge_chat()` 完全复制 `init_openai_chat()` 的所有参数（model, streaming, temperature, model_kwargs/stream_options）
- 仅多加 `api_key=os.getenv("FORGE_API_KEY")` 和 `base_url=os.getenv("FORGE_API_BASE", FORGE_BASE_URL)` — 最小必要改动
- OpenAI 不需要显式 `api_key` 因为 langchain 默认读 `OPENAI_API_KEY` → Forge 必须显式传 → 正确
- Factory 注册 `"forge": init_forge_chat` 匹配现有格式
- README 和 env.example 格式匹配其他 provider

**注意：** CLA 未签署（Elastic CLA），需要处理。

**验证了问题 9：** 好的 repo 抽象（factory + dict registry）→ Worker 只需填参数 → 代码干净。

**最终状态：** 3 files, code 无需改动, body 已更新

### 16. judgeval #713 ✅ 已修复（深度 review 确认）

**代码：非常干净。** 2 files (1 docs + 1 test). 无需改核心代码 — judgeval 的 `wrap()` 函数包装标准 OpenAI client，Forge 通过 `base_url` 直接可用。

**深度 review：**
- **README**: `from judgeval.tracer import wrap` import 匹配 repo 现有 README pattern（第 113、210 行用同样的 import）
- **Test**: 使用 `wrap_openai_client_sync(tracer, client)` 匹配 conftest.py 的内部 wrapper fixture 模式
- `pytest.skip` 处理 `FORGE_API_KEY` 缺失 — 与 conftest.py 的 `openai_api_key` fixture 模式一致
- `expected_span_name="OPENAI_API_CALL"` 正确（Forge 走 OpenAI path）
- **小差异**: Forge test 没有 response 断言（现有 test 有 `assert response.choices` 等），但 test 重点是 instrumentation span
- Gemini bot 建议提取 magic string URL 为常量 — 合理但 minor

**Worker 做对的事情：** 没有改任何核心代码，只加了文档和测试。这是最理想的 integration — 利用现有 OpenAI-compatible infrastructure。

**PR 外部状态：** gemini-code-assist 和 propel-code-bot 自动 review 完成，无 blocker。

**最终状态：** 2 files, code 无需改动, body 已更新

### 17. weave #6297 ✅ 已修复（elif 路由 bug）

**代码：发现关键 bug，已修复。** 2 files (1 code + 1 AGENTS.md). 集成到 Weave 的 LLM Completions routing。

**深度 review 发现的 bug：**
`_setup_completion_model_info()` 的 elif 分派链中，Forge 的 `elif "/" in model_name:` 放在了 `elif model_info:` 前面，导致 **937 个包含 `/` 的 model**（占 provider map 1409 个 model 的 66%）被 Forge 分支拦截，无法到达 `model_info` 分支。

**影响范围：**
- **404 个 SKIP-prefix model**（azure/*, vertex_ai/*, bedrock/*）：原来走 `elif model_info:` 正确获取 API key → 被 `elif "/" in model_name:` 拦截，prefix 在 SKIP 里不做 Forge 路由，但也不 return → 落到默认 return，`api_key=None, provider="openai"` → API 调用失败
- **511 个非 SKIP model**（deepseek/*, groq/*, fireworks_ai/*, mistral/*, openai/*, xai/*）：如果有 FORGE_API_KEY → 被静默路由到 Forge（可能非预期）；如果没有 FORGE_API_KEY → 同样落到默认 return，`api_key=None` → 失败

**根因：** Worker 把 Forge 检测作为 `elif` 插在 `is_explicit_custom` 和 `model_info` 之间，但 `elif` 是互斥的 — 一旦匹配 `"/" in model_name`，`model_info` 分支永远不会被执行。

**修复（手动完成）：** 把 `elif model_info:` 整块移到 `elif "/" in model_name:` **之前**。同时初始化 Forge 分支的 `extra_headers = {}`。
```
修复后 elif 顺序：is_coreweave → is_explicit_custom → model_info → "/" in model_name (Forge) → default return
```

**Worker 修复尝试（失败）：** 让 pipeline 的 manager → worker 尝试修复，Worker 只加了 `extra_headers = {}` 初始化（修了一个次要的变量初始化问题），完全没有触碰 elif 路由 bug。Worker commit 已清除，替换为手动的正确修复（commit 3a0529d）。

**Pipeline 修复能力的教训：**
- Worker 对 task packet 中描述的 bug 理解不够，选了最表面的修复（变量初始化）而不是根本问题（elif 顺序）
- 即使 task packet 详细描述了 root cause 和 fix approach，Worker 仍然没有正确执行
- 此类涉及代码结构重排的 bug 可能超出 Worker 当前能力，需要人工介入

**为什么之前浅看没发现：** 浅看只看了 Forge 代码本身的逻辑（SKIP_PREFIXES、normalize、extra_headers），没有分析 elif 链的互斥性对 **其他 937 个 model** 的影响。必须理解整个 dispatch 函数的结构，不能只看新增代码。

**实现细节（Forge 代码本身质量尚可）：**
- `_normalize_forge_model_name()`: 小写化 provider prefix — 合理
- `FORGE_SKIP_PREFIXES`: 修复后意图正确 — model_info 优先处理已知 provider，SKIP 作为第二层防护
- `secret_fetcher` 获取 API key — 匹配函数内其他 provider 的 pattern
- `extra_headers` 处理：保留现有 headers → 匹配 Weave 的 header 管理 pattern

**旧 body 问题：** About Forge 写 "200+ LLM models"（实际 ~4,990），链接错误（docs.forge.tensorblock.co 不存在），底部有 "Generated with AgentPR"。全部已修复。

**PR 外部状态：** maintainer jwlee64 回复 "This is awesome, I can look at this by end of week"。需签 CLA + approve CI。

**最终状态：** elif 路由 bug 已修复（手动，commit 3a0529d），body 已更新。需 force push 到 PR 分支

---

## Forge 标准描述（核实后）

后续所有 PR body 的 About Forge 统一用这个版本：

> [Forge](https://github.com/TensorBlock/forge) is an open-source middleware service for unified AI model provider management. It routes requests across 40+ AI providers with access to thousands of models through a single OpenAI-compatible API.
>
> - Repo: https://github.com/TensorBlock/forge
> - Docs: https://www.tensorblock.co/api-docs/overview

**数据来源（2026-03-09 核实）：**
- "40+ providers": README 列 42 个，API docs/model-ids 页 38 个有 model ID → 40+ 准确
- "thousands of models": API docs/model-ids 页 ~4,990 个 model ID → 准确
- "open-source middleware service": README 原话
- 不要用：gateway、inference service、proxy 等未经 README 确认的描述

---

## 核心 Review 原则

从 DAMO-ConvAI 和 pipecat 的 review 中提炼的原则，适用于后续所有 PR：

### 1. 先找对参照，再动手

不要看到一个 provider 就开始对比。先扫描所有相关 provider，找**最相似**的：
- Forge 是聚合 router → 找 OpenRouter/LiteLLM 等同类，不是随便找 Groq
- 直连 provider → 找同类直连 provider
- 如果 repo 里没有聚合 provider → 找最相近的架构（如 subclass 最近的 base class）

### 2. 最小改动原则

改动 = 风险。每一行 diff 都需要回答："为什么需要这个改动？"
- 删多余的东西 ✅（`os.getenv` fallback、`ValueError` 验证等 Worker 自加的）
- 改已有的参数类型 ⚠️ — 除非有明确参照说应该不同
- 加新的东西 ❌ — 除非参照 provider 有

### 3. 发现问题不等于立刻修复

正确流程：发现问题 → 看完所有参照 → 确定最小改动 → 再动手。
反模式：发现 Worker 乱加了 `os.getenv()` → 顺手把 `api_key` 也改了 → 过度修正。

### 4. 数据必须回源

任何写进 PR body 的数字/事实，必须从源头（Forge README、API docs）核实：
- 不要引用记忆中的数字（"77 providers" 事件）
- 不要轻易"纠正"一个近似正确的数字
- 不确定 → 用模糊但安全的表述（"multiple providers"）

### 5. 理解产品再 review 代码

ai-gradio review 中，自动化 review agent 把 `"OpenAI/gpt-4o-mini"` 标记为 CRITICAL bug — 认为 OpenAI API 不接受带 `/` 的 model name。但实际上这个 model name 是发给 **Forge API**（base_url 已经指向 Forge），而 Forge API **要求** `Provider/model` 格式。

教训：不理解产品（Forge 的 model naming 约定）就 review 代码，会产生 false positive。Review 前要确认：请求发给谁？那个 API 期望什么格式？

### 6. Review 范围 = 我们的改动，不是 repo 本身

openlit 的 automated review agent 标记了 12 个 issues，其中 5 个是 pre-existing repo patterns。我们的 review 范围是：
- Forge 代码是否破坏了什么？
- Forge 代码是否匹配 repo 的现有 pattern？
- 是否有多余的"改进"？

不是：repo 本身的架构应该怎么改进。

### 7. PR body = 给 repo maintainer 看的

- Usage 必须是**用户视角**（env var + 实例化代码），不是内部实现函数
- Summary 描述**实际代码改动**，不是泛泛的 "adds Forge support"
- Test Evidence 写**实际跑了什么、结果是什么**，不是 grading system 的内部 reason code
- About Forge 用上面的标准描述，不写营销文

---

## 审查标准

对每个 PR 检查：

### 代码
1. **只改了必要的文件吗？** 是否碰了不需要碰的类型定义、import、配置？
2. **严格复制最相似 provider pattern？** 先看所有相关 provider，选最相似的（Forge 是聚合 router → 找 OpenRouter，不是随便找 Groq）。逐行对比
3. **搜索下游影响？** 被改动的函数/类型/变量在 codebase 其他地方怎么用？
4. **环境变量处理一致？** 跟其他 provider 的 env var 读取方式一致吗？
5. **没有多余的"改进"？** validation、error handling、type changes 是否超出了必要范围？

### PR Body
1. **Summary 准确？** 描述的是实际改动，不是泛泛的"adds Forge support"
2. **Changes 列表准确？** 文件名和描述是否匹配实际 diff
3. **Usage 是用户视角？** 应该是 env var + model name format，不是内部 API 调用
4. **Test Evidence 真实？** 不输出 grading code，描述实际测试情况
5. **About Forge 事实准确？** 40+ providers，~4,990 model IDs，open-source middleware service，不编数据。回源核实：README + tensorblock.co/api-docs/model-ids
