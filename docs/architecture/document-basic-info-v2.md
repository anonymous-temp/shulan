# Document 当前基础信息与独立来源贡献

> 2026-11-13，`feature/document-basic-info` 同步 `develop@a3ccf12`，通过 !133 的 Analysis 公开准备与固定轮次读取、!132 的 Profile 当前接收，复验 !134 的观察时间修复。该批验证显式工厂组合。当前集成分支的默认配置见本文「默认装配与验收」。

## 当前责任与边界

Document 保存固定原文、实际准备绑定、逐字段与临床贡献、完整活动库存、生命周期和持久发布回执。Analysis 决定抽取结果及逐字段 `low|medium`，Profile 决定当前画像值；Document 只检查字段契约、原文证据和固定来源的一致性，不重评档位，不设置 API 最高档。

当前实现使用 `DocumentCurrentUnderstandingService`、`DocumentPreparedService`、当前来源 artifacts/reader、`DocumentCurrentAdmission`、`DocumentCurrentPlanReader`、`DocumentCurrentManagedSource` 和 `DocumentCurrentSourcePublisher` 一条路径。纯读取图与显式写命令复用同一贡献和计划算法，不是两套处理流程。`DocumentVersionedSourceReader`、旧贡献 carry 分派及新路径中的历史扫描、absence/drain、ready 授权门禁均已删除。现有 `.v2` 是唯一当前来源协议的标识，不表示并存两套业务算法。

早期 Document 分支独立验证公开端口。显式工厂位于 `adapters.analysis.basic_info`，构造时不执行模型、数据库写入或画像建档，也不注册默认服务。

## 公开准备与固定读取

`DocumentPreparationPort` 只依赖 Analysis 的两个公开方法：

```python
async def prepare_document(*, user_id: str, subject_id: str, **file_kwargs): ...
async def read_prepared_round(
    *, user_id: str, subject_id: str, document_id: str, interpretation_round: int
): ...
```

前者固定不发布，返回带 `preparation_binding` 和实际 `output_selection` 的公开处理结果；后者返回 `user_id/subject_id/document_id/preparation_binding/parsed_document/interpretation/output_selection`。固定轮次读取不得下载、OCR、调用模型、建档、写入或以最新索引替换原归档。

Document 不传版本选择、不引用 Analysis 私有对象。它核对请求主体、文档 ID、原轮次、解析和理解摘要以及准备前后真实绑定相等，再归档完整原文。抽取修订、校验修订和输出选择分别保留实际值，不推断三者同名，更不白名单某个 v12/v13。`DocumentCurrentBundle.as_dict()` 公开实际选择、轮次、三类固定引用及原请求回执；`DocumentPreparedInput.as_dict()/from_dict()` 固定相同引用、选择和回执，不选择新执行器。

`DocumentPreparedService` 使用既有持久记录表保存原准备回执；来源准入必须验证原回执。归档完成后的 `read_bundle` 和来源读路径只读 Document 固定快照，不回调 Analysis。

材料沿用 `document.material.v1`，事实、理解、贡献、库存、完整来源和捕获证明分别使用 `document.facts.v2`、`document.understanding.v2`、`document.source-contribution.v2`、`document.source-inventory.v2`、`document.source-observations.v2`、`document.source-capture-proof.v2`。这些是当前固定材料类型，读取不尝试旧类型或改标签。

## 字段与临床分别准入

理解中的 `basicInfo{name,gender,age}` 和逐字段 `basicInfoMeta` 原样归档。每字段保存 `status/sourceReliability/observedAt/reason/evidence/subjectStatus/subjectRef/subjectEvidence/candidates`。

只有 `status=extracted`、`subjectStatus=single`、有效单值、`low|medium`、原文证据和非空简短依据同时成立时才产生基础字段贡献。姓名 trim 后非空，性别为 `男|女`，报告年龄是 0 至 150 的有限数值，布尔值非法。报告日期保留原 ISO 日期或时间精度；不填造生日和观察时间。`not_found` 不产生候选，身份不明、多候选或冲突只归档待核实材料；章节结果不能绕过顶层身份结算独立写入同一主体。

证据 `{quote,start,end}` 必须与固定全文的 Python 字符区间 `[start,end)` 完全相等。Document 校验值域、档位及原文指针并分别归档基础字段和临床观察。各章节可共用同一受检者的原文身份依据。仅明确的多人证据触发分组，归属不明的观察保留待确认分组。

