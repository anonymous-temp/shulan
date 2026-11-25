# 评估报告六章（assessment.v2）内容规格

本文是评估报告（`type=evaluate`，回调 `data.version = "assessment.v2"`）的内容规格。章节结构、表格列、分析思路以甲方 2026-11-21 提供的示例报告（48 岁男性一例，30 页）为准；文风按本文执行，保证对所有受检者成立。

本文是验收依据：代码、分析方法说明、回调字段和样版渲染都按本文核对。对外字段契约仍以 [docs/api/API.md](../docs/api/API.md) 为准。

## 1. 总原则

### 1.1 分工

报告由模型驱动：甲方模板定骨架，模型看完整份资料后做判断并成文，程序只整理事实、核对引用与数值、兜住安全底线。

| 层 | 负责 | 说明 |
|---|---|---|
| 骨架 | 模板 | 六章、各节、各表格的列与回调字段固定，对应甲方示例。行数、系统数、集群数、主诉数随每位受检者的资料变化 |
| 事实 | 程序 | 把体检与检验、体检结论、问卷与自述、菌群检测整理成一张带编号的事实表；派生指标按公式计算；每条记录的展示字段（数值、参考范围、画图字段）由程序给出 |
| 分析 | 模型 | 一次读完整张事实表，决定：表型、各系统的状态与表格行、理想目标、主诉、失衡集群及优先级、优先排查事项、常规随访、检测盲区、生活诱因、认知校准 |
| 成文 | 模型 | 按分析结果分节写正文，各节并行 |
| 复核 | 模型 + 程序 | 独立复核对照事实表逐格检查并改正；程序校验引用编号、标出事实表中找不到的数字、清除内部标记 |
| 安全网 | 程序 | 危险信号规则在模型判断之后补漏：模型漏列的危险发现补为优先排查事项并记入审计；不删除、不降级模型给出的事项 |
| 呈现 | 甲方前端 | 按回调里的结构化字段渲染。本仓库提供样版渲染（`scripts/render_assessment_presentation.py`）作为参照 |

规则表不再决定报告内容。一个发现算不算优先排查、哪些指标放在同一行、定什么状态、建议补查什么，都由模型根据这位受检者的资料判断，这样没见过的指标、别的医院的写法、别的菌群检测机构都不需要先补规则。

### 1.2 文风（所有读者可见文字）

- 正式书面语。不用“您”；确需指代写“受检者”，多数句子省略主语。
- 不写免责声明、推销句、比喻、夸张和感叹号，不写药物与补充剂的名称和剂量，不承诺疗效。
- 不出现后台与来源方用语：原件、原报告、主检、模板、问卷显示、体检显示、检测方、检测机构；需要交代出处时写检查名称或“体检结论”“检查建议”。
- 按证据强度选措辞：检查直接显示或指南明确判定的直接陈述；有研究支持的关联用“提示”“符合”“常见于”；机制推断（包括由菌群功能预测推断营养状态）用“可能”“是一条线索”，并点出可用什么检查验证。限定语要克制，一格最多一处；“菌群功能评分是预测潜能而非体内实测”只在第四章开头说明一次。
- 每格都要有信息量：偏离了多少、意味着什么、还好的一面，至少写清两件。只复述数值不算解读。
- 理想目标直接写数值，不加“指南”“功能医学参考”之类的标注；引用标准时写机构或指南简称，不写年份。
- 正文不出现事实表编号和内部字段名。

### 1.3 对所有用户成立

- 每一节规定“资料缺失时的写法”，不留空节、不编造。没有问卷时不写主诉与生活诱因；没有体检时第一章体格与第三章只写说明；没有菌群时第四章只写说明、集群只用临床证据。
- 数量随资料：主诉、集群、随访、盲区、生活诱因都不固定条数。基本正常的受检者如实写正常，集群可以为零。
- 系统以五个为基础，受检者在这五个系统之外有重要发现（肺、乳腺、妇科、泌尿生殖等）时由模型增加一个系统并命名。
- 性别、年龄相关的内容由模型按受检者情况取舍。孕期与未成年人目前没有专门处理。

### 1.4 状态分档（全报告统一）

读者看到五档状态；回调的 `level` 仍是原有四个取值，两者的对应如下。

| 显示（`status`） | `level` | 含义 |
|---|---|---|
| 重点关注 | `focus` | 超出临床参考范围且有临床意义，或影像、定性结果明确异常，或达到指南的诊断切点 |
| 亚临床边界 | `borderline` | 处在临床范围边缘、轻度偏离，或需要随访的良性所见 |
| 高于理想值 / 低于理想值 | `borderline` | 在临床范围内，但偏离理想目标 |
| 稳健达标 | `normal` | 临床范围和理想目标都达到 |
| 未检测 | `notTested` | 本次资料没有该系统的检查 |

只偏离理想目标而没有超出临床范围或指南切点的，不定为重点关注。程序只做一条硬约束：检验结果远超化验单参考范围的记录，其所在的行不得显示为稳健达标（改为亚临床边界并记入审计）。

