# Document 完整中立来源提供方

本文描述 Document 的固定贡献、当前有效清单和完整来源快照。它补齐 [正式来源生命周期](document-publication-lifecycle.md) 与 [Profile 完整材料接收契约](profile-document-source-contract.md) 之间的提供方接口；不代表生产入口已经切换。

## 当前范围

`DocumentCompleteSourceService` 从固定 [understanding bundle](document-understanding-bridge.md) 或真实 [旧 v16 发布回执](document-report-publication.md) 捕获中立贡献。`capture_source` 只接受独立准入提供方已经持久验证的计划引用，不接受调用者传入 `complete=true`、quiesced 标志或任意当前贡献列表。

`DocumentFreshSourceAdmission` 的真实自动计划只覆盖全新 canonical source 的单个完整 base。已有正式历史、未知历史、扫描截断、已有事件来源或无法证明质量时拒绝 fresh。已从真实 fresh 起点发布并应用的来源，可使用独立交付的 [managed 命令](document-managed-source.md) 更正基础、补充与退休贡献；旧历史来源迁移仍未实施。接口测试中的 synthetic verified plan 只验证消费协议，不能用于生产签发完整性证明。

[默认根装配](document-source-composition.md)现已组合完整来源 reader、Profile resolver、正式命令与独立后台 worker；既有 HTTP、旧输出缓存身份及已选旧实现保留。只有完成来源准入与同一发布 gate 校验才可正式发布，不能仅安装 reader 就绕过本文末尾的条件。

## 固定材料与贡献

提供方公开两个贡献捕获命令，均不重新 OCR、调用模型、执行 v16 提取或发布画像。

| 命令 | 固定输入 | 验证和结果 |
| --- | --- | --- |
| `capture_base_contribution` | `scope + understanding_snapshot_id` | 纯读同一 bundle 的 material/facts/原解释，实际版本须为 `analysis-health-report-v12`，原质量须为 complete；生成 base 贡献及独立输入证明 |
| `capture_legacy_report_contribution` | `scope + publication_receipt_ref` | 纯读真实 `report-source-v16` 命令回执、投影和执行清单；要求正式旧 opaque revision 和完整原质量；提取真实中立检查行 |
| `capture_source` | `scope + verified_plan_ref` | 验证完整 active 清单、退休审计、原质量和固定材料；持久 inventory、完整 source、capture proof |

每份贡献使用 `document.source-contribution.v1`，保存原值、日期、单位、证据和快照内 pointer。v16 使用既有中立行转换；转换前完整输出仍在固定旧投影中，证据指向该投影。AI 摘要、Report 私有正文和新私有 projection 不进入中立事实。

`document.source-inventory.v1` 的 `contributions` 仅表示当前有效集合，`retiredContributions` 只作更正、撤回、替代审计。不能把所有历史正式修订求并集作为当前事实。完整 source 为 `document.source-observations.v1`，其 coverage 固定绑定 inventory 引用、摘要和有序贡献 ID。原解释 partial/unavailable 不能移入 details 后无条件标成 accepted；accepted 需要原质量和有效集合完整性同时成立。

每份完整来源最多 1000 条观察、1000 个有效贡献和 1000 个退休审计项，规范化内容不超过 4 MB。超限明确失败，不截断后声称完整。不同真实材料修订混合的组合当前不支持；不得改标签凑齐版本。

## 新来源的真实准入顺序

1. 通过兼容 prepare 固定 `DocumentPreparedPublicationInput`，保留同次 preparation receipt；该操作为 `publish=False`。
2. `prepare_fresh_plan(scope, prepared, request_id)` 先纯读固定准备、Document 正式历史和 Profile presence。已知历史、unknown/truncated/present 在任何写入之前拒绝，旧来源继续使用原 legacy 入口。预检通过后验证原始质量，持久 prepared manifest，再通过共享来源 ledger 的单文档 CAS 将 mode 从 legacy 置为 admission；已有正式 ticket、历史 event 或不同请求绑定拒绝。
3. gate 已阻断旧入口后，重新有界读取 Document 正式回执、Report 命令、事件意图和旧观察行，并重新读取 Profile 全历史 absence。此时发现 race/unknown 或取消，只有 CAS 证明本次 owner proof 从未签发、没有事件和旧 ticket 时才撤销本次 gate 回 legacy，保留最近一次撤销审计。
4. 先通过 CAS 持久标记 owner proof 已签发，再持久 owner fence、Profile absence proof 和单 base verified plan；`capture_source` 生成完整 source/inventory。签发后的失败或 ACK 未知保留 fence，通过原 request/manifest 恢复，不能仅查 Profile marker 暂时为空就解锁，否则会与尚在插入 marker 的授权并发。
5. 根协调器显式调用 Profile 的 fresh reserve/authorize。Profile 自己再次验证 owner fence、全历史 absence 和首次完整材料，并持久 blocked/ready。
6. `activate` 纯读 Profile 公共 `get_cutover` 的 ready 回执，精确核对 scope/source、owner proof、absence proof、首次 source ref/digest，再将 Document mode 改为 events。
7. `DocumentCompleteSourcePublisher` 使用现有持久 outbox/recovery 发送。只有接受回执不能认定已经应用，生命周期仍按 accepted/applied/settled 和撤回水位判断。

这里的 `activeLegacyWriters=0`、`activeLegacyWorkers=0` 指没有执行者持有能够正式写入或投递的许可：真实 ticket 集合为空，CAS 已禁止新许可。尚未取得许可的旧任务可以继续计算，但其最终正式 save 必须再次进入同一 gate 并失败。它不声称所有 Python 进程、OCR 或模型准备任务已经停稳；`publish=False` 准备允许存在。Profile 的任何旧 job 历史仍独立阻断 fresh。

