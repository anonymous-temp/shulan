# Shulan 文档导航与维护状态

> 最近源码与文档复核：2026-10-10，基线 `develop@c9b8988`；仅核对目录、接线、导航及实现状态，未执行真实服务或部署验收。2026-09-03/08 的专项记录保留为历史。
>
> 文件处理专项复核：2026-10-29，基线 `develop@618e3d4`；补充模块工作流图、直接文本抽取调用边界及导航。其余专题沿用各自标注的复核日期。
>
> 本页说明文档的权威级别、适用范围和复审状态；不替代任何 API 或技术设计正文。

## 1. 权威来源

2026-11-13，Profile 与 Analysis 公开端口已通过 !132、!133 合入，观察时间修复经 !134 合入。Document 同步 `develop@a3ccf12` 复验归档和画像发布闭环，后续 Report 支持及默认切换见 [单流程收敛计划](architecture/basic-info-single-pipeline-plan.md)。

当前 Report 已接入 Agent、受控画像、五类产物存储/查询和 Chat 报告追问，并由 DurableReportService 持久化受理、恢复和独立调度回调。Analysis、Record、Report 均已接入共享文件处理。以下“当前”状态以本次源码核验为准；历史章节中的未实施描述仅代表当时基线。

| 主题 | 权威文档 | 说明 |
|---|---|---|
| 当前外部 API 契约 | [API.md](./api/API.md) | 调用方字段、状态码、SSE、回调与示例的唯一当前契约 |
| API 编辑规则 | [API_EDITING_GUIDE.md](./api/API_EDITING_GUIDE.md) | API 版本、历史归档与编辑边界 |
| 当前源码与启动方式 | [项目 README](../README.md) | 当前目录、环境、启动和测试入口 |
| 代码目录与架构入门 | [代码导航与架构阅读指南](../wiki/code-navigation.md) | 当前目录职责、根文件、五域 Agent、公共能力、请求链路与修改入口 |
| Git 与代码规范 | [贡献指南](../CONTRIBUTING.md) | 分支、提交、评审、测试与 Ruff 标准 |
| develop 合并审核 | [develop 分支 PR 审核规范](./develop-pr-review.md) | 审核人每次必读的检查清单、拒绝条件、复审流程和审核记录模板 |
| Report 双人协作 | [Report Git 提交流程](./report-git-workflow.md) | 任务分工、模块边界、全量离线单元测试与 `develop → 任务分支 → PR → develop` 流程 |
| 架构文档状态 | [architecture/README.md](./architecture/README.md) | 架构/专项正文的当前实现、建议稿和历史验证分类 |
| 外部依赖原始资料 | [integrations/README.md](./integrations/README.md) | 供应方接口和联调资料；对外能力边界仍以聚合后的 `API.md` 为准 |
| 应用分层目标 | [Shulan 应用架构：模块化单体与 Ports/Adapters](./architecture/application-architecture.md) | 模块化、Report Agent 与持久化链路已实施；独立 Worker 进程、资源契约和兼容收口仍待完成 |
| API 实现合规与迁移 | [API 实现架构合规与迁移指南](./architecture/api-implementation-compliance.md) | 28 条本地 Tornado 路由的合规矩阵、过渡边界和验收门禁 |
| Report AI 与记忆接入 | [最终报告接入 Agent 中枢指南](./architecture/report-agent-integration-guide.md) | 模型、画像、五类产物/查询、Chat 追问及持久化受理/恢复/回调已接线；真实环境待验收 |
| Report 回调运维 | [最终报告回调运维指南](../wiki/report-callback-operations.md) | 当前命令：迁移全部积压到配置地址、只读预览、单批次查询与补投 |
| 肠道屏障实现与运维 | [肠道屏障 Wiki](../wiki/gut-barrier.md) | 独立主报告、存储/投递、Report 解读委托、模板绑定及队列检查 |
| HTTP 请求与响应日志 | [API 日志查看与配置](../wiki/http-logging.md) | 正文预览、缩略阈值、SSE DEBUG 日志及请求关联 |
| 压力测试方法与运行 | [压力测试 Wiki](../wiki/load-testing.md) | 分层场景、TTFT 指标口径、成本纪律；运行入口在 `tests/loadtest/` |
| 压力测试报告 | [chat-stream 本地阶梯实测](../wiki/loadtest-report.md) | 2026-09-23 开发服 5/10/50 并发实测基线，含拐点与效力限制，不代表线上容量 |
| 上传文件工作流、共享入口与留痕 | [上传文件统一解析指南](./architecture/shared-document-ingestion-guide.md) | 含 Analysis / Record / Report / health_profile / Chat 关系图、处理顺序、DocMind 与本地提取边界、复用与画像来源 |
| 安全停服与发布 | [Tornado 安全停服与生产更新方案](./architecture/graceful-shutdown-and-deployment.md) | 应用排空及 Report 持久恢复已实现；Linux/进程管理器演练待完成 |
| 动态建档当前实现 | [动态建档 Wiki](../wiki/record-dynamic-flow.md) | 患者分析、阶段与计数、升级清理及验证 |
| Agent 当前实现 | [Agent Core Wiki](../wiki/agents.md) | Agent 能力、运行时、持久化、测试与已知限制 |
| API 架构当前实现 | [API 架构实施状态 Wiki](../wiki/api-architecture.md) | 当前路由实现链、过渡边界和负责人快速入口 |
| 计划与问题 | [TODO.md](../TODO.md)、[ISSUE.md](../ISSUE.md) | 未实施任务与已确认风险 |

