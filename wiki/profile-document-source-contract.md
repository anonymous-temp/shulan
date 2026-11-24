# Profile 完整 Document 来源与接收就绪

本页锁定 Profile 与 Document owner 对齐的公开消费契约，版本为 `document.source-observations.v1`、`document.source-inventory.v1` 和 `document.source-contribution.v1`。Profile 已实现消费 DTO、完整性校验、固定解析器及接收门槛；Document 的真实完整捕获、历史贡献清单与排空证据 reader 已由独立提供方交付；[默认根装配](document-source-composition.md)已先注册固定解析器和 owner fence，再构造命令侧及独立后台周期。代码默认接线不表示历史来源自动迁移或真实部署已经验收。单份 `document.facts.v1` 不足以覆盖旧 Report v16 的补充贡献，不能进入新切换链。

## 公共读取和身份

公共类型从 `modules.health_profile.contracts` 导入；`ProfileDocumentSourceResolver` 从 `modules.health_profile.application` 导入。Resolver 只消费 `ProfileDocumentSourceReader` 的三个异步纯读方法：

- `read_source_observations(*, scope, snapshot_id) -> dict`：读取固定完整来源 canonical JSON。
- `read_source_inventory(*, scope, snapshot_id) -> dict`：读取固定来源清单 canonical JSON。
- `read_retraction(*, scope, snapshot_id) -> dict`：读取既有 `document.retraction.v1` canonical JSON。

读取必须由 Document 再次校验 scope；不能调用 ensure、补索引、解析、模型、latest 查询或正式发布。提供方不能验证归属、原始修订、当前有效集合或真实排空证明时，应明确返回不可用，不得合成完整结果。Profile 不读取 Document 私有集合，也不解释 Report 的私有投影。

三种材料 canonical 顶层均有 scope{userId,subjectId}、sourceType=health_report、sourceId、sourceRevision 和 contractVersion。sourceId 是稳定 canonical source；sourceRevision 是原件材料版本，不是哈希式旧 legacySourceRevision，也不是发布排序 publicationRevision。来源与每份贡献的材料版本必须一致。JSON 摘要为键排序、紧凑分隔、UTF-8、禁止非有限数值后的 SHA-256；顶层完整材料 snapshotId 为 `document_source_snapshot_` 加摘要。每次读取保持原始字段及空值，不由 Profile 重写来源身份。

## 完整材料与贡献

完整材料 payload 字段如下：

| 字段 | 内容 |
|---|---|
| quality | status=accepted; details: 原始处理质量信息，不作为事实 |
| materialSnapshotRefs | 有序固定原件引用，含 snapshotId、contractVersion=document.material.v1、contentDigest、documentId、sourceRevision |
| contributions | 当前有效贡献的完整有序数组，至少包含 base，不能把全部历史修订并集作为当前来源 |
| coverage | contractVersion=document.source-coverage.v1、完整来源身份、legacySourceRevision、inventorySnapshotRef、inventoryDigest、orderedContributionIds、complete=true |

每份贡献含 contributionId、kind=base或legacy_report_source、contractVersion=document.source-contribution.v1、snapshotId、contentDigest、sourceRevision、legacySourceRevision、implementationSelection、reportType、reportTime、observations。implementationSelection 精确包含 profileName、contractVersion、implementationRevision；本版只接受：

| kind | 精确选择 |
|---|---|
| base | health_report / 1 / analysis-health-report-v12 |
| legacy_report_source | report_source / 1 / report-source-v16 |

新 Report 私有消费者输出不得冒用旧选择。不同中立解释版本需独立扩展支持，不跟随 latest。contributionId、固定引用及局部观察 pointer 不能重复或猜测跨版本对应。

贡献摘要可复算的 canonical 为：顶层来源身份加 contractVersion=document.source-contribution.v1，payload 为该贡献除 contractVersion、sourceRevision、snapshotId、contentDigest 外的全部原始字段。可选字段是否存在也属于固定内容，不在验证时补字段再算摘要。

observations 仅接受以下中立字段：observationRef、identityStatus、stableObservationId、label、rawValue、unit、referenceRange、flag、observedAt、kind、method、specimen、narrativeType、evidence、sourceId，以及旧中立 sourceCategory/sourceObservationId/sourceSection。rawValue 保留原 JSON 值，evidence 保留原文/位置；禁止 factType、AI 总结、患者结论等额外字段。identityStatus=verified 必须有可信 stableObservationId；否则为 identity_unresolved 且不提供该 ID。

Profile 按每份贡献自己的 reportType/reportTime 复用原映射，不把不同子报告的日期互借。可空 scope 元数据不凭空制造“有子报告归属”的标志；真实 sourceCategory/sourceObservationId/sourceSection 仍禁止借其他报告日期。事实来源指针按 `/contributions/{转义后的 contributionId}{observationRef}` 命名，只标识当前固定快照中的观察。

quality.status=accepted 专指 Document 已验证这份当前有效完整来源集合可正式发布，不等同于原始 OCR 或解释任务的 completed/partial 状态；原始处理质量只能保留在 details。缺少 status、unavailable、partial 或未知状态一律拒绝就绪和接收，不能用不完整的新集合撤空既有来源。

不得把原 processing=partial/unavailable 仅搬入 details 后无条件标为 accepted。生产 capture 必须同时依据原始处理质量和当前有效贡献完整性验证；不能证明完整来源时拒绝 source-wide 替换，保留原兼容路径或另行采用明确支持的 partial 治理。

