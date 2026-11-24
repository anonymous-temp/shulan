# Profile 新来源准入与兼容归档

本批是 Profile 提供方能力：精确只读存在性证明、独立 fresh 两阶段准入、只保存兼容历史的归档命令，以及旧 Record/Chat/Questionnaire 发送器的模式隔离。没有默认启用任何来源切换，也没有替消费者实现 owner admission fence、业务完成 outbox 或部署迁移。真实 Document/Record/Chat/Questionnaire 适配和组合根接线分别验收。

## 公共入口

公共类型从 `modules.health_profile.contracts` 导入，服务从 `modules.health_profile.application` 导入：

- `MongoProfileSourcePresenceReader.read_legacy_presence(scope, *, source_type, source_id, limit=1000)` 返回 `ProfileSourcePresenceV1`。
- `ProfileFreshSourceAdmission.reserve_fresh_source(authorization, *, scope)` 返回持久 `blocked`；`authorize_fresh_source(authorization, *, scope)` 才可能返回 `ready`。
- `ProfileSourceMaterialArchive.archive_source_material(*, scope, canonical_report_id, report_id, source_fingerprint, document_id, result, source_key_value=None)` 返回 `ProfileArchivedSourceMaterialV1`。
- `create_application_profile_source_admission(inbox=..., owner_fence_readers=...)` 构造 `presence/admission/archive`，不执行 I/O。注入的 Inbox 必须与该应用的 Profile publication 存储一致。

## 只读 presence 证明

查询强制 PRIMARY，每个集合最多读取 `limit + 1` 条元数据，默认单查询最大 2000 毫秒（可配置 1–10000），limit 范围 1–1000。构造和查询均不创建主体、索引、view、job 或 marker。返回状态：

| 状态 | 含义 |
| --- | --- |
| absent | 本次完整有界查询未见 Profile 管辖的旧历史 |
| present | 发现至少一条旧历史或现有来源发布记录 |
| missing | 主体不存在、非 active 或非健康报告来源没有本人 anchor |
| unavailable | 读取失败、范围含混、身份冲突或超出有界扫描预算 |

所有类型均检查该用户下完整 sourceType/sourceId 的所有旧 job 状态（含 completed/failed/dead_letter、空 subject 的旧 job）、所有目标主体的事实贡献、所有 Profile cursor/alias/publication/cutover。不会只查 pending，也不会漏掉该来源对血亲主体的贡献。health_report 额外检查 canonical/alias 报告及全部 opaque 历史修订；其他用户或含混主体占用同一报告身份时不伪装为空。报告的 profileIngestion pending 状态也属于正式历史。

公共返回只含各 section 的 kind、observedCount、truncated、evidenceDigest，不返回医学值、私有 job 正文或数据库集合名。proofRef 为 `profile_presence_<contentDigest>`；digest 覆盖 contractVersion、scope、sourceType/sourceId、status 和有序 sections，不包含 readAt。相同空集合在不同时间可得到相同 digest，但该值本身不赋予授权，也不是服务端新建的历史快照。

这些 PRIMARY 查询不是跨集合事务。只有 owner 真实阻断旧正式写入和 Profile 持久 marker 配合再次查询，才能完成 fresh 准入。不存在用一次扫描替代排空的路径。

## Owner admission fence

`ProfileOwnerAdmissionFenceV1` 的固定 JSON 字段如下：

```json
{
    "contractVersion": "profile-owner-admission-fence.v1",
    "scope": {
        "userId": "u",
        "subjectId": "self"
    },
    "sourceType": "health_report",
    "sourceId": "canonical-source",
    "fenceId": "owner-persisted-fence",
    "fenceGeneration": 1,
    "preparedManifestRef": "fixed-publish-false-preparation",
    "formalHistoryAbsent": true,
    "legacyIntentsAbsent": true,
    "activeLegacyWriters": 0,
    "activeLegacyWorkers": 0,
    "legacyAdmissionBlocked": true,
    "issuedAt": "2026-11-10T00:00:00Z"
}
```

这不是业务请求可自行提交的布尔声明。注册 owner reader 必须基于自己的持久正式历史、receipt、意图和真实 gate/ticket 证明这些字段。健康报告 owner 为 document，其余 owner 分别为 record/chat/questionnaire。Profile 自己再次查旧 job/事实/历史，不能由 owner 证明代替。

formalHistoryAbsent/legacyIntentsAbsent 指旧正式来源归档、发布和摄取投递历史；允许 preparedManifestRef 指定的首个 `publish=False` 固定准备草稿存在。activeLegacyWorkers 指持有正式写入/投递授权的执行者；没有这种授权的 OCR/LLM 准备可以继续，但其后续正式写入必须经过已阻断的 gate。已经获得 ticket 的执行者必须排空，不能仅修改模式后声称为零。

`read_admission_fence(*, scope, snapshot_ref)` 只读固定 canonical 证明。`is_admission_fence_held(*, scope, snapshot_ref, content_digest)` 单独核对当前 gate 仍持有，仅用于准入；历史 Document inventory/material 回放不重新扫描当前 absence，也不要求原 live stateVersion 永远不变。

## 两阶段 fresh 命令