完整和部分解读都投递已提取的内容。固定临床贡献保留原 `processing` 质量与逐条核对结果，原件已支持的观察使用固定原件位置。值、时间或原件依据不足的观察作为待核实候选。Profile 负责主体归属、逐字段可信度选择及逐项事实状态。来源作用域仍用于原件归档、发布与撤回。

公开 `POST /api/v1/interpret/health-report` 的新临床发布由固定命令携带默认采信策略。Document 复用已归档的解读观察并保留该策略，跳过该临床贡献的原件逐项核验。Profile 根据已验证的发布来源将有结果的观察保存为 `active / confirmed`。系统默认采信的来源依据与人工确认分开。实际检查时间未知时另存首次画像录入时间；同一操作恢复沿用固定记录。Record、Report 等其他入口仍按各自证据准入。

同一主体和生命周期内，只有固定解读轮次的观察引用与已证明的原件位置一致时，跨入口贡献才共享画像事实。相同数值在不同原件位置分别保留。原件归档和解读结果可继续按原引用追溯。

固定 bundle 读取从原理解重新计算准入视图，并核对事实质量、理解质量与 `clinicalAccepted`；两份快照的 quality 一起改成 complete 并重建一致引用，也不能让原 partial 获得临床准入。缺少临床质量不影响有效基础字段独立进入画像，也不会在后续基础轮次撤回已固定的完整临床贡献。

完整来源保留：

- `contributions`：当前有序固定贡献引用；引用含真实 `basicInfo` 或临床摘要声明。
- `clinicalContributions`：独立临床贡献及观察数据；不重复人口学字段。
- `basicInfo`：所有活动基础字段的完整数组，逐条附固定 understanding/facts/round/extractor 引用。
- `clinicalAvailable`：有活动临床库存或明确临床退休证据时为真；基础信息 partial 且无临床证据时为假，不触碰既有临床。
- `coverage` 与库存：同一固定正整数 `lifecycleEpoch`、真实材料引用、库存摘要和有序贡献 ID。

当前 coverage、库存和贡献不含 `legacySourceRevision`，库存也不含 `legacyState`。`inventory.fence` 固定为 `{admissionKind: current, evidenceRef: planRef}`，其中 `evidenceRef` 与 `activeSetEvidenceRef` 均指本次持久计划。不能由客户端传完整标志自行证明完整库存。

## 持久准入、恢复与撤回

`DocumentCurrentAdmission.prepare_source_plan(scope, prepared, request_id, lifecycle_epoch)` 先核对原准备回执并捕获所有可用贡献，然后在既有记录表保存固定 manifest/plan，使用原 publication ledger 的 `admit_current_source` CAS 固定首次主体、来源、epoch、准备 manifest、计划和操作 ID。不扫描历史表，不申请迁移或额外授权，不增加投递队列。没有可发布贡献时只保留归档，不创建来源绑定。

传入的 epoch 必须来自调用方首次取得的 Profile 公开主体快照；恢复沿原 manifest，不重新查找当前本人。首次操作固定只约束原准入；后续 managed 命令仍通过原 publication revision CAS 形成新计划，当前 fence 同步指向新计划。旧默认写者票据检查仅保护串行装配期间的并发生产者，不能解释成历史迁移功能。

ACK 丢失时同一请求、manifest、epoch 和计划可恢复；更换任一固定输入明确冲突。来源读取重建并逐项核对原贡献、证据、库存和计划，不扫描最新结果、不重跑模型。新的完整临床库存可退休实际变化的旧临床输入；部分恢复保留原完整临床。基础字段按贡献 carry，缺失不清空，同档冲突不能通过先退休旧依据再加入新依据绕过 Profile 选择。

所有退休贡献保留固定证据且永久不能重入活动集。显式全部退休允许空库存，但保留真实 prior/material 引用。`document.retraction.v1` 当前正文新增正整数 `lifecycleEpoch`，只取原来源计划/命令，公开撤回 reader 还核对该来源首次持久计划中的实际 sourceRevision/epoch，不能只相信任意正整数正文或重新查找当前本人。真实 Profile 允许先受理 pub2 撤回并返回 waiting；pub1 迟到时先受已受理撤回屏障抑制，再逐轮完成结算，不在中间阶段复活画像或临床事实。该行为已使用双方真实提供方和合成 BSON I/O 组合验证。

`document.clinical-content.v1` 对完整临床理解和观察数据作稳定摘要，覆盖章节、总结、扩展及未知临床字段，排除拥有的人口学字段和执行元数据。因此仅基础信息变化保留原临床贡献，临床描述变化即使观察列表不变也会退休旧临床。Report 消费者可通过下文当前贡献命令声明固定输入。正式调用方接线在提供方合入后进行。

