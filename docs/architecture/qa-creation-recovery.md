# 建档创建失败与恢复

## 1. 两种存储的职责

未完成建档仍只保存在 Redis。创建受理、失败原因及文件检查点也放在原 shulan:session:{userId}:{sessionId} 键中：首次受理起两小时绝对过期，恢复和查询不续期；首题发布后改用现有活动问答两小时 TTL，完成才写 MongoDB QA 历史。

文档索引、归档及已保存报告属于独立文件链路，原来就持久保存。它们用于跨会话复用和防止重复提交，不随 QA 缓存过期删除。更换 sessionId 不会清除文件的提交状态或恢复额度。

## 2. 创建状态与调度

QACreationCoordinator 在处理画像、文件和模型前，以 Redis 单键 CAS 保存规范化输入摘要和状态。相同输入返回已有状态；不同输入返回 CREATION_REQUEST_CONFLICT。重试读取不争抢正在执行的会话锁。

创建请求同步等待首题最多 120 秒，期间完成直接返回首题，当前尝试失败则提前返回对应失败状态；仍在处理时返回 202/processing，由前端继续轮询 status。该等待预算仅用于创建首题；普通答案提交等待本轮完成，SSE 答案提交通过事件返回结果。后台受 TaskSupervisor 管理，HTTP 断开或同步等待到期不取消工作。每次执行最多 600 秒，共最多 3 次；失败返回可展示的错误分类和重试间隔。创建失败可经 status 和 history 查询，history 为空。创建期间不能回答问题或转 Chat。

文件处理质量和建档创建状态分别记录。共享解释器只交付有依据的内容：完整读取为 complete，存在尚无法确认的内容为 partial，完全没有可用结果为 unavailable。Record 将明确的文档解析/解读错误保存为 settled unavailable 附件，继续使用其余可信结果和用户输入；全部附件不可用也可继续建档，读取说明由 documentProcessing[].message 独立展示，不强制加入首问正文。归属/权限、保存失败及 submission_unknown 仍进入创建错误或恢复流程，不作内容降级。

每个输入附件的 documentProcessing 包含 fileIndex、status、message、retryable，以及可取得的 documentId/sourceRevision；既有 documents 仅保存实际采用的报告引用。partial 只包含可信结果，unavailable 不放入 parsedHealthReports，不作为患者事实提供给问答。附件状态和结构化快照都在首题发布时固定，之后文件版本升级不会改变当前会话。文件模型预算和局部恢复独立于 create 的 120 秒同步等待。

进程每 15 秒按 Redis SCAN 游标分批发现到期工作，每批最多检查 100 个会话键、调度 20 个候选；集群模式也使用同一单键 CAS，避免跨 slot 事务。重启重建扫描游标，租约过期后可接管。在途调度受应用生命周期控制，排空时停止接收新恢复工作。

检查点保存首次画像快照、已归档文件的 documentId、已结算附件的结构化快照或 unavailable 状态，以及当前处理阶段。首题生成失败后恢复时，complete、partial、unavailable 附件都复用本次检查点，不重新触发文件处理。旧任务必须同时满足会话租约、创建 revision 和绝对有效期才能更新或发布首题；不能把过期记录重新写回。Redis 不可用时不声称已受理。

## 3. 文件恢复边界

- 本地准备在提交标记前完成；此时失败可以安全重试。
- 明确的上游拒绝与超时、断线、响应丢失分开记录；无法确认受理结果时才进入 submission_unknown。
- 已保存 taskId：文件恢复时继续查原任务。已有解析或解读：读取归档检查点。QA 已结算附件（包括 unavailable）：读取本次 QA 临时快照。
- submission_unknown 且无 taskId：等待 300 秒后，按文件原子认领一次受控重提。不同会话、不同入口和不同进程共享额度。
- 第二次提交仍不确定：停止自动重提。更换 sessionId、重复上传相同字节或反复查询都不会重置额度。
- 供应商可能已接受失联的首次请求，重提可能产生重复远端任务和费用。等待不证明原任务已取消；当前客户端无法在缺少 taskId 时自动查回该任务。
- 原尝试 trace、payloadRefs 及 recoveryHistory 保留，旧租约不能写入新尝试。最终报告来源仍使用同一 canonicalReportId，画像摄取复用已有幂等机制。

