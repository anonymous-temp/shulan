# Shulan 应用架构：模块化单体与 Ports/Adapters

> 状态：当前分层规则；Report Agent、持久化受理与恢复已接线，独立进程、资源契约及兼容收口仍为目标
>
> 更新日期：2026-10-10；源码复核基线：`develop@c9b8988`，未执行真实环境验收
>
> 决策范围：Tornado Web 接入、业务模块、依赖组装、运行时生命周期、基础设施适配器及最终报告的未来接入契约
>
> 当前实施边界：Report 已完成目录、集中路由与显式注入迁移；原“本次不修改 report_api”的限制是历史协作边界，不再适用于当前代码

最新状态总览见 [架构导航](README.md)。当前已存在 `DurableReportService`、持久化批次/产物/回调存储和进程内周期 Worker。标准 `ReportModule` 资源契约、独立 Worker 进程和通用 WorkerRegistry 尚待建设；第 9 节的进一步收口签名是设计参考，不是当前 Python 接口。旧迁移手册保留为历史草案。

Report 当前实现见 [完整接入指南](report-agent-integration-guide.md)：模型经 AgentHub，生成时读取画像，五类产物独立保存并提供列表/详情及 Chat 原/新会话追问。上传文件已接入共享处理。产物、生成任务和回调交付状态分别持久化；Chat 通过受控报告上下文读取，不直接查库，也不把 AI 产物自动写成健康事实。

## 1. 决策摘要

Shulan 采用“模块化单体 + Ports/Adapters”架构。

模块化单体意味着项目仍作为一个进程和一个代码仓库交付，但每项业务能力有明确所有权，不再依靠一个不断扩大的 `server.py` 或一个平铺的 `services/` 目录维持结构。Ports/Adapters 意味着业务用例依赖稳定端口，MongoDB、Redis、HTTP Client、AgentHub 等具体实现由组合根注入。

本次决策固定以下规则：

1. 所有 Tornado Handler 统一位于 `web/handlers/`。
2. 所有公开路由统一、显式地在 `web/routes.py` 注册，并拥有唯一名称。
3. Handler 只处理 HTTP/SSE 协议并调用应用服务，不创建 Client、不访问数据库、不编排长流程。
4. 核心功能按业务模块归属到 `modules/<module>/`，不再把所有业务长期平铺在顶层 `services/`。
5. 业务模块在 `ports.py` 定义需要的 Repository、外部 Client 和 AI 能力端口；具体实现位于 `adapters/`。Analysis、Chat、Record 已按此规则建立第一批端口和适配器。
6. `bootstrap/` 是唯一组合根，负责创建并连接 Handler、Service、Repository、AgentRuntime、外部 Client 和 Worker，并登记由应用拥有的可关闭资源。
7. `ApplicationBundle`/`AppContainer` 共同形成类型化所有权边界并共享一个已实现的 `ResourceRegistry`。Registry 支持同步/异步资源、逆序关闭和失败聚合；独立 `WorkerRegistry` 仍是后续收敛目标。
8. `server.py` 仅作为迁移期兼容入口，最终不再包含 Handler、路由、业务依赖组装或 Worker 生命周期实现。
9. 最终报告以持久记录受理，任务执行与回调交付分离；TaskSupervisor 只负责进程内追踪。当前回调持续退避补投，没有最大尝试次数、死信终止或 TTL，不能套用旧设计中的有限重试状态。
10. Report 生产 Handler 显式注入 DurableReportService，查询 Handler 注入产物存储；旧 Pipeline/协调器仅保留显式注入兼容。肠道屏障由独立 GutBarrierService、集合和主报告投递链负责，两者都由 bootstrap 组装、runtime 调度。

## 2. 背景与问题

### 2.1 现有合理边界

当前项目已经存在可以延续的架构基础：

- `main.py` 是配置加载和进程启动入口；
- `modules/{analysis,record,chat}/application/service.py` 承担主要应用用例，`services/*.py` 保留兼容入口；Record 的 `intake.py` 负责主诉与信息覆盖，`presentation.py` 负责固定文案和承接来源校验；共享 `agents/llm/text_quality.py` 提供生成文本检测，目前由 Record 接入。
- `agents/` 已形成 AgentHub、Graph、LLM Gateway、运行时和持久化边界；
- `health_profile/` 已具备领域模型、应用组装、Repository 和 Worker；
- `web/lifecycle.py` 已包含请求排空与应用生命周期能力；
- `db/` 集中管理现有 MongoDB、Redis 连接和集合对象。
- Analysis、Chat、Record 已在 `modules/*/ports.py` 定义自有出站端口，具体 DocMind、Session、Prompt 和 Outbox 实现已进入 `adapters/`；
- `runtime/resources.py` 已提供共享资源登记、同步/异步逆序关闭及失败聚合。

因此本方案不是全面重写，而是先建立统一的接入和所有权规则，再逐模块迁移现有实现。

### 2.2 `server.py` 的职责过载

迁移前的 `server.py` 同时承载通用响应、健康检查和业务 Handler、全部路由、Service/Repository/AgentRuntime/Report Pipeline 组装、周期 Worker、HTTPServer、IOLoop 和优雅停服。

