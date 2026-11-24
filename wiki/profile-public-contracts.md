# Profile 公共读取与来源发布契约

后续增量：[Record v2 与部分应用结算](profile-record-source-settlement.md) 扩展了下述最初来源切片。公共回执现区分 incomplete、partial、applied 与 superseded_by_retraction；settledPublicationRevision 与 appliedPublicationRevision 分开，历史发布是否完整应用须按具体发布查询，不能再从较高水位推断。Record v1 仍拒绝正式迁入，v2 需显式契约授权。

本页记录 I-035 的实现切片：唯一权威画像的结构化只读接口、独立来源事件账本、体检来源的完整快照应用与恢复。没有新增 HTTP 路由，也没有切换 Chat、Record、Report、Questionnaire 或 Document 的正式发布路径。现有画像事实、主体和治理仍由 Profile 统一维护。

## 只读入口

`modules.health_profile.application.ProfileSnapshotReader` 提供 `resolve_scope(user_id, subject_id=None)`、`list_subjects(user_id)` 和 `read(user_id, subject_id=None, max_facts=300, statuses=..., required_publications=...)`。这些方法不会调用 `ensure_self_profile`、创建主体或视图、创建索引、投递任务、加载模型或修改来源。主体不存在返回 missing，存储读取失败返回 unavailable；它们不等于资料为空。

`ProfileSnapshotV1` 固定 `contractVersion=profile-snapshot.v1`，携带 snapshotId、单一主体 scope、profileRevision、readAt、主体关系、事实及来源、状态、有效时间、truncated、stale、coverage 和来源水位。profileRevision 仍是既有 profileVersion，不产生第二套治理真值；消费者自己的筛选、预算、Prompt 和 consumerProjectionRevision 留在消费者模块。快照对外字段不包含内部写入幂等标识，输入与序列化使用独立数据副本。

读取在事实查询前后比较画像修订，变化时重读一次；这是明确标注的乐观检查，不是跨集合事务快照。未结束的来源发布或不能满足 required_publications 时标记 stale，并分别报告 publicationPending、publicationApplying 和 requiredPublicationsApplied。消费者需要看到本次来源时，应先检查来源回执是否成功，再读取满足指定水位的快照；收到事件不代表事实可读。旧写链未整体切换前，乐观修订检查不承诺原子隔离。

`HealthReportSourceService.get_owned_revision(report_id, source_revision, *, user_id, subject_id)` 只读取已保存的精确报告修订，校验归属，不排队、不补主体。这里的 source_revision 是旧报告修订 ID，可能是哈希派生值；它与新事件的材料 sourceRevision 不同。

## 来源事件与回执

公共值对象位于 `modules.health_profile.contracts`。ProfileSourceEventV1 包含服务端绑定的 scope、eventId、sourceType/sourceId、材料 sourceRevision、可选 legacySourceRevision、单调 publicationRevision、priorRevision、eventKind、occurredAt、contentDigest 和 ProfileSourceSnapshotRefV1。snapshotRef 只包含 provider、contractVersion、snapshotId，不包含 URL、数据库集合或消费者的私有报告 schema。contentDigest 是公共不可变快照经 JSON 键排序、紧凑分隔、UTF-8 编码后的 SHA-256；禁止非有限数值。

来源域拥有发布顺序：首次 publish 使用 publicationRevision=1 和空 priorRevision；后续 correct/retract 严格加一并指向前一修订。原材料不变时更正可以只推进 publicationRevision；legacySourceRevision 仅用于精确历史读取与迁移证据，不能用于排序。

ProfileSourceInbox.receive(event, scope=...) 同时检查 eventId 和 scope/sourceType/sourceId/publicationRevision。更换 eventId 重投相同发布仍只有一次应用；相同身份修改内容、引用、事件类型或前置关系会冲突。get_receipt(event_id, scope=...) 返回 received、waiting、processing、retry_wait、applied 或 failed，并区分最高已接受修订与已连续应用修订。acceptedRetractionRevision 仅是已接受的撤回屏障，appliedRetractionRevision 才表示撤回治理已完成。缺前置修订默认最多等待一天；失败达到预算或不可恢复时留存 failed，修复后通过 redrive 显式重驱，不无限显示处理中。

## 显式迁入与旧链保护

首次接收之前，来源 owner 必须停稳旧 Publisher 与旧 Worker，并保存 ProfileSourceCutoverAuthorizationV1。authorize_cutover(authorization, scope=...) 先持久化迁入标记，再检查尚未结束的旧任务；标记即使检查失败也保留，阻止该来源继续进入旧队列。get_cutover 可纯读查询 blocked/ready 及原授权，便于重启后恢复同一操作。没有 ready 标记时 receive 明确拒绝，不能靠一次 pending 扫描自动切换正式发布路径。