发生冲突时，Shulan 对外 API 以 `docs/api/API.md` 为准；当前代码事实以生产代码和自动化测试为准；目标设计以对应 `docs/architecture/` 文档为准。外部依赖文档和历史文档不能覆盖当前契约。

## 2. 文档分类

### 2.1 API

| 文档 | 状态 | 使用方式 |
|---|---|---|
| [API.md](./api/API.md) | 当前 | 对外接入的唯一有效契约 |
| [API_EDITING_GUIDE.md](./api/API_EDITING_GUIDE.md) | 当前 | 修改 API 文档前必读 |
| [USER_HEALTH_PROFILE_API_DRAFT.md](./api/drafts/USER_HEALTH_PROFILE_API_DRAFT.md) | 已并入当前 API | 只保留评审历史 |
| [`api/history/`](./api/history/) | 只读历史 | 已发布版本快照，不追随当前代码修改 |

### 2.2 当前架构与实施设计

| 文档 | 复审状态 | 当前定位 |
|---|---|---|
| [代码导航与架构阅读指南](../wiki/code-navigation.md) | 2026-11-10 源码核对 | 当前实现阅读入口；区分业务模块、Agent 平台、普通数据路径与本地产物 |
| [Agent 工具运行指南](../wiki/agent-tool-runtime.md) | 2026-11-12 健康分析显式接入 | 受控工具运行框架与群聊查询工具；健康分析显式消费，旧业务与全局默认注册保持原状，真实接口待验收 |
| [application-architecture.md](./architecture/application-architecture.md) | 2026-10-10 源码复核 | 分层规则有效；Report Agent 和持久化链路已接线，模块资源契约及独立进程仍待收口 |
| [api-implementation-compliance.md](./architecture/api-implementation-compliance.md) | 2026-10-10 当前治理基线 | 28 条本地 API、当前实现链及真实剩余边界 |
| [graceful-shutdown-and-deployment.md](./architecture/graceful-shutdown-and-deployment.md) | 2026-10-10 源码复核 | 应用排空与 Report 持久恢复已接线；Linux 演练及生产停止预算待验证 |
| [agent-target-architecture.md](./architecture/agent-target-architecture.md) | 2026-10-10 部分目标已实现 | Chat/Record/Analysis/Report 及画像能力已接入；通用工具循环与高级上下文仍待实施 |
| [report-agent-integration-guide.md](./architecture/report-agent-integration-guide.md) | 2026-10-10 源码复核 | 模型、画像、产物/查询、Chat 追问及持久化受理/恢复/回调；真实环境待验收 |
| [shared-document-ingestion-guide.md](./architecture/shared-document-ingestion-guide.md) | 2026-10-29 文件处理专项复核 | 含可编辑工作流图、模块职责、当前本地提取例外及共享质量/恢复边界 |
| [体检报告解读职责改造](./architecture/health-report-interpretation-implementation-plan.md) | 2026-11-11 本地分模块实现，集成验证见 ISSUE I-046 | Analysis 两步处理、可靠存储后响应、后台交付和画像完整视图；[当前实现](../wiki/health-report-interpretation.md)、[需求](./product/health-report-interpretation-requirements.md)，真实依赖尚未验收 |
| [健康分析生成接口实现计划](./architecture/health-analysis-implementation-plan.md) | 2026-11-12 本地代码与模拟客户端已实现，真实联调未验收 | 独立同步四项、五步循证和语义去重；首轮组合12658 passed / 275 skipped，本地模块汇总、最终复验及本轮提供方门槛见 [I-047](../ISSUE.md#i-047-健康分析b0前置交接与公共纯读依赖) |
| [agent-knowledge-retrieval.md](./architecture/agent-knowledge-retrieval.md) | 2026-09-03 首版已实现 | Chat/Record 按需检索及画像循证；不是最终报告检索或通用工具循环 |
| [建档问卷循证诱因分析](./architecture/record-questionnaire-evidence-workflow.md) | 2026-11-06 需求与设计，待实施 | 自有知识库现状、五步流程、候选诱因与问题映射、答案后判断更新及验收 |
| [建档问卷因素术语与工具调研](./architecture/record-questionnaire-factor-terminology.md) | 2026-11-06 调研与设计建议，待实施 | 医学术语与关系、Infermedica/Ada/DXplain 公开实践、项目表达约束和参考来源 |
| [user-health-profile-design.md](./architecture/user-health-profile-design.md) | 当前画像与循证设计 | 包含 Report 上传原始文件的受控来源；AI 生成产物独立保存，不自动写成健康事实 |
| [chat-doctor-signed-card.md](./architecture/chat-doctor-signed-card.md) | 2026-09-03 Outbox 已实现 | 历史同步发现卡片；长 SSE/WebSocket 暂缓 |
| [agent-context-ownership-reassessment.md](./architecture/agent-context-ownership-reassessment.md) | 2026-09-03 Deferred | 继续 AION Context 所有权，满足触发条件后重评 |
| [sse-text-batching.md](./architecture/sse-text-batching.md) | 当前实现 | Chat/Record 文本合并、配置和事件顺序 |
| [qa-creation-recovery.md](./architecture/qa-creation-recovery.md) | 当前实现与运维 | 建档创建持久任务、文件检查点及受控恢复 |
| [llm-web-search.md](./architecture/llm-web-search.md) | 当前参数与上游限制 | 共享网关联网搜索参数及固定 AION 版本支持范围 |
| [analysis-recovery-validation-2026-09-16.md](./architecture/analysis-recovery-validation-2026-09-16.md) | 历史专项验证记录 | v6/v7 修复与验证，不代表当前版本部署验收 |
| [analysis-first-pass-validation-2026-09-16.md](./architecture/analysis-first-pass-validation-2026-09-16.md) | 被后续修复替代的历史验证 | v5 诊断前结论，保留复现背景 |
| [aion-docmind-client-proposal.md](./architecture/aion-docmind-client-proposal.md) | 上游建议稿 | 供 AION 维护者评审，不代表已发布契约 |

### 2.3 已完成评审、历史或被替代设计

| 文档 | 状态 | 后续阅读入口 |
|---|---|---|
| [report-api-migration-guide.md](./architecture/report-api-migration-guide.md) | 2026-09-03 标为历史草案，执行基线过期 | 当前 AI 交接见 Report Agent 指南；可靠任务方案需基于最新契约重新评审，旧 PR 0—8 不直接执行 |
| [chat-langgraph-poc-review.md](./architecture/chat-langgraph-poc-review.md) | POC 已完成，保留设计记录 | 当前实现见 [Agent Core Wiki](../wiki/agents.md) |
| [health-memory-design.md](./architecture/health-memory-design.md) | 已由完整画像方案取代 | 见 [user-health-profile-design.md](./architecture/user-health-profile-design.md) |
| [aion-agent-protocol.md](./architecture/aion-agent-protocol.md) | 已归档 | 只有出现真实工具协议缺口时重新评审 |
| [chat-doctor-signed-card-draft.md](./architecture/chat-doctor-signed-card-draft.md) | 历史草案 | 见当前实施设计 |

### 2.4 外部依赖接口

| 文档 | 状态 | 说明 |
|---|---|---|
| [外部依赖接口导航](./integrations/README.md) | 当前内部参考 | 目录边界、供应商分组和维护规则 |
| [EviMed 知识库检索 API](./integrations/evimed/知识库检索API.md) | 上游契约参考 | 项目适配行为见 Agent 检索设计和当前代码 |
| [EviMed 语音与文档解析 API](./integrations/evimed/语音与文档解析API.md) | 上游原始资料 | 由独立服务直接提供，但作为 Shulan 对外能力已聚合进 `API.md` |

### 2.5 产品与合作方参考

| 文档 | 状态 | 说明 |
|---|---|---|
| [项目需求.md](./product/history/项目需求.md) | 原始产品输入 | 记录早期业务目标，不作为当前 API 或架构契约 |
| [健康分析生成接口需求](./product/health-analysis-requirements.md) | 2026-11-12 本地代码与模拟客户端已实现 | 继承此前问卷要求，明确三源、基础卷、10～20题与去重优先；本地模块汇总、最终复验、基础信息提供方和真实联调见 [I-047](../ISSUE.md#i-047-健康分析b0前置交接与公共纯读依赖) |
| [问卷系统 Schema](./product/survey-system-schema.md) | 需求方协议输入，2026-11-03 v1.0.0 | 定义分页问卷和答案形状，尚非当前 Record API |
| [整份建档问卷生产化](../wiki/record-questionnaire.md) | 开发分支实现，2026-11-05 源码说明，待真实验收 | 含[报告驱动的生成逻辑](../wiki/record-questionnaire.md#生成与来源边界)：证据输入、3＋7＋最多5题、健管师清单去重、发布校验与降级；另含生命周期、画像及 API Lab 接入 |
| [Record 建档问卷功能演变](./product/record-questionnaire-evolution.md) | 2026-11-04 历史与源码核查 | 记录 `b13e8af → 618e3d4` 的功能变化及当前实现边界 |
| [建档问卷需求与 Schema 对照](./product/record-questionnaire-requirements-analysis.md) | 2026-11-04 需求分析，待确认 | 新需求理解、当前能力差距、字段映射和关键决策，不代表已实施方案 |
| [灵犀树兰AI对接文档.md](./product/history/灵犀树兰AI对接文档.md) | 外部链接索引 | 原始合作方资料入口，不保证与当前实现同步 |

## 3. 2026-09-03 架构复核（历史）

本节保留当时的审计结论和待办背景，不作为当前实现状态。Report 及文件链路的最新状态见上方导航；具体剩余验收见当前 TODO/ISSUE。

本轮逐份核对 architecture 原有 14 份文档，新增统一状态导航，并区分当前实现、尚未实施方案和历史记录。重点纠正了旧 report_api 路径、Report 回调触发及 15/30 分钟时限、尚不存在的 Graph/Service/Worker、报告与画像来源混淆、卡片实时推送和部署等待预算等描述；未修改业务实现或 API 契约。

Report 目录迁移已经完成，但全局 AION 直连、只读画像接入及持久任务仍未完成。旧迁移手册不再作为当前权威实施入口；历史设计正文保留。完整逐份结论和源码入口见 [architecture/README.md](architecture/README.md)。

后续确认补充：五类最终报告保存成功后，用户需能在 Chat 原会话及新会话中追问。因此结果持久化、报告查询、finalReportId 和 Agent 只读报告上下文已加入当前交接必做项；报告为 AI 未核验分析，不自动变成健康事实。当前仅完成文档步骤，未修改 API.md 或业务实现；持久任务队列和医生审核回传仍单独跟踪。

### 3.1 2026-08-24 审计记录（历史）

以下保留上次审计的背景和当时问题，不作为当前未完成任务清单；旧 Report 手册的权威级别、路径和阶段结论已由本轮复核替代。未涉及的其他问题以当前 TODO/ISSUE 及源码复核为准。

当时对 `README.md`、`docs/`、`wiki/`、`TODO.md`、`ISSUE.md` 和路由/模块进行了交叉核对。

已确认并修正的滞后内容：

- README 目录树引用不存在的 `settings.py`、`services/report.py`、`prompts/report/`、`utils/background_task.py` 和 `project_docs/`。
- README 把外部语音 WebSocket `/realtime` 表述为当前 Tornado 服务路由。
- `TODO.md` 曾使用“Report 由其他团队负责，本次禁止修改”的阶段性旧约束；现已改为明确的独立负责人交接范围，但该范围不是永久架构豁免。
- Agent Wiki 仍把普通对话画像摄取描述为未实现，并把 Report 迁移绑定到旧的团队边界。
- 普通聊天 POC 评审仍标记为“等待 Agent Core 实施”，但生产调用链已经完成迁移。
- 健康记忆简版仍标记“事实保存边界待确认”，已经被完整画像设计和实现取代。
- 医生卡片设计仍标记为“第一版实施设计”，实际 Outbox 主链路已经实现。
- EviMed 检索和语音/文档解析原始资料曾与聚合 API 契约混放，现已迁入 `docs/integrations/evimed/`；语音与文档解析仍作为 Shulan 对外能力保留在当前 `API.md`。
- 非 Report Handler、Analysis/Chat/Record 应用实现、首批 ports/Adapter、组合根和 ResourceRegistry 已完成第一轮迁移；`server.py`、`services/` 仅保留兼容面。
- 24 条本地 Tornado 路由已完成逐组审计；当前合规状态、负责人和标准迁移步骤已汇总到 `api-implementation-compliance.md` 与 `wiki/api-architecture.md`。
- Report 的迁移基线、文档冲突、目标文件映射、可靠任务、后续四报告、分阶段 PR、切换回滚与完成定义已汇总到 `report-api-migration-guide.md`；当前已完成目录与路由切片。

仍需通过后续代码实施解决，而不是只改文档的问题：

- 应用级安全停服已实现，仍需 Linux 真实信号、长 SSE/报告和进程管理器配置验证。
- 最终报告已受理任务仍是进程内任务，异常退出不可恢复。
- Report 仍使用进程内 Pipeline，完整 `ReportService`、持久化任务和独立 Worker 尚未落地。
- 最终报告模型调用尚未统一进入 AgentHub。
- 最终报告空来源受理语义及 callback 对 3xx/普通 4xx 的重试与 API 文案不一致，已登记为 ISSUE I-024，需由 PR 0 characterization 与 ADR 裁决。
- Health Profile 内部 Repository/Worker 尚未按模块 ports/Adapter 结构拆分；当前只建立公共模块入口。
- Health 探针仍从 Tornado settings 读取运行时依赖；业务 Handler 的 DTO 和 HTTP/SSE 错误映射尚未统一。
- 新模块公共入口仍反向懒加载旧 `services.*`，`server.py` 动态 Handler 与业务 settings 兼容面尚未删除。
- Agent/画像长任务 lease 尚无通用自动续期，超过 TTL 时存在重复执行风险。

这些事项已分别写入 `TODO.md` 与 `ISSUE.md`，设计依据见本页“权威来源”。

## 4. 维护规则

1. 当前事实、目标设计和历史记录必须分开标注，不使用一份旧方案同时表达三种状态。
2. `docs/api/history/` 是只读版本快照；修正文案只改当前 `API.md` 并按发布流程生成新历史版本。
3. 架构文档头部必须包含状态和更新日期；实现完成后同步更新状态，而不是删除原设计背景。
4. 新增或移动生产模块时，同一变更必须更新 README 目录树、相关架构文档和静态架构测试。
5. 已发现但尚未处理的缺陷写入 `ISSUE.md`；已经确认要实施的工作写入 `TODO.md`。
6. 设计正文放在 `docs/architecture/`；Wiki 解释当前实现和操作方法，不代替目标设计。
7. 外部依赖原始文档统一存放在 `docs/integrations/<provider>/` 并保持原文；若相关能力属于 Shulan 对外 API，即使由独立服务直接提供，也必须在 `API.md` 保留聚合后的完整契约。
8. 每次发布 API 时复核当前版本号、更新时间、测试地址和 `api/history/` 快照。
9. 至少在重要里程碑或每季度执行一次文档—代码交叉审计，并更新本页的最近审计日期。

- [Document 当前基础信息提供方](./architecture/document-basic-info-v2.md)：固定归档、独立贡献、持久恢复和实际公开端口的离线组合。

健康分析六入参消费者及共享画像接线状态见[基础信息消费者说明](../wiki/health-analysis-basic-info.md)。