这会把 HTTP 契约、业务变化、依赖变化和运维生命周期变化集中到同一个文件。继续把新 Handler 复制进 `server.py` 只能获得位置一致，不能获得职责一致。

### 2.3 `report_api/` 的边界偏离

迁移前的 `report_api/` 同时包含 Handler、生产依赖 builder、应用流程、医学规则、模型调用、外部 Client、回调和遗留实现。这个以 `api` 命名的混合包现已删除，不能继续作为当前入口。

当前已落地的 Report 接线为：

- `web/routes.py` 集中注册提交、列表及详情三条 Report 路由；
- 路由 `report.submit` 引用 `web.handlers.report.ReportGenerateHandler`；
- `bootstrap/` 调用 `adapters.report.factory.build_report_pipeline()`；
- 生产 Handler 通过 route kwargs 注入 DurableReportService；旧 Pipeline、任务监督器与协调器保留显式构造兼容；
- 组合根检查自己新建 Pipeline 的直接成员，并把其中可关闭 Client 登记到 `ResourceRegistry`；
- `modules/report/` 保存业务代码与首批 ports，`adapters/report/` 保存具体外部实现；
- 主叙事、follow-up 和报告检索均经已注册 Report Graph，业务文件不再创建全局模型 Client；

当前剩余治理包括 Pipeline 多职责、应用入口、模块资源声明及独立 Worker 进程；真实数据库/回调恢复需部署验收。旧包与 settings 路由迁移已完成，新代码不得复制 Service Locator 或导入时创建模型 Client。

## 3. 范围与所有权

### 3.1 首轮实施范围（历史阶段，已基本完成）

- 建立 `web/` 骨架和统一 BaseHandler；
- 将 Report 之外的 Tornado Handler 从 `server.py` 迁入 `web/handlers/`；
- 在 `web/routes.py` 集中注册包括 Report 在内的全部公开路由；
- 建立 `bootstrap/` 的类型化依赖容器和应用工厂；
- 为 Analysis、Chat、Record 建立模块自有端口，并将 DocMind、MongoDB/Redis Session、PromptManager 和 Chat Outbox 具体实现移入 Adapter；
- 建立 `ApplicationBundle`/`AppContainer` 共享的 `ResourceRegistry`，固定同步/异步资源、逆序关闭及失败聚合语义；
- 保持 Analysis、Record、Chat、Health Profile 的外部 API、SSE、存储和 Agent 行为兼容；
- 为现有 `services/`、`health_profile/`、`db/` 提供清晰的迁移归属，不要求为了目录整齐一次性改完所有导入；
- 增加覆盖非 Report 范围的路由、构造、端口注入、依赖方向、资源所有权和生命周期测试；
- 更新本架构文档及文档导航。

### 3.2 首轮未包含的工作（历史范围，不代表当前禁止修改）

以下是 2026-08-24 首轮迁移的排除范围。后续已完成 Report 目录与 Handler 迁移；AI 接入和可靠化仍按当前任务逐步推进，不能把旧排除项当作永久豁免。

- 修改 `report_api/` 内的 Handler、Pipeline、Schema、规则、Client 或回调；
- 改变 `/api/v1/report` 的请求、立即响应、回调和错误行为；
- 实现 Report 持久化 job、租约、重试、死信或人工 redrive；
- 将 Report 模型调用迁入 AgentHub；
- 删除 Report 的兼容 settings 或旧 builder；
- 借架构迁移修改现有字段、状态码、SSE 事件顺序或数据库语义；
- 一次性迁移 `db/`、`agents/` 或 `health_profile/` 的全部内部结构。
- 建立独立 `WorkerRegistry`；当前 Worker 仍由 Container 与 Runtime 显式持有和调度。

### 3.3 共享文件协作规则

Report 负责人未来需要修改共享接入面时，应仅围绕下列文件协调：

- `web/routes.py`：在保留 `report.submit` 的前提下按需调整最小服务注入，不重新建立第二张路由表；
- `bootstrap/container.py`、`bootstrap/factory.py`：注入 ReportService、Repository、Client 和 Worker；
- `runtime/`：注册 Report Worker 和关闭钩子；
- `tests/unit/architecture/`：移除临时例外并启用完整门禁；
- 本文档及 Report 专项文档：同步实施状态。

共享文件的改动应与 `modules/report/`、`adapters/report/` 内部重构分提交，避免 Report 和非 Report 迁移难以审查、回滚。Git 流程按 CONTRIBUTING.md 与 Report 协作指南执行。

## 4. 总体架构

```text
main.py
  │
  ▼
bootstrap/                     唯一组合根
  ├── 构造 AppContainer
  ├── 构造 ApplicationBundle
  └── 连接具体 Adapter 与业务端口
  │
  ├───────────────┐
  ▼               ▼
web/              runtime/
路由、Handler      生命周期、Worker、资源关闭
  │
  ▼
modules/*/application          应用用例与流程边界
  │                  ▲
  ├── domain/        │ ports
  ▼                  │
modules/*/domain     │
                     │
adapters/ ───────────┘          MongoDB、Redis、HTTP、AgentHub
```

依赖方向由外向内：Web 和 Adapter 可以依赖应用/领域契约；应用与领域不能反向依赖 Tornado 或具体基础设施。`bootstrap/` 负责把两侧连接起来。

