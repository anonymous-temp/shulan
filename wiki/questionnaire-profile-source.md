# Questionnaire 独立画像来源消费

> 状态：Questionnaire 本域消费者、固定材料、持久任务和实际 owner 准入桥已实现；默认启动装配已接入共享 Document/Profile 图。Profile Questionnaire V2 已进入共同基线并接入实际 DTO、fresh 准入、resolver 和治理回归；Document 附件 Publisher 也已进入共同基线，并通过真实公共组件与离线存储/模型替身的完整组合。离线通过不代表真实部署或历史来源迁移完成。

## 1. 接受版本与业务提交

新问卷只有在显式注入 `profile_source_delivery` 时，才在创建记录中固定 `profileSourceSelection`。支持的消费者为 `questionnaire-profile-source.v1`，来源材料为 `profile.questionnaire-source.v2`，附件命令选择为 `document-questionnaire-attachment-publication-v1`。策略中的次数、间隔、总时限和租约在接受时一起固定，部署更改不扩张已接受任务预算。

创建时没有版本标记的旧问卷继续使用原正式链。当前公共 `ProfileScope` V1 只接受不超过 256 字符且已规范化的用户 ID；旧问卷 API 允许 512 字符。因此新创建的 257–512 字符用户 ID 和带首尾空白的身份在首次接受时固定为 `questionnaire-profile-legacy.v1`，不裁剪或修改身份，也不增加 HTTP 参数限制。规范化长 ID 保持旧链可摄取能力；空白 ID 的旧队列行为没有在本次隐式修复。已经接受事件模式的来源绝不因错误回退旧发布。

提交仍由原 Questionnaire 业务租约和 Mongo CAS 保存。原始 `rawAnswers`、规范化值、完整问卷、解释与附件归档继续保存；同一提交写入紧凑 `profileIngestion` 意图，包含 `deliveryMode=profile-source-event.v1`、固定消费者、原文摘要、附件清单摘要和策略。没有在原提交文档中再次复制整份原文或大材料。

提交之后的 `ensure_queued` 仅尝试登记本域任务，不调用 Profile、Document 或模型。不论画像提供方是否可用，原提交和独立因子分析仍可完成。提交确认丢失按原 canonical answer hash 重放第一次业务结果，不替换首次原文。HTTP GET 不驱动来源发现或恢复。

## 2. 独立后台顺序

后台读取已经提交的唯一 Questionnaire 记录，并逐项核对固定原文与意图，再执行：

1. 解析并保存本人 scope，生成固定来源材料。
2. 验证本域 owner fence 和 Profile 全历史 absence，持久保存授权后 reserve，再 authorize 至真实 fresh ready。
3. 读取公开的固定 submitted-material，验证其 `submissionProof` 与答案材料一致。冻结附件命令引用，再调用 Document 附件 Publisher。
4. 只有全部附件得到真实持久记录回执，才发送答案事件。同一附件记录的真实 `pending` 也是持久受理；不伪造 `queued`，也不等待附件画像应用完成。
5. 保存答案接收回执，再独立轮询实际应用状态。`received` 不等于 `applied`；`partial`、`failed`、`superseded` 保留提供方真实治理含义。

附件命令准备后不再读取 latest。附件确认丢失重投原 command ID；答案首次确认丢失重投原完整 event，不能仅凭相同 event ID 的查询结果推断正文已经一致接受。已有本地接收回执后才能纯轮询。附件与答案分别保存状态和回执，答案失败不会重发已经受理的附件。

每次领取持久消耗尝试次数，进程中断也不退还预算。外部调用受剩余任务时限、每次 30 秒和当前租约一半三者最小值约束；调用后再次 CAS 校验 owner，迟到执行者不能覆盖新 owner。耗尽或过期进入明确失败；只有显式原因与新策略的 transport redrive 才能重新开启传输，累计尝试与旧策略仍保存。Profile 的终态治理回执不由本域 transport redrive 修改。

## 3. 材料与容量

本域 raw draft 保存完整原始答案、问卷 schema、接受的每题证据和 canonical answers，附件正文由既有 Questionnaire 归档及公开 submitted-material 管理。Profile V2 材料仅包含中立答案行和 `questionnaire-submission-proof.v1`，不包含 AI 建议、报告摘要或由业务域声明的权威事实。

答案映射冻结旧语义：text 使用已经接受的规范化值；选项使用原 schema 的文字和顺序；明确“没有”保留原 none label；未回答不产生陈述。JSON pointer 对 `~` 和 `/` 转义，原始名称不裁剪。映射按原 schema 数组顺序执行，分块 JSON 的对象键排序不会改变问卷顺序。

