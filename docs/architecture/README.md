# 架构文档状态与当前实现基线

> 状态：当前架构导航与源码核验记录
>
> 更新日期：2026-11-10；2026-10-10 及 2026-09-03 的旧基线说明保留为历史
>
> 本次重构基线：`refactor/agent` 阶段 0—6 本地实现完成；最后代码候选完整离线测试 12228 passed / 275 skipped，最终提交组合重验与各批记录见 [ISSUE](../../ISSUE.md)。不代表真实服务或生产环境验收

## 1. 使用规则

2026-11-13，Profile 与 Analysis 公开端口已通过 !132、!133 合入，观察时间修复经 !134 合入。Document、Report 与 Record 的当前接线和默认切换已在统一集成分支合成复验，健康分析复用同一 Profile 基础信息确认。后续发布状态见 [单流程收敛计划](basic-info-single-pipeline-plan.md)。

对外行为以 [API.md](../api/API.md) 为准，已实现能力以当前源码和对应测试为准。架构文档中的“目标”“建议”“待实施”不能作为现成 Python 接口或已经具备的可靠性承诺。历史文档保留原设计以便追溯，不按旧目录、旧依赖版本或旧 PR 顺序重新实施。

当前状态表列出本目录的架构设计、实施说明、建议稿和历史验证记录。本轮核对模块归属、Agent/画像入口、任务与产物存储及路由；未执行真实模型、数据库、Linux 信号或容量测试。

## 2. 逐份状态

共享文件处理的当前入口为 [统一解析与留痕指南](shared-document-ingestion-guide.md)，Analysis、Record、Report 均已接线。Report 的 Agent、画像读取、产物查询和 Chat 追问，以及持久化受理、生成中断恢复和回调 Worker 均已存在；源码具备接线不等同于部署验收完成。

2026-09-17 建档专项更新见 [动态建档实现与升级](../../wiki/record-dynamic-flow.md)：当前分支支持多项困扰分别记录信息与选题，初始约 5 次实质交互为软目标，首轮最多成功处理 10 次主动问题回答，随后先邀请补充，不再回到主动建档。全程 20 次实质交互为硬上限；第 20 次答复全部待回应及本次新增疑问后结束。外围 `qaFlow.version=2` 保持兼容，旧会话下一次成功续答时适用首轮上限，既有问题和历史保留。该源码更新不表示真实模型或部署环境已完成验收，下方历史核验记录不作为当前建档规则。