owner fence 的固定读取与 `is_admission_fence_held` 分开。授权时检查当前持有；历史完整 source、inventory、retraction 和 plan 读取只验证固定证明，归档或后续事件发生后不重新扫描实时 absence，也不要求旧 stateVersion 继续有效。

## 公开接口与装配

| 接口 | 行为 |
| --- | --- |
| `read_source_observations(scope, snapshot_id)` | 只读完整 source，重核固定 plan、输入证明和 inventory；缺 proof 明确不可用 |
| `read_source_inventory(scope, snapshot_id)` | 只读同一固定清单；不查 latest，不建立新准入 |
| `read_retraction(scope, snapshot_id)` | 保留现有 `document.retraction.v1` 规范内容 |
| `ProfileDocumentCompleteSourceReader` | 仅把公共 ProfileScope 转为 DocumentScope，不访问 Profile 私表 |
| `ProfileDocumentOwnerFenceReader` | 暴露固定 owner proof 与当前 held 检查，二者不混淆 |
| `read_status` / `recover_unissued` | 纯读 admission 状态及撤销审计；显式恢复 lost gate ACK 只允许精确原请求且 proof 从未签发 |
| `DocumentCompleteSourcePublisher` | 显式接受完整 source；保持原契约名，复用持久 ledger/recovery、纠正和救援撤回 |

旧 `DocumentPublisher` 仍接受原 `document.facts.v1`，不会将完整 source 冒充单份 Facts。新入口不自动切 gate，不自动授权 Profile，不能绕过缺失 ready 的失败。

装配函数位于 `adapters/analysis/complete_source.py` 与 `adapters/analysis/source_admission.py`，构造零 I/O。新准入证明集合要求唯一 `_id`、majority 写、PRIMARY 读、无 TTL。旧回执、Report 命令和观察集合继续复用；所有旧 Publisher、兼容命令、新准入和事件 worker 必须共享同一 ledger。根装配负责集合和索引，不新增客户端、DDL 或 API 环境开关。

Document 历史扫描按 user/source 固定查询，最大 1000 行；Report 材料引用解引用共享同一个 1000 次预算，整个读阶段有 10 秒上限。每次超限返回 unavailable 与明确 reason。为证明空集而遇到同用户过多无关历史时也保守拒绝，不能按截断结果放行。

## 修订、恢复与剩余迁移

`sourceRevision` 表示实际材料修订，`legacySourceRevision` 是历史 opaque 整数，`publicationRevision` 才表示事件顺序。同一原件的固定解释更正可以仍为材料修订 1，但完整 source 的内容摘要与引用必须变化，事件修订递增。纯读旧快照保留旧值，不能借当前 index 补旧字段。新事件不伪造旧 opaque revision。

跨集合没有事务：gate/proof/source/outbox 的中间状态均可观察，ACK 丢失重放固定身份；缺最终 capture proof 的 source 不可读取。未签发 proof 的 gate ACK 未知可显式 `recover_unissued`；签发后不可借此回退。撤回、预算、取消和重试继续遵循 [生命周期说明](document-publication-lifecycle.md)。admission/events 来源不能通过普通 `switch_mode` 回退旧 sink，撤回后的旧材料不能因此复活。

生产来源迁移仍依赖以下独立交付：

- Profile fresh presence/admission/archive 已有公共提供方；本域离线组合验证固定 bundle、真实双侧 proof、blocked/ready、只归档、事件应用/撤回及 ACK 恢复。公共 archive 保存真实历史 opaque 身份，但不排旧队列、不 capture 旧观察；生产集合装配与真实 Mongo 并发仍需验收。
- managed 来源的 base 更正与 Report v16 补充已提供独立 active/retired 计划：固定 prior 生命周期、CAS、有效贡献与退休证据。当前 fresh 单 base 计划不能代替它，统一正式命令及消费者仍待接入；旧 v16 正式 Publisher 在 events mode 仍被阻断。
- Record/Report/Chat 的消费回执、history、恢复和 Observation v2 接入需在提供方完成后各自迭代。不能先默认切 Record，再让 Report 无法为同一来源补充材料。
- 独立 Analysis 的默认正式 save 也必须接入 managed 来源公开命令。新入口须显式选择新的 Publisher/consumerRevision，不能在 `report-fixed-documents.v1` 中隐式改变旧正式命令；真实 archive opaque revision 与事件 publication/job 回执分别表达。固定旧任务可以明确拒绝已不可执行的版本，新默认入口则必须有实际 managed 分派。
- Questionnaire 的既有附件归档发布需单独提供兼容命令：读取 Q 公开固定 archive 与已提交证明，保留旧 fingerprint/uuid5 来源 ID、原解释和真实版本，禁止重新 OCR/模型或将 Q archive 冒充 Document canonical bundle。Q owned 材料查询也需后续转为公开 reader，不在本批跨域修改。
- 已有历史来源只有在旧执行者真实排空且当前有效集合完整可证时才可迁入；不自动扫描后伪造 quiesced 或将历史并集作为当前集合。

离线测试中的真实组合指调用实际 Document/Profile 提供方实现，存储由支持实际 BSON 编解码和 CAS 查询语义的测试集合替代。它覆盖旧来源预检拒绝后可继续 legacy、gate 后历史竞态安全撤销、proof/激活 ACK 未知恢复、归档后纯读不重查 absence，以及同材料修订下新摘要与事件修订递增。没有连接真实数据库、调用模型或执行部署；不能把这些结果外推为真实数据库并发验收。
