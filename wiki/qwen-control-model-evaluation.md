# Qwen3.8-Flash 主控替换核查与实测

核查日期：2026-09-19。业务代码基线：`fix/qa-response-latency` / `a2c79ec`。本次仅扩展手动测试工具和离线契约测试，没有修改生产模型路由、业务流程、提示词或数据库。

## 结论与完成状态

本次真实对照不支持为了提速而将 `record.plan` 换成 Qwen3.8-Flash：6 次回答后下一题全部超过 5 秒，中位数 8.74 秒；同批 DeepSeek 中位数 4.78 秒，3/6 在 5 秒内。千问这组没有业务校验修复，DeepSeek 有 1 次，但样本不足以证明千问在完整业务上更稳定。模拟响应的耗时不计入性能结果，官方“响应速度”描述也不作为本项目的测速结论。

优先比较“只换主控”：A 组全部保留 DeepSeek；B 组只有 `record.plan` 使用 Qwen3.8-Flash，`record.question`、`chat.reply`、`knowledge.route` 保留 DeepSeek。先保留原提示词与输出规则，再单独测试 Qwen JSON Object / JSON Schema 优化，避免把模型替换和其他改动的收益混在一起。生产当前使用全局供应商配置，直接修改三项 `SHULAN_LLM_*` 会影响其他角色；本次按角色路由仅存在于测试工具。

已使用用户提供的北京业务空间执行同批 A/B、B/A，共 16 个端到端请求，全部成功。双方均关闭思考，候选额外显式设置 `preserve_thinking=false`；提示词、校验、表达模型保持相同。实际接口路径使用所提供 `/api/v1` 同域名下的 `/compatible-mode/v1`。

| 同批对照指标 | DeepSeek 主控 | Qwen3.8-Flash 主控 |
| --- | --- | --- |
| 首题，2 次 | 2.93–3.34 秒 | 4.06–4.61 秒 |
| 回答后下一题，6 次 | 3.91–7.98 秒 | 5.51–11.93 秒 |
| 下一题中位数 | 4.78 秒 | 8.74 秒 |
| 下一题 5 秒内 | 3/6 | 0/6 |
| 分析校验修复次数 | 1 | 0 |
| 分析内部首内容，包含修复调用 | 0.44–1.05 秒 | 0.52–1.39 秒 |

Qwen 6 次分析全部命中 2,048 输入 tokens 缓存，但完整分析仍需 4.41–10.30 秒，不能用缓存命中或内部首字快代替端到端达标。样本中位数约增加 83%，仅描述这批结果，不是跨时间、地域和负载的模型速度排名。原始文件：`outputs/qa_optimization_v1/qwen-ab/summary.json` 和同目录四份分组明细。

另外直接执行了 9 次千问接口探测：JSON Object、严格 JSON Schema、函数调用、工具结果回传、3 次隐式缓存流式和 2 次显式缓存流式均成功。第一次极短 JSON 调用耗时 31.74 秒，其后短调用约 0.52–0.65 秒；这次慢请求独立保留在接口探测结果中，不混入建档对照，也不删除。尚未分解连接与服务端阶段，不能认定为固定的冷启动耗时。

2,233 输入 tokens 的固定合成前缀中，隐式缓存实测从 0 变为 2,048 cached tokens，首字从 0.744 秒变为 0.618/0.575 秒；显式缓存先创建 2,216 tokens，下一次命中 2,216，首字分别为 0.569/0.508 秒。这是很短输出的接口实验，不代表业务建档可在半秒结束。原始文件：`outputs/qa_optimization_v1/qwen-live-contract.json`。

本端点实际工具调用响应出现 `finish_reason="stop"` 且 `tool_calls` 非空，并成功完成 tool 消息往返。将来接原生工具调用时，应以实际工具字段判定并处理，不能只等 `finish_reason="tool_calls"`。

