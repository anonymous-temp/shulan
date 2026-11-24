# Agent 版本端口与受控工具运行

> 状态：I-036 平台实现。固定工作流端口和工具运行器可供业务域显式注入；现有生产 Graph 已按域分批注册。五个业务主 Agent 已封装各域完整既有流程，Report/GutBarrier 默认入口已接通；完整工具研究循环仅在 Report 显式启用，且默认关闭。外部模型、MongoDB 部署和真实业务效果需另行验收。

## 1. 公开领域端口

`agents.AgentPort` 是 `invoke` / `stream` 协议，不要求实现使用 LangGraph。两种现成实现有不同用途：

| 实现 | 用途 | 控制权 |
|---|---|---|
| `WorkflowAgentPort` | 包装一个领域的完整固定业务流程，作为领域主 Agent | 领域 runner 保留受保护规则、任务存储、恢复和回调所有权；端口不新增模型路由或自动重试 |
| `BoundAgentPort` | 调用已注册 Graph 的某个精确版本，适合现有模型子任务 | Graph 决定内部步骤，调用者不能通过端口任意改选 capability |

版本选择是 `AgentSelection(capability, implementation_revision, contract_version='1')`。三项共同标识一次选择；不支持的选择明确抛错，不自动使用 latest。`GraphRegistry.register(..., legacy=True)` 保留原 capability 别名，新增并存版本使用 `legacy=False`，不会替换旧别名。端口注册表 `runtime.ports` 只接受完整选择。

主 Agent 接线示例中的 `pipeline` 是本域原有整条流水线的依赖，不能仅替换 narrative 单步 Graph 的名称：

```python
from agents import AgentRequest
from agents import AgentRunContext
from agents import AgentSelection
from agents import WorkflowAgentPort

selection = AgentSelection('report.workflow', 'report-workflow-v1', '1')

async def runner(request, context, *, thread_id):
    # pipeline 由本域组合根注入；返回公共结果 Mapping。
    # 仍由 pipeline 负责受理、规则、子任务、产物存储和独立回调。
    return await pipeline.execute(request.payload, context=context)

def configure(staged):
    staged.ports.register(selection, WorkflowAgentPort(runner, selection, 'report'))

runtime.install_module('report.workflow', configure)
port = runtime.ports.get(selection)
result = await port.invoke(
    AgentRequest({'taskId': business_task_id}),
    AgentRunContext.create(user_id=user_id, session_id=session_id),
    thread_id=business_task_id,
)
```

普通查询、状态轮询和确定性管理接口继续使用本域服务，不经过主 Agent。应用服务应只在明确的 AI 业务路径调用端口，且检查其依赖的模块 readiness。尚未接入的服务不能因为注册了端口就声称已完成 Agent 化。

新端口会复制输入，线程键覆盖用户、主体、会话、领域、角色、capability、契约版本、实现版本和业务线程标识。跨域只交付公共结果，不传 GraphState。旧 `AgentHub` 不使用新键规则，已有 checkpoint 的线程标识和文本 `complete` / `stream` 行为保持兼容。迁移旧持久任务时须继续走原 Hub/适配器，或显式迁移并保存线程映射；直接换成新端口会形成新的 checkpoint 空间。

## 2. 注册就绪与失败隔离

[生产装配](../agents/runtime/application.py) 把 Analysis、Chat、Health Profile、Report、Record 注册为独立同步批次。`AgentRuntime.install_module` 在私有暂存区编译 Graph、注册 Profile 和公开 Port，全部校验成功后才发布。某域中途失败时：

1. 本批 Graph、Profile、Port 均不可见；已有成功模块保持可用。
2. `runtime.module_statuses[name]` 保存 `ready=False`、异常类型和清理失败类型，不保存可能含敏感数据的异常正文。
3. 只逆序关闭调用方显式提供的本模块 `cleanup`，不关闭共享客户端和 checkpointer。
4. 该域 AI capability 查找明确失败。组合根需把 readiness 和依赖失败映射为现有接口的明确错误；本批未改 HTTP 状态码、路由启停或服务组装策略。

