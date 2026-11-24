# Document 菌群原件规则固定版本

`adapters.analysis.microbiome_source` 为通用文档解释器提供原件事实校正。原实现直接调用 Report 私有 PDF 解析、来源记录和上下文规则，导致 Report 升级可能改变同一个 `analysis-health-report-v12` 的冷缓存结果。现在这些实际依赖已静态冻结为 Document 自有 `document-microbiome-source-v1`；Record、Analysis 继续经过现有通用解释器消费该入口。

## 实现边界

固定叶子实现位于 `modules/analysis/application/_microbiome_v1/`，从 `efca880` 的实际算法提取，包含 95 个需要的函数、类型与常量：原生 PDF 几何/表格读取、指标和风险卡、菌名与分级、日期、来源上下文，以及必要的内部 Pydantic 模型。提取后逐项核对 AST，除内部导入路径外与原实现相同；没有复制 Report 请求流程、发布器、模型调用或输出流水线。

顶层与函数内部的依赖均指向 Document 固定包，包括菌群 occurrence 读取器内部对表格读取算法的导入。这里没有别名转发到当前 Report 实现，也不依赖 Report 的 schema 默认值。Report 可以独立修改其解析与投影；需要 Document 支持新规则时，应由 Document 独立迭代并明确选择版本。

`read_microbiome_source_facts` 的 `implementation_revision` 缺省固定为 `document-microbiome-source-v1`，未知修订在读取原件前明确拒绝。现有调用无须增加参数。当前解释器版本、缓存身份、Prompt、输入正文、质量与错误语义保持不变；此改动不改变 HTTP 或扩大来源发布范围。

原件读取继续明确使用 `ocr=False`。输入不是 PDF、文件无法解析或不能证明属于明确菌群报告时，仍返回无可用原件校正，由现有解释器保留原路径。数字正负号、原始日期、印刷分级与区间分级、候选基因与检出基因仍保持各自证据，不能因本次拆分提升为新的患者事实。

## 离线验证

`tests/unit/services/analysis/test_microbiome_policy.py` 使用与既有 Report 回归相同的 PyMuPDF 内置中文字体和表格/文本样例，验证原生 PDF、真实解释器冷路径、来源定位、风险与菌群行、日期歧义、禁止 OCR、未知版本拒绝，以及 Report 函数/schema 被替换后的完整输出稳定性。独立子进程在拒绝加载 Report 模块的条件下执行 Document 解析，覆盖函数内部动态依赖。

既有 `test_native_source_merge.py` 继续验证原件校正、覆盖率、质量和模型正文不变。测试不下载文件、不调用模型、不连接真实数据库；真实扫描件及第三方 PDF 库升级仍须独立验收。
