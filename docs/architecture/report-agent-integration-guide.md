# 最终报告接入 Agent 中枢、持久化与报告追问指南

> 状态：第 2—6 步及第 14 节持久化受理、恢复与可靠回调已接线；历史离线验证记录保留，真实模型、MongoDB、回调及内容一致性仍待部署验收
>
> 更新日期：2026-10-10；源码复核基线：`develop@c9b8988`，本轮仅文档同步，未重跑业务测试
>
> 范围：五类报告统一 AI 入口、读取画像、独立结果存储，以及普通 Chat 的原会话和跨会话报告追问；不重做已完成的目录迁移

文件专项当前实现见 [上传文件统一解析、内容复用与画像留痕指南](shared-document-ingestion-guide.md)：Analysis、Record、Report 均已接入共享服务，Report 支持 URL/documentId 和专用 report_source 输出。请求复用仍保留同 URL 内容变更限制，不能据共享文件身份推断每次 Report 请求都会重新下载。上传原始文件中的可信观测可受控摄取；AI 生成评估、处方和建议不自动变成健康事实。

## 1. 阅读顺序与实施范围

本文集中说明当前 Report 的模型、画像、存储、查询、Chat 追问与可靠任务链路。早期六步接线已实施，后续维护以现有入口和验收矩阵为准；源码接线、离线覆盖与真实部署验收分别记录。

### 1.1 必读文档

| 阅读顺序 | 文档 | 用途 |
|---|---|---|
| 1 | 本文 | 已确认目标、实施顺序、报告记忆和交付验收 |
| 2 | [架构当前基线](README.md)、[应用分层规则](application-architecture.md)、[API 合规矩阵](api-implementation-compliance.md) | 当前源码与目标的区别，模块、Adapter、Agent 和组合根职责 |
| 3 | [Agent Wiki](../../wiki/agents.md) | AgentHub、LLMGateway、Prompt、临时上下文及测试方法 |
| 4 | [用户画像设计](user-health-profile-design.md) | 健康事实、主体隔离与只读画像；区别于本次独立报告记忆 |
| 5 | [知识库检索设计](agent-knowledge-retrieval.md) | 外部循证边界；不能把报告生成结论当成知识库证据 |
| 6 | [API.md](../api/API.md)、[API 编辑指南](../api/API_EDITING_GUIDE.md) | 当前接口契约，以及新增接口、字段、版本与历史快照规则 |
| 7 | [Report Git 提交流程](../report-git-workflow.md) | 双人修改范围、共享文件协调与人工提交/评审约定 |

旧 [Report 端到端迁移手册](report-api-migration-guide.md) 仅作历史参考，不是实施前置条件。本文第 10 节接口增量以及第 14 节持久化受理、恢复和 callback outbox 已有对应源码；通用 WorkerRegistry 不属于现有依赖，是否对外可用以实际部署版本为准。

### 1.2 已确认的产品边界

```text
本次报告资料 + 用户长期画像
             ↓
       Agent 生成五类报告
             ↓
       独立报告存储
        ↙           ↘
现有业务回调      Agent 只读报告工具
                         ↓
                普通 Chat 解释与追问
```

- 保存 evaluate、intervene、instruction、opinion、explain 全部五类产物；每份保存成功即可咨询，不等待其余报告或医生核验。
- 所有产物标为“AI 生成、未经医生核验”，不能因为类型名称含“诊断意见”“临床医嘱”而冒充医生签发内容。
- 同一用户在原会话和新会话均可追问；独立报告记忆不自动转成 health_profile_facts，也不覆盖真实病史。
- 维护时保持 Report、Agent、Chat 及测试接线一并验收。跨文件变更由共享目录负责人协调，不能遗漏 Chat 联动。
- 报告结果、生成任务与回调任务均持久化，并通过后台 Worker 恢复中断和持续补投；医生审核回传及实时主动推送仍是独立任务。

第 2—6 步已在当前源码中实施。后续不得再按旧基线从零创建第二套 Graph、存储或查询入口；应先以本文验收矩阵检查现有调用链，再以小切片修复缺口。

## 2. 当前代码入口

| 文件 | 当前职责 | 维护边界 |
|---|---|---|
| `web/routes.py`、`web/handlers/report.py` | `report.submit` 路由；生产注入 DurableReportService 和 TaskSupervisor | 先持久化受理再响应；不在 Handler 中创建模型或直读数据库 |
| `bootstrap/factory.py` | 应用级依赖组装、资源登记 | 注入共享 AgentHub、画像读取、报告存储及报告只读工具依赖 |
| `adapters/report/factory.py` | 构造现有 Pipeline、文档/循证/回调/模型适配器，读取指定会话 | 已接收 AI/画像依赖，不读取 LLM 密钥或创建 AION Client |
| `adapters/report/agent_narrative.py` | AgentNarrativeClient 与每报告 NarrativeClientFactory | 已接入 AgentHub，保留 generate、JSON、重试与限流边界 |
| `modules/report/narrative.py` | NarrativeClient 协议、各叙事任务 payload、医学校验与降级 | 保留确定性约束和 `generate(payload)` 调用方式 |
| `modules/report/pipeline.py` | 来源解析、会话补充、评分、检索、生成、回调与后续编排 | AI、画像、共享文件及产物保存均已接线；维护确定性规则和失败隔离 |
| `modules/report/reports.py` | 后续报告编排、确定性拼装及 safe_complete_async | 模型调用经注入的 follow-up Gateway，固定模板位于 prompts/report/followups |
| `adapters/report/followup.py` | 模型、产品目录、HTTP 回调的具体 I/O | 模型已通过注入的 Agent Gateway 调用，产品和回调继续走业务端口 |
| `modules/report/task_coordinator.py` | 请求规范化指纹、执行结果类型及旧注入方式的进程内兼容协调 | 生产请求复用以 MongoDB 受理记录为准，不依赖进程内缓存 |
| `modules/report/durable.py`、`adapters/report/durable_store.py` | 生成租约、恢复、补偿与回调投递 | 使用现有定时任务机制，生成和补投独立调度 |
| `modules/chat/application/service.py`、`web/handlers/chat.py` | Chat 完整/SSE 输入、会话与画像记忆接线 | 已支持可选报告引用与受控报告 Provider，不直接查询 MongoDB |
| `agents/runtime/context.py`、`agents/graphs/chat/graph.py` | 本轮上下文与 Chat 模型请求 | 已有 report_context 临时上下文，与画像、外部循证分区 |