共享运行时基础依赖创建失败仍向上抛错。`required=True` 用于必须阻断启动的批次；生产业务域默认独立失败。`enabled=False` 可在启动阶段跳过一个模块，不能理解为在线卸载或热切换，也未新增单 API 环境开关。注册操作只用于单线程同步启动阶段，资源清理为同步回调；异步业务资源仍交由应用的 ResourceRegistry 管理。

暂存 Profile 可读取已发布的共享 Profile，新增项保持私有；这保证 Chat/Record 能复用既有 `knowledge.route`，同时避免本域失败泄漏半成品。共享检索底座和业务检索策略的完整拆分属于对应后续迁移。

## 3. 工具运行接线

工具模式使用现有 AION 0.6.0 的原生模型—工具—模型循环，没有新增上游依赖。模型 SDK 创建及发送仍集中于 `AionLLMGateway`。`AionToolRunGateway` 在原生循环外增加项目侧权限、预算、持久回执和取消边界；不会替换原文本 Gateway。

```python
from agents.llm.tool_gateway import AionToolRunGateway
from agents.persistence.tool_runs import MongoToolRunRepository
from agents.tools.ledger import ToolRunLedger

# collection 由组合根注入 collection_agent_tool_runs；不能回退到进程内存。
ledger = ToolRunLedger(MongoToolRunRepository(collection))
tool_gateway = AionToolRunGateway(runtime.llm_gateway, ledger, authorize=authorize_resource)
result = await tool_gateway.run(tool_request)
if result.status == 'completed':
    evidence = result.result.content
```

`ToolRunRequest` 包含以下固定输入；调用者必须持久化原始 run ID、预算、版本选择和用于重建请求的输入引用，重试不得临时生成新 deadline 或改选版本：

| 字段 | 约束 |
|---|---|
| `llm_request` | 必须为 stateless，工具模式与 provider 内置 `web_search` 互斥；旧搜索路径继续可用 |
| `context` | 服务端提供 user、subject、root run、run、parent run、domain、role；不能信任模型提供身份 |
| `budget` | 固定模型轮次、工具次数、绝对截止时间及 token/cost 会计预留 |
| `tools` | 固定名字、版本、Schema、描述和处理器；同名只选一个版本 |
| `allowed_tools` | 服务端允许的 `(name, version)` 集合，不能由模型修改 |

工具参数使用 AION 支持的有限 JSON Schema 子集，根对象必须 `additionalProperties=False`；可选参数须给出合法 `default`。处理器接口为 `handler(arguments, ToolCallContext)`，可以是异步函数或同步函数；同步函数在工作线程执行，不能承诺能被强制终止。`authorize` 可进一步检查参数中的资源权限，必须返回 `True`；资源所属模块仍负责最终用户/主体/资源授权。

普通读取可返回 JSON 值或 `ToolOutcome`。有副作用的工具必须声明 `effect='command'`，接收平台生成的稳定幂等键，并返回 `ToolOutcome(value, receipt_ref=owner_receipt)`。幂等键来自服务端身份与 root/run/call ID，处理器必须把它传给能力所有者；本平台不会用“重跑工具”推断副作用是否成功。先执行成功、后回执保存失败的窗口属于 unknown，需要向能力所有者核对。

## 4. 父子预算和取消

根任务及其子任务共用一个 Mongo 文档，依靠 `_id` 唯一性和 `revision` CAS 原子更新。子任务受理前先从父任务预留完整额度，deadline 不得超过父任务；嵌套子任务同样受限。父任务自己的后续轮次只能使用未分配余额。

| 预算项 | 实际保证 |
|---|---|
| `max_model_calls` | 在派发原生模型轮次之前扣减；父子合计不能超限。它不是 SDK HTTP 重试次数或收费次数 |
| `max_tool_calls` | 工具处理器执行前扣减，共享硬上限；权限拒绝也不会返还已登记的调用额度 |
| `deadline_at` | 派发前检查，执行中轮询取消；不能撤回已到达外部服务的操作或强杀同步线程 |
| token/cost reservation | 子任务分配的会计额度；不是供应商实际费用硬上限，也没有声称知道单次请求最大费用 |
| `usage` / `usage_complete` | 汇总所有已观察到的模型轮次及子任务，用量缺失或任务中断时明确不完整；SDK 内部重试费用可能不可见 |