## 5. 目标目录结构

以下是迁移完成后的所有权模型，不要求本次建立全部空目录或机械搬移全部文件：

```text
shulan/
├── main.py
├── server.py                         # 迁移期兼容入口；最终仅 re-export
│
├── bootstrap/
│   ├── container.py                  # 类型化 AppContainer
│   └── factory.py                    # 创建 Service、Adapter、Bundle
│
├── runtime/
│   ├── application.py                # ApplicationBundle
│   ├── lifecycle.py                  # READY/DRAINING/STOPPED
│   ├── resources.py                  # 同步/异步资源、逆序关闭与失败聚合
│   ├── supervisor.py                 # 当前进程任务追踪
│   └── workers.py                    # 未来 WorkerRegistry 收敛位置
│
├── web/
│   ├── application.py                # Tornado Application 构造
│   ├── routes.py                     # 唯一公开路由清单
│   ├── errors.py                     # 应用异常到 HTTP 的映射
│   ├── lifecycle.py                  # 请求追踪和 draining 适配
│   ├── schemas/                      # 仅属于 HTTP 的 DTO
│   └── handlers/
│       ├── base.py
│       ├── health.py
│       ├── analysis.py
│       ├── record.py
│       ├── chat.py
│       ├── health_profile.py
│       └── report.py                 # 已实现；当前注入 Pipeline 与任务协调器
│
├── modules/
│   ├── analysis/
│   │   ├── application/
│   │   ├── domain/
│   │   └── ports.py
│   ├── record/                       # application + ports.py
│   ├── chat/                         # application + domain + ports.py
│   ├── health_profile/
│   └── report/                       # 已迁入；以下 application/domain 为进一步拆分目标
│       ├── application/
│       │   ├── service.py
│       │   ├── pipeline.py
│       │   └── worker.py
│       ├── domain/
│       │   ├── models.py
│       │   ├── jobs.py
│       │   ├── clinical.py
│       │   ├── evidence.py
│       │   └── parsing.py
│       └── ports.py
│
├── adapters/
│   ├── analysis/
│   │   └── docmind.py                # 已实现：DocumentParsingClient
│   ├── persistence/
│   │   └── sessions.py               # 已实现：Chat/Record MongoDB/Redis
│   ├── messaging/
│   │   └── chat_outbox.py            # 已实现：Chat Outbox
│   ├── record/
│   │   └── prompts.py                # 已实现：PromptRenderer
│   ├── ai/
│   │   └── report_narrative.py       # Report 未来目标
│   └── report/                       # 已存在；以下文件名及 job_repository 为目标拆分
│       ├── source_client.py
│       ├── document_client.py
│       ├── evidence_client.py
│       ├── callback_client.py
│       └── job_repository.py
│
├── agents/                           # 现有 AI 编排平台
├── services/                         # 迁移期兼容应用入口
├── health_profile/                   # 迁移期现有画像模块
├── db/                               # 迁移期现有持久化实现
├── config/
├── prompts/
├── utils/                            # 仅无 I/O 的纯工具
└── tests/
```

### 5.1 渐进迁移规则

- Analysis、Chat、Record 的实现已迁入对应 `modules/*/application`，`services/*.py` 只保留弃用导入兼容；
- 现有 `health_profile/` 可以整体视为 `modules/health_profile`，先通过端口接入，不在 Web 搬移中拆包；
- Chat/Record Session 已由 `adapters/persistence/sessions.py` 适配现有 `db/` Client/collection；其余 `db/` 调用不为目录整齐一次性迁移；
- `agents/` 是共享 AI 平台；业务模块通过 AgentHub 的稳定能力契约使用它；
- 兼容 re-export 必须注明删除条件，旧路径不得继续新增实现。

## 6. 层和模块职责

### 6.1 `main.py`

`main.py` 只负责加载配置、调用顶层应用工厂、运行 ApplicationBundle，并把启动失败映射为非零退出码。它不导入具体 Handler，也不逐个创建 Repository 或 Client。

### 6.2 `bootstrap/`

`bootstrap/` 是唯一组合根，允许同时了解具体 Service、Adapter、AgentRuntime、Worker 和配置。它负责从配置创建资源、将 Adapter 绑定到端口、构造 `AppContainer` 和 `ApplicationBundle`，并把所拥有的可关闭资源登记到共享 `ResourceRegistry`。

当前组合根已经显式构造并注入 Analysis 的 DocMind Client、Chat/Record 的 MongoDB/Redis Session Adapter、Record Prompt Renderer 和 Chat Outbox。`tests/unit/bootstrap/test_factory.py` 固定这些端口只在组合根连接，并验证 legacy Report Pipeline 直接可关闭成员的资源登记。

禁止在 Handler、领域模型、普通工具函数或模块导入阶段创建应用级 Client。

### 6.3 `web/`

Web 层负责路由、HTTP 方法、body/header 解析、入站 DTO 校验、状态码、响应包络、SSE 事件、request ID、日志脱敏、断连和 draining，并调用一个明确的应用服务方法。

Web 层不负责数据库访问、外部 HTTP 调用、Agent Graph 选择、多阶段流程编排、可靠任务持久化或第三方重试。

### 6.4 `modules/*/application`

