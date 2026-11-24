# Profile Record v2 与部分应用结算

本页记录 Profile 提供方的独立增量。代码没有切换 Record、Chat 或 Questionnaire 消费者，也没有新增 HTTP 接口。Record 的正式迁入要求来源 owner 选择 `profile.record-source.v2`，完成不可变材料持久化、公开读取、来源发布顺序和旧投递停稳。旧 `profile.record-source.v1` 仍以 `source.record_cutover_not_ready` 拒绝正式授权与接收。

## 原始材料与固定提取

公共 `ProfileRecordSourceSnapshotV2` 从 [模块 facade](../modules/health_profile/contracts.py) 导出，保存 scope、sourceId/sourceRevision、sessionId、roundNumber、completedAt，以及以下原始材料：

- `qaHistory` 保留原始数组位置，包括旧提取器会跳过的空项；不得先压缩或重排问答。
- `userInputs` 仅包含明确 provenance=user_input 的 name、gender、age、height、weight。
- `hospitalData`、`weRunData` 保留原始列表与嵌套 JSON；`smartDeviceData` 保留原始键和值。

Profile 复用原 `RecordAnswerCandidateExtractor.extract` 的清洗、16,000 字符附带资料预算、分批与 Prompt。医院和运动材料保持列表顺序，设备字段按原规则排序；不增加模型调用或要求业务域预先声明权威 factType。材料超过公共快照的 4 MB 硬上限会明确拒绝。

`profile-record-source-v2` 固定处理修订包含材料契约、子 Agent 版本、映射器、主体解析器、事实注册表和治理策略版本。基础候选先写 `basicCandidatesCheckpoint`；模型每批原始返回先保存，完整提取完成后再保存 `extractedCandidates`。重放复用已保存输出。模型成功但输出尚未持久时仍可能重调，不承诺模型恰好执行一次。

`prepare_self_scope(user_id, display_name=...)` 是显式命令。Record owner 只能将明确 user_input.name 传作首次本人名称；已存在主体保留原名称。快照读接口不会创建或更名主体。

## 部分应用与人工治理

固定提取子 Agent 版本暂不可用时，也可在基础映射器与治理 manifest 完全匹配的前提下完成基础 partial；不会因此改用 latest。处理版本、scope、摘要或权限不匹配则继续拒绝。

模型调用持续失败、返回格式不可用或恢复尝试耗尽时，已有持久基础候选可进入独立 `partialPlan`。只有明确的 user_input 基础候选参与该计划；它不撤销旧来源的非基础贡献，不将模型未提取的内容视为已处理。部分计划也有持久次数上限、租约检查和逐事实幂等记录。

性别、年龄、身高、体重继续沿用原 `USER_CONFIRMED` 与原数值约束。更正不能覆盖人工修改、确认或删除；对应关系不明确时仍返回 `source.manual_governance_requires_review`。来源撤回沿用既有治理：用户确认的事实可以保留人工依据，而不是随来源被删除。

`redrive` 可显式重新驱动 failed 或 partial 发布。完整计划复用自身部分计划的事实 ID，并排除这些事实的自我撤销；不会先撤销，再被幂等 create 跳过而永久丢失基础值。部分计划后发生人工变更时，完整重驱须停止并交人工处理。

## 回执、水位与撤回结算

`ProfileSourceReceiptV1.applicationState` 区分：

| 值 | 含义 |
|---|---|
| `not_applied` | 尚无持久治理计划完成的依据 |
| `incomplete` | 已有治理计划但未完成；可能已产生部分效果 |
| `partial` | 仅受控基础计划完成，完整来源提取仍未完成 |
| `applied` | 该具体发布的完整治理已完成 |
| `superseded_by_retraction` | 该发布被后续撤回结算，不代表它曾完整应用 |

`partialApplication` 保留部分计划状态、次数、原因和已完成结果；`supersededByPublicationRevision` 指向结算它的撤回发布。partial 不推进完整应用或结算水位，普通 correct 不能跳过失败/partial 前驱。

`settledPublicationRevision` 表示已连续处理或由明确撤回结算的位置；`appliedPublicationRevision` 表示最近完成完整治理的发布修订，不能用大于等于关系推断所有历史发布均已应用。`required_publications` 查询具体发布状态，并在快照 coverage.requiredPublicationStates 中返回 partial、incomplete 或撤回结算状态；只有具体状态 applied 才满足 requiredPublicationsApplied。

接收预算按已结算水位计算：publish/correct 最多提前 1,000 个修订，并为 retract 保留第 1,001 个槽位。超界新事件在任何 alias 或 publication 写入前返回 source.unsettled_revision_limit_exceeded；已经接受的同身份重投仍幂等。历史已接受但超过此预算的撤回返回明确 failed/source.settlement_revision_limit_exceeded，并持久标记以停止重复全扫，需要 owner/operator 通过合法中间撤回或后续提供方治理处理；不会无限显示 waiting，也不会伪造结算。

后续 retract 可结算连续已受理的 failed/partial 前驱及其等待更正，缺号时继续等待。撤回计划先持久记录这些前驱的完整和部分 create 种子，包括尚未落库的确定事实 ID，再撤销来源贡献、保存结算标记并以租约 CAS 推进水位。游标提交前失败不会提前将前驱显示为已结算；重放继续同一计划。接受撤回屏障后禁止重驱旧发布，过期执行者的迟到 create 不能复活已撤回贡献。另一条合法撤回也可结算此前失败的撤回。

没有跨多文档事务承诺；处理期间可能看见部分效果。读取会通过 applying/pending、具体发布状态与乐观画像修订检查说明这一限制。

## 消费方接入条件与验证

正式授权必须设置 `ProfileSourceCutoverAuthorizationV1.materialContractVersion='profile.record-source.v2'`，并继续声明旧发布器、旧 Worker 已停稳、旧来源投递意图已退休。已有旧授权缺少新增可选字段仍可按原语义重放；它不能自动获得 Record v2 能力。注册 `ProfileUserSourceResolver` 时绑定明确的 provider 与 v2 契约；retract 使用已有独立不可变撤回材料。

[离线回归](../tests/unit/health_profile/test_record_source_settlement.py) 覆盖原始材料与旧提取输入等价、部分结果、完整重驱、真实回执、乱序缺号、撤回种子、游标中断、过期执行者、人工修改和版本失配。使用 Fake 数据库和模型，没有真实数据库迁移、模型调用或正式消费者切换。

相关说明：[公共画像契约](profile-public-contracts.md)、[多来源原始材料](profile-multi-source-publications.md)。