复杂多困扰轨迹进一步表明“短测试没有修复”不能外推为更稳定：两组都通过了该场景的 10 项自动流程检查，并正确结束；DeepSeek 5 轮实质回答、2 次分析修复、总计 35.31 秒，Qwen 8 轮实质回答、5 次分析修复、总计 157.78 秒。轨迹和轮数不同，整段耗时不能用来直接计算模型速度倍率。Qwen 修复涉及证据原文为空、旧困扰依据遗漏或空数组；DeepSeek 修复主要涉及信息缺口的分类枚举。可针对这些已观察到的错误约束输出结构，但不能删除证据校验来获得更好时延。

| 复杂场景，每组各 1 条轨迹 | DeepSeek 主控 | Qwen 主控 |
| --- | --- | --- |
| 两项困扰同等关注 | 5 轮后完成；2 次修复；35.31 秒 | 8 轮后完成；5 次修复；157.78 秒 |
| 预设中途新增反酸 | 4 轮即收尾；1 次修复；21.91 秒；新增困扰未触发 | 8 轮后完成；1 次修复；116.65 秒；保留腹胀并接纳新增反酸 |
| 第 20 轮同时处理旧、新疑问 | 13.77 秒后失败；两次分析均未通过校验 | 修复 1 次后完成，46.28 秒；12 项自动检查通过 |

第二个场景会在第 5 次回答加入新困扰；DeepSeek 提前结束，因此并未测试到它如何处理这个新增困扰，不能把自动检查通过解释为该分支已覆盖。第 20 轮场景使用相同人工种子状态，DeepSeek 首次输出含替代字符，修复后又出现非法分类，抛出 `RecordGenerationError`；Qwen 首次编号类型错误，修复后完成所有旧、新疑问回应并保存最终状态。这些异常均保留在结果中，不能宣称现有 DeepSeek 已完全解决稳定性，也不能从一次成功收尾断言 Qwen 更可靠。

完整流程使用真实应用服务、图和模型，内存存储，未启用知识检索；普通提交路径的耗时不等于 SSE 首字时间。自动检查覆盖状态、困扰保留、不重复目的、幂等和收尾；已人工核对新增反酸、两项困扰和最终疑问的轨迹，未进行临床专家质量评分。汇总位于 `outputs/qa_optimization_v1/qwen-flow-summary.json`，完整轨迹位于本机 Codex 可视化目录下的 `qwen-flow-comparison`。

最后对两份完全相同的患者输入重放规划调用，避免两模型自行提出不同问题的影响。每份输入在三个变体中 SHA-256 相同，模型提示词相同；Qwen JSON 组只额外增加 `response_format=json_object`。

| 固定输入，仅规划完整输出耗时 | DeepSeek | Qwen | Qwen + JSON Object |
| --- | --- | --- | --- |
| 首次提供两项困扰 | 3.41 秒 | 15.70 秒 | 15.76 秒 |
| 含历史的后续患者状态 | 5.15 秒 | 11.51 秒 | 10.37 秒 |

六次均生成语法有效 JSON；这里只检查语法，没有重跑对应状态的业务校验。Qwen 内部首内容仍约 0.60–1.01 秒；只是最终 JSON 完整输出慢。第二份输入的 Qwen 普通组甚至命中 4,096/4,455 输入 tokens，仍需 11.51 秒。本轮单独开启 JSON Object 未解决速度问题；完整业务 JSON Schema 尚未做性能比较，不能承诺其收益。原始数据：`outputs/qa_optimization_v1/qwen-fixed-plan-replay.json`。

此前当前分支的真实 DeepSeek 小样本结果如下，不能当作与千问同一时段、同一负载的正式 A/B 数据：

| 场景 | 样本数 | 用户可见正文/问题时间 | 5 秒内 |
| --- | --- | --- | --- |
| 普通聊天 | 12 | 1.12–2.10 秒 | 12/12 |
| 带真实证据检索的聊天 | 2 | 2.07–2.97 秒 | 2/2 |
| 建档首题 | 2 | 2.76–3.00 秒 | 2/2 |
| 建档回答后下一题 | 6 | 3.62–7.98 秒 | 3/6 |