只有完成的子任务会把实际模型/工具次数结算回父任务并释放未使用的预留。失败、取消、unknown、失联均不释放可能已消费的子任务额度。token/cost 预留随完成子任务释放，表示其占用结束，不代表扣除了实际费用；实际 token 使用另行累计，cost 不伪造为零。

`ledger.cancel(context)` 请求取消当前节点及其后代。后代在检查时同时验证祖先状态、lease、取消和 deadline；不能在父任务终止后继续派发。已经派发的调用可保存已知结果回执，但不能发布取消后的成功业务结果。取消范围不会自动覆盖另一个独立业务根任务，Report 独立委派和回调仍遵循业务所有者的取消规则。

## 5. 恢复、回执与流式边界

本实现把一次 AION 原生工具循环视为不可逐步恢复的执行单元。lease 失效不会重新领取执行；恢复查询会把运行态收敛为 `interrupted`，未确认回执标为 `unknown`。相同完整输入重试只返回已保存的结果或中断状态；同一 run ID 改输入、版本、允许列表或预算会冲突。

| 结果 | 下一步 |
|---|---|
| `completed` | 可以读取最终结果；重复查询不再调用模型或工具 |
| `failed` | 本单元终止，没有可发布答案；原 ID 不隐式重跑 |
| `cancelled` | 取消生效且没有未确认执行；原 ID 不重跑 |
| `interrupted` + unknown 回执 | 向工具/业务所有者查询幂等键或 receipt_ref，保留预留额度；本批没有自动对账后续跑或自动返还接口 |
| 存储不可用或仍有有效执行 owner | 明确抛错/冲突，不回退内存执行或重复派发 |

`stream_events` 只输出内部阶段元数据和一个携带状态的终态事件。`kind='completed'` 表示单元终结，业务成功还必须检查 `result.status`。原生 `model_delta`、中间草稿、工具参数和工具正文都不会作为正文 SSE 透出。业务主 Agent 应在最终校验通过后再生成对外 SSE/回调，工具产出的材料不能直接成为用户画像事实。

原生循环不使用 AION_FULL 会话，最终结果与回执写入工具账本；它不伪装成 LangGraph 的逐步 checkpoint，也不改变现有 Graph、Report Worker 或业务回调的恢复语义。派发未知的 HTTP 重试统计返回 `None`，不会把多轮模型调用误报为传输重试。

## 6. 持久资源与验证范围

新集合由 `SHULAN_MONGODB_COLL_AGENT_TOOL_RUNS` 配置，默认 `agent_tool_runs`，通过 `db.mongodb.collection_agent_tool_runs` 暴露。只依赖 Mongo 的 `_id` 索引和单文档 CAS，不引入新中间件、事务或默认 Worker。运行器默认一个根任务最多 64 个节点、每节点 256 条回执、账本 8 MiB、工具返回 256 KiB；超过边界明确终止。

没有自动 TTL。删除账本会失去原 run ID 的重复派发保护，需结合业务任务保留期和所有者回执统一制定清理策略；unknown 不能当作普通缓存清理。账本保存服务端身份、版本/输入指纹、调用参数摘要、回执引用、用量和最终结果，不保存原始工具参数/正文。最终结果可能含业务信息，应继承业务数据的访问与保留策略。

离线验证使用真实 AION 0.6 原生循环与 OpenAI SDK，通过 `httpx.MockTransport` 回放多轮工具事件；Mongo CAS 使用测试集合，不连接真实数据库。覆盖精确版本、旧别名兼容、域注册失败隔离、固定工作流、原生参数校验、权限、父子额度、取消、lease 失效、未知副作用、总用量、重复请求及草稿隔离。完整测试命令和结果随本批交付记录提供；离线通过不代表真实供应商或生产 Mongo 验收。

## 7. 聊天记录查询工具

> 状态：2026-11-10交付共享工具；2026-11-11健康分析通过本域固定资料读取显式消费。工具未注册为全局默认工具，旧业务不自动接入；真实群聊接口与生产环境待验收。

