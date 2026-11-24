# Document 材料、事实快照与显式发布

> 更新日期：2026-11-10。本页说明 I-034 提供方代码，当前 HTTP、Record、Report 仍使用旧 facade；生产组合根没有启用新事件链。当前任务按用户例外在 refactor/agent 集成，各消费者独立接入并完成非作者审核和完整测试。

## 入口与副作用

| 内部能力 | 行为 | OCR / 模型 | 画像发布 |
| --- | --- | --- | --- |
| `read_material` / `read_facts` / `read_retraction` | 按明确 scope 和 snapshot_id 读取固定快照，缺失报错 | 无 | 无 |
| `capture_material` / `capture_facts` | 显式归档已保存的解析或解读结果，写入新的不可变快照 | 无 | 无 |
| `prepare_document` | 显式处理命令，复用内容索引、已知 DocMind taskId、已有解读与恢复检查点，再捕获事实快照 | 按需调用现有处理链 | 无 |
| `project` | 把固定 facts 快照交给消费方提供的确切版本 projector，返回独立投影 | 由该 projector 的能力决定 | Document 不调用 Publisher |
| `DocumentPublisher.publish` / `retract` | 显式来源级发布命令，持久分配发布修订，重试同一事件 | 无 | 通过注入的公共端口 |
| 旧 `process_document(s)` / `process` | 保留原默认解读、扩展输出、来源写入和摄取补偿 | 原规则 | 统一经过 `LegacyPublicationGateway` |

所有新入口要求 `DocumentScope(user_id, subject_id)`，不隐式调用 `ensure_self_profile`。scope 必须由已授权调用链提供，内容摘要不代表访问权限。capture 和 prepare 是写入归档的命令，不包装成只读查询；真正的 read 不创建索引或集合、不恢复任务，也不写来源。其他请求已经启动的旧处理任务仍可按自己的授权发布，新 prepare 不新增该副作用。

`prepare_document` 使用现有 DocumentReuseCoordinator 的 `publish=False` 分支，不接受 report_id、隐式 subject_id 或调用方覆盖发布标志，避免重新进入旧 reportId 兼容路径。它不改变提示词、OCR、默认完整结果和 partial/unavailable 的恢复政策。它完成后，旧 facade 仍能对同一文档执行原来的正式发布和补偿，不重新解析或解读。

## 不可变材料与中立事实

`modules.analysis.snapshots.DocumentSnapshot` 使用内部规范 JSON 字符串冻结内容，payload 属性每次返回副本。`document.material.v1` 保存 documentId、原内容 SHA-256、文件信息、实际 parserRevision、原件/解析归档引用和完整 parsedDocument，包括原始正文及解析结构。材料以内容寻址文档为单位，当前 sourceRevision 固定为 1；新的文件内容产生独立 documentId 和 sourceId，不把旧来源 hash revision 当作材料版本。

`document.facts.v1` 保存 materialSnapshotRef、实际 extractorRevision/validationRevision、解读轮次和归档引用、单独的 legacySourceRevision、质量缺口和 observations。公共 observations 仅包含 observationRef、identityStatus、stableObservationId、label、rawValue、unit、referenceRange、flag、observedAt、kind、method、specimen、narrativeType、evidence、sourceId。原 JSON 值和现有证据直接保留；仅去除与对应分段实际 occurrence 精确匹配的根镜像和无独立证据的影像拼接展示，保留独立根事实与原快照内引用；Report 的 metrics、taxa、supplementaryHospitalData 及模型总结不成为公共事实字段。

observationRef 仅在同一不可变快照中有效。仅已有 `v6:` 物理绑定才标记 verified 并携带 stableObservationId，其余明确为 identity_unresolved；不根据行号、文本相似度或当前模型猜跨修订身份。本批不支持逐观察 supersedes，correct/retract 使用整份来源替换/撤回语义。Profile 仍负责事实映射、来源贡献撤销和人工更正/删除权威；无法可靠关联的人工决定需要显式治理处理。

快照摘要覆盖 scope、契约、sourceType、sourceId、sourceRevision 和完整 payload，snapshotId 为 document_snapshot_ 加该摘要。MongoDocumentSnapshotStore 在独立 metadata/chunk 集合中先写不可变分块，再提交 manifest；同键同内容可以重试，冲突拒绝，读时校验授权范围、长度和摘要。读取不引用后续会被覆盖的实时解读来补字段。capture_facts 对索引和解读变化做二次检查，发现并发恢复时要求重新捕获，不将新内容贴上旧实现版本。

## 消费方投影

消费方实现 `DocumentFactsProjector`，声明完整 `OutputSelection(profile_name, contract_version, implementation_revision)`，并通过 project 接收固定 facts 快照。提供方检查完整选择一致，返回带 facts_snapshot_id 和 selection 的 DocumentProjection；不会自动选择 latest 或让某个 Report 输出覆盖 Record 输入。消费方可以分别迭代 projector，事实提供方升级则注册新契约并单独迁移消费者。

本批 project 不持久缓存消费方投影，不承诺其回调可以免费或无模型执行；可重复执行的计算和成本由消费方实现管理。已有 OutputProfile 兼容缓存、report_source 私有结构及旧画像桥仍保留在原 facade 中，不能把这一兼容投影当作新的公共 facts。版本选择与旧缓存规则见 [Document 输出版本选择](document-output-selection.md)。

