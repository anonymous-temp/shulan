# 评估报告六章（assessment.v2）样版渲染与组件说明

本文给甲方前端使用：说明回调里每个字段画成什么组件、怎么画、缺数据时显示什么。内容规格见 [report-assessment-v2.md](report-assessment-v2.md)（验收依据），对外字段契约以 [docs/api/API.md](../docs/api/API.md) 为准。

生产页面由甲方前端渲染。本仓库的 `scripts/render_assessment_presentation.py` 是参照样版：只读回调字段，不做医学判断，可打印 A4 PDF。

## 1. 生成样版

```bash
# 回调 JSON（assessment.v2 或本地形状均可）→ HTML
python scripts/render_evaluation_report.py callback.json report.html
# HTML → A4 PDF（无页眉页脚，页码由样式表输出）
chrome-headless-shell --no-sandbox --disable-gpu --no-pdf-header-footer --print-to-pdf=report.pdf file:///abs/path/report.html
# 用带 checkpoint 的运行目录离线重放投影、回调与 HTML（不调用模型）
python scripts/replay_assessment_presentation.py RUN_DIR --output OUT_DIR
```

重放需要与单元测试相同的离线环境变量（见 `tests/unit/conftest.py`），不读取 `.env`。

## 2. 通用约定

版式跟随甲方示例报告：每一节是一张固定列的表格，一行回答一个问题；我们只在单元格里加小图（范围条、分档刻度、评分条、丰度条）并保留集群关系图和三阶段路线图。

### 2.1 字段路径写法

`data.chapters[]` 按 `code` 取各章：`overview`、`symptoms`、`clinical`、`microbiome`、`clusters`、`conclusion`。下文 `overview.systems[]` 指 `code=overview` 那一章的 `content.systems[]`；`introduction.*`、`appendices.*` 指 `data` 下的同名对象。

### 2.2 状态与优先级标签

体格行、系统、分系统和表格行的 `status` 是五档文字之一（或“未检测”），`level` 是对应的四档取值。标签按 `status` 画，颜色永远与图标和文字同时出现。

| `status` | `level` | 标签 |
|---|---|---|
| 重点关注 | `focus` | 橙红底（`#fbe7dc` / `#8a3413`），圆内感叹号 |
| 亚临床边界 | `borderline` | 黄底（`#fdf2d6` / `#6a4500`），圆环加中心点 |
| 高于理想值 | `borderline` | 橙色描边，上三角 |
| 低于理想值 | `borderline` | 蓝色描边，下三角 |
| 稳健达标 | `normal` | 绿底（`#e6f3e4` / `#1c5c1c`），对勾 |
| 未检测 | `notTested` | 灰底（`#eceeec` / `#3f423f`），短横 |

`status` 是其他文字时（如菌群报告的“I 度失调”“低于实足年龄”），画 `level` 对应的标签，原文字放在标签下。旧回调里“重点关注：项目……”这类状态只取开头的档名。优先级标签：P0 优先医疗排查、P1 核心干预、P2 协同调理。

### 2.3 追加字段与退化

追加字段见内容规格第 7 节和 API 文档。读到字段就用；旧回调没有这些字段时按下表退化。

| 位置 | 优先使用 | 没有时 |
|---|---|---|
| 1.1、第三章的“理想目标” | 行组的 `comparisonReference`，显示为“理想：……” | 各成员的 `target.text` |
| 1.2 核心异常与关键实测值 | `systems[].keyFindings` | `anchors[]` 画成值标签 |
| 2.1 两栏证据 | `symptoms[].clinicalEvidence`、`microbiomeEvidence` | `clinicalSignals[]`、`microbiomeSignals[]` 画成值标签 |
| 2.2 小标题 | `symptoms[].interpretationTitle` | 主诉名称 |
| 5.1 两类证据 | `clusters[].clinicalEvidence`、`microbiomeEvidence` | 对应的信号值标签 |

### 2.4 不显示的内容

- 不显示“部分报告”提示，完整性由回调 `status` 与 `partialReasons` 表达。
- 报告末尾不加免责声明；不显示内部状态和英文代码。缺内容时显示回调给出的说明句，或省略该部分。
- 表头和标签不出现“您”。程序生成的行标签已在回调里去掉“您”和后台用语；模型撰写的正文不在渲染端改写。
- 理想目标只显示数值，不加出处类别标注，旁边不放文献角标。文献角标只跟在自身 `evidenceRefs` 非空的文字块之后；附录按编号连续列出正文标注的文献和各项理想目标的出处（`rows[].target.evidenceRefs`），并用一句话说明没有标注编号的文献是理想目标的依据。

### 2.5 打印