## 2. 各章内容

“模型”指由整案分析决定或由分节成文写出，“程序”指确定性生成。

### 封面与前言

- 封面（程序）：姓名、性别、年龄、档案编码；临床体检日期；菌群采样日期（缺失时用收样日期并注明）；两类资料的间隔。
- 前言与阅读指引（固定文字）：报告定位、双轨评价、结构导览。

### 第一章 全息健康图景概览

| 节 | 内容 | 决定者 | 回调字段（`chapters[code=overview].content`） |
|---|---|---|---|
| 1.1 基础体格与人体成分 | 一句表型判断加说明；表格：评估指标、实测数值、参考基准 / 理想目标、状态、生理与代谢意义 | 行的划分、理想目标、状态、文字：模型；每行的实测值：程序 | `bodySummary`、`phenotype`、`bodyGroups[]`（`name`、`rows`、`comparisonReference`、`status`、`level`、`summary`） |
| 1.2 多系统健康状态分级总览 | 表格：系统、综合状态、核心异常与关键实测值、当前阶段评估结论 | 模型 | `summary`、`systems[]`（`name`、`status`、`level`、`keyFindings`、`summary`、`anchors`） |
| 1.3 临床优先排查项与未检风险提示 | 优先排查（时限、科室、要做的检查、完整推理）；常规专科随访；检测盲区（逐项对应受检者的发现） | 模型；危险信号规则补漏 | `priorityFindings[]`（`urgent`、`routine`、`gaps`） |
| 1.4 核心系统失衡态势速览 | 集群清单与一句关系说明 | 模型 | `graphHub`、`graphClusterIds`、`graphSummary` |

资料缺失时：没有体格资料，`bodySummary` 写“本次资料未包含体格测量”；没有任何检查的系统 `level` 为 `notTested`；没有优先排查事项时 `urgent` 写“本次未发现需优先就医排查的结果”；没有检测盲区时不返回 `gaps`。

### 第二章 核心主诉表型与多维客观指标关联解析

- 2.1 表格：主诉、持续时间与加重情境、临床检验与影像关联信号、菌群与功能代谢关联证据。字段 `symptoms[]`（`name`、`history`、`clinicalEvidence`、`microbiomeEvidence`、`clinicalSignals`、`microbiomeSignals`）。
- 2.2 每个主诉一个名词性标题和一段成因解析（`interpretationTitle`、`interpretation`），把检验数值、生理机制、菌群证据和生活因素连成一条线；最后“综合印证”（`symptomSummary`）。
- 主诉只取受检者本人在问卷或自述里确认的不适或希望改善的方面，不从检查结果反推。没有问卷或受检者表示没有不适时，写“本次未记录主诉症状”。
- 两栏证据只在整案分析为该主诉引用了相应记录时才写；没有可引用的记录时字段为空，不写“未见相关证据”一类的句子。请求里没有菌群报告时，各主诉都没有 `microbiomeEvidence`，样版不画菌群一栏。

### 第三章 分系统临床检验与亚临床功能双轨评估

- 每个系统一节（`clinicalGroups[]`）。表格一行回答一个临床问题，相关指标放在同一行：`templateRows[]` 的 `name`（项目）、`rows`（实测结果与临床参考范围）、`comparisonReference`（理想目标）、`status`、`summary`（解读）。表后有小结（`summary`）；体检结论的文字与检验数值矛盾、或同一指标多个读数不一致时，有“数据校准说明”（`calibrationNote`）。
- 行的划分、理想目标、状态和解读由模型决定；每行的实测记录由程序按引用编号取出。程序保证每条异常记录都进入某一行：模型没有提到的异常记录由程序补成单独一行（记入审计）。
- 3.5 本次未纳入的专项检测（`specialtySummary`）；没有被任何行引用的设备检测记录保留在 `specialtyFindings`。
- 3.6 趋势或基线说明（`trendSummary`、`trends`）；已确认的稳健基线（`baselineSummary`、`normalBaselines`）。
- 没有临床资料时，`summary` 写明本章不作分系统评估。

### 第四章 肠道微生态宏基因组与消化微环境深度评估

- 表格的行按菌群检测报告的分组呈现（程序）：4.1 消化道环节（`digestiveContext`）、4.2 宏观指数（`structureGroups`）、4.3 菌群结构（`organismGroups`，异常项与正常项并列）、4.4 功能潜能（`functionalGroups`）。
- 各组的解读由模型写入 `summary`：4.1 写对菌群定植与消化吸收的影响，4.3 写功能影响与调节意义，4.4 写与临床指标及不适的交叉印证。章首 `overviewSummary`、`sampleSummary`、`fermentationSummary`，4.2 后 `structureSummary`，章末 `riskSummary`（模型的说明加检测报告的模型指数清单）。
- 没有菌群资料时，`overviewSummary` 写明本章不作菌群评估，本章其余字段为空，`digestiveContext` 为空数组（消化道相关检查已在第三章评估）；有菌群资料而没有消化道相关检查时，`digestiveContext` 只有一条说明。

