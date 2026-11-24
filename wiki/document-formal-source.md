# Document 正式来源公开编排

本文描述可并存执行的 `document-formal-source-v1` 与显式 Report 复用修订 `document-formal-source-v2` 提供方。它把固定准备、legacy 兼容、真实 fresh 准入、managed 更正和 Report 补充协调放回 Document 模块，业务消费者不用各自调用 Profile 的准入与归档协议。三域消费者、[默认根装配与后台注册](document-source-composition.md) 已接通，不代表真实环境已经部署。

依赖已独立交付的 [完整来源](document-complete-source.md)、[受管理来源](document-managed-source.md) 和 [Profile 准入](profile-source-admission.md)。旧 `publish_prepared`、`publish_report_source` 及其缓存、契约不变，新入口必须显式选择独立执行修订。

## 公开接口

`DocumentFormalSourceCommand` 固定 scope、requestId、operation、完整有序 `DocumentPreparedPublicationInput` 数组与 commandRevision。operation 为 base 或 report；base 恰好一个输入，report 为 1 至 100 个输入。requestId 是同 scope 的永久幂等身份，相同 ID 换输入、操作或版本明确拒绝。

| 方法 | 行为 |
| --- | --- |
| `submit(command)` | 只在 Document 本域持久保存命令、固定输入与策略，返回 pending；不建主体、不调用 Profile、不提取或发布 |
| `execute(command)` | 显式 submit 后按已接受策略有界推进，至多两次领取、16 个持久阶段，推进时段不超过两倍固定单步超时（不含持久受理与最终纯读）；遇等待或他人持有租约即返回，不 sleep 等画像应用 |
| `prepare_report(command)` | 从固定原件和输入生成或复用同次 v16 业务投影；不发布、不等待画像应用，不代替正式 submit |
| `read_report_projection(scope, projection_ref)` | 纯读固定业务投影；校验归属与契约，缺失明确失败，不重新提取、发布或恢复 |
| `read_receipt(scope, command_id)` | 纯读已有状态与结果；不恢复、不生成、不调用 Profile |
| `recover_pending(limit=100)` | 内部 Worker 扫描、领取并推进持久阶段；不新增外部 HTTP 入口 |
| `advance(command_id)` | 内部有界推进同一持久命令，作用与后台领取相同；普通数据查询不可调用 |

调用者须先持久自己的准备回执和输入版本，再 submit。接收回执不是正式发布成功，也不是画像应用完成。pending、失败和完成回执均返回已持久的 completedBaseCount、totalUniqueSources、按原首次源顺序完成的真实基础回执与已有投影引用；completedBaseCount 统计已获得处理结果的来源（包括 not_published），并不表示画像应用成功；未完成源不填虚构修订。archive 来源的实际 canonical/opaque revision 读取真实归档回执，单独标记 source_managed 和 jobId=None，事件状态另列，不伪装旧队列或应用成功。最终 result 保留每源基础回执、业务投影引用和独立 Report 补充回执；legacy 不透明修订、材料修订与事件发布顺序继续分开。

`prepare_report` 可以先完成内容准备，让 Report 继续生成业务结果；正式基础来源与补充在后台分别推进。新的消费者需要显式保存 commandId 并展示相应状态，不能在旧 consumerRevision 内偷偷换用该入口。

## 路由与固定阶段

先验证所有准备回执、材料/事实/解读引用及归属，再执行任一正式来源写入。同一 canonical source 的不同固定输入不能在一条命令内冲突；完整输入顺序与重复项保留，正式基础操作只处理首次出现的同源。

| 来源状态 | 选定行为 |
| --- | --- |
| legacy 且完整 v12 输入、两侧全历史 absence 可证 | 固定 fresh 路径，后续再次由真实准入提供方核验；竞态失败不会改走旧链 |
| legacy 且已有历史、历史不可用/截断、非完整输入 | 固定 legacy 兼容路径，原 Publisher 继续决定质量、保存与队列结果；未知历史绝不充当 fresh 资格 |
| legacy 且公开 Profile Scope V1 不支持既有长身份 | 在任何 Profile 调用前固定 legacy 兼容，不截断 userId、不新增 HTTP 限制 |
| events 且已应用 | 固定 exact prior publication revision/ref，再执行 managed base 更正；不同先前状态明确冲突 |
| 已受理的 admission/events 尚未应用 | 保存等待的具体发布修订，后台等待；不能读取后来更大的修订后自动覆盖它 |
| 撤回中、已撤回，或 managed 输入质量不足 | 保留业务输入/投影并返回 not_published，不回旧 sink、不复活来源 |

新命令仅按固定实现选择一次路由。Profile 暂时不可用不会把已经进入 fresh 的命令切回 legacy。历史来源迁移和全局 Scope 新契约仍是独立能力；本入口不扫描历史后伪造已排空证明。

fresh 阶段为：固定计划 → 完整来源 capture → 持久授权信封 → reserve → authorize → 真实旧历史归档 → 激活 Document events gate → 显式持久发布。每次跨阶段先保存稳定输入、引用或授权，重试沿原阶段执行。所有旧入口、准入和新 Publisher 共享同一个来源 ledger。

managed 阶段先保存包含 prior/ref 的完整命令再执行；结果回执丢失时用原命令重放。Report 首次 fresh 基础发布后，补充只等待该基础自己的 applied 回执，期间业务投影已可用。任何更新或撤回抢先推进来源，等待中的旧 Report 都不能改用新 prior 继续覆盖。

## 混合附件与 Report 补充