- A4 纵向，页码居中；标题宋体（Noto Serif CJK SC），正文与数字黑体（Noto Sans CJK SC）。
- 各章连续排版，不另起一页。表格可以跨页，表头在每页重复；短行不拆开，成员不少于 5 项或文字超过 220 字的行允许跨页；标题不落在页底。
- 单元格内的小图用行内块（`inline-block`）。Chrome 不会拆分含有块级 SVG 的单元格，用块级图会留下大片空白。

## 3. 通用组件

### 3.1 参考范围条（RangeBar）

输入：`valueNumber`、`referenceLow`、`referenceHigh`、`referenceKind`、`target`、`level`。

- 坐标域：`range` 取 [低−0.55×宽, 高+0.55×宽]；`upper` 取 [0, 上限×1.6]；`lower` 取 [下限−0.6×|下限|, 下限+0.9×|下限|]。再并入实测值与目标端点，两端各留 4%；全部为非负量时下界不低于 0。实测值超出参考 4 个区间宽时截断坐标，在边缘画小三角表示越界。
- 画法：灰色轨道；参考带（浅绿 `#cfe6d8`、描边 `#6fa688`）；目标带画在轨道下方的深青细条（`#0d5f52`）；实测点为按 `level` 着色的圆点，外加白色描边与细外圈；参考带两端写刻度数值（带太窄时两数字分别左右对齐）。
- 位置：放在表格“实测数值 / 实测结果”一格里，每个成员一行：名称、数值、单位、方向箭头，下方一条小范围条；单位按印刷习惯显示（`kg/m2` 写作 kg/m²，`10E9/L` 写作 ×10⁹/L），回调里的 `unit` 保持原样；参考范围和理想目标的文字在相邻的“参考”一格。
- 缺失：`referenceKind=qualitative` 或数值解析不了时只写原文结论，不画条；有 `target` 而没有临床参考范围时只画目标带。

### 3.2 分档刻度条（BandScale）

输入：`bands[]`、`valueNumber` 或 `score`、`level`。坐标域取各档有限端点的最小到最大，开放端各外扩 25%；评分类固定 0–100。档名为“正常/健康/丰富/均衡”等的底色用浅绿，其余用浅黄，“极低/极高”用浅橙，档间留 1px 缝。档内放得下才写档名，内部分界写刻度；放不下时档名写在右侧说明里。

### 3.3 评分条（ScoreBar，4.4 用）

0–100 固定坐标，分档底色同 3.2，不写字。放在 4.4 表格“异常子项及评分”一格里，每个异常子项一行：名称｜条｜分数加箭头；正常子项只在相邻一格列名称和分数，并写出正常档的区间（“基准 25–75”）。

### 3.4 丰度偏离条（AbundanceBar，4.3 用）

全报告共用一条对数轴 0.0001%–100%（每十倍一条细刻度），放在 4.3 表格“异常菌种 / 基因及方向”一格里各成员的右侧。参考带为 [`referenceLow`, `referenceHigh`]，下限为 0 时从轴原点开始。实测 0 画空心圆在原点，数值旁注“未检出”。

### 3.5 其他

- 值标签（ValueTag）：名称 + 数值 + 单位 + 方向箭头，左侧小圆点与浅底按 `level` 着色。用于追加字段缺失时的退化显示（系统锚点、主诉证据、集群证据）和 1.3 事项的小标签。
- 趋势小折线：`points` 至少 2 个可解析数值才画；首尾点标数值和日期。
- 方向箭头为绘制的小图形（不用字符），并带“偏高/偏低”无障碍文字。

## 4. 按章节

### 封面与前言

身份栏：`subject.name/gender/age`、档案编码 `sample.sampleId`；临床体检日期 `introduction.dataTiming.clinicalDate`；菌群采样日期，缺失时用收样日期并注明；一行资料时效（间隔月数，6 个月内写同一健康周期）。然后是 `introduction.preface` 与 `readingGuide[]`。不放图例。

### 第一章

| 节 | 字段 | 表格与画法 |
|---|---|---|
| 1.1 | `overview.bodySummary`、`bodyGroups[]` | 导语段；一张表：评估指标（`name`）｜实测数值（`rows[]`：名称、数值、单位、方向箭头，数值型且有参考范围或目标时下方一条小范围条）｜参考基准 / 理想目标（各成员 `referenceRange`，“理想：`comparisonReference`”）｜状态（`status`）｜生理与代谢意义（`summary`）。没有体格资料时只显示 `bodySummary` 的说明句 |
| 1.2 | `overview.summary`、`systems[]` | 导语段；一张表：系统（`name`）｜综合状态（`status`）｜核心异常与关键实测值（`keyFindings`）｜当前阶段评估结论（`summary`）。`level = notTested` 的系统显示“未检测”和短横 |
| 1.3 | `overview.priorityFindings[]` | `urgent`：“临床优先排查”提示框，每个事项为标题、对应集群、时限 / 科室 / 需完成的检查三组小标签和 `text` 段落；没有事项时显示该部分的 `text`。`routine`：表：发现｜科室｜复查间隔｜说明。`gaps`：表：建议补查｜依据；没有事项时不返回也不显示 |
| 1.4 | `graphHub`、`graphClusterIds[]`、第五章的 `clusters[]`、`graphSummary` | 集群关系图（中心块为主题和最多 3 个关键指标，右侧为各集群节点，按优先级着色）；每个集群一行（编号、名称、优先级标签）；`graphSummary` 段落。没有集群时只显示 `graphSummary` |