DocMind Adapter 关闭 SDK 自动重试，单次连接/读取超时为 10/30 秒，轮询预算 180 秒（最后一次网络调用可能占用额外的连接及读取时间）。查询超时保留任务 ID；这不是取消供应商任务。Analysis/Report 共享文件调用随下一请求恢复，只有 QA 临时创建有周期恢复扫描器。

## 4. 运维恢复

以下命令由开发或运维人员在确认环境后执行。默认只查看，不修改；写入必须使用 --apply 和 --reason。工具按 userId/subjectId/documentId 校验归属，更新时验证读取到的版本与租约；不得删除 document_index 来绕过保护。

查看创建记录，可得到已识别文件引用：

```bash
python -m scripts.recover_qa_creation --user-id TEST_USER --session-id TEST_SESSION
```

查看文件状态：

```bash
python -m scripts.recover_qa_creation --user-id TEST_USER --subject-id TEST_SUBJECT --document-id DOCUMENT_ID
```

优先从原提交日志与供应商记录核实 taskId，确认属于该文件后绑定；下一次处理只查询此任务：

```bash
python -m scripts.recover_qa_creation --user-id TEST_USER --subject-id TEST_SUBJECT --document-id DOCUMENT_ID --task-id VERIFIED_TASK_ID --apply --reason "已根据原提交记录核实任务归属"
```

仍无法找回任务时，可由工作人员明确追加一次重提授权。操作写入审计记录，默认自动额度不会被重置。对于明确未提交的失败，修复文件或配置后也可用此入口重新允许处理；不会重置已经存在的远端任务：

```bash
python -m scripts.recover_qa_creation --user-id TEST_USER --subject-id TEST_SUBJECT --document-id DOCUMENT_ID --allow-resubmit --apply --reason "原任务无法核实，已确认允许额外解析"
```

文件处理障碍排除后，如创建已停止自动恢复，可恢复同一份尚未过期的失败创建记录：

```bash
python -m scripts.recover_qa_creation --user-id TEST_USER --session-id TEST_SESSION --apply --reason "已处理文件或上游故障"
```

会话恢复保留原资料和检查点，重新给出至多 3 次执行机会，记录先前次数与错误；不延长原两小时期限，也不重置文件重提额度。正在创建、活动问答、已完成及已过期记录不能通过此工具重置。过期后由前端明确重新建档；文件若仍未恢复，新 sessionId 也会遇到同一错误。

### 问答来源校验与文档版本更新

`qa/status` 返回 HTTP 200 只表示状态读取成功；需同时检查业务 `status/errorCode/retryable`。历史版本曾因内部依据无法在指定资料中连续匹配而产生 `message_evidence_not_found`。当前问答层不再要求连续或逐字匹配：患者分析和回复中的 `evidence/quote`（JSON 中仍使用原有“依据”“原文”“用户原话”等兼容字段）允许准确概括和非连续摘述。改写措辞、合并相关片段或首题未引用报告，不再因此判生成失败，也不使用固定自然语言正则要求首题包含某种报告限制句式。

保留的校验包括 JSON 结构、字段类型与枚举、既有疑问及响应 ID、来源类别的可用性与归属，以及 action 对问题和选项的要求。主要困扰只能来自用户陈述，不能由报告推断；没有本次回答不能新增用户疑问。来源仍须声明为实际存在的本次回答、历史回答、本次报告、长期画像或附带资料，不能把 AI 问题当成用户证据。提示词要求概括保留数值、单位、比较关系、否认和不确定性的含义，不得捏造或错配事实。

报告结果继续按完整事实行提供，异常项优先；超过上下文预算时省略完整条目，不截断 JSON 或半条结果。模型仅接收“附件资料状态”的 fileIndex/status 投影，不再接收可直接复述的读取提示；该状态只限定资料边界，不是健康事实或报告依据，空数组也不证明资料完整。分析概述和正文不主动重复附件故障，有可信报告就自然承接已确认结果，无报告则使用画像、基本信息或用户当前感受。用户主动询问读取情况或具体医学答复缺少必要证据时，仍如实说明相关限制，不编造依据。此调整不放宽文件解读层对原始结果及其来源的验证，也不新增语义等价判定器。通过结构和来源校验不代表系统已经验证每个概括在语义上都正确。

