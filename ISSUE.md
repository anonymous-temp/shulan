# ISSUE

> Report 状态复核：2026-10-10，基线 `develop@c9b8988`。本轮仅同步源码接线与验收边界，未重跑业务测试、真实服务或部署验证；历史问题背景保留，未满足关闭条件的事项继续跟踪。

## I-051 评估报告整案成稿的已知边界

评估报告默认改为整案成稿（`SHULAN_REPORT_AUTHORING_MODE=case`，说明见 [内容规格](wiki/report-assessment-v2.md)）：模型读完整份资料后决定内容并成文，程序核对引用与数值，危险信号规则只做补漏。以下事项尚未解决，上线前后需要跟踪。

- **孕期与未成年人**：分析方法说明和危险信号规则都按成人撰写，没有针对孕期与未成年人的专门处理和样例验证。
- **库外理想目标与指南名称**：理想目标库（`assessment_targets.json`）之外的理想目标和正文里的指南简称来自模型，有独立复核但没有逐条联网核对出处。
- **后续报告的旧字段**：整案模式只写入后续报告读取的三个旧字段（健康状况总览、菌群稳态小结、综合结论），其余旧的逐格字段不再生成；诊断意见、干预方案和报告解读从这些旧字段取到的内容与逐章路径相同或更少，需要 Report 后续报告负责人确认是否改为读取六章回调。
- **回调追加字段**：`assessment.v2` 新增的展示字段（见 [当前 API](docs/api/API.md)）需甲方前端确认后再依赖；`status` 在体格行、系统、分系统和表格行上改为五档文字。
- **没有菌群资料的请求**：一个不含菌群报告的合成请求在开发机实跑通过（第四章只有一句说明，主诉与集群不写菌群证据）；真实业务里这类请求是否会出现、上游是否放行，未与甲方确认。
- **验证范围**：十个输入（一例真实受检者的两种问卷输入与八个合成画像）在开发机用 `deepseek-flash` 实跑通过；未在测试服、生产配置和真实多用户并发下验证。

## I-050 健康分析生成失败诊断

测试服反馈的故障标签 `error/health-analysis/502` 指向 `c0b75ac`，缺少该次请求的错误阶段和原因，实际触发点未能确认。本地复现并修复规范历史题覆盖漏判、新题面未进入补审和同批重复提前淘汰拟稿三类覆盖失败。其中规范历史题覆盖漏判已在旧标签与整改前分支复现。合成用例覆盖三类路径，测试服复验待完成。

