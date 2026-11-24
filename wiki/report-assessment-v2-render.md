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

### 2.1 字段路径写法

`data.chapters[]` 按 `code` 取各章：`overview`、`symptoms`、`clinical`、`microbiome`、`clusters`、`conclusion`。下文 `overview.systems[]` 指 `code=overview` 那一章的 `content.systems[]`；`introduction.*`、`appendices.*` 指 `data` 下的同名对象；`subject.*`、`sample.*` 指回调顶层。

### 2.2 状态分档 `level`（全报告统一）

| `level` | 显示 | 圆点/色块 | 标签底色 / 文字 | 图标 |
|---|---|---|---|---|
| `focus` | 重点关注 | `#ec835a` | `#fbe7dc` / `#8a3413` | 实心圆内感叹号 |
| `borderline` | 亚临床边界 | `#fab219` | `#fdf2d6` / `#6a4500` | 圆环加中心点 |
| `normal` | 稳健达标 | `#0ca30c` | `#e6f3e4` / `#1c5c1c` | 对勾 |
| `notTested` | 未检测 | `#9aa09c` | `#eceeec` / `#3f423f` | 短横 |
| `null` | 不分级 | `#7d837f` | 同未检测 | 不显示标签 |

- 颜色永远与图标和文字同时出现，不单靠颜色区分（色盲与黑白打印可读）。
- 优先级标签：P0 优先医疗排查（`#f9e1e0` / `#9a2121`，三角图标）、P1 核心干预（同 `focus` 配色）、P2 协同调理（`#e7f0ed` / `#0d5f52`）、P3 随访巩固（同 `notTested`）。
- 前言末尾放一次图例：四档状态、参考范围条样例、优先级。

### 2.3 新字段与退化

规格 2.7 节的字段只追加、可为 `null`。前端读到字段就用；字段缺失（旧回调或解析不了）时，可按 `modules/report/assessment_chart_fields.py` 的同一规则从 `value`、`unit`、`referenceRange`、状态文字解析；解析不了就不画图，只显示原文数值和文字。

| 对象 | 字段 | 说明 |
|---|---|---|
| 检测行（临床） | `valueNumber`、`referenceLow`、`referenceHigh`、`referenceKind`（`range`/`upper`/`lower`/`qualitative`）、`level`、`direction`（`up`/`down`/`null`）、`target{low, high, text, evidenceRefs}` | 由临床视图给出；`target` 只在目标表条目适用时出现 |
| 菌群评分行 | `score`、`bands[{label, low, high}]`、`level`、`direction` | `bands` 取自报告说明（如 偏低 0–25、正常 25–75、偏高 75–100）；开放端为 `null` |
| 菌群丰度行 | `abundance`、`referenceLow`、`referenceHigh`、`level`、`direction` | 百分比数值 |
| 菌群宏观指数 | `valueNumber`、`referenceLow`、`referenceHigh`、`referenceKind`、`bands`、`level`、`direction`；肠龄另有 `actualAge`、`ageDifference` | 总分的 `level` 随其失衡分度 |
| 系统 | `level`、`anchors[{name, value, unit, direction, level}]`（最多 6 项）、`moreCount` | 五个固定系统总是列出 |
| 优先事项（规划契约） | `overview.priorityItems[]{code, title, priority, timeframe{text, days}, department, checks[], factIds[], basisRows?}`、`overview.routineFollowups[]{code, title, interval{text, days}, department}`、`overview.testGaps[]{code, test, reason}` | 规划契约实现后出现；样版也接受放在 `conclusion` 下 |
| 趋势 | `trends[].points[{date, value}]` | 已有 |

### 2.4 不显示的内容

- 不显示“部分报告”提示，完整性由回调 `status` 与 `partialReasons` 表达。
- 报告末尾不加免责声明；不显示“未形成解读”“暂无可展示的解读”这类内部状态，缺内容时显示本文各节的缺失说明或直接省略该格。
- 表头和标签不出现“您的”。问卷原题作标签时去掉“您”，并改成短语（如“平时运动频率”）。
- 程序生成的标签去掉“原报告”“原件”“主检”等后台用语（档名、病原评估名称）。模型撰写的正文不在渲染端改写。
- 无出处的“模板比较目标”（`comparisonReference`）不显示；目标区间只画 `target`。