`report_api/` 已删除。当前已存在 `report.narrative`、`report.followup` 和 `report.retrieve_context` 三个无状态能力，固定 Prompt 位于 `prompts/report/`，五类产物通过 Report 自有端口保存和查询。真实调用链仍必须通过测试持续检查，不能只看类名里是否有 Gateway。

## 3. 统一 AI 入口

当前模型调用链：

```text
ReportPipeline / follow-up 编排
  -> 报告领域端口 / Agent 适配器
  -> AgentHub.invoke
  -> 已注册的 Report Graph
  -> LLMGateway.complete
  -> AionLLMGateway
  -> 配置的唯一模型提供商
```

业务适配器只借用 `AgentHub`，不持有整个 AgentRuntime，不从业务层调用 `runtime.llm_gateway`。Graph 内部通过 `LLMRequest` 调用 Gateway。模型密钥、提供商和默认模型仍由 `agents/runtime/application.py` 与运行时创建逻辑管理；不新增 Report 专用的 LLM 密钥或按请求切换提供商。

可复用公共类型：

```python
from agents import AgentHub
from agents import AgentRequest
from agents import AgentRunContext
```

新增稳定 capability 常量时从 `agents` 公共 facade 导出；当前该 facade 还未导出 Report 常量，需随实现添加，不能在文档示例中假定已经可导入。

## 4. 主报告 Graph 与 Prompt

当前主叙事入口如下；同目录还有 followup_graph.py、retrieval_graph.py 和对应 Prompt：

```text
agents/graphs/report/
  __init__.py
  graph.py
  profile.py
  registration.py
prompts/report/
  narrative.txt
adapters/report/
  agent_narrative.py
```

主叙事已使用无状态能力 `report.narrative`，一个 Graph 覆盖现有 NarrativeClient 调用，不为每个内部 task 单独建图。以下示例对应当前 Profile 的关键配置，完整定义见 `agents/graphs/report/profile.py`：

```python
from agents.llm.profiles import ModelProfile
from agents.llm.types import ContextPolicy

profile = ModelProfile(
    name='report.narrative',
    context_policy=ContextPolicy.STATELESS,
    prompt_name='report/narrative.txt',
    thinking=False,
    model_kwargs={'temperature': 0},
)
```

Graph 校验非空对象 payload、序列化 JSON、调用 Gateway，返回 `content/reasoning_content/usage/provider/model/status` 和 request/run 标识。注册函数参考 `agents/graphs/analysis/registration.py`，在 `agents/runtime/application.py` 注册，使用 `checkpointed=False`。

固定系统角色、医学安全边界和输出规则已从原主适配器迁入 `prompts/report/narrative.txt`；动态来源、证据、白名单和任务约束继续由报告业务构建为结构化 payload。Gateway 通过 `ModelProfile.prompt_name` 加载 Prompt，报告业务不另建模型 Client 或重复加载系统 Prompt。

Graph 不负责 HTTP 校验、文件下载、确定性医学规则、报告 Schema 校验、回调或结果持久化。既有 DocMind、EviMedClient、PublicEvidenceClient、评分和证据绑定继续保留；不以 Chat 的通用检索替换报告循证管线。

## 5. 适配器兼容、身份与成本

### 5.1 保持现有协议

`modules/report/narrative.py` 的既有协议必须可继续调用：

```python
async def generate(self, payload: dict[str, Any]) -> dict[str, Any]: ...
```

当前适配器不要求调用方提供 `step_id/attempt_id`；每次 `generate(payload)` 创建不含健康数据的逻辑调用标识，同一次适配器重试保持 request_id 不变，每次实际模型尝试生成独立 run_id。若未来确需稳定业务步骤 ID，必须同步升级协议、所有调用方和测试替身，作为单独兼容变更评审。

当前生产受理与生成状态持久化在 report_batches，finalReportId 同时关联批次、产物和交付记录；这不新增公开 job_id 字段。早期第 2—4 步的执行标识隔离属于迁移背景，不能继续据此断言没有持久任务。中断恢复的边界见第 14 节，不承诺重放中断的模型调用。

### 5.2 保留现有调用行为

