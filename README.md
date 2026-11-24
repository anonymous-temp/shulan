# 树兰健康 AI API 服务

---

## 项目简介

树兰健康 AI 服务是一套基于大语言模型的健康管理后端接口，围绕用户体检数据提供完整的健康评估链路：

1. **体检报告解读** —— 上传体检 / 检验 / 影像报告，自动识别报告类型并抽取结构化指标。
2. **健康问答建档** —— 新会话以10–15题了解主诉、疾病史、血亲家族史及相关生活习惯，从五大类24个受控细项选题，优先真实相关性并交错大类。每轮用简短分析自然承接，随后直接简答已有疑问并邀请提问，具体答疑最多5轮后自动结束；旧会话保留原题数预算和已发布问题。
3. **AI 健康对话** —— 建档完成后，用户可与健康助理进行自由的健康咨询对话。
4. **健康报告生成** —— 综合问答建档数据，生成包含评分、诊断、饮食 / 运动 / 行为处方的完整报告。
5. **肠道屏障报告** —— 综合专项指标及附带资料，异步生成独立主报告，并由现有报告流程提供解读。

服务以 HTTP + JSON 形式对外提供，问答与对话接口支持 SSE 流式输出。

---

## 技术栈

| 类别 | 技术 |
|---|---|
| 语言 | Python 3.12+ |
| Web / 异步运行时 | Tornado 6.5.2、asyncio |
| AI 编排 | LangGraph 1.2.9、AION Gateway |
| 数据与协调 | MongoDB、Redis |
| 数据模型 | Pydantic 2 |
| HTTP Client | HTTPX |
| 测试 | pytest、pytest-asyncio、Tornado testing |

---

## 目录结构

以下按职责列出主要目录与关键文件。初次阅读请结合[代码导航与架构阅读指南](wiki/code-navigation.md)：其中对照业务模块、主 Agent、公共能力和目录，展示查询、AI 回复及持久任务的真实调用链，并说明修改功能的入口和本地目录边界。测试布局见[测试规范](tests/README.md)。