## 无循环公开装配与真实 Profile 接线

`create_document_basic_info_source_readers(prepared, ledger, admission_records)` 先构造固定材料/贡献 artifacts、初始准入、当前计划纯 reader、公开完整来源 reader 及 `DocumentCurrentProfileSourceReader`。返回的 `DocumentCurrentSourceReaders` 是冻结的装配值；其中公开来源 reader 不含 capture 命令，也不依赖 Profile Inbox 或 publisher。

`DocumentCurrentProfileSourceReader` 只在 Document 拥有的 adapter 边界将公开 `ProfileScope` 转为 `DocumentScope`，不解析当前本人或猜测归属。读取原文、来源和库存仍验证原固定 scope、摘要、计划与贡献。装配顺序为：

```python
readers = create_document_basic_info_source_readers(
    prepared=prepared, ledger=ledger, admission_records=records
)
resolver = ProfileCurrentDocumentSourceResolver(readers.profile)
profile = create_application_profile_public_contracts(
    resolvers=resolver.registrations(), basic_info_collection=basic_info_collection
)
runtime = create_document_basic_info_profile_runtime(
    readers=readers, profile_inbox=profile.inbox
)
```

这些是已存在且实际组合测试使用的公开工厂；示意代码省略所属模块导入与资源注入。Profile 先完整校验当前材料，再自行固定来源生命周期，Doc 不操作 Profile 私有表或应用方法。当前 `ProfileCurrentDocumentPublicationReceiver` 直接使用公开 Inbox `receive/get_receipt` 和公共回执字段；不调用 `get_cutover`、ready 或旧授权 API。

命令 runtime 复用已构造的相同 reader、plan reader 和 ledger，再构造 publisher、managed 命令与 `DocumentPublicationRecovery`。没有延迟代理、初始化后替换 reader/admission 或循环 service locator。原 `create_document_basic_info_source_runtime` 工厂只是该共同构造路径的入口，测试专用 Profile 替身和实际公开 Inbox 使用同一个发布/恢复算法。

## 验证与具名交接

`test_document_current_profile_integration.py` 组合真实 Document snapshot/chunk/计划/ledger、已合入的 `ProfileCurrentDocumentSourceResolver`、公开 Profile 工厂、Inbox、worker、BasicInfo 和 snapshot reader。Mongo I/O 使用合成集合并执行真实 BSON 编码往返；不是实际在线数据库。覆盖 complete/partial、最高档确认、基础退休与临床独立、无授权当前接收、ACK 丢失与双方重启、基础提交后 ACK 丢失及后续 API 更正、生命周期变化、乱序撤回不复活、固定快照和只读重建。撤回 reader 的原计划 epoch 绑定有独立先红后绿回归。质量专项同时覆盖 missing/null/非对象/无状态/非法状态、合法三种状态及重建一致引用后的伪造 quality 归档读取；真实双方组合验证缺失质量首次仅入基础信息、后续缺失/null/未知质量仍保留原临床事实。

无可读原文的失败轮次由 Analysis 保留自身诊断归档，公开 prepare 以不可用结束。Document 不为该轮次生成准备回执、固定材料或可发布贡献。可读取且有有效基础字段的部分轮次按基础与临床独立准入规则处理。

当前临床贡献将原生提取器的单个证据对象投影为单元素列表，原有列表保持顺序，原解释中的证据对象和解释摘要仍按实际内容固定。归档读回使用同一投影验证，画像接收保留完整证据。

Analysis 的 100 条候选限制按原始模型节点生效，已验证的报告汇总字段可以合并更多候选。Document 逐条校验并保留固定候选证据；未决字段不生成画像贡献，独立有效的人口学字段和临床材料可继续准入。

此前的合成准备端口用例继续隔离验证 Document 的归档和质量边界。本批另用已合入的 Analysis 服务与当前抽取图，通过 Document 和 Profile 的公开工厂验证实际模块调用。下载、OCR、模型及 MongoDB I/O 使用合成替身，实际 BSON 编解码和固定资料摘要仍参与验证。最终离线证据见 `document-analysis-profile-final.{log,xml,json}`。

