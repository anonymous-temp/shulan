# aion Agent 对话协议设计（历史参考）

状态：已归档，不是当前 Shulan Agent Core 的实施前置条件

设计日期：2026-08-07

状态复核：2026-09-03，Shulan 基线 `6625647`。本文仅保留早期协议设想，不审定当前安装的 AION 版本，不授权修改 AION；现用调用契约以 `agents/llm/aion_gateway.py` 和适配测试为准，见 [架构文档状态](README.md)。

历史安装基线：`aion 0.2.2`，commit `8020b72cf4fd27ed6a0607c24f4a445cda367c8a`

2026-08-11 复审时的历史 Shulan 基线：`aion 0.3.2.dev17+g4c4a64f46`，commit `4c4a64f46d3f8053d06db28addf10bd65dc4974d`；不是当前环境版本声明

> 2026-08-07 实施决策：Shulan 第一版 LangGraph POC 不以本协议为前置条件，继续使用当前 `aion` 字符串接口，并暂不实现工具循环。只有实际业务场景证明当前接口不足时，才按具体缺口恢复本协议中的相应增量。

> 2026-08-11 复审：AION 已提供 Client 级自动上下文和 Context v1 导入导出。Shulan 当前通过统一 LLM Gateway 接入 AION，普通聊天 POC 不依赖本文设想的多消息、工具调用或流式工具增量类型。本文第 1—11 节只记录当时对未来工具协议的设想，其中的版本建议、改动范围和“接入前验收门槛”均不再作为当前实施要求。若未来出现真实工具调用需求，应以当时 AION 公共 API 为基线逐项重新评审，不直接照搬本文类型设计。

## 1. 目标

本协议为 `aion` 增加 LangGraph/Agent 所需的最小通用能力：

- 原生多消息输入；
- 完整响应中的工具调用；
- 流式响应中的工具调用增量；
- 完成原因、响应 ID、实际模型和 usage 元数据；
- 保持现有字符串输入、属性访问和字典下标访问兼容。

本轮不要求 `aion` 依赖 LangChain 或 LangGraph。`aion` 只负责统一模型提供商协议，Shulan 使用自定义 LangGraph 模型节点接入。

## 2. 兼容性原则

1. 现有调用保持不变：

   ```python
   result = client.complete("你好", thinking=True)
   async for chunk in client.stream_async("你好"):
       print(chunk.content or "")
   ```

2. `ChatResult.content`、`ChatResult.reasoning_content`、`ChatResult.usage`、`result[key]` 和 `str(result)` 的语义不变。
3. `content` 参数扩展为 `ChatInput`，不新增一套平行的 Agent 调用方法。
4. 新字段只做增量添加；未返回的字段使用 `None` 或空列表，不抛出 `AttributeError`。
5. 同步和异步、完整和流式接口必须使用同一套序列化与响应解析函数。

当前实现基于已经标记为 `v0.3.0` 的源码，建议将包含本协议的版本发布为 `aion 0.3.1`。

## 3. 输入协议

### 3.1 公共类型

建议在 `aion.base.models` 定义以下公共模型。以下代码表达接口语义，具体实现可以继续使用 dataclass。

```python
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping, Sequence, TypeAlias

Role: TypeAlias = Literal["system", "user", "assistant", "tool"]
MessageContent: TypeAlias = str | list[dict[str, Any]] | None


@dataclass(slots=True)
class FunctionCall:
    name: str
    arguments: str


@dataclass(slots=True)
class ToolCall:
    id: str
    function: FunctionCall
    type: Literal["function"] = "function"


@dataclass(slots=True)
class ChatMessage:
    role: Role
    content: MessageContent = None
    name: str | None = None
    tool_call_id: str | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)


ChatMessageLike: TypeAlias = ChatMessage | Mapping[str, Any]
ChatInput: TypeAlias = str | Sequence[ChatMessageLike]
```

第一版必须支持文本和工具调用。`MessageContent` 预留内容块，是为了以后支持图像或文件输入；本 POC 不要求实现多模态能力归一化。

### 3.2 `complete/stream` 签名

`BaseClient` 及提供商 Client 的 `content` 类型改为 `ChatInput`，方法名和参数位置保持不变：

```python
def complete(self, content: ChatInput, model: str | None = None, **kwargs) -> ChatResult: ...

async def complete_async(
    self,
    content: ChatInput,
    model: str | None = None,
    **kwargs,
) -> ChatResult: ...

def stream(
    self,
    content: ChatInput,
    model: str | None = None,
    **kwargs,
) -> Iterator[ChatStreamChunk]: ...

async def stream_async(
    self,
    content: ChatInput,
    model: str | None = None,
    **kwargs,
) -> AsyncIterator[ChatStreamChunk]: ...
```

