# 共享 LLM 网关的联网搜索参数

更新：2026-11-07，开发分支 `feature/record-questionnaire`。Shulan 固定依赖 AION `v0.6.0`，共享网关适配阿里云和 DeepSeek 的公开搜索参数。Chat、Report 等既有业务默认关闭；问卷因素链仅在自有知识库没有适用材料时发起联网检索。

## 1. 调用与职责

调用链保持为：

```text
业务应用层 → AgentHub → 已注册 Graph → LLMGateway → AionLLMGateway → AION → 部署配置的模型提供商
```

- 业务在自己的 Graph 中决定本次是否开启搜索，并构造 `LLMRequest`。
- 网关不识别 Report 等业务名称，不增加业务专用客户端或密钥。
- AION 负责厂商参数、协议、搜索及必要的网页获取、模型上下文和搜索追踪。
- 单次部署仍使用一个提供商。开启搜索不会切换提供商或模型。
- 本参数控制模型的联网搜索，不控制现有 KnowledgeGateway/EviMed 知识检索；后者继续由各 Graph 的检索流程决定。

## 2. 参数契约

| 位置 | 参数 | 默认值 | 含义 |
|---|---|---|---|
| `ModelProfile` | `web_search: bool` | `False` | 业务模型配置的搜索默认值 |
| `LLMRequest` | `web_search: bool \| None` | `None` | 本次调用覆盖值；`None` 继承 Profile |
| `LLMRequest` | `effective_web_search` | 只读属性 | 计算后的实际开关 |
| `LLMRequest` | `search_query: str \| None` | `None` | 与模型正文分离的检索词；提供时必须是非空字符串，且实际搜索开关为 `True` |

优先级为：**本次请求显式值 → Profile 值 → 默认 `False`**。

请求传 `False` 可以覆盖 Profile 的 `True`。只接受布尔值，不接受字符串 `"true"`/`"false"` 或整数 `0`/`1`。完整调用 `complete()` 与流式调用 `stream()` 使用相同规则。

开关的执行语义取决于 AION 提供商适配。DeepSeek 在 AION `v0.6.0` 中收到 `search=True` 后，会先调用 Search、按需 Fetch，再调用模型；这不只是允许模型自主决定是否搜索。阿里云仍由 AION 映射到厂商 `enable_search`。开关本身不证明已经取得可用材料，须检查实际搜索追踪与来源。

`search_query` 不复用完整模型提示词。共享请求只接受非空字符串或 `None`，DeepSeek 进一步要求检索词长度为 1～128 个字符；超限明确失败，不将长提示词直接提交为 Search 查询。问卷因素适配器将有界检索词与模型 `content` 分别传入，仍以原模型任务正文组织回答。

`False` 禁止本次调用发起模型联网搜索，不删除此前成功回答中的搜索信息；有状态会话仍按原规则恢复历史。

## 3. 业务接入示例

以下代码放在已有 Graph 的模型调用节点内，`gateway` 继续由运行时注入：

```python
from agents.llm import ContextPolicy
from agents.llm import LLMRequest
from agents.llm import ModelProfile

profile = ModelProfile(
    name='example.answer',
    context_policy=ContextPolicy.STATELESS,
    # web_search 默认 False
)

request = LLMRequest(
    profile=profile,
    content='需要回答的问题',
    web_search=True,  # 由业务策略控制本次调用
    search_query='Python asyncio 文档',  # 独立、有界的实际检索词
)
result = await gateway.complete(request)

# 流式调用使用同一个请求参数。
async for chunk in gateway.stream(request):
    ...
```

某项能力需要默认开启搜索时，可以在其 `ModelProfile` 中设置 `web_search=True`；单次请求依然可以显式关闭。Chat、Report 等已有业务 Profile 保持默认关闭。问卷因素链在知识库没有适用材料时，才显式开启当前检索调用，不扩大其他业务的联网范围。

如果业务希望从自己的 Agent 输入接收这个值，需要在对应的 Graph State 声明字段，并在构造 `LLMRequest` 时传入 `web_search=state.get('web_search')`。`AgentHub` 不会自动修改业务 Graph 的输入协议。隔离验证图 `agents/graphs/validation/graph.py` 提供了完整与流式的接入示例。

本次不向任何业务 HTTP 接口自动增加前端参数；前端是否可控制，由该业务自己的契约和参数校验决定。