慢轮包括一次分析耗时 6.19 秒、问题生成 1.77 秒，以及两次分析校验后的修复调用。分析模型内部很早出 token，不等于用户已经看到下一题。原始数据和限制见本机 `outputs/qa_optimization_v1/新版问答真实API时延实测报告.md`。

## 官方接口核查

### 模型与地域

准确模型 ID 为 `qwen3.8-flash`，支持文本输出、Function Calling、结构化输出和上下文缓存。官方列出的上下文窗口为 1,000,000 tokens；最大输入 991,808，思考模式最大输入 983,616，最大输出 131,072。这些是容量上限，不是低时延推荐值，输入与输出也不能分别取满后简单相加。北京、新加坡支持内置搜索，其他列出地域的能力不同。来源：[模型详情](https://help.aliyun.com/zh/model-studio/qwen3-8-flash)。

协议仍采用 Chat Completions：`POST {base_url}/chat/completions`、Bearer API Key、`model`、`messages`。百炼文档推荐业务空间域名，例如 `https://{WorkspaceId}.cn-beijing.maas.aliyuncs.com/compatible-mode/v1`；既有 DashScope 域名仍可用。地域、业务空间和密钥需要对应。当前 AION Aliyun 适配器默认使用北京旧域名，本次测试工具允许显式覆盖端点。来源：[Chat API](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)。

### 思考与生成参数

| 项目 | 核查结果与本项目建议 |
| --- | --- |
| `enable_thinking` | 千问 3.8 Flash 默认开启；快速问答明确设为 `false`。当前分支默认 `thinking=False` 经 AION 能正确转换。 |
| `preserve_thinking` | 默认开启；开启时需按规范回传历史 `reasoning_content`。当前快速链路建议显式关闭，避免与现有上下文表示不一致。 |
| `reasoning_effort` | 支持 `low`、`medium`、`xhigh`；`none` 对应关闭思考。快速路径不额外设置，避免与 `enable_thinking` 产生混淆。 |
| `thinking_budget` | 与 `reasoning_effort` 不能同时设置。若后续评估思考组，只选一种控制方式，并单独统计耗时与通过率。 |
| `max_tokens` | 约束回答长度；不能靠低上限强行截断完整建档 JSON。 |
| `max_completion_tokens` | 包含思考与回答的整体输出上限；不要与其他上限随意叠加。 |
| 采样参数 | 首轮对照保留供应商默认值并记录；后续低温、`presence_penalty=0` 作为独立实验，避免过度改写固定枚举。低温不是正确性保证。 |

思考默认值与历史回传规则来源：[深度思考](https://help.aliyun.com/zh/model-studio/deep-thinking)。参数位置及互斥规则来源：[Chat API](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)。SDK 中非标准参数放入 `extra_body`，SDK 发出的 HTTP JSON 会将它们合并到顶层；直接 HTTP 调用不套 `extra_body`。

### 结构化输出与返回值

Qwen3.8-Flash 支持 JSON Object 和 JSON Schema。`json_object` 要求提示词包含 JSON 关键词，只保证 JSON 语法；`json_schema` 可约束结构和类型。二者都不能保证患者事实、证据归属、否认语义或困扰索引在业务上正确，现有业务校验必须保留。来源：[结构化输出](https://help.aliyun.com/zh/model-studio/qwen-structured-output)。

当前 `record.plan` 通过提示词要求 JSON，未设 `response_format`；`record.question` 和 `knowledge.route` 已设置 `json_object`。因此可将“Qwen 主控 + JSON Object”作为 C 组，随后评估完整 JSON Schema。不能直接把演示 schema 当成覆盖所有建档分支的生产 schema。

非流式正文在 `choices[].message.content`；流式在 `choices[].delta.content`，思考另读 `reasoning_content`。统计用户首字时排除角色空包、思考包、空白和应用的 start 事件。`stream_options.include_usage=true` 可获取用量末包，末包可能没有 choice。还需区分 `finish_reason=stop/length/tool_calls`；当前 AION/Shulan 标准返回类型没有保留 `finish_reason`，这是后续处理截断/原生工具调用前应补的小接口缺口。本次契约测试已验证空 choice 用量末包和缓存明细可以通过现有代码。来源：[Chat API](https://help.aliyun.com/zh/model-studio/qwen-api-via-openai-chat-completions)。

### 工具调用

模型支持不等于应用已经支持。标准闭环是传入 `tools`，读取 `tool_calls`，解析并校验参数，执行工具，然后回传 assistant 工具调用消息和带对应 `tool_call_id` 的 tool 消息。并行调用需处理所有工具结果；流式参数要按工具索引拼接完整后再执行。Qwen 文档当前不保证 `tool_choice="required"` 强制调用；非思考模式可指定具体函数。来源：[Function Calling](https://help.aliyun.com/zh/model-studio/qwen-function-calling)。

当前项目检索采用“模型输出路由 JSON → Python 执行 EviMed → 注入证据”，不是原生 Function Calling。AION 输出规范化会丢弃 `tool_calls`，Shulan 的 `LLMResult/LLMChunk` 也没有工具调用字段，不能只增加 `tools` 参数就上线。继续保留现有检索流程最符合本次小改动目标。原生工具调用应独立接入和验收，不与模型时延比较同时改造。

### 缓存与上下文

隐式前缀缓存自动开启，至少 1,024 tokens 才具备命中条件，是否命中不保证。显式缓存通过消息内容块的 `cache_control` 标记启用，最少同为 1,024 tokens，有效期 5 分钟、命中续期；一次最多 4 个标记。两种模式互斥。Chat Completions 缓存与 Responses 的 Session 缓存是不同机制，当前项目不应直接套用 Responses 的会话缓存参数。来源：[上下文缓存](https://help.aliyun.com/zh/model-studio/context-cache)。

优先保持 system 提示词稳定，把动态患者信息放后面，直接观察 `usage.prompt_tokens_details.cached_tokens` 与 TTFT。当前 AION 原始 usage 能保留该字段；不要将“第二次请求”直接标为“命中缓存”。显式缓存需要消息内容块能力，当前 AION 将提示词和消息序列化为纯字符串，不能仅加环境变量实现。缓存减少重复输入计算，不会省去完整规划输出、业务校验或第二次问题生成。

项目有三层不同状态：聊天的 AION_FULL 历史、建档 STATELESS 每次重建的业务患者上下文、供应商前缀缓存。供应商缓存不替代前两者。聊天目前没有明确 token 预算或历史压缩策略；百万上下文不适合作为无限追加的理由。后续应单独评估历史预算和摘要，同时保留关键事实、否认和未解决问题，避免影响建档质量。

源码审查还确认 AION 存储历史思考为元数据，但发出历史消息时只序列化 role/content。关闭思考的快速链路可继续运行；开启思考后若依赖 `preserve_thinking`，必须先补回传能力，不能声称现有多轮思考上下文已完整兼容。

## 本次可直接执行的对照工具

- `tests/api/scripts/local_qa_latency.py`：支持单次测试按 profile 设置供应商、模型、独立密钥、Base URL 和参数。默认行为仍保持之前的测试方案。
- `tests/api/scripts/compare_qa_models.py`：默认跑 2 轮，顺序为 A/B、B/A；每组建档首题加 3 次回答，仅替换候选组的 `record.plan`。
- `tests/unit/server/test_qa_latency_probe.py`：验证实际 SDK 序列化、供应商与密钥隔离、非思考参数、流式空包、缓存用量、失败样本统计；HTTP 响应为模拟数据，不代表供应商实测。

以下 PowerShell 命令从已配置的环境变量取密钥，通过 stdin 传递，不写入源码或结果。`DASHSCOPE_BASE_URL` 必须对应实际百炼地域；不要将文档解析 AccessKey 当成模型 API Key。

```powershell
$required = @('SHULAN_LLM_API_KEY', 'DASHSCOPE_API_KEY', 'DASHSCOPE_BASE_URL')
foreach ($name in $required) {
    if (-not [Environment]::GetEnvironmentVariable($name)) { throw "Missing environment variable: $name" }
}
$settings = @{
    baseline = @{
        provider = 'DeepSeek'
        model = 'deepseek-flash'
        api_key = $env:SHULAN_LLM_API_KEY
    }
    candidate = @{
        provider = 'Aliyun'
        model = 'qwen3.8-flash'
        api_key = $env:DASHSCOPE_API_KEY
        base_url = $env:DASHSCOPE_BASE_URL
        model_kwargs = @{ extra_body = @{ preserve_thinking = $false } }
    }
    knowledge_api_key = $env:SHULAN_KNOWLEDGE_API_KEY
}
$settings | ConvertTo-Json -Depth 8 -Compress | .venv/Scripts/python.exe -X utf8 -m tests.api.scripts.compare_qa_models --output outputs/qa_optimization_v1/qwen-ab --rounds 2
```

输出目录必须为新目录，避免误混历史结果。每组上限 40 个模型客户端、600 秒；模型和检索调用会计费。普通聊天需要另用单次工具取消 `record_only`，因为只换建档主控不会改善 `chat.reply` 的速度。环境变量内的密钥不会自动用于离线单测。

短对照会记录用户可见问题时间、规划/表达耗时、校验修复、成功率、5 秒内成功数、用量和缓存字段。失败样本保留在尝试次数里，延迟中位数明确仅针对成功请求。计时包含本机 HTTP 与真实外部 API；存储仍在内存，未包含部署端 MongoDB/Redis、画像加载、公网到浏览器的链路。

短对照仍是筛选实验：回答根据模型提出的问题生成，因此不同模型的后续路径可能不同；AB/BA 也不能保证缓存冷热一致。本次另外补了上述固定输入重放与复杂轨迹，但每种样本仍少；后续还需扩充否认、矛盾、拒答、真实匿名案例及适量并发测试。重点检查证据不捏造、旧事实不丢失、不重复追问、收尾和重放幂等性。不能用三个正常回答推断医疗质量或生产稳定性。

## 选择标准

优先要求流程正确性不回退，再比较端到端 5 秒内成功率、校验修复率、超时/限流与实际成本。3–5 秒是待验收目标，不是换模型后的保证。建议扩样后以正文/问题 P95 不超过 5 秒作为上线评估指标之一，单独统计冷启动与检索轮次；当前小样本不报告有代表性的 P95。

如果千问只改善模型首 token，但完整规划仍长、校验修复更多，或导致重复追问，不应替换。若主控耗时和修复都改善，可做按角色的小配置改动逐步上线；如果问题生成仍占主要耗时，再单独比较表达模型。JSON Schema、原生工具调用、显式缓存、历史压缩分别作为后续实验，避免一次改动过多导致无法归因。

按本次结果，优先保留当前主控作为速度基线，修复已观察到的分类枚举、依据完整性和编号类型输出问题，并减少每轮重复生成的分析内容；第 20 轮失败应列为部署前回归案例。缓存优化与连接复用可单独测量，但不能把它们当作已解决规划生成瓶颈。此次未自动切换生产模型，也未声称已实现每轮 3–5 秒目标。

## 代码验证

新增与原有时延探针共 6 项离线测试通过；实际 SDK 请求序列化由模拟 HTTP 端验证，不计入上述真实时延。涉及的 4 个 Python 文件通过 Ruff 检查和格式验证。全量测试为 2,400 通过、1 个已有的 Windows 毫秒计时断言失败（`test_metrics_include_tokens_and_actual_recovery_input`，`16 > 16`）；该测试与 6 项探针测试随后单独复测全部通过。本次未改动这一无关计时测试，也未把全量结果记为全部通过。