### 3.3 消息构建规则

`_build_messages()` 必须遵循确定性规则：

- 输入是 `str`：保持旧行为，返回 `DEFAULT_MESSAGES + user message`。
- 输入是消息序列：认为调用方传入了完整上下文，不隐式添加 `DEFAULT_MESSAGES`。
- 输入序列不能为空。
- 不修改调用方传入的 list、dict 或 dataclass。
- `ChatMessage` 转换为 OpenAI-compatible dict；Mapping 至少执行浅拷贝。
- `assistant` 消息允许 `content=None` 且包含 `tool_calls`。
- `tool` 消息必须包含 `tool_call_id`。
- 无效 role 或缺失必要字段应在网络请求前抛出 `ValueError`。

消息序列不隐式添加默认 system prompt，是为了让 Agent 的 checkpoint、实际模型输入和审计记录完全一致。需要默认 system prompt 时，由 Agent 显式把它放入消息序列。

示例：

```python
messages = [
    ChatMessage(role="system", content="你是健康咨询助手"),
    ChatMessage(role="user", content="我最近睡不好"),
    ChatMessage(role="assistant", content="这种情况持续多久了？"),
    ChatMessage(role="user", content="大约两周"),
]
result = await client.complete_async(messages, thinking=True)
```

## 4. 完整响应协议

扩展 `ChatResult`：

```python
@dataclass
class ChatResult:
    content: str | None = None
    reasoning_content: str | None = None
    usage: dict[str, Any] | None = None
    tool_calls: list[ToolCall] = field(default_factory=list)
    finish_reason: str | None = None
    response_id: str | None = None
    model: str | None = None
    created: int | None = None
```

解析规则：

- `content` 来自 `choices[0].message.content`。
- `reasoning_content` 保持当前兼容解析。
- `tool_calls` 来自 `choices[0].message.tool_calls`，函数参数保持为原始 JSON 字符串，不在 `aion` 内自动解析。
- `finish_reason` 来自 `choices[0].finish_reason`。
- `response_id`、`model`、`created` 来自响应顶层。
- `usage` 继续转换为普通 dict。
- 响应没有 choices 时，消息和完成原因字段为空，但仍保留顶层元数据与 usage。

函数参数不自动 `json.loads()`，因为不完整 JSON、重复键、Schema 校验和修复策略应由工具执行层处理，不能在模型接入层静默改变。

工具调用示例：

```python
result = await client.complete_async(
    messages,
    tools=[{
        "type": "function",
        "function": {
            "name": "query_health_metric",
            "description": "查询用户已经授权的健康指标",
            "parameters": {
                "type": "object",
                "properties": {"metric": {"type": "string"}},
                "required": ["metric"],
            },
        },
    }],
)

for call in result.tool_calls:
    print(call.id, call.function.name, call.function.arguments)
```

## 5. 流式响应协议

流式工具调用的函数名和参数可能分散在多个 chunk 中，必须保留增量位置和原始片段。

```python
@dataclass(slots=True)
class FunctionCallDelta:
    name: str | None = None
    arguments: str | None = None


@dataclass(slots=True)
class ToolCallDelta:
    index: int
    id: str | None = None
    function: FunctionCallDelta | None = None
    type: Literal["function"] | None = None


@dataclass
class ChatStreamChunk:
    # 两个字段在流式场景中均表示增量，保留现有属性名以兼容调用方。
    content: str | None = None
    reasoning_content: str | None = None
    usage: dict[str, Any] | None = None
    tool_call_deltas: list[ToolCallDelta] = field(default_factory=list)
    finish_reason: str | None = None
    response_id: str | None = None
    model: str | None = None
    created: int | None = None
```

流式规则：

- 每个上游 chunk 对应一个 `ChatStreamChunk`，不丢弃只包含 usage 或 finish reason 的 chunk。
- `delta.tool_calls[*].index` 必须保留；`id`、`name` 可能只在首个相关 chunk 中出现。
- `function.arguments` 是字符串增量，消费者按同一 `index` 和到达顺序拼接。
- `finish_reason="tool_calls"` 必须被保留，Agent 据此进入工具节点。
- `response_id/model/created` 在上游提供时保留。
- 第一版不要求 `aion` 在生成器内部聚合完整工具调用，避免隐藏状态；可后续增加独立的纯函数 accumulator。

Shulan 的适配器将按 `index` 聚合：

```text
index -> id + type + concatenated(name fragments) + concatenated(argument fragments)
```

聚合结束后必须再校验 `id`、函数名和参数 Schema，不能直接执行模型返回的调用。

## 6. 工具消息回传

模型返回工具调用后，下一次请求的消息序列必须能完整表达：