### 第五章 系统失衡集群综合评估与生活诱因溯源

- 5.1 每个集群：名称（机制链写法）、优先级（P0 优先医疗排查 / P1 核心干预 / P2 协同调理）、临床与影像证据、菌群与代谢证据、对身体的影响与走向、处理方向。字段 `clusters[]`（`id`、`name`、`priority`、`clinicalEvidence`、`microbiomeEvidence`、`interpretation`、`action`、`clinicalSignals`、`microbiomeSignals`）。有优先排查事项的集群为 P0。两类证据文字的取舍与第二章相同：没有可引用的记录时字段为空。
- 5.2 先天背景、既往事件与保护因素（`lifestyleSummary`）；生活诱因表（`lifestyleFactors[]`：维度 `name`、行为特征 `value`、正在维持的异常 `interpretation`、对应集群 `clusterIds`）；认知校准（`lifestyleRecognition[]`：先写受检者的看法或体检结论里的说法，再用数据说明实际情况，最后给一个小改变）。
- 没有集群时 `clusterSummary` 写“本次未形成需集中干预的失衡集群”。

### 第六章 综合健康评估结论与精准干预方向建议

- 综合结论（`summary`）；三个层级（`layers[]`：首月、90 天、持续，`clusterIds` 分别对应 P0、P1、P2 集群）。
- 管理方向（`managementDirections[]`）：MTPN（功效菌选择依据、益生元与膳食纤维、营养素方向与需避开的成分，只写方向）和 LSMI（去诱因的生活方式方向）。有优先排查事项时 MTPN 带“先完成第一层级排查，再开始配方干预”；没有菌群资料时 MTPN 只写说明。
- 报告末尾不加免责声明。参考文献附录保留（`appendices.referenceLiterature`）。

## 3. 生成流程（整案模式，默认）

`SHULAN_REPORT_AUTHORING_MODE=case`（默认）。入口 `modules/report/evaluation_report.py::generate_evaluation_report()`。

### 3.1 步骤

| 步骤 | 谁做 | 位置 | 模型任务 |
|---|---|---|---|
| 来源解析、派生指标、旧字段所需的问题整理与评分 | 程序与既有模型步骤 | 与逐章路径共用 | 既有任务 |
| 事实表 | 程序 | `assessment_case_sheet.py` | — |
| 整案分析 | 模型，1 次 | `assessment_case_authoring.py`、`assessment_case_plan.py` | `analyze_assessment_case` |
| 危险信号安全网 | 程序 | `assessment_case_plan.py::apply_priority_safety_net` | — |
| 分节成文 | 模型，各节并行（约 12–15 次） | `assessment_case_authoring.py::write_case_sections` | `write_assessment_case_section` |
| 独立复核与改正 | 模型，按章分组并行（至多 6 次） | `assessment_case_authoring.py::review_case` | `review_assessment_case` |
| 装配回调 | 程序 | `assessment_case_projection.py` | — |

三个任务都以思考模式运行，使用同一个系统提示词 `prompts/report/narrative.txt`；分析方法、写作要求和复核要求在 `modules/report/assessment_case_guides.py`。

### 3.2 事实表

每条记录一个编号：`L` 体检与检验记录，`D` 派生指标（程序按公式算好，附算式与代入值），`A` 体检结论与检查建议，`Q` 问卷与自述（逐题原文），`M` 菌群检测（异常项逐条编号，参考范围内的成员按组列名称与数值）。另列“已核对出处的理想目标”供模型优先采用。体检日期与菌群采样（或收样）日期的间隔由程序算出，并直接写明读法：不超过 6 个月属同一健康周期、两类资料可互相印证，超过 6 个月以较新的资料为准。一例 48 岁男性实例的事实表约 2 万字符。

模型只用编号引用记录。程序据此检查每个引用是否存在，并把该记录的展示行（数值、参考范围、画图字段）装进回调。

### 3.3 分析结果（计划）

`normalize_case_analysis()` 校验模型的分析，得到计划（审计里保留统计，不保留受检者文字）：

| 字段 | 内容 |
|---|---|
| `phenotype` | 表型名称与支撑编号 |
| `dataNotes[]` | 资料核对：日期间隔（`timing`）、读数校准（`calibration`）、缺失的常规检查（`missing`） |
| `bodyRows[]`、`systems[].rows[]` | 表格行：标题、引用编号、理想目标、状态；`origin` 为 `model` 或 `program`（程序补的异常记录行） |
| `systems[]` | 五个默认系统按固定顺序，加至多一个追加系统；`status`、`keyHandles` |
| `symptoms[]` | 主诉（必须引用 `Q` 编号）及相关的临床、菌群编号 |
| `clusters[]` | 名称、优先级、三类证据编号；按 P0、P1、P2 排序后编号 `C1…` |
| `priorityItems[]` | 事项、时限与天数（三档：当日至 3 天内、2 周内、1 个月内）、科室、检查、依据编号、所属集群；`origin` 为 `model`、`safety_net`（规则补漏）或 `review`（复核补漏） |
| `followups[]`、`testGaps[]` | 常规随访（至多 6 项）、检测盲区（至多 6 项，资料里已有的检查会被去掉） |
| `background`、`lifestyle[]`、`protective`、`recognitions[]` | 先天背景与既往事件、五个维度的生活诱因及对应集群、保护因素、认知校准 |
| `audit` | `dropped`（没有有效引用而丢弃的条目）、`corrections`（程序改正）、`programRows`（程序补行）、`safetyNet`（规则补漏记录） |

