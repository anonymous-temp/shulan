# Document 输出版本选择与兼容缓存

> 更新日期：2026-11-09。本文说明 Document 提供方的内部接口；消费者显式接入须在提供方先按本任务约定集成 refactor/agent 后单独交付，不表示全部多智能体迁移已经完成。

## 选择契约

`modules.analysis.contracts.OutputSelection(profile_name, contract_version, implementation_revision)` 使用完整、非空字符串指定输出。`contract_version` 表示输出契约，`implementation_revision` 对应注册实现的不可变修订标签。当前不自动计算 Prompt、模型配置或代码指纹；实现、提示词或相关配置改变时，提供方必须登记新的实现修订，并保留仍需执行的旧实现。

```python
from modules.analysis.contracts import OutputSelection

selection = OutputSelection('report_source', '1', 'report-source-v16')
result = await document_service.process_document(
    user_id=user_id,
    document_id=document_id,
    output_selection=selection,
)
```

`process_documents(files, ..., output_selection=selection)` 支持同样的选择，保留文件顺序、重复引用与逐文件使用记录。`output_selection` 是内部 Python 参数，不新增 HTTP 字段；`process()` 的公开返回仍只使用原 `report`。同时传入 `output_profile` 时，其名称必须与选择一致。不支持版本抛出 `UnsupportedOutputVersionError`（`code=document.output_version_unsupported`、`retryable=False`），在下载、解析和模型执行前拒绝，不自动使用 latest。

`ProcessedHealthReport.output_selection` 记录扩展输出的实际选择。`report` 继续表示原共享解读；`structured_data` 才是所选扩展输出。默认 health_report 路径保持原调用与历史读取语义，不额外执行模型。显式 health_report 仅支持当前服务装配的解释器修订：已有匹配快照直接复用，历史输入版本不同时生成独立输出并保留原快照；旧解释器未装配时明确拒绝，不承诺可以运行任意历史版本。

## 注册与旧别名

`OutputProfile(name, version, extractor, needs_bytes=False, contract_version='1')` 保留原位置参数，`version` 仍是实现修订。`DocumentOutputProfiles` 按完整选择注册，完全相同的键再次注册会拒绝；不同契约或实现可以并存。既有 `register_report_source(extractor, version=...)` 默认建立旧名称别名，新增并存版本必须显式使用 `legacy=False`。

```python
profiles.register_report_source(old_extractor, version='report-source-v16')
profiles.register_report_source(new_extractor, version='report-source-v17', legacy=False)
# get('report_source') continues to select report-source-v16.
profile = profiles.get(OutputSelection('report_source', '1', 'report-source-v17'))
```

旧别名建立后不可重绑，冲突抛出 `OutputRegistrationConflictError`，且失败注册不留下新键。部署装配必须继续显式保留原别名绑定，不能把新实现作为旧别名的首次注册来规避兼容策略。当前 Report factory 无需修改，仍可使用旧 `.get('report_source')` 与 `.version` 校验。`health_report` 仍为保留名称，由服务持有的解释器实现，不注册为业务 extractor。所有 report_source 版本仍经过现有输出规范化与质量检查；新增版本键本身不会放宽字段、出处或画像发布规则。

## 实际输入与缓存

`ProcessedHealthReport.input_snapshot` 返回 `DocumentInputSnapshot`：解析归档引用、原共享解读归档引用、实际解释器修订、验证修订及结果轮次。值从已保存文档索引读取，历史缺失版本为 None，不以当前部署版本补全。extractor 的 `DocumentSnapshot.input_snapshot` 指向这份原始共享输入；若启用类型提示而另行解读，输出缓存另存 `interpretationExecutionVersion`，与原输入的生成版本分开。

新输出缓存身份包含 userId/subjectId、有序文档及来源修订、完整输出选择、原输入归档与实际版本、内容摘要、输入 report 摘要、类型提示及额外解读执行版本。索引读取继续校验用户和主体；内容摘要不能代表访问授权。解释器升级不会把旧完整快照标成新版本，也不会仅因部署常量变化而重新生成其 Report 投影。

旧 v1 缓存只在契约、旧别名、有序文档与来源修订能证明匹配时兼容读取，沿用原 extractionId、来源修订和摄取任务。无类型提示的 report_source 提取不会执行解释器，因此按授权范围、固定 extractor 和这些精确输入查找旧缓存，忽略旧键误记的部署解释器版本，兼容实际输入版本混合的组合；存在多个匹配旧输出时明确报 document.output_cache_conflict，要求显式核对，不选择 latest。带类型提示的旧输出仍严格匹配实际执行版本。新缓存格式保留可验证的旧发布键；仅增加缓存元数据不新增逻辑发布。无法证实的旧缓存不会被当作当前快照，仍保留原记录。没有旧缓存时，混合输入版本按各自真实快照生成新缓存，不伪造一个共同解释器版本。

恢复使用精确注册的实现，失败后已保存的扩展结果继续复用，画像发布失败不重新执行 extractor。每次新的类型提示请求沿既有政策选择当时装配的解释器修订，并写入该输出任务；执行前再次检查这个固定修订，版本不可用时明确失败。新请求可按升级后的验证器恢复 partial，已保存任务不能在原键下偷换解释器。已完成缓存的读取不需要重新执行解释器，但请求的输出契约仍须受支持。现有已知 DocMind taskId 恢复、租约、partial/unavailable 传播、来源校验与摄取补偿保持原语义。

## 本批边界与验证

I-031 批次实现版本选择和真实输入缓存身份；后续 I-034 提供的不可变材料/事实、独立投影入口和显式 Publisher 见 [Document 材料与显式发布](document-facts-publication.md)。旧 sourceRevision 继续保持原来源标识规则，单调 publicationRevision 使用独立账本，不混用两个序列。现有默认 health_report 的恢复策略保持兼容，新的输出选择不等于公共材料/事实版本已经可独立选择。

离线回归位于 `tests/unit/services/analysis/test_output_selection.py`，覆盖同名版本并存、别名冲突、未支持版本提前拒绝、新旧契约缓存隔离、部署升级下的缓存命中/未命中、历史版本未知、旧缓存沿用、固定版本恢复及组合文件。现有 Document 工作流、Record 引用、Report 质量隔离和发布测试共同检查兼容性；真实模型、MongoDB 多进程和部署切换仍需单独验收。

## Report 消费者接入

Report 的文档适配器现固定选择 `OutputSelection("report_source", "1", "report-source-v16")`，单文件及组合处理均携带该内部选择；不改变 HTTP 请求参数、解释内容、原件校验或降级规则。注册同名新投影时不会移动旧字符串入口的固定别名，Record 继续使用自身输入契约。当前 Document 缓存及任务执行清单记录真实输入版本与所选实现；不支持的组合明确失败，不以新实现冒充旧版本。

本步骤固定的是文档消费适配器和 Document 任务，尚未为现有 Report 主任务追补任意历史执行版本。主任务版本迁移和中断恢复仍沿用现有封口策略，后续 Agent 受理封装再登记完整域执行清单。
