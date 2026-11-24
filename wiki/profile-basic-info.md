# Profile 双来源基础信息提供方

状态：2026-11-12，`feature/profile-basic-info@2c618ef` 已由用户通过 !131 合入 `develop@8d8fa7a`。本页说明共享基础信息实现与公共 Python 契约；不表示已部署或消费者业务已接通。当前来源接收提供方已由用户通过 !132 合入 `develop@054a2fc`，见 [Profile 当前来源接收](profile-document-basic-info.md)。共同交接登记为 [I-048](../ISSUE.md#i-048-双来源基础信息与共享profile交接)。HTTP 行为见 [API.md](../docs/api/API.md#用户健康画像接口)。

2026-11-13 决策更新：用户确认无生产数据，不建设旧数据兼容。后续来源接收按 [单流程收敛计划](../docs/architecture/basic-info-single-pipeline-plan.md) 执行；本页中此前有关旧版本保留的说明仅作为上一轮实现记录，不再是新代码兼容要求。正常在途请求恢复、字段治理和幂等继续保留。

## 1. 所有权与接入关口

`health_profile/basic_info.py` 是姓名、性别、年龄来源事实与当前选择的唯一提供方。`modules.health_profile` 暴露 `ProfileBasicInfoService`、`MongoBasicInfoStore`、`BasicInfoConflict` 和 `BasicInfoUnavailable`；DTO 从 `modules.health_profile.contracts` 导入，统一用 `model_dump(mode='json', by_alias=True)` 或快照、值对象、回执、分页对象的 `as_dict()` 序列化。消费者不得读取私有集合或复制选择规则。

组合根通过已公开的 `create_application_profile_public_contracts(...).basic_info` 获取与 reader/inbox 共用同一 Repository 和真实集合的提供方；工厂可显式指定 `basic_info_collection`。构造无 I/O，消费者无需访问 `reader._repository` 或 `service._basic_info`。

真实 `MongoHealthProfileRepository` 可显式注入 `basic_info` 集合；未提供时，实际 PyMongo 异步 `views` 集合自动派生同库 `<views集合名>_basic_info`。构造不访问数据库。旧的无该能力测试替身保持旧接口。该集合使用 Mongo `_id` 唯一性与单文档 revision CAS，保存范围内来源、选择、字段治理水位及命令；不设 TTL。单个范围文档最多 12 MB，达到预算明确失败，不丢弃旧来源、pending 命令或静默降低保障。

共享基础信息已合入 develop；后续各消费者从最新 develop 接入真实 DTO/Service，再完成真实组合与各自完整测试。正式 Document V2 接收由新的 Profile 独立分支提供，不在消费者中复制实现。Document V2 的 neutral basicInfo 数组适配、HealthAnalysis Runtime 补偿与 Record/Chat 的 V2 人口学上下文适配仍由对应模块交付。旧 Record/Chat 域事实 DTO 要求已知 datetime，未知报告观察日期不得伪造后塞入旧 V1 上下文。

## 2. 当前值与来源

每个字段独立保存来源与当前选择，自动来源可信度固定为 `low < medium < very_high`。报告 AI 只能给出 `low`、`medium` 或未检索到；信息明确为中档，信息不够明确但有单一可辨认值且确定属于目标本人时为低档，主体不明或互斥候选不生成来源，未检索到不发空来源。Profile 校验字段值、档位、单主体、引用证据与范围，API 确认固定 very_high。业务档位不替代事实的 assertion、verification 或 status，也不改临床事实政策。

无当前值时可填入低档或中档；有当前值时，仅更高档来源自动替换（包括同值升档）。同档冲突保留先前当前值，记录候选供 V2 查询。撤回当前依据后按仍有效来源的档位及稳定受理顺序回退。历史无法确认等级的当前事实为 `unknown` 并受保护，不能由报告自动覆盖；已保存的用户确认可认定 very_high。空来源集合不被推断为报告低档。历史观察/确认时点缺证据时不从存储时间编造；非报告历史年龄的 `ageBasis` 可以为 null。

`subject.displayName` 仍是展示标签，可能为“本人”“父亲”，无证据时不迁移成真实姓名。报告抽取只进入姓名事实与 overview，不改人工标签；显式姓名确认或已有改名入口可以更新展示。公开主体读取从 canonical 显式确认姓名投影，不执行可能迟到覆盖新值的 displayName 回写。

新基础字段没有观察日期时保存 null。报告日期、无时区 ISO 时间与带时区时间保留原精度；物化排序中的日期/无时区时间采用 Profile 的 UTC+08:00 日期基准，不把执行时间冒充观察日期。非人口学事实仍要求观察时间。 恢复指纹与确认时间身份比较按 MongoDB 的 UTC 毫秒精度规范化，避免 BSON 回读的时区或微秒截断造成伪冲突；该规范化不重写公开报告日期、原文证据或已登记旧命令的快照。

### 2.1 既有来源写入的兼容同步

Profile 所属的旧摄取 writer 和正式来源应用均通过内部基础信息桥接发布真实已物化事实；不要求 Record、Chat 或其它消费者访问私有状态。来源进入 Profile 后先持久保存操作、主体、生命周期、字段治理水位、事实身份、来源版本、内容指纹及原始时间，再物化事实并发布候选，最后推进原有投影和事件。重试及已有事件的幂等返回也协调同一原始意图，不重新获取确认资格。

旧 Record 明确 user_input 仍为 very_high，confirmedAt 使用原 qaCompletedAt；它是后台来源候选，同档冲突保留当前值。健康分析接口的新明确确认及既有人工更正仍可更正同档旧值。自动来源不推进人工字段水位，也不把后台执行时间当作用户确认时间；非报告来源不标为报告年龄。撤回只退休相应来源，保留仍有效的独立来源，重新选择有效依据。

主体归档或恢复时，同一主体 CAS 保存新生命周期及其开始时间。初始生命周期兼容先受理来源、后首次创建主体；后续生命周期中，无原始意图的来源须以原受理时间和可靠生命周期开始时间证明归属。历史事实标签、固定计划或原范围可以作为恢复依据；缺少证明时不将历史后台任务绑定为当前生命周期的新确认。

## 3. 显式确认命令

```python
async def register(user_id: str, operation_id: str, *,
                   values: BasicInfoValuesV1, confirmed_at: datetime,
                   subject_id: str | None = None) -> BasicInfoCommandReceiptV1: ...
async def apply(user_id: str, command_id: str) -> BasicInfoCommandReceiptV1: ...
async def get(user_id: str, operation_id: str) -> BasicInfoCommandReceiptV1 | None: ...
async def list_pending(*, user_id: str | None = None, limit: int = 100,
                       cursor: str | None = None) -> BasicInfoCommandPageV1: ...
async def cancel(user_id: str, command_id: str) -> BasicInfoCommandReceiptV1: ...
async def read(user_id: str, *, subject_id: str | None = None,
               as_of: datetime | None = None) -> BasicInfoSnapshotV2: ...
```

值对象要求 trim 后姓名非空（不另设姓名字符上限）、严格整数周岁 0–150、`male`/`female`（接受“男”/“女”规范化）。`confirmed_at` 必须是后端已固定的有时区时间。重复 `userId + operationId` 只接受相同规范化 payloadHash，改变值、时间或原始主体请求会冲突。首次操作先持久绑定稳定主体 ID；所有本人建档入口共用创建锚点。ACK 丢失后通过原 operationId 查询，或全用户 `list_pending()` 分页恢复；不得重新解析“当前本人”后重绑。

命令 contractVersion 为 `profile-basic-info-command.v1`。回执固定 `commandId/operationId/userId/subjectId/lifecycleEpoch/payloadHash/confirmedAt/fieldEpochs`；lifecycleEpoch 和命令三字段水位均为正整数。总状态为 `registered/pending/applied/no_change/superseded/blocked/cancelled`；`fields` 始终包含 name/gender/age，逐字段状态为 `pending/applied/no_change/superseded/blocked/cancelled`，并可带 factId 与 sourceReliability。registered/pending 可含已完成字段；终态保留每个字段的原始回执。任一 pending 优先；其后按 blocked、cancelled、superseded 聚合；全 no_change 才为 no_change，其余已完成字段为 applied。

注册索引固定逐字段初始水位；范围 CAS 只为尚未被较新操作改变的字段递增水位，其余字段直接标为 superseded，恢复不得重新领取较新的水位。应用按字段物化兼容事实，再在相同范围 CAS 发布来源、当前指针及字段回执。无变化确认仍登记新的独立 manual 来源、confirmedAt 和字段水位，不能因复用已有 factId 丢掉用户确认。较新的已受理确认、主动更正或删除能阻断旧命令；新 HTTP 请求有意提交新确认仍允许同档改值。部分成功保留字段回执，重试只处理未终结字段。

既有 `subjects/upsert` 显式改名、active 人口学事实的确认/更正、拒绝和删除使用同一字段屏障。先核对既有请求及版本，再持久登记治理意图；失败后原 requestId 可以恢复。 治理意图使用跨生命周期可查的 userId/requestId/field 索引固定原主体、生命周期、初始及分配字段水位、内容指纹及首次确认时间；字段水位在原范围内只推进一次。确认及更正的原、新人口学事实在原业务 CAS 同时保存生命周期标记；仅对有原范围和请求指纹证明的旧中断操作修复内部标记，不改变其公开事实版本。删除擦除相应 canonical 来源的值/证据副本并阻止旧命令补回，既有 fact 墓碑仍生效；`deleteScope=fact` 不删除原报告、原上传命令等各自保留的原始材料。

物化事实标记所属生命周期，报告物化幂等键细化到生命周期与独立贡献。应用在物化前后检查固定主体状态/epoch；新生命周期不将旧生命周期管理事实或未发布物化行当作 legacy 重新迁入。公开读取即使新 epoch 尚无持久范围，也持续过滤这些行。跨文档共享 view 的 epoch/revision marker 可修复，读端不把落后 marker 当成稳定新快照。

## 4. 报告完整库存与撤回

```python
async def ingest_report(user_id, subject_id, *, source_id, source_revision,
                        fields, publication_revision=None,
                        lifecycle_epoch=None) -> BasicInfoSnapshotV2: ...
async def withdraw_report(user_id, subject_id, *, source_id,
                          contribution_ids=None,
                          publication_revision=None,
                          lifecycle_epoch=None) -> BasicInfoSnapshotV2: ...
```

`fields` 是该来源的完整活动贡献数组，含旧有效字段的 carry；不是增量。每项包含 `field/value/sourceReliability/observedAt/reason/evidence`，并保留 Document 的 `contributionId`、固定 facts/understanding 引用、解释轮次/修订、主体证据。reason 最长 2000 字符，evidence.quote 非空，字段数最多 1000。同一报告的同字段多份独立贡献使用不同物化幂等身份；兼容事实中的 sourceId/sourceRevision 始终指真实原始来源。

生产来源适配必须传实际单调 `publication_revision`，不能用内容 sourceRevision 代替。Document V2 另传首次固定的 `lifecycle_epoch`，不允许归档恢复后重新绑定当前生命周期；旧直接调用保持兼容。重复同版本同内容幂等，旧版本不复活来源，同版本异内容明确冲突。省略 publication_revision 仅用于调用方已保证串行有序的直接调用。完整库存缺失的旧贡献退役；不能借“先删旧、再放同档新值”绕过同档保留规则。 Profile 合法的更高 publication 可重新接纳未删除且内容一致的旧贡献；整批活动集更新后，先重选失效的当前值，再按原稳定顺序比较本次有效贡献，恢复的更高档依据可以胜出。合格替换的同档预选同样使用原入档顺序；已删除贡献不能借出高档替换资格，也不得恢复。Document 的永久退休清单另受其提供方约束，不能用更高 publication 绕过。`contribution_ids` 可只撤回一项，省略则撤回整个来源；其它 carry 继续有效。

canonical 撤回提交后，修复对应兼容事实状态；没有其它有效来源时退役，有独立用户同值确认时保留。修复或 ACK 失败可通过相同 publication 再次调用恢复。初次报告第一个字段写入后、第二个写入失败，尚未提交的物化行不会通过概览或公共快照暴露。

## 5. 年龄依据与公开读取

新报告年龄为 `reported_at_report`；用户确认周岁为 `confirmed_at_time`。独立可信生日与该确认在 confirmedAt 的周岁相符时，实时概览使用统一读取 asOf 计算年龄，标 `dob_derived`；迟到补偿不以实际执行日判断相符性。无 API 年龄确认时保留原可信生日推导优先，低档/中档报告不能替换它。生日与确认不符时保持确认年龄及原生日事实，sourceRef 明确 `birthDateDecision=conflict`、比较时间、生日 factId/version 与未采用原因；不自动改原生日物理 status。

生日失效或撤回后回退到保留的最近确认时点周岁并标注；没有生日依据时不自行给时点年龄每年加一。`as_of` 是公共读参数与冻结快照元数据，不是上传接口的第五个业务字段。分析消费者固定当次上传年龄及当次 asOf，不随生日、来源补偿或后来画像改动重算旧结果。

`ProfileSnapshotReader.read_v2()` 显式返回 `profile-snapshot.v2`，内含 `profile-basic-info.v2`。basicInfo.fields 给三字段当前值、档位、时间、依据、治理水位；basicInfo.sources 给候选与历史来源及 active/current/deleted 标志。V2 不要求消费者反射私有对象。fresh V1/current 对已启用 canonical 的 active 人口学事实只暴露统一选中依据；临床、pending/disputed 语义不变。分页会补足过滤后的页面，并把字段版本/生命周期绑定进一致性标识。已保存的 V1 DTO、旧任务快照字节与摘要恢复不改写、不升级契约。

## 6. 失败与验收边界

本提供方只做合法参数、用户与主体归属、固定生命周期/版本和取消校验，不引入登录、网关证明、用户群关系授权或持久 grant。后台恢复执行已受理固定命令，不需凭据续期。`BasicInfoConflict` 属于既有 `HealthProfileConflictError`，身份/内容/版本或主体状态冲突沿用公开 HTTP 409；旧导入路径保留相同异常类身份。参数无效为 ValueError/验证错误；存储不可用、容量超限、持续 CAS 争用保留重试语义，不返回假成功或使用 Fake 代替真实提供方。来源摄取边界将基础信息错误转换为各自稳定业务错误：确定的生命周期或内容冲突不进入通用重试，暂时存储故障仍可重试；沿用原队列终止状态，不新增任务系统。

本次 R1–R5 整改增加真实 Record 映射、正式来源应用、手动确认/更正生命周期、报告贡献恢复及实际 HTTP 组合回归，使用真实 Repository/Store/Service 与合成 Mongo I/O，保留历史测试及其断言。最终完整离线测试、Ruff、文档检查、非作者复核和受测版本证据统一登记在 I-048；先前 21cf1d2 的 12433 passed / 275 skipped 仅是整改前基线，不替代修复后验收。未执行真实 MongoDB/Redis 多进程、模型质量或部署验收。