```python
messages.extend([
    ChatMessage(
        role="assistant",
        content=None,
        tool_calls=result.tool_calls,
    ),
    ChatMessage(
        role="tool",
        tool_call_id=result.tool_calls[0].id,
        content='{"value": 72, "unit": "bpm"}',
    ),
])
result = await client.complete_async(messages, tools=tools)
```

这条往返链路是 Agent 协议验收的核心，不只是验证请求能携带 `tools`。

## 7. 提供商能力与工厂

能力描述和提供商工厂不是工具循环的阻塞项，可以在核心协议之后实现。

建议能力模型采用三态值，`None` 表示当前版本无法确定，避免把未知能力误报为不支持：

```python
@dataclass(frozen=True, slots=True)
class ChatCapabilities:
    tools: bool | None = None
    json_schema: bool | None = None
    reasoning: bool | None = None
    stream_usage: bool | None = None
```

能力与具体模型有关，推荐接口为：

```python
client.capabilities(model: str | None = None) -> ChatCapabilities
```

统一工厂建议放在 `aion.chat`：

```python
client = create_client(
    provider="deepseek",  # 或 qwen
    api_key="...",
    model="...",
    system_content=None,
)
```

Shulan 每次运行只创建当前配置提供商的客户端，不做单请求动态路由。

## 8. aion 建议改动范围

```text
aion/base/models.py
  增加消息、工具调用、工具增量和流式 chunk 模型

aion/base/chat.py
  扩展 _build_messages
  扩展完整响应解析
  扩展流式响应解析
  为公开方法补充类型标注

aion/deepseek/chat.py
aion/aliyun/chat.py
  接受 ChatInput，继续只处理厂商特有参数

tests/base/test_messages.py
tests/base/test_completion.py
tests/base/test_stream.py
tests/providers/test_deepseek_params.py
tests/providers/test_qwen_params.py
  增加无网络协议测试
```

模型序列化和响应解析应集中在 `base`，不能分别复制到 DeepSeek 和 Qwen 实现。

## 9. 必须通过的无网络测试

### 输入

- 字符串输入仍生成默认 system + user 消息。
- 原生消息序列不被包装进单条 `user.content`。
- 原生消息序列不隐式加入 `DEFAULT_MESSAGES`。
- 输入 list/dict 在调用后不发生变化。
- assistant tool call 与 tool result 可正确序列化。
- 空消息序列、非法 role、缺少 `tool_call_id` 在请求前失败。

### 完整响应

- 普通正文、reasoning 和 usage 保持旧行为。
- 多个工具调用的 id、type、name、arguments 完整保留。
- `finish_reason="tool_calls"`、响应 ID 和实际模型完整保留。
- 无 choices 的 usage-only 响应不会报错。
- 属性访问、字典下标访问和 `str(result)` 向后兼容。

### 流式响应

- content/reasoning 增量保持旧行为。
- 两个并行工具调用可按不同 index 区分。
- 跨多个 chunk 的 name/arguments 片段不丢失、不重排。
- 只有 finish reason 或 usage 的末尾 chunk 仍会 yield。
- 同步和异步解析结果一致。

### 提供商参数

- DeepSeek 的 `thinking/reasoning_effort/extra_body` 行为不回归。
- Qwen 的 `enable_thinking/thinking_budget/enable_search` 行为不回归。
- `tools`、`tool_choice`、`parallel_tool_calls` 和 `response_format` 继续透传。

## 10. 验收门槛

满足以下条件后，Shulan 才开始接入真实 `aion` Agent 节点：

1. 上述无网络测试全部通过。
2. 使用模拟 OpenAI-compatible 响应完成一次“模型请求工具—工具结果回传—模型最终回答”往返。
3. DeepSeek 或 Qwen 至少一个真实测试模型完成同步、异步、流式工具调用 smoke test。
4. 发布可固定 commit 或版本的新 `aion` 包。
5. Shulan 更新依赖后，离线探针确认消息没有嵌套、工具字段没有丢失。

另一个提供商可以在 POC 阶段只完成兼容性报告，但其不支持项必须通过 `capabilities()` 明确暴露，不能静默降级。

## 11. Shulan 集成边界

`aion` 升级前，Shulan 不在业务代码中解析 OpenAI SDK 私有对象，也不临时调用 `client.async_client` 绕过 `aion`。升级后由 `agents/llm/aion_adapter.py` 负责：

- LangGraph 消息与 `aion.ChatMessage` 的领域转换；
- 聚合 `ChatStreamChunk.tool_call_deltas`；
- 把 content/reasoning 转成内部流事件；
- 校验提供商能力并产生明确配置错误。

工具注册、权限、参数 Schema、超时、审计和最大循环次数属于 Shulan Agent 层，不进入 `aion`。