不得在 `model_kwargs` 或 `extra_body` 中另设 `search`、`search_query`、`enable_search`、`web_search`，也不得通过原始 `tools` 注入 `web_search*` 工具，以免绕过统一校验或覆盖本次明确关闭的设置。这类配置会返回参数错误。其他模型参数继续按原有方式透传。

## 4. 当前 AION 支持范围

Shulan 的 `requirements.txt` 与 `pyproject.toml` 统一固定 `aion[docmind] @ git+https://gitee.com/loongspears/aion@v0.6.0`，保留 DocMind SDK。显式配置的 `SHULAN_LLM_DEFAULT_MODEL` 仍优先；未显式指定模型时，DeepSeek 由 AION 使用默认值 `deepseek-flash`。Shulan 将 OpenAI SDK 限定在 2.x，以保留基于 httpx 的传输重试计数。当前 Shulan 调用的公开方法支持如下：

| AION 客户端 | 关闭搜索 | 开启搜索 |
|---|---|---|
| `aliyun` / `qwen` | 向其公开 `search` 参数传 `False` | 传 `search=True`，AION 转换为厂商 `enable_search`；实际模型也必须支持此能力 |
| `deepseek` | `search=False`，保持普通模型调用 | `search=True` 与独立 `search_query` 交给 SDK，先搜索及必要获取，再生成回答 |

网关检查当前调用方法是否**显式声明可用关键字传入的 `search` 参数**；传入独立检索词时还检查 `search_query`。只有 `**kwargs` 不足以证明客户端支持对应能力。无法确认支持且本次开启时，抛出 `LLMCapabilityNotSupportedError`，不静默降级为普通模型回答。

网关只映射 AION 的公开参数，不直接生成厂商请求，不使用 Report 名称判断能力。搜索参数关闭且客户端没有该参数时，不向旧客户端额外传参，兼容现有普通调用。

## 5. 搜索来源与追踪

完整结果和流式 Chunk 分别保留模型正文、推理、用量与搜索来源。AION `v0.6.0` 的结构化材料来自 `search_trace.sources`，网关支持 SDK 的 dataclass 返回值，将来源 `excerpt` 映射为内部 `search_sources[].text`，并保留来源 `excerpt_origin`。完整 `search_trace` 保留 `query`、原始来源字段 `sources` 和操作字段 `operations`；流末尾仅含追踪或来源、没有正文的 Chunk 也必须传出。

当前 DeepSeek SDK 最多返回 5 条来源，每条摘录最多 1200 个字符。这些是本次检索实际取得的有界摘录，不能称为完整网页或文献全文。`excerpt_origin` 区分 Search 结果与 Fetch 获取等材料来源，不代表医学质量分级；成功检索不等于材料适用、引文支持结论或个人病因成立。

问卷因素链把实际摘录转换成独立来源和 passage，在 `passages[].locator.excerpt_origin` 中保留摘录来源，再执行适用性与逐字引用校验。适配器返回 `searchTrace`，流水线将完整追踪及触发原因保存到内部 `factorPreparation.searchTraces`；对外 assessment 结构保持不变。模型生成正文中的链接、引文或自报来源不能补充成检索材料。来源缺失、内容不足、检索失败或不能形成有效支持时，保留证据缺口，不用模型常识填补医学依据。

厂商协议继续封装在 AION；Shulan 的 Report、Graph 和问卷适配器均不直接调用 DeepSeek HTTP/SDK。本次依赖升级及适配不新增环境变量，不改变既有提供商、密钥、超时和模型配置的归属。

## 6. 兼容性与验证

- 新字段放在现有 dataclass 字段之后，保持旧位置参数的含义。
- Chat、Report 等普通业务继续关闭搜索；Provider 选择、思考开关、Prompt 和上下文保存规则保持不变。搜索来源和追踪是共享网关结果的增量字段，不自动扩展业务 HTTP 或 SSE 契约。
- 有状态调用仍只保存成功完成的轮次；临时 `supplemental_context` 不进入持久化的用户消息。失败及未完整消费的流不保存部分轮次。
- 离线验证覆盖搜索开关、独立检索词、SDK dataclass 来源、完整与流式尾追踪、默认关闭、上下文连续性、失败行为及问卷适配；使用真实客户端配合替代传输时，不访问真实服务。
- 本地离线测试不证明真实模型返回了适用于医学分析的来源。真实验收应分别记录搜索执行和来源结构、知识库优先及联网回退、引用适用性；单次普通文档搜索成功也不能代替完整问卷病例验收。

```powershell
conda run -n shulan python -m pytest -q tests/unit/agents/llm tests/unit/architecture
```
