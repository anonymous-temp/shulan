# 项目代码导航与架构阅读指南

> 当前实现导航；源码核对基线：`refactor/agent@a3333f8`，2026-11-10。本页解释实际目录、关键文件和调用关系，不把目标设计或历史说明当作已运行功能。

[项目 README](../README.md) 提供目录树、安装和启动方法；本页回答“架构概念对应哪个目录、一个请求经过哪些文件、修改功能应该从哪里开始”。详细业务规则继续以各域 Wiki 和源码为准，对外契约以 [API 文档](../docs/api/API.md) 为准。完整目标边界和六张流程图见[多智能体方案](../docs/architecture/modular-multi-agent-architecture.md)，其中“重构前基线”章节是历史背景。

## 1. 先认识业务模块和 Agent 的范围

项目采用模块化单体：同一 Web 应用承载多个业务模块，后台 Worker 当前也由进程内运行时调度。所有外部请求进入 HTTP API，由明确路由和参数选择用例；普通数据查询、管理操作走所属模块的数据能力，需要 AI 的业务任务才调用对应 Agent。请求内返回和持久受理后执行，是 AI 任务内部的生命周期安排。

一个业务模块包含公开入口、查询与命令、数据、任务状态、主 Agent 和私有执行能力，因此业务模块范围大于一次 Agent 运行范围。既有五个业务主 Agent 是 Record、Chat、Report、Questionnaire、GutBarrier。新增健康分析使用独立 `modules/health_analysis/agent.py` 固定入口及执行身份，复用统一模型/知识网关，业务不进入旧问卷生命周期。主 Agent 首期封装固定工作流，不为封装额外调用一次模型；普通规则函数、Graph节点和模型适配器不能一概视为自主子Agent。

`agents/` 是共享执行平台和内部模型工作流目录；业务 Agent 的完整实现还涉及 `modules/`、`adapters/`、`prompts/` 和装配文件。`AgentHub` 执行调用方指定的已注册 Graph，不通过一个全局模型决定业务归属。Report 私有 ResearchAgent 支持有界工具循环，代码默认关闭；是否在某个部署启用须由配置维护者确认。

## 2. 一级目录和根文件

### 2.1 一级目录

