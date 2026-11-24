# 问答响应延迟优化

本次修改针对建档问答和普通聊天的交互等待，不改变会话存储、建档状态机或 SSE 事件结构。部署后，建档客户端不传 `thinking` 或传 `false` 即使用快速模式；普通聊天已恢复默认开启思考，需要快速模式时应显式传 `false`。客户端传 `true` 时不会享受关闭思考带来的加速。

## 已实现

- 建档分析默认关闭思考，聊天回复恢复默认开启思考，显式 `thinking=true/false` 继续有效。建档表达和检索路由继续使用非思考模式，新增 JSON 对象输出约束；业务结构及证据校验保留。
- 建档没有待生成的疑问回应、且报告没有异常提示时，直接跳过检索路由模型。一般轮次只调用患者理解与问答表达两个模型阶段，避免第三次模型调用。需复用的历史回应不重复检索。
- 有新疑问或异常报告提示时仍走知识检索；路由输入只保留本轮回应任务、回答和异常报告资料，不再重复发送整个患者上下文。问答模型原有患者背景和普通聊天历史不裁剪。
- 交互检索路由限制为 2 秒，外部检索每路限制为 3 秒，最多两路并发。超时走现有降级路径；部分检索超时时保留其他通道成功的资料。调用方取消操作继续向上传递。对应常量在 `agents/knowledge/routing.py`，不会修改报告生成模块的检索超时。
- 检索路由的模型网关失败转换为已有领域错误，避免可选路由失败直接中断本轮问答。

## 验证和上线观察

### 思考强度参数

`/api/v1/chat/send/complete`、`/api/v1/chat/send/stream`、`/api/v1/qa/answer/complete` 和 `/api/v1/qa/answer/stream` 均接受可选字段 `reasoningEffort`，取值为 `low`、`high`、`max`，省略时为 `low`。聊天默认 `thinking=true`，建档默认 `thinking=false`；强度不会自动开启思考。非法值（包括 `null`）在调用业务服务或开始 SSE 前返回 HTTP 400。该参数仅作用于当前请求，不修改会话默认值；建档相同答案的幂等重放仍返回已保存结果。

例如聊天请求：

```json
{"userId":"user-1","sessionId":"chat-1","message":"请解释这份报告","reasoningEffort":"high"}
```

建档提交如需启用思考，须同时显式传入 `"thinking": true`；强度只用于分析阶段的首次生成，表达、检索路由和分析修复仍关闭思考。

Python 业务接口 `sendMessage`、`sendMessageStream`、`submitAnswer`、`submitAnswerStream` 均接受 `reasoning_effort='low'` 参数。LLM 接口支持 Profile 默认值和单次请求覆盖：

```python
profile = ModelProfile(
    name='example', context_policy=ContextPolicy.STATELESS,
    thinking=True, reasoning_effort='low',
)
request = LLMRequest(profile=profile, content='用户问题', reasoning_effort='max')
result = await gateway.complete(request)  # gateway.stream(request) 同样支持
```

优先级为 `LLMRequest.reasoning_effort` → 兼容已有 `ModelProfile.model_kwargs['reasoning_effort']` → `ModelProfile.reasoning_effort`（默认 `low`）。不要放入 `extra_body`，避免覆盖请求级选择。关闭思考时不向 AION 发送强度参数。

已安装的 AION DeepSeek 客户端明确支持该参数。其他客户端未声明支持时，显式选择强度会抛出 `LLMCapabilityNotSupportedError`；未指定强度的其他厂商 LLM 调用保留原行为，不将 `**kwargs` 当作能力支持。下方历史时延数据来自当时版本，不代表新增强度参数后的实测结果。

无网络回归覆盖普通建档两阶段调用、默认与显式思考、问题校验、检索路由跳过与保留、检索超时、并发部分成功、任务取消、API 参数转发和 SSE 契约。2026-09-19 在 Python 3.12.14 下运行全量测试，2395 项通过；本次修改的 Python 文件通过 Ruff 检查和格式验证。执行 `python -m pytest`（Windows 受限环境可通过 `--basetemp` 指定仓库外的可写临时目录）；这些测试使用模拟模型与存储，不代表生产时延测量。

测试环境保持当前供应商和模型配置。测试快速模式时，建档可省略 `thinking` 或传 `false`，聊天需显式传 `false`，再分别测试普通聊天、普通建档、医学知识检索和多轮长会话。统计从发出 HTTP 请求到收到并渲染首个非空 `content`（聊天）或 `question` / 完成结果（建档）的 P50/P95，并单独记录错误和检索降级率。`start`、心跳和 `reasoning` 不作为正文首字，不能用于宣称达到 3–5 秒目标。代理需保留现有 `X-Accel-Buffering: no` 行为，不聚合 SSE 数据。

本次没有把建档结果改成未校验的逐 token 输出；建档依然需要完整结构化生成与校验。知识检索最坏情况下仍可能等待约 2+3 秒，再进入正文生成。因此，这次移除确定的额外等待，但不承诺每轮正文都在 5 秒内到达。若测试仍不能达标，应依据阶段耗时再决定模型路由、上下文预算或增量展示的下一步改造。

回滚时撤销本次代码提交即可恢复默认思考与原检索路径；无需数据库迁移。暂时需要原思考行为的调用方可显式传 `thinking=true`，该参数不恢复原来的检索等待策略。

## 本地真实模型实测（2026-09-19）

针对 `162c2d3`，使用当前 HTTP Handler、业务服务和 Agent 图，真实调用 `deepseek-flash` 与 EviMed，内存保存合成会话；不含部署数据库、画像查询和前端公网链路。两个有效批次共 22 个请求，均成功，单并发，不能据此承诺生产 P95。首字只统计正文，排除 `start`、`reasoning` 和空白片段。

| 场景 | 样本数 | 正文首字范围 | 5秒内 |
|---|---:|---:|---:|
| 普通聊天 | 12 | 1.12–2.10 秒 | 12/12 |
| 真实医学检索聊天 | 2 | 2.07–2.97 秒 | 2/2 |
| 建档创建首题 | 2 | 2.76–3.00 秒 | 2/2 |
| 建档连续回答 | 6 | 3.62–7.98 秒 | 3/6 |

建档尚未全面达到5秒内输出：第一批两轮进入分析校验修复；第二批没有重试，但最慢一轮分析生成耗时6.19秒，表达生成耗时1.77秒。普通建档确实未执行检索路由，剩余问题主要在结构化分析生成与修复。需要进一步缩短分析输出、降低校验修复频率；不能把内部分析首 token 或 SSE `start` 当作患者正文首字。

本地复现：`python -m tests.api.scripts.local_qa_latency --output <结果路径>`，通过标准输入提供 `provider`、`model`、`api_key`、可选的 `knowledge_api_key` JSON 字段；不把密钥写进脚本或提交。部署后复现：`python -m tests.api.scripts.qa_latency --base-url <测试服地址> --output <结果路径> --revision <确认的代码版本>`。工具仅允许已标明 dev/test 的服务器，使用随机 `test-latency-` 账号；会调用真实模型和保存测试会话，不生成报告。