### 3.4 分节成文

每节一次调用，输入相同的事实表与计划，只写本节的格子，返回 `{"cells": {id: 正文}}`。格子 id 如 `overview.bodySummary`、`clinical.cardiometabolic.row.3`、`clusters.C2.impact`，各有字数上限。主诉与集群的临床证据、菌群证据两格，只在分析为它引用了相应记录时才要求写；数据校准说明只交给引用了相关记录的系统。失败的节重试一次；仍失败时该节的格子缺失，表格行退回为“列出原值”，回调标 `partial_success`（`REPORT_CONTENT_INCOMPLETE`）。

### 3.5 复核

复核按章分组并行，输入事实表、计划、成稿和程序发现的疑点（事实表里找不到的数字、后台用语、超长），返回有问题的格子及改正后的完整文字。检查的问题：与资料不符的数值、方向、归属；资料里没有的症状、病史、习惯；把推测写成定论；算式或切点用错；前后矛盾；应尽快查因的发现被写成先观察；时限、科室、优先级与计划不一致；漏掉的危险发现（返回后补为优先排查事项）。复核失败不影响交付，审计里记录。

### 3.6 程序核验与安全网

- 引用：不存在的编号被丢弃；没有任何有效引用的行、集群被丢弃并记入审计。
- 覆盖：每条异常的体检与检验记录必须出现在第一章或第三章的某一行，否则由程序补行。
- 数字：正文里带小数或三位以上的数字，在事实表和计划里都找不到的，交给复核逐个判断（指南切点和由资料算出的倍数可以保留）。下面两种写法算作能找到：记录值少写了小数位（4.7804 写成 4.78）；同一段文字里引用的两个数之差，或引用的数与该记录参考范围上下限、理想目标之差（7.62 与理想值 5.6 之差 2.02）。
- 内部标记：编号、Markdown 符号、“您”在入库前清除。
- 安全网：`assessment_red_flags.py` 的危险信号规则在模型分析之后运行。规则命中而模型没有列为优先排查的，补成事项（`origin = safety_net`）；模型已列的事项若规则给出更紧的时限，取更紧的时限。规则不删除、不降级模型的事项。

### 3.7 时间预算与回退

- 整案成稿的预算为 780 秒（`CASE_AUTHORING_BUDGET_SECONDS`），其中整案分析约占 45%。
- 整案分析每次最多等 240 秒，至多两次；返回的分析里没有任何模型给出的表格行时按失败处理。
- 整案分析两次都失败时回退到逐章路径（`chapter`，审计 `caseAuthoringFallback = chapter`）。
- 剩余时间不足 600 秒（`CHAPTER_FALLBACK_MIN_SECONDS`）时不再回退，改为交付降级报告（`caseAuthoringFallback = source_only`）：表格列出全部记录的原值，优先排查事项由危险信号规则给出（时限、科室、要做的检查），需要模型分析的小节写“本次未能完成综合分析，本节暂缺；检查记录与需优先处理的事项见第一章和第三章。”回调标 `partial_success`（`REPORT_CONTENT_INCOMPLETE`）。
- 装配整案结果出错时回落到逐章路径的装配，回调同样标 `partial_success`。
- 后续报告读取的三个旧字段（健康状况总览、菌群稳态小结、综合结论）由整案成稿的对应文字写入。旧的问题整理步骤产生的不完整原因只影响旧字段，不使六章报告标为部分成功（记在审计 `formerPathReasons`）。

## 4. 分析方法说明

`modules/report/assessment_case_guides.py`：`ANALYSIS_METHOD`（整案分析的方法）、`WRITING_GUIDE` 与 `SECTION_GUIDES`（写作要求与各节要求）、`REVIEW_GUIDE`（复核要求）、`CASE_GUIDE_VERSION`（版本号，写入审计）。

这些文字说明的是做法，不是逐指标的规则。修改后必须在多画像样例集上重跑并通过第 8 节的检查，再更新版本号。

## 5. 理想目标

- `modules/report/assessment_targets.json` 是已核对出处的理想目标库（由 `scripts/build_assessment_targets.py` 生成并核对出处数值）。适用于受检者的条目列在事实表里，模型优先采用；对应的出处进入参考文献附录。
- 库里没有的指标，由模型按通行的指南切点或常用区间给出，直接显示数值，不加标注。
- 理想目标只用于“高于 / 低于理想值”这一档；定为重点关注需要超出临床范围、达到指南切点或有明确的影像、定性异常。

