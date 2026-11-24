# Document 正式来源的默认装配

本文描述已接入默认路径的 `bootstrap/document_sources.py`，本地实现与离线验收不代表已部署。三个消费者使用同一个 Document/Profile 公共依赖图，普通 HTTP 查询不创建该图的任务或调用模型。构造仅登记依赖，不访问存储、启动 Worker 或建立新客户端。

## 依赖与所有权

根先从既有 Document 集合派生永久来源账本，所有默认处理器和旧兼容发布网关共用同一个 ledger 对象。随后构造固定 compatibility、材料读取、旧 Report 发布器、Document 纯来源 reader 与 owner fence；这些纯 reader 先注册到 Profile，再构造准入、归档、managed 命令与 Formal 服务。没有先放可变代理、再填充 Profile 依赖的环。

Profile 完整来源与撤回使用同一个 resolver，默认画像摄取 Worker 接收这份 resolver 表。它内部已包含公开事件 Worker；不再额外调度 `profile_contracts.worker`，避免旧的空 resolver 消费入口先领取新来源。Profile 仍是权威事实来源，各域通过已有本域 renderer 使用结构化快照。

Document 新准入的 presence 与授权分别使用 Profile 公开服务；ready 状态由公开 Inbox 读取。快照、来源账本、准入证据、managed 命令和 Formal 命令仍归 Document，不由消费者直接写入。

## 三个固定消费者

- Record 新受理选择 `record-document-formal-preparation.v2`，其 base 固定 `document-formal-source-v1`。旧准备 V1 与 legacy 由真实 previous port 继续处理，创建 checkpoint 不换版本。普通 pending 等待真实归档，已有首问所需的真实修订不伪造。
- Report 新受理选择 `report-formal-documents.v1` 与 `document-formal-source-v2`，复用固定 v16 业务投影。完整执行清单选择具体工厂；旧 `report-fixed-documents.v1` 和其不可用配置清单仍保持实际实现。任务内 ContextVar 隔离并发版本，默认变化不替换已受理任务。
- 独立 Analysis 使用 `analysis-formal-source-v1`。解析前保存本域意图，HTTP 取消或超时不删除该意图；后台等待原处理器完成，再固定快照并处理正式来源与别名。不会为恢复再次启动 OCR 或模型。

显式自定义处理器、旧文档 Port 或画像 Worker 注入保留已有兼容装配，不据此启用部分新默认。只要使用真实 Runtime 与默认处理器，旧消费者工厂也得到同一个来源 gate；切换消费者版本不能绕过已托管来源。完全自定义处理器由其装配方负责自己的依赖与版本，不通过修改该对象偷偷安装 gate。

## 后台与关闭

新增三个由现有进程监管的恢复入口：Document Formal、Document outbox、Analysis delivery。每五秒进行一次有界扫描，并在启动时登记首轮扫描；同一进程内运行未结束时跳过下一次重叠调用。跨进程仍以各提供方持久租约和 CAS 裁决。

Document outbox 保存本进程的两个 seek 游标，各自扫描到末尾后回绕；失败保留旧游标，进程重启从持久记录首轮扫描，不把游标当执行 checkpoint。Formal 与 Analysis 按固定任务的期限、版本及累计预算恢复。业务查询不会唤醒这些周期。

关闭沿原过程先停止周期与新领取，再等待监管中的任务排空；根只关闭其登记的共享资源。没有新增线程服务、消息中间件、HTTP 路由或独立微服务。

## 迁移边界

Document 三个消费者与后台链路先完成验收，随后 [Questionnaire](questionnaire-source-composition.md)、[Record](record-source-composition.md) 默认来源和独立周期已分别接入；[Chat](chat-source-composition.md) 默认来源和周期也已接入并完成离线组合验收。三域先提供纯 reader/fence，再构造同一个 Profile 图；业务来源任务不创建第二个 Profile 摄取 worker。已有 legacy 来源保留原身份，只有验证为新来源的材料才进入 fresh 准入；不自动迁移历史来源，不双链发布。

派生集合使用既有数据库客户端和配置前缀，不运行 DDL、不添加 TTL。离线真实组件组合覆盖构造零 I/O、共用 gate、fresh/legacy 路径、版本分派及后台调度；真实 Mongo 并发、提供商模型质量、外部回调和部署仍单独验收。

相关说明：[正式来源编排](document-formal-source.md)、[Record 正式消费](record-formal-documents.md)、[Report 正式消费](report-formal-documents.md)、[Analysis 固定意图](analysis-formal-source.md)。
