# TODO

本文记录计划内任务；已发现问题见 `ISSUE.md`。Report 状态于 2026-10-10 按 `develop@c9b8988` 复核：已勾选的接线项表示源码已存在，真实模型、数据库、回调和 Linux 验收仍单独保留，不能据此宣称本轮重跑业务测试。

## 当前实施决策

- [I-049](ISSUE.md#i-049-体检报告写入画像与主体归属)：体检报告可保存待核实观察。空画像首报默认入本人，姓名及性别双项明确冲突时隔离至其他人。上线验收覆盖部分报告、归属隔离、去重及恢复。

- 双来源工作在 `feature/basic-info-integration` 统一交付。模块独立提交并合入该集成分支，在实际组合验收后发起单个到 `develop` 的 PR，由用户在 Git 服务器审核合入。下方旧分支的逐次合入记录用于追溯。

- 集成分支合入前审核的 R01–R10 均在本批处理。各项修复、合成回归和最终候选证据见 [I-048](ISSUE.md#i-048-双来源基础信息与共享profile交接)。平台复审、合入及真实服务验收继续单独跟踪。

- I-048：Document !135 和 Profile 显式退休 !136 已合入 `develop@e303ce8`。Report 在所属分支交付当前原件事实投影，合入后由 Document 固定实际执行输出及临床来源贡献。

- Analysis 合入前审核整改（2026-11-13）：R1–R4 全部纳入本批，分别覆盖临床递归择优、基础证据独立累计、容器主体歧义和真实归档阶段命名；[I-048](ISSUE.md#i-048-双来源基础信息与共享profile交接) 登记作者分工、原审核回应和新候选完整验收。平台关联及正式批准由用户处理。

- I-048（2026-11-13）：Profile、Analysis 公开端口及观察时间修复分别通过 !132、!133、!134 合入。Document 同步 `develop@a3ccf12`，通过实际 prepare/read、固定归档、持久投递和 Profile 当前选择复验日期闭环，完成后交用户审核。[实施计划](docs/architecture/basic-info-single-pipeline-plan.md)、[Document 说明](docs/architecture/document-basic-info-v2.md)。

- 暂停对 `aion` 的前瞻性上游协议扩展；现有文本调用保持兼容。2026-11-09 已确认按多智能体方案推进项目侧工具适配，优先复用锁定的 AION 0.6.0 原生工具循环，不预设升级上游。
- 普通聊天 LangGraph POC 第一版已完成；当时未实现工具调用循环。当前 Chat/Record 已有固定工作流的受控知识检索，不等同于通用工具循环。
- 只有 Shulan 出现当前 `aion` 无法满足的真实场景时，才恢复对应的 `aion` 增量设计。
- Web、业务模块与 Adapter 按 `docs/architecture/application-architecture.md` 小步迁移；不执行一次性目录重写。
- 正常生产停服统一使用 `SIGTERM` 无损排空；Report 已持久化受理并具备中断恢复，运行中的解析/模型不会因恢复重新调用，真实故障场景仍待部署验收。

## Analysis当前抽取与提供方交接（2026-11-13）

- 用户决策：没有生产数据；当前报告抽取不做历史缓存、旧执行器、旧格式或旧任务版本兼容，不给调用方v12/v13选择。此前旧版本分派要求被替代。
- 本次整改起点为主工作目录 `D:/996-work/shulan`、`refactor/analysis@03f3d95`，已引入 `develop@054a2fc`（!132）。Analysis 负责人 `/root/profile_plan_review` 只维护所属代码、测试及两份本域文档；主协调者 `/root` 维护共享文档并串行完成本地 Git 操作，不代改其他模块实现。
- 当前交付：CurrentHealthReportInterpreter、逐字段low/medium与证据、主体校验和两阶段预算；prepare_document不接revision/publish，固定不发布；read_prepared_round公开scope、固定绑定、完整parsed_document、interpretation与实际selection，纯读归档。
- 过渡边界：现用默认原算法、原prompt和真实身份暂留；不能给current贴旧标签或失败后回退。Document当前消费等前置适配用户合入后，Analysis与组合根在阶段5切默认并删除过渡业务实现/接线。目前不能宣称全项目已单算法或已默认接通。
- 实际交接：2026-11-13，Analysis 负责人 /root/profile_plan_review 与 Document 负责人 /root/document_provider 确认 prepare/read 和 PreparedDocumentRound 契约。早期以合成 Port 验证；当前 Document 使用已合入的公开端口复验，统一记录见 I-048。
- 本轮同步时先有 890 项相关回归通过；随后非作者复核发现跨片段和补核查会丢失合法冲突候选，列为本批阻断，修复和回归纳入本次交付。字段合并必须汇集有效值和有证据的候选，互斥时输出空值与待核实状态，继续保留临床结果；无效候选不能制造冲突。修复前全量已中止并保存为 `analysis-profile-merged-superseded-before-conflict-fix.*`，不作验收。最终完整回归及提交指纹以 `D:/996-work/shulan-test-artifacts/analysis-profile-merged-final.{log,xml,json}` 为准；本地非作者复核绑定同一候选。未进行真实数据库、模型或部署验收。

## 双来源基础信息（2026-11-12，前期分支记录）

本次按用户已批准的双来源计划实施。客户端明确上传固定为`very_high`；报告由AI逐字段判断`medium`或`low`，不再统一低档；无可信归属或互斥候选不猜值，历史`unknown`保持保护。画像统一选择，年龄遵守确认时点、报告时点及可信生日规则，不增加授权系统或强制重解析入口。

- [x] Profile提供方：`codex/profile-basic-info@ee77191`已完成版本化候选/来源选择、持久确认恢复、字段治理、生命周期与年龄读取，12433 passed / 275 skipped；等待用户先合入develop。
- [x] Document准备：`codex/document-basic-info@1ac54f1`已交付显式V2字段/临床独立贡献、归档及恢复，12440 passed / 275 skipped；实际Profile接线仍等待用户合入。
- [x] Analysis本域：`b5e5109`已快进回`refactor/analysis`，v13抽取/旧版恢复及12472 passed / 275 skipped完成；默认仍v12，切换等待实际提供方合入。
- [x] Health Analysis本域：在`feature/health-analysis`子分支完成六入参、移除额外授权、确认消费者、Runtime补偿入口与客户端；12698 passed / 275 skipped，真实接线仍等待Profile合入。
- [x] 首批四候选分别完成Ruff、完整pytest和非作者复核；结果各自绑定提交，不作为跨模块已集成证据。
- [x] 前期各模块实现已由统一集成分支汇总，最新交付状态查看本页的当前实施决策。

当前协作方式及模块交接见 [I-048](ISSUE.md#i-048-双来源基础信息与共享profile交接)。

## 健康分析生成（2026-11-11，三入参历史基线）

用户已授权按本分支的[需求](docs/product/health-analysis-requirements.md)和[实施计划](docs/architecture/health-analysis-implementation-plan.md)开发，并明确先完成模拟客户端及离线基础测试，真实在线测试目前无法提供。2026-11-12完整组合12658 passed / 275 skipped，页面113项及合成HTTP10场景通过；实际授权、生产容量、真实依赖质量与部署验收继续保留，见 [I-047](ISSUE.md#i-047-健康分析b0前置交接与公共纯读依赖)。

- [x] 核对现有问卷、Profile/Document、群聊工具、普通日志与预算边界，定位真实公共依赖缺口。
- [x] 冻结新域多卷输入协议，区分全部已问题和真实回答，保留未知、否认、选项映射与跨卷身份。
- [x] 声明可信授权端口及拒绝访问的缺省实现，提供允许、拒绝、依赖故障与取消的合成样例。
- [ ] B0：交接可信调用身份、用户群组关系验证和发送人映射；完成代表性实际依赖样例，定版容量、deadline及追溯保留清理规则。
- [x] B1代码：Profile/Document独立提供方分支交付完整原解读和独立贡献的纯读出口，本地组合验证。
- [x] B2—B4代码：三源快照、持久追溯、生活目录覆盖、循证匹配、生成、共享限流、Web日志策略、同步入口及默认装配。
- [x] API Lab及真实本机HTTP合成演示：基础卷、7/1/0题、三源、错误与持久追溯，浏览器答题及追加历史后去重。
- [x] 完成最终组合的Ruff、全量离线回归与验收记录，保留逐文件SHA256、JUnit及合成追溯。
- [x] 按用户本次Git约定，将模块成果本地提交并汇总到 `feature/health-analysis`，保留非作者复审及最终完整回归记录；由用户检查后自行创建develop PR。
- [ ] B5：旧链兼容、完整离线回归与真实合成资料的运营端等待链验收；按约定切换。

本任务Git流程由用户2026-11-12明确：任务分支内可自由创建模块分支、提交和本地合并，最终内容全部合回 `feature/health-analysis`；主智能体不推送、不创建PR、不合入develop。模块职责、交接、全量离线测试与非作者复审继续遵守贡献指南，真实在线测试和部署不在本次离线交付结论中。

## 体检报告解读职责改造（2026-11-11）

用户已验收需求和计划文档，并明确授权开始代码实施。按最新指示，各模块只能从当前任务分支 refactor/health-report-interpretation 创建新分支，并合回该任务分支；不得合入从 develop 创建的旧模块分支。最终由用户统一审核并合入 develop，流程及纠正记录见 I-046。

- [x] 明确 Analysis 仅负责文件到文本、LLM 文本到结构化数据，修订需求及实施计划，取消按调用方分流的功能实现。
- [x] 在 ISSUE I-046 登记 Analysis、Document、Profile 和应用编排的模块交接及验收边界。
- [x] 完成当前 Analysis 抽取与最终摘要结算，通过核心与完整离线回归。
- [x] 完成固定轮次归档、公共资料读取及 I-044 的同材料并发交付。
- [x] 完成 Profile 完整视图和 I-043 查询适配，不放宽事实准入。
- [x] 完成持久就绪后响应、后台交付、默认装配和消费者回归。
- [x] 完成非作者复核、各模块本地集成及组合离线验证，按实际实现同步 API 和运行文档。
- [ ] 在合成资料和实际依赖环境完成抽取质量、存储、画像及重启验收。

依据见 [需求](docs/product/health-report-interpretation-requirements.md)、[实施计划](docs/architecture/health-report-interpretation-implementation-plan.md) 和 [交接 I-046](ISSUE.md#i-046-analysis存储画像与应用编排分批交付)。代码实施按本次明确授权推进，勾选项仍不表示真实服务或部署已验收。

## 聊天记录查询工具（2026-11-10）

- [x] 将树兰群聊查询封装为共享只读工具 `query_chat_history`，提供公共查询协议、HTTP Adapter 和显式创建工厂。
- [x] 增加可选的 `SHULAN_CHAT_HISTORY_QUERY_URL` 完整地址配置，四份模板均留空；实现错误区分及离线测试场景。

该条记录2026-11-10备用工具交付。2026-11-11健康分析已显式消费，其他旧业务和全局默认工具注册保持原状；真实接口、身份映射和生产环境仍未验收。健康分析接入见I-047，配置与使用见 [Agent工具运行指南](wiki/agent-tool-runtime.md)。

## 多智能体分批重构（2026-11-09）

用户已确认实施[最新架构方案](docs/architecture/modular-multi-agent-architecture.md)阶段 0—6；阶段 7 的部署隔离不在本次范围。先保持行为完成封装，再按域启用自主循环；五个平级主 Agent、纯查询不启动模型、唯一权威画像和持续回调补投为验收约束。依赖和负责人见 ISSUE I-030— I-039；下列未勾选项均不代表已实现或已上线。

- [x] 阶段 0：将设计和实施登记纳入 refactor/agent，保存全量离线基线、模块负责人及兼容样例；基线 10423 passed、275 skipped，未执行真实服务验收。
- [x] 阶段 1：Document 交付显式版本选择与真实输入版本；Record 先封装现有输入；Report 在提供方合入后独立固定选择。旧版本不可用时明确失败，不隐式采用最新实现。
- [ ] 阶段 2：分离不可变材料/事实快照、消费者私有解读和唯一来源发布；引入独立 publicationRevision，兼容旧来源身份及撤回水位。
- [ ] 阶段 3：Profile 交付权威结构快照和幂等来源接收；消费者分别迁移 renderer，来源按范围逐批切换，影子路径不正式发布。
- [x] 阶段 4：Agent 平台交付工具协议、版本注册、预算/权限/取消与回执；Report 公开持久后续用例先独立交付，GutBarrier 再接入。
- [x] 阶段 5：按 Report、GutBarrier、Chat、Record、Questionnaire 顺序完成行为等价封装、受控工具和 RAG；先补后台恢复，再移除查询触发恢复。
- [x] 阶段 6：Report ResearchAgent 已交付并通过离线多轮补查、引用、预算、取消与重放验证；新研究路径默认关闭，真实模型质量评测与启用验收仍须单独完成。
- [ ] 每批完成改动文件 Ruff、完整 tests/unit 和非作者审核后合入 refactor/agent；同步 Wiki、架构状态及六张图，最终由用户提交 develop PR，真实模型/数据库/回调/Linux 验收单独记录。

本任务 Git 约定（用户 2026-11-09 明确指示）：从 refactor/agent 新建模块子分支，各自完成任务后合回该集成分支，最终由用户自行提交 PR 合入 develop。该约定仅覆盖本次任务的分支起点和集成顺序，模块职责、依赖交接、非作者审核及全量测试继续遵守贡献指南。已创建的两条分支保留独立成果，由主智能体串行调整到 refactor/agent 基线后集成。

执行基线：2026-11-09 已执行 git fetch origin develop，远端 develop 为 da08e5f0d4bddfc116da779a9e813bdeec81ad41；设计分支起始提交为 087df7d。第一批独立分支为 refactor/document-output-selection 和 refactor/record-document-context，均由该 develop 引用创建。原始任务内容与后续 PR、测试记录分别保留，不以新分支存在代表依赖已完成。

## 建档问卷循证与诱因核实（2026-11-06）

客户五步业务流程已确认，实施设计见 [建档问卷循证诱因分析与补充问题设计](docs/architecture/record-questionnaire-evidence-workflow.md)。当前仅完成需求与源码核查文档，以下能力尚未实现。

- [ ] 明确初始问卷、已有自述和健管师沟通记录的输入契约、归属及资料版本。
- [ ] 按五步流程接入自有 KnowledgeGateway，保存风险、循证候选诱因、个体匹配与待核实条件。
- [ ] 建立候选诱因与补充问题、选项答案的关联，避免重复询问已有信息。
- [ ] 提交后更新候选诱因判断，明确结果查询、版本、幂等与补偿；不以画像事实提取代替该步骤。
- [ ] 确定题量、补充轮次及结果消费契约后，同步当前 API、API Lab 和验收；保持历史提交和画像来源边界。
- [ ] 完成完整离线回归及真实知识服务、模型的合成资料验收。

## Record 问答质量改进（2026-09-15）

- [x] 1. 移除选项操作提示，清理后校验重复和空值，问题末尾统一提示文字/语音补充。
- [x] 2. 建立共享生成文本检测，先接入 Record；完整分析校验后展示，正式输出失败不推进会话。
- [x] 3. 固定补充问题和单个“没有了”选项，快捷结束走正常保存及幂等流程。
- [x] 4. 记录有来源的主诉、已知/未知信息及已问目的，在初始 3–5 次内兼顾相关背景，保留专科范围和 20 次总上限。
- [x] 5. 结构化生成承接与提问理由，对外继续使用 assistantMessage 字符串；更新测试页面和当前契约。
- [ ] 在部署新版后，使用合成报告和对话完成真实模型质量验收，并联调客户正式前端；用例见 [动态建档实现与升级](wiki/record-dynamic-flow.md)。

## P0：生产安全停服与架构治理

### 上传文件解析与画像来源统一（2026-09-08）

- [x] 第二阶段：共享文件内容身份、URL 完整下载后去重、documentId 主动引用、Mongo 执行权与恢复检查点、规范报告来源和别名关联。
- [x] 第二阶段：Analysis/Record 参数与响应接线；建档 documents、解析快照及完成/Chat 转换后的持久化。
- [x] Report 已接入文件引用、共享索引与专用 report_source 输出；请求级复用仍有同 URL 内容变更限制。
- [ ] 单独评审同 URL 内容变更时的 Report 请求复用协议，完成端到端验收；共享层去重不等同于每次批次重新下载。
- [ ] 在测试环境进行真实 MongoDB 多进程、DocMind/模型和三个 HTTP 入口的联合验收。

- [x] 保留 2026-09-08 三入口历史审计；随后 Report 已通过 Adapter 接入共享服务。
- [x] 为共享服务提供 `process_document`，保留原始 URL、完整 SDK 响应、实际模型输入/输出、结构化结果和画像任务引用；失败及重试独立留痕，大内容分块归档。
- [x] 保持 Analysis/Record 公开结果兼容，Record 附带会话及文件序号；通过无网络测试验证共享服务到画像指标的来源链。
- [x] Report 已通过共享服务处理上传/引用及会话回退，批次保存 documents 来源引用；质量与画像摄取限制见 [共享文件指南](docs/architecture/shared-document-ingestion-guide.md)。
- [ ] 在测试环境验收仅通过最终报告上传即可异步查询画像指标，并确认文本/扫描/混合 PDF、连续截图、故障和重试行为；新增代码未连接真实外部服务。
- [ ] 按实际需要单独设计历史原文补录、Analysis/Record 原文件二进制备份、超长文件分段提取、失败解析自动恢复以及来源删除/保留治理。

详细方案见 `docs/architecture/graceful-shutdown-and-deployment.md` 与 `docs/architecture/application-architecture.md`。

### 安全停服与发布

- [x] 引入应用生命周期状态 `STARTING / READY / DRAINING / STOPPED`，并注册 `SIGTERM/SIGINT`。
- [x] 收到停止信号后先切换 readiness、调用 `HTTPServer.stop()`，拒绝 keep-alive 连接上的新业务请求，再等待存量工作。
- [x] 统一追踪普通 HTTP/SSE、启动回调、最终报告任务与周期 Worker；正常停服只等待自然完成，不因固定 deadline 自动取消。
- [x] 停止 Outbox 周期调度；画像摄取 Worker 完成已领取任务后不再领取同批后续任务。
- [x] 拆分 `/health/live` 与 `/health/ready`，增加请求拒绝、任务自然排空和关闭顺序测试。
- [x] 建立共享 `ResourceRegistry`，支持同步/异步资源、逆序关闭、幂等关闭和多失败聚合；`AppContainer`/`ApplicationBundle` 共用 Registry，Bundle 登记其拥有的 AgentRuntime，组合根登记 Report Pipeline 的直接可关闭 Client。
- [ ] 在 Linux 使用真实 `SIGTERM` 验证长 HTTP、SSE、报告任务、Worker 和退出码。
- [ ] 配置 systemd 或容器，确保进程管理器不会在排空完成前发送 `SIGKILL`；按部署条件选择单实例排空升级或双实例滚动/蓝绿发布，并完成回滚演练。
- [ ] 修复 `loongspears.utils` 全局进程池关闭顺序，保证服务正常退出和 pytest 退出都不向已关闭日志流写入。

### Web 与应用分层

- [x] 建立 `web/application.py`、集中命名的 `web/routes.py`、统一 Handler 基类和 `web/handlers/`；`server.py` 已缩减为历史启动与测试导入兼容门面。
- [x] 机械搬移 Health、Analysis、Record、Chat、Health Profile Handler，并通过现有 HTTP/SSE 契约测试保持外部行为不变。
- [x] 把最终报告 Handler 迁入 `web/handlers/report.py`，通过 `web/routes.py` 显式注入 Pipeline、任务监督器和进程内请求协调器，并把生产依赖 builder 接入 `bootstrap/`。
- [x] 将原 `report_api` 业务与出站实现分别迁入 `modules/report/` 和 `adapters/report/`，删除旧目录与旧导入。当前 evaluate 为 success/partial_success 时进入后续四类报告处理，不因 evaluate 回调失败而阻断。
- [x] Report 六步源码接线已实施；真实环境验收见下方“最终报告 AI、存储与 Chat 追问”，不以源码接线替代交付验收。
- [ ] 基于现有 DurableReportService 继续收口稳定应用接口、完整模块资源契约及独立 Worker 进程；不重复创建持久任务仓库。
- [x] 建立 Report 端到端迁移草案；2026-09-03 已将 `docs/architecture/report-api-migration-guide.md` 标为历史文档，原 PR/ADR 清单不再直接执行。
- [x] 按 `6625647` 复核 `docs/architecture/` 全部 14 份原有文档，新增状态导航，更新已实现/待实施边界并同步关联入口；不代表完成待办业务功能或生产验收。
- [x] 将共享 session lease ID 纯函数迁入 `utils/identifiers.py`，将 Chat Outbox 实现迁入 `adapters/messaging/chat_outbox.py`；原 `services/` 文件仅保留弃用导入兼容。
- [x] 为 Analysis、Chat、Record 建立模块自有 `ports.py`；将 DocMind、Chat/Record MongoDB/Redis Session、Record PromptManager 和 Chat Outbox 的具体构造迁入 `adapters/`，由 `bootstrap/factory.py` 注入应用服务。
- [x] 建立路由、分层与统一 AI 入口静态门禁；源码扫描已包含 modules 和 adapters，本轮未重跑或宣称所有门禁通过。
- [x] 将 adapters 纳入统一 AI 入口扫描，Report 主叙事/follow-up 经 AgentHub；保持禁止移动文件绕过门禁。
- [x] 增加 `tests/unit/bootstrap/` 组合根构造测试与 `tests/unit/runtime/test_resources.py`，覆盖端口单次注入、共享 Registry、同步/异步逆序关闭和失败聚合。
- [ ] 由 Web/Runtime 负责人建立 RuntimeHealthService 与类型化 `ShulanApplication.runtime_context`，改造 `/health`、`/health/live`、`/health/ready` 和全部 Handler 的请求准入，删除固定延迟、lifecycle 字符串 settings、业务 settings 查找和架构测试豁免。
- [ ] 由各业务模块负责人提供稳定 Command/Query/Result/Error，Web 负责人建立统一 DTO 与 HTTP/SSE 错误映射，删除跨模块异常、异常类名字符串和逐 Handler 的宽泛 `Exception` 兼容。
- [ ] 由 Health Profile 负责人把顶层 `health_profile/` 的领域、应用、ports、Mongo/Job/Agent Adapter、Worker 和资源所有权迁入标准模块边界；迁移期只允许旧路径单向 re-export 新路径。
- [ ] 修正 `modules/{analysis,chat,record}/application` 反向懒加载旧 `services.*` 的兼容环；迁移旧契约测试和调用方后，删除 `server.py` 动态 Handler、非框架业务 settings 与无调用方 `services/` 别名。
- [ ] 在模块接口稳定后类型化 `AppContainer` 业务字段并建立 WorkerRegistry；补充禁止动态 Handler、业务 settings 和反向旧路径依赖的架构门禁。
- [ ] 由 Platform 负责人把稳定 Agent capability 标识提升到 `agents` 公共 facade；Analysis、Record、Chat 改用公共契约后，禁止应用层导入 `agents.graphs.*` 实现路径。

### 最终报告 AI、存储与 Chat 追问

六步源码接线已实施，五类产物保存后可查询和咨询，均保留 AI 未核验属性；独立报告记忆不自动回写健康事实。以下接线里程碑与真实环境验收分别记录，未来切片仍由维护者审核和手动提交。

- [x] 第 1 步：交接指南、验收矩阵及架构导航已建立，本次按当前源码同步状态。
- [x] 第 2 步：实现 report.narrative、固定 Prompt、兼容适配器与每报告 Factory，并立即接入主 Pipeline；保留 JSON、超时、重试、并发和业务校验。
- [x] 第 3 步：通过无状态 report.followup 和服务器任务允许列表统一四类后续报告的全部有效模型调用，迁移模板并清除直连。
- [x] 第 4 步：读取一次受控长期画像快照并映射进报告输入，验证主体、状态、来源、时间、预算及失败降级。
- [x] 第 5 步：独立持久化五类报告产物和批次，提供 finalReportId、列表/详情查询及回调引用，接入真实生成链路；先保存再发成功回调，重复提交幂等、不同内容不覆盖，失败不重跑 LLM。
- [x] 第 6 步：接入 report.retrieve_context 与 Chat 临时报告上下文，可选指定报告；原会话、新会话和完整/SSE 均支持追问，歧义澄清、跨用户隔离、12,000 字符预算及未核验属性有测试。
- [x] Report 查询、引用及 Chat 可选报告上下文已写入现行 API 与联调入口；本次状态同步不升级 API 版本。
- [ ] 在实际部署版本验证“生成 → 查询 → 选择 → 追问 → 同用户新会话再问”，覆盖完整/SSE 和多用户隔离。
- [ ] 完成已保存结果的重启读取、回调失败仍可咨询、保存失败不发布成功、版本隔离及旧客户端兼容验收，关闭 I-025。
- [ ] 后续单独设计最终报告删除与保留治理、历史数据受控补录及医生核验/修订回传；首版仅保存新报告、长期保留且无自动过期，不自动提取报告健康事实。

### 最终报告可靠任务

Report 批次、产物与回调交付状态均已持久化，生成与回调 Worker 在 Web 进程内运行。已有源码覆盖受理、续租、中断补齐失败结果及补投，真实 MongoDB/强退验证仍保留，不能仅由结果落库关闭 I-020。

- [x] 对齐最终报告预览、回调 Schema 与当前 API/前端契约，解决 ISSUE I-023 的既有测试失败。
- [ ] 基于最新源码与 API 补齐空来源、嵌套 extra、callback 3xx/普通 4xx/429/5xx/传输错误的回归矩阵；I-024 的旧偏差前提已变化，不沿用旧 PR 0/ADR 强制顺序，不在重构中顺手改变行为。
- [x] `/api/v1/report` 在返回 processing 前确认持久化受理，并按规范化请求指纹复用批次。
- [x] 分离生成与交付状态，接入原子领取、租约续期、中断恢复、补偿及人工 redrive；回调持续退避补投，没有尝试上限、死信终止或 TTL。
- [ ] 如需改变重试或终止策略，另行完成业务/API 与运维评审，不照搬旧迁移手册的状态草案。
- [x] 按产物建立幂等交付记录和稳定 Idempotency-Key；向外增加事件字段仍必须先经过 API 评审。
- [ ] 长期将 Report Worker 与 Tornado Web 进程分离，使 Web 发布不等待分钟级报告任务。
- [ ] 覆盖 `SIGKILL` 后新实例恢复、重复领取、租约过期、重复回调和旧任务版本兼容测试。

## 已完成里程碑：普通对话 LangGraph POC

- [x] 为普通对话完整输出、SSE、历史和推荐问题建立无网络 Handler 契约测试。
- [x] 引入 LangGraph，先使用 InMemorySaver 与 fake model 验证最小图，并固定通过验证的依赖版本。
- [x] 使用当前 `aion` 字符串接口实现统一模型节点，并通过 AION Context 隔离和恢复模型会话。
- [x] 保持现有 Tornado API 契约并直接完成 LangGraph 切换，不保留临时 `legacy` 运行路径。
- [x] 接入官方 MongoDB checkpointer，并完成独立集合、索引和 Runtime 生命周期测试。
- [ ] 在 Linux 集成环境验证服务重启后的 checkpoint 恢复。
- [x] 使用 Redis 共享业务 session lease 串行化 Chat 与 Record 的同会话操作。
- [x] 验证 SSE 断连后继续生成并保存完整回复，以及同会话并发拒绝行为。
- [ ] 在 Linux 环境完成 Agent SSE、MongoDB checkpointer 和 Redis lease 的并发负载验证。
- [x] POC 只预留健康记忆接口，不自动写入长期画像。

暂缓项：多消息原生协议、工具调用循环、模型能力描述与 `aion` 工厂。相关探索保留在 `docs/architecture/aion-agent-protocol.md`，不作为当前 POC 前置条件。

## P1：Agent 基础能力

- [x] 使用 LangGraph 作为核心编排与状态运行时，坚持“确定性工作流 + 局部受控 Agent”。
- [ ] 项目单次运行只启用一个模型提供商，但允许通过配置整体切换 DeepSeek、Qwen 或后续提供商。
- [ ] 使用 Pydantic 定义 Agent 状态、工具输入和结构化模型输出。
- [ ] 建立工具权限、超时、重试、幂等、最大循环次数和审计机制。
- [ ] 建立 Prompt、模型、工作流版本以及 Token、延迟和异常指标。
- [ ] 对日志中的请求、响应、reasoning 和健康信息进行脱敏。

## P2：记忆与数据

- [ ] 按会话/交互、用户画像、用户报告三个领域拆分 AI 数据。
  - [x] 建立用户画像独立领域模型和五个 MongoDB 集合骨架，与来源数据分离。
  - [x] 实现用户画像读取、稳定健康主体列表及幂等新增/乐观并发更新接口。
  - [x] 通过 `profile.retrieve_evidence` 和统一 `KnowledgeGateway` 提供第一版按需循证检索。
  - [x] 实现健康事实分页查询、待确认事实处理、单条事实删除和变更审计接口。
  - [x] 事实修改使用事实/画像双版本、`requestId` 和主体级 Redis lease，并支持中断后幂等恢复。
  - [x] 删除事实时立即清除健康值并隐藏正常读取，仅保留非敏感墓碑和审计事件。
  - [x] 将体检报告改为按稳定 `reportId` 追加保存；建档只解析创建请求的 `fileList`，并固定为当前 QA 会话快照，不再读取用户最近上传的独立报告。
  - [x] 体检报告保存后幂等创建画像摄取任务，投递失败保留待补偿状态。
  - [x] 实现首版摄取 Worker，将白名单检验指标写入事实、审计事件和画像投影。
  - [x] 增加报告 `profileIngestion=pending` 补偿扫描器，恢复来源保存成功但任务投递失败的记录。
  - [ ] 基于真实报告样本扩充指标别名、标准单位换算及影像/综合体检候选策略。
  - [x] 已完成建档问答持久化后幂等创建画像任务，QA 转为 Chat 时保留建档来源与投递状态。
  - [x] 建档创建读取当前长期画像；院内、微信运动和智能设备数据参与问题生成及受控画像候选，医生产品不作为健康事实，报告继续使用独立摄取链路。
  - [x] QA→Chat 使用有总预算的建档交接摘要继承本次报告、附带数据和建档问答，保证画像异步摄取尚未完成时同一会话仍可立即连续；新会话继续只读取长期画像。
  - [ ] 基于真实院内、微信运动和智能设备样本扩展步数、心率、血压等事实类型与单位治理。
  - [x] Worker 按已注册来源类型领取任务，并将报告、建档任务路由到独立处理器。
  - [x] 实现建档基本信息确定性映射和无状态 Agent 候选提取，动态回答默认以 pending 写入本人画像。
  - [x] 增加建档 `qaProfileIngestion=pending` 补偿扫描器，恢复快照保存成功但任务投递失败的记录。
  - [x] 为新 Chat 消息增加稳定 ID，整轮保存后幂等投递单条用户消息画像任务。
  - [x] 通过 `profile.extract_chat_candidates` 仅提取用户原文，复用主体解析并以 pending 写入事实。
  - [x] 普通对话按用户跨会话读取受控画像记忆；区分 active/pending/disputed，仅允许血亲家族史影响本人回答，并保证摘要不进入 AION 历史、业务消息或 LangGraph checkpoint。
  - [x] 增加聊天 `chatProfileIngestion=pending` 补偿扫描器，恢复消息已保存但任务投递失败的记录。
  - [x] 增加报告、建档、Chat 摄取到新会话画像记忆的无网络闭环测试和真实 API 人工验收流程。
  - [x] 在健康画像读取响应中增加按类别组织的当前事实快照，完整展示 active、pending 和 disputed 状态，同时保持正式概览、检验趋势、摘要、提醒和循证仅使用 active。
  - [x] 增量扩展标准检验和跨类型报告逐项结果摄取，指标区展示身体测量、检验、菌群、报告评估及未标准化结果；保留来源和定性原文，严格限制趋势可比性，不扩大问答自动确认。
  - [x] 保存摄取任务处理计数和稳定原因码，增加只读 inspect，重驱审计保留上一执行摘要。
  - [ ] 授权后核实历史报告主体并受控恢复摄取；对原解读缺少逐项结果的报告规划重新解析及来源修订，不能仅重驱任务或伪造用户确认。
  - [x] 共享层实现全文分段与区间留痕，保留多日期和混合类型，失败后恢复成功段；无日期来源指标可查询。
  - [x] 支持单条来源中同一主体、同一事实类型的多个不同值，并幂等去除完全重复候选。
  - [ ] 处理依赖前文的短回答、临时事实自动失效以及纠正/停药等跨事实冲突。
- [x] 建立健康事实主体模型，区分本人、血亲、非血亲家属、朋友、其他和未知主体。
- [x] 对父母及明确父系/母系祖父母按规范关系跨会话复用稳定 `subjectId`。
- [ ] 持久化兄弟姐妹、子女、配偶、朋友等歧义主体候选，并由用户确认后绑定稳定 `subjectId`。
- [ ] 将记忆更新拆分为 AI 候选事实、主体解析、保存策略、冲突检测和受控提交。
  - [x] 建立严格 `MemoryCandidate` 和确定性保存策略，模型不能直接写入正式画像。
  - [x] 建立持久化摄取任务的幂等、租约、重试、完成和死信生命周期。
  - [x] 增加画像摄取终态任务查询、logger 告警和受控人工 redrive；修复永久错误前不自动重放死信。
  - [ ] 将画像摄取 `dead_letter` 错误日志接入生产告警平台，并制定响应时限与值班流程。
  - [x] 首版报告摄取已接入事实事件写入和画像投影重建。
  - [x] 独立报告接口未指定主体时创建或复用本人主体；历史空主体报告不回填，误投递任务安全完成。
  - [ ] 将已完成的建档单一亲属解析扩展为跨来源主体解析，并补齐单位归一化和冲突检测。
- [x] 建立版本化健康事实类型注册表；边界未确认的类型不自动进入长期画像。
- [x] 保留事实确认、更正、拒绝和删除历史，公开审计不返回健康值或模型 reasoning。
- [ ] 补充事实自动失效、冲突合并及主体合并/拆分历史。
- [ ] 保留未完成建档问答的 Redis 两小时 TTL 产品策略。

## P3：后续业务迁移

- [x] 支持医生签发内容以 `custom` 业务消息插入同一对话流，默认不进入 AI 上下文；详见 `docs/architecture/chat-doctor-signed-card.md`。
- [x] 使用 MongoDB Outbox 持久化 AI 输出期间收到的 `custom` 消息；返回 `202 queued`，在完整 AI 回复保存并释放 Session lease 后顺序投递，并通过周期任务恢复重启遗留事件。
- [ ] 确定 `custom` Outbox 中 `delivered/failed` 事件的保留、归档、告警和人工补偿策略；当前保留记录以维持长期 `eventId` 幂等审计。
- [ ] （暂缓）为 `custom` 业务消息增加会话事件 SSE 或 WebSocket；当前采用前台轮询与页面恢复时同步历史，详见 `docs/architecture/chat-doctor-signed-card.md` 第 11 节。
- [ ] 将健康建档问答迁移为显式状态图。
- [x] Report 已接入持久化受理、并发生成、产物保存及中断终态恢复；进一步拆分 Pipeline 和独立 Worker 仍见上方待办。
- [ ] 将体检报告解读迁移为解析、抽取、校验、归一化和保存工作流。
- [x] 抽象 `KnowledgeGateway`，接入 EviMed AI 检索 API V2；`chat.reply` 和 `record.question` 已实现受控按需检索与失败降级，并区分合法空结果 `empty` 与服务不可用 `unavailable`。
- [x] 检索资料仅通过 `supplemental_context` 用于当前轮，AION Context 仅保存原始用户输入和最终回答，不持久化检索全文。
- [ ] 建立知识检索评估集、独立重排、医学证据等级和结构化引用输出；向量检索等其他知识源后续通过同一 Gateway 扩展。

## 建档完成与最终报告协作方案

> 以下保留早期显式完成建档的方案比较与当时清单，不是当前 API 契约或新增实施要求。当前建档自动完成，`/qa/complete` 只读取已完成结果，不能结束活动会话；Report 已持久化受理。当前行为以 API.md、动态建档 Wiki 和上方 Report 待办为准。

当前约束：建档未显式完成前只存 Redis（TTL 两小时），超时即放弃；`/qa/complete` 完成后才持久化到 MongoDB 并允许同一 `sessionId` 进入 Chat。`/api/v1/report` 已迁入标准目录，但仍使用进程内 Pipeline；后续可靠任务改造必须保持现有请求、立即响应和终态回调契约。

| 方案 | 调用方式 | 一致性与可靠性 | 实施成本 | 结论 |
| --- | --- | --- | --- | --- |
| 1. 前端串行双请求 | 先 `/qa/complete`，成功后 `/report` | 避免报告早于建档落库；两个请求间仍可能因断网形成“建档完成但报告未请求” | 低 | **本次先实施** |
| 2. 完成令牌 + 串行双请求 | `/qa/complete` 返回 `completionId`，`/report` 必须携带 | 报告绑定不可变完成快照，便于幂等与审计；仍有前端调用间隙 | 中 | 可作为方案1增强 |
| 3. QA 完成事件 + Outbox | `/qa/complete` 同事务写完成快照和 Outbox，消费者创建报告任务 | 语义清晰、可重试、可恢复，最终一致性最好 | 中高 | **长期推荐方案** |
| 4. 业务编排接口 | `/health-assessment/complete` 编排 QA 完成和报告任务 | 一个业务命令覆盖跨域流程，调用简单；需新增编排层 | 中 | 适合业务边界稳定后 |
| 5. 预注册报告任务 | 前端可并发请求；报告任务处于 `waiting_for_qa`，待完成后执行 | 支持乱序和并发，但状态、超时、补偿复杂，当前收益有限 | 高 | 暂不推荐 |
| 6. `/report` 隐式结束 QA | 报告请求同时完成建档 | 请求少，但 Report 与 Record 强耦合、权限和失败语义混乱 | 低表面/高维护 | 不推荐 |

推荐方案为“QA 完成事件 + Transactional Outbox”：`/qa/complete` 只表达建档完成，报告是 `qa.completed` 事件的下游结果；报告失败不回滚建档，也不阻塞 Chat。若 MongoDB 部署不支持事务，需要使用幂等写入顺序、补偿扫描或独立持久化消息设施实现同等保障。

### 方案1当前实施范围

- [x] 增加 `/api/v1/qa/complete`，仅允许 `awaiting_decision` 状态显式完成；continue/complete 使用 `round` 防止过期命令重放。
- [x] 每轮最后一题返回 `awaiting_decision`，不写 MongoDB；继续问答仍复用 Redis 两小时 TTL。
- [x] Record 与 Chat 共享 session 门禁/lease，未完成问答不能被同 session 的 Chat 接管。
- [x] `/qa/complete` 成功后写入 MongoDB 完成快照，再删除 Redis 临时状态。
- [ ] 前端必须在 `/qa/complete` 成功后串行调用 `/api/v1/report`；失败时提供“重新生成报告”入口。

### QA—Report 后续增强候选（不属于当前架构迁移的既定 API 契约）

- [ ] 经产品与 API 版本评审后，再决定 `/report` 是否必须校验 `(userId, sessionId)` 对应 QA 已显式完成；当前契约继续允许会话查询失败或不存在时使用空上下文生成。
- [ ] 明确读取完整 `qaHistory`、`extractedInfo`、`healthData` 等完成快照，且不得漏掉第一道正式问题。
- [ ] 在已有进程内请求指纹去重基础上，重新评审持久化幂等边界；旧 Report 手册第 10.3 节仅供背景参考，当前请求没有 `completionId/requestId`，不得未经评审直接把不存在的字段写入唯一键。
- [ ] 将内部生成与投递状态持久化为 `pending/running/retry_wait/generated/dead_letter` 和 `not_ready/pending/delivering/retry_wait/delivered/dead_letter`；对外 `processing/success/partial_success/failed` 不作为内部 job 状态。
- [ ] 先提供内部任务查询、重试和 redrive 能力，覆盖补偿场景；新增公开 Tornado 路由必须另行完成 API 版本评审。
- [ ] 后续若采用推荐方案，增加 `qa.completed` Outbox 消费者，并保证消费和报告任务创建幂等。

## 工程整理

- [ ] `config/question_topics.json` 作为 AI 服务模板，应迁移到更合适的位置。
- [ ] 生产系统为 Linux，Python 环境为 `conda activate shulan`；操作系统强绑定设计必须先讨论。

## 架构设计

- [x] 将全部公开路由统一注册到 `web/routes.py`，报告 Handler 固定放在 `web/handlers/report.py`。
- [x] 将报告领域实现迁移到 `modules/report/`，报告外部服务实现迁移到 `adapters/report/`。
- [x] 将分散的自动化测试和真实接口测试统一迁移到 `tests/unit/`、`tests/api/`，并建立测试规范。

## 功能重复

- [x] Report 上传文件已通过共享文档服务处理，原始可信观测可受控摄取；本地表格/临床提取保留为专用输出，来源质量、同 URL 复用限制及真实验收见 I-027。

## Document 当前基础信息提供方（2026-11-13）

- [x] 在 Profile R4 候选中同步 !135，用实际 Document 和 Profile 验证发布、回选、重放和后续更正。候选验收证据见 [I-048](ISSUE.md#i-048-双来源基础信息与共享profile交接)。
- [x] 本域一个当前准备、独立字段/临床贡献、固定 epoch 和持久恢复，见[实施说明](docs/architecture/document-basic-info-v2.md)。
- [x] 删除本轮双版本读取、旧贡献 carry 和历史 absence/drain 授权门禁；公共端口不选择 AI 修订。
- [x] Profile !132、Analysis !133 已合入。已用实际公开服务组合验证固定归档、持久发布和画像应用，外部 I/O 使用合成替身。
- [x] Analysis 日期修复经 !134 合入 `develop@a3ccf12`，Document 同步并复验日期链路。合并候选的完整离线记录见 [I-048](ISSUE.md#i-048-双来源基础信息与共享profile交接)。
- [x] Report 所属分支提供当前原件事实投影及 !137 R1–R5 整改，专项、完整验收及 Document 交接见 [I-048](ISSUE.md#i-048-双来源基础信息与共享profile交接)。
- [x] 在 Document 显式工厂接入 !137 的实际原件投影，提供固定保存、纯读、临床贡献发布及恢复。
- [x] Document 投影复审覆盖无变化水位、较新轮次恢复旧临床内容、活动组续页扩展、定量值、退休逐条筛选及共享测试夹具。验证与交接见 [I-048](ISSUE.md#i-048-双来源基础信息与共享profile交接)。
- [x] Report 与 Record 接入当前固定投影和贡献命令。合成组合覆盖不同发布及撤回顺序，同一原件观察只应用一次，独立的同值位置分别保留。
- [x] 集成分支切换生产组合根，Analysis、Report 与 Record 默认使用当前准备和发布。历史归档读取及显式注入继续按原边界。