| 目录 | 职责与关键入口 | 阅读或修改时的边界 |
|---|---|---|
| [web/](../web/) | [routes.py](../web/routes.py) 集中注册路由；[application.py](../web/application.py) 创建 Tornado 应用；`handlers/` 处理参数、HTTP/SSE 响应和用例调用 | 先从路由定位 Handler；普通查询不经过模型判断请求类型 |
| [bootstrap/](../bootstrap/) | [factory.py](../bootstrap/factory.py) 构造依赖；[container.py](../bootstrap/container.py) 保存应用依赖和资源边界 | 连接业务与具体 Adapter；域内工厂由这里调用，不在 Handler 内另建全套依赖 |
| [runtime/](../runtime/) | [application.py](../runtime/application.py) 启动 HTTP 与周期任务；`lifecycle.py` 管准入，`supervisor.py` 跟踪进程任务，`resources.py` 关闭资源，`source_recovery.py` 防止同一本域恢复周期重叠 | 这是整个进程的运行管理；持久业务任务状态仍由所属模块保存 |
| [modules/](../modules/) | 七个业务或公共模块的用例、主 Agent、领域规则、输入输出契约和 Port | 修改业务流程优先进入本域；公共能力升级由提供方独立交付 |
| [adapters/](../adapters/) | 数据库存储、HTTP/文档解析客户端、模型调用适配、跨模块公共能力转换和域工厂 | 实现模块需要的具体依赖；消费者通过公开契约调用其他域 |
| [agents/](../agents/) | Agent 接口、精确版本注册、Graph、模型网关、工具协议、知识检索及执行持久化 | 五个主 Agent 的业务定义在 `modules/`；本目录不能直接代表全部业务范围 |
| [health_profile/](../health_profile/) | 画像事实、来源接收、提取与治理、快照、持久化和 Worker 的主要实现 | 经 `modules/health_profile/` 暴露公共边界；不是另一套画像系统 |
| [services/](../services/) | `analysis.py`、`record.py`、`chat.py` 等旧导入别名，转发到当前实现 | 保留历史调用兼容，新业务不继续堆放在这里；删除前需核对旧调用方及懒加载关系 |
| [db/](../db/) | `mongodb.py`、`redis.py` 提供现有连接及集合对象 | 连接由应用统一使用；测试不能为了读取配置而导入真实连接 |
| [config/](../config/) | `env.py` 加载配置；`report_endpoints.py` 校验报告外部地址；`question_topics.json` 保存建档主题配置 | 自动化只访问 `.env.example*` 模板，私有环境文件由配置维护者管理 |
| [prompts/](../prompts/) | Analysis、Chat、Record、Report、Questionnaire、Profile、Knowledge 的主要提示词模板 | 修改域内提示词需核对调用它的工作流及版本；提示词目录不是独立 Agent 注册表 |
| [utils/](../utils/) | 标识、报告格式辅助、产物路径和日志；`log/` 是日志功能源码 | 通用函数不承接新的跨域业务编排；`log/` 与根目录 `logs/` 的输出用途不同 |
| [scripts/](../scripts/) | 回调运维、任务恢复、队列检查、报告预览及文档维护入口 | 按脚本说明显式执行；运维命令不是启动时自动运行的业务组件 |
| [tests/](../tests/) | 离线单测、手动 API 联调、压测及固定样例 | 新测试统一放这里，执行范围见第 7 节 |
| [docs/](../docs/) | `api/` 为契约，`architecture/` 为设计与状态，`integrations/` 为供应方资料，`product/` 为产品原始资料 | API 历史、设计草案和产品附件各有适用范围，不覆盖当前接口契约 |
| [wiki/](./) | 当前实现、使用方法、运维步骤和限制；本页是代码阅读入口 | 专题实现细节从本页链接进入，不复制多份同级权威说明 |

### 2.2 根文件

| 文件 | 作用 |
|---|---|
| [main.py](../main.py)、[server.py](../server.py) | `main.py` 配置日志并通过 `server.run()` 启动；`server.py` 还保留旧 Handler 和导入兼容面，真正的装配与进程运行已分到 `bootstrap/`、`runtime/` |
| [pyproject.toml](../pyproject.toml)、[requirements.txt](../requirements.txt)、[.python-version](../.python-version) | 项目和依赖元数据、pip 依赖入口、Python 版本；pytest 默认收集范围也由 `pyproject.toml` 定义 |
| [.env.example](../.env.example)、[开发模板](../.env.example.dev)、[测试模板](../.env.example.test)、[生产模板](../.env.example.production) | 可入库的配置模板；不代表某个部署的实际凭据或启用状态 |
| [.ruff.toml](../.ruff.toml)、[.gitattributes](../.gitattributes)、[.gitignore](../.gitignore) | Python 风格、文本换行与二进制属性、本地文件排除规则；测试和文档源码继续纳入 Git |
| [activate.sh](../activate.sh) | Bash 开发环境辅助：激活 Conda、设置当前目录为 PYTHONPATH、建立日志与输出目录；服务启动命令仍见 README |
| [README.md](../README.md)、[CONTRIBUTING.md](../CONTRIBUTING.md)、[AGENTS.md](../AGENTS.md) | 项目入口、贡献与验证规范、编码智能体的工作约束和必读顺序 |
| [TODO.md](../TODO.md)、[ISSUE.md](../ISSUE.md) | 已确认计划，以及问题、跨模块依赖、负责人和交付记录；历史验证记录按其基线阅读 |