`ProfileFreshSourceAuthorizationV1` 固定 admissionKind=fresh、scope/sourceType/sourceId、authorizationId、ownerFenceRef/ownerFenceDigest、presenceProofRef/presenceProofDigest、materialContractVersion、首个 materialSnapshotRef/materialContentDigest、processingRevision 和 authorizedAt。Record 只接受 profile.record-source.v2，Chat 只接受 profile.chat-source.v2，Questionnaire 只接受 profile.questionnaire-source.v1；健康报告必须是完整 document.source-observations.v1。

reserve 先验证 owner 固定证明与当前 gate，读真实 absence，保存 blocked marker，再次检查 gate 和 absence。Record 先将业务完成证据和新模式持久意图保存到本域，再由后台取得真实 blocked 回执并完成授权；业务响应不等待 Profile 可用，且仅本域 staged draft 不代表画像发布已经受理。任何检查失败都不生成 ready，已保存 blocked marker保留且继续阻断旧入队。它不是人工 quiesced 授权，不能把现有来源自动迁入。

authorize 在相同固定授权上复核；Document 还必须通过完整材料、有效贡献清单与 fresh inventory fence 绑定验证。最终 ready 保存真实 absence proof。相同已 ready 授权的重放直接读取持久证明；后续 archive、公有 publication 或 owner gate 状态变化不会把原授权重新解释为空来源。不同授权/材料身份明确冲突。事件首次发布必须与授权冻结的材料 ref/digest 相同，且普通更正继续使用授权的材料契约。

Document inventory 的旧真实 quiescence fence 保留；新增独立 fresh 形状含 admissionKind、evidenceRef、ownerFenceRef/ownerFenceDigest、profilePresenceProofRef/profilePresenceProofDigest，不出现旧 publishersQuiesced/workersQuiesced 字段。只有 fresh 命令接受该形状。读取历史材料仍验证固定证明、scope、完整清单和 digest，不能触发新准入。

## 兼容历史归档

archive 仅支持 health_report，必须已有相同 scope/canonical source 的 ready source_managed marker。它直接写真实兼容 canonical/alias 或补充 history，状态固定 source_managed、jobId 为空，没有 JobQueue、Agent 或 observation capture 依赖。不会通过旧 save/save_revision 正式投递入口间接排队。

初始记录保留 legacy revision=1；补充修订仍按旧 source_key 的 SHA-256 前 12 位生成真实 JS-safe opaque revision，同 key 重投校验完整 scope/document/fingerprint/result，冲突不覆盖。该 revision 不是发布排序。补充记录必须先有匹配 canonical 文档历史。不能把已有 pending/queued 旧记录悄悄转换成归档。命令返回真实保存记录的身份与 digest；调用方负责提供已固定、真实验证的兼容结果，不能用新 Report 私有投影冒充旧来源证明。

fresh 首次完整材料只能有可证明无旧历史的基础贡献，顺序为固定基础准备、fresh ready、再归档。已有旧 Report v16 正式贡献意味着来源已存在，不属于 fresh。后续 managed 来源的补充归档与完整 active/retired 计划由 Document 独立实现；本批 Profile 离线组合验证不表示该消费者已上线。

## 旧 Record/Chat/Questionnaire 模式隔离

Record 使用 qaProfileIngestion.deliveryMode，Chat 使用对应 chatProfileIngestion 条目的 deliveryMode。缺字段或显式 legacy 保留旧路径；profile-source-event.v1 和任何未知模式均不得追加旧 job，即使 Profile marker 尚未出现。旧 pending 扫描在 limit 前排除非 legacy 模式，避免新域草稿占据恢复批首。该字段只决定旧发送器是否可运行，不是 fresh 证明、排空回执或事实应用状态。

Questionnaire 使用 profileIngestion.deliveryMode。[旧发送器](../health_profile/questionnaire_source.py) 在 queued 快捷返回、enqueue 和 mark_ingestion_queued 之前检查实际已提交快照；仅缺字段、None 或精确 legacy 允许旧路径。新 profile-source-event.v1、未知值、非法类型及不合法的 profileIngestion 容器返回未投递，不修改业务材料或投递状态，也不依赖 Profile marker 已存在。recover_pending 先排除列表中的非 legacy 项，再重新读取当前提交快照验证，不能把新模式 queued 计作旧恢复成功。所属 Questionnaire Store 的数据库筛选必须在 limit 前完成，另由 Questionnaire 域提供；本次 sender 防线不能代替该扫描公平性要求，不启用默认来源切换。

## 版本与验证边界

真实 Document 当前单份内容寻址材料的 sourceRevision 为 1。同原件解释更正改变固定 digest/ref，publicationRevision 递增，材料 sourceRevision 不随发布顺序变化。Profile 组合回归覆盖首次来源、真实 opaque 历史归档、同 sourceRevision=1 的更正与撤回。当前完整来源契约仍要求聚合及各 material/contribution 的材料版本一致；多材料真实版本不一致时明确拒绝，不能重新贴成 1 或 pubRevision，支持它需独立契约迭代。

回归还覆盖 PRIMARY/无写、全部旧 job 状态、空 anchor、别名/历史修订、跨目标贡献、预算溢出、owner证明/当前gate失效、扫描间旧job竞态保留blocked、ACK丢失重放、跨scope/首材料冲突、真实BSON opaque修订、Record/Chat 两域 mode 拒绝及旧扫描公平性，以及 Questionnaire 旧 sender 的模式矩阵、queued 早返回隔离和恢复复核。所有验证均为离线；真实 owner gate、服务、数据库和迁移未执行。