```text
shulan/
├── main.py                                       # 进程入口：打印脱敏配置并启动 Tornado
├── server.py                                     # 历史导入/启动兼容门面
├── activate.sh                                   # Bash 开发环境辅助，实际服务由 main.py 启动
├── .python-version                               # Python 版本
├── .ruff.toml                                    # Python 检查与格式规范
├── .gitattributes                                # 文本换行和二进制文件属性
├── .gitignore                                    # 私有配置、缓存及本地产物排除规则
├── .env.example*                                 # 可入库的主/开发/测试/生产配置模板
├── pyproject.toml                                # 项目元数据、依赖与 pytest 配置
├── requirements.txt                              # pip 依赖清单
├── CONTRIBUTING.md                               # Git 协作、测试与代码规范
├── AGENTS.md                                     # 编码智能体的工作约束和必读规范
├── TODO.md                                       # 后续实施与验收任务
├── ISSUE.md                                      # 已发现问题、风险和处理状态
│
├── bootstrap/                                    # 唯一应用组合根
│   ├── container.py                              # AppContainer：应用依赖与资源所有权边界
│   ├── factory.py                                # 组装 Service、Adapter、Worker 并注入 AgentRuntime
│   ├── document_sources.py                       # 共享 Document/Profile 来源图与三域正式消费者
│   ├── document_contracts.py                     # Document 公共兼容服务、材料及观察读取装配
│   ├── report_documents.py                       # Report 固定文档消费者与历史路径依赖装配
│   ├── questionnaire_sources.py                  # 问卷来源、材料 reader 和后台投递装配
│   ├── record_sources.py                         # Record 原始来源与后台投递装配
│   ├── chat_sources.py                           # Chat 原始来源与唯一新来源 Worker 装配
│   └── chat_history.py                           # 备用群聊查询工具的显式创建工厂
├── runtime/                                      # 进程生命周期、周期任务与安全排空
│   ├── application.py                            # ApplicationBundle、服务启动、周期任务与停服排空
│   ├── lifecycle.py                              # 生命周期状态、readiness 与请求租约
│   ├── resources.py                              # 同步/异步资源登记、逆序关闭与失败聚合
│   ├── source_recovery.py                        # 各域后台来源恢复周期的进程内防重入
│   └── supervisor.py                             # 当前进程任务准入、追踪与等待
├── web/                                          # Tornado 入站适配层
│   ├── application.py                            # 创建 Tornado Application
│   ├── routes.py                                 # 全部公开路由的唯一集中注册表
│   ├── lifecycle.py                              # 请求追踪与 draining 准入
│   ├── http_logging.py                           # HTTP 请求/响应日志边界
│   ├── stream_batch.py                           # Chat / Record SSE 文本合并
│   └── handlers/                                 # HTTP/SSE 参数校验、响应及领域服务调用
│       ├── base.py                               # 统一 Handler 基类
│       ├── health.py                             # 健康检查、存活与就绪探针
│       ├── analysis.py                           # 上传报告解读入口
│       ├── record.py                             # 建档创建、回答、状态与历史接口
│       ├── questionnaire.py                      # 问卷生成、查询、提交与因素评估接口
│       ├── chat.py                               # 健康对话、SSE、custom 消息与历史接口
│       ├── health_profile.py                     # 主体、事实、画像与文档观测接口
│       ├── report.py                             # 最终报告持久化受理及旧 Pipeline 兼容入口
│       ├── report_query.py                       # 最终报告批次列表与产物详情查询
│       └── gut_barrier.py                        # 肠道屏障报告受理入口
│
├── modules/                                      # 模块化业务入口、端口及领域规则
│   ├── health_analysis/                          # 独立同步健康分析、四项输出与持久追溯
│   │   ├── application.py                        # 基础信息确认、并发资料读取、总期限及成功前确认追溯
│   │   ├── agent.py                              # health_analysis.main固定执行身份
│   │   ├── generation.py                         # 五步分析、基础分支与最终四项组装
│   │   ├── survey_quality.py                     # 联合审核、定向改写与受影响题复审
│   │   ├── factors.py                            # 真实医学片段、个人条件与同步匹配
│   │   ├── policy.py                             # 独立24生活目标、3主题例外与适用性
│   │   ├── deduplication.py                      # 程序确定去重、语义覆盖和发布审核
│   │   ├── basic_info_sync.py                    # 固定画像确认与后台恢复
│   │   └── surveys.py                            # 新域冻结Schema、多卷已问题与真实回答解码
│   ├── analysis/                                 # 公共 Document 能力的实际目录及独立 Analysis 用例
│   │   ├── application/service.py                # 报告解读与共享处理应用服务
│   │   ├── application/documents.py              # 统一文件处理及来源留痕
│   │   ├── application/document_reuse.py         # 内容去重、执行协调与检查点恢复
│   │   ├── application/output_profiles.py        # 输出版本注册、固定选择与派生缓存
│   │   ├── contracts.py                          # Document 输出选择与实际输入快照契约
│   │   ├── snapshots.py                          # 不可变材料/事实与显式准备、投影
│   │   ├── formal_source.py                      # 固定正式来源命令及持久恢复
│   │   ├── managed_source.py                     # 托管来源的显式更正与补充
│   │   ├── publication.py                        # 来源级显式发布端口与回执契约
│   │   └── ports.py                              # 文档解析等出站端口
│   ├── record/                                   # 动态建档、答案恢复及会话管理
│   │   ├── agent.py                              # RecordAgent：建档创建、回答及恢复的主入口
│   │   ├── application/service.py                # QASessionManager：建档用例与会话编排
│   │   ├── application/creation.py               # 可恢复的建档创建与发布协调
│   │   ├── application/flow.py                   # 确定性的阶段、计数、主诉与提问策略
│   │   ├── application/interview.py              # 固定主题题位、生活习惯相关性与大类平衡选题、计数及阶段进度
│   │   ├── application/lifestyle.py              # 24个受控生活细项、五类映射与明确兜底问题
│   │   ├── document_context.py                   # Record 独立文档消费快照与附件状态投影
│   │   ├── documents.py                          # Record 固定文档 renderer、准备 checkpoint 与发布端口
│   │   ├── profile_context.py                    # Record 自有画像输入与固定 renderer
│   │   ├── profile_memory.py                     # 完整分页、读取预算与画像上下文 Port
│   │   └── ports.py                              # Session、Prompt、报告处理与画像端口
│   ├── questionnaire/                            # 独立整卷生成、提交、两小时暂存及来源投影
│   │   ├── agent.py                              # QuestionnaireAgent：生成与因素评估主入口
│   │   ├── service.py                            # 异步状态、共享会话租约、幂等及恢复
│   │   ├── generation.py                         # 报告驱动问卷与健管师清单的共同规划
│   │   ├── generation_policy.py                  # 问卷自有的固定生成规则
│   │   ├── factor_tasks.py                       # 因素评估持久任务、领取及恢复
│   │   ├── submitted_material.py                 # 已提交答案及附件的固定公共材料
│   │   ├── context.py                            # 已提交答案及报告原始观测的只读投影
│   │   └── schemas.py                            # 问卷 Schema 与空答案归一化
│   ├── chat/                                     # 健康对话与画像/报告临时上下文
│   │   ├── agent.py                              # ChatAgent：完整/SSE 回复与建议问题主入口
│   │   ├── application/service.py                # ChatSessionManager：完整/SSE 对话与会话门禁
│   │   ├── profile_context.py                    # Chat 自有画像 renderer
│   │   ├── profile_memory.py                     # 公共画像读取、分页预算与本域上下文组织
│   │   ├── profile_source.py                      # 原始用户消息与固定画像来源草稿
│   │   ├── profile_publication.py                 # 保存后独立持久投递、预算与恢复
│   │   └── ports.py                              # Session、Outbox、QA 门禁与只读记忆端口
│   ├── chat_history/                             # 共享群聊查询协议与只读工具，健康分析显式消费
│   ├── report/                                   # 最终报告、持久化受理、恢复与回调
│   │   ├── agent.py                              # ReportAgent：报告任务主入口与固定执行清单
│   │   ├── pipeline.py                           # 来源、评分、循证、主报告与后续报告编排
│   │   ├── durable.py                            # DurableReportService、生成恢复与回调 Worker
│   │   ├── delegation.py                          # Report 公开持久后续任务与固定输入契约
│   │   ├── reports.py                            # 后续报告生成、确定性拼装及降级
│   │   ├── document_projection.py                # Report 自有文档投影及缓存契约
│   │   ├── document_dispatch.py                  # 按任务固定版本选择新旧文档消费者
│   │   ├── research.py                           # 可选有界研究子 Agent，代码默认关闭
│   │   ├── research_integration.py               # 父流程委派证据缺口并校验研究结果
│   │   ├── artifacts.py                          # 五类产物的幂等保存与发布边界
│   │   ├── chat_context.py                       # Chat 报告选择规则与有界正文渲染
│   │   ├── ports.py                              # 来源、画像快照、产物与模型等业务端口
│   │   ├── durable_ports.py                      # 持久任务存储与回调传输端口
│   │   └── schemas.py                            # 请求及报告数据模型
│   ├── gut_barrier/                              # 肠道屏障独立主报告与任务链路
│   │   ├── agent.py                              # GutBarrierAgent：主报告固定工作流入口
│   │   ├── rule_tools.py                         # 确定性专项规则的受控工具包装
│   │   ├── delegation.py                        # 有界持久委派，不影响主报告回调补投
│   │   ├── service.py                            # 受理、租约、恢复、主报告投递及解读委托
│   │   ├── pipeline.py                           # 专项评估、主报告生成与内部审计
│   │   ├── assessment.py                         # 不含 I/O 的专项评估规则
│   │   ├── publication.py                        # 固定主报告的对外内容投影
│   │   └── schemas.py                            # 专项请求与数据模型
│   └── health_profile/                           # 用户健康画像的公共边界
│       ├── application/                          # 画像应用装配与公共门面
│       ├── evidence_contracts.py                 # 固定版本的纯陈述证据契约与 Port
│       ├── evidence_policy.py                    # 精确版本的纯规则提供方
│       └── contracts.py                          # 画像公共快照、来源事件和回执 V1 契约
├── adapters/                                     # 具体出站适配器
│   ├── health_analysis/                          # 公共报告/群聊读取、统一模型与majority追溯存储
│   ├── analysis/                                 # 共享文件解析与归档
│   │   ├── docmind.py                            # DocMind 文档解析 Client 构造
│   │   ├── document_trace.py                     # 解析全过程及大内容分块归档
│   │   ├── document_index.py                     # 文件内容身份、跨进程租约与阶段恢复
│   │   ├── document_download.py                  # 完整字节下载及公网地址校验
│   │   └── image_probe.py                        # 无扩展名图片的受限 HTTP 格式探测
│   ├── persistence/                              # 会话及建档创建任务存储
│   │   ├── sessions.py                           # Chat/Record MongoDB 与 Redis Session Adapter
│   │   └── record_creation.py                    # 建档创建任务的持久存储
│   ├── chat/profile_source.py                    # Chat 来源显式路由与两阶段资源装配
│   ├── chat/source_material.py                   # 不可变原始消息快照及公共读取
│   ├── chat/source_store.py                      # 独立任务 CAS、PRIMARY 证明与分页补偿
│   ├── chat/source_admission.py                  # 实际 fresh 准入、持久 owner fence 与公开接收
│   ├── chat_history/                             # OpenIM 群聊查询 HTTP 适配及响应校验
│   ├── questionnaire_factory.py                  # 问卷生产依赖组装
│   ├── questionnaire_documents.py                # 独立文件分块暂存与只读引用
│   ├── questionnaire_generation.py               # 单次整卷规划模型适配
│   ├── record/prompts.py                         # Record PromptManager Adapter
│   ├── record/documents.py                       # 既有文件处理器到 Record 文档端口的兼容 Adapter
│   ├── record/prepared_documents.py              # 固定 bundle 消费、先保存准备引用再发布的 Adapter
│   ├── record/document_compatibility.py           # Document 公开命令接入与旧受理版本兼容工厂
│   ├── record/formal_documents.py                # 默认 formal v2 与旧 prepared/legacy 固定选择
│   ├── report/                                   # Agent、画像、文件、存储及回调适配
│   │   ├── factory.py                            # Report Pipeline 与具体依赖组装
│   │   ├── main_agent.py                         # Report 主 Agent 与内部模型依赖安装
│   │   ├── formal_documents.py                   # 默认正式文档消费与独立业务投影
│   │   ├── fixed_documents.py                    # 历史固定文档消费兼容实现
│   │   ├── research.py                           # 研究循环按配置装配
│   │   ├── agent_narrative.py                    # 主叙事 Agent Adapter 及每报告 Factory
│   │   ├── agent_followup.py                     # 受控后续报告模型任务 Adapter
│   │   ├── document_source.py                    # Report 上传与引用接入共享文件处理
│   │   ├── profile_snapshot.py                   # 只读画像快照 Adapter
│   │   ├── artifact_store.py                     # MongoDB 报告批次与产物存储
│   │   ├── durable_store.py                      # 持久受理、任务租约、恢复与交付记录
│   │   ├── delegation_store.py                   # 独立后续任务、原子产物冻结与投递投影
│   │   ├── delegation.py                         # Report 后续生成与持久服务装配
│   │   ├── delivery.py                           # 持久回调 Worker 的 HTTP 传输
│   │   └── chat_context.py                       # 向 Chat 提供受控报告上下文
│   ├── gut_barrier/                              # 独立任务、主报告回调与固定模板
│   │   ├── delegation.py                        # Report 公开用例适配与本域委派租约
│   │   ├── factory.py                            # 专项来源、Agent、存储与回调依赖组装
│   │   ├── store.py                              # MongoDB 专项任务与交付状态存储
│   │   ├── delivery.py                           # 主报告投递及模板绑定头
│   │   └── frontend_template.py                  # 固定版本模板合并与历史模板校验
│   └── messaging/chat_outbox.py                  # MongoDB Chat custom 消息 Outbox
│
├── agents/                                       # AgentHub、Graph、LLM Gateway 与持久化
│   ├── hub.py                                    # 业务调用 Graph 的统一入口
│   ├── ports.py                                  # 固定工作流主 Agent 与精确版本 Graph 端口
│   ├── streaming.py                              # 外层流关闭时同步释放内部工作流
│   ├── tools/                                    # 受控工具契约、共享预算、取消与持久回执
│   ├── graphs/                                   # Chat / Record / Analysis / Health Profile / Report Graph
│   │   └── report/                               # 无状态的报告生成与上下文选择能力
│   │       ├── graph.py                          # report.narrative 主叙事 Graph
│   │       ├── followup_graph.py                 # report.followup 受控后续模型任务
│   │       ├── retrieval_graph.py                # report.retrieve_context 报告批次与类型选择
│   │       └── registration.py                   # 注册三项 Report 能力
│   ├── knowledge/                                # 统一知识检索端口、路由与适配
│   ├── llm/                                      # 模型抽象、Profile 与文本校验
│   │   ├── aion_gateway.py                       # AION 模型请求适配入口
│   │   └── tool_gateway.py                       # AION 受控工具循环与事件适配
│   ├── persistence/                              # checkpoint、模型上下文与分布式 lease
│   └── runtime/                                  # Agent 执行依赖管理，与根 runtime 的进程管理分开
│       ├── container.py                          # AgentRuntime 与模块隔离注册
│       ├── registry.py                           # Graph 精确版本注册
│       ├── ports.py                              # 业务 AgentPort 精确版本注册
│       └── application.py                        # 读取应用配置并注册生产 Graph
│
├── services/                                     # 弃用中的旧导入兼容路径
│   ├── analysis.py                               # 转发至 modules.analysis.application
│   ├── record.py                                 # 转发至 modules.record.application
│   ├── chat.py                                   # 转发至 modules.chat.application
│   ├── session_coordination.py                   # 转发至 utils.identifiers
│   └── chat_custom_message_outbox.py             # 转发至 adapters.messaging.chat_outbox
│
├── health_profile/                               # 画像领域实现，由模块公共入口暴露
│   ├── contracts.py                              # 唯一画像的公共读取与来源发布值对象
│   ├── snapshots.py                              # 不补主体、不排队的结构化只读快照
│   ├── report_materials.py                       # 既有本人完整原解读与独立贡献的公共纯读出口
│   ├── source_publications.py                    # 来源迁入标记、幂等接收、发布游标与回执
│   ├── source_material.py                        # 授权来源快照解析与 Profile 原始观察映射
│   ├── source_contracts.py                       # 版本化用户原始材料公共契约
│   ├── source_extraction.py                      # 固定提取版本与持久模型输出
│   ├── source_application.py                     # 来源完整替换/撤回、持久计划与恢复 Worker
│   ├── application.py                            # 画像 Service、来源与摄取 Worker 的生产组装
│   ├── service.py                                # 主体、事实、画像与审计用例
│   ├── repository.py                             # 画像领域数据持久化
│   ├── worker.py                                 # 异步来源摄取及待投递来源补偿
│   ├── memory.py                                 # 旧画像上下文 Provider，保留兼容；默认用各域 renderer
│   ├── evidence_results.py                       # 循证派生结果持久化复用与生成 lease
│   └── summary.py                                # 确定性组装画像综述与建议
├── db/                                           # 共享数据库连接与 collection 定义
│   ├── mongodb.py                                # MongoDB Client、数据库及业务集合
│   └── redis.py                                  # Redis Client
├── config/                                       # 环境配置与问题主题配置
│   ├── env.py                                    # 环境变量加载、默认值及校验
│   ├── report_endpoints.py                       # 报告回调/产品地址的部署边界校验
│   └── question_topics.json                      # 建档主题配置
├── prompts/                                      # 各业务域及公共能力使用的主要提示词模板
│   ├── report/                                   # 主叙事、报告检索及 follow-up 固定模板
│   └── questionnaire/                            # 整卷规划与因素评估模板
├── utils/                                        # 日志、标识、产物路径与通用辅助工具
│   ├── identifiers.py                            # 无 I/O 的稳定 session lease ID
│   ├── report_files.py                           # 报告文件名、类别与历史类型转换
│   └── log/                                      # 公共日志包
│       ├── core.py                               # 日志初始化、模块 logger 与 Tornado 日志接线
│       ├── context.py                            # 按请求/任务隔离的日志身份上下文
│       ├── files.py                              # 日志文件输出与轮转
│       └── preview.py                            # 正文预览与缩略工具
│
├── scripts/                                      # 运维、恢复、队列检查与本地预览入口
│   ├── report_callbacks.py                       # 最终报告回调查询、迁移与补投
│   ├── recover_qa_creation.py                    # 建档创建及不确定文件提交的检查/受控恢复
│   ├── check_gut_barrier_queue.py                # 只读专项队列检查与版本核对
│   ├── render_gut_barrier_callback.py            # 已保存主报告回调 JSON 的本地预览
│   ├── generate_gut_barrier_report.py            # 专项报告本地生成及离线模式
│   ├── artifact_paths.py                         # 报告产物的仓库外输出边界
│   ├── clear_legacy_qa_cache.py                   # 旧建档缓存的显式维护入口
│   ├── format_markdown.py                        # Markdown 段落与 JSON 示例格式维护
│   └── check_env_example.sh                      # 配置模板维护检查
│
├── tests/                                        # 离线测试、API 手动联调与压测代码
│   ├── unit/                                     # 无网络/契约测试；pytest 默认只收集此处
│   │   ├── _support/                             # 假依赖、纯计算与共享验收过程
│   │   ├── tools/                                # 报告、QA 文档和 Record 离线验收入口
│   │   └── api/                                  # API 页面离线回归与浏览器 benchmark
│   ├── api/                                      # 手动 API 联调页面与配套验收工具
│   │   ├── pages/                                # 浏览器联调页面及静态资源
│   │   ├── scripts/                              # 显式运行的真实依赖验收入口
│   │   ├── integration/gut_barrier/               # 真实 MongoDB 验收；保留环境变量门控
│   │   └── serve_pages.py                        # 静态测试页面、同源代理与模拟回调接收端
│   ├── loadtest/                                 # 独立 Locust 压测工具
│   │   └── scenarios/                            # 冒烟、查询、聊天与建档负载场景
│   └── fixtures/analysis/                        # 版本管理的固定文档回放与恢复样例
├── docs/                                         # 文档导航、API、架构、外部依赖及产品资料
│   ├── README.md                                 # 文档权威级别、导航与维护状态
│   ├── api/                                      # 当前 API、草案与历史版本
│   ├── architecture/                             # 架构决策、当前实现、目标设计及验证记录
│   ├── integrations/                             # 按供应商隔离的外部依赖原始资料
│   └── product/                                  # 原始产品需求与 UI 参考
└── wiki/                                         # 当前实现、运维方法与限制
```

