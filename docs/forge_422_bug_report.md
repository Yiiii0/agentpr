# Forge `/v1/responses` 端点 422 Bug 报告

## 问题概述

通过 Forge 的 `/v1/responses` 端点使用 OpenAI Responses API 时，**单轮对话正常，但任何涉及工具调用（function call）的多轮对话都会在第二轮请求时返回 `422 Unprocessable Entity`**。

这意味着所有依赖 Responses API + 工具调用的客户端（如 OpenAI Codex CLI）在通过 Forge 使用时，只要 agent 执行了任何工具就会崩溃。

## 根本原因

`app/api/schemas/openai.py` 中 `ResponsesItemReasoning` 的 `id` 字段被定义为**必填**（required）：

```python
class ResponsesItemReasoning(BaseModel):
    id: str              # ← 必填，但 codex 等客户端不一定传这个字段
    summary: list[object]
    type: str
    content: list[object] | None = None
    encrypted_content: str | None = None
    status: str | None = None
```

但 OpenAI 的 Responses API 规范中，客户端回传 reasoning item 时 `id` **不是必须的**。典型场景：模型执行工具调用后，客户端将上一轮的 output（包含 reasoning item）拼入下一轮的 input。当客户端未请求 `reasoning.encrypted_content` 时，reasoning item 只携带空的 `summary` 和 `type`，不包含 `id`。

Pydantic 验证流程：
1. Forge 收到 input 为 list 的请求
2. 尝试将每个 item 匹配到 `ResponsesRequest.input` union 类型中的某个模型
3. reasoning item（无 `id`）匹配 `ResponsesItemReasoning` 失败（`id` required）
4. 也无法匹配 union 中的任何其他类型
5. list 分支整体失败 → 退回到 str 分支 → 也失败 → 返回 422

注意：同文件中 `ResponsesItemFunctionToolCall` 的 `id` 已经是 `str | None = None`（可选），说明原本的设计意图就是允许 id 缺失，`ResponsesItemReasoning` 的 required 约束是遗漏。

## 建议修复

将 `ResponsesItemReasoning.id` 改为可选：

```python
class ResponsesItemReasoning(BaseModel):
    id: str | None = None    # ← 改为可选
    summary: list[object]
    type: str
    content: list[object] | None = None
    encrypted_content: str | None = None
    status: str | None = None
```

一行改动，无副作用。下游转发逻辑使用的是 `model_dump(exclude_unset=True)`，不会注入多余的 null 字段。

### 潜在的同类问题

建议同时排查 union 中其他 item 类型的 `id` 是否也应改为可选（对齐 OpenAI 的实际行为）：

| 类型 | 当前 `id` 定义 | 建议 |
|---|---|---|
| `ResponsesItemReasoning` | `str` (required) | → `str \| None = None` **（本次 bug）** |
| `ResponsesItemFunctionToolCall` | `str \| None = None` | 已正确 |
| `ResponsesItemLocalShellCall` | `str` (required) | 建议改为可选 |
| `ResponsesItemFileSearchToolCall` | `str` (required) | 建议改为可选 |
| `ResponsesItemComputerToolCall` | `str` (required) | 建议改为可选 |
| 其他 ToolCall 类型 | 各异 | 逐一核对 |

此外，以下 Output 类型已定义但**未加入 `ResponsesRequest.input` 的 union 列表**中：
- `ResponsesItemOutputMessage`
- `ResponsesItemFunctionToolCallOutput`
- `ResponsesItemLocalShellCallOutput`
- `ResponsesItemComputerToolCallOutput`

目前这些类型能被 union 中的其他兼容模型（如 `ResponsesInputMessageItem`、`ResponsesItemCustomToolCallOutput`）"兜底"匹配，暂时不会报错。但建议也将它们显式加入 union 以提高 schema 的准确性和鲁棒性。

## 影响范围

- **所有**通过 Forge `/v1/responses` 使用工具调用的客户端
- OpenAI Codex CLI（codex exec）是最典型的触发者
- 不限于 agentpr，任何使用 Responses API + function calling 的用户都会遇到

## 验证证据

### 测试环境

- Forge API: `https://api.forge.tensorblock.co/v1`
- 模型: `tensorblock/gpt-5.2-codex`
- 客户端: codex-cli，通过 `wire_api="responses"` 连接

### 实验 1：单轮对话（无工具调用）— 成功

```bash
codex exec --json \
  -c 'model_providers.forge.wire_api="responses"' \
  --model "tensorblock/gpt-5.2-codex" \
  "Say OK"
# → 200 OK, 返回 "OK"
```

### 实验 2：多轮对话（有工具调用）— 失败

```bash
codex exec --json --sandbox read-only \
  -c 'model_providers.forge.wire_api="responses"' \
  --model "tensorblock/gpt-5.2-codex" \
  "Read /tmp/test.md and tell me the content."
# 第一轮: 200 OK (模型决定执行 cat 命令)
# 第二轮: 422 Unprocessable Entity (发送工具结果时失败)
```

### 实验 3：代理抓包定位

通过本地 HTTP 代理拦截 codex 发给 Forge 的请求：

**第一轮请求**（成功）— input 有 4 个 item：
```
[0] type=message  role=developer  (permissions)
[1] type=message  role=user       (AGENTS.md)
[2] type=message  role=user       (environment)
[3] type=message  role=user       (用户 prompt)
```

**第二轮请求**（422 失败）— input 有 7 个 item，新增 3 个：
```
[4] type=reasoning    ← 无 id 字段！
[5] type=function_call
[6] type=function_call_output
```

Item [4] 的实际 JSON：
```json
{
  "type": "reasoning",
  "summary": [],
  "content": null,
  "encrypted_content": null
}
```

### 实验 4：精确复现（直接 curl，绕过 codex）

```bash
# 不带 id 的 reasoning → 422
curl -X POST ".../v1/responses" -d '{
  "model": "tensorblock/gpt-5.2-codex",
  "input": [
    {"type":"message","role":"user","content":[{"type":"input_text","text":"Say OK"}]},
    {"type":"reasoning","summary":[],"content":null,"encrypted_content":null},
    {"type":"function_call","name":"exec_command","arguments":"{}","call_id":"call_123"},
    {"type":"function_call_output","call_id":"call_123","output":"hi"}
  ]
}'
# → 422 Unprocessable Entity

# 带 id 的 reasoning → 通过 schema 验证（400 是业务逻辑层的 id 不存在错误）
curl -X POST ".../v1/responses" -d '{
  "model": "tensorblock/gpt-5.2-codex",
  "input": [
    {"type":"message","role":"user","content":[{"type":"input_text","text":"Say OK"}]},
    {"type":"reasoning","id":"rs_dummy","summary":[],"content":null,"encrypted_content":null},
    {"type":"function_call","name":"exec_command","arguments":"{}","call_id":"call_123"},
    {"type":"function_call_output","call_id":"call_123","output":"hi"}
  ]
}'
# → 400 Bad Request ("Item with id 'rs_dummy' not found")
# ↑ 通过了 Pydantic 验证，进入了业务逻辑层
```

### 对照：Claude Code 为什么没问题？

Claude Code 通过 `ANTHROPIC_BASE_URL` 使用 Forge 的 **Anthropic API** 端点（`/v1/messages`），不走 `/v1/responses`，所以不受此 bug 影响。