## 6. 危险信号与检测盲区规则

- `modules/report/assessment_red_flags.py`（危险信号与常规随访规则）在整案模式下作为安全网，见 3.6；命中的规则出处进入参考文献附录。逐章路径仍用它决定优先排查事项。
- `modules/report/assessment_test_gaps.py`（检测盲区规则）在整案模式下不参与生成，只用于评测和逐章路径。
- 两张规则表按成人设计，规则说明与出处可用各自的 `rule_table()` 导出。

## 7. 回调字段

字段名与结构沿用 `assessment.v2`，只追加：

| 对象 | 追加字段 | 说明 |
|---|---|---|
| `overview` | `phenotype`、`dataNotes[]` | 表型名称；资料核对说明（`kind`、`name`、`text`） |
| `bodyGroups[]` | `comparisonReference` | 该行的理想目标 |
| `systems[]` | `keyFindings` | 核心异常与关键实测值的文字 |
| `symptoms[]` | `clinicalEvidence`、`microbiomeEvidence`、`interpretationTitle` | 2.1 两栏证据文字；2.2 的小标题 |
| `clinicalGroups[]` | `status`、`calibrationNote` | 系统状态；数据校准说明 |
| `templateRows[]` | `recordedConclusions`、`anchors`、`moreCount`、`completion` | 该行引用的体检结论；需关注的成员；`completion` 为 `source_only` 表示解读缺失、只列了原值 |
| `clusters[]` | `clinicalEvidence`、`microbiomeEvidence`、`reportedSignals` | 两类证据文字；相关自述 |
| 优先排查事项 | `origin` | `model`、`safety_net`、`review` |

`status` 在体格行、系统、分系统和表格行上是 1.4 的五档之一（或“未检测”）。

## 8. 验收与本地工具

- 多画像样例集：48 岁男性实例（本地问卷与按甲方示例反推的基线问卷两种输入）；62 岁女性多病；29 岁男性基本正常；无问卷；只有菌群；体检全部正常；含危险信号；另一家医院的指标写法与育龄女性；70 岁以上多病；没有菌群报告。
- 每例自动检查（`scripts/check_assessment_report.py RUN_DIR --request REQUEST`）：六章各节非空或有缺失说明；禁用词为 0；异常项全部出现在报告中；带单位的数值能回溯到原始资料（模型写出的指南切点和倍数列出供人工查看）；危险信号规则的每个命中都已列为优先排查；耗时不超过 30 分钟。
- 本地工具：
    - `scripts/generate_evaluation_report.py --checkpoint`：本地实跑，另存装配输入 `presentation_inputs.pkl`。
    - `scripts/run_assessment_case.py RUN_DIR`：从 checkpoint 重跑整案分析、分节成文与复核（不重跑来源解析），写出事实表 `sheet.txt`、`case.json`、回调与样版；`--reuse` 只重放装配。
    - `scripts/replay_assessment_presentation.py RUN_DIR`：不调模型，按当前代码重放装配、回调与样版。

### 8.1 实测（2026-11-25，本机，`deepseek-flash`，3 份同时生成）

| 输入 | 状态 | 总耗时（秒） | 整案成稿（秒） | 整案成稿的模型调用 | 优先排查事项 |
|---|---|---|---|---|---|
| 48 岁男性实例，基线问卷 | `success` | 342 | 244 | 21 | 1 |
| 48 岁男性实例，补充问卷 | `success` | 268 | 182 | 21 | 1 |
| 62 岁女性多病 | `success` | 260 | 186 | 21 | 1 |
| 29 岁男性基本正常 | `success` | 277 | 230 | 20 | 0 |
| 45 岁女性无问卷 | `success` | 200 | 147 | 19 | 0 |
| 38 岁男性只有菌群 | `success` | 187 | 163 | 12 | 0 |
| 33 岁女性全部正常 | `success` | 306 | 237 | 21 | 0 |
| 57 岁男性含危险信号 | `success` | 256 | 189 | 20 | 2 |
| 28 岁女性，另一家医院的写法 | `success` | 254 | 192 | 21 | 1 |
| 72 岁男性多病 | `success` | 238 | 162 | 21 | 2 |
| 62 岁女性没有菌群报告 | `success` | 163 | 118 | 16 | 1 |

十一个输入的自动检查全部通过：空节 0，禁用词 0，异常项遗漏 0，找不到出处的带单位数值 0，危险信号规则的每个命中都已列为优先排查。总耗时包含来源解析、旧字段所需的问题整理与评分。

## 附录：逐章路径（回退）

`SHULAN_REPORT_AUTHORING_MODE=chapter` 是上一版的逐章成稿路径：程序按规则决定内容，模型逐格写作并逐句核对来源。整案模式的整案分析不可用时回退到它。下面两节是它的规划输出契约与流程，保留供维护参考；文中“第 1.3 节”“第 4 节”等指本附录所述路径的行为，危险信号、检测盲区与目标区间在该路径下仍由规则表决定。

