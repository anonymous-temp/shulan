# Report 独立文档投影与缓存

状态：消费者契约已实现并完成离线验证，生产装配尚未切换。当前 HTTP 流程继续使用 `SharedReportDocuments` 兼容路径。本文不表示 Document 公共材料读取、正式来源发布和 Report 新路径已经完成共同验收。

## 模块边界

Document 拥有原件、解析材料、事实及不可变理解快照。Report 通过公开快照构造 `ReportSourceInput`，拥有 `ReportProjectionService`、实际 `report-source-v16` 提取实现以及本域缓存。升级 Report 投影不修改 Record 输入；读取投影和影子执行均不发布 Profile 事实。

`ReportSourceInput.from_bundle` 要求固定的 `DocumentPreparedBundle` 与该材料的原件，校验原件 SHA-256 和长度，并固定材料、事实、理解引用与输入选择。取用字典返回副本。投影受理前固定附件顺序和完整输入清单，拒绝跨用户或跨主体组合。正常执行提取时按首次 documentId 去重，与旧 `process_documents` 保持一致；含原文降级的组合沿用旧专用路径，保留重复附件顺序，并对整组只使用原文和原件，不混入成功附件的模型解读。受理与缓存绑定仍保留完整有序清单。同一 documentId 对应冲突的固定版本或内容时明确拒绝，不查当前索引挑选最新值。

`OutputSelection('report_source', '1', 'report-source-v16')` 显式选择当前真实实现。缓存键由完整输入清单和输出选择组成；同名新版本必须登记真实实现，不能在缓存未命中时回落到新实现。版本不受支持时，即使旧缓存存在也明确失败。缓存校验输入绑定与输出摘要；重复写入只接受相同内容。

## 质量与兼容行为

Report 自己归一化 `subject`、`sample`、指标、菌群、疾病结果和补充检查等输出，并保持附件顺序、原件摘要及原有 `partial/unavailable` 质量语义。当前投影与旧 `report_source` 归一化在离线基线中对照，未增加模型调用。

失败材料读取由 Document 公共模块负责；是否采用原件回退由 Report 决定。只有材料明确绑定 `analysis-health-report-v1` 至 `v5` 的失败调用、解析成功且文本非空时，才允许旧回退。缺少真实版本或 v6 及以上输入明确拒绝。旧回退不能与隔离中的 partial/unavailable 输入组合，以免重新从原文引入被排除的观察值。

本域投影结果不等于经过可信来源过滤的最终报告输入。实际接入仍须复用 `trusted_report_source`、原件表格核验和报告解析器；不得直接把原始投影作为可发布的临床结论。

## 当前原件投影

`project_current_report_source` 接收有序的 `(DocumentCurrentBundle, bytes)`，返回实际 v16 选择和来源输出。适配修订 `report-understanding-current.v2` 按固定临床章节识别根镜像及影像显示汇总。候选须匹配同一原件中的结果列、检查结论或影像字段。建议与参考栏不作为结果依据。原生 PDF 读取关闭 OCR。

显式 `project_current_report_source_with_observation_refs` 对应候选修订 `report-understanding-current.v3`。它在唯一核验物理位置后附上固定 Analysis 指针。表格呈现优先级不用于证明指针归属。同值独立位置分别保留，固定镜像路径只有在同一原件位置得到证明时才随附。当前 Document 工厂继续执行 v2。后续切换须完成并校验所有已受理 v2 命令、回执和快照。

核验通过的结果可保留原件位置、标本和方法，同值的独立出处分别保留。`complete` 描述处理质量，Document 后续正式发布还需绑定固定投影、执行回执和原件出处。既有 HTTP 和 v16 调用继续使用各自现行路径。

## 待完成的生产接入

1. 同步独立交付的 Document 材料读取能力，经公开 API 按固定材料引用取原件；不得读取可变索引重建旧输入。
2. 通过独立兼容 Publisher 保留旧正式 `reportSource` 来源效果。新 Report 私有投影不能直接作为 Document 公共事实；正式发布切换必须共享去重、撤回和修订水位。
3. Report Adapter 接入本域投影与缓存，并在公共装配中提供专属 Mongo 集合；生产要求 `_id` 唯一、主节点读取、确认写入及与任务引用一致的保留周期，不设置自动 TTL。
4. 完成真实公共 Gateway 组合测试、正常与回退链的行为等价、恢复固定版本验证后再切换。旧任务继续按原执行版本处理，不因新投影重新生成已完成报告。

## 验证边界

单元测试覆盖真实 v16 对照、新旧实现并存、缓存命中与重启、版本缺失、顺序冻结、跨主体拒绝、原件及缓存损坏、质量状态、旧回退资格、重复写入冲突和未确认写入。Mongo 并发、真实材料保留、模型质量和外部回调尚需单独联调；离线测试不替代这些验收。