这个授权是来源 owner 的显式停稳声明，Profile 不会替它停止外部进程或发布器。原 I-035 切片仅支持 health_report；新增 Chat/Questionnaire 材料能力、Record 硬拒绝迁入及后续条件见[多来源原始材料](profile-multi-source-publications.md)；其他来源必须由其 owner 与 Profile 分别交付适配，不能只换 sourceType 就绕过迁移条件。生产体检来源队列和处理器检查同一个标记：新旧链不能在同一来源范围同时被选为正式 Publisher。已迁入来源的旧任务可返回 source.managed_by_publication。旧报告与历史修订中尚未投递的 pending 标记会持久进入 source_managed，记录同一原因且不伪造 queued/jobId，防止它们长期占据旧扫描批次而饿死其他来源。回退不能移除标记或越过撤回水位。真实停稳、切换和回退尚需部署环境验收。

## Profile 自有应用与恢复

底层工厂独立调用时保留两个兼容解析器：health_report / health-report-observations.v1 纯读旧报告观察快照，以及 profile / profile-source-retraction.v1 读取受控撤回快照。ProfileSourceMaterial 只作为解析器与 Profile mapper 之间的原始资料接口；Profile 校验快照摘要及主体/来源身份，自己执行观察映射、候选策略和事实治理，不接受业务报告 AI 总结作为健康事实。当前[应用组合根](document-source-composition.md)还显式注册 Document 完整来源/撤回及各业务源的固定材料 resolver；Record 使用已实现的 v2，v1 仍拒绝。各域 owner、不可变材料和后台发布独立维护；单份 document.facts.v1 不能冒充完整来源，历史来源仍须满足显式迁移条件，不自动转换。

correct 使用整份来源快照替换旧贡献，retract 撤销该来源支持；仍有其他来源支持的事实继续保留。观察身份可以只在当前快照内有效，新事实 ID 绑定 publication 和 observationRef，不伪造跨修订的物理关联。人工确认、更正、拒绝和删除优先；对应关系无法可靠确认时返回 source.manual_governance_requires_review，不借新发布修订重新生成已删除事实。已保存计划执行期间也重检人工变更，并以持久补偿退役本计划的新贡献。已明确由用户确认的事实可保留人工依据，不因原报告撤回而删除用户决定。

应用前先保存有界、不可变的 Profile 自有计划；事实与审计逐项幂等执行，最后单文档 CAS 推进来源已应用水位，再修复回执状态。上一计划即使尚未创建全部事实，后续撤回也为其确定性 ID 保留退役记录，防止过期 Worker 的迟到创建复活来源。没有跨多文档原子性承诺：处理中可能出现部分效果，回执和快照必须如实反映未完成状态，扫描器通过原计划补偿或续做。

专属集合 `health_profile_source_publications` 保存 cutover、alias、publication、cursor 四类记录，通过 `SHULAN_MONGODB_COLL_HEALTH_PROFILE_SOURCE_PUBLICATIONS` 配置。eventId 绑定、发布记录与游标之间的中断由 staged 标记恢复；单文档 CAS 与来源租约控制竞争，过期租约不能推进 applied 水位。活跃执行者在应用检查点按 token CAS 续租，单次 I/O 超过租约仍按失效处理；每次应用派发前持久计数，进程中断和租约丢失同样消耗默认五次预算，不会无限恢复。重试耗尽与缺前置超时均可查询并显式重驱。该集合没有自动 TTL，保留与删除策略需另行评审，不能删掉撤回或迁入标记来回退。

`create_application_profile_public_contracts()` 构造 reader、inbox 和 worker。现有画像 Worker 的持久 pending 扫描已组合新的发布恢复器，继续由原生命周期管理；不创建独立服务进程。实际启用新的 resolver 时，向生产 Worker 工厂显式传入 source_snapshot_resolvers，并保持事件版本与读取实现可恢复。

## 离线验证范围

新增离线用例覆盖纯读无写入、作用域隔离、旧哈希修订不参与排序、重复事件与发布冲突、缺前置等待、内容摘要错误、已接受/已应用撤回水位、人工确认与删除、计划后的人工变更、其他来源支持、alias 中断修复、事实写入后中断、游标已推进后的回执修复、过期租约、显式迁入和旧入队屏障。没有读取私有环境文件、执行真实 MongoDB 迁移、调用真实模型或切换正式消费者；离线通过不代表生产切换已经验收。
