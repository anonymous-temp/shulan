# 用户健康画像 API 草案

> **Draft Version**: 0.3.0
>
> **Updated**: 2026年8月17日
>
> **Status**: 已并入 `docs/api/API.md`；当前版本为 0.14.0，本文仅保留评审历史
>
> **Current Contract**: `docs/api/API.md`

## 1. 文档边界

本文曾用于评审用户健康画像的候选 API。相关接口已经并入当前公开契约 `docs/api/API.md`；本文不再作为调用方接入依据，内容与当前契约不一致时以 `API.md` 为准。

本文中的“用户”指产品账号，“健康主体”指健康数据实际描述的人。手机号、账号状态和登录身份由外部用户系统管理，不属于本 API。

## 2. 接口总览

| 路径 | 名称 | 用途 |
|---|---|---|
| `/api/v1/health-profile/get` | 获取健康画像 | 获取一个主体的健康总览、指标趋势、异常、建议和循证支持 |
| `/api/v1/health-profile/subjects/list` | 获取健康主体列表 | 列出本人、家属、朋友等稳定主体 |
| `/api/v1/health-profile/subjects/upsert` | 新增或更新健康主体 | 创建家属/朋友主体或修改展示信息 |
| `/api/v1/health-profile/facts/list` | 获取健康事实 | 分页查询画像中的事实和待确认事实 |
| `/api/v1/health-profile/facts/resolve` | 处理待确认事实 | 确认、拒绝或更正候选事实 |
| `/api/v1/health-profile/facts/delete` | 删除健康事实 | 删除单条画像事实并立即停止普通读取返回 |
| `/api/v1/health-profile/changes/list` | 获取画像变更记录 | 分页查询不含模型 reasoning 的画像变更审计 |

所有接口均使用 `POST application/json`，以便与项目当前 API 风格一致，并避免健康主体标识进入 URL 查询日志。

## 3. 通用约定

### 3.1 身份与授权

- `userId`：产品用户标识，所有接口必填。
- `subjectId`：健康主体标识。读取本人画像时可省略；服务端使用该用户唯一的 `self` 主体。
- 家人、朋友或其他主体必须明确提供 `subjectId`。
- `userId` 只用于数据定位，不是授权凭据。当前接口未定义应用层认证头，只能由能够保证 `userId` 可信的上游服务调用；正式开放前需另行发布认证契约。
- `subjectId` 只在对应 `userId` 范围内有效；跨用户或跨主体访问返回 `404`。

### 3.2 时间与分页

- 时间统一使用带时区的 ISO 8601 字符串；示例使用 UTC 的 `Z`。
- 列表接口使用不透明 `cursor`，调用方不得解析或构造 cursor。
- `cursor` 与生成它的用户、主体和过滤条件绑定，更换任一查询条件后必须从 `cursor=null` 重新读取。
- `limit` 默认 20，范围 1 到 100。

### 3.3 版本和并发

- `profileVersion` 是一个主体画像的单调递增版本。
- 修改接口必须提交读取时得到的 `expectedProfileVersion`。
- 版本不匹配返回 HTTP `409`，调用方重新读取后再决定是否重试。
- 修改接口必须提供全局唯一 `requestId`；相同内容的重复请求不会重复执行，相同 `requestId` 携带不同内容返回 HTTP `409`。
- 事实处理结果后来被更正或删除时，再重放旧的处理请求返回 HTTP `409`，调用方应重新读取事实；接口不会通过幂等响应恢复已经失效或删除的健康值。

### 3.4 通用响应结构

```json
{
    "code": 200,
    "message": "success",
    "uuid": "A1B2C3D4-E5F6-7890-ABCD-EF1234567890",
    "timestamp": 1786582800,
    "elapsed_ms": 42,
    "data": {}
}
```

### 3.5 通用错误

