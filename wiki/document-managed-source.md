# Document 托管来源纠正与补充

本提供方为已完成真实 fresh 准入、且首个完整来源已应用的 canonical source 提供固定版本的后续命令。它负责当前有效贡献集合、退休证据、兼容历史归档和持久发布，不修改 Record、Report、Analysis 的默认装配，不把旧来源自动迁入，也不把归档成功视为画像已经应用。

此前的 [完整来源与 fresh 准入](document-complete-source.md)、[来源生命周期](document-publication-lifecycle.md) 和 [旧 Report 发布命令](document-report-publication.md) 保留各自语义。`document-managed-source-v1` 是独立执行修订，不能在 `report-fixed-documents.v1` 或旧兼容 Publisher 中隐式换用。

## 公开入口

`adapters.analysis.managed_source.create_document_managed_source_runtime` 显式接收既有 compatibility、report_publications、material_reader、source_admission、publication_runtime、Profile 公开 archive 与 Document command_records。返回：

| 入口 | 行为 |
|---|---|
| `lifecycle.read(scope, source_id)` | 纯读来源模式、发布水位和当前有效 snapshot；pending、retracting、withdrawn 不提供有效 ref |
| `reports.prepare(scope, inputs, revision)` | 使用固定材料和真实 v16 算法产生可复用输出，不归档、不发布 |
| `reports.read(scope, snapshot_id)` | 纯读固定输出、实际 outputTraceId 和有序输入清单，不重新提取 |
| `commands.execute(command)` | 执行显式更正或补充退休，持久归档证明、active/retired 计划与真实发布回执 |
| `commands.read_receipt(scope, command_id)` | 纯读原命令回执；后续更正或撤回后仍可读取历史证明 |
| `source_reader` | 公开完整 source、inventory 和 retraction 纯读，供 Profile resolver 使用 |
| `publisher.retract(...)` | 既有整源救援撤回，不受普通纠正历史预算限制 |

构造只组装资源，不创建客户端、索引、后台任务或模型调用。command_records 必须使用 PRIMARY 读取、majority 写入、唯一 `_id`、无 TTL；快照使用已有不可变分块存储。所有旧正式入口、fresh 准入和新 Publisher 必须共享同一 Document ledger，工厂对对象关系进行校验。部署仍需提供既有存储权限、耐久性和独立恢复 Worker。

根装配先调用 `create_document_managed_source_readers`，只注入 Document compatibility、report_publications、ledger、admission_records、command_records，取得供 Profile 注册的 source_reader 和 owner_fence_reader。此工厂不依赖 Profile Inbox、准入或 archive；返回的两个 facade 只公开纯读方法，内部历史验证没有命令侧 Profile ports。随后才构造真正 Profile resolver/Inbox/fresh/archive，再创建 fresh 命令与 managed runtime。前后共享同一持久集合与 ledger，已注册 reader 可读取之后产生的固定证明，不需要后装代理或替换 resolver，避免装配环。

调用者先持久保存 `DocumentPreparedPublicationInput`，其中包含 material/facts/understanding 三个固定 ref、明确 selection 和原 preparation receipt。首次准备可调用公开 `compatibility.prepare_bundle`；恢复只读取此前固定 bundle，不调用最新解释器填补旧结果。

```python
from modules.analysis.managed_source_contracts import (
    MANAGED_SOURCE_REVISION,
    DocumentManagedSourceCommand,
)

state = await runtime.lifecycle.read(scope=scope, source_id=canonical_source_id)
if state.status != 'active':
    raise RuntimeError('先恢复或结算当前来源')

command = DocumentManagedSourceCommand(
    scope=scope,
    source_id=canonical_source_id,
    request_id=persisted_request_id,
    prior_publication_revision=state.publication_revision,
    prior_source_snapshot_ref=state.active_snapshot_ref,
    operation='replace_base',
    inputs=(fixed_input,),
    command_revision=MANAGED_SOURCE_REVISION,
)
# 调用前把完整 command 持久到本域任务；重试复用同一 command。
receipt = await runtime.commands.execute(command)
```

`request_id` 只在 scope/source 内标识命令；同 ID 改变任何输入或执行修订均拒绝。命令选择的上一来源必须是当前 exact intent 自己已经返回 applied 回执的完整来源，不能用较大的汇总水位代替。当前仅支持真实 fresh 起点，已有历史来源仍需独立的完整迁移证明。

## 当前有效贡献与退休规则

| operation | 输入与效果 |
|---|---|
| `replace_base` | 一个固定 bundle；替换基础中立观察，并退休实际依赖旧材料或旧 understanding 的 Report 补充 |
| `replace_report` | 1 至 100 个有序固定 bundle；首个文档拥有 canonical source；更新相同有序文档集合的 v16 补充槽 |
| `retire_report` | 明确选择当前补充槽的完整有序固定输入；只退休该槽，不重新提取、归档或生成新 Report |

材料 ref 未改变而 understanding ref 改变也属于真实输入变化。基础更正保留无关补充，拒绝用新基础解释冒充旧 Report 输入；Report 必须在自己的任务中按新输入重新调用公开准备命令。Document 不在纠正时暗中重跑 Report 模型。

同一固定基础输入的重复请求是 no-op，新的请求 trace 不会使已存在补充被退休。中立 payload、实际依赖或版本变化仍是合法纠正，即使原 PDF/OCR ref 不变。相同旧内容在 A → B → C 后重新成为有效内容时使用新的 contribution incarnation，不能将退休 ID 复活。

