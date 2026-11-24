# Profile 固定版本的纯陈述证据规则

## 当前交付边界

Profile 提供同步、无模型和无数据库操作的陈述证据 Port。它解释调用方给定的原始回答和选定词语，返回语境、陈述种类、主体和肯否定状态；不会创建主体、摄取来源、生成或确认权威画像事实。既有 [Record 私有处理器](../health_profile/record_observations.py) 保持原状。

本能力由 Profile 独立交付；Questionnaire 已在随后消费者迭代中接通本域 Port、目标准入、任务清单与 checkpoint 版本，见 [Questionnaire 固定证据规则消费](questionnaire-evidence-policy.md)。该纯规则接入不代表各域正式来源已迁移。没有新增 HTTP 参数、路由、Prompt 或模型调用。

## 公共入口与固定选择

使用 [纯 DTO 与 Port](../modules/health_profile/evidence_contracts.py) 和 [公开应用门面](../modules/health_profile/application/__init__.py)；[实现工厂](../modules/health_profile/evidence_policy.py) 只接受精确选择：

```json
{
    "contractVersion": "profile.statement-evidence.v1",
    "implementationRevision": "profile-statement-evidence-v1"
}
```

```python
from modules.health_profile.application import create_profile_evidence_policy
from modules.health_profile.evidence_contracts import PROFILE_STATEMENT_EVIDENCE_V1
from modules.health_profile.evidence_contracts import ProfileEvidenceContextV1
from modules.health_profile.evidence_contracts import ProfileStatementEvidenceV1

policy = create_profile_evidence_policy(selection=PROFILE_STATEMENT_EVIDENCE_V1)
context = policy.classify_context(
    ProfileEvidenceContextV1(question='既往病史有哪些', dimension='medical_history')
)
assessment = policy.assess_statement(
    ProfileStatementEvidenceV1(
        kind='condition',
        label='高血压',
        answer=['高血压'],
        question='既往病史有哪些',
        dimension='medical_history',
    )
)
```

工厂没有默认选择；未知 contract/revision、未实现的 v2 或 latest 返回 `ProfileEvidencePolicyUnsupportedError`，不会调用其他版本。`policy.selection` 和两个结果的 selection 均为实际执行的固定选择，可供消费者与持久任务清单核对。DTO 同时支持 snake_case Python 字段和 camelCase 序列化，禁止未知字段和文本类型强制转换。

纯导入路径不会加载旧 `health_profile` 聚合包、Agent、MongoDB 或配置模块；现有 `modules.health_profile.contracts` 也提供 DTO 重导出，但它本身仍有历史聚合依赖。需要纯能力的消费者应使用上述独立 `evidence_contracts` 路径。

## 输入与结果语义

| 方法 | 输入 | 返回 |
|---|---|---|
| classify_context | question 原文、可选 dimension | topic：goal / medical_history / family_history / null，以及 selection |
| assess_statement | kind：condition / symptom，label，原 answer JSON，question 原文、可选 dimension | statementKind、subjectKind、presence，以及 selection |

已声明的 goal / medical_history / family_history 优先；其他非空 dimension 不会重新猜问题语境。answer 保留原字符串、数组边界、对象顺序和 JSON 标量；尤其不能将多选数组先压平，否则互斥选项冲突会丢失。`assess_statement` 固定检查本人候选，不接受来源模块提供的权威 factType、主体 ID 或模型推断事实。

statementKind 为 condition / symptom / history / family_history / interest；不能判定为受支持陈述时，statementKind、subjectKind、presence 均为 null。其余 presence 为 present / absent / unknown / conflicting。present 表示该类陈述的存在状态，不能仅凭 present 将 interest 或 history 提升为当前疾病；它也不代表医生确认或画像应用成功。返回值不包含原始答案正文、事实 ID 或事实存储对象。

调用方仍负责候选词是否命中允许引用的原文、问题与业务目标的关系，以及接纳哪些结果。Questionnaire 原规则应继续保留：answerIds 和 label 原文命中、questionName 到 dimension 的映射、家庭病史排除、选择式个人病史使用 medical_history 而普通自由文本使用 goal，并且仅接纳同候选种类、self、present 的结果。疾病目标由本域 adapter 映射 disease → condition。过去已恢复疾病与过去症状的既有处理差异也保持，不借本次封装改成统一“只允许当前状态”。

## 真正的版本隔离

[独立 v1 实现](../modules/health_profile/_evidence_v1.py) 从基线 `0b79755140655566b25d6ac55fa2253ec1e50a4d` 的 question_topic / normalize_record_statement 冻结完整 54 个定义的依赖闭包，包括分句、主体与亲属词表、并列断言、否定、假设、时态、互斥选择和相应常量。它只依赖标准库，不在运行时调用、复制、动态加载或引用当前私有规则。

此版本的行为变化必须使用新的 implementationRevision 和独立实现，旧 revision 继续真实可执行；新增公开形状则另升 contractVersion。更改当前 Profile 私有处理器不会自动改变 v1，消费者也不会自动转向新实现。此处复制规则是一次显式兼容冻结，维护归属仍为 Profile，不让各业务域分别修改一份画像语义。

消费者后续须在受理、准备结果和 checkpoint 中固定选择，执行前验证实际 provider 与结果 selection；已知历史任务显式映射冻结 v1，未知或缺失的不可识别清单不能自动使用 latest。不允许将不同版本的中间结果合并。上述持久任务策略属于消费者，本纯 Port 不读取或补写任何任务。

## 离线验证

[规则回归](../tests/unit/health_profile/test_evidence_policy.py) 对照既有私有实现，覆盖本人/亲属同句、否定并列、否定治疗与否定患病、假设和咨询、过去状态、互斥选项、完整 JSON 输入、精确版本拒绝以及变更全部旧私有函数和常量后 v1 仍不漂移。独立 Python 子进程禁止导入存储、配置、Agent 与旧 Profile 模块，验证纯公开工厂仍可完成两类判定。测试不调用真实模型、数据库或服务，不代表医学规则质量的线上验收。