### A 规划输出（内容规划结果契约）

内容规划（`modules/report/evaluation_content_plan.py: plan_assessment_content`）的结果挂在 `data._assessment_content_plan`，同一份副本写入审计 `contentPlan.plan`。下游按本节投影第 1.3、5、6 章。所有 `factIds`、`triggeredBy` 都是本次报告的来源事实编号（与集群的 `factIds` 同一套），不是给模型看的 `F0001` 句柄。

#### A.1 顶层字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `version` | string | `assessment-content-plan.v10-rule-priority` |
| `status` | `reviewed` / `unavailable` | 只要有可用内容就是 `reviewed`（下游兼容）；只有没有来源事实或没有时间预算时才是 `unavailable`。是否降级看 `completeness` |
| `completeness` | `complete` / `degraded` | 任一节状态为 `partial`、`fallback`、`failed` 时为 `degraded` |
| `clusterState` | `clusters_present` / `no_imbalance` / `unavailable` | `no_imbalance`：没有危险信号、没有非随访类健康问题、没有菌群分项偏移，规划不造集群，下游写“未形成需集中干预的失衡集群”并列维持要点 |
| `clusters`、`symptoms`、`lifestyleFactors`、`recognitions`、`cellScopes` | array | 原有五节，字段不变；新增字段见 6.3 |
| `priorityItems` | array | 第 1.3 节第 1 部分与第六章第一层级的 P0 事项（6.2） |
| `routineFollowups` | array | 第 1.3 节第 2 部分的常规专科随访（6.2） |
| `testGaps` | array | 第 1.3 节第 3 部分的检测盲区（6.2） |
| `sectionStatus` | object | 每节一项：`{state, reasons[], dropped[{code, errors[]}], repaired[], rows}`（6.4） |
| `rulesVersion` | object | `{redFlags, testGaps}` 规则表版本 |

#### A.2 程序判定的事项

`priorityItems[]`（只由危险信号规则授予，按时限从紧到松排序）：

| 字段 | 说明 |
|---|---|
| `code`、`ruleId` | 规则编号，如 `RF-FOBT-POSITIVE`；原检查建议补充来源为 `RF-ADVICE-URGENT:<问题code>` |
| `title` | 事项名，如“粪便隐血阳性”“缺铁性贫血需排查消化道失血” |
| `priority` | 固定 `P0` |
| `timeframe` | `{text, days, basis}`；`basis` 为 `program_default` 或 `original_advice` |
| `department` | 科室 |
| `checks[]` | 需完成的检查 |
| `factIds[]` | 触发规则的检验/检查记录（优先实测记录，没有时才用主检结论） |
| `problemCodes[]` | 关联的临床问题 code |
| `clusterCodes[]` | 承接该事项的 P0 集群 code |
| `evidenceText[]` | 命中原文，如“粪便隐血试验：弱阳性”（只供审计与写作参考，数值以 `factIds` 为准） |
| `sources[]` | 出处 `{key, title, publisher, citation, doi?, note?}`；程序默认时限另列 `program_default` |
| `origin` | `red_flag_rule` 或 `original_advice` |

`routineFollowups[]`：结节、息肉、囊肿、幽门螺杆菌阳性、肿瘤标志物轻度升高等规则命中的随访，加上原检查建议里有随访安排、但未进任何集群也未被 P0 覆盖的临床问题。

| 字段 | 说明 |
|---|---|
| `code`、`ruleId` | 规则编号（如 `RT-GB-POLYP`）；原建议来源为 `advice:<问题code>` / `RT-ORIGINAL-ADVICE` |
| `title` | 发现名 |
| `interval` | `{text, days, basis}`；`basis`：`original_advice`（取自原检查建议，如“每6个月复查”→180）、`rule_default`（规则表默认）、`original_advice_without_interval`（原建议没写间隔，`days` 为 `null`，`text` 为“按原检查建议”） |
| `department` | 原建议里的科室优先，没有时用规则表科室 |
| `factIds[]`、`problemCodes[]`、`originalAdvice[]`、`evidenceText[]`、`sources[]`、`origin` | 同上 |

`testGaps[]`：

| 字段 | 说明 |
|---|---|
| `code` | 项目代码，如 `lpa`、`ogtt`、`pepsinogen` |
| `test` | 项目名，如“脂蛋白(a)” |
| `reason` | 补查理由（书面语，可直接成句） |
| `ruleId`、`alsoRuleIds[]` | 首个命中的规则；其他也建议该项目的规则 |
| `finding` | 触发发现的短名 |
| `triggeredBy[]` | 触发发现的来源事实编号；按年龄性别筛查的条目为空数组 |
| `applies` | 适用条件（`sex`、`min_age`、`max_age`） |
| `sources[]` | 出处 |

#### A.3 五节里的新增字段