## 当前有效集合和历史退休

inventory payload 必须包含：

| 字段 | 约束 |
|---|---|
| fence | evidenceRef、publishersQuiesced=true、workersQuiesced=true、legacyIntentsRetired=true |
| activeSetEvidenceRef | Document 已验证当前有效集合的不可变证明引用，不能为空 |
| legacyState | absent 或 captured |
| legacySourceRevision | captured 时必须有明确旧修订；absent 时才允许空，并须有真实证据 |
| contributions | 当前有效贡献引用，按固定原件顺序和旧有效发布顺序排列；逐项含 contributionId/kind/contractVersion/snapshotId/contentDigest/sourceRevision/legacySourceRevision/implementationSelection |
| retiredContributions | 退休贡献的相同原引用字段，加 retirementKind=superseded/retracted/corrected、retirementEvidenceRef、可选 supersededByContributionId |

Profile 会重新读取 inventory 并复算摘要，核对 scope、来源、材料版本、旧修订、有效贡献顺序、每项引用/摘要/选择一一对应。active 与 retired 的 contributionId 或 snapshotId 不得重叠；替代目标若给出必须指向当前有效贡献。退休条目只保留审计与覆盖证明，绝不进入候选提取。无法证明有效/退休关系时必须拒绝 ready，不能把历史全集当完整事实集。

这些证据由 Document 在真实旧 writer/worker 排空与持久 fence 下建立，Profile 不能替外部进程证明停稳。布尔值、一次 pending 扫描、缺少 reportSource 字段，都不能替代真实证据。Profile 校验公开证明的一致性；Document reader 对证据真实性和全部旧有效贡献的捕获负责。真实实现尚未交付时仅 Fake 可验证协议，不能伪造生产授权。

## 接收门槛与固定执行

`resolver.inspect(snapshot_ref, scope=..., source_id=..., content_digest=...)` 是纯读就绪检查，成功返回 ProfileDocumentSourceReadinessV1；不写授权或推进水位。失败抛出具备明确 code 的来源错误，不能解释为“空材料”。

安装使用 `resolver.registrations()`，同时注册完整材料和撤回的精确版本。公共工厂只有二者指向同一固定解析器时才开放 Document 接收就绪；缺一项保持拒绝。根 bootstrap 和生产 Document reader 的装配另批完成。

Document 来源 authorize_cutover 需显式 materialContractVersion、materialSnapshotRef、materialContentDigest、processingRevision=profile-document-source-v1、legacyQuiescenceEvidenceRef，以及现有停稳声明。Profile 检查固定材料和 inventory 的证据引用匹配后，按既有持久 marker 与旧 Job 检查流程迁入；不能自动生成 quiesced。初次 publish 必须使用授权绑定的同一完整材料。后续 correct 也必须提供新的完整当前来源材料；迁入后不允许退回 legacy resolver 或单份基础 Facts。

接收前再次验证材料，processingSelection 在最早的持久 alias 中冻结，再复制到 publication，覆盖解析器、mapper、策略、事实注册表版本。ACK 丢失或 alias 之后中断会沿原清单恢复；已受理同内容重投不因 reader 临时不可用而重读或换版本。执行时核对保存清单，版本变化明确失败，不采用最新实现。Doc Publisher 对未知 ACK 应重新 receive 同一冻结事件以让 Inbox 核对 fingerprint；单凭 eventId 查询不能证明内容一致。

处理继续使用原 Profile 来源级治理：完整更正替换原贡献，撤回撤销来源支持，其他来源和人工决定优先；处理中可能存在部分效果，不能把 received 当 applied。撤回对未完成计划保留退休记录并结算，防止迟到 Worker 复活事实。未新增模型调用、跨多文档事务承诺或独立治理真值。

单份 canonical JSON 上限 4 MB，有效贡献和退休清单各最多 1000 项，当前观察总数最多 1000；超界明确拒绝，不截断来源。

## Chat v2 与 Questionnaire 后续依赖

`ProfileChatSourceSnapshotV2` / `profile.chat-source.v2` 保存 role=user、原始 content 和原始 ISO timestamp 字符串，正文空白和原始时区写法都属于不可变来源身份。Profile 固定处理版本 profile-chat-source-v2 内部复用旧 content.strip() 和 naive 时间按东八区解释的规则，显式时区保持原意，不依赖机器时区，也不改来源存储。非法时间或空白正文在调用模型前明确失败。原 v1 的材料与处理清单继续原版本重放，不静默改其摘要或模型输入；新的 Chat 正式消费者应选择 v2。

Questionnaire 正式切换须由本域保留独立持久附件发布意图：原画像处理器在答案提取前还发布成功附件，跳过 document_reference 并按来源身份去重。答案事件不承担附件来源发布，Profile 不偷偷补建消费者 Publisher；该依赖未完成时不能宣称问卷正式迁移完成。

## 验证边界

离线回归覆盖完整 base/v16 映射和日期等价、缺失清单/证据/版本拒绝、当前有效与退休集合隔离、构造无发布、重复/乱序、alias 中断、受理清单冻结、部分治理撤回结算、人工决定优先以及 Chat 原始身份与旧模型输入等价。没有读取私有环境、调用真实服务或执行数据库迁移。完整 pytest 与非作者审核在本分支交付时记录，不能代替真实 Document 捕获和部署切换验收。