应用层表达用户可见用例和一致性边界。例如 Analysis 的体检报告解释，Record 的会话创建/回答/完成，Chat 的消息/历史/建议，Health Profile 的事实与审计，以及 Report 的提交/查询/重试/执行。

应用服务可以协调领域对象、Repository 端口、AgentHub 端口和外部服务端口，但不依赖 Tornado、不读取 HTTP body、不返回 `HTTPError`。

### 6.5 `modules/*/domain`

领域层保存状态机、不可变模型、业务校验和确定性规则。它不能导入 Tornado、MongoDB collection、Redis Client、HTTP Client、AION Client 或环境配置。

### 6.6 `modules/*/ports.py`

端口由使用它的业务模块拥有，而不是由 Adapter 拥有。端口应表达业务语义，例如 `RecordSessionCache.save()`，而不是泄漏底层 `find_one_and_update()`。

当前已建立：

- `modules/analysis/ports.py`：`DocumentParsingClient`；
- `modules/chat/ports.py`：Session Repository、Outbox、QA 门禁、画像来源和只读画像记忆；
- `modules/record/ports.py`：Session Repository/Cache、Prompt Renderer、报告处理和画像相关端口。

Health Profile 当前只有模块公共入口，内部 Repository/Worker 尚未按同样目录拆分。Report 的 `modules/report/ports.py` 已包含来源、文档、循证、回调、follow-up、画像快照、产物存储和报告上下文选择端口；`modules/report/durable_ports.py` 定义持久任务存储与回调传输端口。后续完整模块资源契约仍需单独收口。

### 6.7 `adapters/`

Adapter 实现业务端口。当前 `adapters/analysis/docmind.py` 构造 DocMind Client，`adapters/persistence/sessions.py` 提供 Chat/Record MongoDB 与 Redis Session 实现，`adapters/record/prompts.py` 适配 PromptManager，`adapters/messaging/chat_outbox.py` 实现 Chat Outbox。Adapter 负责隔离具体依赖，并逐步把驱动或供应商异常转换为项目稳定异常，避免其穿透到 Handler。

Report 的下载、共享文件处理、EviMed、公共证据、回调、画像快照和持久存储实现位于 `adapters/report/`；主叙事及 follow-up 经 Agent Adapter 调用 AgentHub，报告上下文选择也有独立 Adapter。Health Profile 内部具体持久化仍在顶层旧包，尚待单独拆分；Report 完整资源所有权契约也仍待收口。

### 6.8 `runtime/`

Runtime 负责进程生命周期而不实现业务规则：生命周期状态和 readiness、当前请求与任务追踪、Worker 启停和排空、HTTPServer 停止接收新连接、资源逆序关闭，以及信号、超时和退出码协调。

`ResourceRegistry` 已实现同步 `close()`、异步 `aclose()`、登记逆序关闭、重复关闭幂等、关闭后拒绝登记，以及尝试关闭全部资源后用 `ResourceCloseError` 聚合失败。同步关闭遇到异步资源时会在未关闭任何资源前要求改用 `aclose()`。`AppContainer` 与 `ApplicationBundle` 使用同一 Registry；Bundle 会登记自己拥有的 AgentRuntime。Worker 目前仍由 Container/Runtime 显式持有，独立 `WorkerRegistry` 尚未实现。

### 6.9 `utils/`

进入 `utils/` 的代码必须无业务状态、无 I/O、无框架资源、输入输出明确。稳定 ID、文本清洗和纯分页计算可以进入 utils；Repository、Outbox、Worker、LLM Client、知识检索 Client 不能进入 utils。

上述是新增代码的目标规则，不是当前目录的完整事实：`utils/knowledge.py` 仍是已使用的知识库 HTTP 客户端；`utils/log/` 已是公共日志包，包含初始化与兼容入口、身份上下文、文件轮转和正文缩略工具。业务继续从 `utils.log` 导入日志入口，配置由现有日志环境变量控制；后续按职责评审迁移，不为满足目录形式改动现有实现。

## 7. Tornado 接口规范

### 7.1 集中注册、分文件实现

`web/routes.py` 是全部公开接口的唯一注册清单。推荐使用命名 `URLSpec`：

```python
def build_routes(container: AppContainer) -> list[tornado.web.URLSpec]:
    return [
        tornado.web.URLSpec(
            r"/api/v1/chat/send/complete",
            ChatSendCompleteHandler,
            {"service": container.chat_service},
            name="chat.send.complete",
        ),
    ]
```

禁止通过导入副作用、装饰器扫描或业务模块自行调用 Application 来隐式注册路由。集中注册不代表所有 Handler 必须放在同一个文件。

### 7.2 显式依赖注入

标准 Handler 通过 Tornado `initialize()` 接收最小应用服务依赖：

```python
class ChatSendCompleteHandler(BaseHandler):
    def initialize(self, service: ChatService) -> None:
        self._service = service

    async def post(self) -> None:
        command = ChatSendCommand.from_http(self.json_body)
        result = await self._service.send_complete(command)
        self.write_success(result)
```

标准 Handler 禁止：

- 导入 `db`、具体 `adapters`、AION 或具体 Agent Graph；
- 调用 `build_*_client()`；
- 使用 `asyncio.create_task()` 承诺可靠任务；
- 从 `application.settings` 按字符串查找业务 Service；
- 持有多项无关业务服务并承担跨域编排。