### 2.5 打印

- A4 纵向，页边距 16/15/18 mm，页码居中；正文 10pt，标题宋体（Noto Serif CJK SC），正文与数字黑体（Noto Sans CJK SC），表格与刻度数字用等宽数字。
- 每章另起一页；卡片、范围条行、矩阵行、路线图阶段不跨页；小标题不落在页底。

## 3. 通用组件

### 3.1 参考范围条（RangeBar）

输入：`valueNumber`、`referenceLow`、`referenceHigh`、`referenceKind`、`target`、`level`。

- 坐标域：`range` 取 [低−0.55×宽, 高+0.55×宽]；`upper` 取 [0, 上限×1.6]；`lower` 取 [下限−0.6×|下限|, 下限+0.9×|下限|]。再并入实测值与目标端点，两端各留 4%；全部为非负量时下界不低于 0。实测值超出参考 4 个区间宽时截断坐标，在边缘画小三角表示越界。
- 画法：灰色轨道；参考带（浅绿 `#cfe6d8`、描边 `#6fa688`）；目标带画在轨道下方的深青细条（`#0d5f52`）；实测点为按 `level` 着色的圆点，外加白色描边与细外圈；参考带两端写刻度数值（带太窄时两数字分别左右对齐）。
- 行布局：名称（日期、计算值注记）｜数值+单位+方向箭头+状态标签（只对 `focus`/`borderline` 显示标签）｜范围条｜“参考 原文区间”“目标 文本 [文献号]”。
- 缺失：`referenceKind=qualitative` 或数值解析不了，改为一行文字（原文结论）；有数值无参考写“无参考区间”；有 `target` 无临床参考时只画目标带。

### 3.2 分档刻度条（BandScale）

输入：`bands[]`、`valueNumber` 或 `score`、`level`。坐标域取各档有限端点的最小到最大，开放端各外扩 25%；评分类固定 0–100。档名为“正常/健康/丰富/均衡”等的底色用浅绿，其余用浅黄，“极低/极高”用浅橙，档间留 1px 缝。档内放得下才写档名，内部分界写刻度；放不下时档名写在右侧说明里。

### 3.3 评分条（ScoreBar，4.4 用）

0–100 固定坐标，分档底色同 3.2，不写字；每个通路组上方用一行色块图例写出各分档（同组分档不同的分别列出）。两栏紧凑排布：名称｜条｜分数+箭头。偏离项排在前面，名称加粗。

### 3.4 丰度偏离条（AbundanceBar，4.3 用）

全报告共用一条对数轴 0.0001%–100%（每十倍一条细刻度），组头放一行刻度标签。参考带为 [`referenceLow`, `referenceHigh`]，下限为 0 时从轴原点开始。实测 0 画空心圆在原点，数值旁注“未检出”。

### 3.5 其他

- 值标签（ValueTag）：名称 + 数值 + 单位 + 方向箭头，左侧小圆点与浅底按 `level` 着色。用于系统锚点、主诉矩阵、集群证据、行动卡片依据。
- 趋势小折线：`points` 至少 2 个可解析数值才画；首尾点标数值和日期。
- 方向箭头为绘制的小图形（不用字符），并带“偏高/偏低”无障碍文字。

## 4. 按章节

### 封面与前言

| 字段 | 组件 | 画法要点 / 缺失时 |
|---|---|---|
| `subject.name/gender/age`、`sample.sampleId/method` | 身份卡（两列键值） | 档案编码用 `sampleId` |
| `introduction.dataTiming.clinicalDate/clinicalInstitution` | 身份卡 | 机构缺失只写日期 |
| `dataTiming.microbiomeSampleDate`，缺失时 `microbiomeReceivedDate`、`microbiomeReportDate` | 身份卡 + 资料时效条 | 无采样日时用收样日并注明“采样日期未注明”；间隔月数 = 天数/30.44，≤6 个月写“属同一健康周期，可交叉印证”，>6 个月写“结论以较新的资料为准”；任一日期缺失则不显示时效条 |
| `introduction.preface`、`readingGuide[]` | 段落 + 编号列表 + 图例 | 固定文字 |

