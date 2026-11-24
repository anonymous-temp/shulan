# 问卷因素分析临时试验

状态：独立试验，未接入生产问卷路由、画像或报告任务。此工具用于将同一份规范结果渲染为临床中文、MeSH 关系分类和参考 SNOMED CT 语义的关系视图；不是三套独立病因算法，也不宣称完成 SNOMED CT 编码。

设计依据：[循证分析流程](../../docs/architecture/record-questionnaire-evidence-workflow.md)、[因素术语规范](../../docs/architecture/record-questionnaire-factor-terminology.md)。

## 输入及来源

- 输入报告解读 JSON、发布问卷 JSON、回答 XLSX。原文件只读；选择具体工作表和回答行，默认第一张表第二行。
- 保留全部报告观测和子报告 expansion，不沿用生产选题的前 16 条投影。解读 JSON 并非原始体检文件，AI 摘要及题目承接不进入个人事实。
- XLSX 按带编号的题干列读取，保留原题、原答和单元格定位；不读取 IP 等非题目列。空值保持未知，公式拒绝。
- 题干唯一匹配仅生成提示，不证明发布版本一致；多处匹配或题干不同均不猜测、不按序号绑定。原问卷没有候选映射时，本试验只能作回顾分析。
- 每次输入保留文件哈希和总体指纹，分析不能套用到另一组文件。报告时间和回答时间分开保存。

## 离线准备与回放

使用项目 shulan Python 环境，从仓库根运行。按本次用户要求，案例文件保存到项目根目录下 `api_test/out/record-factor-trial/20261106/`，该目录已加入 `.gitignore`。也支持明确授权的仓库外目录；其他仓库内目录拒绝写入，不能将健康资料放进 fixture 或 Git。

```powershell
python -m tests.unit.tools.questionnaire_factor_case --report-json REPORT.json --questionnaire-json QUESTIONNAIRE.json --answers-xlsx ANSWERS.xlsx --output-dir APPROVED_OUTPUT/input --prepare
python -m tests.unit.tools.questionnaire_factor_case --report-json REPORT.json --questionnaire-json QUESTIONNAIRE.json --answers-xlsx ANSWERS.xlsx --analysis REVIEWED_ANALYSIS.json --knowledge SOURCE_EXCERPTS.json --output-dir APPROVED_OUTPUT/replay
```

回放分析必须遵循真实入口中的 `ANALYSIS_CONTRACT`，并绑定准备所得的 `inputFingerprint`。知识文件使用 `sources` 数组，条目至少包括 `id/title/url/sourceText/provenance`；原文片段类型为 `public_source_excerpt` 或 `evimed_original_text`。保留出处、取得方式和日期；人工/助手提供的分析不冒充生产模型运行结果。回放只是校验和展示给定结果，不执行医学推断。

输出为 `result.json`、`comparison.md` 和白底 `comparison.html`，存在同名文件时拒绝覆盖。JSON 包含运行方式、完整证据、规范分析与三种视图，属于敏感健康资料，应按输入资料同等管理。

## 完整证据链审阅版（v2）

为了审阅具体结论，回放支持可选的 `analysis.review` 扩展。基础 `factors` 保持唯一的最终判断，扩展中不得重新生成另一份不一致的结论。此结构是项目试验设计，不是医学标准、正式生产接口或已完成的 SNOMED CT 编码。

| 字段 | 用途与约束 |
| --- | --- |
| `case.reportSourceAvailability` | 声明输入仅为解读 JSON、未核验原体检文件；不生成原报告页码或“已核验原文”标记。 |
| `case.historicalInterpretations` | 单独保留报告 AI 摘要和旧问卷 description，带来源及 JSON pointer；不进入 `case.evidence`，不能作为个人事实引用。 |
| `review.targetProblems` | 具体指标、报告诊断或自述症状，引用个人记录；旧风险解读引用和本次风险解读分开。 |
| `review.chains[].initialCandidate` | 回顾重建的候选、形成依据、报告引用；医学资料同时保留短原文、含义、适用范围和局限。 |
| `review.chains[].conditions` | 每个待核实条件的初始、最终状态和证据。风险因素存在不等于个体病因成立。 |
| `review.chains[].answerEffects` | 通过 Excel 单元格定位实际题干与原答，说明支持、减弱、冲突或未改变哪个条件；不得冒充原发布问卷的确定映射。 |
| `review.chains[].finalAssessment` | 最终结论、已确认内容、尚未成立内容、下一步核实；结论必须与对应 `factor.interpretation` 一致。 |
| `reviewProjection` | 程序从上述引用展开的客户端阅读副本，包含完整记录、问题、回答和来源元数据；HTML/Markdown 使用同一数据生成。 |