Report Handler 已不使用 `report_pipeline` setting，旧例外失效。Health 探针与生命周期基类仍保留运行时 settings 查找，见 API 合规矩阵；不能将这些剩余问题描述为全部 Handler 已完全符合目标。

### 7.3 Handler 与 Service 的错误边界

应用服务抛出稳定应用异常，Web 层统一映射为状态码和错误包络。Handler 不应捕获所有 `Exception` 后自行构造不同格式的响应。SSE 的终态错误也应通过同一错误分类映射为稳定事件。

## 8. AppContainer 与 ApplicationBundle

### 8.1 类型化依赖容器

`AppContainer` 只保存构造完成的应用依赖，不负责动态 Service Locator：

下例是收口后的目标类型示意。当前真实字段见 `bootstrap/container.py`，包括 analysis_service_factory、report_pipeline、report_task_coordinator 等，多数字段仍标注 Any；不能照此示意替换构造参数。

```python
@dataclass(frozen=True)
class AppContainer:
    agent_runtime: AgentRuntime
    analysis_service: AnalysisService
    record_service: RecordService
    chat_service: ChatService
    health_profile_service: HealthProfileService
    lifecycle: ApplicationLifecycle
    task_supervisor: TaskSupervisor
    resources: ResourceRegistry
```

Container 还显式持有当前画像 Worker 和迁移期依赖，但 Handler 只通过路由 kwargs 接收最小服务。未来独立 `WorkerRegistry` 建立后，Worker 可从 Container 的业务依赖集合中进一步收口；在此之前不得为了形式统一破坏现有启停语义。

### 8.2 顶层运行对象

当前应用工厂返回明确的顶层对象，并复用 Container 的资源 Registry：

```python
@dataclass
class ApplicationBundle:
    web_application: tornado.web.Application
    container: AppContainer
    owns_agent_runtime: bool = False
    resources: ResourceRegistry | None = None
```

Bundle 未显式传入 Registry 时采用 `container.resources`，因此 Container 与 Bundle 使用同一份资源所有权记录；当 `owns_agent_runtime=True` 时，Bundle 会在尚未登记的前提下登记 AgentRuntime。`close()` 处理纯同步资源，`aclose()` 统一处理同步和异步资源。

`tornado.web.Application.settings` 最终只保留 Tornado 框架级配置。业务服务通过路由 kwargs 注入；Worker 和可关闭资源由 Bundle/Container 类型化边界持有。独立 `ResourceRegistry` 已实现，只有 `WorkerRegistry` 仍是后续目标。

当前生产 Report Handler 显式注入 DurableReportService，Service 持有 Pipeline；显式旧 Pipeline 注入仅保留兼容。组合根仍以 `_register_owned_closeables` 登记新建 Pipeline 及直接可关闭成员，Pipeline 也有 aclose；这不是未来完整模块资源声明的替代品。后续明确所有权后需一并复核重复关闭、借用资源和创建失败清理，再移除反射探测。

### 8.3 ResourceRegistry 语义

`ResourceRegistry` 固定以下行为：

1. 分别通过 `register_sync()` 和 `register_async()` 显式登记资源；
2. 按登记的逆序关闭，匹配依赖的反向释放顺序；
3. `aclose()` 同时处理同步和异步资源；
4. 同步 `close()` 发现异步资源时不做部分关闭，而是要求调用 `aclose()`；
5. 单个资源关闭失败不阻止其余资源关闭，最后统一抛出包含全部失败的 `ResourceCloseError`；
6. 关闭完成后重复关闭为空操作，且禁止再登记资源。

### 8.4 启停顺序

```text
加载配置
→ 创建共享资源和 Adapter
→ 创建应用服务与 AppContainer
→ 创建 Tornado Application
→ 创建 ApplicationBundle
→ 启动 HTTPServer
→ 启动 Worker
→ READY
→ 收到终止信号并进入 DRAINING
→ 停止接收新请求和领取新任务
→ 等待请求与执行中任务
→ 关闭 HTTP 连接
→ 由共享 ResourceRegistry 按登记逆序关闭 AgentRuntime、HTTP Client 等托管资源
→ STOPPED
```

构造阶段和启动阶段必须可区分。测试创建 Application 时不应隐式启动无限 Worker 或发起网络请求。

## 9. Report 未来接入契约