| HTTP 状态码 | `message` 示例 | 语义 |
|---|---|---|
| `400` | `参数错误` | JSON 无效、请求体不是对象或缺少必填字段 |
| `404` | `健康主体不存在` | 用户、主体或事实不存在，或不属于该用户 |
| `409` | `画像版本已更新` | 乐观并发冲突、主体合并冲突或幂等键语义冲突 |
| `422` | `事实值无法校验` | 字段类型、枚举、数值、事实类型或关系无法通过领域校验 |
| `500` | `服务器内部异常` | 未分类的服务端异常 |

默认情况下，循证检索失败不会让整个画像读取失败，而是在 `evidence.status` 中降级说明。

## 4. 公共数据对象

### 4.1 `subject`

```json
{
    "subjectId": "subject_01K2SELF",
    "kind": "self",
    "displayName": "张三",
    "relationship": null,
    "status": "active",
    "version": 3
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `subjectId` | string | 不透明稳定主体 ID |
| `kind` | string | `self`、`blood_relative`、`non_blood_family`、`friend`、`other`、`unknown`；`unknown` 仅用于待解析主体 |
| `displayName` | string / null | 展示标签，不作为身份主键 |
| `relationship` | object / null | 相对产品用户的关系 |
| `relationship.relationType` | string | `father`、`mother`、`child`、`sibling`、`spouse`、`friend`、`other` 等 |
| `relationship.isBloodRelative` | boolean | 是否为血亲 |
| `status` | string | `active`、`merged`、`archived` |
| `version` | integer | 主体记录版本 |

### 4.2 `source`

```json
{
    "sourceType": "health_report",
    "sourceId": "report_01K2REPORT",
    "sourceRevision": 1,
    "pointer": "expansion[3]",
    "observedAt": "2026-07-01T00:00:00Z"
}
```

`sourceType` 可为 `health_report`、`record`、`chat`、`hospital`、`device`、`manual`。普通读取默认不返回完整聊天原文和报告全文。

### 4.3 `fact`

```json
{
    "factId": "fact_01K2ALT",
    "factType": "lab.alt",
    "category": "laboratory",
    "displayName": "丙氨酸氨基转移酶",
    "valueType": "quantity",
    "value": {
        "number": 48,
        "unit": "U/L"
    },
    "interpretation": "high",
    "referenceRange": {
        "text": "9-50 U/L",
        "low": 9,
        "high": 50,
        "unit": "U/L"
    },
    "assertionLevel": "observed",
    "verificationStatus": "verified_source",
    "confidence": 0.98,
    "status": "active",
    "effectivePeriod": {
        "start": "2026-07-01T00:00:00Z",
        "end": null
    },
    "sources": [
        {
            "sourceType": "health_report",
            "sourceId": "report_01K2REPORT",
            "sourceRevision": 1,
            "pointer": "expansion[3]",
            "observedAt": "2026-07-01T00:00:00Z"
        }
    ],
    "version": 1,
    "updatedAt": "2026-08-13T09:00:00Z"
}
```

| 字段 | 类型 | 说明 |
|---|---|---|
| `factId` | string | 事实 ID |
| `factType` | string | 版本化事实注册表中的规范类型 |
| `category` | string | `demographic`、`anthropometric`、`laboratory`、`condition`、`symptom`、`medication`、`allergy`、`lifestyle`、`family_history`、`goal` 等 |
| `displayName` | string | 面向用户展示的事实名称 |
| `valueType` | string | `text`、`coded`、`boolean`、`quantity`、`date`、`period`、`object` |
| `value` | string / boolean / number / object / array / null | 与 `valueType` 匹配的规范值 |
| `interpretation` | string / null | `normal`、`low`、`high`、`critical`、`unknown` |
| `referenceRange` | object / null | 来源提供的参考范围；字段内容随事实类型变化 |
| `assertionLevel` | string | `observed`、`clinician_recorded`、`user_confirmed`、`user_reported`、`model_inferred` |
| `verificationStatus` | string | `verified_source`、`confirmed`、`unconfirmed`、`conflicted` |
| `confidence` | number | 0 到 1 的提取置信度，不代表医学真实性 |
| `status` | string | `active`、`pending`、`disputed`、`superseded`、`retracted`、`expired` |
| `effectivePeriod` | object | 事实发生或有效时间；不是数据写入时间 |
| `sources` | array[object] | 可追溯的来源引用 |
| `version` | integer | 事实版本 |
| `updatedAt` | string | 事实最后更新时间，带时区的 ISO 8601 字符串 |

## 5. 获取健康画像

### 5.1 接口信息

- **请求路径**：`/api/v1/health-profile/get`
- **请求方法**：`POST`
- **Content-Type**：`application/json`

### 5.2 请求参数

| 参数名 | 类型 | 必填 | 描述 |
|---|---|---|---|
| `userId` | string | 是 | 产品用户 ID |
| `subjectId` | string | 否 | 省略时读取本人主体 |
| `includeEvidence` | boolean | 否 | 是否检索循证支持，默认 `false` |
| `includePending` | boolean | 否 | 是否返回待确认数量和摘要，默认 `true` |
| `indicatorLimit` | integer | 否 | 每个指标最多返回的趋势点，默认 12，范围 1 到 50 |

### 5.3 请求示例

```json
{
    "userId": "user_12345",
    "includeEvidence": true,
    "includePending": true,
    "indicatorLimit": 12
}
```

### 5.4 成功响应

```json
{
    "code": 200,
    "message": "success",
    "uuid": "A1B2C3D4-E5F6-7890-ABCD-EF1234567890",
    "timestamp": 1786582800,
    "elapsed_ms": 128,
    "data": {
        "userId": "user_12345",
        "subject": {
            "subjectId": "subject_01K2SELF",
            "kind": "self",
            "displayName": "张三",
            "relationship": null,
            "status": "active",
            "version": 3
        },
        "profileVersion": 42,
        "schemaVersion": 1,
        "updatedAt": "2026-08-13T09:02:00Z",
        "overview": {
            "gender": "male",
            "dateOfBirth": "1978-03-12",
            "age": 48,
            "heightCm": 162,
            "weightKg": 63.5,
            "bmi": 24.2,
            "pendingConfirmationCount": 2
        },
        "indicatorGroups": [
            {
                "code": "liver_function",
                "name": "肝功能",
                "indicators": [
                    {
                        "code": "ALT",
                        "name": "丙氨酸氨基转移酶",
                        "current": {
                            "factId": "fact_01K2ALT",
                            "value": 48,
                            "unit": "U/L",
                            "referenceRange": "9-50 U/L",
                            "interpretation": "high",
                            "observedAt": "2026-07-01T00:00:00Z"
                        },
                        "trend": "up",
                        "observations": [
                            {
                                "factId": "fact_01K1ALT",
                                "value": 36,
                                "unit": "U/L",
                                "observedAt": "2025-07-01T00:00:00Z"
                            },
                            {
                                "factId": "fact_01K2ALT",
                                "value": 48,
                                "unit": "U/L",
                                "observedAt": "2026-07-01T00:00:00Z"
                            }
                        ]
                    }
                ]
            }
        ],
        "alerts": [
            {
                "alertId": "alert_01K2LIVER",
                "severity": "important",
                "title": "肝脏功能异常",
                "factIds": [
                    "fact_01K2ALT",
                    "fact_01K2GGT"
                ],
                "status": "active"
            }
        ],
        "summary": {
            "overall": "当前需要重点关注肝脏代谢与血脂相关指标。",
            "priorities": [
                "复查肝功能和血脂指标",
                "结合临床医生意见评估持续异常原因"
            ],
            "followUp": "建议在医生指导下确定复查时间。",
            "generatedAt": "2026-08-13T09:02:00Z",
            "basedOnProfileVersion": 42
        },
        "evidence": {
            "status": "ready",
            "knowledgeBaseRevision": null,
            "generatedAt": "2026-08-13T09:02:01Z",
            "items": [
                {
                    "evidenceId": "evidence_01K2",
                    "title": "肝酶异常与代谢风险的循证关联",
                    "conclusion": "持续肝酶异常需要结合代谢指标和临床信息综合评估。",
                    "evidenceGrade": "ungraded",
                    "relevanceGrade": "retrieved",
                    "factIds": [
                        "fact_01K2ALT",
                        "fact_01K2GGT"
                    ],
                    "citations": [
                        {
                            "citationId": "kb_doc_123",
                            "title": "Clinical practice guideline for liver test abnormalities",
                            "publisher": "示例指南发布机构",
                            "publishedAt": "2025-01-01",
                            "url": "https://example.org/guideline/123"
                        }
                    ]
                }
            ]
        }
    }
}
```

### 5.5 `evidence` 字段

| 字段 | 类型 | 说明 |
|---|---|---|
| `evidence.status` | string | 本次循证结果状态，取值见下表 |
| `evidence.knowledgeBaseRevision` | string / null | 知识库返回修订标识时透传；供应方未返回时为 `null` |
| `evidence.generatedAt` | string / null | 实际执行检索的时间；未请求时为 `null` |
| `evidence.items` | array | 检索到的循证条目；无结果或不可用时为 `[]` |
| `evidence.items[].evidenceGrade` | string | 当前固定为 `ungraded`，不在没有分级数据时推断证据等级 |
| `evidence.items[].relevanceGrade` | string | 当前为 `retrieved`，表示知识库召回，尚未承诺临床相关性等级 |

### 5.6 `evidence.status`

| 值 | 说明 |
|---|---|
| `ready` | 已返回与当前画像版本匹配的循证结果 |
| `not_requested` | 请求没有启用 `includeEvidence` |
| `empty` | 检索成功，但没有足够相关的知识条目 |
| `unavailable` | 知识库暂不可用；画像其他部分仍然有效 |

### 5.7 注意事项

1. 存在有效 `dateOfBirth` 时，`age` 根据出生日期和请求日期动态计算；否则返回最近一次 `demographic.age` 观察值。仅有年龄不会反推出 `dateOfBirth`。
2. `bmi` 根据同一有效时点附近的身高和体重计算，并保留计算规则版本。
3. `summary` 是当前事实的展示性聚合，不等于诊断。
4. `evidence.items[].factIds` 必须能在该主体的事实中找到。
5. 联系电话不在响应中，由外部用户系统向前端提供。
6. `includePending=false` 时，`overview.pendingConfirmationCount` 不返回，`summary` 为 `null`。
7. 循证检索失败不会影响画像其他字段，调用方根据 `evidence.status` 判断是否展示。

## 6. 获取健康主体列表

### 6.1 接口信息

- **请求路径**：`/api/v1/health-profile/subjects/list`
- **请求方法**：`POST`

### 6.2 请求参数

| 参数名 | 类型 | 必填 | 描述 |
|---|---|---|---|
| `userId` | string | 是 | 产品用户 ID |
| `includeArchived` | boolean | 否 | 是否包含已归档主体，默认 `false` |

### 6.3 请求示例

```json
{
    "userId": "user_12345",
    "includeArchived": false
}
```

### 6.4 成功响应

```json
{
    "code": 200,
    "message": "success",
    "uuid": "B1B2C3D4-E5F6-7890-ABCD-EF1234567890",
    "timestamp": 1786582800,
    "elapsed_ms": 18,
    "data": {
        "userId": "user_12345",
        "subjects": [
            {
                "subjectId": "subject_01K2SELF",
                "kind": "self",
                "displayName": "张三",
                "relationship": null,
                "status": "active",
                "version": 3
            },
            {
                "subjectId": "subject_01K2FATHER",
                "kind": "blood_relative",
                "displayName": "父亲",
                "relationship": {
                    "relationType": "father",
                    "isBloodRelative": true
                },
                "status": "active",
                "version": 1
            }
        ]
    }
}
```

## 7. 新增或更新健康主体

### 7.1 接口信息

- **请求路径**：`/api/v1/health-profile/subjects/upsert`
- **请求方法**：`POST`

### 7.2 请求参数

| 参数名 | 类型 | 必填 | 描述 |
|---|---|---|---|
| `userId` | string | 是 | 产品用户 ID |
| `requestId` | string | 是 | 修改操作幂等键 |
| `subjectId` | string | 否 | 缺失表示创建；存在表示更新 |
| `kind` | string | 创建时是 | 不能通过本接口创建第二个 `self` 主体 |
| `displayName` | string / null | 否 | 主体展示标签 |
| `relationship` | object / null | 条件必填 | 非本人主体需要明确关系 |
| `expectedSubjectVersion` | integer | 更新时是 | 乐观并发版本 |

创建请求不能使用 `self` 或 `unknown`；`relationship.isBloodRelative` 必须与 `kind` 一致。更新时 `kind` 不可修改，未提供的可选字段保持原值，显式传入 `displayName: null` 表示清除展示标签。

### 7.3 创建父亲主体示例

```json
{
    "userId": "user_12345",
    "requestId": "request_subject_001",
    "kind": "blood_relative",
    "displayName": "父亲",
    "relationship": {
        "relationType": "father",
        "isBloodRelative": true
    }
}
```

### 7.4 成功响应

```json
{
    "code": 200,
    "message": "success",
    "uuid": "C1B2C3D4-E5F6-7890-ABCD-EF1234567890",
    "timestamp": 1786582800,
    "elapsed_ms": 25,
    "data": {
        "created": true,
        "subject": {
            "subjectId": "subject_01K2FATHER",
            "kind": "blood_relative",
            "displayName": "父亲",
            "relationship": {
                "relationType": "father",
                "isBloodRelative": true
            },
            "status": "active",
            "version": 1
        }
    }
}
```

重复提交相同 `requestId` 和相同请求内容时返回第一次操作的结果。相同 `requestId` 携带不同内容返回 HTTP `409`。更新时 `expectedSubjectVersion` 与当前版本不一致也返回 HTTP `409`。

## 8. 获取健康事实

### 8.1 接口信息

- **请求路径**：`/api/v1/health-profile/facts/list`
- **请求方法**：`POST`
- **Content-Type**：`application/json`

### 8.2 请求参数

| 参数名 | 类型 | 必填 | 描述 |
|---|---|---|---|
| `userId` | string | 是 | 产品用户 ID |
| `subjectId` | string | 否 | 省略时读取本人主体 |
| `categories` | array[string] | 否 | 分类过滤；支持公共 `fact.category` 的全部枚举；缺失或 `[]` 表示全部 |
| `statuses` | array[string] | 否 | 支持 `active`、`pending`、`disputed`、`superseded`、`retracted`、`expired`；缺失时默认前三项，不允许 `[]` 或 `deleted` |
| `factTypes` | array[string] | 否 | 规范事实类型过滤；缺失或 `[]` 表示全部 |
| `effectiveFrom` | string | 否 | `effectivePeriod.start` 的包含性下界，必须携带时区 |
| `effectiveTo` | string | 否 | `effectivePeriod.start` 的包含性上界，必须携带时区且不能早于 `effectiveFrom` |
| `cursor` | string / null | 否 | 首页传 `null` 或省略；后续页原样传回上一页 `nextCursor` |
| `limit` | integer | 否 | 默认 20，范围 1 到 100 |

### 8.3 请求示例

```json
{
    "userId": "user_12345",
    "subjectId": "subject_01K2SELF",
    "categories": [
        "laboratory",
        "condition"
    ],
    "statuses": [
        "active",
        "pending",
        "disputed"
    ],
    "cursor": null,
    "limit": 20
}
```

### 8.4 成功响应

```json
{
    "code": 200,
    "message": "success",
    "uuid": "D1B2C3D4-E5F6-7890-ABCD-EF1234567890",
    "timestamp": 1786582800,
    "elapsed_ms": 36,
    "data": {
        "userId": "user_12345",
        "subjectId": "subject_01K2SELF",
        "profileVersion": 42,
        "items": [
            {
                "factId": "fact_01K2ALT",
                "factType": "lab.alt",
                "category": "laboratory",
                "displayName": "丙氨酸氨基转移酶",
                "valueType": "quantity",
                "value": {
                    "number": 48,
                    "unit": "U/L"
                },
                "interpretation": "high",
                "referenceRange": {
                    "text": "9-50 U/L",
                    "low": 9,
                    "high": 50,
                    "unit": "U/L"
                },
                "assertionLevel": "observed",
                "verificationStatus": "verified_source",
                "confidence": 0.98,
                "status": "active",
                "effectivePeriod": {
                    "start": "2026-07-01T00:00:00Z",
                    "end": null
                },
                "sources": [
                    {
                        "sourceType": "health_report",
                        "sourceId": "report_01K2REPORT",
                        "sourceRevision": 1,
                        "pointer": "expansion[3]",
                        "observedAt": "2026-07-01T00:00:00Z"
                    }
                ],
                "version": 1,
                "updatedAt": "2026-08-13T09:00:00Z"
            }
        ],
        "nextCursor": null
    }
}
```

| 响应字段 | 类型 | 说明 |
|---|---|---|
| `data.userId` | string | 产品用户 ID |
| `data.subjectId` | string | 实际读取的健康主体 ID |
| `data.profileVersion` | integer | 返回该页时的画像版本 |
| `data.items` | array[object] | 健康事实数组；元素结构见 4.3 节 `fact` |
| `data.nextCursor` | string / null | 下一页游标；为 `null` 表示没有下一页 |

结果按 `effectivePeriod.start` 倒序排列；时间相同时按 `factId` 升序排列。分页期间若画像版本发生变化，调用方可根据 `profileVersion` 决定是否从第一页重新读取。已删除事实不会通过本接口返回。

## 9. 处理待确认事实

### 9.1 接口信息

- **请求路径**：`/api/v1/health-profile/facts/resolve`
- **请求方法**：`POST`
- **Content-Type**：`application/json`

### 9.2 请求参数

| 参数名 | 类型 | 必填 | 描述 |
|---|---|---|---|
| `userId` | string | 是 | 产品用户 ID |
| `subjectId` | string | 是 | 健康主体 ID |
| `factId` | string | 是 | `pending` 或 `disputed` 事实 ID |
| `decision` | string | 是 | `confirm`、`reject` 或 `replace` |
| `replacement` | object | 条件必填 | `decision=replace` 时必填；其他决定禁止提供 |
| `replacement.valueType` | string | 条件必填 | 更正值类型；枚举见 4.3 节 `fact.valueType` |
| `replacement.value` | string / boolean / number / object | 条件必填 | 与 `replacement.valueType` 匹配的更正值 |
| `replacement.effectivePeriod` | object | 条件必填 | 更正事实的有效时间 |
| `replacement.effectivePeriod.start` | string | 条件必填 | 开始时间，必须携带时区 |
| `replacement.effectivePeriod.end` | string / null | 否 | 结束时间；非空时必须携带时区且不早于开始时间 |
| `reason` | string / null | 否 | 用户可见的更正原因，不得包含模型 reasoning |
| `expectedFactVersion` | integer | 是 | 当前事实版本 |
| `expectedProfileVersion` | integer | 是 | 当前画像版本 |
| `requestId` | string | 是 | 修改操作幂等键 |

### 9.3 更正事实示例

```json
{
    "userId": "user_12345",
    "subjectId": "subject_01K2SELF",
    "factId": "fact_01K2WEIGHT",
    "decision": "replace",
    "replacement": {
        "valueType": "quantity",
        "value": {
            "number": 62.5,
            "unit": "kg"
        },
        "effectivePeriod": {
            "start": "2026-08-13T00:00:00Z",
            "end": null
        }
    },
    "reason": "用户更正录入错误",
    "expectedFactVersion": 1,
    "expectedProfileVersion": 42,
    "requestId": "request_fact_resolve_001"
}
```

### 9.4 成功响应

```json
{
    "code": 200,
    "message": "success",
    "uuid": "E1B2C3D4-E5F6-7890-ABCD-EF1234567890",
    "timestamp": 1786582800,
    "elapsed_ms": 31,
    "data": {
        "decision": "replace",
        "previousFactId": "fact_01K2WEIGHT",
        "fact": {
            "factId": "fact_01K2WEIGHT_NEW",
            "factType": "anthropometric.weight",
            "category": "anthropometric",
            "displayName": "体重",
            "valueType": "quantity",
            "value": {
                "number": 62.5,
                "unit": "kg"
            },
            "interpretation": null,
            "assertionLevel": "user_confirmed",
            "verificationStatus": "confirmed",
            "confidence": 1.0,
            "status": "active",
            "effectivePeriod": {
                "start": "2026-08-13T00:00:00Z",
                "end": null
            },
            "sources": [
                {
                    "sourceType": "manual",
                    "sourceId": "request_fact_resolve_001",
                    "sourceRevision": 1,
                    "pointer": null,
                    "observedAt": "2026-08-13T09:00:00Z"
                }
            ],
            "version": 1,
            "updatedAt": "2026-08-13T09:00:00Z"
        },
        "profileVersion": 43
    }
}
```

| 响应字段 | 类型 | 说明 |
|---|---|---|
| `data.decision` | string | 本次执行的 `confirm`、`reject` 或 `replace` |
| `data.previousFactId` | string | 被处理的原事实 ID |
| `data.fact` | object | 处理后的事实；结构见 4.3 节 `fact` |
| `data.profileVersion` | integer | 处理完成后的画像版本 |

- `confirm` 将原事实确认为 `active`，并返回同一个 `factId`。
- `reject` 将原事实标记为 `retracted`，并返回同一个 `factId`。
- `replace` 将原事实标记为 `superseded`，创建并返回一个新的 `active` 事实，因此返回新的 `factId`。
- 本接口只处理 `pending` 或 `disputed` 事实；其他状态返回 HTTP `409`。

## 10. 删除健康事实

### 10.1 接口信息

- **请求路径**：`/api/v1/health-profile/facts/delete`
- **请求方法**：`POST`
- **Content-Type**：`application/json`

### 10.2 请求参数

| 参数名 | 类型 | 必填 | 描述 |
|---|---|---|---|
| `userId` | string | 是 | 产品用户 ID |
| `subjectId` | string | 是 | 健康主体 ID |
| `factId` | string | 是 | 要删除的事实 ID |
| `deleteScope` | string | 是 | 当前唯一允许值为 `fact`，表示只删除指定画像事实 |
| `reason` | string / null | 否 | 删除原因，最长 1000 个字符；不在事实或公开变更记录中返回 |
| `expectedFactVersion` | integer | 是 | 当前事实版本 |
| `expectedProfileVersion` | integer | 是 | 当前画像版本 |
| `requestId` | string | 是 | 修改操作幂等键 |

### 10.3 请求示例

```json
{
    "userId": "user_12345",
    "subjectId": "subject_01K2SELF",
    "factId": "fact_01K2SYMPTOM",
    "deleteScope": "fact",
    "reason": "用户要求删除该条记录",
    "expectedFactVersion": 2,
    "expectedProfileVersion": 43,
    "requestId": "request_fact_delete_001"
}
```

### 10.4 成功响应

```json
{
    "code": 200,
    "message": "success",
    "uuid": "F1B2C3D4-E5F6-7890-ABCD-EF1234567890",
    "timestamp": 1786582800,
    "elapsed_ms": 29,
    "data": {
        "factId": "fact_01K2SYMPTOM",
        "status": "deleted",
        "profileVersion": 44,
        "deletedAt": "2026-08-13T09:00:00Z"
    }
}
```

| 响应字段 | 类型 | 说明 |
|---|---|---|
| `data.factId` | string | 已删除的事实 ID |
| `data.status` | string | 固定为 `deleted` |
| `data.profileVersion` | integer | 删除完成后的画像版本 |
| `data.deletedAt` | string | 删除完成时间，带时区的 ISO 8601 字符串 |

成功响应表示该事实值已删除，并且不会再通过画像或事实列表接口返回。`deleteScope=fact` 不删除原始体检报告、建档问答或聊天记录；这些来源数据由各自接口和数据保留规则管理。变更记录只保留不含事实值的删除事件。

## 11. 获取画像变更记录

### 11.1 接口信息

- **请求路径**：`/api/v1/health-profile/changes/list`
- **请求方法**：`POST`
- **Content-Type**：`application/json`

### 11.2 请求参数

| 参数名 | 类型 | 必填 | 描述 |
|---|---|---|---|
| `userId` | string | 是 | 产品用户 ID |
| `subjectId` | string | 否 | 省略时读取本人主体 |
| `eventTypes` | array[string] | 否 | 支持 `subject_created`、`subject_updated`、`fact_ingested`、`fact_confirmed`、`fact_retracted`、`fact_superseded`、`fact_deleted`；省略表示全部，不允许 `[]` |
| `cursor` | string / null | 否 | 首页传 `null` 或省略；后续页原样传回上一页 `nextCursor` |
| `limit` | integer | 否 | 默认 20，范围 1 到 100 |

### 11.3 请求示例

```json
{
    "userId": "user_12345",
    "subjectId": "subject_01K2SELF",
    "eventTypes": [
        "fact_confirmed",
        "fact_superseded",
        "fact_deleted"
    ],
    "cursor": null,
    "limit": 20
}
```

### 11.4 成功响应

```json
{
    "code": 200,
    "message": "success",
    "uuid": "01B2C3D4-E5F6-7890-ABCD-EF1234567890A",
    "timestamp": 1786582800,
    "elapsed_ms": 22,
    "data": {
        "userId": "user_12345",
        "subjectId": "subject_01K2SELF",
        "items": [
            {
                "eventId": "event_01K2",
                "sequence": 43,
                "eventType": "fact_superseded",
                "factId": "fact_01K2WEIGHT",
                "reasonCode": "user_correction",
                "actorType": "user",
                "sourceType": "manual",
                "createdAt": "2026-08-13T09:00:00Z"
            }
        ],
        "nextCursor": null
    }
}
```

| 响应字段 | 类型 | 说明 |
|---|---|---|
| `data.userId` | string | 产品用户 ID |
| `data.subjectId` | string | 实际读取的健康主体 ID |
| `data.items` | array[object] | 按 `sequence` 倒序排列的变更事件 |
| `data.items[].eventId` | string | 变更事件 ID |
| `data.items[].sequence` | integer | 该主体画像内单调递增的事件序号 |
| `data.items[].eventType` | string | 事件类型；枚举同请求参数 `eventTypes` |
| `data.items[].factId` | string / null | 关联事实 ID；主体事件为 `null` |
| `data.items[].reasonCode` | string | 稳定的变更原因代码 |
| `data.items[].actorType` | string | 操作者类型，例如 `user` 或 `system` |
| `data.items[].sourceType` | string / null | 变更来源类型 |
| `data.items[].createdAt` | string | 事件时间，带时区的 ISO 8601 字符串 |
| `data.nextCursor` | string / null | 下一页游标；为 `null` 表示没有下一页 |

变更记录不返回 LLM reasoning、完整聊天原文、请求中的自由文本原因或已删除的健康事实值。

## 12. 家族史与朋友咨询的响应规则

1. 父亲主体的糖尿病事实出现在父亲画像中。
2. 本人画像可以出现一条引用父亲 `subjectId` 的 `family_history.diabetes` 关联，但不得显示为本人的 `condition.diabetes`。
3. 配偶、继亲和朋友的疾病默认不生成本人的家族史关联。
4. 主体无法消歧时，候选保留为 `unknown` 或仅用于会话，不进入本人画像。
5. API 不允许使用一个主体的 `factId` 修改另一个主体；此类请求返回 `404`，避免泄露事实是否存在。

## 13. 降级与一致性

- 来源数据刚保存但尚未反映到画像时，`profileVersion` 和 `updatedAt` 仍表示最后一次完整更新；接口不会返回部分更新状态。
- 循证检索失败时返回 `evidence.status=unavailable`，画像事实、趋势和异常仍正常返回。