本地出现的 `tmp/`、`temp/`、`api_test/`、日志、缓存及构建目录不属于源码目录树，按 `.gitignore` 排除；`api_test` 只保留旧工具的可选输入/输出路径，正式测试入口均在 `tests/`。测试源码、固定样例、文档与生产模板继续纳入版本管理；部分报告工具要求仓库外输出，详见[代码导航](wiki/code-navigation.md)。

Report 的可选有界研究循环、固定预算、原文引用与默认关闭边界见 [Report 研究子智能体](wiki/report-research-agent.md)。

多智能体重构的实施状态、六张 Mermaid 图与全部 HTTP 入口归属见[架构方案](docs/architecture/modular-multi-agent-architecture.md)。默认来源装配分别见 [Document](wiki/document-source-composition.md)、[Questionnaire](wiki/questionnaire-source-composition.md)、[Record](wiki/record-source-composition.md) 和 [Chat](wiki/chat-source-composition.md)；各域保存成功与画像摄取完成独立，普通查询不运行 Agent。

动态建档的患者分析、会话边界、幂等与测试阶段升级见 [动态建档实现](wiki/record-dynamic-flow.md)。

Document 内部输出的显式版本选择、固定旧别名和缓存兼容见 [Document 输出版本选择](wiki/document-output-selection.md)；独立材料/事实快照、无发布准备与持久发布账本见 [Document 材料与显式发布](wiki/document-facts-publication.md)。