每次真实 transition 保存上一 event、上一已应用回执、上一计划、替换贡献、真实归档回执和不可变退休证据。退休证据记录原因及旧/新 material、understanding 依赖。新 inventory 只列当前 active 贡献；retired 仅用于审计。A → B 的证据在 B → C 后保持原样；可选 successor 字段只指当前有效贡献，因此 A 的该字段变为 `None`，不会伪造 A 直接由 C 替换。

材料 `sourceRevision`、旧归档 `legacySourceRevision`、事件 `publicationRevision` 分别表示实际材料版本、旧不透明整数身份、发布顺序。两次不同解读可以保留真实材料版本 1，同时生成不同 digest 和连续发布修订；不能把 publicationRevision 写成材料版本来凑一致。当前公共完整来源仍要求所有材料修订一致，混合材料版本明确拒绝。

## 固定 v16 输出与兼容归档

`reports.prepare` 必须显式传入 `document-managed-report-v16-v1`，只使用注册的真实 `report-source-v16`、固定原件 bytes、真实解释版本及固定公开内容投影。它直接执行同一 extractor 与 normalizer，不调用会自动正式发布的旧 coordinator.process。新 cache、trace 和准备契约独立于旧缓存键；不修改旧 `publicationBaseKey` 或旧恢复行为。

完整受理 manifest 保留输入顺序和重复项；实际提取按首次 documentId 去重。同一文档选择互相矛盾的 fixed refs 会在提取前失败。每个输入原件均按不可变 material ref 验证，不读取当前 index 选择 bytes。公开 interpretation 转换冻结为 `document-understanding-public.v1`，剔除私有 evidence 后与旧 public_result 语义相同；归档 source-content 仍保留其证据。内部警告映射也固定，后续全局 helper 或文案变化不影响此修订。

准备结果保存真实 `outputTraceId`、projection、processingStatus、全部输入与材料引用。调用 execute 时复用同一固定准备结果，Report 消费者可直接使用返回 projectionRef 构建自己的私有投影，无需第二次提取。partial/unavailable 输出可以用于本域业务降级，但不能归档为完整补充或发布 accepted 来源；旧版本 raw fallback 仍由独立消费者策略处理，不能借此提升质量。

正式 execute 通过 Profile 公开 `archive_source_material` 保留旧历史读取所需的基础与补充记录。该命令只归档，不 enqueue、不创建旧 observation，也不绕过 legacy gate。基础记录及基础更正沿用真实旧身份算法；托管 v16 补充使用明确的 `managed-report-v16` source key，回执返回真实归档 opaque revision，不伪称旧 coordinator 的 publication key。档案保留不代表该贡献仍有效，画像唯一正式摄取路径是完整来源 Inbox。

本命令只拥有首个 canonical source。组合附件中其他文档的独立基础来源仍由消费者逐个显式处理，不能以首源的一个组合事件替代所有附件来源；每源结果和失败必须分别持久。Report 先按每源准备/纠正基础，再针对首源调用组合补充，具体执行编排属于 Report 独立迭代。

## 并发、恢复与边界

最后发布使用共同 ledger 的 expected-prior CAS。两个并发更正只有一个可推进来源；失败候选可能已有兼容历史归档，但不会进入当前有效 inventory。整源撤回在候选计算或归档期间仍可获受理，稍后的纠正 CAS 必须失败，历史归档或旧回执不能复活 withdrawn 来源。

归档 ACK、事件 ACK 或本地最终回执 ACK 丢失后，重试读取同一持久命令与计划、重投同包，绝不读取 latest 或重新把当前 absence 当作历史证明。Profile received、applied、settled 和撤回水位仍由原公开回执分别表达；命令返回 published 只表示事件已建立/发送，不能向业务宣称画像已应用。Profile 失败重驱动与 Document 传输恢复保持各自公开边界。

单个 input manifest 最多 100 项；单贡献最多 1000 个观察及 4,000,000 字节；归档请求最多 4,000,000 字节。普通 managed transition 只接受 prior publication revision 小于 100，历史读取同样有界；超限明确失败，需要独立治理迭代，整源撤回仍可用。此版本没有无限自动重试，也没有隐式跨集合事务。

## 后续生产接线

| 调用域 | 独立接线责任 |
|---|---|
| Record | 固定本域新 consumerRevision，首次 fresh 命令/后续 replace_base，持久材料与真实归档、事件回执；旧固定任务继续旧语义 |
| Report | 为每个附件保存来源处理结果，使用同一次固定 v16 输出构建私有投影，再提交首源补充；partial 不正式发布 |
| Analysis | 覆盖默认正式解读入口的 legacy/managed 选择，对已有托管来源实际调用公开更正命令 |
| 根装配 | 注册新的完整 source_reader/原 retraction reader，保持共享 ledger、Profile resolver 与独立恢复 Worker 一致 |
| Questionnaire | 后续附件兼容 Publisher 先消费 Q 公开 submitted-material 固定证明；本模块不读取 Q 私有归档或集合 |

当前交付仅提供方，不默认启用上述消费者，不支持把未知旧历史自动判为 fresh。Record 切换前必须同时满足 Report 与 Analysis 访问同 canonical source 的公开命令支持；否则旧默认入口会被合法的 legacy gate 拒绝。

离线回归组合真实 Document 准备、固定原件、v16、中立映射、Profile fresh/resolver/archive/Inbox/application 和 Document recovery，持久存储使用实际 BSON 编解码替身。验证覆盖基础纠正、输入 ref 不变的解读更正、贡献退休、A → B → C、撤回竞态、并发 CAS、ACK 重放、纯读与工厂零 I/O；不等同于真实 MongoDB、线上模型或生产切换验收。
