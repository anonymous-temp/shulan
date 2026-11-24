# Document 问卷附件兼容发布

本提供方把旧 Questionnaire 画像处理器中的附件正式发布效果移到 Document 公开命令。它读取 Questionnaire 已提交的固定材料，沿用旧 `health_report` 来源身份与排队服务；不重新下载、OCR、解释或调用模型，不把问卷附件伪装成 Document canonical 快照。当前只交付独立提供方，默认装配和 Questionnaire 来源消费者另行交付。

## 1. 公开入口与固定版本

入口是 `adapters.analysis.questionnaire_publication.create_document_questionnaire_attachment_publisher`，协议与选择常量位于 `modules.analysis.questionnaire_publication_contracts`。显式选择为 `document-questionnaire-attachment-publication-v1`，不解析 `latest`。

| 方法 | 输入 | 输出与副作用 |
|---|---|---|
| `prepare` | `scope`、`request_id`、`submitted_reference`、`submitted_digest`、固定 `questionnaire_event`、显式 `selection` | 纯读两份固定材料后持久不可变命令；返回 `document.questionnaire-attachment-command.v1`、`commandId`、`commandDigest`、有序 `items` 和 `excluded`；不正式发布 |
| `publish` | `scope`、`command_id` | 校验真实 fresh ready、重新验证全部固定材料，再按顺序正式保存附件；逐项持久 ACK，最后返回 `document.questionnaire-attachment-receipt.v1` |
| `read_receipt` | `scope`、`command_id` | 只读本域命令与原始 ACK；未完成的已准备命令返回 `None`，未知命令明确不存在；成功回执不会重读 Questionnaire、Profile 或重新发布 |

同一 `request_id` 在同一作用域与问卷下只能绑定一组固定输入。材料、事件、版本或摘要变化会冲突，不能沿原命令换入新材料。恢复使用已经持久的 `commandId` 和最初输入，不重新调用 `read_submission` 选择当前材料。

## 2. 两份提交证明必须一致

附件读取使用 [Questionnaire 已提交材料](questionnaire-submitted-material.md) 的 `questionnaire.submitted-material.v1` / `questionnaire-submitted-material-v1` 固定引用。该 reader 通过重新核对已提交归档与摘要保持固定读取；本提供方不假定它另建了物理历史副本，固定内容已不可读或改变时明确失败。

答案来源读取通过注入的公共 `read_source_snapshot(ref, scope=...)`，当前只支持 `profile.questionnaire-source.v2`。必须是该问卷首次 `publish`，`sourceRevision=1`、`publicationRevision=1`，不带伪造的旧来源修订。Document 自行复算两个公开载荷的摘要，并要求原始来源的完整 `submissionProof` 与附件提交材料完全相等，包括提交身份、session、时间、答案摘要、原答案摘要和附件清单摘要。仅同一问卷 ID、同一用户或消费者声称 ready 都不够。

`submissionProof.deliveryMode` 必须是 `profile-source-event.v1`。旧 V1 缺少完整证明，不由新命令认领；旧模式仍由原处理器负责。公共 fresh 准入负责验证旧来源全历史不存在，消费者还必须在受理时固定模式并阻止旧 sender/扫描重新投递。不能让同一提交同时进入两套发布流程。

## 3. ready、附件受理与答案投递

Questionnaire 业务提交只持久本域提交与 outbox，不等待 Profile。独立 worker 依次执行：

1. 使用真实公共 Profile fresh 服务完成该问卷精确材料的 `ready`。
2. 调用 `prepare` 并持久返回的命令，再调用 `publish`，恢复时复用同一命令。
3. 持久附件 `accepted` 回执，然后向 Profile Inbox 投递固定答案事件。

`publish` 自行调用公共 `get_cutover`，核对 actual fresh ready 的 scope、来源、材料契约、引用、摘要及 Questionnaire owner fence。首次成功核对的授权也会固定保存，不接受消费者传入的 ready 布尔值。一个附件失败不返回整组成功，已保存的逐项 ACK 留给重放继续。

| 附件回执状态 | 能证明的结果 | 不能证明的结果 |
|---|---|---|
| `ingestionStatus=pending` | 正式来源记录已持久，旧后台可重试排队 | 任务已入队或事实已应用 |
| `ingestionStatus=queued` + 真实 `ingestionJobId` | 正式来源记录和实际排队身份已确认 | 任务已完成或事实已应用 |
| 整组 `status=accepted` | 每个选中附件都有上述真实持久回执，排除项已固定 | 附件画像已完成治理 |