公共查询协议及工具包装位于 `modules/chat_history/`，OpenIM HTTP 适配位于 `adapters/chat_history/`，显式创建工厂位于 `bootstrap/chat_history.py`。这些模块的导入不读取配置、不创建客户端或发起请求；工厂被显式调用时才从 `config.env.CHAT_HISTORY_QUERY_URL` 读取完整接口地址。

### 7.1 配置与构造

四份 `.env.example*` 模板仅增加 `SHULAN_CHAT_HISTORY_QUERY_URL=`，均不提供默认地址。部署配置由维护者自行填写包含 `/api/openim/open-api/message/search` 路径的完整 HTTP/HTTPS URL；工具不按环境推导地址，也不提供鉴权配置。地址为空时创建工具明确报错，不影响未调用该工厂的现有应用启动。

```python
from bootstrap.chat_history import create_application_chat_history_tool

# 显式调用时读取服务端配置；此处只创建工具，不查询聊天记录。
tool = create_application_chat_history_tool()
```

工厂也接受显式 `query_url`，此时不读取配置；`timeout_seconds` 默认 20 秒，`client_factory` 可注入离线 HTTP 替身。普通查询请求按次创建并关闭客户端，不自动重试、不跟随重定向，不附加鉴权或分页参数。

### 7.2 输入与返回

| 项目 | 契约 |
|---|---|
| 工具名称 | `query_chat_history` |
| 工具版本 | `chat-history-query-v1` |
| 执行性质 | `effect='read'` |
| 唯一参数 | `groupId`，必填非空字符串；拒绝额外参数 |
| 上游调用 | 向配置的完整 URL 发送 POST，JSON 正文只有 `groupId` |
| 成功判定 | HTTP 成功，外层 `state == 200`，内层 `results.errCode == 0`，聊天数据结构有效 |
| 工具返回 | 上游 `results.data` 对象，包含 `chatLogs`、`chatLogsNum`，保留其他原始字段 |

参数示例：

```json
{
    "groupId": "test-group-001"
}
```

`chatLogs` 保留每条记录的 `chatLog`、`isRevoked` 和其他原始字段，包括文本、图片、文件及未覆盖的系统消息类型。`content` 按原值保留，不再次解析其内嵌 JSON；附件 URL 的查询参数不被删除或重编码。工具不筛选消息、不下载附件、不生成摘要或健康事实，撤回标记也不替代业务方的展示和使用规则。返回值只代表上游本次返回的数据；`chatLogsNum` 不被用于假定分页规则或保证全部历史已经获取。

`ChatHistoryQueryError` 通过 `category` 区分 `not_configured`、`invalid_configuration`、`invalid_arguments`、`http_error`、`timeout`、`transport_error`、`business_error`、`invalid_json` 和 `malformed_response`，并按适用情况携带安全的 HTTP 状态码或业务码。失败不作为合法空历史返回，异常消息不包含原始响应正文、配置地址或聊天文本。

### 7.3 后续显式接入

未来业务接入时，将工具放入本次 `ToolRunRequest.tools`，并由服务端将精确身份加入 `allowed_tools`。下例只展示工具装配；`llm_request`、`context`、`budget` 仍须按本页规则由业务方构造并随任务固定，不创建独立的模型入口或新工具运行器。

```python
from agents.tools import ToolRunRequest
from bootstrap.chat_history import create_application_chat_history_tool

tool = create_application_chat_history_tool()
tool_request = ToolRunRequest(
    llm_request=llm_request,
    context=context,
    budget=budget,
    tools=(tool,),
    allowed_tools=frozenset({tool.identity}),
)
```

工具权限与群组资源权限分别由接入方验证，不能以模型或请求自行提供的 `groupId` 代替用户与群组归属核对。健康分析在读取前取得 `VerifiedAccessScope`，固定群号与可靠发送人映射，再由 `ChatHistoryMaterialsAdapter` 调用本工具；无真实授权提供方时默认拒绝。适配器按ID去重、撤回优先、时间排序并保留单次覆盖限制，未知人不归患者，图片/文件不下载；模型资料整理再区分本人陈述、问句、家属和非健康内容。其他问卷、报告等业务仍须各自显式接入，工厂不会使任何Agent自动查询群聊。