本节保留进一步收口的目标契约，原接口签名、release/activation 和状态枚举属于设计参考，不代表现有 DurableReportService 的实现。当前持久化受理、生成中断恢复及持续回调补投见 [Report 指南第 14 节](report-agent-integration-guide.md#14-持久化受理生成中断恢复与长期回调)。后续只针对现有链路缺口评审，不重新建设第二套持久任务或机械执行旧迁移手册。

### 9.1 标准应用入口

Report Handler 最终只能依赖一个 `ReportService`。建议稳定公共接口至少包含：

```python
class ReportService(Protocol):
    async def submit(self, command: ReportSubmitCommand) -> ReportSubmissionResult: ...
    async def release_after_ack(
        self, job_id: str, http_request_uuid: str
    ) -> None: ...
    async def get(self, query: ReportJobQuery) -> ReportJobView: ...
    async def retry(self, command: ReportRetryCommand) -> ReportJobView: ...
```

`submit()` 只负责验证受理条件、创建或返回幂等但暂不可 claim 的 job 并快速响应，不在请求协程中执行完整 Pipeline。Handler 必须在 `await finish()` 后调用 `release_after_ack()`；若进程在两者之间崩溃，由 activation deadline 补偿扫描幂等释放，不能靠 sleep 保证响应先于 Pipeline。

### 9.2 组合根提供者

Report 模块应提供一个无 Tornado 副作用的构造入口，由 `bootstrap/` 调用：

```python
@dataclass
class ReportModule:
    service: ReportService
    worker: ReportWorker
    resources: tuple[AsyncCloseable | Closeable, ...]

def build_report_module(
    *,
    config: ReportConfig,
    job_repository: ReportJobRepository,
    qa_snapshot_reader: QASnapshotReader,
    source_client: ReportSourceClient,
    document_parser: DocumentParser,
    evidence_retriever: EvidenceRetriever,
    public_evidence_retriever: PublicEvidenceRetriever,
    narrative_client_factory: NarrativeClientFactory,
    callback_serializer: ReportCallbackSerializer,
    callback_destination_resolver: CallbackDestinationResolver,
    callback_client: ReportCallbackClient,
    followup_generator_factory: FollowupGeneratorFactory | None,
    product_catalog_client: ProductCatalogClient | None,
    clock: Clock,
) -> ReportModule: ...
```

目标构造函数不得在 import 时启动 Worker、自己安装路由或写入 Tornado settings。上例尚未实现，FollowupReportService 和 WorkerRegistry 也仅为目标；当前四类 follow-up 均在使用，不能根据旧草案擅自废弃。当前 AI 接线继续基于既有 adapters/report/factory.py 和 bootstrap，不要求先建立上述模块工厂。

### 9.3 持久化 job 契约

Report job 至少需要以下语义：

- 稳定幂等键和唯一约束；
- 持久化时先不可 claim，HTTP ack 完成后 release，并能补偿超时未 release 的受理；
- 不可变输入快照或可复现的版本化引用；
- 分离生成与投递状态：生成至少包含 `pending`、`running`、`retry_wait`、`generated`、`dead_letter`，投递至少包含 `not_ready`、`pending`、`delivering`、`retry_wait`、`delivered`、`dead_letter`；
- 原子领取和有所有者的执行租约；
- 租约续期与过期恢复；
- 有上限、可观测的重试和下一次执行时间；
- 错误分类、尝试次数和必要审计字段；
- 在内部 job/outbox 中保存稳定 `callback_event_id` 并实现终态回调去重；未经 API 契约评审，不把该内部字段加入当前回调 body；
- 查询、人工 retry/redrive 和运维可观测入口。

`TaskSupervisor` 只追踪当前进程正在执行的任务并协助停服，不能替代持久化 job、租约或重试状态。

### 9.4 AI 与外部依赖端口

Report 应在 `modules/report/ports.py` 定义 `QASnapshotReader`、`ReportSourceClient`、`DocumentParser`、`EvidenceRetriever`、`NarrativeClientFactory`、`NarrativeGenerator`、`ReportCallbackClient`、`ReportJobRepository` 等业务语义端口。Factory 按 Report job 创建只保存不可变 job/用户/会话标识的 Generator；两者都不持有独立模型 Client。

具体实现位于 `adapters/report/`。主叙事 `report.narrative`、后续任务 `report.followup` 和报告读取 `report.retrieve_context` 已注册；follow-up 使用服务器允许列表选择任务和固定 Prompt。Report 业务不得创建 AION Client，统一 AI 静态扫描已包含 modules 和 adapters；本轮不宣称全部门禁或真实模型验收通过。

### 9.5 Report 交接清单

Report 负责人交付时应逐项确认：

- [ ] 冻结并覆盖现有 `/api/v1/report` 请求、立即响应、错误和回调契约；
- [x] 将 Handler 迁入 `web/handlers/report.py` 并通过 route kwargs 注入；
- [ ] 将 Handler 进一步收口为只依赖 ReportService；
- [ ] 提供 `build_report_module()` 或等价的无副作用构造入口；
- [ ] 将 Pipeline 从 Web、配置和全局资源创建中解耦；
- [ ] 拆分 HTTP DTO、领域模型和回调模型的所有权；
- [x] 将来源、文档、证据和回调具体实现归入 adapters/report，并建立首批 ports；
- [x] 将主叙事和后续模型调用迁入 AgentHub/LLMGateway；
- [x] 接入持久化受理、请求复用、生成领取/续租、中断恢复及回调持续补投；
- [ ] 如需生成重试上限或死信等新策略，基于当前行为另行评审，不能按旧状态枚举默认实现；
- [ ] 如需 finish 后 release/activation 协议，单独评审其必要性；当前生产路径不采用该目标接口；
- [x] 按产物建立幂等交付记录和稳定 Idempotency-Key，接收端处理重复投递；未经 API 评审不增加回调字段；
- [x] 将生成、恢复、回调周期调度接入 runtime，由 ResourceRegistry 管理已登记资源；
- [ ] 建立完整 ReportModule 资源契约及独立 Worker 进程；
- [x] 使用 `report.submit` 命名路由和新 Web Handler，删除 report_api 旧包；
- [x] 删除 Report Handler 的 report_pipeline settings 查找；
- [ ] 完成 AI、资源、任务和领域边界门禁，不能仅凭旧目录删除宣称全部通过；
- [ ] 验证进程强制退出、租约过期、重复领取、重复提交和重复回调；
- [x] 同步本次源码实现状态；API 契约已发布内容继续按现行版本维护；

周期 Worker 已接入 ApplicationBundle 的启停与排空，但没有通用 WorkerRegistry 或完整 ReportModule 资源清单；不能把已有 runtime 接线等同于模块资源契约全部收口。

## 10. 依赖规则与架构门禁

| 发起位置 | 允许依赖 | 禁止依赖 |
|---|---|---|
| `web/handlers` | 应用服务接口、Web DTO、Web 公共能力 | DB、具体 Adapter、AION、具体 Graph |
| `web/routes.py` | Handler、类型化 Container | 业务流程、数据库构造、外部 Client 构造 |
| `modules/*/application` | 本模块领域、端口、共享应用契约 | Tornado、Handler、具体 Adapter、环境配置 |
| `modules/*/domain` | 本模块模型、纯 utils | Web、MongoDB、Redis、HTTPX、AION |
| `adapters` | 所实现的端口、第三方库 | Handler、反向调用业务流程 |
| `runtime` | 生命周期协议、Worker/资源协议 | 医学规则、请求 DTO、具体业务流程 |
| `utils` | 标准库和真正通用依赖 | 项目业务模块、I/O 资源、框架全局状态 |
| `bootstrap` | 全部构造类型 | 业务规则实现 |

静态或构造测试应固定以下门禁：

1. `server.py` 不再定义 Handler 或公开路由。
2. 所有公开路由只在 `web/routes.py` 注册，路径和名称均唯一。
3. 所有业务 Handler（含 Report）不从 `application.settings` 获取业务依赖；Health 与生命周期兼容仍需另行收口。
4. Handler 中禁止可靠语义的 `asyncio.create_task()`。
5. `web/` 不导入 `db`、具体 Adapter、AION 或具体 Graph。
6. 应用层和领域层不导入 Tornado。
7. 领域层不导入具体 DB、Redis、HTTP 或模型 SDK。
8. 只有组合根可以连接具体 Service 和 Adapter。
9. 只有统一 LLM Gateway 可以创建 AION Chat Client。
10. `utils/` 不依赖业务模块且不创建 I/O 资源。
11. 禁止恢复 report_api 旧导入或 Report 业务 settings 查找；Report 通过 AgentHub 接入，不得新增模型直连或通过移动文件绕开门禁。
12. 新代码不得创建新的 `*_api` 混合业务包。

当前 `test_web_architecture.py` 已增加“应用服务除 `agents` 公共契约与纯工具外，只依赖本模块端口”的门禁；`tests/unit/bootstrap/test_factory.py` 固定具体 Adapter 在组合根注入，`tests/unit/runtime/test_resources.py` 固定 Registry 的关闭语义。

## 11. 测试策略

### 11.1 Web 契约测试

- 使用 Fake Service 测试 Handler；
- 固定字段、状态码、响应包络和 SSE 事件；
- 验证错误映射、断连和 draining；
- 验证路由完整性、唯一名称和 URL 顺序；
- 比较迁移前后的对外契约，不在机械搬移中顺带修正行为。

### 11.2 应用与领域测试

- 应用服务注入 Fake Repository、Client 和 Agent capability；
- 领域规则采用纯单元测试；
- 跨存储流程覆盖幂等、失败补偿和重试；
- 不通过启动 Tornado 才能测试核心业务。

### 11.3 Adapter 和 Bootstrap 测试

- MongoDB、Redis 和外部 API 使用集成/契约测试；
- 验证组合根把具体 Adapter 单次注入正确端口；
- 验证 Container/Bundle 共享 Registry，所拥有的资源被登记；
- 验证测试构造 Application 不启动后台无限循环；
- 验证同步/异步资源逆序关闭、重复关闭、异步关闭要求和多失败聚合。

### 11.4 Report 专项测试

现有离线覆盖包括 Report Graph/模型适配、画像选择、产物查询、Chat 上下文、持久受理、续租、中断恢复、回调补投和人工 redrive。真实 MongoDB 多进程、强退及接收端幂等仍需部署环境验收；本轮仅改文档，不重跑业务测试。

## 12. 分阶段实施

### 阶段 A：非 Report Web 与组合根迁移（历史首轮，已完成）

1. 冻结当前 API、SSE、路由和生命周期契约。
2. 建立 `web/handlers/`、`web/routes.py` 和 `web/application.py`。
3. 迁移 Health、Analysis、Record、Chat、Health Profile Handler。
4. 建立 `bootstrap/`、类型化 Container 和 ApplicationBundle。
5. 将 Worker/资源启停从 `server.py` 移入 `runtime/`。
6. 首轮曾保留 Report legacy bridge；后续目录与路由切片已将其移除，不再重建。
7. 保持 `server.make_app()`、`server.run()` 等必要兼容入口。
8. 增加路由、依赖和构造门禁，运行现有测试。

### 阶段 B：非 Report 业务模块归位（第一轮已完成）

已完成：

1. Analysis、Record、Chat 的稳定应用入口迁入 `modules/*/application`；
2. 三个模块建立自有 ports，Session、DocMind、Prompt 和 Outbox 具体实现进入 `adapters/`；
3. `bootstrap/` 显式构造并注入具体 Adapter；
4. 建立 ResourceRegistry 和 `tests/unit/bootstrap/`、`tests/unit/runtime/` 测试。

仍待后续按需推进：Health Profile 内部拆分、更多基础设施适配，以及确认无旧调用方后删除 `services/` 兼容别名。机械搬移、接口重命名和行为改造继续分开提交。

### 阶段 C：Report 独立改造（Report 负责人）

已接线：目录与路由、Agent、受控画像、共享文件、五类产物/查询、Chat 追问、持久化受理、恢复和回调 Worker。进一步拆分 Pipeline、稳定应用接口、独立 Worker 进程、完整资源契约及真实环境验收仍待完成，不按历史草案重复创建已有组件。

### 阶段 D：收口

- 将 `server.py` 缩减为兼容转发或删除；
- 清理无调用方的兼容模块；
- 收紧全部架构门禁；
- 同步 README、Wiki、API、运维和架构文档；
- 在 Linux 和真实进程管理器环境验证优雅停服与 Worker 恢复。

## 13. 风险与控制

| 风险 | 表现 | 控制措施 |
|---|---|---|
| 大爆炸式搬移 | review 困难、回归定位困难 | 一次一个层或业务域，机械搬移与行为修改分开提交 |
| import 环 | Web、应用、领域互相导入 | 先定义端口和 DTO 所有权，组合根做具体连接 |
| 测试 patch 失效 | mock 仍 patch 旧路径 | 短期 re-export，逐批更新并增加真实构造测试 |
| 隐式资源创建 | import 时连接 Mongo/HTTP | 所有创建动作移到组合根，模块导入无网络副作用 |
| Service Locator 回流 | Handler 从 settings 查字符串 | 显式 URLSpec kwargs 注入，静态测试禁止 |
| Report 与主架构并发冲突 | 两方同时修改路由/工厂 | 保留明确共享文件清单，分别评审 AI 接入与可靠任务切片 |
| 把目录迁移误当作全部完成 | 剩余模型直连和任务风险被忽略 | 在既有目录继续收口端口、Agent 与应用边界，不重搬文件或恢复旧桥接 |
| 双重 Worker | 新旧链路重复生成或回调 | 单一 job 所有权、部署开关、幂等键和稳定事件 ID |
| 外部契约漂移 | 状态码、SSE、回调改变 | 契约测试作为合并门禁 |
| 文档再次滞后 | 目标路径被描述成当前事实 | 文档明确标记目标、兼容和实施阶段 |

## 14. 验收标准

### 14.1 首轮迁移验收条件（不表示全部门禁当前通过）

- `server.py` 不再定义非 Report Handler 或维护路由清单；
- `web/routes.py` 能审计全部公开 Tornado 路由，包括 report.submit；
- 非 Report Handler 位于 `web/handlers/` 并使用统一 BaseHandler；
- 非 Report Handler 通过显式注入调用应用服务，不查找业务 settings；
- 组合根集中创建 Application、Service、AgentRuntime、Worker 和资源；
- Analysis、Chat、Record 应用服务只依赖本模块 ports，具体 DocMind、Session、Prompt 和 Outbox 由 Adapter 实现并经组合根注入；
- ApplicationBundle/AppContainer 共享 ResourceRegistry，支持同步/异步资源逆序关闭和失败聚合；WorkerRegistry 仍为后续目标；
- 现有 API、SSE、数据库和 Agent 行为保持兼容；
- 旧 report_api 包和生产导入已移除，新增实现不再进入兼容路径；
- 单元测试、契约测试和架构门禁通过。

### 14.2 最终架构验收

- 每项业务能力具有明确的模块所有权和应用入口；
- `services/`、`db/` 等兼容目录完成归位或有明确保留理由；
- Web、应用、领域、Adapter 和 Runtime 依赖方向由测试固定；
- Report 不再是混合 `*_api` 包，并通过标准 ReportService 接入；
- Report 已受理任务持久化、可恢复、可幂等重试；
- Report 模型调用统一经 AgentHub；
- 兼容 `report_pipeline` setting 和架构豁免已删除；
- `server.py` 只保留薄兼容入口或已删除；
- 文档、API、运维说明与当前实现一致。

## 15. 关联文档

- [API 实现架构合规与迁移指南](./api-implementation-compliance.md)
- [Report API 端到端迁移实施手册（历史草案）](./report-api-migration-guide.md)
- [Tornado 安全停服与生产更新方案](./graceful-shutdown-and-deployment.md)
- [AI Agent 目标架构](./agent-target-architecture.md)
- [最终报告接入 Agent 中枢指南](./report-agent-integration-guide.md)
- [用户健康画像设计方案](./user-health-profile-design.md)
- [Shulan Agent Core Wiki](../../wiki/agents.md)
- [API 架构实施状态 Wiki](../../wiki/api-architecture.md)
