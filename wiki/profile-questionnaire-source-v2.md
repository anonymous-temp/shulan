# Profile 问卷来源 V2

本页记录独立 Profile 提供方 `profile.questionnaire-source.v2`。它接收 Questionnaire 已提交答案的完整旧映射及固定提交证明，复用既有提取与治理流程；没有修改 Questionnaire HTTP 校验、提交容量、Prompt、模型调用次数或全局 `ProfileScope`。V1 契约、容量及历史重放逻辑保持不变。消费者持久材料、后台发布、附件命令和默认装配由各域独立交付，本提供方不自动迁入任何来源。

## 公共契约与提交绑定

从 `modules.health_profile.contracts` 导入 `ProfileQuestionnaireSourceSnapshotV2`、`ProfileAnswerEvidenceV2` 和 `ProfileQuestionnaireSubmissionProofV1`。快照使用 `sourceType=questionnaire`、`status=submitted`，`sourceId` 是问卷 ID，`sourceRevision` 是已提交业务材料修订；`sessionId` 保留原值，长度最多 512。`scope` 仍是原 `ProfileScope`，不能用问卷答案声明权威事实或扩大主体权限。

| 字段 | V2 约束与含义 |
|---|---|
| `answers` | 最多 500 条完整已映射答案，保留原顺序；继承索引及指针去重规则 |
| `answerIndex` | 非负严格整数，对应原答案位置 |
| `sourcePointer` | 最多 1039 字符；原问题名经 JSON Pointer 转义后的路径 |
| `questionId`、`intakeDimension`、`question` | 各最多 512 字符，保留原值 |
| `questionType` | `text`、`radiogroup` 或 `checkbox` |
| `answer` | 严格字符串，保持 Questionnaire 既有 `answer_rows` 的映射结果，不截断或重新摘要 |
| `answeredAt`、`submittedAt` | 最多 128 字符，保留原始时间表示 |
| `provided` | 只能为 `true` |
| `submissionProof` | 必填的 `questionnaire-submission-proof.v1` 中立提交证明 |

`submissionProof` 必须包含 `contractVersion`、`status`、`deliveryMode`、`userId`、`questionnaireId`、`submissionId`、`sessionId`、`sourceRevision`、`submittedAt`、`answerHash`、`rawAnswerHash` 与 `attachmentInventoryDigest`。模式固定为 `profile-source-event.v1`，状态固定为 `submitted`；三个摘要必须是 64 位小写十六进制。证明的 userId、问卷 ID、submissionId、sessionId、修订和提交时间分别与外层 scope、sourceId、sessionId、sourceRevision、submittedAt 完全相等。缺失、冲突或附加未知字段均拒绝。

该证明不含附件报告正文、摘要或事实。Profile 公共 resolver 校验整个来源材料的 scope、来源身份、修订与内容摘要；附件 Publisher 还须通过 Questionnaire 公共已提交材料 reader 独立核对完整证明与摘要，不能仅因 DTO 可解析就宣称业务已经提交或附件获准发布。答案来源与附件来源分别治理，不能从问卷来源重复发布报告事实。

## 容量与旧输入兼容

V2 的完整规范化 JSON 上限是 32 MiB，按 `json.dumps(model_dump(mode='json', by_alias=True), ensure_ascii=False, allow_nan=False)` 编码后的 UTF-8 字节计算，包含 envelope 和提交证明。32 MiB 只属于该 V2 契约；V1 仍是原 4,000,000 字节，全局合同和 Questionnaire 的 15 MiB BSON 容量均不扩大。

此界限绑定当前 Questionnaire 提交布局及 `answer_rows` 映射：问题名最多 512 字符，最坏 JSON Pointer 为 `/answers/` 加 1024 个转义字符再加 `/value`，总长 1039；题目数最多 500。现提交正文在 `questionnaire`、`answers.question` 与 `submissionResponse.questionnaire` 保留三份相关问题材料，JSON 控制字符最坏转义为六个字符，因此映射主值的预算按两倍 15 MiB 计算；名称重复余量最多 `4 × 512 × 500 = 1,024,000` 字节，checkbox 连接符余量最多 `199 × 3 × 500 = 298,500` 字节，再预留行固定字段 256 KiB 与 envelope 64 KiB，总预算 `33,107,460 < 33,554,432` 字节。未来改变该持久布局或映射算法必须独立复核容量，不能沿用这个推导自动放宽新契约。