- 集群：`priority` 的 P0 只由规则授予——绑定了某个 `priorityItems` 的临床问题、或对其记录覆盖最多的集群升为 P0；没有绑定规则条目的 P0 降为 P1。原值记在 `sourcePriority`。`priorityItemCodes[]` 列出该集群承接的 P0 事项，P0 集群至少对应一个；`priorityWorkupCodes[]` 保留为承接的问题 code（兼容旧字段）。`origin`：`model`、`red_flag_rule`（P0 条目无人承接时程序补的主题）、`rule_fallback`（有异常但没有任何已审集群时程序按临床问题补的主题）。
- 主诉、生活因素：`origin` 为 `model` 或 `rule_fallback`（问卷有不适或生活方式回答、但没有任何已审条目时由程序按原始回答补齐；生活因素按睡眠、运动与久坐、饮酒与饮品、吸烟、精神压力、口腔护理、饮食与进餐分条）。

#### A.4 各节状态与原因码

`sectionStatus` 的键：`clusters`、`symptoms`、`lifestyleFactors`、`recognitions`、`cellScopes`，以及 `priorityItems`、`routineFollowups`、`testGaps`（后三者 `state` 固定为 `rule`）。

| `state` | 含义 |
|---|---|
| `reviewed` | 全部条目经审核并通过程序校验（可能含一次定向修复） |
| `partial` | 有条目被丢弃，或在已审条目之外补了程序条目 |
| `fallback` | 本节只有程序条目（或逐格背景只用了来源基线） |
| `failed` | 模型失败且没有可补的程序条目，本节为空 |
| `empty` | 本节按数据本就为空（见原因码） |

| 原因码 | 含义 |
|---|---|
| `items_repaired` | 校验失败的条目经一次定向修复后通过（`repaired[]` 列出 code） |
| `items_dropped` | 定向修复后仍不过的条目被丢弃（`dropped[]` 列出 code 与错误） |
| `items_dropped_in_final_check` | 跨节终检时丢弃的条目 |
| `repair_rejected` | 定向修复的返回未通过自检，失败条目直接丢弃 |
| `section_issue_pending` | 本节缺项（如未保留已确认的主诉）在定向修复后仍存在，交由程序补齐 |
| `review_checks_incomplete` / `sections_missing` | 两轮审核后自检仍不完整或结构缺失，本节清空 |
| `model_call_failed:<异常类型>` | 模型调用失败 |
| `timeout` | 规划总时限内未完成 |
| `rule_fallback` | 本节有程序补齐的条目 |
| `priority_rule_cluster_added` | P0 事项无集群承接，程序补了规则主题 |
| `uncovered_priority_item` / `uncovered_reported_symptom` | 终检时仍有 P0 事项无集群承接、或已确认的主诉无条目（程序无法补齐时） |
| `source_baseline_used` | 逐格背景批次失败，用来源基线（只含本格主记录） |
| `no_imbalance` | 集群为空且无失衡来源 |
| `no_reported_symptoms` | 问卷没有确认的不适 |
| `no_lifestyle_answers` | 没有可用的生活方式回答 |
| `no_recognition_items` | 没有来源冲突或目标对照项 |

#### A.5 集成方需要配合的地方

- 第 1.3 节、第五章、第六章第一层级按 `priorityItems`、`routineFollowups`、`testGaps` 出卡片；格子文字只做成句，没有文字时卡片照常显示。
- `evaluation_report.py` 目前只在 `status != "reviewed"` 时加 `REPORT_LAYOUT_INCOMPLETE`。规划降级时 `status` 仍为 `reviewed`，如需在回调里体现，可在 `completeness == "degraded"` 时追加部分成功原因（字段名由集成方定）。
- 第五章“全部正常”的写法看 `clusterState == "no_imbalance"`，不要用“集群数为 0”推断（有失衡来源却没有任何可用集群时为 `unavailable`）。

#### A.6 规则结论作为写作与来源核对的依据（R）

程序判定的事项除了以卡片和 `contextPurpose` 交给写作，还以可引用来源 `R` 交给写作和来源核对（`modules/report/assessment_rule_sources.py`），与原检查建议 `A` 同级核对。