Chat 的完整回复、流式回复和个性化建议已由本域主 Agent 包裹；固定子任务版本、旧会话 thread 兼容及组合根安装边界见 [Chat 主 Agent](wiki/chat-main-agent.md)。

Chat 自有画像 renderer、完整分页读取和有界降级候选见 [Chat 画像上下文](wiki/chat-profile-context.md)，真实公共 Adapter 已由默认组合根接通，实际部署验收另行完成。

Chat 原始消息的正式画像来源候选见 [Chat 来源持久消费](wiki/chat-profile-source.md)：消息与固定模式先共同保存，画像准入和摄取独立后台恢复。真实公开 Profile 适配及离线组合已交付，默认装配与独立后台周期已接通；已有来源不自动迁移，真实部署另行验收。

Record/Report 迁移所需的完整原解读另存为不可变派生快照，材料、事实与解读保持固定引用，详见 [Document 完整解读兼容桥](wiki/document-understanding-bridge.md)。固定原件与失败材料读取见 [Document 材料读取](wiki/document-material-reader.md)；用于各域上下文迁移的无副作用补充资料入口见 [Document 观察纯读](wiki/document-observation-reader.md)，默认消费者已接通，真实部署另行验收。旧 Report v16 的独立正式发布、固定清单及恢复边界见 [Document Report 兼容发布](wiki/document-report-publication.md)，Report 私有投影不作为公共事实。