| 当前行为 | 维护要求 |
|---|---|
| 单次实际模型调用 timeout=90 秒 | 保留并翻译为 NarrativeError；显式定义限流等待是否计入，不能无意改变原超时范围 |
| temperature=0 | 放入主 Profile 的 model_kwargs；不承诺输出绝对可复现 |
| ReportFallbackPolicy.llm_attempts 默认 2 | 保留适配器尝试上限及退避，不再在 Graph/Gateway 叠加一层默认重试 |
| 共享六路模型并发限制 | 保留等价的跨报告限制；按报告创建适配器不能使并发上限随报告数量倍增 |
| 围栏清理、严格 JSON 对象解析 | 空正文、非法 JSON、非对象输出继续走 NarrativeError 与既有降级 |
| model.* 计数及耗时统计 | 保留或等价映射；日志使用 utils.log，不记录原始 Prompt/结果或密钥 |

业务层的逐项修复和 follow-up 外层重试也应纳入尝试次数清单，避免迁移后重试相乘。平台异常在适配器边界翻译，不让报告业务依赖 AION/提供商异常类。

### 5.3 并发隔离和资源所有权

ReportPipeline 是共享实例，已注入 NarrativeClientFactory；按报告执行创建绑定不可变 user_id、session_id 和执行标识的适配器，并让本次模型辅助步骤使用该对象和固定画像快照。不得修改共享 `pipeline.narrative_client` 或在共享对象上临时覆写用户信息。

Factory 可共享 AgentHub 和有界并发控制器，但不共享跨报告模型历史。AgentRuntime 由应用 Bundle/ResourceRegistry 管理，Report 适配器不关闭借用的 Hub/Gateway。当前 Gateway 按调用创建模型 Client；不要把旧全局 Client 的连接复用描述当成新 Gateway 已提供的能力，连接开销需后续实际测量。

### 5.4 当前原件事实投影的出处对照

默认组合根使用 `project_current_report_source_with_observation_refs`，固定输入适配修订为 `report-understanding-current.v3`，实际原件提取器为 `report-source-v16`。只有固定 Analysis 观察与原件中的结果字段唯一对应时，内部投影行才带有可选的 `analysisObservationRefs`。测量及评估使用固定 `/observations/{index}` 路径；影像所见、诊断分别使用对应的 `/expansion/{field}` 路径，章节路径以固定解释为准。同一原件位置可能对应多个已核实的 Analysis 指针，按固定匹配顺序去重保存。对于同值不同位置且无法唯一定位的结果，保留各原件位置的事实行。

