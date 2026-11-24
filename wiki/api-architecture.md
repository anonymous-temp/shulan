# Shulan API 架构实施状态

> 状态：当前实现快照
>
> 更新日期：2026-11-10；多智能体重构以 `refactor/agent` 本地实现为准，旧合规治理待办保留；真实服务与部署尚未验收
>
> 目标规则以 [API 实现架构合规与迁移指南](../docs/architecture/api-implementation-compliance.md)和 [Shulan 应用架构](../docs/architecture/application-architecture.md)为准

目录、关键文件和请求生命周期的入门对照见 [代码导航与架构阅读指南](code-navigation.md)；本页继续记录接口实现的合规状态和迁移边界。

## 1. 当前结论

本仓库 Tornado 进程当前提供 31 个路由模式、32 个 HTTP 方法入口，全部集中在 `web/routes.py`。接口契约仍以 `docs/api/API.md` 为准；本页说明源码归属和剩余治理边界，不代表实际部署或真实服务验收完成。

```text
集中路由与非 Report Handler 搬移：已完成
Analysis / Record / Chat 第一轮模块化：已完成
Health 探针显式注入：待完成
Health Profile 公共快照/来源契约、独立消费者 renderer：已交付；正式来源切换待完成
Report 目录、Agent、画像、产物/查询、Chat 追问及持久化受理/恢复/回调：已接线
Record / Chat / Report / Questionnaire / GutBarrier 五域主 Agent：已接线，查询旁路
肠道屏障独立主报告、持续主回调与公开 Report 委派：已接线；真实环境待验收
server / services / settings 兼容收口：待后续完成
```

最新模块边界与迁移状态见[业务域多智能体方案](../docs/architecture/modular-multi-agent-architecture.md)。Agent 封装复用既有流程，不增加入口分类模型。Record/Questionnaire 后台任务已独立恢复，普通查询不唤醒生成；Report 固定文档与 Report/Chat/Record 本域画像上下文已默认接通。Document 完整来源发布、各域正式来源迁移与 Profile 新准入仍分批进行，不能把公开 Port 或离线 Fake 当作生产切换完成。

## 2. 路由与实现状态

| 能力 | 路由 | 当前状态 | 当前所有者入口 | 下一步 |
|---|---|---|---|---|
| 问卷 | 三条 `/api/v1/record/questionnaires*`（集合 GET/POST） | 主 Agent、只读查询、后台生成/因子任务已接通 | `modules/questionnaire/service.py` | 正式来源按域迁移，保留附件来源；不新增查询触发恢复 |
| 运行健康 | `/health`、`/health/live`、`/health/ready` | 部分合规 | `web/handlers/health.py` | 注入 RuntimeHealthService，移除 settings/config 查找和固定延迟 |
| 报告解读 | `/api/v1/interpret/health-report` | 第一阶段合规 | `modules/analysis/application` | 翻译跨模块异常，使用类型化 Service 和统一错误映射 |
| 建档问答 | 七条 `/api/v1/qa/*` | 主 Agent、固定文档消费及独立后台恢复已接通 | `modules/record/application` | `/qa/create` 不再让 Handler 认识 Analysis/Health Profile 异常；统一 Record 错误类型 |
| 普通对话 | 五条 `/api/v1/chat/*` | 主 Agent 与本域画像上下文已接通 | `modules/chat/application` | 删除异常类名兼容和宽泛错误映射；新模块入口不再绕经 `services.chat` |
| 健康画像 | 八条 `/api/v1/health-profile/*` | 公共结构化快照和来源契约已交付；正式来源仍迁移中 | 顶层 `health_profile/`，由模块公共入口转发 | 包含原报告指标查询；内部 ports/Adapter、Worker 与资源所有权待收口 |
| 最终报告 | `/api/v1/report`、`/api/v1/report/list`、`/api/v1/report/get` | 部分合规 | `DurableReportService`、Report Pipeline 与产物存储 | 验证真实持久化/回调/Chat 链路，收口单一应用入口与资源契约 |
| 肠道屏障 | `/api/v1/gut-barrier/report` | 独立主 Agent、规则工具及公开 Report 委派已接通 | `GutBarrierService`、独立集合和主报告投递 | 模板绑定、主报告/解读分工及运维见 [肠道屏障 Wiki](gut-barrier.md) |

聚合 API 文档中的实时语音和通用文档解析由外部独立服务提供，不是 `web/routes.py` 中的本地 Tornado 路由，相关实现应在对应服务仓库审计。

## 3. 已知过渡边界

### 3.1 Web/Runtime

- Health Handler 从 `application.settings` 读取生命周期和任务监督器，当前架构门禁有意跳过这三条路由。
- 全部业务 Handler 的请求准入基类也从 settings 读取 lifecycle；目标是由类型化 `ShulanApplication.runtime_context` 提供只读 RequestAdmission，而不是给每条路由重复注入或保留字符串查找。
- 业务 Handler 已显式注入，但仍各自解析 DTO、捕获宽泛异常并重复映射 HTTP/SSE 错误。
- `web/application.py` 仍把 Container 的兼容 settings 全量写入 Tornado Application。

### 3.2 模块入口