Document 完整中立贡献、不可变来源快照与真实新来源准入见 [Document 完整来源](wiki/document-complete-source.md)；已交付的固定更正、补充与退休计划见 [Document 托管来源](wiki/document-managed-source.md)。[Document 正式来源编排](wiki/document-formal-source.md) 已独立提供固定 legacy/fresh/managed 命令、真实归档回执、持久等待及纯读业务投影；[Record 正式文档消费](wiki/record-formal-documents.md)、[Report 正式文档消费](wiki/report-formal-documents.md) 和 [Analysis 固定意图](wiki/analysis-formal-source.md) 已独立交付；[Document 默认装配](wiki/document-source-composition.md) 已接通共享 gate、唯一画像解析器与后台恢复。历史来源不自动迁移，真实部署继续单独验收。

Record 新受理默认使用 formal v2，绑定本域 renderer、准备 checkpoint 和正式来源命令；历史 prepared v1/legacy 仍按受理版本执行真实旧实现，详见 [Record 正式文档消费](wiki/record-formal-documents.md)。[原固定文档消费者](wiki/record-document-consumer.md)保留兼容背景，不能据此把新受理改回旧发布链。

Record 自有画像 renderer、完整分页预算与补充观察边界见 [Record 独立画像上下文](wiki/record-profile-context.md)；真实公共读取 Adapter 已由默认组合根接通，并保留旧正文等价回归；实际部署验收另行完成。

Record 正式画像来源的原始材料、持久完成证据与后台投递候选见 [Record 正式画像来源持久消费](wiki/record-profile-source.md)；真实 fresh 准入、owner fence 和完成证据已交付；默认代码接线、独立后台周期与离线组合已完成；历史来源显式迁移和真实部署另行验收。

整份问卷的主 Agent 入口、固定旧任务版本、纯查询与后台恢复见 [Questionnaire 主 Agent 与后台恢复](wiki/questionnaire-main-agent.md)。证据规则的本域 Port、显式版本选择和旧 checkpoint 兼容见 [Questionnaire 固定证据规则消费](wiki/questionnaire-evidence-policy.md)。

问卷已提交附件的公开固定读取、旧发布身份与 pending 扫描隔离见 [Questionnaire 已提交材料](wiki/questionnaire-submitted-material.md)；已交付的 [Document 问卷附件发布](wiki/document-questionnaire-publication.md) 核对真实 ready 与固定提交证明，保留旧来源身份和持久回执；[Questionnaire 持久来源消费者](wiki/questionnaire-profile-source.md) 已完成真实准入、附件发布与答案事件闭环；默认代码接线与离线组合已完成，历史来源迁移和真实部署另行验收。

Analysis、Chat、Record 的请求链为 `web/routes.py -> web/handlers/* -> modules/*/application`。Analysis、Chat、Record 在各自 `ports.py` 声明出站端口；DocMind、MongoDB/Redis Session、PromptManager 和 Chat Outbox 的具体实现位于 `adapters/`，由 `bootstrap/` 构造并注入。Health Profile 目前只建立了模块公共入口，内部领域、Repository 和 Worker 仍保留在 `health_profile/`，没有在本轮强行拆分。

进程生命周期由 `runtime/application.py` 中的 `ApplicationBundle` 与 `bootstrap/container.py` 中的 `AppContainer` 共同持有，两者共享同一个 `ResourceRegistry`。Registry 支持同步/异步资源、按登记逆序关闭，并在尝试关闭全部资源后聚合失败；独立 `WorkerRegistry` 仍是后续收敛目标。`server.py` 和 `services/` 只保留历史调用方所需的兼容面，新代码应直接使用新路径。

`/api/v1/report` 及其列表、详情接口由 `web/routes.py` 集中注册。生产 Handler 注入 `DurableReportService`，先持久化受理记录再返回 `processing`；生成、恢复和回调补投由进程内周期 Worker 调度。主叙事、后续模型任务及供 Chat 使用的报告上下文选择已通过 AgentHub 接入；列表和详情直接读取产物，不启动 Agent。生成时读取受控画像，五类产物独立持久化并支持 Chat 原会话及跨会话追问。上传文件通过共享解析服务处理。显式注入旧 Pipeline 的调用方仍有进程内兼容路径，不能据此描述生产受理链。当前实现与验收入口见 [Report 完整接入指南](docs/architecture/report-agent-integration-guide.md)；独立 Worker 进程和完整模块资源契约仍待建设，[旧迁移手册](docs/architecture/report-api-migration-guide.md)仅作历史参考。