### 第一章

| 字段 | 组件 | 画法要点 / 缺失时 |
|---|---|---|
| `overview.bodySummary` | 导语段 | |
| `overview.bodyGroups[]{name, summary, rows[]}` | 分组面板 + 每行一条参考范围条 | 组头状态取成员最重档；组 `summary` 放在条下；无体格资料写“本次资料未包含体格测量。” |
| `overview.summary` | 导语段 | |
| `overview.systems[]{code, name, level, anchors[], moreCount, summary}` | 系统状态卡片（两列网格） | 表头色块按 `level` 着色并带状态标签；锚点为值标签，`moreCount>0` 写“另有 N 项”；定论取 `summary` 第一段。`notTested` 写“本次资料未包含该系统的检查项目。”且不写定论。有“其他器官与专项检查”异常时追加第六张卡 |
| `overview.priorityItems[]` | 行动卡片（P0 红框） | 卡内：优先级标签、事项、时限（描边小标签）、科室、需完成检查、依据（值标签）。无 P0 时卡片上方写“本次未发现需优先就医排查的结果。” |
| `overview.routineFollowups[]` | 随访日程表 | 列：首月、3 个月、6 个月、12 个月；按 `interval.days` 落点（≤31/92/183/366 天），首次为实心点，之后按间隔重复为空心点 |
| `overview.testGaps[]` | 检测盲区列表（项目｜理由） | 空数组时省略 |
| 规划契约未出现时 | 行动卡片 | P0 集群（`clusters.clusters[priority=P0]`）画成行动卡：依据取其临床信号，“体检建议”取状态为原报告医嘱的信号原文；`overview.priorityFindings[]{code, name, action}` 画成文字卡（`code=gaps` 进检测盲区） |
| `overview.graphHub`、`graphClusterIds[]`、`clusters.clusters[]{id, name, priority}`、`graphSummary` | 集群关系图（SVG） | 左侧中心块：主题名 + 最多 3 个关键指标（BMI、内脏脂肪面积取自 `bodyGroups`，菌群总分与平衡状态取自 4.2）；右侧每个集群一个节点（编号、名称、优先级），节点底色按优先级；字号 12–15px。无集群写“本次各系统未形成需集中干预的失衡方向。”，不画图 |

### 第二章

| 字段 | 组件 | 画法要点 / 缺失时 |
|---|---|---|
| `symptoms.symptomsLead` | 导语段 | |
| `symptoms.symptoms[]{code, name, value, history, clinicalSignals[], microbiomeSignals[]}` | 主诉×证据矩阵 | 四列：主诉（编号块+名称）｜表现、持续与加重情境（`history` 按“问句：答案”拆行，问句改短标签；来源 B 的信号也放这一列）｜临床信号（值标签）｜菌群与代谢证据（值标签）。单元格只放短语和数值 |
| `symptoms[].interpretation`、信号的 `interpretation` | 2.2 条目（名词性小标题 + 段落） | |
| `symptoms.symptomSummary` | “综合印证”浅底块 | |
| 无主诉 | | 2.1 写“本次未记录主诉症状。”；2.2 标题改为“主要异常与日常感受的关联”，有 `symptomSummary` 就显示，否则写“本次未记录主诉症状，本节不作对照说明。” |

### 第三章