### 第二章

`symptomsLead` 导语；一张表：主诉（编号块加 `name`）｜持续时间与加重情境（`history`）｜临床检验与影像关联信号（`clinicalEvidence`）｜菌群与功能代谢关联证据（`microbiomeEvidence`）。2.2 每个主诉一条：小标题（`interpretationTitle`）加 `interpretation` 段落；最后是“综合印证”提示框（`symptomSummary`）。没有主诉时只显示 `symptomsLead` 与 `symptomSummary` 两句说明。

### 第三章

- `clinical.summary` 导语。每个 `clinicalGroups[]` 一节（3.1 起顺序编号，回调已按糖脂、肝胆、血液、内分泌、其他排序），一张表：检验 / 影像项目（`templateRows[].name`）｜实测结果（`rows[]`，影像文字可换行，数值型加小范围条）｜临床参考范围（理想目标）（各成员参考范围用缩写加区间，如“TG 0.00-1.70”；“理想：`comparisonReference`”）｜状态｜解读（`summary`）。
- 表后：有 `calibrationNote` 时放“数据校准说明”提示框；然后是分系统的 `summary` 段落。
- 其后依次是“本次未纳入的专项检测”（`specialtySummary`；`specialtyFindings` 非空时列“其他专项记录”）和“历年指标演变趋势与已确认稳健安全基线”（`trendSummary`、有 `trends` 时画小折线；“已确认的稳健基线”提示框含 `baselineSummary` 与 `normalBaselines[]` 的名称数值清单）。

### 第四章

| 节 | 字段 | 表格与画法 |
|---|---|---|
| 章首 | `overviewSummary`、`sampleSummary`、`fermentationSummary` | 导语段；“样本基线与发酵耐受”提示框 |
| 4.1 | `digestiveContext[]` | 表：消化道环节（`name`）｜相关检查结果（`rows[]`）｜对菌群定植与消化吸收的影响（`summary`） |
| 4.2 | `structureGroups[]`、`structureSummary` | 表：评估维度｜检测结果（有 `bands` 画分档刻度条，肠龄画实足年龄刻度）｜参考基准｜状态｜解读（`summary`）；表后 `structureSummary` |
| 4.3 | `organismGroups[]` | 表：类别｜异常菌种 / 基因及方向（面板判定标签；异常成员带数值、箭头和对数丰度条）｜已确认正常的部分（`normalBaseline` 的数量与几个示例）｜功能影响与调节意义（`summary`） |
| 4.4 | `functionalGroups[]` | 表：代谢通路｜异常子项及评分（0–100 评分条）｜通路内正常子项｜与临床指标及症状的交叉印证（`summary`） |
| 章末 | `riskSummary` | 段落 |

没有菌群资料时只显示 `overviewSummary` 的说明句。

### 第五章

`clusterSummary` 导语。每个集群：标题行“集群 C1：`name`”加优先级标签，然后四个小节：临床与影像证据（`clinicalEvidence`）、菌群与代谢证据（`microbiomeEvidence`）、对身体的影响与走向（`interpretation`）、处理方向（`action`）。5.2：`lifestyleSummary` 段落；表：生活方式维度（`name`）｜行为特征（`value`）｜正在维持的异常与对应集群（`interpretation` 加 `clusterIds` 标签）；“健康认知校准”提示框（`lifestyleRecognition[]` 编号列表）。

### 第六章

`conclusion.summary`；6.1 三阶段路线图（首月、90 天、持续三栏，来自 `layers[]`，各栏显示该层 `text` 和 `clusterIds` 标签，文字只出现一次）；6.2 `managementDirections[]`：MTPN 的 `items[]`（名称加文字）及其 `text` 提示，LSMI 的 `text`。附录为 `appendices.referenceLiterature[]` 中被正文标注或被理想目标引用的条目，编号连续。

## 5. 资料缺失时

样版对任何为空的列表和为 `null` 的文字都不报错：有说明句就显示说明句，否则省略该部分。没有菌群资料时，第四章只显示 `overviewSummary` 的说明句（`digestiveContext` 为空时不画 4.1），2.1 的表格不画菌群一栏；主诉或集群的某类证据文字为 `null` 时，表格格子显示“—”，集群卡片省略该小节。已用以下情形检查过：没有主诉；没有临床资料；没有集群和生活诱因；没有菌群资料；没有优先排查事项；全部文字为空；以及上一版的回调（没有追加字段，按 2.3 退化）。