| 文档 | 状态 | 适用范围 |
|---|---|---|
| [应用架构](application-architecture.md) | 当前分层规则；部分目标待实施 | Report 持久服务与进程内 Worker 已接线；模块资源契约、独立进程及兼容收口仍待完成 |
| [API 实现合规指南](api-implementation-compliance.md) | 历史28路由核查与分层要求 | 当前32个路由模式、33个方法入口；健康分析增量见I-047，其余归属见API及多智能体方案历史入口表 |
| [Agent 目标架构](agent-target-architecture.md) | 早期目标，部分说明已由多智能体方案更新 | 当前五域主 Agent 与 AION 0.6.0 有界工具循环见多智能体方案；原生循环不承诺逐步 checkpoint，通用高级压缩仍另行评估 |
| [Agent 工具运行指南](../../wiki/agent-tool-runtime.md) | 受控工具框架与健康分析群聊接入已实现 | 健康分析通过显式工厂消费群聊工具；旧业务与全局默认注册保持原状，真实接口待验收 |
| [业务域多智能体与模块解耦](modular-multi-agent-architecture.md) | 2026-11-10 阶段 0—6 本地实现已完成，真实环境未验收 | 五个平级主 Agent、纯查询旁路、Record/Report 固定文档消费、持续回调与公开 Report 委派已接通；研究循环默认关闭，Report/Chat/Record 画像上下文已独立接通，默认来源按域接通，历史来源不自动迁移；六张图和 31 条路由映射 |
| [最终报告 Agent 接入指南](report-agent-integration-guide.md) | 源码已实施，真实环境待验收 | 模型、画像、五类产物/查询、Chat 追问及持久化受理、恢复与回调 |
| [上传文件统一解析与留痕](shared-document-ingestion-guide.md) | 三个入口已接入 | Analysis、Record、Report 的文件内容身份、引用、执行协调、质量与画像来源 |
| [体检报告解读职责改造](health-report-interpretation-implementation-plan.md) | 2026-11-11 本地分模块实现，尚未部署 | Analysis 两步处理与存储、应用编排、画像各自职责；[当前实现](../../wiki/health-report-interpretation.md)及 I-046 记录组合验证状态，真实依赖尚未验收 |
| [健康分析生成接口实现计划](health-analysis-implementation-plan.md) | 2026-11-12 本地代码与模拟客户端已实现，真实联调未验收 | [需求](../product/health-analysis-requirements.md)覆盖原问卷继承与调整；首轮组合12658 passed / 275 skipped，本地模块汇总、最终复验及本轮提供方门槛见 [I-047](../../ISSUE.md#i-047-健康分析b0前置交接与公共纯读依赖) |
| [Report 端到端迁移旧手册](report-api-migration-guide.md) | 历史草案，执行基线已过期 | 保留长期任务可靠化的设计背景；原 PR 0—8、旧目录和旧行为不可直接执行 |
| [安全停服与发布](graceful-shutdown-and-deployment.md) | 排空与持久恢复已接线，部署验证待完成 | 正常排空和异常恢复分别验证；中断恢复不重跑模型 |
| [知识库检索](agent-knowledge-retrieval.md) | 首版已实现 | Chat/Record 按需检索和画像循证；不是通用模型工具循环或报告循证替换方案 |
| [建档问卷循证诱因分析](record-questionnaire-evidence-workflow.md) | 2026-11-06 需求已确认，方案待实施 | 五步分析流程、知识库接入现状、答案后更新诱因判断及接口待定项 |
| [建档问卷因素术语与工具调研](record-questionnaire-factor-terminology.md) | 2026-11-06 调研与设计建议，待实施 | 区分医学术语、行业工具公开做法和项目建议；受控概念、关系、个人证据及表达验收 |
| [用户健康画像](user-health-profile-design.md) | 首版数据闭环与循证已实现，扩展待实施 | 包含 Report 原始文件来源；AI 生成产物不自动写成健康事实 |
| [Profile 原报告公共纯读](../../wiki/profile-report-materials.md) | 2026-11-11 本地提供方增量，真实依赖待验收 | 既有本人原解读及独立贡献，固定身份、生命周期、镜像与限制；不创建主体或发布画像事实 |
| [医生卡片当前设计](chat-doctor-signed-card.md) | 持久 Outbox 已实现，实时推送暂缓 | `custom` 卡片排队、历史展示与模型隔离；长 SSE/WebSocket 仅为备选 |
| [模型上下文所有权重评](agent-context-ownership-reassessment.md) | Deferred，维持现状 | 继续 AION Context + LangGraph checkpoint 分工；触发条件满足后重评 |
| [普通聊天 POC 评审](chat-langgraph-poc-review.md) | 已完成的历史评审 | 保留当时环境、代码基线及实施顺序；当前入口见 Agent Wiki |
| [AION 工具协议设想](aion-agent-protocol.md) | 已归档 | 不要求扩展 AION；当出现真实协议缺口时另行逐项评审 |
| [医生卡片早期草案](chat-doctor-signed-card-draft.md) | 已被当前设计取代 | 仅保留需求形成记录，不能据此实现旧字段或旧并发行为 |
| [健康记忆早期摘要](health-memory-design.md) | 已被完整画像方案取代 | 原则背景；主体合并、工具类名称等示意不表示已实现 |
| [SSE 文本合并](sse-text-batching.md) | 当前实现说明 | Chat/Record 文本合并、配置及事件顺序约束 |
| [建档创建恢复](qa-creation-recovery.md) | 当前实现与运维 | 持久创建任务、文件检查点、状态查询与受控恢复 |
| [LLM 联网搜索参数](llm-web-search.md) | 当前网关参数与上游限制 | 共享网关转发与固定 AION 版本支持范围，不承诺所有提供商可用 |
| [文件恢复验证](analysis-recovery-validation-2026-09-16.md) | 2026-09-16 专项验证记录 | v6/v7 修复、离线复算和真实验收入口，不代表后续版本上线验收 |
| [文件首轮稳定性验证](analysis-first-pass-validation-2026-09-16.md) | 已被后续修复替代的历史记录 | v5 诊断前验证，当前实现见共享文件指南及恢复验证 |
| [AION DocMind 建议](aion-docmind-client-proposal.md) | 上游建议稿 | 等待上游评审，不作为已发布接口或项目现成能力 |

## 3. 2026-09-03 源码核验记录（历史）

- `web/routes.py` 集中注册 24 条本地路由。Report 路由名为 `report.submit`，实现位于 `web/handlers/report.py`，显式接收 Pipeline、TaskSupervisor 和 ReportTaskCoordinator；`report_api/` 已删除。
- Analysis、Record、Chat 的实际用例位于 `modules/*/application/service.py`，旧 `services/` 仍是兼容入口。部分新门面仍反向懒加载旧入口；Health Profile 通过 `modules/health_profile/application` 转发顶层 `health_profile/`，不能标为分层收口已完成。
- `bootstrap/factory.py` 是组装入口；Report 具体依赖由 `adapters/report/factory.py` 构造。`runtime/application.py` 管理 HTTPServer、周期任务和排空，`ResourceRegistry` 管理登记资源的关闭。独立 WorkerRegistry 尚未实现。
- `agents/runtime/application.py` 已注册 Chat、Record、Analysis、画像候选提取和画像循证能力；尚无 `report.narrative` 或 follow-up Graph。AgentHub 不会仅凭 `userId` 自动读取健康画像。
- Chat 通过只读画像 Provider 获取长期事实，再经 `AgentRunContext.health_profile_context` 注入本轮模型输入。Record 在创建时固定当前画像与本次附带资料。画像摄取异步完成，不保证新会话立刻读取尚未落库的事实。
- 独立体检报告解读默认绑定本人；QA 仅解析本次请求的 `fileList`，不隐式读取用户最近一份独立报告。动态建档初始 3–5 次实质交互，再补充答疑，全程最多 20 次并自动完成；升级时旧活动问答需重开，保留已完成历史。见 [动态建档实现](../../wiki/record-dynamic-flow.md)。
- 医生卡片先写 MongoDB Outbox，AI 忙碌时排队，随后写入业务历史；不存在会话级长 SSE 路由。客户端通过历史同步发现卡片，`custom` 不进入模型历史或画像提取。
- 最终报告 `evaluate` 解析和生成硬上限为 30 分钟，后续四份报告不共享该总时限。`evaluate` 回调失败不阻断成功/部分成功结果的后续生成；Report 结果当前不保存到 MongoDB、Redis 或项目目录。
- 最终报告的任务合并及完成结果短期复用由进程内 ReportTaskCoordinator 提供；它不是持久任务队列。主叙事适配器与 `modules/report/reports.py` 中的模型直连尚未收口，不能宣称项目级统一 AI 入口已经验收通过。
- 日志入口为 `utils.log.getLogger(__name__)`，内部委托 `loongspears.utils.log`，级别来自 `config.env.LOG_LEVEL`。新实现不得输出密钥或健康正文；现有请求/响应日志的全量脱敏仍是待办，不能宣称已经完成。

## 4. 阅读与实施顺序

新开发者先读 [代码导航与架构阅读指南](../../wiki/code-navigation.md)，按目录职责和具体 HTTP 请求定位当前代码；该指南解释现有实现，不替代本目录的目标方案或历史记录。

先阅读应用分层规则和 API 合规矩阵，再按业务阅读当前专项指南。Report 维护从 [完整接入指南](report-agent-integration-guide.md) 的当前入口、验收矩阵和第 14 节持久化机制开始；文件来源从共享解析指南开始。不要按历史六步计划重复创建 Graph、存储或查询入口。

后续改造仍需人工审核和真实环境验收。独立 Worker 进程、通用 WorkerRegistry、Linux 部署、安全认证、医生核验回传及删除/保留治理分别跟踪；第 3 节保留的旧路径、24 条路由和未实施结论不再描述当前源码。

- [Document 当前基础信息](document-basic-info-v2.md)：当前归档及来源提供方，连接已合入的 Analysis 与 Profile 端口。

健康分析六入参消费者及共享画像接线状态见[基础信息消费者说明](../../wiki/health-analysis-basic-info.md)。