无效 JSON、结构、ID 或来源等实际生成校验失败仍按既有上限处理：患者分析与回复生成各最多尝试 2 次（首次加一次修复）；创建执行仍最多 3 次，每次最多 600 秒，受原两小时绝对有效期约束。模型调用失败继续按现有错误分类进入恢复流程，不因取消连续原文匹配增加额度。失败日志及内部 `qaCreation.generationDiagnostics` 用于定位生成阶段、字段和来源问题，不保存原始依据或整份模型回复，也不把内部诊断新增到公开 API。

画像日志中的 `report_time_unavailable` 属于独立的报告日期与事实摄取检查，不是问答引文错误。缺少可用日期的报告结果不会进入时效画像，但可信的报告结果仍可以供建档读取。

解释器版本升级不会改变同一建档任务已经结算的文件检查点。若此次修复需要重新解释旧的 partial/unavailable 文档，使用原 `userId`、新的 `sessionId` 和已有 `documentId` 明确创建一次新建档；其余建档输入沿用原请求。收到 202 后使用这个新会话查询状态。新请求复用原文件和 DocMind，由当前版本重新校验并发布来源修订；同一会话重复创建和查询不会触发额外文件恢复。已有完整文档不会自动重算。

只修正首题生成时，可以由运维工具恢复尚未过期的失败会话；这种恢复仍保留旧文档快照，因此不能替代上述文档日期补全验证。历史连续匹配错误记录不会因规则变更自动成为成功，仍需下一次受控生成发布首题。不得通过删除 Redis receipt 或 document_index 绕过幂等和来源保护。

## 5. 前端与验证

create 的 202 与 status 的 processing 表示已受理，沿用相同身份轮询。failed/retryable=true 表示后台仍有恢复机会；false 时展示错误并停止自动轮询。只有 question 才展示回答入口，finished 按原完成流程展示。断线且未收到响应时先查询 status，再用原资料重试 create；不能自动换会话逃避冲突。

API Lab 页面展示创建状态，收到 202/processing 或 failed 后不自动轮询；点击“恢复当前问题”或手动发送 status 查询进度。普通/SSE 命令行客户端仍共享创建等待逻辑，后台创建及恢复继续执行。测试使用合成资料与离线 Redis/Mongo 原子行为模型，覆盖 HTTP 断开、进程中断、过期与旧写入、文件重提竞争、明确拒绝和多次不确定失败。上线前仍需真实 Redis/MongoDB 多进程、强制重启和 DocMind 测试环境联调，离线测试不能证明供应商行为或线上故障已经恢复。

问答依据规则的离线回归应分别验证“准确概括或非连续摘述不再因字面不匹配而拒绝”和“结构、ID、不可用来源、来源归属错误仍被拒绝”。这些用例不构成语义正确性验证，也不能替代真实模型首题生成和部署后的验证；文档中的目标行为不代表线上旧失败会话已恢复。

隔离上传建档验收脚本从本机 HTTP `qa/create` 请求开始，查询 `qa/status` 到首问；使用生产文件协调器、Record/Analysis 图与 HTTP Handler，文档归档、报告来源、画像队列、QA 缓存和会话全部保存在内存，数据库客户端构造被禁止，没有画像 Worker、知识检索或回调。先在项目根目录执行离线演练：

```bash
python -m tests.unit.tools.qa_document_acceptance --smoke --output /tmp/qa-smoke.json
```

在已授权真实调用时执行以下命令；Windows 可将输出路径改为 `$env:TEMP/qa-live.json`：

```bash
python -m tests.api.scripts.qa_document_acceptance --live --config .env --file-url PUBLIC_PDF_URL --output /tmp/qa-live.json
```

`--live` 只读取配置中的三项 LLM 与三项 DocMind 参数。脚本下载 URL 对应的原文件并上传到真实 DocMind，后续调用配置的模型；最多提交一次 DocMind，模型逻辑调用上限默认 64（供应商传输重试保持原配置），总预算默认 900 秒。它不会运行 QA 后台重试扫描。输出必须保存在仓库外，包含耗时、HTTP 状态、附件质量、调用量、分析指标和首问；该首问可能引用报告信息。退出码 0 要求首问成功且至少一份附件为 complete/partial；全部 unavailable 即使首问成功也返回非零，不能误算为文件解读通过。此次验收不检验真实 Redis/MongoDB 持久化或重启恢复。