2026-11-18 联调复现健康分析耗时约 60 秒返回 502，诊断定位到联合审核的输出校验。原诊断不能区分缺失或非法的具体字段。本地将版本、卷级规则、题目裁决、结构化意见和建议列表分开记录，允许在原预算内对同一版问卷补做一次完整审核。旧请求的具体缺项无法从现存日志还原。待在联调环境部署并复验。 [日志字段说明](wiki/http-logging.md#http-正文)

## I-049 体检报告写入画像与主体归属

基线为 `develop@6449316`。Analysis 和 Document 交付完整或部分解读的提取结果及固定证据。Profile 默认接纳空画像首份单人报告，按姓名和性别的双项明确冲突隔离其他人；逐项处理临床证据质量。健康分析、Report、Chat 和 Record 读取 Profile 对应主体的资料。

本轮分工：`/root/delivery_implementation` 负责提取、分组及固定投递；`/root/ownership_implementation` 负责主体归属、发布和生命周期；`/root/read_consumers` 负责消费与公开原件视图；`/root` 负责事实契约、规则、实际 HTTP 组合及交付。

适用于新上传。旧报告无需重解读或补投。接口字段见[当前 API](docs/api/API.md)，归档边界见 [Document 技术说明](docs/architecture/document-basic-info-v2.md)。

### 公开 Analysis 接口默认采信

本期按入口固定默认采信策略，仅用于该接口的新临床发布。Profile 负责区分默认采信与人工确认，并分别保存实际观察时间及无日期结果的录入时间。组合验收覆盖双向复用、同轮重放、撤回和无日期报告粗略比较。

**待处理（2026-11-18）** 同一主体和生命周期内，旧版 Report 已按原件位置写入附件 B 的观察。升级后，公开 Analysis 首次发布 B 时复用同一份解读，旧事实尚未关联新的固定观察引用，画像因此可能生成新的 `factId`。合成复现中，两份原件的 ALT 观察从两条增至三条。后续验收要求同一个原件位置沿用旧 `factId`，保留 Report 和 Analysis 两个可独立撤回的来源。

### 后续检测表格的原件核验

当前 Report 原件投影接收固定归档中的 DocMind 表格坐标，支持跨行表头、并排结果列和明确标注的合并单元格。建议区之后的检测表可以独立按项目和结果列准入。对 PDF 单次读取原生坐标，不重复运行 OCR；同一结果位置的两份证据按坐标合并。检查日期按同一页明确元数据或同一结果行核对，参考列无法补成缺失结果。

新原件核验修订在 Document 固定投递时按观察绑定证据。可核对且有有效时间的观察由 Profile 生效，缺值或证据不足的保留待确认，整份解读的部分完成状态不阻挡已核对的条目。固定发布归档继续以原回执读取。

同一份测试归档通过只读 MongoDB 和内存发布验收。114 项临床观察中 11 项按原件自动生效，其中 5 项来自归档表格、6 项来自原生 PDF。其余 103 项待确认，包含全部 8 项缺值。真实知识库与模型复验生成 1 条关键异常提醒、临床综述和 2 条通过引用校验的循证条目。

## I-048 双来源基础信息与共享Profile交接

### 集成分支合入前审核整改（2026-11-15）

候选以 `feature/basic-info-integration@4184f57` 为起点，目标基线为 `develop@eea8890`。R01–R10 均纳入本次合入前整改。

- R01、R04 由 `/root/report` 负责。Report 同组身份只采用可识别的性别依据。Evaluate 专用 PDF 的 OCR 轮询失败后按当次验证的原件走原生恢复。
- R02、R07 由 `/root/consumption_design_review` 负责。模型自由文本按 Unicode 和大小写等价关系去除确认姓名。年龄硬过滤要求原文明确支持同一单位、上下界及开闭。
- R03、R06 由 `/root/profile` 负责。同一主体及生命周期的固定完整或部分档案可独立提供临床资料。正式贡献和显式退休按文档、轮次和观察指针对齐，覆盖多页 Report 的次页，保留同轮其他观察。当前年龄与出生日期分别投影。
- R05、R08、R09 和 R10 由 `/root` 负责。固定本人生命周期冲突沿用 HTTP 409。写关注错误核对原确认回执，按可靠受理状态返回 503 或补偿。自定义消费者沿用应用注册的当前 Analysis 能力。Report 与 Document 的当前文档同步默认装配和实际修订。

本地候选的日志、JUnit 和提交指纹见 `D:/996-work/shulan-test-artifacts/basic-info-integration-review-2026-11-15-final.{log,xml,json}`，非作者复核见同目录 `basic-info-integration-review-2026-11-15-nonauthor.json`。平台 PR 关联、批准及合入由用户处理。真实 MongoDB、模型、回调和部署验收另行记录。

### 模块交接与历史验证

统一集成分支 `feature/basic-info-integration` 从 `develop@eea8890` 建立，已保留 Profile `78a7b3f`、Document `3ee10b2`、Record `8f9a0ac`、Report `a924478` 和 Health Analysis `fc9246a` 的任务提交。本批所有本地合并都进入该集成分支。用户对完整差异一次审核，由 Git 服务器合入 `develop`。协调者负责默认组合根与综合验证。`/root/profile` 负责固定资料和画像消费者，`/root/report` 负责 Health Analysis 确认、补偿及接口回归，`/root/consumption_design_review` 负责独立 Analysis 入口与回执恢复。

`feature/profile-original-occurrence` 由 `/root/profile` 负责。同一主体和生命周期内，经 Document 验证的原件位置共用一个 Profile 事实，来源指针和发布修订分别保存，可独立撤回。`/root/consumption_design_review` 以两套实际 Profile Worker 和合成 BSON 复核并发来源冲突，确认早发的兄弟事实和前序修订须受保护。当前发布在临床应用前按固定计划预留原件位置，冲突时只清理本次预留，租约更替按原发布续办。`/root` 负责 Document 在实际 Analysis 与 Report 之间传递固定原件证明，以及后续与 Report、Record 的组合接线验收。

`feature/report-document-consumption` 由 `/root/report` 实现 Report 消费和恢复，`/root/consumption_design_review` 负责非作者复核，协调者汇总验证与交付。依赖 !140 的固定准备和消费、Report 原件投影及 Profile 本人和生命周期公开端口。固定提交按已受理的工厂清单领取。Evaluate 复用同一原件提供专用模板和既有原生表格恢复，失败档案仅用于当次业务降级。组合根默认切换须等待 Record 接线及 Analysis 与 Report 跨来源观察去重经实际组合验证。

Document 固定材料消费两轮复审以 `f2cd574` 为修复对象，候选同步含 !139 的 `develop@fbc2633`。R1 允许失去前驱的未受理候选按原请求重新协调，真实发布预约及无变化受理继续重放原凭证。R2 按已核验的主材料、解释轮次、生命周期及临床依赖续办 Report 补充，复用他方已完成的同轮投影。协调者负责 Document 修复，`/root/consumption_regression` 负责回归，`/root/consumption_design_review` 负责非作者复核。Record 和 Report 端口消费边界由 `/root/consumption_handoff` 与协调者核对。最终全量日志、JUnit 和提交指纹见 `D:/996-work/shulan-test-artifacts/document-prepared-consumption-review-final.{log,xml,json}`，独立复核见同目录 `document-prepared-consumption-review-nonauthor.json`。

!138 合入 `develop@f2b0b5c` 后，Document 消费接口由 `/root` 负责，Report 及 Record 分别由 `/root/report_wiring` 和 `/root/record_wiring` 接收。Record 输入固定 Analysis 准备，Report 另附同一主材料对应的固定投影。Document 验证主体、生命周期和固定材料，拥有首次准入、现有来源的更正、发布版本及凭证重放。消费者保留当次准备和业务快照。两条消费者均以原固定输入重试 `DocumentBusyError`；固定材料、生命周期、撤回或临床依赖冲突沿用 `PublicationConflictError`。`unchanged` 可复用同轮发布，应用状态以持久回执为准。正式消费者组合验收在本批合入后进行。

除单页同来源外，合成组合已复现多页 Report 的来源 A 可发布原件 B 的观察，随后 Record 从独立来源 B 再发布同一观察。Report 出处对照必须经 Document 核对原件文档、摘要及固定 Analysis 指针。需要在 Profile 所属分支治理跨来源的有效出处：不同位置同值保留，任一贡献撤回后另一有效出处继续有效。接线与默认切换的最终验收覆盖两种顺序、并发、分次撤回及生命周期隔离。

`feature/report-current-document` 的 R1–R6 整改集中在 Report 原件出处对照。`/root/evidence` 负责独立位置及原文回退，`/root/projection` 负责影像根路径和镜像对照，协调者负责在途恢复、专项组合与交付，并以 Document 接收方确认 v3 入口、固定指针及原件出处字段。Document 当前执行 v2，v3 为显式供应方入口。后续启用前须暂停 v2 新受理并核对每份既有命令、回执和可读快照。切换与跨来源事实合并继续由后续 Document 和 Profile 分支负责。整改候选的全量日志、JUnit 与提交指纹见 `D:/996-work/shulan-test-artifacts/report-current-document-review-final.{log,xml,json}`，独立复核见同目录 `report-current-document-review-nonauthor.json`。

Document 报告投影复审整改基于 `71723f0`。R1 把各文档已接受轮次和无变化回执写入同一来源账本。R2 以固定解释轮次区分恢复早期临床内容的较新贡献。R3 支持指定活动贡献后扩展连续页组。R4 在 Document 转换中保留明确的定量观察。R5 按固定原件出处及所属文档的临床摘要筛选显式退休项。R6 将共同组合夹具迁至 `tests/unit/_support`。本轮由 `/root` 负责命令、定向组合和交付，`/root/current_pipeline_review` 负责账本，`/root/doc_managed_fixes` 负责贡献身份及退休选择，`/root/doc_audit_review` 负责定量转换和测试支持。最终合入候选的日志、JUnit 和指纹见 `D:/996-work/shulan-test-artifacts/document-report-projection-review-final.{log,xml,json}`，非作者复核见 `document-report-projection-review-nonauthor.json`。

Report 原件投影经 !137 合入 `develop@399efb8`。Document 接线分支 `feature/document-report-projection` 使用已合入的 Report v16 端口，固定实际输入、投影、执行回执和逐文档临床依赖，并通过当前来源计划发布可追溯观察。负责人 `/root` 负责 Document 端口装配、命令及交付，`/root/doc_provider` 负责固定投影，`/root/doc_managed_fixes` 负责贡献证明及替换，`/root/doc_audit_review` 负责实际组合回归。依赖与质量约定见下方 !137 交接。完整证据保存为 `D:/996-work/shulan-test-artifacts/document-report-projection-final.{log,xml,json}`，独立复核见 `document-report-projection-nonauthor.json`。

消费者接线及默认切换前须完成同一物理观察的跨提供方对齐。显式合成组合复现同一原件的 `ALT 12 U/L` 分别进入 Analysis 直属贡献和 Report 补充贡献，产生两条活动画像事实。Report 端需提供经核验的原件位置和固定 Analysis 观察指针对照，Document 与 Profile 接线按固定出处只应用一次，并验证不同原件位置的同值检查仍分别保留。未取得共同出处的记录禁止按名称或数值自动合并。

Report [!137](https://gitee.com/evimed/shulan/pulls/137) 合入前复审整改基于 `9e9a3f9`。R1 修正检验根镜像的日期比较，R2 分字段识别影像汇总并验证独立原件出处，R3 依据同一文档的原件位置去重并保留标本和方法，R4 迁移共享合成归档与 PDF 辅助代码，R5 核对项目、结果和字段角色。原生表格优先于同一出处的 OCR 错列，建议和参考区内容不能作为患者结果。 `/root/report_identity_review` 负责 R1、R2，`/root/report_proof_review` 负责 R4，`/root/report_review_tests` 负责专项回归，`/root` 负责原件核验、出处去重和完整交付，`/root/report_contract` 独立复核。最终候选的日志、JUnit 与指纹见 `D:/996-work/shulan-test-artifacts/report-current-source-review-final.{log,xml,json}`，非作者复核见 `report-current-source-review-nonauthor.json`。

直接依赖为 [!133](https://gitee.com/evimed/shulan/pulls/133)、[!134](https://gitee.com/evimed/shulan/pulls/134) 的 Analysis 固定轮次与观察时间，以及 [!135](https://gitee.com/evimed/shulan/pulls/135) 的 Document 固定归档。共同基线含 [!132](https://gitee.com/evimed/shulan/pulls/132)、[!136](https://gitee.com/evimed/shulan/pulls/136) 的 Profile 应用及退休恢复。Report 交接的输入仍为有序固定材料与原件字节，Document 的固定回执须一同绑定实际 v16 选择、输入适配修订 `report-understanding-current.v2`、固定材料引用、文档及临床摘要。按固定投影保存原件出处，正式临床贡献要求 `complete` 并维持原件证据准入。Document 下一阶段负责人 `/root/health_next` 已核对当前实现并确认接收。后续组合须验证按 `documentId → clinicalContentDigest` 保留相同依赖的贡献身份、同值独立原件位置不折叠、无可追溯出处的投影资料不进入正式贡献，以及回执恢复。组合发布及默认装配属于后续分支。

Profile 显式退休与在途恢复已通过 !136 合入 `develop@e303ce8`。Report 基于该提交，在 `feature/report-current-source` 提供当前原件事实投影。`/root` 负责实现、交接和全量回归，`/root/report_projection_tests` 负责合成归档专项，`/root/report_contract` 独立复核。最终候选证据见 `D:/996-work/shulan-test-artifacts/report-current-source-final.{log,xml,json}`，复核见 `report-current-source-nonauthor.json`。

Report 复用真实 v16 原件算法，按固定临床指针保留子报告类型、日期及影像章节。Report 合入后，Document 固定实际投影、执行回执和逐文档临床摘要；Report/Record 消费接线和默认切换依赖 Document 的固定读取与发布。Health Analysis 实际材料读取须绑定确认回执的主体及生命周期，并接入当前 Document 和 Profile 公开端口。

Profile 显式退休复审整改以 `fix/profile-document-retirement@23fdb4e` 为起点，比较基线为 `develop@72a93f4`（!135）。`/root/rejection_plan` 负责 R1，修复人工拒绝后的候选资格、物化与回选。`/root/plan_recovery` 负责 R2，修复当前 Document 计划已写入、应用尚未完成的恢复与显式重驱。`/root/retirement_final_review` 独立复核，`/root` 负责文档、Git 与全量验收。实际 Document 和 Profile 工厂使用合成资料与 BSON 存储验收。最终候选证据见 `D:/996-work/shulan-test-artifacts/profile-document-retirement-review-final.{log,xml,json}`，对应非作者复核记录为 `profile-document-retirement-review-nonauthor.json`。

Profile R4 合入候选已同步含 !135 的 `develop@72a93f4`。本批以 `ede9e75` 为修复基线，`/root/r4_composition` 负责使用已合入的 Document 工厂复验显式退休、剩余依据回选、原请求重放和后续更正，`/root/r4_final_review` 独立复核，`/root` 负责 Git 与全量验收。两模块组合使用合成资料及存储，验收证据见 `D:/996-work/shulan-test-artifacts/profile-document-retirement-merged-final.{log,xml,json}`。下一阶段由 Report 先提供当前原件事实投影，再接 Document 的固定贡献发布。

Profile 显式退休整改（Document 4596c7d 复审 R4）由 `/root/profile_r4_plan` 在 `fix/profile-document-retirement` 实施，`/root` 负责验证交付及与 Document 交接。Profile 从现有当前库存提取经过验证的基础贡献退休证明，将其固定到应用计划。画像 CAS 核对前序活动库存和原贡献身份，退休后重新选择已有有效依据。Profile 独立候选的原证据见 `D:/996-work/shulan-test-artifacts/profile-document-retirement-final.{log,xml,json}`。

Document 复审整改以 `feature/document-basic-info@4596c7d` 为起点。R1 同文档解释轮次由 `/root/document_r1` 修复，R2 临床摘要比较由 `/root/document_r2` 修复，R3 注入恢复器路由由 `/root` 修复。R4 在独立 `fix/profile-document-retirement` 交付，由 `/root/profile_r4_plan` 负责。Document 的 R1–R3 已通过 !135 合入 `develop@72a93f4`。R4 在 Profile 同步该基线后组合验证。阶段验证见 `D:/996-work/shulan-test-artifacts/document-retirement-review-local.{log,xml,json}`。

Document 日期联调复验（2026-11-13）：Analysis 的观察时间修复及复审整改已通过 !134 合入 `develop@a3ccf12`。本批在 `feature/document-basic-info@60b9ea9` 同步该基线，通过已合入的公开端口检验固定材料、来源投递和 Profile 当前选择。第 3 项由 Analysis 提供方修复，Document 负责闭环回归。回归由 `/root/document_time_integration` 实施，`/root/document_merge_review` 独立复核，`/root` 负责基线、文档和最终验证。合并候选证据使用 `D:/996-work/shulan-test-artifacts/document-time-develop-final.{log,xml,json}`。

以下为此前各阶段交接记录。

Document 本轮交接（2026-11-13）：Analysis `a151743` 已通过 !133 合入 `develop@d0e1f9c`。`/root/doc_provider` 负责使用已合入的公开 prepare/read 端口贯通 Document 固定材料、持久发布与 Profile 接收，`/root/doc_review` 独立复核，`/root` 负责文档、Git 与全量验证。对应证据保存为 `document-analysis-profile-final.{log,xml,json}`。Report 当前原件投影与默认切换继续按阶段推进。

实际端口联调补充两项契约边界。Document 固定归档接受报告汇总的完整候选集合，逐条校验证据，未决字段与有效字段、临床贡献分别处理。基础信息观察时间需输出日历日期或受支持的 ISO 时间。Analysis 所属 `fix/analysis-basic-info-time` 基于 `develop@d0e1f9c` 独立修复紧凑日期输出，由 `/root/report_next` 实施、`/root/doc_review` 复核，等待用户合入后再对同一 Document 候选复验日期闭环。

Document 两轮审核整改：第 1、2、4、5、6 项由本批修复，第 7 项同步交接状态。第 3 项由 Analysis 独立提交 `7ba5e7c` 处理，合入前需要同步并复验。`/root` 负责来源发布 CAS 和首次准入，`/root/doc_managed_fixes` 负责并发回执和单次读取复用，`/root/doc_recovery_fix` 负责共享账本恢复分流，`/root/doc_audit_review` 负责独立复核。合入上游前的本批证据使用 `document-basic-info-review-fixes-final.{log,xml,json}`。

以下为 Document 上一候选 `7f703d9` 的交付记录，内含当时尚未合入的依赖状态：

本轮 Document 接线（2026-11-13）：实现负责人 `/root/document_provider` 仅修改所属模块和测试；协调者 `/root` 维护共享文档及串行 Git，独立复核由非作者承担。纯读取组件先构造，Profile 通过公开 `ProfileCurrentDocumentSourceResolver` 注册实际 reader；当前投递直达 `ProfileSourceInbox.receive`，显式转换主体范围，随后使用同一账本构造投递、恢复和命令服务，不在构造后补接依赖。本批 Analysis 输入仍经合成公开准备 Port，真实调用等待 `refactor/analysis` 由用户合入 develop，默认装配尚未切换。最终全量候选日志和提交指纹记录于 `D:/996-work/shulan-test-artifacts/document-profile-integration-final.{log,xml,json}`，不沿用 f4e9708 的旧验收。

本轮故障验证补充：实际接收适配器不再要求默认旧路径的 cutover；撤回 reader 必须使用原持久来源计划验证其修订及生命周期，不能仅以 epoch 为正整数作为有效证明。回执丢失、重启、乱序撤回与主体归档恢复纳入本批回归；历史已通过记录不代替本候选最终验收。

非作者质量准入复核：当前归档原实现会把缺失 processing 当作 complete，已列为本批阻断；当前质量取值及归档读取校验须从原解析结果推导，缺失或非法状态不能生成完整临床贡献，基础字段仍按独立证据规则处理。两份派生快照质量即使相等，也须与原解析质量一致。修复前全量已中止并保存在 `document-profile-integration-superseded-before-quality-fix.*`；不作为最终验收，最终使用前述 final 日志、JUnit、指纹及非作者复核。

Report 后续依赖：现用公开 `modules.report.document_source.extract_intrinsic_report_source` 仍提供原件临床投影，不能因 `legacy_*` 名称删除。当前公共投影端口由 Report 所属分支交付，Document 随后固定回执与临床依赖并提供追加/替换发布；本批只确认职责和来源语义，不添加悬空 DTO 或宣称真实 Report 接通，不把生成报告正文或简单原文共现当成原件事实。

Analysis 观察时间两轮审核整改（2026-11-13）：从 `fix/analysis-basic-info-time@7ba5e7c` 修复 R1 无效时区偏移进位、R2 紧凑时间精度变化、R3 同轮在途结果跳过归档前校验。三项均属于本次合入前缺陷。已固定的轮次沿用原引用和摘要；未固定的结果在首次固定前应用同一校验。观察时间校验由 `/root/observed_time` 实施，恢复路径由 `/root/time_recovery` 实施，`/root/time_review` 独立复核，`/root` 负责集成、文档和 Git。候选验证及提交指纹记录于 `D:/996-work/shulan-test-artifacts/analysis-basic-info-time-review-fixes-final.{log,xml,json}`。Document 在该修复合入后同步 develop，复验实际 Analysis、Document、Profile 的归档和应用。

Analysis 观察时间契约修复（2026-11-13）：Document 使用 !133 已合入的公开端口联调时，发现紧凑日历日期可以进入 Analysis 固定轮次，Document 和 Profile 的观察时间校验会拒绝整份结果。`fix/analysis-basic-info-time` 基于 `develop@d0e1f9c` 统一基础信息字段输出日历日期或 ISO 时间，保留原值、证据与固定摘要。`/root/report_next` 实施，`/root/doc_review` 复核，`/root` 负责 Git 与全量验证。Document 待用户合入该修复后同步基线，再用实际三模块链路复验紧凑日期入档和应用。验收记录使用 `analysis-basic-info-time-final.{log,xml,json}`。

本次 Analysis 合入前审核整改（2026-11-13）：以主目录 `D:/996-work/shulan` 的 `refactor/analysis@03f3d95`、`develop@054a2fc` 为起点，受理临时两轮审核记录的 R1–R4。四项均属于本批必须修复的代码缺陷，不延后到 Document 接线或默认切换。临时审核记录保持原样、不纳入提交；本条记录作者回应及新候选验收，不修改原审核结论。

| 审核项 | 本次整改与验收要求 |
|---|---|
| R1：格式修正丢失子报告临床项目 | 临床择优统计完整子树的 accepted_count，保留所选答复及其未解格式问题；不对临床事实取并集。 |
| R2：舍弃临床候选时丢基础冲突 | 每份已验证答复递归投影全部基础元数据，独立累计有效单值、候选和主体判断；检查点与最终结果保留同一证据集。 |
| R3：空候选容器根主体歧义被过滤 | 原输入完整树参与基础合并，与临床 sections、标题日期证明及去镜像规则分开；空值与空候选的 ambiguous 仍有效。 |
| R4：真实归档拒绝当前留痕名 | 当前解释器采用现有受控的五位编号、repair 及固定核查子号；真实 MongoDocumentTraceStore 加合成 BSON 存储验证准备、恢复和固定纯读，不修改归档适配器契约。 |

实际任务分工：`/root/analysis_repair_review` 负责当前解释器、共享基础投影和 R1/R2/R4 核心回归；`/root/analysis_identity_review` 负责 R3 合并及容器回归；`/root/analysis_trace_review` 负责真实归档组合和旧断言名称同步；三方已确认统一投影/命名规则；R3 初始接线冻结后，累计标记的末尾传播与最终节点清理由协调者串行交接给核心作者，原 R3 作者停止写入，没有并发争用。主协调者 `/root` 维护文档、串行 Git、完整验证及提交指纹；最终代码由未参与实现的智能体复核。仅修改 Analysis 所属范围，不复制或接入尚未合入的 Document 实现。

本轮组合复核补充：新增完整树投影需区分同一证据的汇总视图与独立资料，不能把同一子报告及其派生根重复算作两个无锚点主体。该新增回归由主协调者与独立非作者共同复现，列为最终验收前阻断；修复与回归须保留原跨片段无锚点门禁及显式 ambiguous，不以放宽主体规则规避。相应中间候选证据保留，不替代最终新指纹。

本轮最终证据使用 `D:/996-work/shulan-test-artifacts/analysis-r1-r4-review-final.{log,xml,json}` 及对应非作者复核。旧 `03f3d95` 的 12731 passed / 275 skipped 只证明整改前内容，不能替代新候选。正式 PR 的链接、目标 develop、当前源 SHA、I-048 / !132 关联和维护者批准由用户核验，本地代码整改不表示平台已批准。仅本地中文提交，不推送、创建 PR 或自行合入 develop；未执行真实数据库、模型及部署验收。

Analysis 批次历史推进（2026-11-13）：用户已将 `feature/profile-document-basic-info@b8fea47` 通过 !132 合入 `develop@054a2fc`。本 Analysis 分支引入该真实 Profile 基线，继续交付当前抽取与公开准备、固定轮次读取；该公共端口已通过 !133 合入。Document 在所属分支同步 Profile 并验证真实接收，上游随后已通过 !133 合入，本阶段转入实际调用。默认装配和 Report 当前贡献能力依照原计划后续接线，未提前切换。

本轮 Report 依赖核查：`modules.report.document_source.extract_intrinsic_report_source` 仍承担原件临床事实提取；其现用 `legacy_*` 名称不表示可删除的旧数据兼容。默认切换前需由 Report 所属分支交付当前公共来源投影端口，再由 Document 固定回执与临床依赖并发布。两方当前只确认职责与来源语义，尚未冻结新 DTO 或接通真实 Report；不以生成报告正文或简单原文共现替代事实提取。

以下保留前批 Profile 整改与 Analysis 本域准备记录；其中“Profile 尚未合入”等状态已由本段更新，不作为当前阻塞。主智能体仅本地提交，不推送、创建 PR 或自行合入 develop。

历史整改决策（2026-11-13，以下描述 Profile 审核时基线）：用户确认没有生产数据，不考虑旧数据兼容；本轮转为单一当前契约和处理流程，取消历史双版本解码、旧执行器及失败回退。F1–F3 已核查为本次合入前必须整改，执行与验收见 [单流程收敛计划](docs/architecture/basic-info-single-pipeline-plan.md)。本分支基线仍为 `feature/profile-document-basic-info@19d1f48`，尚未合入 develop；原完整测试仅对应该旧候选，不能替代本轮验收。

本轮具名分工：Profile 实现 `/root/document_plan_review`（主目录）；Analysis 实现 `/root/profile_plan_review`（refactor/analysis）；Document 实现 `/root/document_provider`（feature/document-basic-info）；主协调者 `/root` 负责交接、文档与串行 Git。三方已在本轮会话确认只修改所属模块；Analysis/Document 已确认不带版本选择的公开准备与固定轮次读取，Profile/Document 已确认当前材料结构和真实修订证据边界。后续非作者复核绑定整改后的新提交，不沿用此前审核批准。

合并顺序调整为 Profile 共同治理与当前端口 → Analysis 当前输出公共端口 → Document 真实消费 → 各消费者与组合根切换默认 → 各模块删除过渡实现并组合验收。当前 Document 仍硬性要求原 Analysis 修订，因此默认切换必须等待依赖齐备；过渡现用接线只保障串行合并，不作为历史数据兼容功能。本分支不跨域改造消费者，不推送、创建 PR 或自行合入 develop。

本轮整改作者验收：1427 项 Profile 域及实际 HTTP 组合通过；当前接收经 `ProfileCurrentDocumentSourceResolver(reader).registrations()` 注入既有 Profile 工厂，工厂签名不变。现用默认原入口没有新增旧格式 wire，旧二执行器已删除，共同 planner 按材料能力处理。新 current 无迁移门禁，错误材料先拒绝再绑定，后续合法 publication 可更新，撤回原 epoch 可先于发布到达。最终全项目证据将保存为 `profile-single-pipeline-review-final.*`，非作者复核和最终提交指纹对应以该记录为准。Document 本域准备已独立完整验证并本地提交 f4e9708（12553 passed / 275 skipped），不代表本分支已接其代码或默认链已切换。

平台材料仍由用户处理：PR 链接、最新源 SHA、目标 develop、I-048 与 !131 关联、正式非作者批准及最终合并。当前缺少平台材料不被写成已核验通过，也不阻止在本地完成代码整改与可审查证据。以下保留 2026-11-12 的历史交付记录；其中旧版本兼容决策已被本段取代。

当前推进：用户已将 `feature/profile-basic-info@2c618ef` 通过 !131 合入 `develop@8d8fa7a`。继续接线时确认正式 Document 来源接收仍只有 V1，不能将基础字段直接接入既有发布链，因此从最新 develop 新建 `feature/profile-document-basic-info`，补齐 Profile 所属 V2 正式接收、固定生命周期与独立基础/临床应用；[实现及验证证据](wiki/profile-document-basic-info.md)。原基础信息提供方的整改与验收记录保留如下，不再作为未合并关口。

本轮非作者复核补充：旧 epoch 的 staged alias 在 publication 或 cursor 持久中断后必须终结失败，释放有界恢复扫描，保留原指纹及生命周期；只处理稳定生命周期冲突，其他冲突语义保持。另修复临床事实在主体校验后、实际写入前归档恢复的窗口：V2 物化事实固定原生命周期，统一当前读取隔离跨 epoch 残留，不依赖事后撤回补偿。完整复验前候选的 12568 passed / 275 skipped 记录保存在 `profile-document-basic-info-before-lifecycle-review.*`，已标为被替代，不能作为最终提交验收。

当前依赖关口：该 Profile V2 提供方经本地验证及非作者复核后，先交用户合入 develop；Document 同步后才能实际接线，随后 Analysis、HealthAnalysis 按原顺序完成组合验证。Document 和 Analysis 的本域准备使用 Port/合成替身，不宣称跨分支生产接通。HealthAnalysis 所需报告材料纯读提供方也须从 develop 独立交付，不能合并消费者根分支或复制实现代替。主智能体不推送、不创建 PR、不自行合入 develop。

历史交付（已由用户合入）：2026-11-12，共享 Profile 提供方在独立 `feature/profile-basic-info` 分支实现，基于 `develop@4418996`，合并审核 R1–R5 的代码整改与本地非作者复核已完成；最终完整离线验证及提交对应关系见下列证据，等待用户重新审核；本地检查不代替平台 PR 审核；未推送、未自动合并、未部署。此编号与 Analysis、HealthAnalysis、Document 协作登记指向同一任务。

本域交付：low/medium/very_high 逐字段来源与当前选择、历史 unknown 保护、完整报告库存与撤回、公共 V2 来源读取、稳定确认命令及 pending 恢复、共享治理水位、生命周期隔离、姓名事实与年龄依据。实际实现和端口签名见 [Profile 提供方说明](wiki/profile-basic-info.md)，外部字段见 [API.md](docs/api/API.md#基础信息当前值)。不引入登录或授权服务。

依赖关口：共享Profile公共事实/治理层已由用户合入develop@8d8fa7a；Document V2、Analysis、HealthAnalysis各自在本域接入公共契约，不复制提供方实现，不合并其它模块根分支充当运行时依赖。正式Document V2 receiver由feature/profile-document-basic-info负责，Document neutral V2固定归档/来源协议由Document根负责，提供方实际接线合入后再统一切换报告当前入口并删除过渡默认实现；当前仅本域Port准备。HealthAnalysis Runtime补偿及Record/Chat V2人口学适配由各模块负责，未知报告观察时间不得伪造时间塞进旧强制datetime的上下文DTO。

本次审核回应：

| 项目 | 本次处理 |
| --- | --- |
| R1：既有建档写入被隐藏 | 由 Profile 桥接旧 writer 和正式来源应用，保存真实来源候选，协调创建、撤回及恢复；旧 Record 同档冲突保留当前值，不将后台重试当成新确认。 |
| R2：人工事实被跨生命周期重迁入 | 固定跨生命周期的原操作索引，确认/更正同 CAS 写入事实标签；恢复沿用首次确认时间，原范围可证明的旧中断操作保守补齐，缺证不重绑。 |
| R3：恢复的高档贡献未成为当前值 | 完整活动集更新后按等级及稳定顺序重新比较；合格替换同档预选沿用原入档顺序，已删除贡献不参与替换资格；保留旧版本、unknown、API 最高档及 Document 永久退休保护。 |
| R4：资源冲突错误返回 500 | 基础信息冲突继承既有应用冲突类别，原 Web 映射返回 409；旧导入身份和其余错误类别保持。 |
| R5：公开字段及版本说明过时 | 当前 API 更新至 0.23.1，补齐人口学空观察时间、兼容 schema 1/启用后至少 2、两种 includePending 及相关示例；不回写历史快照。 |

最终完整验证：在主工作目录 `D:/996-work/shulan`、`feature/profile-basic-info` 上执行 `C:/Users/MSR/.conda/envs/shulan/python.exe -m pytest -q --junitxml=D:/996-work/shulan-test-artifacts/profile-basic-info-review-fixes-final.xml`。最终实际通过/跳过数量、用时和完成时间以同名日志、JUnit 和 JSON 为准；验收只采用最终候选退出 0、无失败且受测文件 SHA256 与提交一致的完整记录。当前 pytest 默认收集完整 `tests/unit/`，未使用用例筛选。原有跳过原因包括显式历史模板目录、Windows 符号链接权限和缺少本地 callback 样本，本次未新增或更改跳过条件。

专项与静态检查：基础信息/BSON/HTTP 组合 **98 项通过**，包含既有基础信息 31 项、新来源 28 项、治理及 BSON 17 项、报告恢复 14 项和 HTTP 8 项；相关旧来源路径另有 97 项通过。16 份 Python Ruff/check 与 format 检查、4 份 Markdown 格式/本地链接、API 133 个标准 JSON 示例通过。专项只用于缺陷复现与边界定位，不替代上述最终完整回归。

本地非作者复核：`document_provider` 独立复核 R1/R4/R5，28 来源专项及真实 BSON 时区/毫秒往返、18 个新旧 HTTP 契约通过；`document_plan_review` 独立复核 R2，68 个含旧契约及 BSON 的组合回归通过，核对固定字段水位、原生命周期、确认时间和旧命令身份。R3 经 `profile_plan_review` 独立复核，14 个恢复专项通过，复核同档稳定预选、删除资格过滤、unknown/最高档保护、单 CAS 及固定快照；`document_plan_review` 也独立重验其原同源逆序复现场景通过。以上仅是本地代码复核，不代表平台 PR 已获批准。

验证证据位于 `D:/996-work/shulan-test-artifacts/profile-basic-info-review-fixes-final.log`、同名 `.xml` 和 `.json`；JSON 保存分支/基线、20 个候选文件 SHA256、命令、开始/结束时间、结果及最终提交对应关系。R3 补审前的完整候选曾为 12498 passed / 275 skipped，保存在 `profile-basic-info-review-fixes-before-r3-stable-order.*`，已明确标为被替代候选，不作为最终版本验收；BSON 修正前中止的记录保存在 `profile-basic-info-review-fixes-interrupted-before-bson.*`，不计验收。整改前 21cf1d2 的 12433 passed / 275 skipped 同样不能代替本次修复后的完整验证。

临时人工审核记录保持原样、不纳入提交。平台 PR 链接、源/目标关联与维护者的合并审核由用户处理，本地实现完成不等同于获准合并；未推送或合并 develop。未读取私有配置或真实健康资料，未进行真实 MongoDB/模型/部署验收。

## Analysis当前抽取与提供方交接（2026-11-13）

- 用户决策：没有生产数据；当前报告抽取不做历史缓存、旧执行器、旧格式或旧任务版本兼容，不给调用方v12/v13选择。此前旧版本分派要求被替代。
- 本次整改起点为主工作目录 `D:/996-work/shulan`、`refactor/analysis@03f3d95`，已引入 `develop@054a2fc`（!132）。Analysis 负责人 `/root/profile_plan_review` 只维护所属代码、测试及两份本域文档；主协调者 `/root` 维护共享文档并串行完成本地 Git 操作，不代改其他模块实现。
- 当前交付：CurrentHealthReportInterpreter、逐字段low/medium与证据、主体校验和两阶段预算；prepare_document不接revision/publish，固定不发布；read_prepared_round公开scope、固定绑定、完整parsed_document、interpretation与实际selection，纯读归档。
- 过渡边界：现用默认原算法、原prompt和真实身份暂留；不能给current贴旧标签或失败后回退。Document当前消费等前置适配用户合入后，Analysis与组合根在阶段5切默认并删除过渡业务实现/接线。目前不能宣称全项目已单算法或已默认接通。
- 实际交接：2026-11-13，Analysis负责人/root/profile_plan_review与Document负责人/root/document_provider已确认prepare/read签名及PreparedDocumentRound七字段。Document在其工作树按Port/Fake实现，不读取私有reuse或导入未合代码；真实组合仍待各自用户合入，统一任务I-048。
- 本轮同步时先有 890 项相关回归通过；随后非作者复核发现跨片段和补核查会丢失合法冲突候选，列为本批阻断，修复和回归纳入本次交付。字段合并必须汇集有效值和有证据的候选，互斥时输出空值与待核实状态，继续保留临床结果；无效候选不能制造冲突。修复前全量已中止并保存为 `analysis-profile-merged-superseded-before-conflict-fix.*`，不作验收。最终完整回归及提交指纹以 `D:/996-work/shulan-test-artifacts/analysis-profile-merged-final.{log,xml,json}` 为准；本地非作者复核绑定同一候选。未进行真实数据库、模型或部署验收。

## 双来源基础信息（2026-11-12）

### 健康分析前期分支记录

状态：四线首批本地候选已提交，分别完成完整离线回归和非作者复核；尚未跨模块实际接线。现到必要用户合并关口：先将Profile提供方合入develop，再继续Document实际接线及后续业务/消费者组合。develop核验为`4418996`，Profile/Document根从该基线创建，Analysis/Health Analysis使用其既有业务分支的子分支。本轮替代I-046/I-047的任务Git例外，用户逐模块合入develop后才接入跨模块实际提供方；智能体不自行合入develop、不推送、不建PR。

共享规则：API明确确认固定`very_high`，报告AI逐字段判断`medium/low`；缺失不清空，同档自动冲突保留，历史未知来源保护，来源撤回重新选择。固定主体、生命周期及字段版本防止迟到补偿覆盖后续更正。确认持久受理后有界应用，后台只补基础信息；不新增授权、关系证明、重解析或上传入口。年龄及历史快照规则以本次需求和计划为准。

| 线 | 位置与状态 | 下一门槛 |
|---|---|---|
| Profile | `codex/profile-basic-info@ee77191`，共享提供方；12433 passed / 275 skipped、31专项，Ruff及非作者复核通过 | 交用户先合入develop，再接真实共享依赖 |
| Document | `codex/document-basic-info@1ac54f1`，显式V2已提交；12440 passed / 275 skipped，38专项，非作者复核通过 | 等待Profile合入develop，再验证真实接线 |
| Analysis | `codex/analysis-basic-info@b5e5109`，已快进回`refactor/analysis`；12472 passed / 275 skipped、113专项，默认仍v12 | 等待Profile及Document后默认发布 |
| Health Analysis | `codex/health-analysis-basic-info`，`feature/health-analysis`子分支；已以`fe197fd`快进回业务根；12698 passed / 275 skipped、276专项、113 Node及10合成HTTP，真实确认仍待接线 | 等待Profile后默认确认及组合验收 |

I-047中调用方授权、用户群组关系证明和默认授权503已被用户明确否决，本轮移除。发送人映射仍是资料归属限制，不能将未知发送人当成本人；不是新增鉴权需求。缺少持久确认提供方导致的503仅表示必要写入未可靠受理。

### 提供方合入develop后的消费者接线（待实施）

以下来自对当前源码的只读核对，是真实接线清单，不表示已完成。各模块从届时最新develop分别创建根分支；不在消费者中复制提供方实现。

- [ ] Record：`adapters/record/profile_context.py`、`modules/record/profile_context.py`及`profile_memory.py`增加V2基础信息输入与独立renderer版本。临床分页保留V1，当前姓名/性别/年龄/DOB选择只使用Profile统一结果，固定asOf、ageBasis及依据。旧V1人口学DTO强制datetime，不能将未知观察时间塞成虚假时点，也不能因此丢失全部临床上下文。已持久化healthProfileContext及创建恢复保持原值，context版本不借用来源发布profileConsumerRevision。
- [ ] Chat：对应Profile context/memory适配增加V2基础信息；新turn读取当前快照，历史消息及已保存上下文不回写。自体人口学事实不再按最新时间各自选值，亲属与临床资料保留范围和预算。
- [ ] Report：`adapters/report/versioned_profile.py`、兼容`profile_snapshot.py`与`modules/report/ports.py`增加显式V2私有快照，携带当前基础字段、档位、依据、ageBasis与统一asOf。`patient_context.py`和`pipeline.py`接入当次固定选择，保留显式请求优先与报告时点；旧session/文件年龄不无标注地作为实时年龄。既有V1输入、finalReport、callback和临床/证据checkpoint保持原值，生日或补偿不重算历史。
- [ ] 展示：`tests/api/pages/health-profile-view.js`以overview/basicInfo.name表示真实姓名，subject.displayName保留主体标签语义；年龄区分报告时点、确认时点、生日实时计算，显示时点与档位。显式确认姓名可更新本人展示，自动报告不改人工标签。事实治理页可保留原JSON和版本操作；同步API字段与示例。
- [ ] 组合根：依赖合入后在独立应用分支完成`bootstrap/factory.py`、Report工厂及健康分析真实确认端口注入；组合覆盖V2读取、未知时间、最高档保护、同档冲突、来源撤回、跨生日和旧固定快照恢复。

后续建议根分支为`codex/record-basic-info-consumer`、`codex/chat-basic-info-consumer`、`codex/report-basic-info-consumer`；展示归Profile模块根或其子分支，最终应用组合根单独交付。首个必要动作仍是用户将完成验证的Profile提供方合入develop，随后再接Document、Analysis及相关消费者。

## 健康分析依赖与交接（2026-11-11）

### I-047 健康分析B0前置交接与公共纯读依赖

以下为旧三入参版本历史记录。原授权前置、默认授权503及跨模块汇总到业务分支的Git约定已由I-048取代，不再是本轮实施或发布要求。

状态：2026-11-12代码、模拟客户端和离线组合验收已完成；真实在线测试目前无法提供，真实依赖及部署未验收。当前任务分支为 `feature/health-analysis`，基于 `develop@4418996` 的本任务需求与计划提交继续开发；2026-11-12重新fetch核对develop仍为该基线。Profile/Document由 `sources_design` 在独立 `feature/health-analysis-profile` 交付，初始代码提交为 `bde8c22`。需求与门槛见[健康分析需求](docs/product/health-analysis-requirements.md)和[计划第10节](docs/architecture/health-analysis-implementation-plan.md#10-实施批次与交接)。

历史Git约定（已由I-048本次计划取代）：从develop建立任务工作分支后，可在该工作分支基础上自主拆分、提交与本地合并，所有成果最终汇总到 `feature/health-analysis`，由用户检查并自行创建合入develop的PR。主智能体不推送、不创建PR、不合入develop；此前未执行这些操作。该指示仅覆盖本任务的模块分支及集成顺序，职责交接、非作者审核、配置边界及全量离线测试继续按规范执行，不引用I-046的历史例外代替本次授权。

| 所属方 | 所需支持与真实缺口 | 当前交接状态及验收条件 |
|---|---|---|
| 调用入口 | 本轮不引入授权提供方或用户群组关系证明。 | 原额外授权门禁已撤销；保留参数、资料归属及固定本人主体检查。 |
| 群聊提供方/调用方 | 缺 `userId` 到 OpenIM `sendID` 的权威映射，以及完整性/分页约定；现有工具仅保证单次响应。 | 尚未取得真实提供方约定。不猜发送人、不杜撰分页参数，不将离线合成映射标为真实接入。 |
| Profile / Document | 原公共归档底座需补纯读聚合及固定轮次、材料和结果引用。 | 主智能体已交接职责，sources_design 在独立 `feature/health-analysis-profile` 实现出口、独立贡献与镜像身份、生命周期复核、空项目过滤及实际提供方组合；验收后本地合回任务分支。提供方工作区 `C:/Users/MSR/.codex/worktrees/health-analysis-profile/shulan`，不代改健康分析生成或旧业务生命周期。 |
| 健康分析 / 运行平台 / 存储维护方 | 缺代表性真实依赖的同步性能样本；容量、deadline、调用与排队预算、追溯容量及保留清理规则未冻结。 | 不以旧问卷45/360/600秒或假模型耗时定版，不声称 P95 已测。需合成报告/聊天/答案及允许使用的实际依赖条件，记录模型/提供方、调用数、排队、全耗时、样本口径与失败请求。 |
| 健康分析 | 冻结本期生活目标与发布政策，不把目录耗尽误称全部健康问题耗尽。 | questionnaire_design 已冻结24目标初版与版本，程序确定去重及模型语义覆盖、独立发布校验、有界补题和7/1/0题用例；不凭漏生成证明耗尽，后续新增细分须升级目录版本。 |

发送人映射未知及分页未明确保留为覆盖限制：已授权群聊中的未知发送人不作为患者直接陈述，只处理本次真实响应。旧版授权门禁已被本轮需求撤销；必要画像确认不能可靠受理时仍返回503。用户要求先离线交付后，代码使用明确开发期容量与期限保护，模拟授权与模型只存在测试脚本中；B0/B3/B5实际模型、知识、数据库及完整代理等待链性能仍待提供环境验收，不能用假依赖耗时作生产P95或容量结论。

Profile 提供方出口必须保留：本人纯读 scope、画像明确性别、原 result 的固定身份与质量、来源贡献的独立 extraction/revision/publication 身份及生命周期、镜像关联与覆盖限制。`result=None` 或最新归档缺档不能丢掉仍合法的旧贡献，不能将旧贡献标成最新解读；不同日期的独立记录不能按名称和值去重，撤回/退休资料不能回退复活。授权、程序错误与取消不能降为空来源。

主线已实现独立 `health_analysis.main`、多卷协议、三源采用快照、五步循证与条件匹配、生活目录覆盖、四项输出、共享调用/请求限流、统一期限与取消、Mongo majority追溯确认及新路由日志正文抑制。web_client 已同步 API 0.23.0、API Lab和仅本机的合成演示脚本；用户要求的浏览器基础操作已验证13题完整基础卷、真实未知答案再次分析得到14道新生活题、继续追加历史后无新卷。旧 Questionnaire/Record/Chat/Report 的业务生命周期未改。`VerifiedAccessScope` 仍只代表可信提供方的内部结果，不接受JSON自证授权。

本地组合工作区为 `C:/Users/MSR/.codex/worktrees/health-analysis-validation/shulan`；最终交付以 `feature/health-analysis` 的本地集成提交为准。主智能体负责应用/追溯与组合，questionnaire_design负责生成及循证，sources_design负责公共资料提供方和来源适配，web_client负责Web/日志/API及客户端；未参与实现的final_review负责独立复审。正式授权、追溯访问与保留清理责任、真实效果和部署继续待验收。

首轮组合验收（2026-11-12）：shulan解释器、`PYTHONUTF8=1 python -m pytest tests/unit -q` 完整组合 **12658 passed / 275 skipped，481.60秒**；46个变更Python文件通过Ruff check及format --check。独立提供方 `bde8c22` 的完整离线结果为12420 passed / 275 skipped，522.48秒。收尾另将跨测试模块复用的健康分析假依赖及Document/Profile组合辅助代码统一归入 `tests/unit/_support`，保留断言与用例语义；按实际最终集成内容重新完整复验，精确源提交、文件校验和结束记录随本次交付绑定，不用首轮结果替代新版本验证。

本地模块交付：健康分析实现提交为 `c24511d`，文档收尾为 `cce998b`；Profile/Document提供方最终提交为 `c757893`（父提交 `bde8c22`），专项396 passed，42个Python文件Ruff及format通过。共享组合辅助代码迁至 `tests/unit/_support/document_profile_composition`；独立复核确认286个旧测试函数/方法数量和AST不变，51个迁移定义经导入路径归一后AST等价，且共享辅助模块不反向导入测试模块。最终完整回归与本地集成提交另以closeout记录绑定。

客户端 `node --test tests/unit/api/api_lab.test.cjs` **113 passed**；最终组合 `python -m tests.api.scripts.health_analysis_demo --smoke` **10场景通过**，8个200结果逐uuid确认仓库外追溯文件。浏览器实际验证13题完整基础卷、真实未知答案追加后生成14道新生活题、继续追加后耗尽无卷。19份本次Markdown、138个标准JSON示例及27处新增本地链接通过检查；全仓format_markdown.py --check仍有11份未改动的存量格式问题，未回写历史或扩大修改范围。全量早期的测试同名收集冲突已通过新域测试包隔离修正，最终全量无错误。

组合验收材料位于上述组合worktree父目录：首轮 `input-manifest.json`、`pytest-final.xml`、`validation.json` 和 `demo-traces/`；收尾记录使用 `closeout-*` 文件绑定最终候选与提交，均仅含代码校验、离线结果和合成测试数据。三条README显式模板check-env均退出0，89/89字段、无结构差异，仅既有占位提醒；不运行读取私有配置的裸check-env。新域模型/知识适配不额外输出健康正文，已测新路由普通日志在非法JSON、大正文、参数、拒绝、上游、超时、追溯失败与断开路径只保留安全元数据；不将此结论扩大为第三方SDK、部署注入日志或供应商侧日志的全平台脱敏保证。

## 体检解读职责改造与依赖交接（2026-11-11）

### I-046 Analysis、存储、画像与应用编排分批交付

状态：2026-11-11 四个模块的本地实现、非作者复核和完整离线组合验收已完成，等待用户审核；真实依赖和部署验收保留。此前主智能体误将“完善文档，然后开始工作”理解为代码授权，已在用户指出后停止并保留现场；该历史记录不替代本次明确授权。

历史任务 Git 流程（本轮实施由I-048取代）：所有新模块分支必须从当前任务分支 refactor/health-report-interpretation 创建，完成后只合回当前任务分支；不得创建或合入从 develop 派生的模块分支。最终由用户统一审核并合入 develop，主智能体不推送或合入 develop。该例外只适用于本任务，不改写 CONTRIBUTING 的全局流程。

分支基线纠正：错误本地合并 34aa4f9 已从任务分支移除，已验收文档提交保留。Analysis 与 Profile 成果已在任务分支 d2688a2 上重现并逐文件核验，旧模块分支及冗余合并备份引用已按用户要求删除。Document 与应用编排工作区也已切到同一正确基线；后续仅在下表模块分支开发并合回当前任务分支。用户检查分支历史后已明确恢复开发。

用户明确 Analysis 核心仅为文件到文本、LLM 文本到结构化数据；存储、画像及消费者分别负责所属工作。需求与验收依据见 [需求](docs/product/health-report-interpretation-requirements.md) 和 [实施计划](docs/architecture/health-report-interpretation-implementation-plan.md)。不按调用方保留不同 Analysis 实现。下表记录模块归属和交接范围，完成状态以本节后续验证记录为准。

基线：主智能体 /root 统一记录当前任务分支的基线提交，再由各模块从该提交创建新分支；不得沿用 develop 基线的旧分支。依赖、接口和验收约束沿用下表分工，旧成果须在正确基线上重现并复核，不直接合并旧分支。

| 负责智能体 | 分支 | 职责与依赖 | 验收 |
|---|---|---|---|
| /root/analysis_review | feature/health-report-analysis | Analysis 当前抽取与最终摘要结算；不改存储和画像。 | 两份产物、完整抽取、摘要忠实与无额外消费者模型调用。 |
| /root/document_review | feature/health-report-documents | 固定轮次归档、通用资料纯读及 I-044；向画像和编排提供公共契约。 | 跨轮次固定读取、撤回保护、无副作用查询及并发别名竞争。 |
| /root/profile_review | feature/health-report-profile | 画像 standard/complete 展示、事实状态和 I-043；先基于自身 Port 与假提供方开发。 | 完整项目、文档缺档、隔离、分页、事实准入及列表/详情一致。 |
| /root | feature/health-report-application | 必要持久化后响应、就绪门禁与后台交付；消费 Document 和 Profile 公共契约并完成默认装配。 | 响应不等待发布、保存失败不成功、回执丢失和取消可恢复。 |

各工作区位于 `C:/Users/MSR/.codex/worktrees/` 下对应任务目录；四条模块分支均基于当前任务分支 d2688a2 创建，原成果已在正确基线上重现并保留。本轮仅本地集成到 refactor/health-report-interpretation，不推送或合入 develop。

本地验收记录（均使用 shulan Conda 解释器，PYTHONUTF8=1，完整执行 python -m pytest tests/unit -q，无用例过滤）：

| 模块 | 最终功能提交 | 完整离线结果 | 耗时 |
|---|---|---|---|
| Analysis | 9fa95de | 12308 passed / 275 skipped | 414.79 秒 |
| Document | c93da7e | 12321 passed / 275 skipped | 452.46 秒 |
| Profile | 79965d6 | 12326 passed / 275 skipped | 435.79 秒 |
| 应用编排与最终组合 | 8a61f8b | 12388 passed / 275 skipped | 518.21 秒 |

组合候选先通过43项实际提供方专项回归，再执行完整测试。覆盖默认装配的 standard/complete 列表与详情一致、同一文件的新固定解析、按附件B反查主来源A并隔离A自身指标、缺B索引、退休/撤回、空解析引用分页、响应前归档和就绪失败、回执丢失、取消恢复及并发别名。必要索引在HTTP监听和Worker扫描前初始化；未取得真实来源或事实依据时不伪造状态。响应采用已核验的最终投影，避免二次投影与固定归档不一致。

非作者复核：主智能体复核 Analysis 和 Profile；analysis_review 复核 Document 新身份/缺档/归属边界及应用编排、启动顺序和最终响应标记；profile_review 复核 Document 读取边界以及公开API与Wiki。发现的完整性、固定身份、缺档筛选和响应重复投影问题均已修正后复验。

最终组合34个Python文件通过Ruff check与format --check；9份本次变更Markdown通过格式、相对路径及131个标准JSON示例校验。全仓format_markdown.py --check仍报告11份基线已有格式问题，均在修改前的HEAD中存在，涉及历史API、历史/产品资料等，本任务未改动这些文件。组合日志、JUnit及带文件SHA256的validation.json保存在应用编排worktree父目录，元数据绑定功能提交8a61f8b；此后的验收记录提交仅改文档，不改变已测试代码。

I-043、I-044 的本地修复与离线验收已完成。真实DocMind/模型抽取质量、MongoDB/Redis多进程、Linux重启及部署仍未验收；未读取私有.env或真实用户资料。

## 多智能体重构依赖与交接（2026-11-09）

### I-030 分批实施的共同基线与依赖合入

状态：阶段 0—6 本地实现已完成并通过各候选完整离线验收；最终提交的组合重验按下方记录核对。真实质量和部署未验收，阶段 7 不在本次范围。用户已确认实施[多智能体目标架构](docs/architecture/modular-multi-agent-architecture.md)阶段 0—6。主智能体 /root 负责整体协调、共享装配、文档和 PR 顺序；模块分别使用独立分支/worktree。refactor/agent 作为本任务集成分支，原始基线 develop@da08e5f，设计与协调登记已提交；最终结论以本节最后组合门禁及范围限制为准。

本任务 Git 例外由用户于 2026-11-09 明确授权：从 refactor/agent 新建模块子分支，各自完成任务后合回 refactor/agent，最终由用户自行提交 develop PR。该指示优先于贡献指南的默认分支起点和逐模块 develop PR 流程；不改变其他任务的全局规则。提供方仍必须独立完成契约、实现、兼容样例和全量离线测试，经非作者审核合入集成分支后，消费者才接入生产实现。消费者可先实现 Port/Fake 或包装现有公开能力，不复制未合入的提供方代码。根工作区由 /root 写文档；公共 bootstrap 接线由 /root 串行协调到具体职责分支。Report reports.py 后续修改由 /root 明确指派单一作者，首批 Document/Record 分支不修改该文件。

以下负责人是本次任务的智能体职责分配，不改变仓库维护者的合并权限。2026-11-09 已向 /root/document_boundaries、/root/profile_boundaries、/root/agent_architecture 分别交接 Document 实施准备、Record 实施准备、非作者设计审核；其余批次由 /root 在前置契约完成后逐项交接，未交接不能视为已开始实现。

| 条目 | 所需支持、模块与公共接口 | 负责人、当前状态和依赖 | 验收条件 |
|---|---|---|---|
| I-031 | Document：OutputSelection、实际输入版本、版本化输出注册与缓存 | /root/document_boundaries；已独立交付并合入，真实可执行多版本及冷缓存/恢复已回归 | 同名版本并存、重复键拒绝；legacy 固定；未支持版本不自动 latest；缓存未命中/恢复不能冒用新解释器；旧发布行为不变 |
| I-032 | Record：RecordDocumentPort、RecordDocumentContextV1、现有入口 Adapter | /root/profile_boundaries 与后续 Record owner；本域 Context/Adapter、正式准备及默认消费已合入 | 字段、文件顺序、processing、首次问题输入及创建 checkpoint 等价；业务服务不直接消费共享 report 字段 |
| I-033 | Report：现有 DocumentGateway 显式选择 report-source-v16 | Report owner 独立消费固定 v16；本域投影/缓存、固定清单、正式 V2 与真实旧实现均已合入 | 保持解析、图片组合、缺失/隔离值和降级；不改 HTTP 参数、Prompt 或默认产物 |
| I-034 | Document：材料/事实快照、来源 Publisher、兼容发布桥；Profile：publicationRevision 支持 | Document 与 Profile 独立交付；不可变快照、来源 Publisher、单调修订及旧发布 gate 已接入默认三域 | 原件/OCR 复用；私有投影不作为公共事实；正式观察幂等生效；旧 sourceRevision 哈希不充当排序键；回退不复活撤回来源 |
| I-035 | Profile：唯一权威快照、来源幂等接收；消费者 renderer 和来源 Adapter | Profile 与各来源 owner 分别交付；读取/renderer、inbox/准入、固定原始来源均已合入，四类来源默认接线与独立后台恢复已完成，历史来源不自动迁移 | 不创建多套治理真值、不反写 Session；重复/乱序/撤回可恢复；复用持久标记和补偿扫描，不预设跨集合事务 |
| I-036 | Agent 平台：AgentPort、版本注册、工具事件/结果、预算/权限/取消及回执 | 平台 owner 已交付并接入；固定端口/工具循环、预算、权限、取消、注册失败隔离已有回归 | AION 0.6.0 工具适配、累计用量、流式隔离；注册回滚不污染其他模块；原生工具循环不冒充逐步持久恢复 |
| I-037 | Report：公开持久后续受理；GutBarrier：公开委派 Adapter | Report 提供方先合入，GutBarrier 再消费；公开持久三类委派与持续回调已接通 | 仅 explain_gutBarrier/opinion/instruction；回调保留原 finalReportId/type；不改变报告列表/Chat 选择；主回调与委派独立、禁止双链正式执行 |
| I-038 | 五域 Agent 封装、受控规则工具、Knowledge RAG、本域后台恢复 | 五域按顺序独立封装并合入；复用既有规则/Prompt，公共检索 RAG 与无查询恢复已回归 | 不为封装增加模型调用；评分/Prompt/接口等价；查询不启动 Agent；移除查询唤醒前必须证明后台独立恢复 |
| I-039 | Report：主控与 ResearchAgent 有界补查 | Report 私有 ResearchAgent 已接入有界研究循环并默认关闭；离线闭环/最终内容隔离已回归，真实质量待评测 | 默认关闭；预算、证据、引用、成本与耗时可评测；内部草稿不进 SSE；成功正文与保存结果一致 |

每个实现子分支必须回填源提交、创建/调整时集成基线 SHA、依赖提交/交接记录、全量测试命令与结果；最终 develop PR 由用户提交。生成成功、回调成功和画像摄取成功分别判断；Report/GutBarrier 持久回调继续对非 2xx 退避补投，不受生成/委派预算耗尽影响。真实服务和部署验收未执行时保持未验收，不因离线测试通过关闭历史风险。

### I-040 Questionnaire 私有生成规则冻结

状态：本地实现已合入 `dbf564f`；负责人 `/root/document_boundaries`，分支 `codex/questionnaire-owned-policy`，基线 `refactor/agent@efca880`。最终缺口审计发现 Questionnaire 生成/因子 Adapter 直接引用 Record 私有 lifestyle 目录、桥接文案及呈现校验；Record 独立迭代可在 Questionnaire 固定执行版本不变时改变其模型输入和验收。该依赖属于问卷本域的选题/呈现政策，由 Questionnaire 原样冻结所需 v1 叶子规则与有序目录，移除对应 Record 私有 import；不新增公共概念服务、不改变 Prompt、键序、选项及当前行为。提供方支持不再需要，已有公开 gate/session 协作保持。

验收：固定本域政策修订并明确历史清单映射；未知选择在模型执行前拒绝；Record 目录与校验器改动不会影响 Questionnaire；现有生成/评估回归、Ruff、完整离线测试和非作者复核通过。该项是既定模块解耦范围的收口，不扩展到全库历史分层整理。

### I-041 Document 菌群原始事实读取独立冻结

状态：本地实现已合入 `a771f33`（格式前置 `a9496b4`）；负责人 `/root/agent_architecture`，分支 `codex/document-microbiome-policy`，基线 `refactor/agent@efca880`。最终审计发现通用 Analysis/Record 文档解释器的 `adapters.analysis.microbiome_source` 调用 Report 私有解析、来源记录及上下文函数；Report 独立升级仍可能在 Document 固定版本的冷缓存路径改变 Record 输入。由 Document 适配层冻结所需原件事实算法和实际 v1 实现，移除 Report 私有函数依赖；不改变现有投影、Prompt、公开 HTTP 或既有输入版本语义。

验收：现有菌群材料读取与质量/缺失/来源规则等价；Report 私有算法变动后 Document 固定实现不变；有真实现成材料 fixture 时复用，不查询真实服务；未知选择明确失败；Ruff、完整离线测试和非作者复核通过。该项用于兑现 Report 升级不影响 Record 的首要验收，不扩展 Report 私有投影实现。

### I-042 Chat 启动来源扫描未等待异步聚合

用户启动日志显示 Web 已监听，但 `worker:chat-source` 每轮在 `aggregate(...).to_list(...)` 抛出 AttributeError；PyMongo 4.16.0 的 `aggregate` 是协程，必须先等待取得游标。异常发生在 `recover_pending` 的发现阶段，阻断本轮待办领取与画像投递，不回滚业务消息。原 BSON 替身误用同步 `aggregate`，导致此前离线验收未覆盖真实驱动的异步契约。

负责人 `/root`，非作者审计与复核 `/root/document_boundaries`；独立分支 `codex/chat-source-async-cursor`，创建基线 `refactor/agent@ebc6271`，按本任务约定验收后本地合回。范围仅为 Chat 存储调用、对应测试及实现说明，无新增跨模块依赖。全仓同类审计确认 GutBarrier 已先等待聚合游标，无需修改其实现；`find()` 是同步游标工厂，保持原用法。

修复先等待聚合再异步读取本页，保留过滤、排序、批量、超时与 simple collation；校正共享 BSON 替身，并以实际 SDK autospec 覆盖空页及有来源页。修复前四个回归均复现原异常，见本地 `chat-source-async-red.log`；修复后的定向回归、Ruff、完整 `python -m pytest tests/unit` 和非作者结论分别核对 `D:/996-work/shulan-test-artifacts/chat-source-async-*`，提交与树由 `chat-source-async-validation.json` 绑定。测试结果以最终记录为准，不替代真实 MongoDB 或部署恢复验收。重启沿原持久状态恢复，不自动重置已过期任务的固定预算；详见 [Chat 来源装配说明](wiki/chat-source-composition.md)。

### 本地交付检查点（2026-11-10）

本次交付保留现有 31 个路由模式、32 个业务方法入口（不把通用 CORS 预检计作业务接口）；五个平级主 Agent、普通数据旁路、固定文档消费、唯一 Profile 事实治理、公开 Report 委派及持续回调均已完成本地接线。Record/Questionnaire 查询不恢复，业务来源保存不等待 Profile，Report 私有需求不会隐式更换 Record 文档输入。新研究路径仍默认关闭；通用补充输入、跨域会话交接、跨域取消、全域自主循环、旧版本退役自动化及阶段 7 独立服务不纳入本次完成声明。

最终提交重验使用 `C:/Users/MSR/.conda/envs/shulan/python.exe -m pytest tests/unit`（Python 3.12.12，AION 0.6.0），与其他全量任务串行运行；本地记录为 `D:/996-work/shulan-test-artifacts/agent-final-full-1.log`，提交 SHA、树与结束结果另保存在同目录 `agent-final-validation.json`。六图渲染与源码清单为 `agent-diagrams-final`，路由核查为 `agent-final-routes.json`。实际结束结果须与对应提交核对，不以更早候选结果替代；最终交付回复提供该次完整结果。全部源码检查、Markdown 格式及链接在最后提交前执行，测试期间不修改候选。测试结束后仅在仓库外保存验证回执，保证交付提交与测试内容一致。

第二十三批本地实现交付完成。三个来源装配提供方已非作者复核并独立合入：Questionnaire `56c85ae`（12115 passed / 275 skipped，427.09 秒，`questionnaire-source-composition-full1.log`）、Chat `812ce45`（12114 / 275，454.29 秒，`chat-source-composition-full1.log`）、Record `b5ef222`（12114 / 275，384.29 秒，`record-source-composition-full1.log`），创建基线均为 `3c9db5a`。随后 Questionnaire 默认消费者 `d547777`（创建基线 `efca880`）已验收并合入：12158 / 275，407.51 秒，`questionnaire-source-default-full1.log`。Record 和 Chat 默认消费者分别使用 `codex/record-source-default`、`codex/chat-source-default`，创建于提供方已合入的 `f6d6277`；同步前序已验收消费者后才运行最终全量并按序合入。各消费者根代码由 `/root` 编写，Profile owner 独立审核；Q/Record 根测试由 Profile owner 编写，`/root` 复核，公共装配串行协调。

Record 默认消费者 `1e72ab2`（功能 `d463a9e`，同步前序 Q 基线后复测）已通过非作者复核并合入：12171 passed / 275 skipped，440.17 秒，`record-source-default-full1.log`。I-040 Questionnaire 自有政策 `dbf564f`：12186 / 275，430.69 秒，`questionnaire-owned-policy-full1.log`；I-041 Document 菌群叶子规则 `a771f33`：12175 / 275，378.85 秒，`document-microbiome-full2.log`，两者创建基线均为 `efca880`，均独立审核并绑定最终 SHA。I-041 首轮并行全量有一个原 Record 500 毫秒等待测试返回合法 processing；同一无 source_fact_reader fixture 单独三项通过，候选和断言未改，串行完整复跑通过；full1 的 1 failed / 12174 passed 不计通过证据。Chat 最后候选 `a2c72e6` 已同步全部上述依赖并经最终非作者复核合入：12228 passed / 275 skipped，421.12 秒，`chat-source-default-full1.log`。三域同时开启回归 14 项通过，固定 reader/fence 同时注册，只有一套 Profile 摄取 worker，Document 三项和业务三域共六个独立后台入口。

六图已按真实封装、正式来源与恢复边界调整并重新渲染、逐张检查；目标循环、通用输入/取消和版本退役不会被描述成全域已运行。契约表明确设计名称与真实 Python 接口差别，Document 恢复表区分旧解析推进与新的 Formal/outbox/Analysis 周期。三个环境模板以显式 `check-env --example .env.example --env .env.example.<dev|test|production> --no-values --json` 检查均 errors=0、consistent=true、passed=true；各有 5 条模板占位值提示，未读取私有配置。

第二十二批纳入默认 Document 根装配 `eea50db`（基线 `6b90b69`，12114 passed / 275 skipped，`source-composition-full-1.log`，415.87 秒）、Chat 保存终态修复 `e0e4947`（基线 `810d087`，12030 / 275，`chat-stream-persistence-full1.log`，422.44 秒）及 Report Research 配置隔离 `e90d4a2`（基线 `44138c9`，12068 / 275，`research-isolation-full2.log`，371.95 秒）。各候选已非作者复核并绑定最终 SHA；根真实组件测试由独立作者编写、主协调者复核。根装配统一三域处理器与旧发布 gate，先创建纯 reader 再创建 Profile 和命令侧，唯一画像摄取入口注册完整 Document resolver。新增 Formal/outbox/Analysis 周期有防重叠和有界游标推进，保持普通查询不恢复。实际端到端发现的 ready 状态端口误接已修复并回归；Research journal 等待期间配置身份变化也明确失败，不被临时检索降级吞掉；Chat 保存失败不再发送成功 finish。第二十二批组合完整离线测试通过：12138 passed / 275 skipped，385.27 秒，日志 `agent-batch-22-full-1.log`。Questionnaire、Record、Chat 独立业务来源默认开启仍按顺序实施，辅助装配提供方已分别指派到独立子分支。

第二十一批独立候选已合入共同分支：Document 显式 Formal V2 `c9a2dbc`（基线 `1e07d97`，12021 passed / 275 skipped，`document-formal-projection-reuse-full1.log`，373.47 秒），Report 新消费者 `476f55b`（基线 `810d087`，12047 / 275，`report-formal-documents/full-1.log`，406.81 秒），Record 正式文档消费者 `610be269`（功能提交 `f01a885`，同步基线 `1e07d97`，12038 / 275，`record-formal-documents-full1.log`，413.09 秒），Analysis `8d83ff4`（基线 `810d087`，12039 / 275，`analysis-formal-full-2.log`，383.33 秒）。各候选均完成非作者复核并绑定最终 SHA。Report 新版本冷 legacy 路径只提取一次；旧 V1 保持真实实现和缓存身份。Record 等待真实归档回执，普通 pending 不消耗创建重试且不延长 TTL；终态按单附件降级。Analysis 在解析前持久登记固定输入意图，取消、超时和回执丢失后不靠新 HTTP 请求恢复；可重试旧失败在未证明本次终态时等待原期限，不误封成功重试。第二十一批组合完整离线测试为 12097 passed / 275 skipped（`agent-batch-21-full-1.log`，410.69 秒），29 个 Python 文件 Ruff 和 9 份 Markdown 格式、链接通过，六图源码与已渲染版本一致；五份根文档经非作者复核。默认公共根装配与后台周期仍在独立 `codex/source-composition` 实施，不能把消费者交付写成默认来源切换完成。

阶段审计另登记两个独立可靠性收口：Research 非核心配置失败应只使依赖能力不可用，不能阻止其他业务和普通查询启动；Chat 保存失败不能发送成功 finish。分别由 `/root/agent_architecture`、`/root/document_boundaries` 独立交付；已存在的普通 Profile 读取初始化副作用作为兼容边界记录，不扩展成新模型或恢复流程。

第一批集成基线 `82b8a45` 完整离线测试为 10567 passed / 275 skipped，包含 Document 显式版本及公共快照、Record 文件 Port、Report 输出选择、Profile 公共契约、工具执行平台及 GutBarrier 公开持久委派。

第二批候选分别完成独立审核：Report 主 Agent `3aa1dcc`（10577 / 275）、GutBarrier 主 Agent `a6e436d`（10576 / 275）、Chat 主 Agent `671b886`（10581 / 275，安装失败隔离后续修正 `122e2fc` 在组合门禁重验）、Document 派生解读桥 `b2e1c76`（10583 / 275）、Report 公共画像读取 `a6cc757`（10573 / 275）。主协调者负责公共装配及最终组合门禁，不能用这些独立结果替代组合测试。

Record 主 Agent/后台恢复由 `/root/document_boundaries` 负责，Questionnaire 主 Agent/只读查询由 `/root/agent_architecture` 负责。Profile 三类业务来源及 Record 部分应用/撤回结算由 `/root/profile_boundaries` 独立交付；消费者仅在相应公开能力合入后切换。Report `reports.py` 仍由 `/root` 唯一协调；ResearchAgent 已完成本地实现与离线闭环验证，默认关闭；真实质量评测不以离线测试替代。

第二批组合已合入 `97cf5f5`（10627 passed / 275 skipped），第三批组合已合入 `3ef04f9`（10720 passed / 275 skipped），包含 Record/Questionnaire 主 Agent、无查询恢复、Profile 多来源提供方与组合根接线。测试收集同名冲突和旧注入 fixture 已修正并重新全量验证。

第四批已合入 `38364f7`（10796 passed / 275 skipped，最终组合日志 `agent-batch-4-full-2.log`，276.49 秒），四份文档格式与本地链接通过，六张 Mermaid 已完成语法检查和本地渲染预览。独立候选包括：Document 固定准备快照兼容 Publisher `34f982c`（10649 / 275）、Record 消费契约 `f1e059d`（10658 / 275；主服务与真实提供方接线另交付）、Report ResearchAgent `4896cd6`（同步第三批后10743 / 275）。独立审核后由主协调者执行上述最终组合门禁。

后续提供方：`refactor/document-material-reader` 交付固定原件和失败时材料快照，供 Report 自有投影保持旧回退；`refactor/profile-record-settlement` 交付 Record v2 原始来源、部分应用及撤回结算。消费者分支分别为 `refactor/record-document-consumer` 与 `refactor/report-document-consumer`，提供方合入共同基线后再接真适配器。Profile 的 Chat/Record renderer 及所有正式来源切换仍待逐域交付。不存在已自动完成的生产发布切换。

第五批候选已分别完成非作者审核并同步共同基线全量：Document 材料读取 `96ae8aa`（10823 / 275）、Profile Record v2 与撤回结算 `0bfc67f`（10814 / 275）、Report 自有投影 `d59a2af`（10810 / 275）、Record 真实公共命令接入 `2ffc7a1`（10814 / 275）。组合根接入 Record 默认生产路径，并按持久消费者修订保留历史任务旧实现；Record 状态/历史查询继续纯读。最终组合测试另行记录，不以单分支数字替代。

新增依赖：Report 消费者仍需 Document 独立提供旧 `report-source-v16` 兼容发布命令，保持已有 `reportSource` 正式来源及旧发布身份；新的消费者私有投影不能直接当作公共事实。Profile 需独立提供无副作用的结构化分页契约，显式绑定主体、查询条件及修订，之后 Chat、Record 分别实现自己的排序、配额和上下文渲染，不能用单页截断替换旧选择语义。两项分别由 `/root/agent_architecture`、`/root/profile_boundaries` 负责；另由 `/root` 独立提供 Document 补充观察纯读门面，复用现有条目与 `_id` 顺序，读取不建索引、不创建主体；Record 的本域 renderer 由 `/root/document_boundaries` 实施，生产装配等待提供方验收。`/root` 负责串行公共装配，消费者等待已审核共同基线。

第五批已合入 `ab44768`，组合根与四个独立候选的完整离线测试为 10879 passed / 275 skipped（`agent-batch-5-full-1.log`，289.86 秒）；31 个 Python 文件、11 份 Markdown、六图渲染通过。第六批纯读提供方候选为 Document `4da4d69`（10895 / 275）与 Profile `68aa6c8`（10949 / 275），均已独立审核。第六批组合全量为 10965 passed / 275 skipped（`agent-batch-6-full-1.log`，309.81 秒），12 个 Python 文件 Ruff、7 份 Markdown 格式与链接检查通过，根文档由非作者复核。第六批只先交付公共分页、旧摄取状态和原始观察读取；Chat/Record 本域 renderer 随后同步共同基线接实际 Adapter，当前不把 Port/Fake 候选写成生产完成。

第七批提供方候选：Document Report 兼容发布 `836f45a`（10905 / 275，303.12 秒）与公共观察装配 `6159d7f`（10977 / 275，304.52 秒），均经非作者复核。第七批组合完整离线测试为 11003 passed / 275 skipped（`agent-batch-7-full-1.log`，323.82 秒），11 个 Python 文件 Ruff 和 6 份 Markdown 格式、链接检查通过。Report 正常组合首次文档去重与原文降级保留顺序是两条兼容路径；生产消费者接入另行验收。后续 Document 正式发布生命周期由 `/root/agent_architecture` 独立迭代，Profile 接收能力保持公开契约：不得仅凭扫描就宣称旧写者已排空，也不能用单份基础事实覆盖旧 Report 补充来源。完整来源材料、撤回后的观察过滤与持久投递必须各有验收。

第八批候选均已独立复核并全量验证：Record 本域画像消费 `b9d3789`（11008 / 275，323.37 秒），Chat 本域画像消费 `448af52`（11003 / 275，328.48 秒），Report 正常与原文降级组合修正 `d04843c`（10968 / 275，299.02 秒），Document 真实提取 trace 固定 `6c397d9`（11005 / 275，299.94 秒）。第八批组合全量为 11089 passed / 275 skipped（`agent-batch-8-full-1.log`，317.33 秒），16 个 Python 文件 Ruff 与 8 份 Markdown 格式、链接检查通过，六图源码与已渲染预览版本一致，根文档经非作者复核。各域 renderer 和真实公共 Adapter 已有实现，默认生产装配由主协调者在共同基线之后另行接通。Report 固定文档消费者由 `/root/document_boundaries` 独立接入；公开发布只保留正式旧来源，业务输出和缓存仍归 Report。完整 Document 来源材料的公共 reader/capture 与 Profile 专用 resolver 分别交付；Questionnaire 附件发布必须在答案来源切换时保留独立持久路径，不能丢掉旧正式副作用。

第九批候选：默认 Record/Chat 画像装配 `c95ca38`（11093 / 275，287.84 秒）与 Report 文档消费者执行清单 `f1ce5a6`（11010 / 275，308.38 秒），均完成非作者复核。第九批组合完整离线测试为 11100 passed / 275 skipped（`agent-batch-9-full-1.log`，322.01 秒），6 个 Python 文件 Ruff 与 7 份 Markdown 格式、链接检查通过，根文档经非作者复核。构造不启动读取或模型，两域拥有各自 renderer；旧显式 provider 注入与 server 历史导入门面保留。首轮全量发现该门面依赖并修复，最终以 `domain-profile-memory-full-2.log` 为准；此前收集失败不算通过。Report 受理固定文档消费者版本，不支持的旧清单明确失败封口，成功产物/持续回调不重新生成。新正式来源的生命周期、完整材料 capture/reader 与 Profile resolver 仍按提供方顺序独立验收，不把 Port/Fake 视为来源切换完成。

第十批候选均经独立复核：Document 发布生命周期 `963848b`（11130 / 275，322.56 秒）、Profile 完整文档来源契约 `49d99ea`（11125 / 275，288.40 秒）和 Report 固定文档消费者 `26a72ee`（11128 / 275，273.64 秒）。本批组合完整离线测试为 11216 passed / 275 skipped（`agent-batch-10-full-1.log`，309.38 秒），32 个 Python 文件 Ruff 与 7 份 Markdown 格式、链接检查通过，六图源码与已渲染预览版本一致，根文档经非作者复核；Report 默认生产装配由主协调者在共同基线后单独接通。Document 持久投递、撤回及观察水位已有实现，但完整来源 capture/reader、真实历史排空证明和默认发布切换仍未完成。Profile resolver 只接受经验证的完整贡献集合，不能把单份基础事实冒充完整来源；Questionnaire 附件正式发布仍须独立交付持久意图。后续 Document 完整来源由 `/root/agent_architecture`、Profile 纯读存在性证明与来源管理归档由 `/root/profile_boundaries` 独立交付；Record 来源消费者由 `/root/document_boundaries` 在 `codex/record-profile-source` 独立实施本域原始草稿、完成证据、固定版本与持久投递；旧 Profile Record 发布入口必须先支持对新模式/准入屏障的明确拒绝，提供方由 `/root/profile_boundaries` 独立交付，未完成前仅 Port/Fake，不启用双链。旧哈希修订、固定材料修订与单调发布顺序分别记录。

第十一批候选为 Report 默认固定文档装配 `644967f`，经非作者审核，作者全量为 11227 passed / 275 skipped（`report-document-composition-full-1.log`，301.11 秒）。默认真实 Runtime 与默认处理器使用固定快照、本域缓存及旧正式 Publisher；显式旧注入和已注入 Pipeline 保持兼容。配置失败隔离到 Report 文档能力，单独清单阻止恢复后隐式替换已受理任务。第十一批组合完整离线测试为 11227 passed / 275 skipped（`agent-batch-11-full-1.log`，333.29 秒），5 个 Python 文件 Ruff 与 6 份 Markdown 格式、链接通过，六图源码与已渲染预览版本一致，五份根文档由非作者复核。Chat 正式来源由 `/root` 在 `codex/chat-profile-source` 独立实施：原始消息与来源草稿同次保存，固定文本、时间、版本和预算；后续恢复、不可变快照及真实 Profile fresh 准入仍待接通。Profile 需独立对 Chat/Record 的 `profile-source-event.v1` 及未知 deliveryMode 拒绝旧发布，不能只等待 Profile marker 出现后才阻止双链。

第十二批候选为 Profile 新来源准入 `f7b11aa`，公共历史存在性证明、持久 blocked/ready 新来源授权、来源管理专用归档，以及 Chat/Record 新或未知 deliveryMode 对旧发布入口的拒绝均已独立交付。两位非作者分范围审核无阻断，作者最终全量为 11281 passed / 275 skipped（`profile-source-admission/full-2.log`，296.56 秒）；首轮两项既有错误文本兼容失败已修复，以 full2 为准。第十二批组合完整离线测试为 11292 passed / 275 skipped（`agent-batch-12-full-1.log`，312.97 秒），13 个 Python 文件 Ruff 与 5 份 Markdown 格式、链接通过，六图源码与已渲染预览版本一致，三份根文档经非作者复核；各域真实 owner fence、后台恢复与默认切换仍分别验收。Document fresh 尝试失败的旧链可用性需先修复；尚未持有正式权限的计算任务不等于已排空的旧发布者。

新增迁移前置：Document managed 来源必须覆盖 Record、Report 和独立 Analysis 的正式写入选择，不能只切一个消费者后阻断其他默认入口；Report 补充贡献需显式 active/retired 计划及修订 CAS。Questionnaire 答案迁移必须保留成功附件独立发布，去重 contentSha256/原 artifactId、跳过 document_reference，并保持既有 questionnaire-report UUID 与来源指纹；由 Document 后续独立提供读取 Questionnaire 公开固定归档及提交证明的兼容 Publisher，禁止伪装 canonical 文档快照或重复 OCR/模型调用。以上提供方依赖由 `/root/agent_architecture` 分批负责，Questionnaire 消费与根装配由 `/root` 协调。

第十三批候选为 Record `c2f087d`（11339 passed / 275 skipped，`record-profile-source-full3.log`，275.36 秒）和 Document `b5a486c`（11328 passed / 275 skipped，`document-complete-source/full-2.log`，277.22 秒），均已非作者审核并绑定最终提交。Record 先提交本域完成证据和固定来源意图，独立后台再调用 Profile；Profile 暂时不可用不阻塞业务成功响应。Document 完整中立贡献、来源快照和实际 Profile fresh 准入组合已交付，已有旧来源的拒绝探测不会遗留锁定旧写者的 gate；已签发证明或回执未知时仍保留防重保护。本批未默认开启新的正式来源，Record 真实 Admission/owner fence、Document managed 贡献集合及三域接入仍须独立交付。同批纳入 Profile 固定证据规则提供方 `d0e8dbb`（11468 passed / 275 skipped，`profile-evidence-policy/full-1.log`，339.08 秒），独立冻结可执行 v1 并提供显式版本纯函数契约。提供方加入后重新执行组合全量，以 `agent-batch-13-full-2.log` 为最终门禁；首轮未覆盖该提供方，不作为最终证据。最终组合全量为 11551 passed / 275 skipped（`agent-batch-13-full-2.log`，299.20 秒），27 个 Python 文件 Ruff 和 8 份 Markdown 格式、链接通过，六图源码与已渲染版本一致，根文档经非作者复核。

Questionnaire 证据语义规则提供方由 `/root/profile_boundaries` 在 `codex/profile-evidence-policy` 独立交付，冻结可执行 v1、精确选择与纯只读契约；消费者由 `/root` 在 `codex/questionnaire-evidence-policy` 接入本域 Port、Adapter、固定 Agent 清单和评估 checkpoint。依赖先合入共同基线，已知旧任务显式映射冻结 v1，不支持版本在模型调用前失败；不新增模型调用或改写原始答案。

第十四批候选为 Questionnaire 公开证据规则消费 `7202562`，已通过独立非作者复核；最终作者完整离线测试为 11580 passed / 275 skipped（`questionnaire-evidence-full-2.log`，294.54 秒）。原有问卷规则断言保留，真实 Adapter 明确选择冻结 v1，任务清单、输入与 checkpoint 固定实际版本；缺失的已知旧格式仅映射可执行 v1，当前格式的缺失/未知版本明确失败且不自动重试。full1 通过后发现版本错误仍走一般重试，已补两个用例并重跑，以 full2 为准。本批同步修正架构恢复表中过时的查询唤醒与 GutBarrier 私有后续路径说明。最终组合完整离线测试为 11580 passed / 275 skipped（`agent-batch-14-full-1.log`，381.12 秒），14 个 Python 文件 Ruff 和 6 份 Markdown 格式、链接通过，六图源码与已渲染版本一致，根文档经非作者复核。

当前消费者分工：Questionnaire 固定证据规则由 `/root` 在 `codex/questionnaire-evidence-policy` 接入，提供方已在共同基线 `95d4b62`；Record 真实来源 Admission/owner fence 由 `/root/document_boundaries` 在 `codex/record-source-admission` 独立交付。Chat 原始材料候选移交 `/root/profile_boundaries`，在既有 `codex/chat-profile-source` 保留原文/时间与固定材料，继续本域持久任务、补偿扫描和真实准入。Document managed 贡献计划由 `/root/agent_architecture` 在 `codex/document-managed-source` 交付：prior publication CAS、完整 active/retired 集合、真实 v16 中立输出与显式归档。base 更正只能退休实际依赖旧输入的补充贡献并保存证据，无关贡献保留；后续 Report 重算由消费者另行实施。各域默认装配仍由主协调者等待提供方审核合入后接通，不在本批文字更新中宣称完成。

Questionnaire 来源切换的新增前置由主协调者分支串行交付：先由 Questionnaire 提供公开的已提交材料/附件 reader 与真实提交证据，并在所属持久 store 的旧 pending 扫描中排除新或未知 deliveryMode；随后 Profile 独立补旧 Questionnaire sender 的新模式拒绝，最后消费者才可启用新完成意图。Document 附件兼容 Publisher 消费公开 owner reader，保持既有 UUID/指纹和附件顺序，不读取消费者私有集合或重新运行 OCR/模型。公开 reader、所属扫描过滤和旧 sender 拒绝均已在第十五批交付；附件 Publisher 与正式消费者仍待完成，不能依赖 Profile marker 出现后才隔离旧入队。

第十五批候选包含 Record 真实准入 `8509ffb`（作者基线 `95d4b62`，11589 passed / 275 skipped，`record-source-admission-full2.log`，339.87 秒）和 Chat 持久来源 `1c0e5c6`（同基线，11635 passed / 275 skipped，`chat-profile-source/full-1.log`，341.24 秒），均独立复核并绑定最终提交。Record 根装配必须复用 owner.evidence 作为完成证明；Chat 先独立构造持久 store、snapshot/fence reader，再注册 Profile 接口并构造投递。两域业务保存不调用 Profile，后台单独准入与摄取，默认启动接线仍未开启。同批 Questionnaire 已提交材料 `21d00de`（作者基线 `a959bd5`，11603 passed / 275 skipped，`questionnaire-submitted-material-full-1.log`，343.24 秒）与 Profile 旧问卷发送隔离 `02ccaef`（同基线，11613 passed / 275 skipped，`profile-questionnaire-isolation-full1.log`，355.90 秒）均完成非作者复核。新/未知模式在扫描限额前及 queued/入队/标记前被旧链排除；公开材料只读取已保存原文与归档，不调用 OCR 或模型。最终组合全量为 11758 passed / 275 skipped（`agent-batch-15-full-1.log`，352.83 秒），22 个 Python 文件 Ruff 和 8 份 Markdown 格式、链接通过，六图源码与已渲染版本一致；根共享文档经非作者复核。

后续无环装配依赖已交接：Record 需独立提供仅依赖 source_store 的固定材料 reader，Document 需先提供不依赖 Profile inbox/archive 的来源与 owner proof 读取资源，再装配实际执行命令。两者不使用初始化后的私有对象改写或临时授权占位。Questionnaire 公共已提交材料与旧 pending 模式过滤由 `/root` 在 `codex/questionnaire-submitted-material` 交付；Profile 旧 Questionnaire sender 的模式拒绝改由 `/root/document_boundaries` 在 `codex/profile-questionnaire-isolation` 独立交付。正式切换仍依次验收 Document、Questionnaire、Record、Chat，不能因为后两域的提供方先完成就提前打开默认发布。

后续公开来源编排缺口由 `/root` 单独作为 Document 提供方交付：统一固定 legacy/fresh/managed 路由、真实 Profile 准入与归档、命令进度及恢复；Report、Record、Analysis 只消费 Document 公开命令，不各自操作 Profile 发布协议。Document Questionnaire 附件命令由 `/root/agent_architecture` 独立实现，验证公开的 Questionnaire fresh ready，保留原附件身份、质量与持久记录回执；Questionnaire 来源消费者由 `/root/profile_boundaries` 独立接入，后台按 ready → 附件持久受理 → 答案事件顺序协调。已接受问卷的长名称/答案可能超过现有 Profile V1 指针或材料上限，该兼容缺口由 Profile 独立提供新版本后接入，不能截断原文或让画像故障阻塞提交。

第十六批候选纳入 Document managed 提供方 `2d27bb6`（作者基线 `95d4b62`，11568 passed / 275 skipped，`document-managed-source/full-2.log`，320.68 秒），独立审核已绑定最终提交。完整来源的 base 更正、同一次 v16 准备输出、补充/退休计划、归档与 CAS 回执以及先读后命令的无环装配已交付，旧 fresh 计划未放宽；消费者和默认切换仍待接通。同批 Record 独立材料 reader `f6723b6`（作者基线 `a959bd5`，11595 passed / 275 skipped，`record-source-material-reader-full1.log`，348.72 秒）经非作者完整审核，只依赖既有 store，保持原版本、归属、摘要与错误校验；主协调者按原内容和最终提交绑定审核。最终组合完整离线测试为 11790 passed / 275 skipped（`agent-batch-16-full-1.log`，347.15 秒），12 个 Python 文件 Ruff 和 7 份 Markdown 格式、链接通过，六图源码与已渲染版本一致；五份根文档经非作者复核。Profile Questionnaire V2 提供方由 `/root/document_boundaries` 在 `codex/profile-questionnaire-source-v2` 独立交付，保留 V1，按真实问卷持久预算推导材料/指针上界；Q 消费者待其合入再接真实版本。既有规范化 257–512 字符 userId 可被旧画像摄取，而公共 Scope V1 上限为 256；新接受任务对该已知范围固定 legacy 兼容选择，不截断身份，不在已接受新模式后回退；新 Scope 契约另立迭代，Record/Chat 默认接线也须核对同类边界。

第十七批候选为 Profile 问卷完整来源 V2 `03417d6`（作者基线 `ca1b88d`，11791 passed / 275 skipped，`profile-questionnaire-source-v2-full1.log`，333.94 秒），独立审核绑定最终提交。新契约显式固定 32 MiB 材料上限、最多 500 条完整答案映射证据、1039 字符转义指针与完整 submissionProof，处理修订独立；V1 和全局 Scope 不变，实测合法长答案保持完整，未修改问卷 HTTP、Prompt、消费者或默认接线。最终组合完整离线测试为 11823 passed / 275 skipped（`agent-batch-17-full-1.log`，357.81 秒），5 个 Python 文件 Ruff 和 6 份 Markdown 格式、链接通过，六图源码与已渲染版本一致，根文档经非作者复核；Document 附件提供方与 Questionnaire 消费者须同步该共同基线后再完成实际组合。

来源撤回回执的消费者校验补充由各域独立承担：Questionnaire 作者在当前本域分支补齐 superseded 的更大撤回修订、appliedRetraction 与 settled 水位证明；Chat 同类修正交由 `/root/document_boundaries` 从共同基线建立独立分支，不能只看 settled 即终结，也不跨域代改。

第十八批候选包括 Document 问卷附件公开 Publisher `840dc3b`（已同步第十七批共同基线，11851 passed / 275 skipped，`document-questionnaire-publication/full-2.log`，334.25 秒）与 Chat 撤回证明修正 `179cdf9`（作者基线 `d387862`，11806 passed / 275 skipped，`chat-source-retraction-proof-full1.log`，369.20 秒），均独立复核并绑定最终提交。附件发布采用公开已提交材料、固定 Profile V2 事件与真实 fresh ready，保留旧 UUID、指纹、顺序、内容去重及真实持久回执；已保存但待摄取不是发布失败。Chat 只有更大撤回修订且 appliedRetraction 和 settled 均覆盖时才接受 superseded。最终组合完整离线测试为 11867 passed / 275 skipped（`agent-batch-18-full-1.log`，386.82 秒），7 个 Python 文件 Ruff 和 7 份 Markdown 格式、链接通过，六图源码与已渲染版本一致；五份根文档经非作者复核。Questionnaire 实际 owner/附件全链由消费者同步共同基线后继续，默认来源尚未开启。

Document 统一正式编排由 `/root` 在 `codex/document-formal-source` 完成提供方收尾，消费者不得跨域操作 Profile 协议。待独立审核合入后，Report 新正式消费者由 `/root/agent_architecture` 独立交付、Record 新正式消费者由 `/root/document_boundaries` 独立交付；根负责 Analysis 公开适配和共同装配。新消费者修订必须并存可执行的旧实现；Record 已准备文件若尚无真实归档回执，创建流程保持持久 pending 并由后台恢复，不伪造修订或提前生成首问。Report 业务投影可先完成，画像应用和正式补充状态独立。实际默认切换仍依次 Document、Questionnaire、Record、Chat。

第十九批候选为 Document 正式来源编排 `72c655a`（作者基线 `d387862`，11816 passed / 275 skipped，`document-formal-full-3.log`，357.44 秒），完整内容及追加有界执行、真实进度回执和投影纯读已独立审核并绑定最终提交。26 项定向组合实际 Document/Profile 准入、归档、Inbox 与恢复，覆盖 legacy/fresh/managed、混合附件、固定旧 Report 防覆盖、撤回、ACK 丢失、租约、期限与长身份兼容。full1/2 后补充公开接口，以最终 full3 为作者证据。最终组合完整离线测试为 11893 passed / 275 skipped（`agent-batch-19-full-1.log`，401.01 秒），9 个 Python 文件 Ruff 和 5 份 Markdown 格式、链接通过，六图源码与已渲染版本一致；四份根文档经非作者复核。提供方不启动默认消费者或后台周期。

公共装配前置补充：Document publication runtime 当前工厂总创建新 ledger，需独立提供复用已构造 shared ledger 的路径，以支持纯 source/fence reader → Profile → publication runtime 的无环装配；由 `/root/document_boundaries` 在 `codex/document-runtime-composition` 独立交付，不用延迟可变代理或复制工厂实现。Chat 新来源对旧业务长身份/内容的较窄限制由 `/root` 在 `codex/chat-source-compatibility` 单独补首次固定 legacy 选择；已受理新或未知模式不得回退。Record 同类上界须在本域接线前核对。

第二十批候选纳入 Questionnaire 持久来源消费者 `f7407c1`（作者基线 `f89e3e5`，11944 passed / 275 skipped，`questionnaire-profile-source-full1.log`，389.67 秒），真实 Profile V2、owner/fence、附件 Publisher 和答案摄取闭环已独立审核。业务提交只保存本域原始材料与固定意图，后台 ready → 附件持久记录 → 答案事件；原文分块不截断，严格撤回证明，旧任务沿旧版本，新模式不回退。同批 Chat 既有长输入兼容 `1e5a8d3`（作者基线 `04ece50`，11841 passed / 275 skipped，`chat-source-compatibility-full-2.log`，352.58 秒）和 Document shared ledger 工厂 `d3d3200`（作者基线 `f89e3e5`，11885 passed / 275 skipped，`document-runtime-composition-full1.log`，373.46 秒）均经非作者审核并绑定最终提交。Chat 的空白 messageId/非字典旧 marker 兼容复核问题已修复后重跑 full2；ledger 显式模式拒绝双集合配置，构造零 I/O，实际只读 reader → Profile → runtime → managed 组合通过。最终组合完整离线测试为 12006 passed / 275 skipped（`agent-batch-20-full-1.log`，360.09 秒），18 个 Python 文件 Ruff 和 7 份 Markdown 格式、链接通过，六图源码与已渲染版本一致；四份根文档经非作者复核。默认来源及周期仍未开启。

Report 新消费者实际组合发现 Document Formal 的 legacy 冷缓存路径可能在 managed 准备后再次调用 v16 coordinator；提供方复用固定准备输出的能力由 `/root/profile_boundaries` 在 `codex/document-formal-projection-reuse` 独立交付，保持旧 cache/source key/opaque revision 及真实 trace，Report 不跨域代改。其最终接入需验证完整链路调用数，不能只统计 Report 私有投影。Analysis 由 `/root` 在 `codex/analysis-formal-source` 独立实现显式正式发布策略，保留原参数校验、别名预留、历史首次快照和错误返回；新 base 与 alias 意图持久恢复，默认根接线仍后续串行。

未完成的生产来源切换继续使用已存在的正式 Publisher；影子 prepare/read 不发布。缺少版本身份的旧任务不得自动认领新实现。所有真实模型、数据库并发、外部回调与部署验证仍未验收。

## refactor/agent 审查遗留问题（2026-11-10）

处理安排：按用户决定，以下问题留待后续针对各 Agent 和所属模块的独立迭代处理，涉及公共契约和组合根时先明确提供方、消费者及交接范围。本次仅登记，不修改生产代码；三项均为待修复，不能以既有离线测试通过或已登记替代修复验收。

审查版本为 `refactor/agent@ab227035680e6e1040d8e9b525fd77b5bdce8caa`，比较基点为 `develop@da08e5f0d4bddfc116da779a9e813bdeec81ad41`。本轮 Agent 平台、组合根及运行时定向离线测试 148 项通过，另用现有假模型、内存/BSON 存储和实际业务方法复现下列三项回归；未执行全量测试、真实 MongoDB/Redis 故障或外部服务验收。后续修复需分别补回归用例，并按贡献指南完成全量离线验证。

### I-043 新来源发布后公开原报告指标查询为空

状态：2026-11-11 本地修复与离线验收完成，P1；Document 资料读取、Profile 查询及默认组合根已接通，真实部署待验收。

问题：默认 Analysis 已切换到正式来源发布，材料归档和画像事实写入不再维护旧 `document_observations`，但 `/api/v1/health-profile/document-observations/list` 与 `/api/v1/health-profile/get` 的 `documentObservations` 仍读取旧集合。分析成功且来源已应用后，新报告的原始指标仍无法通过这两个公开入口查询，不能用已入画像的事实替代完整原始指标。

定位：[默认装配](bootstrap/factory.py) 中的 `analysis_service_factory` 与 `document_observation_reader`；[画像服务装配](health_profile/application.py) 的 `_document_observations`；[画像查询服务](health_profile/service.py) 的 `list_document_observations`。公开契约见 [API 原报告指标查询](docs/api/API.md#查询原报告指标)。

复现：复用 `tests/unit/services/analysis/test_analysis_formal_source.py` 的 `setup/process/recover` 与正式来源测试的 `apply`；处理、画像应用和恢复后，分析返回 2 项指标、画像写入 2 项事实、来源为 `active`，公开查询所用存储返回 `items=[]`，观察集合为空。同样材料走保留的旧发布链时可查询到 2 项指标。

修复与验收：将公开原始指标读取接入新来源链，保持旧来源兼容、返回字段和分页语义；用实际默认装配覆盖新旧来源、指定 `documentId`、无日期及未入画像的原始指标，并同时验证独立列表接口和画像详情扩展。来源替换或撤回后的可见性需遵守对应生命周期，不以复制第二套事实真值解决读取缺口。

本地结果：Document c93da7e、Profile 79965d6 与默认装配 8a61f8b 组合验证正式来源应用前后可见性、standard/complete 列表和详情一致及来源撤回；既有观察兼容保留。具体完整测试记录见 I-046；核心回归位于 [实际组合测试](tests/unit/bootstrap/test_profile_archived_documents.py)。

### I-044 同一新文档并发首次发布导致报告别名缺失

状态：2026-11-11 本地修复与离线验收完成，P2；Document 协调同材料首次发布，Analysis 完成各自别名，真实部署待验收。

问题：同一用户和主体的两个请求并发处理同一新文档、使用不同 `reportId` 时，可能同时将路由固定为 `fresh_plan`。命令租约按不同请求的 `commandId` 隔离，只有一个请求能取得来源执行权；另一请求不会重新检查来源生命周期，可能立即失败或反复尝试 fresh 准入后耗尽重试。其分析内容已返回 `complete`，但别名发布尚未执行，对应 `reportId` 始终未归档。

定位：[正式来源步骤](modules/analysis/formal_source_steps.py) 的 `route` 与 `fresh_plan` 分支；[正式来源恢复](modules/analysis/formal_source.py) 的 `advance`；[Analysis 交付](modules/analysis/analysis_formal_delivery.py) 的别名发布前置判断。

复现：使用同一现有测试组件，在两个请求完成 fresh 路由判断后设置并发屏障，再继续执行及后台恢复。两次分析均返回 `complete`，一个命令和别名成功，另一个停在 `fresh_plan`，累计 8 次 `DocumentCompleteSourceUnavailable` 后失败且别名不存在；保留的旧发布链并发处理两个别名均可保存。

修复与验收：按来源协调首次发布竞争，或在竞争后核对并复用同一固定材料的已完成来源；保持已有版本、归属、冲突及撤回保护，不能简单放宽 fresh 准入。补充同来源不同请求/别名的并发、跨实例执行和重启恢复用例，确认两个别名最终可查询、正式来源不会重复生效，不重复解析或调用模型。

本地结果：c93da7e 在竞争后核对真实有效材料并复用既有基础来源，恢复各自别名；较早固定轮次只补原历史，不回退新来源或发布过期补充。实际提供方的并发、回执丢失和恢复回归及最终组合完整测试通过，记录见 I-046；回归位于 [固定归档测试](tests/unit/services/analysis/test_archived_extractions.py) 与 [Analysis 交付测试](tests/unit/services/analysis/test_analysis_formal_source.py)。

### I-045 QA 转 Chat 丢失完成标识导致旧会话重放冲突

状态：Open，P2；归属 Record 完成持久化与恢复，依赖 Chat 会话转换保留完成身份，后续按两域职责协作修复。

问题：升级前已有的活动 QA 会话沿用 legacy source consumer。完成快照已写入 MongoDB，但确认丢失或 Redis 清理失败时，`qaCommit` 仍可能留存；Mongo 的已完成状态会放行 Chat。实际 QA→Chat 转换丢弃新增 `recordCompletionId` 及 `documentProcessing`，导致同一完成结果重放时，标识比较和后续证据比较均失败，后台将 `qaCommit` 标记为失败并停止自动恢复。后台标记失败后，重复提交最后答案会在幂等结果检查前返回 409；若首次冲突由请求触发，该次可能返回 400，不能概括为首次必定 409。默认新来源的完成证据短路路径不等同于此 legacy 场景。

定位：[Record 完成存储](adapters/record/completion.py) 的 `_same_completion/publish_once`；[Record 应用服务](modules/record/application/service.py) 的 `hasPendingSession/_publishFrozenCompletion/_submitTurnImpl`；[Chat 应用服务](modules/chat/application/service.py) 的 `_convertQASessionToChat`。

复现：使用现有 `test_recovery_persistence.py` 完成快照和假存储，调用实际 `publish_once` 首次保存成功；模拟 Redis 留存待提交完成意图后，`hasPendingSession` 返回 False；经过真实 `_convertQASessionToChat` 后完成标识消失，再次提交同一快照抛出 `ValueError: completed Record identity conflicts with the current session owner`。现有仅修改 `type='chat'` 的替身转换保留了真实转换会丢弃的字段，未覆盖该场景。

修复与验收：Chat 转换保留不可变完成身份，Record 重放识别已提交的同一完成结果。使用真实转换方法覆盖完成提交确认丢失或 Redis 清理失败 → 转 Chat → 后台重放，确认 `qaCommit` 可正常清理、Chat 新消息不被覆盖、最后答案重复提交仍幂等成功；不同完成身份继续拒绝，保留新来源与 legacy 两条路径的回归覆盖。

## Agent POC 阻塞项

以下保留早期普通聊天 POC 的问题背景。2026-11-09 已按锁定的 AION 0.6.0 核对工具接口，当前项目集成范围见 I-036；旧 0.2.2 结论不得直接当作当前上游能力缺口。

### I-001 aion 尚未暴露工具调用结果

状态：Resolved（本地协议接入，2026-11-10）。AION 0.6.0 的 tool_functions/tool_options/tool_trace 已通过项目工具网关接入，具备工具事件、全轮累计用量、权限/参数/调用 ID/预算/取消校验；Report 私有研究循环默认关闭，离线真实 SDK MockTransport 已验证多轮闭环。I-036 的本地协议缺口已解除，真实模型质量仍单独验收。原生循环不是裸 tool_calls 的逐步 checkpoint 协议，重启不能从任意模型中间步继续。以下为 0.2.2 历史背景，不再要求先升级上游。

`aion 0.2.2` 可以把 `tools` 参数透传给 OpenAI-compatible API，但 `ChatResult` 只有 `content`、`reasoning_content` 和 `usage`。完整响应中的 `tool_calls`、`finish_reason`、响应 ID 和模型信息会丢失；流式 `delta.tool_calls` 同样会丢失。当前接口无法完成 Agent 的模型—工具—模型循环。

目标接口与验收测试已定义于 `docs/architecture/aion-agent-protocol.md`。本地 `aion` 源码已经完成实现并通过离线测试；在改动提交发布、Shulan 更新依赖前，该问题仍视为集成阻塞。

2026-08-17 补充：Chat 和 Record 已接入“无状态路由模型 → 允许列表检索 Gateway → 业务模型”的受控知识检索。该固定工作流不依赖模型原生 `tool_calls`，因此不改变 I-001 的 Deferred 状态；通用模型—工具—模型循环仍需 AION 补齐工具调用结果协议。

### I-002 aion 输入不是原生多消息协议

状态：Deferred（原生消息输入扩展）。AION 0.6.0 公共内容入口仍以字符串为主，工具历史由原生循环及 Context v3 管理；当前方案先复用这一边界，不将旧消息协议提案作为阶段 4 前置。只有实际需求无法满足时才另行评审。以下为早期提案历史，不代表本次已批准升级依赖。

`BaseClient._build_messages()` 总是把 `content` 包装成单个 `user` 消息。传入消息列表会变成嵌套在 `content` 中的列表，不能直接承载 `system/user/assistant/tool` 历史和工具结果。

已决定保持现有 `content` 参数位置并扩展为 `str | Sequence[ChatMessageLike]`：字符串保留旧行为，消息序列作为完整上下文且不隐式添加默认消息。本地 `aion` 源码实现及 10 个离线协议测试已完成，等待提交发布和 Shulan 依赖更新。实现细节见 `docs/architecture/aion-agent-protocol.md`。

### I-003 模型提供商配置未完全控制实现

状态：Partially resolved（2026-10-10 源码复核）。Chat、Record、Analysis 及 Report 主叙事/follow-up 均经 AgentHub/Graph/AionLLMGateway；统一 AI 静态扫描已包含 adapters。旧业务模型直连已由当前接线替换，但真实提供商切换、全部生产模型任务与内容一致性仍需验收，不在本轮关闭。

历史背景：2026-08-28 与 2026-09-03（6625647）确实发现 Report 业务直接创建 AION Client，旧统一入口门禁因此失败。该记录解释当时问题，不再代表当前源码。

## 当前实现风险

### I-029 Record 自然承接的真实措辞验收仍未通过

状态：Open（2026-10-23，`record-response-v19` 复验仍未通过整体表达验收）。v18已允许面向用户的帮助意图、一至两句交流及不重复主诉的上下文延续，移除显眼的“回到……，接着聊聊……”正例，并同步既有修复提示。三次提示迭代均使用具体主诉、多个报告关注点、无疾病健康目标三场景的完整对话验收；流程、题数、状态恢复和内部提示过滤检查通过，不能据此认定表达质量通过。

首轮无疾病健康目标场景末段连续使用“上题先放下或不急确认＋下题也一起记一记”的骨架，按约定判为失败。最终一轮虽未再出现该三连结构，仍有具体主诉场景第13道提问前的“一周能跑上三次，这点挺好。”、报告场景第6道提问前的“保持每周三次跑步，日常活动量也算稳定。”等复述与评价；健康目标场景第15道提问前还出现“忙碌或压力大的时候，您一般会让自己缓一缓。”，用户没有提供该行为。泛泛介绍主题关联和“已经知道、了解到了”等回执也未稳定消除。现有校验不能完整识别这些变体，仅强化提示词尚未满足表达验收。

完整合成对话与人工结果保存在隔离验收产物 `record-dialogue-style-20261023-v18`、`record-dialogue-style-20261023-v18-r2`、`record-dialogue-style-20261023-v18-r3`；均为内存存储和真实模型调用，不访问业务数据库、画像或回调。发布文本中既有模型原文，也有既定中性兜底，人工审阅分别统计；本轮不放宽来源或事实校验，不增加模型评审调用，不把承接或关注引用作为画像证据。后续须继续验证复述评价、未经提供的行为预设及连贯性，不以同义词替换或仅消除一种模板作为完成标准；I-028 的分析校验问题仍独立跟踪。

v19复验：三类场景各完成15道提问，共142次实际模型调用；具体主诉的首次样本另用了7次调用，因连续分析校验失败进入继续/结束选择并提前结束，已单独保留，不能计入完整措辞验收。完整样本的45条提问承接中，42条来自模型、3条为中性兜底，未出现空泛“一起”陪伴话术；没有逐字禁用或清理“一起”“也”。产物保存于隔离目录 `record-dialogue-style-20261023-v19`，真实调用合计149次，无主动故障注入或业务持久化写入。

本次用词改善未消除既有表达问题：具体主诉第7～9题连续使用“入睡困难这件事，再……看看……”只更换询问对象，第10、12题复述跑步次数和进餐差异；健康目标第5题出现“饮食方面有个大概轮廓了”，第9题“也帮您大致了解一下”仍属赘余，第13题提前宣称“最后了解”但后续还有两题；报告场景仍有泛泛解释主题关联。按完整对话标准，三场景整体表达均未通过，I-029保持Open；离线9984项通过、275项跳过只证明回归检查通过，不替代真实措辞验收，也不证明存在未处理标记的分析已全部成功。

### I-028 Record 已识别的 QA 疑问可能被无效建档事实阻断

状态：Open（2026-10-23 合成真实模型验收）。有血糖、血脂报告的合成用户完成15道提问及前置简答后，继续询问“报告中的血糖和血脂偏高，日常饮食和活动应该怎样调整？”。分析模型两次均识别为实际疑问且可直接回应，但首次把改写后的短语写入具体实体，未通过原话依据校验；修复时保留 `specificity=specific` 却将 `entities` 清空，再次失败。局部修复无法形成可用 intake，直接走分析兜底，未调用表达模型。

该轮增加了1轮QA计数，却仅展示自然邀请，没有实质答复；疑问保留 `pending`、`response=''`。此问题来自已有分析校验与兜底分支，不是内部处理提示过滤删除了有效答复。复现使用隔离验收工具、合成报告和内存存储，未访问生产数据库、画像或回调；外部记录 `record-notices-live-20261023/report/report_focus_rotation.json` 的第50、51次逻辑调用保存了原始分析结果。后续应验证独立有效疑问在无关建档事实无效时仍可进入简答，并覆盖修复失败、QA计数和结束状态，不能以流程完成或禁用提示检查通过代替答复质量验收。

### I-027 最终报告上传文件未进入共享画像来源链路

状态：Partially resolved（2026-10-10）。Report 现通过 adapters/report/document_source.py 接入共享处理服务，支持文件引用、专用输出、质量传播及受控画像来源；AI 生成产物仍不自动成为健康事实。

历史背景：2026-09-08 时 Report 的本地/DocMind 链路尚未接入共享服务。当前待完成的是实际部署上的“仅 Report 上传 → 画像指标 → URL/原文”联合验收，以及请求复用发生在下载前时同 URL 内容替换的协议限制。历史原文不自动恢复，提交结果不确定的来源仍需核查；详见 [共享文件指南](docs/architecture/shared-document-ingestion-guide.md)。

### I-026 指标摄取扩展不会自动修复历史缺失数据

状态：Partially resolved（2026-09-07）。旧实现只映射限定的 lab 数值项目，因此报告保存成功、甚至画像任务 completed，也可能没有产生任何指标。当前增量版本已扩展跨类型逐项结果、标准项目和未标准化结果保留，并在任务完成时保存计数及稳定说明码。

历史报告无主体、没有逐项结构、旧任务进入 dead_letter 或已 completed 的情况不会因升级或读取画像自动恢复。需授权后核实主体与来源，通过受控迁移、原任务重驱或原文件重新解析分别处理；同 ID 接口重试只返回原报告。新文件已统一全文分段并保存完整区间及逐段结果；已有成功快照仍保留原版本。无日期项目可在原报告指标视图查询，不伪造日期进入趋势。以上限制独立于 factGroups/indicatorGroups 展示，不能靠自动确认 pending 或填充虚假趋势解决。

### I-004 同一会话并发请求可能覆盖消息

状态：Partially resolved。普通聊天非流式、SSE、`custom` 卡片投递和 Record 已使用共享 Redis lease 覆盖会话加载、模型调用和业务消息落库。医生消息会在尝试 lease 前写入 MongoDB Outbox，AI 正在输出时返回 `202 queued`，完整回复保存后再按顺序追加，避免并发拒绝导致消息丢失。项目中其他整文档 `replace_one` 路径仍需继续审计。

普通对话读取整份会话、追加消息后再执行 `replace_one`。多个进程或请求同时处理相同 `(userId, sessionId)` 时可能基于相同旧数据写回。

### I-005 SSE 错误事件存在两种结构

`ChatSessionManager.sendMessageStream()` 产生统一响应信封；但 Handler 捕获的异常直接输出 `{type, error}`。客户端需要兼容两种错误结构，契约测试应先固定期望。

### I-006 请求与响应日志可能包含健康敏感数据

基础 Handler 会记录完整 request 和统一 response。普通对话、建档和报告内容可能进入日志，需要在 Agent 上线前脱敏。

### I-007 对话上下文固定截取最近 20 条消息

该方式会丢失早期重要事实，且不能按 Token 大小控制上下文。POC 应替换为近期消息 + 滚动摘要 + Token 预算。

### I-008 自动化契约与 Agent 评估基线不足

状态：Partially resolved。真实联调入口已统一到 `tests/api/`，并补齐全部当前路由的浏览器页面；自动化测试已统一到 `tests/unit/`。SSE 顺序、重启恢复、并发、工具调用和新旧实现一致性的自动化覆盖仍需继续补充。

### I-009 官方 MongoDB checkpointer 不使用异步 PyMongo

当前固定的 `langgraph-checkpoint-mongodb 0.4.0` 使用同步 PyMongo，异步接口通过线程执行器包装，并要求 `pymongo>=4.12,<4.17`。Agent Runtime 因此为 checkpointer 单独持有同步 Mongo Client，AION Context Repository 继续使用项目现有异步 Client。上线前需要在 Linux 环境验证执行器负载、连接池和服务重启恢复；当前版本固定 `pymongo 4.16.0`，升级 PyMongo 前必须先验证官方 checkpointer 的兼容范围。

### I-010 Agent 三类持久化不是原子提交

状态：Open。普通聊天模型调用成功后，AionLLMGateway 会先保存 AION Context，随后 ChatSessionManager 保存业务 Session；LangGraph 还会在各 super-step 写入 checkpoint。三者不属于同一个事务。如果 Context 保存成功但业务消息写入失败，客户端重试可能重复提交同一轮内容；新会话还可能遗留无法从业务 Session 定位的 Context 和 checkpoint。

当前 Redis lease 只能防止重叠执行，不能解决部分写入。正式上线前需要确定稳定 `requestId`、业务消息幂等写入、run/context revision 对应关系以及失败补偿策略。上下文所有权的长期重评见 `docs/architecture/agent-context-ownership-reassessment.md`。

### I-011 报告后台任务可能阻塞 processing 响应发送

状态：Resolved（旧 Pipeline 响应时序修复）。`ReportGenerateHandler` 过去调用 `finish()` 后未等待其 Future，便立即调度后台报告任务。后台解析若在首次让出事件循环前执行同步工作，调用方会延迟收到已经构造完成的 `processing` 响应。历史修复先把任务登记到 `TaskSupervisor`，消除“已受理但尚未追踪”的停服竞态，后台协程再主动让出事件循环，确保 processing 响应完成发送后才启动报告管线；对应测试验证登记与执行顺序，不依赖墙钟阈值。该路径仍用于显式旧 Pipeline 兼容构造；当前生产受理由 DurableReportService 先持久化批次，再由周期 Worker 执行，不能把旧时序修复当作持久受理保证。

### I-012 健康画像修改跨多个 MongoDB 文档且不是原子提交

状态：Partially mitigated。`subjects/upsert` 更新主体文档后，还需要推进画像投影版本并追加审计事件；事实确认、拒绝、更正和删除也会依次修改事实、推进画像投影并追加事件。当前通过主体级 Redis lease、事实/主体与画像乐观版本、`requestId`、请求指纹、唯一索引以及事实/主体/投影上的持久化请求标记，使相同请求重试能够继续补齐投影和事件且不重复推进版本。事实删除还会先将原文档替换为不含健康值的墓碑，避免补偿窗口继续暴露敏感值。但这些写入仍不在同一事务中；进程若恰好在中间退出，在请求重试或补偿扫描执行前仍会短暂不一致。

上线前需要结合生产 MongoDB 形态选择事务或可恢复的操作日志/补偿扫描。若生产使用副本集，可评估 MongoDB transaction；若需兼容单节点，则应以持久化操作记录驱动幂等补偿，不能依赖 Python 内存队列。该决策与操作系统无关，但与 MongoDB 部署形态强绑定，实施前需共同确认。

### I-013 早期画像文档可能存在两种时间字段存储类型

状态：Open。阶段 1/2 的模型曾以 JSON 模式生成 MongoDB 文档，`datetime` 因此会被写为 ISO 8601 字符串；当前已改为 BSON `datetime`，以保证事实有效时间的范围过滤和索引排序。健康事实摄取尚未上线，因此预计不存在需要迁移的历史事实，但已经初始化的主体、画像投影或审计事件可能仍有字符串时间。

部署到已有测试库或生产库前，应扫描五个画像集合的时间字段类型并执行一次幂等迁移；迁移完成前不得假设 `createdAt/updatedAt/effectivePeriod.start` 全部是 BSON Date。该问题只涉及画像新集合，不涉及本轮明确保持不变的 `services/analysis.py` 报告数据。

### I-014 知识库异常被伪装成合法空结果

状态：Resolved。EviMed 客户端过去捕获所有异常并返回空集合，导致认证、余额、限流、非 JSON 响应和协议结构错误都被 Agent Graph 标记为 `empty`；日志也只输出 `ValueError`，无法定位真实原因。

现已增加不包含敏感正文的分类诊断；Agent 调用使用严格错误模式，保留并发检索的部分成功结果，在没有任何可用资料时将失败标记为 `unavailable`，Chat 和 Record 仍按原契约降级调用业务模型。直接使用 `Knowledge` 客户端的旧调用默认仍在失败时返回空结果，避免改变既有兼容行为。

### I-015 建档流式模型空正文触发 JSON 解析异常

状态：Resolved。`record.question` 流过去可能只返回推理事件而没有正文，Service 随后将空字符串交给 JSON 解析器并终止本次问答。现已在解析前识别空正文，并复用现有的去重预设问题作为降级结果；非法正文同样在仍有预设问题可用时降级，避免问答流程中断。

### I-016 测试退出阶段进程池日志写入已关闭流

状态：Open。在配置完整环境中的既有全量 pytest 运行可以完成，但解释器退出时 `loongspears.utils.asyncio` 的进程池关闭回调仍会调用 logger；此时 pytest 捕获的输出流已经关闭，标准库 logging 因而输出 `ValueError: I/O operation on closed file`。异常发生在测试或收集结束后的退出清理阶段，不代表用户画像业务失败；具体测试数量随工作树持续变化，不在问题记录中固化。

后续应在 `loongspears.utils` 中调整进程池 shutdown 与日志 Handler 的生命周期，或确保测试在关闭捕获流前显式结束全局进程池；不要在 Shulan 业务代码中吞掉该异常。生产目标为 Linux，修复依赖后仍需在 Linux pytest 和服务正常退出两种场景验证。

### I-017 同一来源槽位的多个健康事实导致画像任务持续重试

状态：Resolved。建档或 Chat 的一条用户内容可能同时包含同一主体、同一事实类型的多个不同值，例如“本人头痛并且头晕”。旧提取器将第二个候选误判为非法重复槽位，并抛出 `extractor.duplicate_candidate_slot`，导致来源任务进入重试，最终可能转为死信。

现已允许同一逻辑槽位保留多个不同值：多值候选的稳定 ID 包含规范化值指纹，不依赖模型返回顺序；完全相同的候选则在写入前幂等去重。单值槽位继续沿用原候选 ID，避免影响已完成任务和重驱兼容性。提取器与画像图版本已经提升，去重告警只记录来源标识、数量和原因码，不输出健康事实正文。

### I-018 未指定主体的体检报告创建了无法成功的画像任务

状态：Resolved。旧报告接口省略 `subjectId` 时仍以空主体创建画像任务，处理器随后抛出 `subject.unresolved`，任务只能反复重试并最终进入死信。产品现已确认上传报告默认属于用户本人。

报告处理服务会在未传 `subjectId` 时创建或复用用户唯一的本人主体，再以实际主体完成幂等查询、报告保存和画像任务投递；接口响应返回实际绑定的 `subjectId`。修复前已经保存的空主体报告不自动回填；历史误投递任务仍以零候选安全完成。显式主体不存在、跨用户或来源与任务主体不一致继续作为真实错误处理。

### I-019 Tornado 安全停服尚未完成生产验证

状态：Partially resolved。应用现已实现 `STARTING / READY / DRAINING / STOPPED`、`SIGTERM/SIGINT`、`HTTPServer.stop()`、业务请求租约、`TaskSupervisor`、周期任务追踪以及独立 liveness/readiness。正常停服会先停止新流量和新画像任务，再等待 HTTP/SSE、最终报告和当前 Worker 全部自然完成；重复停止信号不会取消存量工作。

剩余风险是生产进程管理器配置和 Linux 真实信号尚未演练。如果 systemd、容器运行时或发布脚本在排空完成前发送 `SIGKILL`，应用无法继续保证无损。完成真实长请求/SSE/Worker/退出码验证和部署配置后，本问题才能关闭。

### I-020 最终报告已受理任务只存在于进程内

状态：Partially resolved（2026-10-10）。生产 Report 已在 processing 响应前持久化受理，生成/恢复/回调由进程内周期 Worker 调度；TaskSupervisor 只追踪本进程任务。待开始任务可重新调度，运行中断任务保留已完成产物并补齐失败结果，不重新解析或调用模型。

历史背景：早期任务只保存在进程内，强退可永久丢失。该源码缺口已接线解决，尚需真实 MongoDB 多进程、主节点切换、SIGKILL、旧 owner 迟到提交及回调幂等验证后才能关闭；独立 Worker 进程仍另行建设。当前回调持续重试，没有死信终止或 TTL，不套用旧迁移手册的有限重试设计。

### I-021 Web、Service 与最终报告模块的职责边界不一致

状态：Partially resolved（2026-10-10）。28 条本地路由已集中命名注册，非 Report 模块第一轮迁移及 Report/Gut Barrier 的显式服务注入已接线。现有静态门禁覆盖路由、注入和依赖方向，本轮未重跑全部门禁。

Report 已接入 Agent、画像、共享文件、产物/查询、Chat 追问和持久化服务；稳定模块应用接口、Pipeline 拆分、资源契约及独立 Worker 进程仍待收口。Health Profile 内部、Health/生命周期 settings、反向旧路径兼容、类型化容器与统一错误映射分别跟踪，详见 `docs/architecture/api-implementation-compliance.md`。

### I-022 长任务租约没有自动续期

状态：Open。Agent 会话 lease 和用户画像 job lease 使用固定 TTL。模型调用、报告解析或摄取任务超过 TTL 时，另一请求或实例可能取得同一逻辑工作的租约，造成并发执行、revision 冲突、重复外部调用或额外模型费用。当前画像写入具备较强幂等保护，但不能把幂等重放当作租约续期替代品。

需要为可超过 TTL 的任务增加带 owner/token 条件的周期续租，续租失败后停止产生新的外部副作用，并在 Linux 故障注入测试中覆盖事件循环阻塞、网络抖动、旧 owner 迟到提交和新 owner 重领。

### I-023 最终报告实现与预览/回调契约测试不一致

状态：Resolved（2026-08-20）。根因是合并提交 `9c67eda` 在保留新版报告实现的同时，把部分预览/回调测试与 `docs/api/API.md` 的最终报告段落回退到了健康管理评估升级前的版本，形成了 “新版实现 + 旧版断言/文档”的组合，并非安全停服功能引起的业务回归。

现已按 `76c97ce` 报告优化中已经确定的契约恢复一致性：固定回调包含 `healthAssessment` 和 `recommended_examinations`；健康关注等级不推断为疾病患病风险；疾病模型指数不进入患者回调或 HTML；循证查询按来源顺序为每个菌种保留一条精确关系；预览只读取固定回调 JSON，并隐藏内部样本标识、审计字段和工作流信息。最终报告专项测试已经全部通过。

### I-024 最终报告历史代码—API 行为偏差

状态：历史描述已过期（2026-09-03 源码/文档复核，基线 `6625647`）。原问题包括“文档强制存在可解析菌群来源，但代码接受空来源”和“普通 4xx/3xx 也自动重试”。当前 API 已不再要求菌群来源必填；`ReportRequest` 及 `test_empty_microbiome_sources_are_treated_as_pending_results` 保留空来源兼容。`adapters/report/callback.py` 现在只重试传输错误、429 和 5xx，普通 4xx 与重定向不重试，已有 `test_callback_does_not_retry_permanent_401` 覆盖永久错误。

上述为 2026-09-03 的历史代码—API 复核。当前生产 DurableReportService 的长期回调 Worker 对所有非 2xx 继续退避补投，不能把旧 callback Client 的单次重试分类套到持久队列；实际语义见 Report 指南第 14 节。本轮未重跑完整回归或真实回调验收；空来源、嵌套 extra 与状态码矩阵仍应以当前生产链路验证。改变现行行为前须完成 API 与调用方评审。

### I-025 最终报告未持久化且普通 Chat 无法追问报告

状态：Partially resolved（2026-10-10）。五类产物独立持久化、finalReportId、列表/详情、report.retrieve_context 及 Chat 完整/SSE、原/新会话上下文均已接线。已保存报告保持 AI 未核验属性，不自动回写画像；保存与回调交付分离。

历史背景：2026-09-03 时结果仅用于回调和进程内复用，普通 Chat 无法读取。当前须在实际部署版本验证原/新会话、多批次选择、重启读取、回调/存储失败、幂等及用户隔离，满足人工验收后才关闭本项。I-020 的任务恢复仍需单独验收，不能因产物落库一并关闭。

### I-012 报告 API 职责和路由分散

状态：Resolved（目录与路由归属范围）。公开路由集中在 web/routes.py，Report Handler、业务和 Adapter 分别归入 web/handlers、modules/report 和 adapters/report，生产依赖由 bootstrap 组装；旧 report_api 包已移除。模型接线与真实提供商验收见 I-003，不属于本条目录归属的关闭条件。外部请求、异步受理及回调契约以当前 API.md 为准。

## 已确认策略，不作为缺陷

- 未完成健康建档只在 Redis 保存两小时；超时后重新建档。
- 生产目标系统为 Linux，当前开发环境差异不改变 Python/Conda 运行约定。
- 单次项目运行只使用一个模型提供商，但必须允许通过配置整体切换。

## Document 当前基础信息集成边界（2026-11-13）

Document 已通过显式工厂组合 !133 的实际 Analysis 公开准备和固定轮次读取，以及 !132 的 Profile 当前接收。合成下载、OCR、模型和 BSON 存储已覆盖完整文件、部分轮次、原生证据和画像应用。观察时间修复及复审整改已通过 !134 合入 `develop@a3ccf12`。Document 同步该基线，闭环回归与全量验证对应 [I-048](#i-048-双来源基础信息与共享profile交接) 的最新候选。Report 原件投影、默认装配和暂留入口清理按后续阶段推进。详见[实施说明](docs/architecture/document-basic-info-v2.md)。