| 字段 | 组件 | 画法要点 / 缺失时 |
|---|---|---|
| `clinical.clinicalGroups[code∈cardiometabolic, organMetabolism, hematologyImmune, endocrineMusculoskeletal].templateRows[]{name, rows[], summary, evidenceRefs}` | 分组面板（3.1–3.4） | 组头状态取成员最重档；`focus`/`borderline` 与未分级但有数值的行逐条画参考范围条；`normal` 成员压成一行“参考范围内：名称 数值 单位；…”；其余无数值记录压成“其他记录”一行；`summary` 放下方。整组无数据不出小节标题，章首写“本次资料未包含……类检查，相应小节从略。” |
| `clinical.specialtyFindings[]` | 专项面板（3.5） | 按名称“ · ”前的设备名分组；`otherClinical` 组的行放本节末“其他检查”；无数据写“本次资料未包含设备专项检测与筛查项目。” |
| `clinical.trends[]{name, direction, points[], interpretation}` | 趋势小折线卡片 | 无可比结果写“本次资料未包含可比较的历次结果。” |
| `clinical.normalBaselines[]{name, rows[]}` | 稳健基线一行清单 | 对勾图标 + 组名 + 名称数值 |

### 第四章

| 字段 | 组件 | 画法要点 / 缺失时 |
|---|---|---|
| `microbiome.sampleContext`、`sampleSummary`、`fermentationSummary` | 章首浅底块 | 采样说明未提到抗生素、泻药、益生菌时补一句“采样前抗生素、泻药与益生菌使用情况未记录。” 整章无菌群数据写“本次资料未包含肠道菌群检测。” |
| `microbiome.digestiveContext[]{name, rows[], symptomContext[], summary}` | 分组面板（4.1） | 同第三章面板；本人报告放组头下一行 |
| `microbiome.structureGroups[].rows[]` | 宏观指数行（4.2） | 有 `bands` 用分档刻度条；有简单参考用参考范围条；名称含“评分/总分”且无参考时画 0–100 轴；肠龄画年龄轴并标出实足年龄刻线，右侧写“与实足年龄相差 ±N 岁”；分类值（平衡状态、肠型）只写值和状态标签。同一丰度行在 4.3 也出现时只在 4.3 画一次 |
| `microbiome.organismGroups[]{rows[], normalBaseline{count, rows[], names[], undetectedRows[]}, summary}` | 功能菌群面板（4.3） | 面板判定行（如“有益菌 异常”）放组头下的状态标签；丰度行画丰度偏离条；基因序列与菌群关联评估画值标签；正常项只计数：“N 项在参考范围内 示例（最多 6 个）等”；未检出序列信号另起一行 |
| `microbiome.functionalGroups[]{name, rows[], normalBaseline.rows[], panelStates[], summary}` | 评分条两栏（4.4） | 组数按实际；组头写“N 项中 M 项偏离”；`panelStates` 一行；分档色块图例；节首一句评分说明（基因预测的 0–100 分相对评分，不等同于体内营养素水平） |
| `microbiome.riskSummary` | 本章风险小结段落 | 末尾固定一句：“菌群风险模型的分值反映样本在模型中的位置，健康管理以临床检查结果为准。” |

### 第五章

| 字段 | 组件 | 画法要点 / 缺失时 |
|---|---|---|
| `clusters.clusterSummary` | 导语段 | |
| `clusters.clusters[]{id, name, priority, clinicalSignals[], microbiomeSignals[], interpretation, action}` | 集群卡片 | 表头：编号块、名称、优先级标签；三栏证据：临床与影像锚点（值标签）｜微生态与代谢锚点（值标签）｜相关生活因素（来源 B 的问答、`lifestyleFactors[].clusterIds` 含本集群的因素名）。下方依次为“健康影响”“处理方向”（`action`，缺失时取 `conclusion.priorities[]` 同编号的 `action`）、“体检建议”。某栏无内容写“本次未见相关记录”；某段无内容直接省略。无集群写“未形成需集中干预的失衡集群。” |
| `clusters.lifestyleSummary`、`lifestyleFactors[kind∈background, history, trigger]` | 先天背景与触发事件 | 无内容省略 |
| `clusters.lifestyleFactors[]{name, value, interpretation, clusterIds[]}` | 生活因素×集群矩阵 | 五个固定维度：饮食与进餐、饮酒与饮品、睡眠、运动与久坐、精神压力与口腔护理（按因素名称关键词归类，归不进的放“其他生活因素”）；列：行为特征（问答拆行）+ 每个集群一列，相关画实心圆点。无记录的维度写“未记录”。表下列出各因素的解读 |
| `clusters.lifestyleRecognition[]{name, text}` | 健康认知校准浅底块 | 无内容省略 |