肠道屏障通过独立的 `GutBarrierService`、任务集合和主报告回调链路运行，与原五类报告流程分开；共享现有回调配置，并将主报告交给原报告模块生成解读。健康分析新增一个独立同步 POST 后，当前本地 Tornado 注册32个路由模式、33个HTTP方法入口，具体契约以 [API 文档](docs/api/API.md) 为准。原有模块目录与调用边界于2026-11-10核对；健康分析本次增量及提供方组合见 [I-047](ISSUE.md#i-047-健康分析b0前置交接与公共纯读依赖)，均不替代真实模型、数据库、回调或部署验收。

---

## 环境准备

### 1. Python version

[.python-version](.python-version)

### 2. 安装依赖

方法一：

[requirements.txt](requirements.txt)

```bash
pip install -r requirements.txt
```

方法二：

[pyproject.toml](pyproject.toml)

```bash
pip install .
```

可选 extras：`pip install ".[loadtest]"` 额外安装压测工具 locust（用于 [tests/loadtest/](tests/loadtest/README.md)，正式部署不需要）。

### 3. 配置环境变量

项目提供四个模板文件，均可提交版本库（不含密文）：

| 文件 | 用途 |
|---|---|
| `.env.example` | 主模板，登记全部变量，作为一致性检查的基准 |
| `.env.example.dev` | 开发环境预填值（DEBUG=true，PORT=9002） |
| `.env.example.test` | 测试环境预填值（PORT=9012） |
| `.env.example.production` | 生产环境预填值（DEBUG=false，PORT=9022） |

选择对应环境复制为 `.env`，再填入密文配置项：

```bash
cp .env.example.dev .env   # 或 .test / .production
```

依赖安装完成后，可运行以下命令检查 `.env` 与 `.env.example` 之间是否存在配置差异：

```bash
check-env
```

每次修改 `.env.example` 后，必须同步三个环境模板，并执行以下三条命令检查变量一致性（环境预填值可以不同）：

```bash
check-env --example .env.example --env .env.example.dev
check-env --example .env.example --env .env.example.test
check-env --example .env.example --env .env.example.production
```

检查指定的.env和模板之间的变量一致性：

```bash
check-env --example .env.example.dev        --env .env
check-env --example .env.example.dev        --env .env.dev
check-env --example .env.example.test       --env .env.test
check-env --example .env.example.production --env .env.production
```

> 自动化开发只允许查看和修改 `.env.example*` 模板；其他 `.env*` 私有配置禁止查看或修改。模板同步检查必须显式指定上述模板路径，不能运行默认读取 `.env` 的裸 `check-env`。本节涉及真实配置的操作由配置维护者自行执行。`.env` 不得提交到版本库，已列入 `.gitignore`。

共享聊天记录查询工具使用 `SHULAN_CHAT_HISTORY_QUERY_URL` 配置完整查询地址，包含 `/api/openim/open-api/message/search` 路径；部署方核对模板与实际配置，代码不按环境自动选择地址。健康分析通过 `bootstrap/health_analysis.py` 显式消费该工具，在固定本人资料范围后按请求的群组标识查询；未配置资料地址时将群聊标为不可用。发送人仅按已验证映射识别，未知发送人不当患者事实，正文还须整理主体与语义。旧问卷、Record、Chat和Report不因此自动查询群聊。构造、返回结构及调用方式见 [Agent工具运行指南](wiki/agent-tool-runtime.md)。

健康分析内部追溯使用 `SHULAN_MONGODB_COLL_HEALTH_ANALYSIS_RUNS`（默认 `health_analysis_runs`），与问卷、报告及画像事实集合隔离，无自动TTL。存储访问权限、保留与清理由部署维护方管理。MongoDB 持久确认与回读校验是成功响应的前置条件。开发期容量和120秒总期限见API及实施计划，不代表真实服务延迟承诺；本机合成客户端见 [测试规范](tests/README.md#5-真实-api-页面)。

评估报告、肠道屏障报告及其后续解读共用 `SHULAN_REPORT_CALLBACK_URL`，推荐产品接口使用 `SHULAN_REPORT_PRODUCT_URL`；两个地址均须显式配置，代码不提供测试地址兜底。生产部署使用以下配置（具体地址以正式接收方确认为准）：

```dotenv
SHULAN_ENVIRONMENT=production
SHULAN_REPORT_CALLBACK_URL=https://m.slmc.top/api/basic-health-assessment/ai-full-back/callback
SHULAN_REPORT_PRODUCT_URL=https://m.slmc.top/api/gut-product/catalog
```

`SHULAN_ENVIRONMENT=production` 时，启动校验及实际发送前校验均要求 HTTPS，并拒绝 `test.slmc.top`（含子域）和本机回环地址；开发、测试环境仍可配置测试域名或模拟接收端。肠道屏障原有的 `SHULAN_GUT_BARRIER_PRODUCT_URL` / `GUT_BARRIER_PRODUCT_URL` 产品接口覆盖项继续兼容，并接受相同校验；显式空值仍表示禁用该产品来源。回调和产品 HTTP 请求不跟随重定向，避免配置地址将请求转到另一个环境。

修改部署配置后须重启全部服务实例，进程环境变量优先于 `.env`；不会自动按 Git 分支切换环境。已有回调任务保存了受理时的地址，生产发送前也会拦截其中的测试地址，任务保留等待处理；普通评估报告积压可按[回调运维指南](wiki/report-callback-operations.md)预览并迁移。肠道屏障使用独立任务集合，普通报告迁移命令不处理该集合，需要单独核对存量任务。开发页面模拟回调为 `http://127.0.0.1:9080/callback`，独立 `python -m tests.api.scripts.report_callback_server` 默认接收地址为 `http://127.0.0.1:10002/callback`。

Agent 运行时使用三个独立的 MongoDB collection，分别保存 AION 模型上下文、LangGraph checkpoint 和 checkpoint 中间写入。默认名称可通过 `SHULAN_MONGODB_COLL_AGENT_LLM_CONTEXTS`、`SHULAN_MONGODB_COLL_AGENT_CHECKPOINTS` 和 `SHULAN_MONGODB_COLL_AGENT_CHECKPOINT_WRITES` 修改。`SHULAN_AGENT_LEASE_TTL_SECONDS` 控制 Redis 会话 lease 的失效时间，默认 300 秒，生产值应大于单次模型调用的最长执行时间。

可选的受控工具运行器另使用 `SHULAN_MONGODB_COLL_AGENT_TOOL_RUNS`（默认 `agent_tool_runs`）保存父子预算、取消和幂等回执，无自动 TTL；业务必须显式注入持久适配器后才能启用，现有业务未默认开启工具循环。领域版本端口、生产注册隔离和恢复边界见 [Agent 工具运行指南](wiki/agent-tool-runtime.md)。

用户健康画像使用七个独立集合，默认名称为 `health_subjects`、`health_profile_facts`、`health_profile_events`、`health_profile_views`、`health_profile_jobs`、`health_profile_evidence_results` 和 `health_profile_source_publications`；可通过对应的 `SHULAN_MONGODB_COLL_HEALTH_*` 变量修改。循证派生结果集合由 `SHULAN_MONGODB_COLL_HEALTH_PROFILE_EVIDENCE_RESULTS` 配置，无自动过期；同一画像版本和解释条件的首份合格结果跨重启复用，生成使用现有 Redis lease，不改变健康事实或画像版本。

Profile 纯读快照、来源事件与显式迁入条件见 [Profile 公共契约](wiki/profile-public-contracts.md) / [Profile 公共事实分页](wiki/profile-fact-pagination.md) / [Profile 旧摄取状态](wiki/profile-legacy-ingestion-status.md) 与 [多来源原始材料](wiki/profile-multi-source-publications.md) / [Record v2 与部分应用结算](wiki/profile-record-source-settlement.md)。默认消费者正式发布链已按域接通；新的 source ledger 和离线通过不代表历史来源已迁移或真实部署已验收。

原报告资料的独立公共纯读出口见 [Profile 原报告资料](wiki/profile-report-materials.md)：返回原始解读、独立来源贡献、固定身份及覆盖限制，不初始化本人、不运行解析和模型。通过现有业务接口使用该读取能力。真实数据库与部署另行验收。

完整 Document 来源、有效/退休贡献清单、接收就绪与 Chat v2 兼容见 [Profile 来源契约](wiki/profile-document-source-contract.md)；完整材料与 managed 升级提供方已独立交付，默认消费者接入与离线组合已完成，历史来源显式迁移和真实部署继续独立验收。

新来源的 PRIMARY 存在性证明、两阶段 fresh 准入与只归档命令见 [Profile 来源准入](wiki/profile-source-admission.md)；owner fence 与默认消费者均已按独立模块交付，已有来源继续保持其固定版本和迁移门槛。

Questionnaire 完整答案的独立容量、固定提交证明和旧 V1 兼容见 [Profile 问卷来源 V2](wiki/profile-questionnaire-source-v2.md)；消费者和默认来源已按独立批次接通，历史来源迁移与真实部署另行验收。

独立冻结的纯陈述判定和精确版本选择见 [Profile 固定证据规则](wiki/profile-evidence-policy.md)；Questionnaire 已通过本域 Adapter 显式选择冻结 v1，正式来源迁移继续独立交付。

最终报告使用 `report_batches`、`report_artifacts` 和 `report_callback_deliveries` 三个集合，分别保存批次、固定产物和回调交付状态；对应配置为 `SHULAN_MONGODB_COLL_REPORT_BATCHES`、`SHULAN_MONGODB_COLL_REPORT_ARTIFACTS` 和 `SHULAN_MONGODB_COLL_REPORT_CALLBACK_DELIVERIES`。肠道屏障使用独立任务集合，配置与运维边界见 [肠道屏障 Wiki](wiki/gut-barrier.md)。

Agent 医学知识库检索使用 `SHULAN_KNOWLEDGE_API_BASE_URL`、`SHULAN_KNOWLEDGE_API_KEY` 和 `SHULAN_KNOWLEDGE_API_TIMEOUT_SECONDS`。密钥为空时 Chat 和 Record 保持原流程，不执行知识检索；`SHULAN_EVIMED_*` 仍仅用于 Report 模块的证据检索。

医生主动消息使用独立的 MongoDB Outbox 保存待投递事件，默认 collection 为 `chat_custom_message_outbox`，可通过 `SHULAN_MONGODB_COLL_CHAT_CUSTOM_MESSAGE_OUTBOX` 修改。消息会先进入 Outbox，再按会话 lease 顺序写入聊天历史，避免 AI 正在输出时丢失医生消息。

---

## 启动服务

```bash
python main.py
```

启动时会先打印配置摘要（敏感信息自动脱敏），随后在 `SHULAN_SERVER_PORT` 配置的端口（默认 `9002`）监听。

启动成功后，日志会打印所有可用接口地址，例如：

```
服务已启动，监听端口: 9002
健康检查接口: http://localhost:9002/health
体检报告解读接口: http://localhost:9002/api/v1/interpret/health-report
...
```

### 安全停止与生产更新

生产环境使用 `SIGTERM` 触发无损排空，例如由 systemd 管理时执行：

```bash
systemctl stop shulan
```

也可以向确认无误的主进程发送信号：

```bash
kill -TERM <pid>
```

收到信号后，旧实例会停止监听新连接以及领取新的画像、Report 和肠道屏障任务，已经进入 Handler 的 HTTP/SSE 请求、已经登记的最终报告任务和正在执行的周期任务会继续自然完成。只有活跃请求与任务全部归零后，进程才关闭连接、释放 AgentRuntime 并正常退出；重复发送停止信号不会取消存量工作。

探针用途：

- `/health/live`：进程存活探针，排空期间保持 HTTP 200；
- `/health/ready`：流量就绪探针，排空期间返回 HTTP 503；
- `/health`：兼容健康检查，同时返回 lifecycle 状态和活跃计数。

单实例可以“排空旧实例 → 更新 → 启动新实例”；需要持续接收新流量时，可以先启动新实例并切流，再向旧实例发送 `SIGTERM`。进程管理器不得在排空完成前自动发送 `SIGKILL`。

完整部署约束见 [Tornado 安全停服与生产更新方案](docs/architecture/graceful-shutdown-and-deployment.md)。

---

## API 概览

当前对外接口以 [API 文档](docs/api/API.md) 为唯一有效契约，完整的请求参数、响应字段、状态码、回调结构和调用示例均以该文档为准。

| 能力 | 主要接口 | 调用方式 | 说明 |
|------|----------|----------|------|
| 健康检查 | `/health`、`/health/live`、`/health/ready` | HTTP GET | 区分进程存活和流量就绪状态。 |
| 体检报告解读 | `/api/v1/interpret/health-report` | HTTP POST | 解析体检、检验和影像报告并返回结构化解读。 |
| 健康问答建档 | `/api/v1/qa/*` | HTTP POST / SSE | 创建问答会话、提交答案、断点续答并查询历史。 |
| 最终报告生成 | `/api/v1/report` | HTTP POST + 异步回调 | 异步生成肠道菌群医学辅助解读报告。 |
| 最终报告查询 | `/api/v1/report/list`、`/api/v1/report/get` | HTTP POST | 按用户查询批次和已保存产物，供展示及 Chat 追问。 |
| 肠道屏障报告 | `/api/v1/gut-barrier/report` | HTTP POST + 异步回调 | 独立主报告及现有报告模块提供的解读。 |
| AI 健康对话 | `/api/v1/chat/*` | HTTP GET / POST / SSE | 获取推荐问题、发送消息、插入 custom 消息并查询历史。 |
| 用户健康画像 | `/api/v1/health-profile/*` | HTTP POST | 查询主体、事实、画像与变更记录，处理待确认事实。 |

- [当前 API 文档](docs/api/API.md)
- [API 文档编辑手册](docs/api/API_EDITING_GUIDE.md)
- [历史版本](docs/api/history/)

EviMed 知识检索是后端依赖；实时语音和通用文档解析虽然由公司内另一独立服务直接提供，但仍属于 Shulan 对外 API 能力，完整聚合契约保留在 [当前 API 文档](docs/api/API.md)。上游原始接口资料见 [外部依赖接口文档](docs/integrations/README.md)。

---

## 架构与运维

- [体检报告解读职责改造](docs/architecture/health-report-interpretation-implementation-plan.md)：2026-11-11 已按验收文档分模块实现，本地集成验证进度见 ISSUE I-046；[当前实现](wiki/health-report-interpretation.md)覆盖两步抽取、固定归档后响应、后台交付和画像完整视图，[需求](docs/product/health-report-interpretation-requirements.md)保持职责边界，真实依赖与部署尚未验收。

- [上传文件统一解析与画像摄取](docs/architecture/shared-document-ingestion-guide.md)：Analysis、Record、Report 已接入共享处理，支持全文分段、documentId 引用、实际字节去重、版本化扩展输出和原报告指标查询；最终报告请求复用仍有独立限制。文件索引配置为 `SHULAN_MONGODB_COLL_DOCUMENT_INDEX`，默认 `document_index`。

- [文档导航与维护状态](docs/README.md)
- [Shulan 应用架构：模块化单体与 Ports/Adapters](docs/architecture/application-architecture.md)
- [API 实现架构合规与迁移指南](docs/architecture/api-implementation-compliance.md)
- [架构文档状态与当前基线](docs/architecture/README.md)
- [最终报告 Agent 与记忆接入指南](docs/architecture/report-agent-integration-guide.md)
- [肠道屏障实现与运维](wiki/gut-barrier.md)：独立任务、主报告/解读分工、模板绑定及队列检查。
- [最终报告回调运维指南](wiki/report-callback-operations.md)：整体迁移积压到当前配置地址、只读预览、单批次补投与状态检查。
- [Report 端到端迁移旧手册（历史草案）](docs/architecture/report-api-migration-guide.md)
- [Tornado 安全停服与生产更新方案](docs/architecture/graceful-shutdown-and-deployment.md)
- [AI Agent 目标架构](docs/architecture/agent-target-architecture.md)
- [Report 主 Agent 与执行版本](wiki/report-main-agent.md)：行为等价封装、固定清单和旧任务兼容边界。
- [Report 结构化画像读取](wiki/report-profile-snapshots.md)：公共快照与消费者自有上下文。
- [Report 独立文档投影](wiki/report-document-projection.md)：固定输入、版本化本域缓存与待接入的正式来源边界。
- [Report 固定文档消费者](wiki/report-fixed-documents.md)：默认固定快照、本域投影与兼容 Publisher，保留旧注入和不可用隔离。
- [Agent Core 当前实现 Wiki](wiki/agents.md)
- [Agent 版本端口与受控工具运行](wiki/agent-tool-runtime.md)
- [API 架构实施状态 Wiki](wiki/api-architecture.md)
- [建档生成容错与答案恢复](wiki/qa-generation-resilience.md)：生成预算、部分结果、保守兜底与断点恢复。
- [计划任务](TODO.md)与[已知问题](ISSUE.md)

---

## 开发说明

### 代码规范

Python 静态检查、导入整理和格式化统一使用 Ruff，以根目录的 [.ruff.toml](.ruff.toml) 为准。具体标准、执行顺序与提交前检查要求见 [贡献指南第五节](CONTRIBUTING.md#五python-代码规范ruff)。

### 异步并发

项目并发通过 asyncio 实现。

### 单元测试和API测试

测试目录、运行方式和真实接口页面的完整规范见 [tests/README.md](tests/README.md)。

### 压力测试

独立 Locust 脚手架位于 [tests/loadtest/](tests/loadtest/README.md)，推荐用独立环境安装；需要项目依赖与压测能力共存的环境可执行 `pip install ".[loadtest]"`（默认安装不含该 extras，不影响正式部署）。分层场景、指标口径与成本纪律见 [压力测试 Wiki](wiki/load-testing.md)。

Document 当前基础信息的固定归档、逐字段贡献与持久恢复见 [Document 当前基础信息](wiki/document-basic-info-v2.md)。Analysis 与 Profile 公开端口的离线组合和默认入口切换进度均在该页记录。