旧 `HealthReportSourceService.save` 在排队暂时失败后仍返回持久 pending 来源；原问卷处理器随后继续答案处理。新命令保留此顺序，附件 pending 已足以让答案继续投递，不把附件应用完成作为前置条件。空附件也必须先核对真实 ready，随后返回明确的空 accepted 回执。

## 4. 旧身份、质量与门禁

附件顺序、按内容去重以及 `document_reference` / 无解读排除使用公开提交材料的固定清单，并逐项复核。`contentKey` 沿用提交时的 `contentSha256`，缺失时使用原 `artifactId`，不以归档后补出的哈希更改旧身份。

```python
fingerprint = sha256(f"{user_id}:{subject_id}:{content_key}".encode()).hexdigest()
report_id = str(uuid5(NAMESPACE_URL, "questionnaire-report:" + fingerprint))
```

实际发布调用共享 Document `LegacyPublicationGateway.save` 和公共 `HealthReportSourceService.save`。原始解读完整传递，包含 `processing=partial`，不提升为 accepted 中立 Facts，不调用 `publish_document` 或 `save_revision`。同一旧报告 ID 已有记录时，沿用旧 `.save` 首份来源内容，不能因为新解释不同就默默创建补充修订。回执同时给出本次 `inputInterpretationDigest` 与实际 `storedResultDigest`，以及真实 `legacySourceRevision`，不会把两份内容假装相同。

问卷答案事件与附件 `health_report` 是不同来源。附件仍受共享 Document legacy ticket/gate 与 Profile source-managed gate 约束；附件来源已切换 events 时明确拒绝旧发布，不能绕过 gate 保存或伪造成功。新 managed Document 来源编排属于独立提供方，不在本命令中隐式改旧附件身份或发布模式。

## 5. ACK、取消与资源

所有材料先验证，再进行第一项正式写入。每次实际保存后写入逐项不可变 ACK；并发重放使用同一报告身份去重，首份真实队列状态固定，不随稍后 pending→queued 漂移。保存 ACK、逐项 ACK 或最终 ACK 丢失后可重复同一命令。已经完整受理的命令只读持久回执，不要求提供方仍可再次生成材料。

取消与异常向调用者传播。共享 legacy gate 对可能已生效的未知写入保留 uncertain ticket，不能凭本命令重试成功自动清理，防止后续 fresh 准入把未知历史误判为空。这里没有新增独立后台 worker；Questionnaire owner outbox 负责持久尝试次数、截止时间、租约和恢复扫描。本命令至多处理当前公开提交上限的 5 个附件，调用方必须为每次等待设置受控预算。

工厂只构造对象，不查询数据库、创建索引、启动任务或建立新客户端。注入 Q 两个公开 reader、公共 Profile Inbox、公共旧报告来源服务、共享 Document publication ledger 和本域 `command_records` 专属集合/命名空间，不与 canonical Document fresh 准入历史混存。集合使用 PRIMARY 读、majority 写、唯一 `_id`、无 TTL；部署负责资源和索引。命令只存引用、摘要、身份、授权与回执，不复制大型报告正文。不存在跨集合事务，恢复依靠旧来源幂等身份和逐步持久 ACK。

## 6. 验证与交付边界

离线回归覆盖真实 Questionnaire submitted reader、旧报告存储/排队服务、BSON 往返、共享 legacy gate、旧处理器发布参数逐项对照、质量 partial、缺哈希、重复与排除项、固定材料改变、实际 ready 失配、三处 ACK 丢失、并发重放、取消与纯读恢复。基础故障测试中的 Q v2 reader/ready 是明确的公共契约替身。

在 [Profile Q v2 提供方](profile-questionnaire-source-v2.md) 合入共同基线后，另以实际 V2 resolver、全历史 presence、fresh reserve/authorize、get_cutover 和 Inbox 完成组合回归：blocked 无附件写，ready 后真实 pending/queued 附件可受理，答案随后 received 而 applied 水位仍为 0；历史旧 job 拒绝 fresh；ready/附件 ACK 丢失恢复原授权和命令；已完成回执纯读。此组合的 Q owner fence 和 raw source reader 仍是显式契约替身，存储使用 BSON 离线替身，未宣称真实 Questionnaire 来源消费者或 MongoDB 部署已经接入；消费者与默认装配分别验收。

本批不修改 HTTP、Prompt、模型参数或业务提交响应，不声称真实 MongoDB、模型服务、部署和全量历史迁移已经验收。相关来源生命周期见 [Document 完整来源](document-complete-source.md) 与 [Profile 多来源发布](profile-multi-source-publications.md)。