Python 包中的 `__init__.py` 通常负责包声明、公共导出或兼容懒加载。判断实际实现位置时继续跟进导出的类，而不是只看包名；尤其留意 `modules/health_profile/application/__init__.py` 的公共门面。

## 3. 五个业务主 Agent 和关键业务文件

| 模块 | 主 Agent 与应用入口 | 内部执行和数据职责 | 专题 |
|---|---|---|---|
| Record | [agent.py](../modules/record/agent.py)、[application/service.py](../modules/record/application/service.py) | `creation.py` 协调建档创建；`flow.py`、`interview.py` 等维护阶段和问题规则；`document_context.py`、`documents.py` 管本域文档输入；`profile_context.py`、`profile_memory.py` 组织画像上下文 | [动态建档](record-dynamic-flow.md)、[正式文档消费](record-formal-documents.md) |
| Chat | [agent.py](../modules/chat/agent.py)、[application/service.py](../modules/chat/application/service.py) | 同一 Service 提供回复、SSE、建议问题、历史与 custom 消息；`profile_context.py`、`profile_memory.py` 负责本域画像上下文；`profile_source.py`、`profile_publication.py` 负责原消息来源与后台投递 | [Chat 主 Agent](chat-main-agent.md)、[来源装配](chat-source-composition.md) |
| Report | [agent.py](../modules/report/agent.py)、[durable.py](../modules/report/durable.py)、[pipeline.py](../modules/report/pipeline.py) | 持久受理与恢复由 Service 负责；Pipeline 组织来源、评估、证据与产物；`reports.py` 生成后续报告；`artifacts.py` 管产物边界；`delegation.py` 提供公开持久后续任务；`research.py` 为可选研究子 Agent | [Report 主 Agent](report-main-agent.md)、[研究循环](report-research-agent.md)、[公开委派](report-delegation.md) |
| Questionnaire | [agent.py](../modules/questionnaire/agent.py)、[service.py](../modules/questionnaire/service.py) | `generation.py`、`generation_policy.py` 管整卷生成；`factors.py`、`factor_tasks.py` 等管理因素评估；`submitted_material.py` 固定已提交材料；本域来源任务独立后台投递 | [问卷主 Agent](questionnaire-main-agent.md)、[本域规则](questionnaire-owned-policy.md) |
| Health Analysis | [agent.py](../modules/health_analysis/agent.py)、[application.py](../modules/health_analysis/application.py) | 三源同步读取、独立五步分析、生活目录覆盖、四项输出及成功前持久追溯；不创建Record会话或旧问卷任务 | [实现计划](../docs/architecture/health-analysis-implementation-plan.md)、[交接与验收](../ISSUE.md#i-047-健康分析b0前置交接与公共纯读依赖) |
| GutBarrier | [agent.py](../modules/gut_barrier/agent.py)、[service.py](../modules/gut_barrier/service.py)、[pipeline.py](../modules/gut_barrier/pipeline.py) | `rule_tools.py` 包装确定性规则；`assessment.py` 等完成专项计算；`publication.py` 固定主报告输出；`delegation.py` 管向 Report 的有界委派；主报告、主回调和后续解读分别保存状态 | [GutBarrier 实现与运维](gut-barrier.md) |

这些 `agent.py` 是业务主入口，不要求把已有 Service 的所有实现搬入同一个文件。例如 Chat 的 `sendMessage()` 进入 ChatAgent 后调用 Service 的内部聊天工作流，是当前复用方式。Record 的 `record.plan/record.question`、Chat 的 `chat.reply/chat.suggestions`、Report 的 `report.narrative/report.followup/report.retrieve_context` 是已注册内部模型能力，不能据此认定每一项都有自主规划循环。

Questionnaire 当前通过 [questionnaire_generation.py](../adapters/questionnaire_generation.py) 与 [questionnaire_factors.py](../adapters/questionnaire_factors.py) 的模型适配器调用 LLMGateway，没有 `agents/graphs/questionnaire/` 目录。GutBarrier 标准 [factory.py](../adapters/gut_barrier/factory.py) 未注入可选 narrative 整理器，主报告以固定规则流程为主；独立 Worker 经 [公开 Report Adapter](../adapters/gut_barrier/delegation.py) 委派后续解读。Report 保持与 GutBarrier 平级。

Analysis 保留独立文档解读 HTTP 用例，Profile 提供查询、治理及必要的提取/循证模型能力；这两者的模型 Graph 不表示新增了同一层级的业务主 Agent。健康检查位于 [web/handlers/health.py](../web/handlers/health.py)，直接读取平台运行状态。

## 4. 公共能力、装配与版本边界

### 4.1 设计名称与实际目录

| 设计名称 | 实际代码与文件族 | 所有权 |
|---|---|---|
| Document | [modules/analysis/](../modules/analysis/) 与 [adapters/analysis/](../adapters/analysis/)；`contracts.py` 定义 OutputSelection；`application/documents.py`、`document_reuse.py`、`output_profiles.py` 管处理、恢复与输出选择；`snapshots.py`、`materials.py`、`understanding.py` 管快照和解读；`formal_source.py`、`managed_source.py`、`publication*.py` 管显式正式来源和发布 | 提供共享材料、事实与版本化能力；当前没有 `modules/document/` 目录 |
| Profile | [modules/health_profile/contracts.py](../modules/health_profile/contracts.py)、[公共门面](../modules/health_profile/application/__init__.py) 与 [health_profile/](../health_profile/)；`snapshots.py` 只读快照，`source_publications.py` 接收来源，`source_extraction.py` 提取，`source_application.py` 治理和恢复，`repository.py` 持久化 | 唯一权威画像；各业务域拥有自己的 renderer，不再各自保存一套画像真值 |
| Knowledge | [agents/knowledge/](../agents/knowledge/)；`protocol.py` 为公开检索接口，`types.py` 为请求/结果，`evimed_gateway.py` 为具体网关，`routing.py` 为检索规划与结果上下文组织 | 提供 RAG 检索能力；结果由消费者验证并用于本域任务，当前没有 `modules/knowledge/` 目录 |

Record 的 [documents.py](../modules/record/documents.py) 和 Report 的 [document_projection.py](../modules/report/document_projection.py)、[document_rendering.py](../modules/report/document_rendering.py) 各自拥有业务输入组织。Report 新增解读需求优先修改本域投影；公共 Document 能力确需升级时，提供方交付新契约与真实实现，Report 显式接入，Record 保持其固定选择。缓存、首次受理清单和恢复使用同一版本，不隐式切换到 latest。

当前标准装配的新受理走 Record formal v2 与 Report formal 消费者；旧 prepared/fixed/legacy 路径保留实际实现并按已有任务版本选择。不要把 `compatibility.py`、`fixed_documents.py` 的存在理解为所有新任务仍走旧发布链，也不能直接删除它们。详见 [Record 正式消费](record-formal-documents.md)、[Report 正式消费](report-formal-documents.md) 和 [Document 装配](document-source-composition.md)。

### 4.2 装配文件分别连接什么

| 文件 | 职责 |
|---|---|
| [bootstrap/factory.py](../bootstrap/factory.py) | 主装配入口，连接模块、依赖、Agent 注册和后台恢复对象，返回 AppContainer |
| [bootstrap/document_sources.py](../bootstrap/document_sources.py) | 构造共享 Document/Profile 来源图及 Analysis、Record、Report 消费者，保持共享发布 gate 和唯一解析器 |
| [bootstrap/document_contracts.py](../bootstrap/document_contracts.py) | 公开兼容服务、材料读取和观察纯读资源的组装，复用已有数据库客户端 |
| [bootstrap/report_documents.py](../bootstrap/report_documents.py) | Report 固定文档消费者、私有投影缓存和兼容发布能力的组装；新 formal 路径及历史版本选择由上层装配连接 |
| [bootstrap/questionnaire_sources.py](../bootstrap/questionnaire_sources.py)、[record_sources.py](../bootstrap/record_sources.py)、[chat_sources.py](../bootstrap/chat_sources.py) | 各域原始来源、材料 reader、准入证明和后台投递能力的独立装配；先注册读取能力，再连接 Profile 和发布 Worker |
| [adapters/report/factory.py](../adapters/report/factory.py)、[main_agent.py](../adapters/report/main_agent.py)、[gut_barrier/factory.py](../adapters/gut_barrier/factory.py)、[questionnaire_factory.py](../adapters/questionnaire_factory.py) | 域内具体依赖工厂，由组合根调用；不构成另一个全局主控 Agent |

画像读取在业务域组织上下文；画像更新则在业务保存后由本域来源任务持久投递到公共 Profile。`runtime/application.py` 调度周期，`runtime/source_recovery.py` 防止同一周期重叠，领域 Service/Store 负责领取与恢复语义。业务保存成功、来源投递成功和画像摄取完成是不同状态。Chat 的 [source_store.py](../adapters/chat/source_store.py) 实现本域来源存储适配，支持后台扫描与投递状态管理。

### 4.3 Agent 平台关键文件

| 文件或目录 | 作用 |
|---|---|
| [agents/contracts.py](../agents/contracts.py)、[ports.py](../agents/ports.py) | 统一请求、结果、事件、版本选择及 AgentPort；WorkflowAgentPort 包装固定工作流，BoundAgentPort 调指定 Graph |
| [agents/runtime/container.py](../agents/runtime/container.py)、[application.py](../agents/runtime/application.py) | 共享 AgentRuntime 与生产能力注册；模块注册暂存、失败隔离和资源边界 |
| [agents/runtime/registry.py](../agents/runtime/registry.py)、[ports.py](../agents/runtime/ports.py) | Graph 与普通 AgentPort 的精确版本注册；未知选择明确失败 |
| [agents/runtime/context.py](../agents/runtime/context.py)、[agents/hub.py](../agents/hub.py) | 本次运行身份/上下文，以及明确指定能力的调用入口 |
| [agents/graphs/](../agents/graphs/) | 按 Chat、Record、Analysis、Health Profile、Report 分组的模型工作流；`registration.py` 注册能力，`graph.py` 等实现流程，`profile.py` 定义模型策略 |
| [agents/llm/aion_gateway.py](../agents/llm/aion_gateway.py)、[tool_gateway.py](../agents/llm/tool_gateway.py) | AION 模型请求适配和受控工具循环；`profiles.py` 是模型策略，不是用户健康画像 |
| [agents/tools/contracts.py](../agents/tools/contracts.py)、[ledger.py](../agents/tools/ledger.py) | 工具协议、父子运行预算、权限、取消和回执；不意味着所有固定业务流程已改为自主工具循环 |
| [agents/persistence/](../agents/persistence/) | LangGraph checkpoint、模型上下文、lease 与工具执行账本；这些状态与业务 Session、报告产物及画像事实各有所有者 |

`modules/report/research.py` 定义研究子 Agent，`research_integration.py` 把研究结果交回父流程，`adapters/report/research.py` 负责按配置装配。正常检索仍有证据缺口时才补查；代码默认关闭，原生工具循环只有有界执行和完成回执，不承诺逐步 checkpoint 恢复。具体边界见 [Agent 工具运行](agent-tool-runtime.md) 和 [Report 研究子智能体](report-research-agent.md)。

## 5. 沿三个请求阅读调用链

以下是定位源码的阅读顺序，省略了内部校验和错误分支；完整 HTTP 字段、状态和 SSE 协议仍查 API 文档。

### 5.1 普通查询：聊天历史

```text
POST /api/v1/chat/history
  → web/routes.py
  → web/handlers/chat.py：ChatHistoryHandler
  → modules/chat/application/service.py：getHistory
  → adapters/persistence/sessions.py：读取已有业务 Session
  → HTTP JSON
```

这条路径不进入 ChatAgent、不启动模型，也不依靠查询来驱动画像恢复。Report 的列表和详情同样直接使用 [report_query.py](../web/handlers/report_query.py) 注入的产物存储能力；这里与 Report 的模型上下文选择能力 `report.retrieve_context` 是不同用途。

### 5.2 AI 用例：生成一次 Chat 回复

```text
POST /api/v1/chat/send/complete
  → web/handlers/chat.py：ChatSendCompleteHandler
  → modules/chat/application/service.py：sendMessage
  → modules/chat/agent.py：ChatAgent
  → Service 的内部聊天工作流：会话门禁、资料与画像上下文
  → agents/hub.py → agents/graphs/chat/graph.py
  → 按需 Knowledge 检索 → agents/llm/aion_gateway.py
  → 保存业务消息与来源标记，登记本域持久来源任务
  → HTTP JSON
```

SSE 入口复用本域工作流及流式适配，数据保存和成功结束约束见 [Chat 主 Agent](chat-main-agent.md)。画像候选的提取和应用由后台继续执行，普通回复不等待画像摄取完成。

### 5.3 持久任务：生成 Report

```text
POST /api/v1/report
  → web/handlers/report.py
  → modules/report/durable.py：submit 持久受理
  → HTTP processing

runtime/application.py 周期调度
  → DurableReportService 领取任务
  → modules/report/agent.py：固定执行清单
  → modules/report/pipeline.py：来源、评估、证据与生成
  → 保存报告产物和独立交付状态
  → ReportCallbackWorker 读取固定内容并投递/退避重试

POST /api/v1/report/list 或 /get
  → web/handlers/report_query.py → 已有产物/交付数据
```

生成状态与回调状态分开；回调失败不要求重新生成已完成正文。旧任务保留其固定中断策略，不能把“后台恢复”解释成所有模型步骤都可任意重跑。GutBarrier 使用本域 Service/Store 和规则工具生成主报告，主回调与委派 Report 后续解读是独立任务路径；本项目没有新增 GutBarrier 查询路由。

## 6. 修改需求时先找哪些文件

| 需求 | 首先定位 | 同步核对 |
|---|---|---|
| HTTP 参数、响应或 SSE 行为 | `web/routes.py`、对应 `web/handlers/` | `docs/api/API.md`、`tests/unit/server/` 或 `tests/unit/web/`；不要把业务流程搬入 Handler |
| 本域流程、评分、提示词 | `modules/<域>/`、该域 `prompts/` 与实际模型 Adapter/Graph | 本域测试和 Wiki；版本清单、已有任务及降级行为 |
| 模型执行、工具预算或 Graph 注册 | `agents/` 和域内调用入口 | `tests/unit/agents/`、领域用例测试；区分通用机制与业务规则 |
| Report 新增文件解读需求 | `modules/report/document_projection.py`、`document_rendering.py`、本域 Adapter | 固定 Document 选择；公共能力不足时登记依赖，由提供方独立升级 |
| 画像事实规则或来源协议 | `modules/health_profile/` 公共契约与 `health_profile/` 实现 | 本域来源 Adapter、修订/撤回、幂等接收与治理测试；消费上下文的排版先改本域 renderer |
| 数据库、外部服务或注入关系 | `adapters/`、`db/`、`bootstrap/` | 存储与组合测试、资源所有权；不要让查询构造模型依赖或新客户端 |
| 任务恢复、停服、回调补投 | 本域 Service/Store、`runtime/` | 租约、中断和固定产物测试；Worker 是内部后续工作，不是新的外部 API |

同类文件名只能帮助定位，最终以实际调用为准：`ports.py`/`contracts.py` 定义边界，`schemas.py` 定义数据结构，`service.py`/`pipeline.py` 组织用例，`agent.py` 提供本域主入口，`store.py`/`repository.py` 通常负责存储，`factory.py` 组装依赖，`profile_*` 在业务模块中通常表示画像消费或来源。Report 的 `evaluation_*`、GutBarrier 的 `rules.py`/`risk_index_rules.py` 等是本域细分规则，沿 Pipeline 调用查找，不以文件数量推断 Agent 数量。

## 7. 测试、文档和本地目录

正式测试代码统一在 [tests/](../tests/)，详细结构和命令见 [tests/README.md](../tests/README.md)：`unit/` 是 pytest 默认离线范围，`unit/_support/` 保存替身与共享验收过程，`api/pages/` 是浏览器联调工具，`api/scripts/` 与 `api/integration/` 需显式连接真实依赖，`loadtest/` 是压测，`fixtures/analysis/` 是需版本管理的固定回放材料。不要按“是否部署到生产”删除测试源码或固定样例。

`docs/api/history/` 为已发布快照，`docs/api/drafts/` 为草案；`docs/architecture/` 的状态查[架构索引](../docs/architecture/README.md)；`docs/integrations/` 和 `docs/product/` 保存外部接口及产品参考，包括有用途的 JSON、图片和 Office 附件。当前实现教程与运维步骤在 Wiki。新增目录、关键入口或职责变化，应同时更新 README 目录树、本页相应映射和相关专题导航，避免只新增文件而不交代调用位置。

下面这些是本地可选目录或文件，不是需要从 Git 拉取的业务模块；不存在时不必手工创建全部目录。忽略规则以 [.gitignore](../.gitignore) 为准。

| 本地路径 | 用途与管理 |
|---|---|
| `tmp/`、`temp/`、`test_files/` | 临时材料或本地测试输入，不提交；Git 不跟踪空目录 |
| `api_test/` | 旧验收工具沿用的输入/产物位置，例如可选 callback 样例与 `out/`；可执行测试入口已迁入 `tests/`，当前整目录忽略 |
| `logs/`、`outputs/`、压测 `results/`、覆盖率产物 | 运行和验收输出，不等同于日志源码或固定 fixture |
| `build/`、`dist/`、`*.egg-info/`、Python/pytest/Ruff 缓存 | 构建与缓存产物，可由对应工具生成，不提交 |
| `.env*` 私有配置、IDE/本地 Agent 配置 | 私有环境与个人工作区；只有 `.env.example` 和 `.env.example.*` 模板纳入 Git，自动化不读取私有环境文件 |

部分报告工具通过 [utils/artifact_paths.py](../utils/artifact_paths.py) 或 [scripts/artifact_paths.py](../scripts/artifact_paths.py) 强制把诊断/医疗产物写到仓库外；不能因为 `tmp/`、`api_test/` 已忽略，就绕过这些工具的输出限制。`scripts/report_callbacks.py`、`recover_qa_creation.py`、`check_gut_barrier_queue.py` 是运维入口；`generate_gut_barrier_report.py`、`render_gut_barrier_callback.py` 是显式生成/预览入口；`format_markdown.py` 与 `check_env_example.sh` 用于维护文档和配置模板。执行前查看对应规范和 `--help`，不在阅读架构时顺带调用真实服务。

## 8. 新开发者的建议阅读顺序

1. 读 README 的环境、启动、目录树，再读本页的分层与五域表。
2. 从 `web/routes.py` 选择一个实际用例，例如 Chat 历史查询，跟到本域 Service 和存储接口。
3. 再跟一次 Chat 回复或 Report 持久任务，对照 `agent.py`、内部 Graph/Adapter、结果保存和后台交付。
4. 回看 `bootstrap/factory.py` 和本域工厂，确认依赖从哪里来、哪个对象负责关闭资源。
5. 阅读对应 Wiki、测试和 CONTRIBUTING；涉及共享能力时先确认契约、版本及提供方职责，再修改实现。