原问卷的 15 MiB BSON 上限不能直接作为 JSON 上限：控制字符的 JSON 转义可能扩大内容。合法旧输入可产生超过 Mongo 单文档上限的 JSON。独立 store 将 raw draft 和公开材料都编码成固定 512 KiB 块，按每块 SHA-256、总 SHA-256、长度和顺序校验；全部块持久确认后才写唯一不可变 manifest。任务、授权、owner proof 只保存紧凑引用。中断留下的未完成块不能被公开 reader 读成完整材料，重试复用原块。所有单块文档都低于 Mongo 单文档限制。

Raw 分块预算为 96 MiB，覆盖原 15 MiB BSON 在最坏 JSON 转义下的上界和元数据；公开材料在准备与固定读取时使用已合入的 Profile V2 DTO 校验，规范化输出必须逐字段等于本域冻结材料，摘要不会因提供方隐式补值而改变。不得用本域分块上限扩大 HTTP 输入容量，也不得为适配提供方截断问卷证据。

## 4. 准入与隔离证明

永久 owner fence 由实际保存的提交证明产生，而不是调用方的 `quiesced` 布尔。事件模式与原文从首次提交就一起存在；兼容版本的旧 Questionnaire sender 和 pending 扫描会在任何入队、queued 早返回之前拒绝此模式。旧版本进程绕过该协议不在这一证明内，需要部署排空。

首次授权同时核对真实 Questionnaire 原文、固定意图、独立任务以及不存在未绑定 owner proof 的本域正式派发记录。Profile absence 检查覆盖相同 source 的全部历史 job、所有主体上的事实贡献和 publication，不将“没有 pending”当作无历史。附件是独立 `health_report` 来源，不作为 `questionnaire` 答案来源的旧历史；附件仍由 Document 保护其自己的正式来源入口。

`read_admission_fence` 只读已固定的历史证明；`is_admission_fence_held` 另行复核当前固定提交。因子结果与无关业务 revision 的变化不改变原文身份，原文、模式、schema、附件清单或提交时间变化会拒绝继续。ready 重放复用原授权，不因本次已经正式发送后出现历史而重新伪造 absence。

## 5. 资源装配与恢复

先调用 `create_questionnaire_source_resources(collection=..., questionnaires=...)`，它只构造本域 store、PRIMARY owner reader、固定材料 reader 和 fence reader，不做 I/O、索引创建或临时授权。公开 snapshot reader 只依赖 source store，可在构造 Profile 之前注册。

组合根随后注册 `('questionnaire', 'profile.questionnaire-source.v2')` 的公共 resolver 和 `questionnaire` owner fence reader，再构造实际 Profile 准入、Document 附件 Publisher 与 `create_questionnaire_profile_source(...)`。Document Publisher 用 `QuestionnaireDocumentAttachmentAdapter` 转换公开 `DocumentScope` 后传入本域 `attachments` Port；薄桥只转换值对象，不替 Document 执行就绪校验。注入 Questionnaire service 的 `profile_source_delivery`，并独立调度 `recover_pending`；默认容器按此顺序接线，显式旧服务注入保持原路径。

补偿扫描按用户与问卷 ID seek，持久保存游标并在扫描尾部绕回。即使一批开头是无效、未知或已经失败的来源，也会继续推进；游标之前新提交的实例在下一轮完整扫描覆盖。普通查询不触发扫描。任务和材料放入显式注入的独立集合，构造与固定读取不建索引，不对业务原文回写后台状态。

## 6. 验证边界

对应测试位于 `tests/unit/questionnaire/test_profile_source.py`、`test_profile_publication.py`、`test_source_material.py` 和 `test_source_admission.py`。覆盖业务保存失败、独立摄取、首次旧版本选择、完整原文与顺序、大 JSON 分块、丢块/损坏/ACK、中断尝试预算、失租、deadline、扫描绕回与固定授权。另有 `test_public_profile_source.py` 使用实际 Profile V2 DTO、fresh 准入、resolver、应用 Worker 和离线模型，验证授权/投递 ACK 丢失重放、最大 pointer/session、大材料与旧 sender 隔离、全部旧 job 状态和跨主体历史事实拒绝 fresh；该文件的附件 Port 使用显式 Fake。`test_document_source_composition.py` 再组合真实 Document 公共附件命令、旧兼容保存/队列与共享发布 gate，验证实际 ready 后附件先受理、pending/queued 与答案应用分离、原始 partial 解读和顺序去重、三阶段 ACK 丢失重启，以及固定附件丢失后零正式写入。所有数据库和模型 I/O 使用离线替身；完整测试与非作者复核绑定最终候选，默认装配另有根接线回归；真实服务验收仍是后续边界。

相关提供方：[Profile 公共准入](profile-source-admission.md)、[Questionnaire 已提交材料](questionnaire-submitted-material.md)、[画像多来源材料](profile-multi-source-publications.md)、[Document 问卷附件发布](document-questionnaire-publication.md)。

显式的两阶段组合根 helper、永久集合命名和单 worker 接线见 [Questionnaire 正式来源分阶段装配](questionnaire-source-composition.md)。该能力已在默认容器的默认 Document/Questionnaire 路径开启。