- Analysis、Record、Chat 的实际实现已经位于 `modules/*/application/service.py`。
- Record/Chat 已有公共域 AgentPort，Analysis 提供公开文档处理能力；遗留模型 capability 路径与 services 门面是兼容治理项，不能据此认为所有入口已经只依赖公共 facade。
- Analysis/Record 的 `application/__init__.py` 仍经旧 `services.*` 懒加载，旧路径又 alias 回新实现；删除 `services/` 前必须先改成“旧路径单向转发新路径”。Chat facade 已直接指向模块内实现。
- Health Profile 的新模块入口继续兼容顶层旧包；已提供公共读取、来源接收与治理契约，Repository、Mongo Store、旧 Worker 的全面目录收口仍待完成。

### 3.3 Report

建档文件类型已统一为 `pdf/image`。Record 在创建前校验并归一化会话 `healthData.fileList`；Analysis 仍以具体扩展名调用 DocMind，不改变独立体检报告解读接口的扩展名契约。无扩展名图片通过 Analysis 自有 `ImageFormatProbe` 端口探测，由 `bootstrap/factory.py` 注入 `adapters/analysis/image_probe.py`，应用服务不直接依赖 HTTP 客户端。探测总超时为 10 秒，只检查文件头，拒绝压缩传输、重定向和非公网地址，校验已声明的 50 MiB 文件大小上限，并将已检查的 IP 固定到连接，同时保留原始 Host、TLS SNI 与证书验证；每次探测结束关闭客户端。

纯文件名和类别规则集中在 `utils/report_files.py`。最终报告只在读取历史会话时将旧图片扩展名或可推断的缺省类型转换为 `image`，不修改历史记录、不放宽公开请求枚举。无扩展名图片的来源指纹继续使用具体格式，JPEG 的 `jpg/jpeg` 历史别名在来源查询时兼容，避免仅因公开类别变化而重复解析已有报告。

- 稳定路由名为 `report.submit/report.list/report.get`。生产提交 Handler 注入 DurableReportService，先持久化再返回 processing；显式注入旧 Pipeline 的兼容调用方仍使用进程内协调器。
- 依赖由 `bootstrap/` 与 `adapters/` 组装，模型经 `report.narrative/report.followup`，画像与共享文件服务已接入。
- 五类产物、批次与交付记录独立持久化；`report.retrieve_context` 支持 Chat 完整/SSE、原会话及跨会话临时报告上下文，AI 产物不自动写入健康事实。
- runtime 周期调度生成、恢复及回调补投。中断恢复保留已有产物，为缺失类型补齐失败结构，不重跑解析或模型；回调持续补投，接收端须幂等。
- evaluate 解析与生成仍有独立预算，后续报告不共享同一总时限。源码接线、离线覆盖和真实部署验收分开记录；独立 Worker 进程及完整模块资源契约仍是后续目标。

### 3.4 启动兼容

- `main.py` 仍通过 `server.run()` 启动。
- `server.py` 动态创建旧 Handler 别名，并从 settings 回填旧测试和调用方需要的业务 Service。
- 独立 WorkerRegistry 尚未建立，周期 Worker 仍由 Runtime 显式持有。

## 4. 负责人快速入口

| 负责人范围 | 必读章节 | 建议首个独立 PR |
|---|---|---|
| Report | [最终报告 Agent 接入指南](../docs/architecture/report-agent-integration-guide.md) | 基于现有调用链完成真实模型/数据库/回调/Chat 验收，收口稳定应用接口与资源契约 |
| Health Profile | [迁移指南第 7 节](../docs/architecture/api-implementation-compliance.md#7-health-profile-迁移方向) | 定义模块 ports 和无副作用 builder，保持现有 Service 契约 |
| Web/Runtime | [迁移指南第 5、9 节](../docs/architecture/api-implementation-compliance.md#5-health-探针迁移方向) | 建立 RuntimeHealthService 与类型化 WebRuntimeContext，迁移 Health 和全部请求准入并增加门禁 |
| Analysis/Record/Chat | [迁移指南第 6、9 节](../docs/architecture/api-implementation-compliance.md#6-analysis-与-record-的跨模块边界) | 建立模块自有稳定错误类型，删除 Web 的跨模块异常认识 |
| Platform | [迁移指南第 9、12 节](../docs/architecture/api-implementation-compliance.md#9-共用-web-与兼容层收口) | 修正新模块到旧 `services.*` 的反向懒加载，并把 capability 标识提升到 `agents` 公共 facade |

## 5. 修改接口实现时的检查顺序

1. 在 `docs/api/API.md` 确认现有对外契约。
2. 在 `web/routes.py` 确认稳定路由名与唯一注册位置。
3. 让 Handler 只接收一个模块应用入口或运行时只读端口。
4. 在本模块定义 Command、Query、Result、Error 与 ports。
5. 在 Adapter 实现具体基础设施，并只在 `bootstrap/` 组装。
6. 为 HTTP/SSE 契约、应用服务、Adapter、bootstrap、Worker/资源和依赖方向补测试。
7. 更新本页状态、架构迁移指南、TODO/ISSUE；外部契约变化时同时更新 API 当前版与历史版本。

## 6. 当前完成定义

接口只有同时满足以下条件才可标记为最终合规：路由集中命名；Handler 显式注入且只处理传输；应用入口和异常归本模块所有；具体基础设施通过 ports/adapters 注入；可靠任务先持久化；Worker/资源所有权明确；HTTP/SSE 与故障恢复测试通过；旧 settings、builder、兼容路径和测试豁免已经删除。