- **开放范围**：只对命中的格开放。`priority.urgent`／`priority.routine`／`priority.gaps` 各取对应事项，`layer.clinical` 取三类事项；P0 集群的 `synthesis.*`、`synthesisAction.*` 取该集群 `priorityItemCodes` 所列事项；第三章 `clinicalRow.*` 取本行适用的目标区间。只是转述原检查建议的条目（`origin = original_advice`）不进 R，仍按 A 核对；触发记录不在本格范围内的规则不开放。
- **格式**：每格按顺序编号 `R01`、`R02`……，含 `kind`（`priority_item`／`routine_followup`／`test_gap`／`target`）、`ruleId`、`conclusion`（一句确定性结论）、时限或间隔、科室、需完成的检查、补查项目与理由或目标区间与适用条件、触发记录（`recordIds`，即本格的 F 编号）、出处（`program_default` 与原检查建议不算文献，时限取默认档位时另注 `basisNote`）。
- **写作**：照用 R 的段落在 `cite` 中写对应 R（可与 E 并列），并绑定 R 的触发记录；时限、科室、检查、项目和数值照抄 R。R 不是受检者事实，发现、测值、日期仍以 F 为依据；R 不写成原检查建议。
- **来源核对**：`patient_action` 命题新增用途 `rule_conclusion`（给 `ruleIds`，同句转述原医嘱时可同时给 `adviceIds`）。程序硬校验：所选 R 须在本段 `cite` 中（只引用一个 R 时自动补齐）；命题中的专科、检查项目、时限只能来自所选 R 或 A；引用 R 的段落中，时限、就诊科室须见于所引 R、本段原医嘱、记录或所引文献段落，引用目标区间的句子里的数值须见于所引 R 或本段记录；引用 R 的段落须绑定其触发记录；引用本格范围外的 R 编号直接驳回。回执规整：`ruleIds` 只保留本段所引 R，非 `rule_conclusion` 用途的 `ruleIds` 删去；服务端把行动拆段时 R 随行动段保留。
- **文献表**：已发布句子带 `rules[{ruleId, kind, sources}]`。六章投影时（参考文献终排之后）按 DOI 或题名复用已有条目，否则顺延编号追加，编号并入该格文字的 `evidenceRefs`，与目标区间文献同一套接入方式；重复投影不重复追加。

### B 生成流程、时间预算与兜底

#### B.1 流程

入口 `modules/report/evaluation_report.py::generate_evaluation_report()`。

1. 来源解析、临床问题整理、版面分组与风险背景关联。
2. 内容规划（集群、主诉、生活因素；单项定向修复，仍不过只丢该项，各节独立降级）与维度评分并行。
3. 程序判定：危险信号与常规随访（`assessment_red_flags.py`）、检测盲区（`assessment_test_gaps.py`）、目标区间（`assessment_targets.json` + `assessment_clinical_rules.py`）、检测行四档与结构化字段。
4. 证据召回：文献库、PubMed、DeepSeek 联网检索（`adapters/report/deepseek_tools.py`）查同一批主题。
5. 按格成稿：首稿 → 医学审核 + 来源核对 → 提前修复（首轮未过的明细格立即修复）→ 协调修复轮 → 句级保留或原值兜底。
6. 读者措辞整理（`evaluation_presentation.py`，确定性，不调模型）→ 六章投影（`assessment_presentation.py`）→ 回调（`evaluation_callback.py`）。

#### B.2 模型与并发

- 全程只用 `deepseek-flash`（2026-11 实测 `deepseek-v4-pro` 效果不如前者）。整章写作与审核开思考，强度 `high`；用低强度重放来源核对，漏检明显增多，不采用。
- 每份报告模型并发 12，应用共享上限 24（`fallback_policy.py`）。

#### B.3 时间预算

- 硬上限 30 分钟（`REPORT_GENERATION_TIMEOUT_SECONDS`）。
- 修复轮、最终修复和兼容字段只在目标时间（1140 秒）内排程；超出时由句级保留接手，不再等待。
- 兼容字段只写后续报告实际读取的旧字段（综合结论、重点方向、下一步、风险、医学发现、菌群）。

#### B.4 来源核对与兜底

- 回执由程序规整后再校验：同一格内 K 编号唯一、未使用的 F/E 删除、解释段里的行动句拆为行动段。数值、日期、编号范围、引文为原文子串等硬校验不放松（`assessment_source_receipts.py`）。
- 最后仍不过的格：只删审核实际驳回的分句，余下部分重新通过全部校验后采用；否则列出原值、参考范围和状态作为兜底文字。审计键 `sentenceRetention`、`sourceOnlyFallbackTargets`；只有原值兜底会使报告标为部分完成。
- 内容规划降级时回调 `partialReasons` 带 `REPORT_LAYOUT_INCOMPLETE`。

#### B.5 实测（2026-11-24，本机，`deepseek-flash`）

| 运行 | 总耗时 | 模型调用 | 被扣下的空格子 | 汉字 |
|---|---|---|---|---|
| 基线（9b0c8f5 加内容规格提交） | 1661 秒 | 198 | 16 | 24776 |
| 提速后，48 岁男性实例 | 1166 秒 | 164 | 0 | 27821 |
| 提速后，62 岁女性合成例 | 1145 秒（基线 1735 秒） | 182 | 0 | 24967 |

#### B.6 本地工具

- `scripts/generate_evaluation_report.py --checkpoint`：本地实跑，另存六章投影输入 `presentation_inputs.pkl`。
- `scripts/replay_assessment_presentation.py RUN_DIR`：不调模型，按当前代码重放投影、回调与样版 HTML。
- `scripts/check_assessment_report.py RUN_DIR --request REQUEST`：空节、禁用词、异常项覆盖、数值回溯、P0 与危险信号一致性、篇幅与耗时。
- `scripts/build_assessment_targets.py`：重建目标区间表（检索、模型提议、程序核对出处数值）。