### 第六章

| 字段 | 组件 | 画法要点 / 缺失时 |
|---|---|---|
| `conclusion.summary` | 导语段 | |
| 规划契约 + `clusters.clusters[].priority` + `conclusion.layers[]{code, text}` | 三阶段路线图（三栏） | 首月（第一层级 临床专科排查与随访）：`priorityItems`、`routineFollowups`、`testGaps`（契约未出现时为 P0 集群）；90 天（第二层级 核心代谢与肠道微生态干预）：P1 集群；持续（第三层级 营养储备与长期健康维护）：P2/P3 集群。`layers` 按 `code` 归入：`clinical`/`routine`→首月，`core`→90 天，`maintenance`→持续。空阶段分别写“本次未发现需在首月内完成的专科排查或随访事项。”“本次未形成需在 90 天内集中干预的代谢或微生态方向。”“保持现有健康习惯，按常规周期复查。” |
| `conclusion.managementDirections[]` | 方向块 | 带 `items[]` 的为“MTPN 个性化功能靶向配方方向”，`code=lifestyle` 的为“LSMI 去诱因生活方式干预方向”；有 P0 事项时块首写“先完成第一层级的专科排查，再开始配方与生活方式干预。”；都没有写“本次资料不足以形成配方与生活方式干预方向。” |
| `appendices.referenceLiterature[]` | 参考文献附录 | 只列正文引用到的编号 |

## 5. 程序判定规则摘要

- 临床行（字段缺失时的退化）：状态以“重点关注”开头或阳性 → `focus`；数值越出参考区间 20% 以上，或越界且状态为肥胖、过量 → `focus`；轻度越界或状态为偏高、偏低、有异常所见、结节等 → `borderline`；在参考区间内 → `normal`；无法判断为 `null`。临床视图给出 `level` 后以其为准。
- 菌群行：失衡分度（I 度及以上“失调”）→ `focus`；单项丰度、评分、指数越出参考或分档非正常 → `borderline`；在参考内或分档为正常、丰富、健康 → `normal`；总分随其失衡分度；肠龄高于实足年龄 → `borderline`。
- 系统：成员（第三章对应组的原子行，糖脂系统另含体格；菌群系统为宏观指数与功能菌群行）中最重的一档；无成员为 `notTested`。锚点取异常成员，按“重点关注优先、审阅单元引用过的优先、具体菌名优先于面板判定”排序。

## 6. 样版截图说明

样版 PDF 与截图放在仓库外 `_shulan_review/runs_v2/sample_render/`（含真实受检者资料，不入仓库）：

1. `shot_overview.png`（48 岁男性实例第一章）：1.2 五张系统状态卡片，表头色块分别为重点关注（橙）与亚临床边界（黄），卡内是带数值和方向的锚点标签与“另有 N 项”；其下为 1.3 的 P0 行动卡片（依据标签、体检建议）与 1.4 集群关系图（中心为 BMI、内脏脂肪面积与菌群总分，右侧 C1–C3 按优先级着色）。
2. `shot_ranges.png`（第三章 3.1 糖脂代谢）：每个分组面板头部带成员最重的状态标签；甘油三酯、总胆固醇、低密度脂蛋白等异常项各一条参考范围条（浅绿参考带、橙色实测点、带边刻度），参考范围内的项目压成一行清单，解读段落在条下。
3. `shot_microbiome.png`（第四章 4.3–4.4）：4.3 的丰度对数轴偏离条（共用刻度，0% 画空心圆），正常菌只计数并举例；4.4 每个通路组上方是分档色块图例，下方两栏 0–100 评分条，偏离项在前。

## 7. 尚未接入

- 规划契约（`priorityItems`、`routineFollowups`、`testGaps`）与临床行结构化字段由其他模块实现；样版已按“有就画、没有就退化”接入，契约出现后 1.3 与 6.1 自动改用契约。
- 集群对象尚未在投影里追加 `level`、`anchors`、`moreCount`；样版暂由集群自身的信号画值标签，颜色取各信号的 `level`。