文档身份、原件摘要、`sourceOccurrence` 与这些指针一同保存在固定投影中。Document 核对固定 Analysis 观察和原件位置，Profile 按主体、生命周期和原件位置合并同一观察。历史 v2 投影及已提交贡献按自身固定回执读取。预切换的 v2 清点、排空和切换步骤记录于 I-048。[双来源接线与验收](../../ISSUE.md#i-048-双来源基础信息与共享profile交接)

### 5.5 当前 Document 固定附件消费

当前 Document 附件网关由默认组合根通过 `ReportPipeline` 的工厂依赖接入，按已受理批次和固定的本人主体及生命周期处理。常规 `fileList` 只使用公开当前准备和固定投影，连续图像由原 Report 规则识别。已采用的连续页固定投影作为一个申请发送到 Document，纯临床补充页也保留组引用。主件携带基础信息，次页以经验证的 Report 原件观察参与同一组。不同组合或新轮次的主动更正仍须由业务方明确给定原活动贡献，普通请求不从近似匹配推断替换目标。 显式注入入口由其实际提供方固定投影修订，恢复沿用原批次的工厂清单。

画像应用核对本次任务的身份、Profile 本人绑定及每页固定原件的姓名与性别依据。发现确定冲突时整组暂留报告资料，拒绝提交到该画像。缺失字段维持现有文档归属判断，历史年龄不用于判断是否本人。

Report 批次在填充附件前记录本人主体和生命周期，随后保存每份附件的原准备回执、原件位置与固定快照。重建网关时沿用首次准备引用。通过身份核对的贡献先在批次内保存固定命令，再执行 Document 消费。临时故障按固定命令和独立的提交租约重试。已接收回执与画像已应用分别记录，抵达重试上限仍无法取得回执时记录未知，已明确拒绝的输入记录未提交。临时存储错误导致回执不明时继续生成报告，独立恢复以实际落盘的命令为准。

原有报告生成终态封存和回调投递继续由 Report 工作流处理。独立的提交恢复按已受理的工厂清单领取各自的持久命令，保留原来提交时的主体、生命周期、准备引用和投影，也保留已有报告正文、附件快照和回调记录。Document 接收后由现有发布恢复推进画像。

Evaluate 专用 `originalReportUrl` 在 JSON 优先规则选中后才读取。Report 投影和 Evaluate 模板恢复共用固定原件，分别执行各自所需的原生布局读取并关闭 OCR。体检、检验和影像的部分结果沿用既有原生专项表格恢复。专用 PDF 的共享 OCR 或解释暂时不可用时，可按当次准备的固定归属、原件摘要和原生模板为该报告恢复内容，并记录为原件降级和未提交。该降级无准备回执，只为当次报告提供内容。同批次正常恢复仍按已保存的准备回执。当前真实端口的合成组合验证覆盖双来源原件去重、不同位置同值保留和独立撤回。真实模型、MongoDB、回调与部署另行验收。[双来源接线与验收](../../ISSUE.md#i-048-双来源基础信息与共享profile交接)

## 6. 后续四份报告

当前四种输出是 `intervene`、`instruction`、`opinion`、`explain`，均属于有效业务。固定模板已位于 `prompts/report/followups/`；其产品导向 Prompt 与主 narrative 的禁止产品推荐 Prompt 保持隔离，维护时区分有效任务与未使用文本。

当前 `safe_complete_async` 默认单次 1800 秒、最多三次尝试、共享四路并发；外层还有 ReportFallbackPolicy 控制的生成重试、固定空报告降级和回调重试。`FollowupReportGateway.complete_text` 另有 300 秒默认值，不能把它当成全部生产调用的现状。维护时用测试固定实际调用路径、次数、依赖和输出，不顺带统一成主报告的 90 秒或 NarrativeError 规则。

已确认使用独立的无状态 `report.followup` capability，以服务器端 task_type 允许列表区分现有内部模型任务与固定模板，不按每个输出类型重复建设完整工作流。task_type 不是对外请求字段；每个有效模型调用都必须对应受控模板，禁止传任意系统 Prompt、模型名或工具。主叙事和 follow-up 的 Profile/Prompt 保持隔离，不要求先实现历史手册中的 child job 框架。

产品目录与 HTTP 回调不是 LLM 调用，继续通过既有端口处理。迁移期间保留分模块生成降级、独立交付状态及完成结果重投，不能重新把任一模块失败变为四份报告全失败。

## 7. 只读长期画像接入

### 7.1 迁移前基线与已实施能力

迁移前 Report 只通过 `load_report_session` 查询指定会话，这不是长期画像读取。当前已由 bootstrap 注入 Report 专用只读画像 Provider；AgentHub 仍不会根据 userId 自动查库。

可参考现有 `modules/health_profile/application` 公共入口、HealthProfileService 的主体/事实读取方法以及 `health_profile/memory.py` 的只读 Provider。Chat 的 `build_context(user_id)` 默认最多 24 条事实、6000 字符，包含 active/pending/disputed 与血亲家族史，并只保留部分指标的最新观测；它是对话提示，不是完整报告数据集，不能直接当作报告的全部权威事实。

### 7.2 已实施的接入边界

当前已在 Report 自有端口中定义只读画像契约，由 `adapters/report/profile_snapshot.py` 适配画像模块并在 bootstrap 注入。输出为结构化 `ReportProfileSnapshot`，不抓取前端投影全文或解析 Chat 提示词。

快照至少需要事实 ID、主体及关系、factType、值和单位、有效/观察时间、确认状态、来源引用及读取时间/可取得的画像版本。按当前用户本人选择数据，血亲仅作为家族史；不能把朋友或家属疾病作为本人疾病。明确截断和选择边界，不能声称一个有预算的快照包含全部病史。

报告开始时读取一次，供该次评估、叙事和后续四份报告复用，重试不重新读取随时变化的画像。这里的“固定快照”先指本次执行内不变的数据；跨进程恢复所需的持久快照另行评审。若读取过程中版本发生变化，需检测后重取或明确标记，不能仅凭读取起始时间宣称数据库事务快照。

画像不能只拼进模型 Prompt 而绕过现有白名单、评分和证据约束。先在业务层将允许的事实映射进相应的 typed 输入或明确分区的 payload，再由原有校验流程处理；未建立映射的资料不能被模型自由扩写成正式结论。

当前画像接入遵守以下边界，后续修改时保留对应契约测试：

- 当前请求及明确报告来源继续保持已有优先级；历史画像补充缺失背景。不同日期的观察不简单互相覆盖，矛盾资料保留出处并按规则处理，不按来源标签静默选边。
- active 也需保留断言和验证等级，不等于医生确诊；pending/disputed 默认不作为确定事实或评分依据，若保留为辅助线索必须与正式依据分区。
- 画像为空时使用当前报告和会话资料继续；读取失败记录脱敏 warning 并按无画像降级，不额外等待画像提取 LLM 完成。
- QA/Chat 摄取异步运行，新报告不能保证读到尚未落库的事实；本次请求和指定 QA 资料仍是即时来源。
- 不读取其他会话的全部原文，不导入 Chat 的 AION Context，不把 custom 卡片、模型 reasoning 或检索正文作为健康事实。
- 报告结论不自动写回画像。后续若需要回写，必须通过独立来源、审核和摄取边界，避免模型生成内容自我强化。

### 7.3 与 STATELESS 的关系

Report 仍使用 `ContextPolicy.STATELESS` 与 `checkpointed=False`。无状态表示不复用模型多轮历史，不禁止显式携带长期画像。

现有 `AgentRunContext.health_profile_context` 和 `LLMRequest.supplemental_context` 可承载受控临时文本，但仅填写 context 不会自动传给模型；Graph 必须显式使用。首版推荐将业务已校验的报告画像快照映射到 payload，避免同时在 payload 和 supplemental_context 重复发送同一份健康数据。若选择临时文本通道，必须证明它不绕过事实校验、不进入 checkpoint 或模型历史。

## 8. 独立最终报告存储（已实施，待真实环境验收）

### 8.1 所有权与最小数据边界

MongoDB 已有独立最终报告批次和报告产物存储，与 Analysis 上传体检报告、健康事实和 Agent checkpoint 分开。领域类型、读写端口及查询门面属于 Report，MongoDB 实现属于 adapters，连接由 `bootstrap/` 组装，资源生命周期由 `runtime/resources.py` 的 ResourceRegistry 管理。Chat 及 Agent 节点不得绕过端口直接操作集合。

每次实际生成分配唯一 `finalReportId`，五类报告共享该批次标识，以 reportType 区分各自产物。finalReportId 与上传体检报告的 reportId 不是同一引用。记录用户、本人主体、来源会话、生成时间、报告类型、生成状态、审核状态、最终正文及来源引用；正文采用最终对外报告结构，不保存模型 reasoning、内部提示词或密钥。

批次身份必须在受理响应前确定，正在运行的相同规范化请求和短期完成结果重投复用原 finalReportId；真正重新生成分配新批次，保留旧报告。不能为重复 HTTP 请求先生成新 ID，再在后台悄悄合并到另一批次。

五类产物分别提交，不等待五份全部成功后才整体保存。每个产物的最终正文不可被同批次重试覆盖成不同内容；相同内容的重复提交保持幂等，不同内容的重放产生明确冲突。生成结果与回调交付状态分开，不能用回调成功与否决定报告是否可查询。

### 8.2 保存、发布与失败

```text
单类报告生成、校验并固定对外结构
  -> 幂等保存产物
  -> 成为可查询、可供 Chat 引用的报告
  -> 发送该产物的业务回调
```

- 每类产物保存成功即可查询，未完成类型保留各自状态；失败或固定空降级产物不得被当作有实质结论的完整报告。
- 保存失败执行有上限重试，只重试存储，不重跑解析或 LLM；仍失败时不发送对应成功报告回调、不宣称归档完成，并用统一 logger 记录可定位的错误。其他产物独立处理。
- 回调失败不删除已保存报告，不阻断其被 Chat 查询；回调重投引用原产物，不制造新版本或重复推理。
- 存储成功后即使服务重启，已完成产物仍可读取。新批次记录同时保存生成任务：未开始的任务继续调度，运行中任务租约过期后为缺失类型保存中断失败结果，不重新调用模型。

新增配置已在 `config/env.py` 和 `.env.example*` 模板登记；维护配置时同步这些入口，不读取或修改 `.env` 本身。默认仅保存上线后的新报告，历史回调不自动补录；长期保留、不设置自动过期，后续删除与保留治理单独设计。

## 9. Agent 报告记忆与 Chat 追问（已实施，待真实环境验收）

### 9.1 受控报告读取

Report 提供模块所有的只读查询门面，Agent 通过受控适配器调用。无状态 `report.retrieve_context` 能力使用 `checkpointed=False`；只接收当前用户已授权的有限报告目录元数据，并选择报告批次与产物类型。它不读取报告正文、不开放任意 MongoDB 查询，也不要求 AION 原生工具循环或向量库。

目录与读取工具属于私人报告资源访问，不能把报告正文发送到外部循证检索接口。多候选时使用一次受控模型路由选择报告与产物类型；该路由通过 LLMGateway，业务层再验证输出 ID 和类型是否存在于原始授权候选中。当前不由选择 Graph 做章节级路由，正文仍由现有 12,000 字符预算渲染器处理。

### 9.2 报告选择规则

1. 显式指定 finalReportId 时按其读取，reportType 若提供必须与该批次一起使用；不存在、不属于当前用户或不可用时明确失败，不静默切换报告。
2. 没有显式 ID、但用户明确询问“最新报告”时，按生成时间选择最新批次，并说明各类产物可用状态，不把未完成类型伪装成已有结果。
3. 其他情况下依次使用：当前对话已明确引用的报告 → 当前会话关联报告 → 用户唯一可用报告。
4. 某一级仍存在多份无法区分的候选时，提供必要的类型、日期等选择信息并追问，不混合不同批次，也不猜测用户想读哪份。

自动读取同样支持同 userId 的新 sessionId；不能仅查新会话而遗漏用户过去已保存的最终报告。用于继续追问的会话引用只保存必要的 finalReportId/reportType，不复制完整报告正文。候选目录有预算限制时须保留“并非完整目录”的信息，不能把截断后的一条候选误判为用户唯一报告。

### 9.3 临时上下文与回答

- 每轮读取固定的报告内容快照，补充预算默认最多 12,000 字符；画像、报告和外部循证分别标注，互不混为权威事实。
- 报告通过独立的临时运行上下文交给 chat.reply；不能复用 health_profile_context 字段混入报告，不能将全文放进 AION 历史、Chat checkpoint 或业务消息。正常用户提问、助手回答和必要引用仍按既有规则保存；回答中的引用摘要仍会进入后续对话历史。
- 报告内容是外部数据而非系统指令。回答注明报告类型、时间及“AI 生成、未经医生核验”，不把报告建议改述为确诊、医嘱已签发或用户已执行。
- 只解释实际读到的产物内容；预算截断、来源不足、查询异常或报告尚未生成时明确说明，不能从模型历史或想象中补造本次报告内容。
- 完整回复和 SSE 共用读取逻辑；无报告时普通聊天继续。当前 AI 正在输出时不插入新报告改变本轮输入，后续一轮再读取新结果；保留已有断连后完整生成和落库行为。

报告分析不自动写入 health_profile_facts；画像事实、AI 报告分析与外部知识证据分别归因。custom 卡片仍不直接进入模型上下文，医生核验和修订版本回传不在本次实现范围。

## 10. 接口增量与兼容要求（源码已实施，待部署验收）

### 10.1 已确认的接口增量

| 接口或字段 | 当前行为 |
|---|---|
| `POST /api/v1/report/list` | 按用户查询报告批次，支持会话过滤、分页及各类产物状态 |
| `POST /api/v1/report/get` | 按 userId + finalReportId + reportType 读取指定产物 |
| Chat complete/stream 请求可选 finalReportId、reportType | 指定本轮报告；reportType 必须与 finalReportId 一起使用，不传新字段时沿用自动选择和旧调用方式 |
| Report 受理响应和五类回调增加 finalReportId | 关联生成批次、展示、查询、重投及后续咨询 |

所有读取校验当前 userId 与报告归属，错误引用不能回退到其他报告。生产查询必须由可信调用方或网关控制；仅传 userId 并不构成身份认证。本次不新增用户登录系统。

此表的源码和 API.md 契约已实现；对外可用性仍以实际部署版本及真实 MongoDB/回调验收为准。API 文档只写调用方所需契约，不写集合、Graph、Prompt 或内部调度。

### 10.2 保持的行为与明确变更

1. HTTP 受理在持久化确认后快速返回 processing，完整工作由 runtime 调度的生成 Worker 执行。
2. evaluate 解析和生成硬上限为 30 分钟；四类后续报告不与它共享一个总时限。不能沿用旧文档的 15 分钟总时限。
3. success/partial_success 结果继续生成后续四份报告，evaluate 回调失败不阻断；failed 结果不进入该生成分支。
4. 原第 2—6 步的模型、画像、存储、查询与报告引用均已接线；后续维护保持现行 API 字段及降级规则。
5. 相同规范化请求运行中及完成后 15 分钟内复用持久化批次；重复请求不重跑模型，也不重新开启已确认回调。
6. 五类结果保存于独立 MongoDB 产物存储，生成失败也可查询并交付；回调前必须保存，Agent checkpoint 不作为报告仓库。
7. 正常停服停止领取新任务并等待在途任务；强退后的未开始任务恢复调度，运行中的中断任务按第 14 节形成失败终态。

## 11. 分步实施与人工确认

以下是原六步实施里程碑及当前验收边界；不再按旧顺序从头实施或在文档步骤暂停。后续修复仍由维护者测试、审核并手动提交：

| 步骤 | 交付内容 | 当前状态与验收 |
|---|---|---|
| 1. 交接文档 | 本计划并入主指南；同步架构导航、TODO、ISSUE；纠正不保存报告的旧范围 | 文档已建立，本次同步当前源码状态；本次文档改动未提交 |
| 2. 主报告 AI | 固定旧行为测试；report.narrative/Profile/Prompt、兼容 generate(payload) 的适配器及每报告 Factory | 源码已实施，有单元与架构测试；真实报告内容对比待验收 |
| 3. 后续四类 AI | report.followup 受控任务、固定模板、共享并发、重试和失败隔离 | 源码已实施，统一 AI 入口门禁已覆盖；真实四类回调待验收 |
| 4. 读取长期画像 | 固定画像快照、主体/状态/来源/时间约束及失败降级 | 源码已实施，有画像契约测试；真实画像数据待验收 |
| 5. 存储与查询 | 五类产物保存、稳定 finalReportId、列表/详情、先存储再回调 | 源码已实施，有幂等与失败测试；真实 MongoDB/回调待验收 |
| 6. Chat 追问 | report.retrieve_context、独立临时上下文、可选引用及完整/SSE 共用选择规则 | 源码已实施，有候选授权、歧义澄清和上下文隔离测试；真实原/新会话待验收 |

原实施记录中，第 2—6 步曾分别记录上述离线测试或门禁通过；该历史结论保留，不代表本轮重新执行，也不替代真实环境验收。

## 12. 测试与验收场景

测试至少覆盖：多用户并发不串身份；相同请求不重复推理；主 JSON/timeout/NarrativeError 兼容；follow-up 分模块失败及回调重投；画像为空/失败/超预算；本人和家族史隔离；pending/disputed 不冒充确认事实；同一执行画像固定；不同执行可读取更新；原文和画像不写入模型历史/checkpoint；无画像也不丢失已有结构化报告内容。

现有测试入口：

```bash
conda activate shulan
python -m pytest -q tests/unit/report tests/unit/server/test_report_architecture.py
python -m pytest -q tests/unit/agents tests/unit/health_profile tests/unit/bootstrap tests/unit/architecture
```

新增 Graph 测试放在 `tests/unit/agents/graphs/report/`，报告适配器、存储和 Pipeline 测试放在 `tests/unit/report/`；Chat 及 Web/组合根在对应已有目录补齐契约测试。真实 API 联调扩展 `tests/api/pages/report.html` 和 Chat 页面及对应脚本。无网络测试使用替身，不读取 `.env` 或真实服务凭据；Python 开发与测试均在 shulan Conda 环境运行。

本次新增闭环必须验证：

- 五类报告分别落库；部分失败、固定空降级、尚未生成和可用正文状态不混淆。
- 某一产物已完成、其他产物仍在生成时，原会话可立即追问；同用户新会话仍可读取该报告。
- 分别验证服务重启后已保存结果可查询、未开始任务恢复调度、生成租约过期后补齐失败结果；后两者不以重新调用模型恢复运行中的任务。
- 保存失败不发送对应成功回调、不重复调用 LLM；回调失败仍可查询已保存结果。
- 同一批次重放幂等、不同正文冲突不覆盖；请求复用和回调重投不生成新版本；实际重生成保留历史批次。
- 多用户与多主体隔离；显式旧报告、最新报告、会话关联和多候选澄清均选择正确，候选目录截断不导致误选。
- 报告 Prompt 注入、越权/伪造 ID、超预算、检索失败均受控；全文不进入 AION 历史或 Chat checkpoint。
- 报告建议不自动进入正式健康事实，回答明确未经医生核验，医生 custom 卡片继续保持上下文隔离。
- 完整/SSE 行为一致；旧客户端不传新增字段可继续使用；原有 SSE 断连完整落库规则不回退。
- 测试页能够实际操作“生成报告 → 查询产物 → 选择报告 → Chat 追问 → 同用户新会话再问”。

统一 AI 静态门禁已扫描 adapters、agents、modules、兼容 services 和 server.py；有效模型 SDK 实现仅允许平台 AionLLMGateway。Report 业务不得创建 AION/OpenAI Client 或读取 LLM 密钥，不能通过搬动 Client 绕开测试。源码扫描存在不表示本轮重新执行或全部门禁通过。

## 13. 交付完成定义与后续边界

- [x] 主叙事及四类后续报告所有模型调用经 AgentHub/Graph/LLMGateway。
- [x] 固定 Prompt 集中管理，主叙事和产品导向任务边界分开。
- [x] 只读画像参与报告输入，状态、主体、来源、时间和预算规则有测试。
- [x] 现有 JSON、超时、重试、并发、降级、请求复用和回调语义有自动化回归保护。
- [x] 五类报告独立持久化，finalReportId 稳定，先保存再发布成功回调。
- [x] report/list、report/get、Chat 可选引用及五类回调引用已在源码和 API 文档中实现。
- [x] report.retrieve_context 已接入 Chat complete/stream 共用路径，错误或歧义引用不静默替换。
- [x] 报告独立于健康事实保存，保留 AI 未核验属性，不自动回写画像或进入 Chat checkpoint。
- [x] 资源所有权、共享对象生命周期及脱敏日志边界有架构测试保护。
- [x] 保留既有离线单元、架构及 API 契约测试与历史验证记录；本轮仅文档复核，不重跑或扩大其验收结论。
- [ ] 真实 LLM、MongoDB、业务回调、三组报告内容一致性及原/新会话追问人工验收完成。

只有“读取画像生成报告 → 五类产物保存 → Agent 读取报告 → Chat 原会话和新会话追问”全部通过才算本次交付完成，不能仅以模型入口迁移或落库成功代替用户体验验收。

默认只处理上线后的新报告，历史回调数据不自动补录；结果长期保留、无自动过期。持久受理、回调 Worker 和生成中断恢复见第 14 节。医生审核/修订回传、实时推送、删除与保留治理、通用 WorkerRegistry 及其他分层治理仍分别跟踪，不因本次完成而关闭。

## 14. 持久化受理、生成中断恢复与长期回调

配置切换、批量迁移、只读预览及单批次补投的完整命令见 [最终报告回调运维指南](../../wiki/report-callback-operations.md)。本节说明持久化机制与配置定义。

生产组合根注入 `DurableReportService`：通过校验的请求先以 majority+journal 写确认保存至 `report_batches`，再返回 `processing`；无法确认写入时返回 503。批次保存规范化请求、固定回调目标、活跃请求指纹和生成状态。同一指纹运行中或完成后 900 秒内复用批次；过期只释放活跃指纹，历史文档不删除。

生成任务以 MongoDB 单文档条件更新领取，租约 120 秒、30 秒续租。待开始任务在重启后继续运行；过期的运行任务进入恢复流程，保留已有产物，为缺失类型保存确定性的失败结构。恢复和补投均不重新运行解析或模型。已经产生终态的 `(finalReportId, reportType)` 不被迟到结果覆盖。

`report_artifacts` 仍是唯一正文来源，`report_callback_deliveries` 保存不可变内容摘要、回调目标、尝试次数、下次时间和领取令牌。产物保存后幂等入队；批次 `reconcileNeeded` 只有在五类产物及五类交付任务均齐全时清除。入队、批次完成或运维修改中途退出都由重入补偿处理，不依赖跨集合事务。

后台投递与生成分离，默认每 5 秒扫描到期任务，入队也可唤醒扫描；并发 4，单次请求总超时 15 秒。所有未收到 2xx 的结果继续退避补投：默认 30 秒、120 秒、600 秒、3600 秒，此后按小时持续尝试，并加入 0.9—1.0 倍错峰；合法 Retry-After 可以延后。没有最大尝试次数、死信终止或 TTL。租约过期可重新领取，只有当前令牌可以确认投递，停机排空停止新领取并等待在途操作。

回调调度配置统一在 `config/env.py` 读取，`.env.example*` 提供完整默认值。以下配置写入部署 `.env` 或进程环境变量，重启后生效；系统环境变量优先，未设置或留空使用默认值，非法值在启动时统一报错。

| 配置项 | 默认值 | 含义与范围 |
| --- | --- | --- |
| `SHULAN_REPORT_CALLBACK_URL` | 部署配置 | 后续报告和肠道屏障报告的公共接收地址。 |
| `SHULAN_REPORT_EVALUATE_CALLBACK_URL` | 从公共地址确定 | 仅指定 `evaluate` 的接收地址。留空且公共路径为 `/api/basic-health-assessment/ai-full-back/callback` 时，使用同域的 `/api/basic-health-assessment/ai-full-back-2026-11/callback`。自定义公共路径按原地址接收。新批次在受理时保存各自目标，重试使用批次快照。 |
| `SHULAN_REPORT_CALLBACK_RETRY_DELAYS_SECONDS` | `30,120,600,3600` | 第 1、2、3、4 次失败后的基础等待秒数；超过序列后始终重复最后一项。逗号分隔、非递减、正整数，每项最多 31536000 秒；单值如 `600` 表示固定间隔 |
| `SHULAN_REPORT_CALLBACK_POLL_INTERVAL_SECONDS` | `5` | 到期任务扫描周期，1—3600 秒；首次入队和服务启动也会触发扫描 |
| `SHULAN_REPORT_CALLBACK_CONCURRENCY` | `4` | 每个进程的回调并发数及 HTTP 连接池上限，1—64 |
| `SHULAN_REPORT_CALLBACK_TIMEOUT_SECONDS` | `15` | 单次 HTTP 请求总超时，1—60 秒；同时传入 Worker 和 HTTP 客户端，为固定 120 秒领取租约留出余量 |
| `SHULAN_REPORT_CALLBACK_BATCH_SIZE` | `100` | 每轮扫描最多领取的任务数，1—10000；不是每项任务的重试次数上限 |
| `SHULAN_REPORT_CALLBACK_RETRY_JITTER_RATIO` | `0.1` | 随机缩短基础间隔的最大比例，`0 <= 比例 < 1`；设置 `0` 关闭错峰 |
| `SHULAN_REPORT_CALLBACK_BACKLOG_LOG_INTERVAL_SECONDS` | `300` | 每个回调 Worker 实例的积压汇总日志最小输出间隔，1—86400 秒；只在扫描时检查，与重试间隔无关 |

回调日志通过 `callback_scope=report` 和 `callback_scope=gut_barrier` 区分 Report 与 Gut Barrier 队列。两个业务独立统计积压并分别限流，因此同一服务进程每 300 秒通常会各输出一条汇总日志；多个进程各自计时，并非整个部署共享一个输出间隔。

重试时间从本次投递失败时开始计算，基础间隔乘以 `[1 - 错峰比例, 1]` 的随机系数，再取该时间和合法 `Retry-After` 要求的较晚者。默认实际等待范围为 27—30 秒、108—120 秒、540—600 秒、3240—3600 秒；扫描周期、并发占用和积压可能进一步延后实际发送。`Retry-After` 可以超过配置序列最后一项，不会让发送提前。

重启不会重算已保存的 `nextRetryAt`，也不会重置尝试次数或重新投递已确认任务；新失败按当前配置和累计尝试次数计算下次时间。如需让指定批次立即重新进入投递调度，使用下述 `scripts.report_callbacks retry` 命令。生成与补偿恢复扫描仍每 5 秒运行，回调扫描配置仅作用于投递 Worker。

回调采用至少一次交付：相同正文及 `Idempotency-Key=<finalReportId>:<type>` 可能重复出现。HTTP 2xx 表示接收方已经接收；接收端须自行保证幂等。`status/error` 加入后续四类正文，所有查询继续保留 AI 未核验属性；投递状态位于独立 `delivery` 元数据中，不进入医学正文。

`scripts.report_callbacks` 提供按用户和批次查询、补投和修正未确认目标的命令。操作意图及操作人、原因先保存，目标版本只前进，旧在途请求的确认不能覆盖新目标。历史批次没有 `durableVersion`，不自动生成任务；显式补投只读取既有正文，要求提供目标地址，不伪造缺失的历史报告。

只切换评估接收端时设置 `SHULAN_REPORT_EVALUATE_CALLBACK_URL`，公共接收端由 `SHULAN_REPORT_CALLBACK_URL` 配置。重启相关服务实例后，新批次采用当前配置。对于已经启用回调、尚有未确认结果或有类型未入队的 Report 批次，`python -m scripts.report_callbacks migrate-pending --dry-run` 可预览范围，去掉 `--dry-run` 则按类型更新目标。该命令扫描普通 Report 队列，`--reason` 可选。已确认结果不重发，相同目标不重复重置，历史批次需已启用投递才纳入迁移。单批 `retry` 默认沿用各自地址，显式 `--target-url` 会把该批所有未确认类型统一改投该地址。

迁移按命令开始时间和 `_id` 上界限定扫描范围，分页读取批次及投递元数据，避免一次加载全部历史或报告正文。每个匹配批次复用持久化 `redrivePending`、递增目标版本和令牌撤销机制。正在生成的报告类型从更新后的批次快照取对应地址。批次审计用统一 `operationId` 关联。命令只使变更目标的未确认任务立即到期，Worker 继续使用持久化正文和原幂等标识投递。

批次级失败不阻断其余批次，输出匹配、迁移和失败计数，错误只含批次 ID 与异常类型；部分失败以非零状态退出。没有全库事务：中断前已保存的批次意图可由补偿修复，尚未处理的批次通过重跑命令继续迁移。扫描与投递并行，因此预览数量不是锁定快照，已在迁移更新前确认成功的任务也会保持成功。新受理批次依赖重启后的配置，不依赖迁移命令持续监听。

集合均长期保存，新增配置为 `SHULAN_MONGODB_COLL_REPORT_CALLBACK_DELIVERIES`，默认 `report_callback_deliveries`。上线需要创建新增索引的数据库权限，并同步接收方新增字段与幂等处理。离线集合模型不能替代真实 MongoDB 多进程、主节点切换和强退恢复验证；本机模拟 HTTP 接收端可以独立验证投递失败、恢复和确认状态。