真实离线容量回归使用一个合法 radiogroup，其 choice.text 为五百万个控制字符 `\x01`；原 Questionnaire 校验、准备与提交容量检查均通过，原提交 BSON 约 15 MB，映射 JSON 约 30 MB。V2 接受完整材料，V1 仍明确拒绝。另测精确 32 MiB 与多一字节、合法 500 题及 501 条拒绝、512 个 `~` 的转义指针和 512 字符 sessionId。消费者应使用自有分块或其它适当持久机制保存材料和完整摘要，不能把 32 MiB JSON 直接塞进单个 MongoDB 文档。

## 固定处理与恢复

`ProfileUserSourceResolver` 使用明确的 `provider + profile.questionnaire-source.v2` 注册键及来源域的只读 reader。`DurableProfileSourceExtractor` 首次执行持久固定 `processingRevision=profile-questionnaire-source-v2`、V2 材料契约及精确 `ANSWER_EXTRACTION` 选择；实际沿用原 `profile-record-extraction-v4` 子能力。输入行的字段顺序与 Questionnaire 旧 `answer_rows` 一致，既有模型正文与分批规则不变。V1 仍沿用自己的转换和处理修订。

每批模型结果按原流程先保存后解析，重复投递或治理重试复用已经保存的输出；固定版本不可用时沿用明确版本错误，不能改用 latest。模型返回与持久保存之间中断仍可能重调模型，不宣称模型恰好执行一次。收到事件、提取完成与 applied 保持原有独立阶段。

新鲜来源的 `reserve_fresh_source` / `authorize_fresh_source` 接受显式 V2 材料契约，仍要求真实 owner fence、全历史缺席检查和固定授权，不因材料升级而豁免。历史 V1 的明确授权仍保持兼容。Questionnaire 旧 sender 的 deliveryMode 门禁和 store 的 limit 前筛选分别由各自独立提供方负责，见 [来源准入与旧入口隔离](profile-source-admission.md)。

## 身份范围与消费者责任

V2 没有扩大 `ProfileScope.userId` 的 256 字符限制。源码核对显示，Questionnaire 接受至多 512 字符的身份，旧 Profile queue、本人主体和旧问卷处理链对规范化 userId 没有同等长度上限，因此规范化 257–512 字符 userId 存在可用的旧兼容能力。消费者必须在业务创建或受理时固定选择 `legacy` 保留该能力；不能先接受新 deliveryMode，再在后台失败后切回旧链。此兼容选择属于独立 Questionnaire 消费者，本提供方不代改其受理流程。

已接受新模式的来源继续使用原固定合同，身份不支持时保持可观察的失败或不可用状态；它不能阻塞或撤销已保存的 HTTP 业务提交。原带首尾空白的 userId 在旧 queue 规范化后可能无法精确读取原问卷记录，本次不隐式修复或改变该已有行为。更广身份范围需另立 Scope 契约迭代；本页不宣称 V2 覆盖所有旧 HTTP 可接受身份。

## 实现与验证位置

- [公共快照与证明](../health_profile/source_contracts.py)、[公共导出](../modules/health_profile/contracts.py)、[固定处理与 resolver](../health_profile/source_extraction.py)、[新鲜来源授权](../health_profile/source_admission.py)。
- [V2 回归](../tests/unit/health_profile/test_questionnaire_source_v2.py)：实际 Questionnaire 容量和提交证明、旧新提取正文逐字对照、固定处理清单、持久输出恢复、真实 Profile fresh reserve/ready/receive 与 V1 兼容。
- [多来源原始材料与持久提取](profile-multi-source-publications.md)：既有来源治理、跨主体贡献与恢复流程。

这些验证只使用离线依赖，不调用真实模型、数据库或私有环境。Questionnaire 消费者、Document 附件发布、默认装配和部署后实际恢复仍需各自组合验收。
