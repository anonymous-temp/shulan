# Questionnaire 固定证据规则消费

## 实现范围

Questionnaire 通过本域 [证据规则 Port](../modules/questionnaire/evidence.py) 与 [公开能力适配器](../adapters/questionnaire_evidence.py) 调用 Profile 的 [固定纯规则](profile-evidence-policy.md)。生产工厂显式选择 `profile.statement-evidence.v1 / profile-statement-evidence-v1`，不再导入 `health_profile.record_observations` 私有实现。

规则调用为进程内同步纯函数，不访问数据库、不创建画像事实、不调用模型。HTTP 查询、提交、生成和因素评估的对外字段及既有 Prompt 保持不变。问卷答案正式来源和附件发布迁移是独立任务，本切片没有切换画像摄取路径。

## 职责与原始材料

| 所属模块 | 负责内容 |
|---|---|
| Questionnaire | answerIds 和原文标签匹配；questionName 对应维度；多选/单选病史的准入；家族史不作为本人疾病；目标、候选和最终结果校验 |
| Profile 公开纯规则 | 问题语境、陈述类型、本人或血亲、否定/假设/冲突和时态识别 |
| Questionnaire Adapter | 精确选择规则版本，验证每次结果携带的实际选择，映射成本域结果 |

传给规则的标签、原问题、`answerText` 或 `answerQuoteUnits` 保留原值；列表不拼成文本，不去空格，不把模型改写当作用户事实。Profile 返回的 `condition/symptom`、`self` 和 `present` 必须同时符合当前目标，未知、过往疾病、家族史、健康兴趣不能因 presence 为 present 就被接纳为本人的当前疾病。既有“既往已愈疾病”和“过去症状”的区别保持原规则。

## 执行身份与恢复

新任务使用 `questionnaire-agent-manifest.v2`，受理时持久保存 `evidenceRuleSelection`。生产 manifest 来自实际注入的规则 Adapter，不能只填写版本名称。准备快照、准备失败记录、评估输入哈希和评估 checkpoint 都固定该选择。

已知旧 `questionnaire-agent-manifest.v1` 或没有 manifest 的历史任务，只能通过完整旧清单映射到可执行的冻结 v1；旧准备和 checkpoint 没有选择字段时同样显式映射 v1。已有旧检查点的输入哈希可被识别并重存为带版本的哈希，已完成的目标发现和评估继续复用。旧清单不随当前 main、模型或规则版本变化。当前格式缺少选择、显式 null、不支持版本、输入与检查点版本冲突均明确失败，不解析为 latest，也不自动重新调用模型。

执行取得模型并发槽后、模型成功或异常返回后、准备返回、checkpoint 保存和最终结果返回均再次核验选择。版本异常不进入普通检索失败或准备失败降级；模型故障、无适用证据等既有业务降级保持不变。选择元数据只用于内部执行，不进入业务 SSE 正文或新增 HTTP 字段。

## 升级与回退

规则需要新能力时由 Profile 独立交付可执行的新版本，Questionnaire 再显式选择并发布对应任务清单。未支持的旧任务明确失败，已完成结果不重新生成。回退按固定任务版本执行，不能把不认识的新任务交给旧实现。普通查询仍直接读取所属模块已保存数据。

## 验证范围

离线回归覆盖问卷已有疾病/症状/家族/否定案例，原始 JSON 传递，Profile 私有规则替换后冻结规则仍独立执行，错误版本与返回契约，等待并发槽期间规则变化，模型失败同时发生版本变化，以及旧 checkpoint 恢复不重复模型调用。完整测试记录在任务交付和根 ISSUE 中登记；离线结果不代替真实模型质量、数据库并发或来源发布部署验收。