现有真实调用两次都看到完整 case，包括答案。因此扩展的时间性质由程序固定为 `retrospective_reconstruction`，`historicalPreAnswerRunAvailable=false`；不允许模型声称曾经执行了答题前候选生成。本次老板审阅样例为助手依据公开资料整理后本地回放，不冒充真实模型或私有知识库运行。未来要验证前瞻流程，须先单独冻结报告侧候选，再接收答案；不在本次临时展示中伪造这一历史。

审阅页面默认展示“具体问题与记录 → 循证候选 → 问卷影响 → 最终结论”四层，引用可定位到报告 JSON、工作表单元格和公开来源章节。来源种类、适用限制与反向结果一并展示；未完成正式证据分级时明确保留 `ungraded`，不生成无依据的可靠性分数。底层校验仅保证引用、结构和状态一致性，医学适用性仍待专业审阅。

保留旧结果目录，在新目录生成 v2；新增来源元数据会改变 case 指纹，不把旧分析指纹套到新 case。历史文件不迁移或覆盖。当前真实模型入口仍使用基础分析契约，不自动生成审阅扩展；可用真实输出进一步补齐扩展并回放验证，但不能将此样例作为已完成生产自动化的证明。

## 真实检索及模型调用

仅从显式选定的进程环境变量读取密钥，不读取任何 `.env*`。独立入口使用项目同款 AION SDK 和 EviMed 协议，不导入会加载应用配置的 `agents` 或 `utils.knowledge`，不启动应用服务或连接数据库。

```powershell
python -m tests.api.scripts.questionnaire_factor_case --report-json REPORT.json --questionnaire-json QUESTIONNAIRE.json --answers-xlsx ANSWERS.xlsx --provider PROVIDER --model MODEL --output-dir APPROVED_OUTPUT/live
```

默认密钥变量为 `SHULAN_LLM_API_KEY`、`SHULAN_KNOWLEDGE_API_KEY`，可用 `--llm-key-env`、`--knowledge-key-env` 指定；值不得写入命令、源码或输出。`--knowledge-base-url` 和 `--llm-base-url` 仅用于显式选择服务。此次不新增环境模板配置项。

调用过程：第一次模型调用基于完整资料形成通用医学检索式；最多三次指南/文献检索（可显式配置为一至五次），要求原文而非 AI 摘要；第二次模型调用依据原文、个人回答和受控试验词表形成候选判断；本地校验后生成三个一致的视图。每次模型调用默认最多 8000 输出 token、180 秒超时，不自动追加模型修复调用。生成后的医学语义仍须人工审核。

密钥缺失、服务错误、无原文、引用无效或结构不符均不产生成功报告。若 SDK 提供结束原因并明确标识截断，拒绝发布；当前安装版 AION 不透出结束原因时，运行记录明确保存 `finishReasonAvailable=false`，不能宣称已验证未截断。可解析 JSON 也不证明语义内容完整。上游服务可能自带传输重试；模型调用计数是逻辑调用次数，不能据此宣称计费请求次数的硬上限。真实入口会将报告和回答证据发送到所选模型，将通用检索式发送到知识服务，运行前需确认相应数据处理授权。

## 校验能力及边界

- 校验输入指纹、受控概念、关系枚举、个人状态、目标风险、来源 ID 和逐字原文片段；不允许直接因果类型。
- 未知、冲突、否认不能变成支持；空回答不能证明存在或否认。缺少反证不代表排除其他解释。
- 校验引用存在不能证明该引用支持医学结论；当前没有自动诊断正确性或个体因果性验证器。词表为试验设计，不是官方医学标准或已审批知识库。
- 题干包含“或”、不同时间的习惯、家族主体、不同测量值等语义约束须结合真实模型结果审阅，不能以格式测试通过代替。
- 临床结果不承诺每个异常都能找到确定病因。MeSH 分类仅约束关系类型；SNOMED CT 视图的 `code` 固定为空，避免伪造标准映射。

## 验证命令

```powershell
python -m pytest tests/unit/tools/test_questionnaire_factor_case.py tests/unit/tools/test_questionnaire_factor_review.py
python -m pytest
python -m ruff check tests/unit/_support/harnesses/questionnaire_factor_case.py tests/unit/tools/questionnaire_factor_case.py tests/unit/tools/test_questionnaire_factor_case.py tests/api/scripts/questionnaire_factor_case.py
python -m ruff format --check tests/unit/_support/harnesses/questionnaire_factor_case.py tests/unit/tools/questionnaire_factor_case.py tests/unit/tools/test_questionnaire_factor_case.py tests/api/scripts/questionnaire_factor_case.py
```

自动测试仅使用合成资料。生产接入时仍需将同一流程接到已有网关与不可变提交快照，补齐医学目录审核、版本治理和任务生命周期；本试验不修改这些生产行为。