Report 每个独立附件先保存自己的基础来源结果。首个 canonical source 属于 events 时，使用公开 managed 命令；它复用同一次 `document-managed-report-v16-v1` 准备输出，正式 projectionRef 必须与业务准备一致。

首源是 legacy 而后续附件已是 events 时，使用独立 `formal-fixed-bases-v1` 内部绑定：明确区分真实 legacy receipt 和 managed archiveRef，重新验证固定准备与实际历史归档，然后复用 Document 原有 outcome 和 v16 coordinator。managed 次要附件只作已有固定材料，不重新进入旧基础发布入口。原 v16 输出缓存、publication key、source key 与 opaque revision 算法继续执行；旧接口本身不变。

此路径生成新的 `document.formal-legacy-report-source.v1` 快照及独立回执，不把 archiveRef 冒充旧写入回执。完整有序输入仍保存；同源重复项只在真实执行中去重。业务内容与正式发布均可追溯到固定材料，Report 自有投影/缓存仍由 Report 消费者负责。

partial/unavailable 不提升为完整 neutral 来源。legacy 输入沿原兼容行为处理；managed 不完整输入只保留业务材料，拒绝正式替换。旧输出算法需要的原件与已保存 OCR 继续复用，不再次运行文档解释模型。

## Formal v2 的固定 Report 输出复用

新 Report 消费者可显式保存 `commandRevision=document-formal-source-v2`；它仅支持 report 操作。旧 v1、默认版本常量和旧 `publish_report_source` 都保持可执行，已接受命令不能换版本或自动选 latest。两版继续使用相同公开方法，v2 不新增 HTTP；默认 Report 的 v2 选择已由[组合根](document-source-composition.md)独立接入，不改变已受理 v1 命令。

v2 的首源为 legacy 时，将已经持久化的 `document-managed-report-v16-v1` 投影用于正式补充，冷缓存只执行一次固定 v16 提取。它先验证投影 manifest、scope、有序固定输入、基础历史回执与实际原件。生产 `report-source-v16` 的临床读取仅消费报告的临床字段；归档注入的 reportId、userId、subjectId、documentId、sourceRevision 不影响该固定提取。v2 只在剥离这五个身份字段后报告内容完全相等时复用；其他临床字段、原件、文件元数据、inputSnapshot 和顺序继续精确绑定。这项等价规则不适用于任意同名提取器或新版本。

完整旧 outcome 仍进入原 cache identity，包括实际旧 sourceRevision；旧 publication/source key 与 opaque revision 算法不变。缓存已存在且输出相同时保留原缓存及其执行来历；输出不同则保存来源失败，不能覆盖旧缓存或把旧回执声称为本次准备输出。固定业务投影仍可纯读，Report 可以保留正文并单独展示来源状态。基础临床内容不一致同样明确拒绝，不再提取、读 latest 或伪造回执。

新入口复用原租约/CAS、持久 payload checkpoint 和正式 legacy sink。输出写入前后丢失确认均沿固定准备恢复，迟到执行者不能更新后继租约。发布创建真实独立 audit trace，记录 preparedProjectionRef、preparedOutputTraceId、固定复用修订及被排除的归档字段；原准备 trace 才是执行 v16 的那次记录。复用不会编造 output_raw 或声称对包含旧 sourceRevision 的输入又做了一次提取。并发命令及旧发布入口继续复用同一缓存身份和真实修订。

## 持久恢复与预算

新任务保存期限、租约、单步 I/O 超时、重试间隔与累计传输失败上限，不因重启重置。当前默认总期限 24 小时、租约 120 秒、每步 20 秒、等待间隔 30 秒、累计传输失败 8 次；每次领取最多推进 8 个持久阶段。普通步骤完成不清空累计失败预算。

持久 CAS 同时检查版本、owner、leaseGeneration 和租约有效期。过期执行者不能写结果或释放后继租约。取消本次执行保留原阶段和未知外部执行状态，后续恢复不新建业务命令；技术取消不宣称撤销已受理来源。

达到任务期限或失败预算时，命令保存失败终态，固定证据与进度保留。已形成的 Document outbox、Profile Inbox 和既有回调各自继续原恢复政策，不受此任务预算停止影响。当前没有通用自动重开失败任务能力；未知旧版本、固定身份/版本/归属冲突明确失败，不能通过创建新 ID 代替原准入证明。

来源尚未应用是持久等待，不是传输失败；查询不唤醒等待。实际 Mongo 并发、网络 ACK 故障和线上 Worker 周期仍须环境验收。

## 装配与验收

`adapters.analysis.formal_source.create_document_formal_source_service` 只组装，构造零 I/O。根先按 managed 提供方顺序注册纯读来源/fence reader，再创建 Profile 实际服务与 Document 命令，最后注入本正式入口；不得用临时授权占位或后改 resolver。

formal command 使用独立 Document 集合、PRIMARY 读取、majority+j 写入及唯一 `_id`，无 TTL。固定材料继续使用不可变分块快照，命令记录只保存引用、授权和紧凑回执；不把报告原文复制到单条任务记录。它不创建客户端、索引或 Worker；组合根负责生命周期及后台注册。

离线验证组合实际 Document/Profile 准入、归档、Inbox、应用与恢复实现，存储采用真实 BSON 编解码替身。覆盖业务提交/查询零 Profile 调用、fresh/legacy/managed、同次报告投影与等待、ACK 丢失、撤回竞态、混合附件、长身份兼容、重复准备回执、租约、期限、取消及未知版本。离线通过不替代真实存储并发、模型质量或部署验收。