交接责任：Analysis 与 Profile 公开提供方均已合入 develop。`/root/doc_provider` 负责本次 Document 联调，`/root/doc_review` 独立复核。协调者负责最终默认组合与完整回归，统一登记见 [I-048](../../ISSUE.md#i-048-双来源基础信息与共享profile交接)。

### 两轮审核整改

4596c7d 复审后，更正以已验证前驱的活动及累计退休库存检查同文档解释轮次。迟到旧轮次仍可读取，无法预留新发布。临床摘要只比较保留临床内容及结构归一后的观察，原始固定解释和实际证据指针用于追溯。当前运行时要求注入恢复器绑定同一账本和 current 路由。部分退休的 Profile 应用支持通过独立分支交付，实际组合回归见 I-048。

当前发布以完整材料的已验证计划为依据，在同一账本 CAS 内比对初次准入或更正的实际前驱。并发重试复用同一固定发布及首份回执，后续应用状态由账本记录。首次准入在固定绑定前构造并验证完整来源、库存和证明。计划读回只在本次校验内复用固定快照和派生贡献，各次读取均重新验证摘要、作用域与证明。共享账本在领取恢复任务前按所属路径分流，撤回沿用原来源绑定。

Analysis 日期修复及复审整改已通过 !134 合入 `develop@a3ccf12`。Document 通过实际公开端口验证观察时间在固定材料、已发布贡献和 Profile 当前选择中的一致性，覆盖紧凑日期时间、精度保留和无效时区的空值。同份报告中的其他有效贡献继续处理，验收记录见 I-048。

### Report 原件事实投影与发布依赖

现用 Report 原文件事实能力仍是有效业务，不能因为代码带有 legacy 命名就整段删除。公开 `modules.report.document_source.extract_intrinsic_report_source` 使用固定全文、原件表格和被接受的观察；`adapters.report.document_profiles.configure_report_source_profiles` 注册实际来源提取器。现用 `DocumentManagedReportPreparation.prepare` 已能冻结真实 manifest、selection、projection、outputTraceId 与原件引用；`ReportProjectionService.project_prepared` 复用固定输出，不第二次抽取。

公共 `utils.report_source_observations.report_source_items` 只翻译来源输出中的 metrics、panelCounts、taxa、原报告 diseaseResults.sourceScore/sourceLevel、enterotype 和 supplementaryHospitalData，不翻译 aiSummary、最终报告建议或生成的医疗结论。它不是原始事实 validator；字符串 quote 共现不能替代表格几何、跨页结构和字段角色证明。

Report 提供 `project_current_report_source_with_observation_refs`，固定 v2 投影沿用原回执读取。输入按请求顺序提供 `(DocumentCurrentBundle, bytes)`。调用方以固定主体校验准备结果并通过 `DocumentMaterialReader.read_original` 读取原件。Report 复验同组 scope、原件 SHA256、长度和归档解析文本，折叠同一固定文档的重复请求，拒绝同一文档冲突轮次。

1. Report 复用实际 `report_source/1/report-source-v16`，原生 PDF 表格读取关闭 OCR。当前输入适配修订 `report-understanding-current.v3` 在固定观察路径上识别根镜像和影像汇总，再依据同一原件的项目、结果及字段角色核验候选。相同结果按原件出处区分，保留可验证的标本及方法。正式临床贡献要求 `complete` 和原件证据，`partial` 与 `unavailable` 依现行质量规则处理。
2. Document 在执行回执中固定输入顺序、快照引用、原件摘要、实际 Analysis selection、Report 输入适配修订及输出 selection。投影、摘要和 trace 回执一并归档，供纯读使用。临床贡献固定原件出处和投影指针，补充检查行保存 `sourceDocumentId`、`sourceChecksum`、`sourceOccurrence` 及可用的 `sourcePage`、`specimen` 和 `method`。
3. 仅上述真实固定来源输出可转为现有 `kind=clinical` 贡献，保持完整 material/understanding/facts 引用审计与逐文档 `clinicalContentDigest` 依赖，不引入第三种公共 kind、不从生成正文抽事实。仅基础字段或轮次引用变化不能退休该 Report 来源；实际临床输入变化按对应 documentId 退休。旧不透明来源修订不用于排序或伪造新身份。
4. Report 消费者复用固定投影和原准备回执。历史 v2 的停止新准入、在途任务清点和切换步骤见 I-048。

当前 Document 显式工厂接收 `material_reader`，并提供 `report_projections.prepare` 与 `read`。已有不可变记录及快照保存有序输入、执行选择、适配修订、投影摘要、执行编号及逐文档临床依赖。快照完成后，重读和同请求重放只核验固定材料。若投影凭证已落盘但快照尚未完成，只能以原修订重新生成完全相同的结果。单份文件或连续图像页遵循 Report 的准入规则。

初始来源计划可绑定同主体、同生命周期且第一份固定材料完全一致的投影。后续命令可追加不同有序文档组，或指定活动 Report 贡献进行同组替换和有序续页扩展。续页扩展与原贡献的更正退休一起发布，保留未变页面的原件出处。

来源账本按文档固定最高已接受轮次。真实更正在预留发布的同一次 CAS 校验并更新轮次；有效的同摘要命令更新轮次和接受凭证，原贡献和发布版本继续使用。历史读取验证原固定证据，已接受命令可在回执中断后重放。Report 贡献 ID 绑定有序固定轮次，允许较新轮次以新的身份恢复早期临床内容。

正式临床贡献要求完整质量及可核实原件出处。显式退休按原件出处和所属文档的临床摘要逐条筛选。固定投影完整保留，发布证明绑定原顺序和实际选入观察。报告中相邻的数值和可识别单位在 Document 转换时拆分，并保持固定原件证据。缺少原件出处的结构化数据留在固定投影中。默认组合按经核验的原件出处在画像中合并 Analysis 与 Report 的同一观察。同值的独立原件位置继续分别保留。


### Report 和 Record 固定材料消费接口

当前显式工厂提供 `runtime.consumption.consume_prepared`。应用方提交已保存的 `DocumentPreparedInput`、同一主体的正整数生命周期和固定请求 ID。Report 可同时提交以该材料开头的固定投影。Record 提交 Analysis 的固定材料及其直接贡献，不自行选择来源账本的前驱。Report 的续页更正可提交明确的原活动贡献 ID；同时更正主文档解释轮次时，须先应用主文档的固定轮次，再对仍活动的 Report 贡献执行显式替换。

同一来源首次受理固定计划，两个消费者重复提交同一轮复用已存在的来源及发布版本。可直接追加 Report 投影时沿用同一发布账本。补充或更正固定材料复用当前受理、临床依赖和退休规则。新更正要求前驱已应用，短暂未达到时返回 `DocumentBusyError`，可以按原输入重试。消费者固定原请求、输入及生命周期，每个前驱下的候选单独保存。前驱推进时，只有尚未在发布账本受理的候选可以按原请求重新协调。已进入来源 CAS 或持久外发记录的发布、以及已受理的无变化步骤都按原命令重放。Report 的 Analysis 步骤完成后，按固定主材料和最新已接受的主解释轮次继续，通过当前临床依赖与退休校验再追加或复用同一投影。整个来源的发布版本可以因其他贡献推进。重启沿用已受理的步骤和固定的同轮复用凭证。初次提交允许已持久受理后返回。无可发布贡献时固定材料仍可读取，返回 `no_contribution`。未提交归档不写入画像。

### 跨来源原件观察对照

默认组合根设置 `verify_original_occurrences=True`，Document 使用 `report-understanding-current.v3`。准备材料仍来自同一 Analysis 固定轮次。独立 Analysis 消费使用同一材料和生命周期的单件投影核验原件位置，该投影只作证据。Report 消费可复用与主件完全一致的单件投影，多页贡献则保留每一行实际所属的文档。恢复优先读取固定投影和指针。

Document 比对原件文档、字节摘要及固定 Analysis 指针，逐条携带原件位置、解析摘要和固定投影引用。已证实对应的观察以同一原件行的项目、值、单位、标本、方法和有效日期进入画像，保留各自固定指针及证据。日期无法核验或未建立原件指针对照的观察继续按各自贡献处理。不同原件位置的同值项目分别保留。Profile 按同一主体及生命周期应用共用事实，并按各来源独立撤回。

默认离线组合覆盖 Analysis 与 Report 的两种提交顺序、并发和分次撤回。固定 v2 投影和已提交贡献按自身证明读取。显式注入的 Document 工厂根据实际配置固定修订，按 v3 接受的新发布使用原件身份。

### 默认装配与验收

默认组合根将 Analysis、Report 和 Record 接到同一个当前 Document 来源图。Analysis 使用应用注册的当前解读能力，注入 Record 消费端口或 Profile Worker 时仍可使用该能力。Report 与 Record 通过公开端口消费固定材料，Profile 通过当前接收器处理贡献。既有固定存储、发布账本及业务快照保留各自的读写和恢复职责。

预切换阶段的依赖补齐、旧命令排空和装配清理顺序保存在 I-048。真实模型、MongoDB、回调及部署仍需验收。[I-048](../../ISSUE.md#i-048-双来源基础信息与共享profile交接)