```python
scope = DocumentScope(user_id, subject_id)
facts = await snapshot_service.prepare_document(scope=scope, file_id=file_url)
fixed = await snapshot_service.read_facts(scope=scope, snapshot_id=facts.snapshot_id)
projection = await snapshot_service.project(
    scope=scope,
    snapshot_id=fixed.snapshot_id,
    selection=record_projector.selection,
    projector=record_projector,
)
```

## 显式 Publisher 和持久账本

DocumentPublisher 仅消费固定 facts 快照，事件端口由 `DocumentPublicationPort.deliver(event)` 注入。事件使用 health_report 来源类型、原 canonicalReportId 作为 sourceId，保留材料 sourceRevision 和单独的 legacySourceRevision；snapshotRef 使用 provider=document、contractVersion=document.facts.v1、snapshotId。eventKind 为 publish、correct 或 retract，correct 代表完整新快照，retract 使用同样不可变存储中的 document.retraction.v1 tombstone，其 snapshotRef.provider 仍为 document。tombstone 的规范内容包含 scope、contractVersion、sourceType、sourceId、sourceRevision、publicationRevision、priorRevision、eventKind=retract，摘要直接由该内容计算；后续接收适配器不得更换事件摘要或临时生成不同撤回形状。接收端自行定义和映射公共 DTO，本域没有导入 Profile 私有报告 schema，也没有复制其 inbox 实现。

MongoDocumentPublicationLedger 使用独立 sources 和 intents 集合。来源文档 CAS 同时冻结本次事件和递增 publicationRevision，首个修订为 1，后续严格加 1，priorRevision 必须匹配调用方明确提供的 expected_prior_revision。原事件、时间、快照引用和摘要保存在 currentIntent；写入独立永久 intent 行失败后，从当前预留事件恢复，不能再消耗一个修订号。历史 intent 行按 scope/sourceId/request_id 确定，重复请求永久复用，相同请求键改变内容或 prior 则冲突。

receiver 的 pending、waiting、processing、retry_wait、applied、failed 回执明确区分受理与应用。只有 applied 才提升 appliedRevision；前一修订尚未应用时，不分配下一次来源更正。applied 为吸收状态，迟到的 pending 不会降低水位。收到 applied 后账本写失败，重试先补水位而不再次发送已经确认应用的事件；外部接收成功但响应丢失时，仍用同一 eventId 重投，要求接收端幂等。该提供方没有常驻扫描 worker，投递失败由调用方按原 request_id 重试；后续组合根接线需负责持久任务调度与监控，不能依赖 HTTP waiter 存活。

```python
# 必须先完成以下停写/水位切换，且配置实际公共 receiver adapter。
receipt = await publisher.publish(
    scope=scope,
    snapshot_id=facts.snapshot_id,
    request_id='durable-caller-request-id',
    expected_prior_revision=None,
)
assert receipt.state in {'pending', 'waiting', 'processing', 'retry_wait', 'applied', 'failed'}
```

## 来源切换与回退边界

初始来源模式为 legacy，DocumentPublisher 会拒绝未经切换的来源。生产接线必须给原 HealthReportProcessingService 注入同一个 publication_ledger，并让新的 Publisher 使用该账本；未接入 gate 的旧进程必须先停写并排空。LegacyPublicationGateway 在同一来源登记开始/结束票据，整个 canonical 保存和补充修订属于一次受保护的调用，异常结果保留未决票据。时间流逝不能证明旧 Profile 写入没有发生，因此不自动清除超时票据。

switch_mode 要求可信迁移协调者提供 PublicationCutoverEvidence，引用接收端对账记录和旧链停止/排空记录；账本校验 expected_publication_revision、receiver_applied_revision 与自身水位相等，accepted_retraction 不得超过已应用水位，并拒绝存在未决旧票据的来源。证据对象是协调者的证明输入，不会自行探测外部旧进程或查验 Profile；本批没有向 HTTP 暴露切换命令。未决旧票据只有在证明旧操作已终止且副作用已核对后，才能使用 finish_legacy 显式关闭。

一旦产生新事件修订，当前旧的追加式 sink 无法表达整份来源替换与撤回，故切回 legacy 明确 unsupported。可以回退计算实现或二进制，但必须保留新发布 gate 和原固定事件/水位；不得复活已撤回来源，也不得重置 publicationRevision。完整可逆发布桥需要后续提供方独立实现，不能通过租约过期或重新编号来绕过。

## 装配、验证与未启用项

本域提供 DocumentSnapshotService、MongoDocumentSnapshotStore、DocumentPublisher、MongoDocumentPublicationLedger 和 LegacyPublicationGateway，全部支持独立注入。生产 metadata/chunk/source/intent 集合必须独立于旧 Document 输出和 Profile 集合，并使用可靠写入配置；当前没有修改共享 bootstrap。新 inbox adapter、receiver 快照 resolver、常驻恢复 worker、迁移协调器及消费者正式接入属于后续独立交付，在其完成前仍只有旧 facade 是生产正式发布链。

离线回归覆盖 preparation 无画像写入、显式 scope、读取不创建主体或重跑模型、原档案改写后的快照不变、原证据与未知身份、精确投影版本、分块写中断与校验、重复受理、并发修订竞争、CAS 后归档失败、回执/水位故障恢复、迟到回执、同材料纠错和整份撤回、旧写未决阻止切换及不可用回退。真实 MongoDB 多进程、外部 Profile 接收、线上模型和部署切换未在本次离线检查中验证。
