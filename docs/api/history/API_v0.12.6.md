# API 文档

> **Version**: 0.12.6（test）
>
> **Last Updated**: 2026年8月18日
>
> **测试服**: `http://123.57.56.88:9012`



## 通用响应数据结构

#### 响应示例

```json
{
    "code": 200,
    "message": "success",
    "uuid": "C78339AE-7D25-4948-8826-2641556A5A06",
    "timestamp": 1781164619,
    "elapsed_ms": 1000,
    "data": {}
}
```

#### 响应字段

| 字段       | 类型          | 说明                           |
| ---------- | ------------- | ------------------------------ |
| code       | int           | 状态码                         |
| message    | string        | 提示信息                       |
| uuid       | string        | 请求任务的唯一ID               |
| timestamp  | string        | 生成时间                       |
| elapsed_ms | string        | 耗时（ms）                     |
| data       | object / null | 数据详情（具体内容见接口详情） |

#### 状态码

| code | 含义                            | message          |
| ---- | ------------------------------- | ---------------- |
| 200  | 请求成功                        | success          |
| 400  | 参数错误                        | 参数错误         |
| 404  | 资源不存在（如 `task_id` 无效） | 资源不存在       |
| 409  | 资源状态或幂等事件冲突          | 资源状态冲突     |
| 413  | 文件大小超过限制                | 文件大小超过限制 |
| 415  | 不支持的文件格式                | 不支持的文件格式 |
| 422  | 字段类型、长度或结构校验失败    | 校验失败         |
| 422  | 校验失败                        | 校验失败         |
| 500  | 服务器内部异常                  | 服务器内部异常   |
| 502  | 服务器上游异常                  | 服务器上游异常   |



## 健康检查接口

#### 接口信息

- **请求路径**：`/health`
- **请求方法**：`GET`

#### 请求参数

无需参数。

#### 请求示例

```bash
curl http://host:port/health
```

#### 响应示例

```json
{
    "code": 200,
    "message": "success",
    "uuid": "10FC484B-2F47-48D1-8ADB-BA463B140E6A",
    "timestamp": 1781167812,
    "elapsed_ms": 1000,
    "data": {
        "status": "healthy",
        "environment": "dev"
    }
}
```

**data 字段说明**

| 字段        | 类型   | 说明     |
| ----------- | ------ | -------- |
| status      | string | 状态     |
| environment | string | 服务环境 |



## 体检报告AI解读接口

针对体检报告单独优化处理，可以处理各项指标、影像所见与诊断等。

#### 接口信息

- **请求路径**：`/api/v1/interpret/health-report`
- **请求方法**：`POST`
- **Content-Type**：`application/json`

#### 请求参数

| 参数名      | 类型   | 必填 | 描述 |
| :---------- | :----- | :--- | :--- |
| `fileId`    | string | 是   | 体检报告文件的 HTTP 或 HTTPS 网络地址；地址中必须能够识别受支持的文件扩展名。 |
| `userId`    | string | 是   | 用户唯一 ID。 |
| `subjectId` | string | 否   | 报告对应的健康主体 ID。未提供时，服务端自动创建或复用该用户的本人主体。 |
| `reportId`  | string | 否   | 报告唯一 ID，最长 128 个字符，只能包含字母、数字、点、冒号、下划线和短横线。未提供时由服务端生成。调用方需要安全重试时应主动提供并复用同一个值。 |

#### 请求示例

```json
{
    "fileId": "https://m.slmc.top/api/v1/file/preview?objectName=5a1a42b1890056a1c8a28cef71b0a922.pdf",
    "userId": "test-user-001",
    "subjectId": "subject-self-001",
    "reportId": "report-20260817-001"
}
```

```bash
curl -X POST http://host:port/api/v1/interpret/health-report \
  -H "Content-Type: application/json" \
  -d '{"fileId":"https://m.slmc.top/api/v1/file/preview?objectName=5a1a42b1890056a1c8a28cef71b0a922.pdf","userId":"test-user-001","subjectId":"subject-self-001","reportId":"report-20260817-001"}'
```

#### 响应示例

```json
{
    "code": 200,
    "message": "success",
    "uuid": "10FC484B-2F47-48D1-8ADB-BA463B140E6A",
    "timestamp": 1781167812,
    "elapsed_ms": 30000,
    "data": {
        "reportId": "report-20260817-001",
        "userId": "test-user-001",
        "subjectId": "subject-self-001",
        "reportType": "lab",
        "title": "血液生化检验报告",
        "reportTime": "2026-08-16",
        "aiSummary": "尿酸指标高于报告参考范围，建议结合医生意见持续关注。",
        "status": "abnormal",
        "expansion": [
            {
                "itemName": "尿酸",
                "resultValue": "468",
                "referenceRange": "208-428",
                "unit": "μmol/L",
                "flag": "偏高"
            }
        ]
    }
}
```

##### data 字段说明（通用结构）

| 字段             | 类型                    | 说明 |
| ---------------- | ----------------------- | ---- |
| `data.reportId`  | string                  | 报告唯一 ID。请求未提供时由服务端生成。 |
| `data.userId`    | string                  | 请求中的用户唯一 ID。 |
| `data.subjectId` | string                  | 报告实际绑定的健康主体 ID；请求未提供时返回服务端创建或复用的本人主体 ID。 |
| `data.reportType` | string                 | 报告类型：`lab`（检验报告）、`imaging`（影像报告）、`checkup`（综合体检报告）、`unknown`（无法确定）。 |
| `data.title`      | string / null          | 报告名称。 |
| `data.reportTime` | string / null          | 报告时间，尽量保留原文格式。 |
| `data.aiSummary`  | string / null          | 对报告的整体 AI 解读。 |
| `data.status`     | string                 | 总体状态：`normal`（正常）、`abnormal`（存在异常）、`pending`（无法判断）。 |
| `data.expansion`  | object / array / null  | 报告类型对应的特殊结构，详情见下文。 |

##### 幂等与错误处理

- 请求主动提供 `reportId` 时，相同 `reportId`、`userId`、实际绑定的 `subjectId` 和 `fileId` 的重复请求返回首次保存的报告，不会再次解析文件或调用模型。
- 同一 `reportId` 不能用于另一份文件、其他用户或其他健康主体；冲突时返回 HTTP `409`。
- 不提供 `reportId` 时，每次成功请求都会生成新的报告记录，因此网络重试可能产生多份报告。
- 服务端会异步尝试把受支持的检验指标更新到报告实际绑定的主体画像；接口成功响应仅表示报告已经保存，不表示画像已经更新完成。
- 未提供 `subjectId` 时报告默认绑定该用户的本人主体；调用方需要绑定其他既有主体时应明确传入对应 `subjectId`。
- `fileId` 缺失或格式错误、`reportId` 不符合约束、`subjectId` 为空字符串时返回 HTTP `400`；文件类型不受支持时返回 HTTP `415`；文件解析或模型解读失败时返回 HTTP `502`。

##### `data.expansion` 字段说明（特殊结构）

###### lab（检验报告）

`expansion` 为数组，每个元素为一项检测指标，无指标时返回空数组 `[]`。

```json
{
    "expansion": [
        {
            "itemName": "尿酸",
            "resultValue": "4.5",
            "referenceRange": "2~3",
            "unit": "ml",
            "flag": "偏高｜偏低｜正常"
        },
        {
            "itemName": "红细胞",
            "resultValue": "4.49",
            "referenceRange": "4.3~5.8",
            "unit": "ml",
            "flag": "偏高｜偏低｜正常"
        }
    ]
}
```

| 字段           | 类型           | 说明                           |
| -------------- | -------------- | ------------------------------ |
| itemName       | string or null | 项目名称                       |
| resultValue    | string or null | 结果值                         |
| referenceRange | string or null | 参考范围                       |
| unit           | string or null | 单位                           |
| flag           | string or null | 异常标记：偏高 / 偏低 / 正常。 |

###### Imaging（影像报告）

`expansion` 为对象，包含影像所见和影像诊断。

```json
{
    "expansion": {
        "findings": "影像所见",
        "diagnosis": "影像诊断"
    }
}
```

| 字段      | 类型           | 说明       |
| --------- | -------------- | ---------- |
| findings  | string or null | 影像所见。 |
| diagnosis | string or null | 影像诊断。 |

###### checkup（综合体检报告）

`expansion` 为对象，包含体检机构和体检时间。

```json
{
    "expansion": {
        "checkupOrgName": "体检机构",
        "checkupTime": "体检时间"
    }
}
```

| 字段           | 类型           | 说明       |
| -------------- | -------------- | ---------- |
| checkupOrgName | string or null | 体检机构。 |
| checkupTime    | string or null | 体检时间。 |



## AI建档问答接口

### 接口总览

| 接口                | 名称         | 说明                                             |
| ------------------- | ------------ | ------------------------------------------------ |
| `/api/v1/qa/create` | 会话创建接口 | 开始问答会话，并获取第一个问题                   |
| `/api/v1/qa/continue` | 继续问答接口 | 本轮结束后，使用同一会话生成新一轮问题         |
| `/api/v1/qa/complete` | 完成问答接口 | 用户明确结束建档，并将完整问答持久化            |
| `/api/v1/qa/answer/complete` | 答案提交接口 | 提交前一个问题的答案，并获取后续问题             |
| `/api/v1/qa/answer/stream` | 流式答案提交接口 | 提交答案并流式返回推理过程和后续问题（SSE）     |
| `/api/v1/qa/status` | 状态获取接口 | 获取当前会话状态，并返回后续问题（用于断点续答） |
| `/api/v1/qa/history` | 历史记录接口 | 获取已完成会话的完整问答历史记录                 |

### 问答流程

1. 请求”会话创建接口”，开始本轮简单问答，并获取第一个问题。
2. 请求”答案提交接口”，提交上一个问题的答案，并获取下一个问题。
3. 重复步骤二，直到本轮问答结束。
4. 前端询问用户“是否继续提问？”；用户确认后请求“继续问答接口”，否则请求“完成问答接口”。

> 问答中断，可以通过”状态获取接口”接着上一个问题继续问答，而不必重新开始创建会话。

### 问答特点

#### 问题数量

- **每轮问题数**：2-3个（由AI根据体检报告智能规划）
- **所有问题**：都是根据用户健康状况动态生成的正式问题
- **不再提问**：基本信息确认和自由补充两个固定问题
- **继续提问**：每轮结束后由前端询问用户，确认后后端重新生成2-3个问题

#### 深度提问策略

AI会根据以下原则生成问题：

1. **问题总数很少，每个问题都要有价值** - 避免泛泛而问，聚焦关键健康指标
2. **优先关注对用户健康影响最显著的指标** - 针对性强，而非全面覆盖
3. **深度挖掘而非广度覆盖** - 如果体检报告显示多个异常指标，选择影响最大的1-2个方向进行深度追问

#### 问题结构

| 顺序 | 问题ID | 问题类型 | 说明 |
| ---- | ------ | -------- | ---- |
| 第1-3题 | AI动态生成 | `text` / `radio` / `checkbox` | 根据体检报告、用户情况和已有问答历史动态生成 |

### 会话创建接口

#### 接口信息

- **请求路径**：`/api/v1/qa/create`
- **请求方法**：`POST`
- **Content-Type**：`application/json`

#### 说明

创建一个新的AI问答会话。本接口支持幂等性，重复调用不会覆盖现有数据。

**幂等性规则**：

- ✅ **会话不存在** → 创建新会话
- ✅ **会话存在且未完成** → 返回现有会话的当前问题（不创建新会话）
- ❌ **会话存在且已完成** → 返回错误（不允许覆盖已完成的会话）

**注意**：前端应使用唯一的 `sessionId`（如UUID），避免与已完成的会话冲突。

#### 请求参数

| 参数名      | 类型   | 必填 | 描述   |
| :---------- | :----- | :--- | :----- |
| `userId`    | string | 是   | 用户ID |
| `sessionId` | string | 是   | 会话ID |
| `name` | string | 否 | 患者真实姓名 |
| `fileList` | array | 否 | 本次建档使用的体检图片或报告，最多5份；缺失或为空表示本次建档不附带报告 |
| `hospitalData` | array[object] | 否 | 院内数据 |
| `weRunData` | array[object] | 否 | 微信运动数据 |
| `doctorProductPackages` | array[object] | 否 | 医生产品 |
| `smartDeviceData` | object | 否 | 华为智能设备数据 |
| `basicInfo` | object | 否 | 可靠的基本信息，可含 `gender`、`age`、`height`、`weight`；用于规划，不生成确认题 |

`fileList` 元素字段：

| 参数名 | 类型 | 必填 | 描述 |
| :--- | :--- | :--- | :--- |
| `fileList[].url` | string | 是 | 报告的 HTTP 或 HTTPS 地址；相同 URL 不得重复 |
| `fileList[].fileType` | string | 否 | 文件类型，可选值为 `pdf`、`jpg`、`jpeg`、`png`、`bmp`、`gif`；URL 无法体现扩展名时必须提供，URL 已包含扩展名时取值必须与其一致 |

#### 请求示例

> 请求数据格式详情见 *灵犀树兰对接文档*。

```json
{
    "userId": "user_12345",
    "sessionId": "session_67890",
    "name": "张三",
    "fileList": [
        {
            "url": "https://example.com/report1.pdf",
            "fileType": "pdf"
        },
        {
            "url": "https://example.com/file/download?objectName=report2.jpg",
            "fileType": "jpg"
        }
    ],
    "hospitalData": [],
    "weRunData": [],
    "doctorProductPackages": [
        {
            "id": 1,
            "packageName": "基础营养包",
            "products": [
                {
                    "id": 101,
                    "serviceName": "益生菌补充剂"
                }
            ]
        }
    ],
    "smartDeviceData": {
        "temperature": []
    },
    "basicInfo": {
        "gender": "男",
        "age": 30,
        "height": 175,
        "weight": 70
    }
}
```

```bash
curl -X POST http://host:port/api/v1/qa/create \
  -H "Content-Type: application/json" \
  -d '{
    "userId": "user_12345",
    "sessionId": "session_67890",
    "fileList": [
      {
        "url": "https://example.com/report1.pdf",
        "fileType": "pdf"
      }
    ]
  }'
```

`fileList` 中的每份报告均作为独立报告处理，并默认属于该 `userId` 的本人主体。只有全部报告成功处理后才会创建建档会话；任一报告失败时，本次请求返回错误，不会创建内容不完整的会话。创建成功后，本次会话后续问题始终使用创建时的报告数据；会话进行期间新上传的其它报告不会改变当前问答依据。

#### 响应示例

```json
{
    "code": 200,
    "message": "success",
    "uuid": "10FC484B-2F47-48D1-8ADB-BA463B140E6A",
    "timestamp": 1781167812,
    "elapsed_ms": 30000,
    "data": {
        "userId": "userId",
        "sessionId": "sessionId",
        "status": "question",
        "progress": "33%",
        "questionId": "q_gut_symptoms",
        "question": "识别到您的肠道菌群存在失衡的问题，请问您有没有以下肠道症状呢？",
        "questionType": "checkbox",
        "options": [
            "腹痛",
            "腹泻",
            "便秘",
            "大便不成形",
            "反酸 / 嗳气"
        ]
    }
}
```

**成功响应（会话已存在且未完成，幂等返回）**：

```json
{
    "code": 200,
    "message": "success",
    "uuid": "10FC484B-2F47-48D1-8ADB-BA463B140E6A",
    "timestamp": 1781167812,
    "elapsed_ms": 30000,
    "data": {
        "userId": "userId",
        "sessionId": "sessionId",
        "status": "question",
        "progress": "30%",
        "questionId": "q_3",
        "question": "您最近是否有失眠的情况？",
        "questionType": "radio",
        "options": [
            "从不",
            "偶尔",
            "经常",
            "总是"
        ]
    }
}
```

**说明**：当会话已存在且未完成时，接口会返回当前待答问题，不会创建新会话或覆盖已有数据。

**错误响应（会话已完成）**：

```json
{
    "code": 400,
    "message": "会话已完成，不允许重新创建",
    "uuid": "10FC484B-2F47-48D1-8ADB-BA463B140E6A",
    "timestamp": 1781167812,
    "elapsed_ms": 10,
    "data": null
}
```

其它错误状态：

| HTTP 状态码 | 触发条件 |
| :--- | :--- |
| `400` | `fileList` 不是数组、超过5份、元素或 URL 无效、URL 重复，或 `fileType` 与 URL 扩展名不一致 |
| `415` | 报告文件类型不受支持 |
| `502` | 报告文件解析或 AI 解读失败 |

**data 字段说明**

| 字段         | 类型                    | 说明                                  |
| ------------ | ----------------------- | ------------------------------------- |
| userId       | string                  | 用户ID                                |
| sessionId    | string                  | 会话ID                                |
| status       | string                  | `question`、`awaiting_decision` 或 `finished` |
| round        | integer                 | 当前轮次；continue/complete 必须原样携带 |
| progress     | string                  | 建档进度                              |
| questionId   | string                  | 问题ID                                |
| question     | string                  | 问题                                  |
| questionType | string                  | `text`、`radio` 或 `checkbox`         |
| options      | object/array[string]    | text 字段模板或选择题选项             |
| reasoning    | string                  | AI 推理过程（启用思考模式时）         |

1. `status` 表示：`question`（本轮答题中）、`awaiting_decision`（本轮结束，等待选择）或 `finished`（显式完成）。

2. `questionType` 支持 `text`（文本/结构化输入）、`radio`（单选）和 `checkbox`（多选）。

   不同的题目类型使`options`字段的类型也不相同。详情见下文。

3. `reasoning`字段包含 AI 的推理思考过程，解释为什么提出这个问题。仅在 `thinking=true` 时有值，`thinking=false` 时为空字符串。

##### `questionType: text|radio|checkbox`

`questionType`字段值为`radio`或`checkbox`的情况下，`options`为字符串数组。这些问题由AI根据用户的体检报告和健康状况动态生成，通常聚焦于关键健康指标的深度追问。

`questionType=text` 时 `options` 为字段名到默认值的对象；提交时 `answer` 可为对象，也可使用 `other` 提交普通文本。

**示例：**

```json
{
    "userId": "userId",
    "sessionId": "sessionId",
    "status": "question",
    "progress": "26%",
    "questionId": "q_gut_symptoms",
    "questionType": "checkbox",
    "question": "识别到您的肠道菌群存在失衡的问题，请问您有没有以下肠道症状呢？",
    "options": [
        "腹痛",
        "腹泻",
        "便秘",
        "大便不成形",
        "反酸 / 嗳气"
    ],
    "reasoning": "用户体检报告显示肠道菌群失衡指标异常，需要深入了解具体症状以便精准调理..."
}
```

**说明**：
- `radio`（单选）：用户只能选择一个选项
- `checkbox`（多选）：用户可以选择多个选项
- AI会根据问答历史和体检报告动态调整问题深度和方向

##### `status: awaiting_decision`

每轮最后一个问题提交后返回 `awaiting_decision`。此时问答仍未完成，只在 Redis 保留两小时；前端必须让用户选择 `/qa/continue` 或 `/qa/complete`。

```json
{
    "userId": "userId",
    "sessionId": "sessionId",
    "status": "awaiting_decision",
    "progress": "100%",
    "round": 1
}
```

### 继续问答接口

- **请求路径**：`/api/v1/qa/continue`
- **请求方法**：`POST`
- **Content-Type**：`application/json`

前端在一轮问答返回 `status: awaiting_decision` 后自行询问用户“是否继续提问？”。用户确认时调用本接口，后端会保留原问答历史，并重新规划和生成2-3个正式问题。

```json
{
    "userId": "user_12345",
    "sessionId": "session_67890",
    "round": 1,
    "thinking": true
}
```

`round` 必须使用 `awaiting_decision` 响应中的当前轮次。成功响应与会话创建接口的问题结构一致，返回新一轮的第一题；同一轮命令重试时幂等返回当前待答问题，过期轮次返回 HTTP 409。

### 完成问答接口

- **请求路径**：`/api/v1/qa/complete`
- **请求方法**：`POST`
- **Content-Type**：`application/json`

仅当会话为 `awaiting_decision` 时允许完成；重复调用已完成会话时幂等返回。成功后完整问答写入 MongoDB，Redis 临时状态删除，同一 `sessionId` 随后才允许进入 Chat。

```json
{
    "userId": "user_12345",
    "sessionId": "session_67890",
    "round": 1
}
```

```json
{
    "userId": "user_12345",
    "sessionId": "session_67890",
    "status": "finished",
    "round": 1,
    "progress": "100%",
    "completedAt": "2026-08-13T10:00:00"
}
```

`round` 必须与当前等待决策的轮次一致；过期轮次返回 HTTP 409。本接口不生成最终报告。方案1要求前端收到成功响应后，再串行调用 `/api/v1/report`。

### 答案提交接口

#### 接口信息

- **请求路径**：`/api/v1/qa/answer/complete`
- **请求方法**：`POST`
- **Content-Type**：`application/json`

#### 请求参数

| 参数名       | 类型                    | 必填 | 描述                                               |
| :----------- | :---------------------- | :--- | :------------------------------------------------- |
| `userId`     | string                  | 是   | 用户ID                                             |
| `sessionId`  | string                  | 是   | 会话ID                                             |
| `questionId` | string                  | 是   | 问题ID                                             |
| `answer`     | object/array/string     | 否   | text 可用对象或字符串；选择题使用数组。            |
| `other`      | string                  | 否   | 用户自定义回答。`answer` 和 `other` 至少提供其中一个。 |
| `thinking`   | boolean                 | 否   | 是否启用思考模式，默认为 `true`                     |

1. `radio` 的 `answer` 数组长度为 1，`checkbox` 可含多个元素；`text` 可提交对象或字符串。
2. `other`字段为用户自定义回答。若同时提供 `answer` 和 `other`，则优先使用 `other`，AI 会忽略 `answer` 字段内容。
3. `thinking` 参数说明：
   - `true`（默认）：启用思考模式，AI 会生成推理过程，响应中包含 `reasoning` 字段
   - `false`：关闭思考模式，AI 直接生成问题，`reasoning` 字段为空字符串

#### 请求示例

##### `questionType: radio`

单选题的 `answer` 数组只包含一个元素。

```json
{
    "userId": "userId",
    "sessionId": "sessionId",
    "questionId": "questionId",
    "answer": [
        "answer1"
    ]
}
```

```shell
curl -X POST http://host:port/api/v1/qa/answer \
  -H "Content-Type: application/json" \
  -d '{"userId": "userId", "sessionId": "sessionId", "questionId": "questionId", "answer": ["answer1"]}'
```

##### `questionType: checkbox`

多选题的 `answer` 数组可以包含多个元素。

```json
{
    "userId": "userId",
    "sessionId": "sessionId",
    "questionId": "questionId",
    "answer": [
        "answer1",
        "answer2"
    ]
}
```

```shell
curl -X POST http://host:port/api/v1/qa/answer \
  -H "Content-Type: application/json" \
  -d '{"userId": "userId", "sessionId": "sessionId", "questionId": "questionId", "answer": ["answer1", "answer2"]}'
```

#### 响应示例

同 *会话创建接口*。

当`thinking=true`时，额外添加一个`data.reasoning`字段。

### 流式答案提交接口（SSE）

#### 接口信息

- **请求路径**：`/api/v1/qa/answer/stream`
- **请求方法**：`POST`
- **Content-Type**：`application/json`
- **响应类型**：`text/event-stream` (SSE)

#### 说明

提交答案并流式返回推理过程和下一个问题。推理过程逐字流式返回，问题数据一次性返回。

#### 请求参数

同 *答案提交接口*。

| 参数名       | 类型                    | 必填 | 描述                                               |
| :----------- | :---------------------- | :--- | :------------------------------------------------- |
| `userId`     | string                  | 是   | 用户ID                                             |
| `sessionId`  | string                  | 是   | 会话ID                                             |
| `questionId` | string                  | 是   | 问题ID                                             |
| `answer`     | object/array/string     | 否   | text 可用对象或字符串；选择题使用数组。            |
| `other`      | string                  | 否   | 用户自定义回答。`answer` 和 `other` 至少提供其中一个。 |
| `thinking`   | boolean                 | 否   | 是否启用思考模式，默认为 `true`                     |

**thinking 参数说明**：
- `true`（默认）：启用思考模式，流式输出包含 `reasoning` 和 `question` 两种类型的事件
- `false`：关闭思考模式，流式输出只包含 `question` 类型的事件

#### 请求示例

```bash
# 启用思考模式（默认）
curl -X POST http://host:port/api/v1/qa/answer/stream \
  -H "Content-Type: application/json" \
  -d '{
    "userId": "user123",
    "sessionId": "session456",
    "questionId": "q1",
    "answer": ["睡眠良好"],
    "thinking": true
  }'

# 关闭思考模式
curl -X POST http://host:port/api/v1/qa/answer/stream \
  -H "Content-Type: application/json" \
  -d '{
    "userId": "user123",
    "sessionId": "session456",
    "questionId": "q1",
    "answer": {"name": "张三", "age": "25"},
    "thinking": false
  }'
```

#### 响应示例

```json
{
    "code": 200,
    "message": "success",
    "uuid": "request-uuid",
    "timestamp": 1781167890,
    "elapsed_ms": 10,
    "data": {
        "type": "start",
        "userId": "user123",
        "sessionId": "session456"
    }
}
```

**data 字段示例**

1. 开始事件

   ```json
   {
       "type": "start",
       "userId": "userId",
       "sessionId": "sessionId"
   }
   ```

2. 推理过程事件（仅当`thinking=true`时出现）

   ```json
   {
       "type": "reasoning",
       "content": "用户",
       "userId": "userId",
       "sessionId": "sessionId"
   }

   {
       "type": "reasoning",
       "content": "已确认基本信",
       "userId": "userId",
       "sessionId": "sessionId"
   }

   {
       "type": "reasoning",
       "content": "息，接下来需要了解...",
       "userId": "userId",
       "sessionId": "sessionId"
   }
   ```

3. 问题事件，返回提问问题。结构同非流式接口，虽然为流式接口，但是问题是一次性返回的。

   ```json
   {
       "data": {
           "type": "question",
           "status": "question",
           "progress": "20%",
           "questionId": "q2",
           "question": "您的饮食习惯如何？",
           "questionType": "radio",
           "options": [
               "规律三餐",
               "经常外卖",
               "不规律",
               "其他"
           ],
           "userId": "user123",
           "sessionId": "session456"
       }
   }
   ```

4. 本轮结束事件

   ```json
   {
       "data": {
           "type": "finish",
           "status": "awaiting_decision",
           "progress": "100%",
           "userId": "user123",
           "sessionId": "session456"
       }
   }
   ```

5. 错误事件（仅在异常时出现）

   ```json
   {
       "data": {
           "type": "error",
           "error": "问题ID不匹配或会话状态异常",
           "userId": "user123",
           "sessionId": "session456"
       }
   }
   ```

### 状态获取接口

#### 接口信息

- **请求路径**：`/api/v1/qa/status`
- **请求方法**：`POST`
- **Content-Type**：`application/json`

#### 请求参数

| 参数名      | 类型   | 必填 | 描述   |
| :---------- | :----- | :--- | :----- |
| `userId`    | string | 是   | 用户ID |
| `sessionId` | string | 是   | 会话ID |

#### 请求示例

```json
{
    "userId": "userId",
    "sessionId": "sessionId"
}
```

```shell
curl -X POST http://host:port/api/v1/qa/status \
  -H "Content-Type: application/json" \
  -d '{"userId": "userId", "sessionId": "sessionId"}'
```

#### 响应示例

同 *会话创建接口*。

### 历史记录接口

#### 接口信息

- **请求路径**：`/api/v1/qa/history`
- **请求方法**：`POST`
- **Content-Type**：`application/json`

#### 说明

用于获取会话的完整问答历史记录。无论会话是否完成，都可以获取历史记录。

**重要说明**：
- 历史记录只包含**已回答**的问题（用户已提交答案的问题）
- 当前待答问题（已返回给用户但未提交答案）**不**包含在历史记录中
- 当前待答问题应通过**状态获取接口**（`/api/v1/qa/status`）获取

#### 请求参数

| 参数名      | 类型   | 必填 | 描述   |
| :---------- | :----- | :--- | :----- |
| `userId`    | string | 是   | 用户ID |
| `sessionId` | string | 是   | 会话ID |

#### 请求示例

```json
{
    "userId": "user123",
    "sessionId": "session456"
}
```

```bash
curl -X POST http://host:port/api/v1/qa/history \
  -H "Content-Type: application/json" \
  -d '{"userId": "user123", "sessionId": "session456"}'
```

#### 响应示例

**成功响应（会话已完成）**：

```json
{
    "code": 200,
    "message": "success",
    "uuid": "A1B2C3D4-E5F6-7890-ABCD-EF1234567890",
    "timestamp": 1781167890,
    "elapsed_ms": 50,
    "data": {
        "userId": "user123",
        "sessionId": "session456",
        "status": "finished",
        "createdAt": "2026-06-17 10:00:00",
        "completedAt": "2026-06-17 10:15:00",
        "history": [
            {
                "questionId": "q_sleep",
                "question": "您平均每天睡眠时间是多少？",
                "questionType": "radio",
                "answer": [
                    "7-8小时"
                ],
                "answeredAt": "2026-06-17 10:01:23"
            },
            {
                "questionId": "q_2",
                "question": "您平均每天睡眠时间是多少？",
                "questionType": "radio",
                "answer": [
                    "7-8小时"
                ],
                "answeredAt": "2026-06-17 10:03:45"
            },
            {
                "questionId": "q_3",
                "question": "您是否有以下症状？（可多选）",
                "questionType": "checkbox",
                "answer": [
                    "头痛",
                    "失眠"
                ],
                "answeredAt": "2026-06-17 10:05:12"
            }
        ]
    }
}
```

**成功响应（会话进行中）**：

```json
{
    "code": 200,
    "message": "success",
    "uuid": "A1B2C3D4-E5F6-7890-ABCD-EF1234567890",
    "timestamp": 1781167890,
    "elapsed_ms": 50,
    "data": {
        "userId": "user123",
        "sessionId": "session789",
        "status": "question",
        "createdAt": "2026-06-17 10:00:00",
        "history": [
            {
                "questionId": "q_exercise",
                "question": "您每周运动频率如何？",
                "questionType": "radio",
                "answer": "每周1-2次",
                "answeredAt": "2026-06-17 10:01:23"
            },
            {
                "questionId": "q_2",
                "question": "您平均每天睡眠时间是多少？",
                "questionType": "radio",
                "answer": "6-7小时",
                "answeredAt": "2026-06-17 10:03:45"
            }
        ]
    }
}
```

**说明**：
- 会话答题中时 `status` 为 `question`；本轮结束等待选择时为 `awaiting_decision`
- 不包含 `completedAt` 字段（因为会话未完成）
- `history` 只包含已回答的问题（Q1、Q2）
- 当前待答问题（Q3）应通过 `/api/v1/qa/status` 接口获取

#### 响应字段说明

| 字段            | 类型   | 说明                                               |
| :-------------- | :----- | :------------------------------------------------- |
| `userId`        | string | 用户ID                                             |
| `sessionId`     | string | 会话ID                                             |
| `status`        | string | `question` / `awaiting_decision` / `finished` |
| `round`         | integer | 当前问答轮次                                      |
| `createdAt`     | string | 会话创建时间                                       |
| `completedAt`   | string | 会话完成时间（仅当 `status='finished'` 时存在）    |
| `history`       | array  | 已回答的问答历史记录数组                           |
| `history[].questionId`   | string | 问题ID                              |
| `history[].question`     | string | 问题内容                            |
| `history[].questionType` | string | 问题类型：`text`/`radio`/`checkbox` |
| `history[].answer`       | object/array/string | 用户答案；类型取决于问题类型与 `other` |
| `history[].answeredAt`   | string | 回答时间                            |

#### 答案格式说明

根据 `questionType` 不同，`answer` 字段为单选或多选答案：

1. **radio 类型**：`answer` 为只包含一个单选项的数组
   ```json
   {
       "answer": [
           "7-8小时"
       ]
   }
   ```

2. **checkbox 类型**：`answer` 为数组（多个选项）
   ```json
   {
       "answer": [
           "头痛",
           "失眠"
       ]
   }
   ```

#### 错误响应

**会话不存在**：

```json
{
    "code": 404,
    "message": "会话不存在",
    "uuid": "A1B2C3D4-E5F6-7890-ABCD-EF1234567890",
    "timestamp": 1781167890,
    "elapsed_ms": 10,
    "data": null
}
```

#### 注意事项

1. 无论会话是否完成，都可以获取历史记录。
2. 历史记录只包含**已回答**的问题（`qaHistory`）。
3. 当前待答问题（已返回给用户但未提交答案）不包含在历史记录中，应通过 `/api/v1/qa/status` 接口获取。
4. 未显式完成时，`status` 为 `question` 或 `awaiting_decision`，不包含 `completedAt` 字段。
5. 会话已完成时，`status` 为 `finished`，包含 `completedAt` 字段。



## 最终报告接口

本接口用于异步生成肠道菌群医学辅助解读报告。请求、立即响应、回调结构、证据约束以及失败语义均在本节定义；本节是该接口的完整对外契约。

#### 接口信息

- **请求路径**：`/api/v1/report`
- **请求方法**：`POST`
- **Content-Type**：`application/json`
- 请求 JSON 字段与现有调用方保持兼容。
- 接口校验并受理任务后立即返回 `processing`，不等待报告生成。
- 后台生成任务的硬上限为 15 分钟；正常流程预留最后 45 秒完成证据编号、固定 JSON 和回调。
- 生成成功、部分成功或失败都向回调接口地址发送 JSON。
- 测试环境回调地址为 `https://test.slmc.top/api/basic-health-assessment/ai-full-back/callback`。

#### 请求参数

| 参数名 | 类型 | 必填 | 描述 |
| :--- | :--- | :--- | :--- |
| `userId` | string | 是 | 用户唯一 ID。 |
| `sessionId` | string | 是 | 报告生成会话 ID；用于尝试补充个体上下文。 |
| `name` | string | 否 | 受检者姓名。 |
| `age` | string / number | 否 | 受检者年龄。 |
| `sex` | string | 否 | 受检者性别。 |
| `fileList` | array | 条件必填 | PDF 或图片格式的菌群检测报告。 |
| `testData` | array | 条件必填 | 检测平台详情地址和总结地址。 |
| `microbiomeData` | object | 条件必填 | 已结构化的菌群检测结果，兼容字段。 |
| `hospitalData` | array | 否 | 院内检查与检验结果。 |

`fileList`、`testData` 和 `microbiomeData` 至少提供一个包含可解析菌群结果的数据源。

##### 请求示例

```json
{
    "userId": "用户唯一ID",
    "sessionId": "346291b2-2166-430f-8352-ec7d9c11e64d",
    "name": "患者姓名",
    "age": "28",
    "sex": "男",
    "fileList": [
        {
            "url": "https://example.com/report.pdf",
            "fileType": "pdf"
        }
    ],
    "testData": [
        {
            "resultDetailUrl": "https://example.com/detail",
            "resultSummaryUrl": "https://example.com/summary"
        }
    ],
    "hospitalData": []
}
```

`microbiomeData/testData` 是调用方传入的当前专用检测数据，优先于可能包含旧报告的 `fileList` 上传件；多份结构化检测数据之间仍按报告日期、采样日期和调用方顺序选择最新项。每个来源都独立解析；只有样本编码、日期或报告事实能够证明属于同一快照时，才允许按 `microbiomeData` → `testData` → PDF → 图片的优先级补齐缺失字段；不同报告不合并，历史上传件不进入对外 JSON。

##### curl 示例

```bash
curl -X POST http://host:port/api/v1/report \
  -H "Content-Type: application/json" \
  -d '{"userId":"用户唯一ID","sessionId":"346291b2-2166-430f-8352-ec7d9c11e64d","microbiomeData":{"overallMetrics":[],"taxa":[]}}'
```

#### 响应示例

```json
{
    "code": 200,
    "message": "报告生成任务已受理",
    "uuid": "0F12075F-8F9C-47AC-9AF9-03C8B37F7F23",
    "timestamp": 1786093200,
    "elapsed_ms": 2.1,
    "data": {
        "userId": "用户唯一ID",
        "sessionId": "346291b2-2166-430f-8352-ec7d9c11e64d",
        "status": "processing"
    }
}
```

#### 回调接口

成功、部分成功和失败均使用同一套字段。字段值为空时不删除字段：单值返回 `null`，数组返回 `[]`，动态键对象返回 `{}`，固定结构对象返回其完整子字段。

以下内容是供前端联调使用的完整标准 JSON 测试数据，可直接复制并由 JSON 解析器读取。字段含义及约束见本节后续的“回调字段说明”。

<!-- evaluate-callback-example:start -->

```json
{
    "userId": "111",
    "sessionId": "dz98ua0ftryn9ckbhj39an",
    "type": "evaluate",
    "status": "success",
    "generated_at": "2026-08-10T16:30:00Z",
    "subject": {
        "name": "模拟患者",
        "gender": "男",
        "age": "28岁",
        "report_date": "2026-04-10"
    },
    "sample": {
        "sample_id": "3718407",
        "sample_type": "粪便",
        "test_item": "肠道菌群检测",
        "sample_date": "2026-04-09",
        "report_date": "2026-04-10",
        "sequencing_platform": null
    },
    "diagnosis_summary": {
        "conclusion": "本次菌群检测提示肠道微生态多样性下降，肠沙门菌异常检出，并存在短链脂肪酸相关功能菌不足，建议结合临床资料持续评估。",
        "clinical_context": {
            "异常菌群概况": "检出肠沙门菌，有益菌直肠真杆菌丰度偏低。",
            "肠型": "肠型I",
            "健康关注说明": "当前属于中度关注，建议结合症状和检查结果复评。",
            "综合健康画像": "近期存在腹胀、饮食偏油和睡眠不足，需同步调整生活方式。"
        },
        "risk": "中等风险",
        "evidence_refs": [
            1,
            2
        ]
    },
    "diagnostic_basis": {
        "abnormal_findings": [
            {
                "finding_type": "microbe",
                "item_id": "taxon-salmonella-enterica",
                "problem": "肠沙门菌 (Salmonella enterica)",
                "priority": "重点关注",
                "indicators": [
                    {
                        "name": "肠沙门菌 (Salmonella enterica)",
                        "value": "0.01%",
                        "status": "超标"
                    }
                ],
                "related_diseases_or_adverse_effects": [
                    {
                        "name": "沙门菌病",
                        "description": "人体文献支持肠沙门菌与沙门菌病的感染关联，个体临床意义需结合症状判断。",
                        "relation_type": "established_pathogen",
                        "evidence_refs": [
                            1
                        ]
                    }
                ],
                "biological_mechanism": "肠沙门菌异常检出可作为感染相关背景信息，应结合胃肠道症状和临床检查判断其实际意义。",
                "recommendations": [],
                "tips": {
                    "special_reminder": null,
                    "practical_goal": null
                },
                "interpretation_evidence_refs": [
                    1
                ],
                "recommendation_evidence_refs": [],
                "evidence_refs": [
                    1
                ],
                "evidence_level": "人体文献证据"
            },
            {
                "finding_type": "disease",
                "item_id": "disease-colorectal-cancer",
                "problem": "结直肠癌",
                "priority": "原报告关联项目",
                "indicators": [
                    {
                        "name": "原报告模型指数",
                        "value": "1.2",
                        "status": "检测方原值"
                    },
                    {
                        "name": "无痛肠镜检查",
                        "value": "结直肠黏膜大致正常",
                        "status": "已完成"
                    }
                ],
                "related_diseases_or_adverse_effects": [],
                "biological_mechanism": "该项目来自检测方模型，需结合年龄、症状、家族史及规范筛查结果综合判断。",
                "recommendations": [
                    "已有检查·结肠镜检查（确认）：用于辅助结直肠癌诊断",
                    "粪便隐血试验（筛查）：用于结直肠癌筛查"
                ],
                "tips": {
                    "special_reminder": null,
                    "practical_goal": null
                },
                "interpretation_evidence_refs": [
                    2
                ],
                "recommendation_evidence_refs": [
                    3
                ],
                "evidence_refs": [
                    2,
                    3
                ],
                "evidence_level": "指南/共识"
            }
        ],
        "evidence_refs": [
            1,
            2,
            3
        ]
    },
    "diagnostic_advice": {
        "report_interpretation": [
            "本次结果用于辅助理解肠道菌群状态，不能单独作为疾病诊断依据。",
            "建议结合腹胀等症状、既往检查和医生评估进行动态观察。"
        ],
        "Plan": {
            "未来1-2周": null,
            "未来1个月": null,
            "未来2-3个月": null,
            "未来6-12个月": null
        },
        "health_recommendations": [
            {
                "category": "diet",
                "content": "保持食物多样，适量增加蔬菜、全谷物及其他膳食纤维来源。",
                "evidence_refs": [
                    2
                ]
            },
            {
                "category": "sleep",
                "content": "保持相对固定的入睡和起床时间，并持续观察睡眠改善情况。",
                "evidence_refs": []
            }
        ],
        "not_recommended_behaviors": {},
        "medical_alerts": []
    },
    "evidence": [
        {
            "number": 1,
            "evidence_type": "literature",
            "topic": "菌种与疾病关联",
            "title": "肠沙门菌与人类胃肠道感染相关研究",
            "source": "示例医学期刊",
            "publication_date": "2025",
            "url": "https://example.com/evidence/salmonella",
            "evidence_level": "peer_reviewed_literature",
            "provider": "evimed_v2",
            "summary": "示例摘要：人体研究记录了肠沙门菌与胃肠道感染的临床关联。",
            "study_type": "队列研究",
            "authors": [
                "示例作者"
            ]
        },
        {
            "number": 2,
            "evidence_type": "guideline",
            "topic": "配套临床检查、健康调护",
            "title": "结直肠癌筛查与健康生活方式指南",
            "source": "示例医学学会",
            "publication_date": "2025",
            "url": "https://example.com/evidence/colorectal-screening",
            "evidence_level": "guideline_or_consensus",
            "provider": "evimed_v2",
            "summary": "示例摘要：指南说明了结直肠癌筛查方法及健康生活方式建议。",
            "study_type": "临床实践指南",
            "authors": []
        },
        {
            "number": 3,
            "evidence_type": "guideline",
            "topic": "配套临床检查",
            "title": "结直肠癌筛查指南",
            "source": "示例医学学会",
            "publication_date": "2025",
            "url": "https://example.com/evidence/colorectal-screening",
            "evidence_level": "guideline_or_consensus",
            "provider": "evimed_v2",
            "summary": "指南文本块明确列出结肠镜及粪便隐血试验的筛查作用。",
            "study_type": "临床实践指南",
            "authors": []
        }
    ],
    "partial_reasons": [],
    "error": {
        "error_code": null,
        "message": null
    }
}
```
<!-- evaluate-callback-example:end -->

以下是不带注释的简化标准 JSON 示例：

```json
{
    "userId": "用户唯一ID",
    "sessionId": "346291b2-2166-430f-8352-ec7d9c11e64d",
    "type": "evaluate",
    "status": "success",
    "generated_at": "2026-08-09T09:00:00Z",
    "subject": {
        "name": "患者姓名",
        "gender": "男",
        "age": "28岁",
        "report_date": "2026-04-10"
    },
    "sample": {
        "sample_id": "3718407",
        "sample_type": "粪便",
        "test_item": "肠道菌群检测",
        "sample_date": "2026-04-09",
        "report_date": "2026-04-10",
        "sequencing_platform": null
    },
    "diagnosis_summary": {
        "conclusion": "个体化全文总结",
        "clinical_context": {
            "异常菌群概况": "本次异常菌群概括",
            "肠型": "肠型I"
        },
        "risk": "中等风险",
        "evidence_refs": [
            1
        ]
    },
    "diagnostic_basis": {
        "abnormal_findings": [
            {
                "finding_type": "microbe",
                "item_id": "taxon-salmonella-enterica",
                "problem": "肠沙门菌 (Salmonella enterica)",
                "priority": "重点关注",
                "indicators": [
                    {
                        "name": "肠沙门菌 (Salmonella enterica)",
                        "value": "0.01%",
                        "status": "超标"
                    }
                ],
                "related_diseases_or_adverse_effects": [
                    {
                        "name": "沙门菌病",
                        "description": "与沙门菌病的人体感染关联",
                        "relation_type": "established_pathogen",
                        "evidence_refs": [
                            1
                        ]
                    }
                ],
                "biological_mechanism": "经证据校验的医学解释",
                "recommendations": [],
                "tips": {
                    "special_reminder": null,
                    "practical_goal": null
                },
                "interpretation_evidence_refs": [
                    1
                ],
                "recommendation_evidence_refs": [],
                "evidence_refs": [
                    1
                ],
                "evidence_level": "人体文献证据"
            },
            {
                "finding_type": "disease",
                "item_id": "disease-colorectal-cancer",
                "problem": "结直肠癌",
                "priority": "原报告关联项目",
                "indicators": [
                    {
                        "name": "原报告模型指数",
                        "value": "1.2",
                        "status": "检测方原值"
                    }
                ],
                "related_diseases_or_adverse_effects": [],
                "biological_mechanism": "该项目的医学含义与关联风险",
                "recommendations": [
                    "结肠镜检查（筛查）：用于观察结直肠黏膜并发现可疑病变"
                ],
                "tips": {
                    "special_reminder": null,
                    "practical_goal": null
                },
                "interpretation_evidence_refs": [
                    1
                ],
                "recommendation_evidence_refs": [
                    1
                ],
                "evidence_refs": [
                    1
                ],
                "evidence_level": "指南/共识"
            }
        ],
        "evidence_refs": [
            1
        ]
    },
    "diagnostic_advice": {
        "report_interpretation": [
            "本次报告整体理解"
        ],
        "Plan": {
            "未来1-2周": null,
            "未来1个月": null,
            "未来2-3个月": null,
            "未来6-12个月": null
        },
        "health_recommendations": [
            {
                "category": "diet",
                "content": "有相应证据支持的膳食建议",
                "evidence_refs": [
                    1
                ]
            }
        ],
        "not_recommended_behaviors": {},
        "medical_alerts": []
    },
    "evidence": [
        {
            "number": 1,
            "evidence_type": "literature",
            "topic": "菌种与疾病关联",
            "title": "文献题名",
            "source": "期刊",
            "publication_date": "2025",
            "url": null,
            "evidence_level": "peer_reviewed_literature",
            "provider": null,
            "summary": "文献摘要",
            "study_type": null,
            "authors": []
        }
    ],
    "partial_reasons": [],
    "error": {
        "error_code": null,
        "message": null
    }
}
```


+##### 回调字段说明

回调示例只展示标准 JSON 数据；原逐字段注释统一整理在以下字段表中。数组元素使用 `[]` 表示，例如 `evidence[].number`。

###### 顶层字段

| 字段路径 | 类型 | 说明 |
| :--- | :--- | :--- |
| `userId` | string | 用户 ID；回调统一序列化为字符串。 |
| `sessionId` | string | 报告生成会话 ID。 |
| `type` | string | 报告类型，当前固定为 `evaluate`（评估报告）。 |
| `status` | string | 生成状态：`success`、`partial_success` 或 `failed`。 |
| `generated_at` | string / null | 报告生成完成时间，采用 ISO 8601 格式；失败时为 `null`。 |
| `subject` | object | 受检者信息；对象及其固定子字段始终返回。 |
| `sample` | object | 样本信息；对象及其固定子字段始终返回。 |
| `diagnosis_summary` | object | 评估报告综合摘要。 |
| `diagnostic_basis` | object | 评估依据，包括异常菌群和原报告疾病或健康问题。 |
| `diagnostic_advice` | object | 健康调护及后续建议。 |
| `evidence` | array | 公开证据目录；无证据时返回 `[]`。 |
| `partial_reasons` | array | 部分成功原因的稳定机器码；`success` 和 `failed` 通常返回 `[]`。 |
| `error` | object | 错误信息；成功或部分成功时固定子字段均为 `null`。 |

###### 受检者与样本字段

| 字段路径 | 类型 | 说明 |
| :--- | :--- | :--- |
| `subject.name` | string / null | 受检者姓名；无内容时为 `null`。 |
| `subject.gender` | string / null | 受检者性别；无内容时为 `null`。 |
| `subject.age` | string / null | 受检者年龄，包含“岁”；无内容时为 `null`。 |
| `subject.report_date` | string / null | 菌群检测报告日期，格式为 `YYYY-MM-DD`；无内容时为 `null`。 |
| `sample.sample_id` | string / null | 样本编码；无内容时为 `null`。 |
| `sample.sample_type` | string / null | 样本类型；无内容时为 `null`。 |
| `sample.test_item` | string / null | 检测项目；无内容时为 `null`。 |
| `sample.sample_date` | string / null | 采样日期，格式为 `YYYY-MM-DD`；无内容时为 `null`。 |
| `sample.report_date` | string / null | 检测报告日期，格式为 `YYYY-MM-DD`；无内容时为 `null`。 |
| `sample.sequencing_platform` | string / null | 测序平台；无内容时为 `null`。 |

样本信息优先取结构化检测数据；缺失时，仅允许从同一请求、且能够证明属于同一检测快照的菌群报告中补充。

###### 综合摘要字段

| 字段路径 | 类型 | 说明 |
| :--- | :--- | :--- |
| `diagnosis_summary.conclusion` | string / null | 报告全文总结；无内容时为 `null`。 |
| `diagnosis_summary.clinical_context` | object | 个体化临床背景，内部键允许动态扩展；无内容时为 `{}`。 |
| `diagnosis_summary.risk` | string / null | 综合关注程度：`低风险`、`中等风险` 或 `高风险`；无法判断时为 `null`。 |
| `diagnosis_summary.evidence_refs` | array[integer] | 支撑综合摘要的证据序号；每个序号必须能在 `evidence[].number` 中找到。 |

`clinical_context` 可包含“异常菌群概况”“肠型”“健康关注说明”“综合健康画像”等键，实际键由可用检测资料和会话上下文决定。

###### 评估依据字段

| 字段路径 | 类型 | 说明 |
| :--- | :--- | :--- |
| `diagnostic_basis.abnormal_findings` | array | 异常菌群和疾病关联项目列表；无内容时为 `[]`。 |
| `diagnostic_basis.abnormal_findings[].finding_type` | string / null | 项目类型：`microbe`（菌群）或 `disease`（疾病或健康问题）。 |
| `diagnostic_basis.abnormal_findings[].item_id` | string / null | 项目稳定 ID；无内容时为 `null`。 |
| `diagnostic_basis.abnormal_findings[].problem` | string / null | 前端展示的问题名称；无内容时为 `null`。 |
| `diagnostic_basis.abnormal_findings[].priority` | string / null | 项目关注优先级；疾病项目可使用“原报告关联项目”表明其不代表临床诊断。 |
| `diagnostic_basis.abnormal_findings[].indicators` | array | 来源报告指标；无内容时为 `[]`。 |
| `diagnostic_basis.abnormal_findings[].indicators[].name` | string / null | 指标名称；无内容时为 `null`。 |
| `diagnostic_basis.abnormal_findings[].indicators[].value` | string / null | 来源报告检测值及单位；疾病模型指数为检测方原值，不解释为患病概率。 |
| `diagnostic_basis.abnormal_findings[].indicators[].status` | string / null | 来源报告状态或已有检查状态；无内容时为 `null`。 |
| `diagnostic_basis.abnormal_findings[].related_diseases_or_adverse_effects` | array | 与菌群精确绑定的疾病或不良影响，最多 3 项；疾病项目及无合格关联时返回 `[]`。 |
| `diagnostic_basis.abnormal_findings[].related_diseases_or_adverse_effects[].name` | string / null | 相关疾病或不良影响名称；无内容时为 `null`。 |
| `diagnostic_basis.abnormal_findings[].related_diseases_or_adverse_effects[].description` | string / null | 证据能够支持的原子化关联解释；无内容时为 `null`。 |
| `diagnostic_basis.abnormal_findings[].related_diseases_or_adverse_effects[].relation_type` | string / null | 关系类型：`established_pathogen`、`opportunistic`、`epidemiologic_association`、`colonization` 或 `environmental_or_dietary`。 |
| `diagnostic_basis.abnormal_findings[].related_diseases_or_adverse_effects[].evidence_refs` | array[integer] | 仅支撑该菌群关联的证据序号。 |
| `diagnostic_basis.abnormal_findings[].biological_mechanism` | string / null | 经来源事实和证据校验后的医学解读；无内容时为 `null`。 |
| `diagnostic_basis.abnormal_findings[].recommendations` | array[string] | 配套检查及作用；菌群项目通常返回 `[]`，疾病项目最多 3 项。 |
| `diagnostic_basis.abnormal_findings[].tips` | object | 固定提示对象，不允许在对象和数组之间切换。 |
| `diagnostic_basis.abnormal_findings[].tips.special_reminder` | string / null | 特别提醒；无可靠内容时为 `null`。 |
| `diagnostic_basis.abnormal_findings[].tips.practical_goal` | string / null | 可执行目标；无可靠内容时为 `null`。 |
| `diagnostic_basis.abnormal_findings[].interpretation_evidence_refs` | array[integer] | 仅支撑本项目医学解读的证据序号；无内容时为 `[]`。 |
| `diagnostic_basis.abnormal_findings[].recommendation_evidence_refs` | array[integer] | 仅支撑配套检查及作用的证据序号；无内容时为 `[]`。 |
| `diagnostic_basis.abnormal_findings[].evidence_refs` | array[integer] | 兼容字段，为该项目各类证据序号的去重并集。 |
| `diagnostic_basis.abnormal_findings[].evidence_level` | string / null | 该项目采用的最高公开证据等级；无证据时为 `null`。 |
| `diagnostic_basis.evidence_refs` | array[integer] | 支撑整个评估依据的去重证据序号列表。 |

###### 健康建议字段

| 字段路径 | 类型 | 说明 |
| :--- | :--- | :--- |
| `diagnostic_advice.report_interpretation` | array[string] | 报告整体理解；无内容时为 `[]`。 |
| `diagnostic_advice.Plan` | object | 固定时间计划对象；不根据不可靠信息猜测时间计划。 |
| `diagnostic_advice.Plan.未来1-2周` | string / null | 未来 1 至 2 周计划；无可靠内容时为 `null`。 |
| `diagnostic_advice.Plan.未来1个月` | string / null | 未来 1 个月计划；无可靠内容时为 `null`。 |
| `diagnostic_advice.Plan.未来2-3个月` | string / null | 未来 2 至 3 个月计划；无可靠内容时为 `null`。 |
| `diagnostic_advice.Plan.未来6-12个月` | string / null | 未来 6 至 12 个月计划；无可靠内容时为 `null`。 |
| `diagnostic_advice.health_recommendations` | array | 有证据或来源事实支持的健康建议；无内容时为 `[]`。 |
| `diagnostic_advice.health_recommendations[].category` | string | 建议类别：`diet`、`exercise`、`sleep`、`bowel_habits` 或 `general`。 |
| `diagnostic_advice.health_recommendations[].content` | string / null | 建议内容；无内容时为 `null`。 |
| `diagnostic_advice.health_recommendations[].evidence_refs` | array[integer] | 支撑该建议的证据序号；未绑定公开证据时为 `[]`。 |
| `diagnostic_advice.not_recommended_behaviors` | object | 不推荐行为，内部键允许动态扩展；无可靠内容时为 `{}`。 |
| `diagnostic_advice.medical_alerts` | array[string] | 医疗警示；无可靠内容时为 `[]`。 |

###### 证据与错误字段

| 字段路径 | 类型 | 说明 |
| :--- | :--- | :--- |
| `evidence[].number` | integer | 从 1 开始的证据序号，供所有 `evidence_refs` 引用。 |
| `evidence[].evidence_type` | string / null | 证据类型，例如 `guideline`、`literature`、`clinical_trial` 或 `drug_label`。 |
| `evidence[].topic` | string / null | 证据用途主题；无内容时为 `null`。 |
| `evidence[].title` | string / null | 文献或指南题名；无内容时为 `null`。 |
| `evidence[].source` | string / null | 文献来源或发布机构；无内容时为 `null`。 |
| `evidence[].publication_date` | string / null | 发布年份或日期；无内容时为 `null`。 |
| `evidence[].url` | string / null | 证据链接；无内容时为 `null`。 |
| `evidence[].evidence_level` | string / null | 标准化证据等级；无内容时为 `null`。 |
| `evidence[].provider` | string / null | 证据提供方，例如 `evimed_v2`、`pubmed`、`pmc` 或 `official_guideline`。 |
| `evidence[].summary` | string / null | 来源平台返回或公开文本支持的摘要；无内容时为 `null`。 |
| `evidence[].study_type` | string / null | 研究设计或文件类型；无法识别时为 `null`。 |
| `evidence[].authors` | array[string] | 作者列表；无内容时为 `[]`。 |
| `error.error_code` | string / null | 稳定错误码；失败时填写，成功或部分成功时为 `null`。 |
| `error.message` | string / null | 面向调用方的错误说明；失败时填写，成功或部分成功时为 `null`。 |

1. `type` 当前固定为 `evaluate`（评估报告）；`status` 取 `success`、`partial_success` 或 `failed`。`opinion`、`intervene` 等其他类型的示例只作为字段组织参考，不改变本接口的报告类型。
2. 异常菌和原报告疾病项目统一进入 `diagnostic_basis.abnormal_findings`，由 `finding_type` 区分；疾病模型指数只标记为“检测方原值”，不解释为患病概率。
3. 原健康调护建议进入 `diagnostic_advice.health_recommendations`。当前生成流程没有可靠时间计划、禁忌行为或就医警报时，对应固定字段返回 `null`、`{}` 或 `[]`，不猜测内容。
4. 证据在顶层 `evidence.number` 使用从 1 开始的数字序号；所有 `evidence_refs` 直接引用该数字，不再返回 `ev-...` 内部编码。
5. `clinical_context` 和 `not_recommended_behaviors` 的内部键允许动态扩展；其字段本身始终返回。
6. `tips` 固定为对象，不允许在数组和对象之间切换。
7. 异常菌暂时没有合格疾病关联时，`related_diseases_or_adverse_effects` 仍返回 `[]`，但 `biological_mechanism` 会至少解释本次来源检测结果；有直接人体证据的临床角色时，该解读和它的证据序号同时返回，不使用无证据疾病填空。

##### 证据检索与使用约束

- 生产流程只调用 `POST /review/api/v2/literature-guide`，通过 `type=literature|guide` 分轨，不再请求 V1 的 `/review/api/guide` 或 `/review/api/literature`。
- 指南请求设置 `searchBlock=true`。文本块只用于支持具体结论，最终引用仍绑定其父指南标题、制定机构、年份和链接。
- V2 的 `id`、`authors`、`abstract`、`aiSummary`、`studyType`、`impactFactor`、`coreJournals`、`language` 和多来源 `url` 会用于去重、筛选、排序和字段映射；影响因子和核心期刊标签不直接映射为患者可见证据等级。
- 菌种首轮使用精确拉丁名；只有解释后仍没有可用证据时，才追加一次“拉丁名 + 原报告中文名 + 中文临床词”混合检索。不强制使用 `human clinical`、`human gut microbiome` 或 `stool`。
- 菌种-疾病关系检索始终保留准确菌种和疾病两个概念；第二轮使用中文“临床关联”扩展，不强制样本部位。
- 疾病项首轮使用规范名检索，只对未形成直接可展示证据的项目追加中英文混合检索，并将疾病临床意义与诊断/筛查证据分轨管理。
- 补偿检索的触发条件是“结构化解释后没有可公开展示证据”，不是“原始接口有返回”；这可防止无关首页结果错误阻断回退检索。
- 关联性证据不得改写为因果。普通物种不外推特定菌株、毒素或毒力基因；物种结果不采用仅属水平的结论。
- 纯动物、体外、无法对齐目标或非适用人群证据不进入公开报告。
- V2 完成后仍无合格证据的目标才启动联网兜底：PubMed `ESearch → EFetch XML`；存在 PMCID 时只读取 PMC 公开 XML 中与目标直接相关的段落；Crossref 只使用已有 DOI 或标题高度一致的记录补充元数据。
- 不访问、抓取或绕过出版商付费全文。可选配置为 `SHULAN_NCBI_API_KEY`、`SHULAN_NCBI_CONTACT_EMAIL` 和 `SHULAN_CROSSREF_MAILTO`。
- 临床试验仅从 EviMed 文献端返回内容中识别和重分类，不调用未配置的第三方试验源。
- `ASD` 等无法唯一定义的来源项不猜测、不公开展示，只保留在服务端内部审计记录中。

##### 部分成功和失败

`partial_success` 表示来源结构化事实仍已生成，但某个外部证据、解读或个体化文字字段不完整。`partial_reasons` 是稳定机器码，不进入患者报告正文。

当前机器码包括：`SOURCE_COUNT_MISMATCH`、`EVIDENCE_RETRIEVAL_INCOMPLETE`、`EVIDENCE_FILTER_EMPTY`、`TAXON_RELATION_UNRESOLVED`、`EVIDENCE_INTERPRETATION_INCOMPLETE`、`MICROBE_EVIDENCE_INCOMPLETE`、`DISEASE_EVIDENCE_INCOMPLETE`、`DIAGNOSTIC_EVIDENCE_INCOMPLETE`、`MODEL_NARRATIVE_UNAVAILABLE`、`MODEL_NARRATIVE_INCOMPLETE`、`GENERATION_DEADLINE_REACHED`。达到生成截止时，只要来源已成功解析就返回 `partial_success`，不再丢弃已有结构化结果。

失败回调也返回“回调接口”定义的全部字段。其中报告内容使用固定空值，错误信息只进入 `error`：

```json
{
    "userId": "用户唯一ID",
    "sessionId": "346291b2-2166-430f-8352-ec7d9c11e64d",
    "type": "evaluate",
    "status": "failed",
    "generated_at": null,
    "subject": {
        "name": null,
        "gender": null,
        "age": null,
        "report_date": null
    },
    "sample": {
        "sample_id": null,
        "sample_type": null,
        "test_item": null,
        "sample_date": null,
        "report_date": null,
        "sequencing_platform": null
    },
    "diagnosis_summary": {
        "conclusion": null,
        "clinical_context": {},
        "risk": null,
        "evidence_refs": []
    },
    "diagnostic_basis": {
        "abnormal_findings": [],
        "evidence_refs": []
    },
    "diagnostic_advice": {
        "report_interpretation": [],
        "Plan": {
            "未来1-2周": null,
            "未来1个月": null,
            "未来2-3个月": null,
            "未来6-12个月": null
        },
        "health_recommendations": [],
        "not_recommended_behaviors": {},
        "medical_alerts": []
    },
    "evidence": [],
    "partial_reasons": [],
    "error": {
        "error_code": "REPORT_GENERATION_TIMEOUT",
        "message": "报告生成超时，请稍后重试"
    }
}
```

错误响应和回调不得包含堆栈、本地路径、密钥、MongoDB 连接串或 Redis 凭据。

#### 注意事项

1. 接口受理请求后立即返回，不等待报告生成完成。
2. 后台任务总时限为 15 分钟；达到截止时间且来源已解析时返回 `partial_success`，无法生成时返回 `failed`。
3. 成功、部分成功和失败均向配置的回调地址发送同一固定结构的标准 JSON。
4. `sessionId` 查询仅用于补充上下文；查询失败或会话不存在不阻断报告生成。
5. 报告不写入 MongoDB、Redis 或项目目录；生产环境不提供报告查询或 HTML 预览接口。
6. 调用方必须按 `status`、`partial_reasons` 和 `error.error_code` 判断最终结果，并保留未知字段的向前兼容能力。

## AI对话接口

提供普通的 AI 聊天对话功能。与 AI 问答接口不同，本接口是自由对话，没有固定的问题流程。

### 接口总览

| 接口                         | 名称         | 说明                              |
| ---------------------------- | ------------ | --------------------------------- |
| `/api/v1/chat/suggestions`   | 推荐问题接口 | 获取推荐的对话起始问题            |
| `/api/v1/chat/messages/custom` | 自定义消息接口 | 向既有会话插入不参与 AI 上下文的业务卡片 |
| `/api/v1/chat/send/complete` | 消息发送接口 | 发送消息并获取完整 AI 回复        |
| `/api/v1/chat/send/stream`   | 流式输出接口 | 发送消息并获取 AI 流式回复（SSE） |
| `/api/v1/chat/history`       | 历史记录接口 | 获取对话历史记录                  |

### 对话流程

1. 使用新的 `sessionId` 开始新对话，或使用已完成的问答 `sessionId` 继续对话。
2. 反复调用"消息发送接口"进行自由对话。

### 会话关系说明

1. **新建对话会话**：使用全新的 `sessionId`，系统会自动创建普通对话会话。
2. **问答转对话**：显式调用 `/qa/complete` 并得到 `status: finished` 后，可以使用相同的 `sessionId` 继续普通对话。
3. **限制**：问答为 `question` 或 `awaiting_decision` 时，不能使用该 `sessionId` 进行普通对话。
4. **限制**：普通对话的 `sessionId` 不能用于 AI 问答接口。

### 推荐问题接口

#### 接口信息

- **请求路径**：`/api/v1/chat/suggestions`
- **请求方法**：`GET`

#### 请求参数

| 参数名   | 类型   | 必填 | 描述                            |
| :------- | :----- | :--- | :------------------------------ |
| `userId` | string | 否   | 用户ID。提供则返回个性化问题。  |
| `count`  | int    | 否   | 返回问题数量，默认3个，范围1-10 |

#### 请求示例

```shell
curl http://host:port/api/v1/chat/suggestions?userId=user123&count=5
```

#### 响应示例

```json
{
    "code": 200,
    "message": "success",
    "uuid": "A1B2C3D4-E5F6-7890-ABCD-EF1234567890",
    "timestamp": 1781167890,
    "elapsed_ms": 20,
    "data": {
        "suggestions": [
            "如何改善睡眠质量？",
            "日常饮食需要注意什么？",
            "如何制定合理的运动计划？"
        ]
    }
}
```

**data 字段说明**

| 字段          | 类型          | 说明                                    |
| :------------ | :------------ | :-------------------------------------- |
| `suggestions` | array[string] | 推荐问题列表（数量由count参数指定）      |

#### 注意事项

1. 无 `userId` 或用户无问答历史时，返回随机通用问题。
2. 有 `userId` 且用户有已完成的问答会话时，返回基于问答历史的个性化问题。
3. 每次请求无 `userId` 时，返回的问题是随机的，可能不同。
4. `count` 参数必须在 1-10 之间，超出范围会返回 400 错误。

### 自定义消息接口

用于向已结束建档、可进入普通对话阶段的既有会话插入业务卡片，例如医生签发的健康报告、诊断意见或干预方案。用户无需先发送普通聊天消息；建档问答仍在进行时不允许插入。

自定义消息的 `role` 固定为 `custom`，不会进入后续 AI 对话的上下文。

#### 接口信息

- **请求路径**：`/api/v1/chat/messages/custom`
- **请求方法**：`POST`
- **Content-Type**：`application/json`

#### 请求参数

| 参数名 | 类型 | 必填 | 描述 |
|---|---|---|---|
| `userId` | string | 是 | 用户 ID。必须为非空字符串，最长 256 个字符 |
| `sessionId` | string | 是 | 已结束建档、可进入普通对话阶段的既有会话 ID。必须为非空字符串，最长 256 个字符 |
| `eventId` | string | 是 | 由调用方生成的事件幂等键。必须为非空字符串，最长 128 个字符 |
| `displayType` | string | 否 | 展示形式。当前只允许 `CARD`；字段缺失或为 `null` 时默认为 `CARD`，空字符串无效 |
| `customType` | string | 否 | 业务卡片类型。枚举见下表；字段缺失或为 `null` 时默认为 `DOCTOR_MESSAGE_CARD`，空字符串无效 |
| `title` | string | 否 | 卡片标题。字段缺失或为 `null` 时不返回该字段；传入时必须为非空字符串，最长 256 个字符 |
| `content` | string | 是 | 用户可直接阅读的正文。必须为非空字符串，最长 16,384 个字符 |
| `payload` | object | 否 | 卡片关联的结构化数据，例如报告 ID、类型和版本。必须是可序列化的 JSON 对象，序列化后最大 16 KiB；字段缺失或为 `null` 时按 `{}` 处理 |

`userId`、`sessionId`、`eventId` 或 `content` 缺失/为 `null` 时返回 `400`；值为空字符串、 非字符串或超出长度限制时返回 `422`。

请求中的 `role` 不作为输入字段；即使调用方额外传入，服务端仍固定保存为 `custom`。

`customType` 当前只允许以下枚举值：

| 枚举值 | 场景 |
|---|---|
| `DOCTOR_MESSAGE_CARD` | 通用医生主动消息，也是默认值 |
| `HEALTH_REPORT_CARD` | 医生审核并签发的健康报告 |
| `INTERVESE_CARD` | 医生签发的干预方案 |
| `DIAGNOSIS_CARD` | 医生签发的诊断意见 |

`displayType` 决定顶层展示形式，`customType` 决定具体业务卡片样式。客户端应使用 `displayType` 判断展示形式，不应依赖 `customType` 的字符串后缀。

#### 请求示例

```json
{
    "userId": "user123",
    "sessionId": "chat_session_001",
    "eventId": "report-signed-20260814-001",
    "displayType": "CARD",
    "customType": "HEALTH_REPORT_CARD",
    "title": "健康评估报告",
    "content": "您的健康报告已经由医生审核完成，请点击查看详情。",
    "payload": {
        "reportId": "report_001",
        "reportType": "health_report",
        "version": 1
    }
}
```

```bash
curl -X POST http://host:port/api/v1/chat/messages/custom \
  -H "Content-Type: application/json" \
  -d '{"userId":"user123","sessionId":"chat_session_001","eventId":"report-signed-20260814-001","displayType":"CARD","customType":"HEALTH_REPORT_CARD","title":"健康评估报告","content":"您的健康报告已经由医生审核完成，请点击查看详情。","payload":{"reportId":"report_001"}}'
```

#### 响应示例

消息已写入会话历史时返回 HTTP `200`：

```json
{
    "code": 200,
    "message": "success",
    "uuid": "A1B2C3D4-E5F6-7890-ABCD-EF1234567890",
    "timestamp": 1786674600,
    "elapsed_ms": 8,
    "data": {
        "userId": "user123",
        "sessionId": "chat_session_001",
        "eventId": "report-signed-20260814-001",
        "created": true,
        "queued": false,
        "status": "delivered",
        "message": {
            "messageId": "13c63166-0b5f-45ce-b91d-58db56df72f8",
            "eventId": "report-signed-20260814-001",
            "role": "custom",
            "displayType": "CARD",
            "customType": "HEALTH_REPORT_CARD",
            "title": "健康评估报告",
            "content": "您的健康报告已经由医生审核完成，请点击查看详情。",
            "payload": {
                "reportId": "report_001"
            },
            "aiContextMode": "none",
            "timestamp": "2026-08-14T10:30:00+08:00"
        }
    }
}
```

消息已被接收、但尚未写入会话历史时返回 HTTP `202`：

```json
{
    "code": 202,
    "message": "queued",
    "uuid": "A1B2C3D4-E5F6-7890-ABCD-EF1234567890",
    "timestamp": 1786674600,
    "elapsed_ms": 5,
    "data": {
        "userId": "user123",
        "sessionId": "chat_session_001",
        "eventId": "report-signed-20260814-001",
        "created": true,
        "queued": true,
        "status": "pending",
        "message": null
    }
}
```

主要非成功响应示例（建档问答尚未结束）：

```json
{
    "code": 409,
    "message": "问答会话未完成，无法插入自定义消息",
    "uuid": "A1B2C3D4-E5F6-7890-ABCD-EF1234567890",
    "timestamp": 1786674600,
    "elapsed_ms": 4,
    "data": null
}
```

#### 响应字段说明

| 字段 | 类型 | 说明 |
|---|---|---|
| `code` | integer | 业务状态码，与本接口的 HTTP 状态码一致 |
| `message` | string | 响应提示；`200` 为 `success`，`202` 为 `queued`，其他状态为具体错误信息 |
| `uuid` | string | 本次 HTTP 请求的唯一标识 |
| `timestamp` | integer | 响应生成时间，Unix 时间戳（秒） |
| `elapsed_ms` | integer | 本次请求耗时，单位为毫秒 |
| `data` | object / null | 成功或已接收时为状态对象；请求失败时为 `null` |
| `data.userId` | string | 用户 ID |
| `data.sessionId` | string | 会话 ID |
| `data.eventId` | string | 请求中的事件幂等键 |
| `data.created` | boolean | `true` 表示本次请求首次接收该事件；`false` 表示命中已有的同一事件 |
| `data.queued` | boolean | `true` 表示消息尚未写入会话历史；`false` 表示已写入 |
| `data.status` | string | 消息状态：`pending` 表示等待写入，`delivered` 表示已写入会话历史 |
| `data.message` | object / null | `delivered` 时为已写入的完整消息；`pending` 时为 `null` |
| `data.message.messageId` | string | 消息唯一标识 |
| `data.message.eventId` | string | 请求中的事件幂等键 |
| `data.message.role` | string | 固定为 `custom` |
| `data.message.displayType` | string | 展示形式，当前为 `CARD` |
| `data.message.customType` | string | 业务卡片类型 |
| `data.message.title` | string | 卡片标题；请求未传或为 `null` 时该字段缺失 |
| `data.message.content` | string | 用户可直接阅读的正文 |
| `data.message.payload` | object | 卡片关联的结构化数据；请求未传或为 `null` 时返回 `{}` |
| `data.message.aiContextMode` | string | 固定为 `none`，表示该消息不进入 AI 对话上下文 |
| `data.message.timestamp` | string | 消息写入会话历史的 ISO 8601 时间 |

#### 状态码

| HTTP 状态码 | `code` | 含义 |
|---|---|---|
| `200` | `200` | 消息已写入会话历史 |
| `202` | `202` | 消息已被接收，尚未写入会话历史 |
| `400` | `400` | 请求体不是合法 JSON 对象，或必填字段缺失/为 `null` |
| `404` | `404` | `userId` 和 `sessionId` 对应的会话不存在 |
| `409` | `409` | 建档问答未完成、同一 `eventId` 的消息内容不一致，或该事件无法写入会话 |
| `422` | `422` | 字段类型、长度、枚举值或空字符串不符合约束 |
| `500` | `500` | 服务器无法完成请求 |

#### 异步状态与查询

1. HTTP `202` 表示事件已被接收，调用方不应使用新的 `eventId` 重复发送同一业务消息。
2. 本接口不提供回调或实时推送。客户端通过 `POST /api/v1/chat/history` 获取已写入会话的卡片。
3. 调用方可以使用完全相同的请求重新调用本接口查询当前状态：返回 `202` 表示仍为 `pending`，返回 `200` 表示已为 `delivered`，返回 `409` 表示事件无法写入或请求内容与原事件不一致。
4. 本接口当前不承诺固定的完成时限；在返回 `pending` 期间，调用方应保留原始 `eventId` 和请求内容。

#### 注意事项

1. 幂等范围为 `(userId, sessionId, eventId)`。
2. 同一事件的请求字段完全相同时，重复调用不会重复追加消息，并返回 `created=false`。
3. 同一 `eventId` 对应的 `displayType`、`customType`、`title`、`content` 或 `payload` 不一致时返回 `409`。
4. 建档正式结束后即可调用，用户无需先发送普通聊天消息。建档问答仍在进行时返回 `409`。
5. 会话不存在时返回 `404`，且不会创建新会话。
6. 如果 AI 正在为该会话生成回复，自定义消息会在该条完整 AI 回复之后出现在历史消息列表中。
7. `POST /api/v1/chat/history` 返回当前完整历史消息列表，不提供增量游标。

### 消息发送接口

#### 接口信息

- **请求路径**：`/api/v1/chat/send/complete`
- **请求方法**：`POST`
- **Content-Type**：`application/json`

#### 请求参数

| 参数名      | 类型   | 必填 | 描述                         |
| :---------- | :----- | :--- | :--------------------------- |
| `userId`    | string | 是   | 用户ID                       |
| `sessionId` | string | 是   | 会话ID（可以是新的或已存在的）|
| `message`   | string | 是   | 用户消息内容                 |
| `thinking`  | boolean| 否   | 是否启用思考模式，默认为 `true` |

**thinking 参数说明**：
- `true`（默认）：启用思考模式，AI 会先推理后输出，响应中包含 `reasoning` 字段
- `false`：关闭思考模式，AI 直接输出，响应中 `reasoning` 字段为空字符串

#### 请求示例

```json
{
    "userId": "user123",
    "sessionId": "chat_session_001",
    "message": "你好，我想咨询一下健康问题",
    "thinking": true
}
```

```bash
# 启用思考模式（默认）
curl -X POST http://host:port/api/v1/chat/send/complete \
  -H "Content-Type: application/json" \
  -d '{"userId": "user123", "sessionId": "chat_session_001", "message": "你好，我想咨询一下健康问题", "thinking": true}'

# 关闭思考模式
curl -X POST http://host:port/api/v1/chat/send/complete \
  -H "Content-Type: application/json" \
  -d '{"userId": "user123", "sessionId": "chat_session_001", "message": "你好，我想咨询一下健康问题", "thinking": false}'
```

#### 响应示例

```json
{
    "code": 200,
    "message": "success",
    "uuid": "A1B2C3D4-E5F6-7890-ABCD-EF1234567890",
    "timestamp": 1781167890,
    "elapsed_ms": 1500,
    "data": {
        "userId": "user123",
        "sessionId": "chat_session_001",
        "message": "你好，我想咨询一下健康问题",
        "reply": "你好！我是健康助手，很高兴为您服务。请告诉我您想咨询什么健康问题？",
        "reasoning": "用户首次打招呼，需要友好回应并引导用户说明具体问题...",
        "timestamp": "2026-06-22T10:30:45"
    }
}
```

**data 字段说明**

| 字段        | 类型   | 说明                              |
| :---------- | :----- | :-------------------------------- |
| `userId`    | string | 用户ID                            |
| `sessionId` | string | 会话ID                            |
| `message`   | string | 用户发送的消息                    |
| `reply`     | string | AI 的回复内容                     |
| `reasoning` | string | AI 的推理思考过程（启用思考模式） |
| `timestamp` | string | 回复时间                          |

#### 注意事项

1. 此接口返回完整的 AI 回复，不是流式输出。如需流式输出，请使用 `/api/v1/chat/send/stream` 接口。

### 流式输出接口（SSE）

#### 接口信息

- **请求路径**：`/api/v1/chat/send/stream`
- **请求方法**：`POST`
- **Content-Type**：`application/json`
- **响应类型**：`text/event-stream` (SSE)

#### 请求参数

同 *消息发送接口*。

| 参数名      | 类型   | 必填 | 描述                         |
| :---------- | :----- | :--- | :--------------------------- |
| `userId`    | string | 是   | 用户ID                       |
| `sessionId` | string | 是   | 会话ID（可以是新的或已存在的）|
| `message`   | string | 是   | 用户消息内容                 |
| `thinking`  | boolean| 否   | 是否启用思考模式，默认为 `true` |

**thinking 参数说明**：
- `true`（默认）：启用思考模式，流式输出包含 `reasoning` 和 `content` 两种类型的事件
- `false`：关闭思考模式，流式输出只包含 `content` 类型的事件

#### 请求示例

```bash
# 启用思考模式（默认）
curl -X POST http://host:port/api/v1/chat/send/stream \
  -H "Content-Type: application/json" \
  -d '{"userId": "user123", "sessionId": "chat_session_001", "message": "你好，我想咨询一下健康问题", "thinking": true}'

# 关闭思考模式
curl -X POST http://host:port/api/v1/chat/send/stream \
  -H "Content-Type: application/json" \
  -d '{"userId": "user123", "sessionId": "chat_session_001", "message": "你好，我想咨询一下健康问题", "thinking": false}'
```

#### 响应示例

```json
{
    "code": 200,
    "message": "success",
    "uuid": "A1B2C3D4-E5F6-7890-ABCD-EF1234567890",
    "timestamp": 1781167890,
    "elapsed_ms": 1500,
    "data": {
        "type": "start",
        "userId": "userId",
        "sessionId": "chat_session_001"
    }
}
```

**data 字段示例**

完整的流式输出包括五种状态事件，通过`type`字段区分。

**重要说明**：
- 当 `thinking=true` 时，AI 会先进行推理思考，然后输出正文。推理过程和正文内容会分别返回。
- 当 `thinking=false` 时，AI 直接输出正文，不返回推理过程（只有 `content` 事件）。
- 客户端在流式输出完成前断开连接时，服务端会停止向该连接发送 SSE，但会继续生成完整回复并保存本轮用户消息、AI 正文和推理内容。客户端后续可通过历史记录接口获取完整会话；断开连接不代表取消本轮生成。

- 开始事件

  ```json
  {
      "type": "start",
      "userId": "userId",
      "sessionId": "sessionId"
  }
  ```

- 推理过程事件（思考阶段，仅当 `thinking=true` 时出现）

  ```json
  {
      "type": "reasoning",
      "content": "让我分析一下用户的问题...",
      "userId": "userId",
      "sessionId": "sessionId"
  }

  {
      "type": "reasoning",
      "content": "用户可能关注的是...",
      "userId": "userId",
      "sessionId": "sessionId"
  }

  {
      "type": "reasoning",
      "content": "我应该从以下几个方面回答...",
      "userId": "userId",
      "sessionId": "sessionId"
  }
  ```

- 内容片段事件（输出正文阶段）

  ```json
  {
      "type": "content",
      "content": "你",
      "userId": "userId",
      "sessionId": "sessionId"
  }

  {
      "type": "content",
      "content": "好，我",
      "userId": "userId",
      "sessionId": "sessionId"
  }

  {
      "type": "content",
      "content": "是健",
      "userId": "userId",
      "sessionId": "sessionId"
  }

  {
      "type": "content",
      "content": "康助手",
      "userId": "userId",
      "sessionId": "sessionId"
  }
  ```

- 结束事件

  ```json
  {
      "type": "finish",
      "userId": "userId",
      "sessionId": "sessionId"
  }
  ```

- 错误事件（仅在出现异常时出现）

  ```json
  {
      "type": "error",
      "error": "连接超时",
      "userId": "userId",
      "sessionId": "sessionId"
  }
  ```

**data 字段说明**

| 字段        | 类型   | 必含 | 说明                                                         |
| :---------- | :----- | :--- | :----------------------------------------------------------- |
| `type`      | string | 是   | 事件类型：`start`、`reasoning`、`content`、`finish`、`error` |
| `userId`    | string | 是   | 用户ID                                                       |
| `sessionId` | string | 是   | 会话ID                                                       |
| `content`   | string | 否   | 流文本内容（仅当 `type=content` 时存在）                     |
| `error`     | string | 否   | 错误说明（仅当 `type=error` 时存在）                         |

**事件类型说明**：

- `start`：流开始事件，表示AI开始生成回复
- `reasoning`：推理模式，此时`content`字段为推理内容
- `content`：内容片段事件，包含AI回复的文本片段
- `finish`：流结束事件，表示AI回复完成
- `error`：错误事件，表示发生异常

### 历史记录接口

#### 接口信息

- **请求路径**：`/api/v1/chat/history`
- **请求方法**：`POST`
- **Content-Type**：`application/json`

#### 请求参数

| 参数名      | 类型   | 必填 | 描述   |
| :---------- | :----- | :--- | :----- |
| `userId`    | string | 是   | 用户ID |
| `sessionId` | string | 是   | 会话ID |

#### 请求示例

```json
{
    "userId": "user123",
    "sessionId": "chat_session_001"
}
```

```bash
curl -X POST http://host:port/api/v1/chat/history \
  -H "Content-Type: application/json" \
  -d '{"userId": "user123", "sessionId": "chat_session_001"}'
```

#### 响应示例

**纯对话会话**：

```json
{
    "code": 200,
    "message": "success",
    "uuid": "A1B2C3D4-E5F6-7890-ABCD-EF1234567890",
    "timestamp": 1781167890,
    "elapsed_ms": 50,
    "data": {
        "userId": "user123",
        "sessionId": "chat_session_001",
        "type": "chat",
        "createdAt": "2026-06-22T10:00:00",
        "lastMessageAt": "2026-06-22T10:35:00",
        "messages": [
            {
                "messageId": "7ed2ec0e-bfc9-4d19-ac11-f312c59fc070",
                "messageRevision": 1,
                "role": "user",
                "content": "你好，我想咨询一下健康问题",
                "timestamp": "2026-06-22T10:30:00"
            },
            {
                "messageId": "4ccb550f-dad7-46d9-9ba5-0af4a100d621",
                "messageRevision": 1,
                "role": "assistant",
                "content": "你好！我是健康助手，很高兴为您服务。请告诉我您想咨询什么健康问题？",
                "reasoning": "用户首次打招呼，需要友好回应并引导用户说明具体问题...",
                "timestamp": "2026-06-22T10:30:15"
            },
            {
                "messageId": "13c63166-0b5f-45ce-b91d-58db56df72f8",
                "eventId": "report-signed-20260814-001",
                "role": "custom",
                "displayType": "CARD",
                "customType": "HEALTH_REPORT_CARD",
                "title": "健康评估报告",
                "content": "您的健康报告已经由医生审核完成，请点击查看详情。",
                "payload": {
                    "reportId": "report_001"
                },
                "aiContextMode": "none",
                "timestamp": "2026-06-22T10:32:00"
            },
            {
                "messageId": "a434c2f6-8328-430d-b4e7-ea0a245455fb",
                "messageRevision": 1,
                "role": "user",
                "content": "我最近睡眠不太好",
                "timestamp": "2026-06-22T10:35:00"
            },
            {
                "messageId": "3507d6fe-65a2-4a30-9d3e-4785600c7e9b",
                "messageRevision": 1,
                "role": "assistant",
                "content": "睡眠问题确实会影响健康。请问您具体是入睡困难，还是容易醒来？",
                "reasoning": "用户提到睡眠问题，需要进一步了解具体症状以便给出准确建议...",
                "timestamp": "2026-06-22T10:35:10"
            }
        ]
    }
}
```

**说明**：
- `messages` 数组中的 `assistant` 角色消息包含 `reasoning` 字段（AI 的推理思考过程）
- `user` 角色消息只有 `content` 字段，没有 `reasoning` 字段
- `custom` 角色表示业务卡片；它可以包含 `eventId`、`payload` 和 `aiContextMode=none`，不会进入模型上下文
- 新产生的 `user`、`assistant` 和 QA 转换 `system` 消息包含稳定的 `messageId` 与 `messageRevision`；历史旧消息可能缺少这两个字段

**从问答转换的对话会话**：

```json
{
    "code": 200,
    "message": "success",
    "uuid": "A1B2C3D4-E5F6-7890-ABCD-EF1234567890",
    "timestamp": 1781167890,
    "elapsed_ms": 50,
    "data": {
        "userId": "user123",
        "sessionId": "session456",
        "type": "chat",
        "createdAt": "2026-06-22T09:00:00",
        "lastMessageAt": "2026-06-22T10:35:00",
        "messages": [
            {
                "messageId": "70be4813-7fa0-43dc-9bbd-a8d2431b184b",
                "messageRevision": 1,
                "role": "system",
                "content": "以下是之前的问答建档信息：\n用户基本信息：name=张三、gender=男、age=30...\n\n现在用户希望与你进行自由对话。",
                "timestamp": "2026-06-22T09:15:00"
            },
            {
                "messageId": "a68824c7-2329-4ab0-9421-1d9a96a1c942",
                "messageRevision": 1,
                "role": "user",
                "content": "根据我的体检报告，有什么建议吗？",
                "timestamp": "2026-06-22T10:30:00"
            },
            {
                "messageId": "62d914d6-fd40-4323-a56d-e801ec64ced7",
                "messageRevision": 1,
                "role": "assistant",
                "content": "根据您的体检报告和问答信息，我建议...",
                "timestamp": "2026-06-22T10:30:20"
            }
        ],
        "qaHistory": [
            {
                "question": {
                    "questionId": "q_sleep",
                    "questionType": "radio",
                    "question": "您最近的睡眠情况如何？"
                },
                "answer": [
                    "睡眠较差"
                ],
                "answeredAt": "2026-06-22T09:05:00"
            }
        ]
    }
}
```

**data 字段说明**

| 字段            | 类型   | 说明                                                    |
| :-------------- | :----- | :------------------------------------------------------ |
| `userId`        | string | 用户ID                                                  |
| `sessionId`     | string | 会话ID                                                  |
| `type`          | string | 会话类型，固定为 `chat`                                 |
| `createdAt`     | string | 会话创建时间                                            |
| `lastMessageAt` | string | 最后一条消息时间                                        |
| `messages`      | array  | 消息历史记录数组                                        |
| `messages[].role` | string | 消息角色：`user`（用户）/ `assistant`（AI）/ `system`（系统）/ `custom`（业务卡片）|
| `messages[].content` | string | 消息内容                                         |
| `messages[].timestamp` | string | 消息时间                                       |
| `messages[].messageId` | string | 消息唯一 ID；新消息均返回，历史旧消息可能缺失 |
| `messages[].messageRevision` | integer | 消息结构修订号；新产生的 `user`、`assistant` 和 QA 转换 `system` 消息当前为 `1`，旧消息或 `custom` 消息可能缺失 |
| `messages[].eventId` | string | 上游幂等事件 ID，仅 `custom` 消息必有 |
| `messages[].payload` | object | 卡片展示快照和权威资源引用，仅 `custom` 消息使用 |
| `messages[].aiContextMode` | string | 是否参与 AI 上下文；第一版 `custom` 固定为 `none` |
| `qaHistory`     | array  | 问答历史（仅当从问答转换来时存在）                      |

#### 注意事项

1. 只能获取 `type` 为 `chat` 的对话会话历史。
2. 如果是纯问答会话（未转换），请使用 `/api/v1/qa/history` 接口。
3. 从问答转换的对话会话会包含 `qaHistory` 字段，记录原问答历史。



## 文件限制

### 支持的文件格式

| 文件类型           | 格式                                                         |
| ------------------ | ------------------------------------------------------------ |
| PDF                | `.pdf`                                                       |
| 图片               | `.jpg`、`.jpeg`、`.png`、`.bmp`、`.gif`                      |

### 最大文件大小

100MB。



## 实时语音输入接口

实时语音输入使用 WebSocket 协议。前端边录音边发送音频二进制分片，后端持续返回识别文本。

> **语音输入接口的输入输出数据结构与其他接口不同，请注意区分。**

### 接口信息

- **请求路径**：`/realtime`
- **请求方法**：`WebSocket`
- **Base URL**：`ws://staic.evimed.com:8454/inputbase/voice-input`

### 音频格式要求

| 参数     | 值                |
| -------- | ----------------- |
| 采样率   | 16kHz             |
|声道     | 单声道            |
| 编码格式 | PCM16             |
| 数据格式 | 二进制 ArrayBuffer |

**重要提示**：

- 前端不要直接发送 `MediaRecorder` 生成的 `audio/webm` 分片
- 需要通过 `AudioContext` 或 `AudioWorklet` 转成 16k 单声道 PCM16 后再发送
- 建议每个音频分片时长：100ms 到 300ms

### 消息格式

#### 前端发送消息

**1. 开始识别（第一条消息必须是此 JSON）**

```json
{
    "type": "start",
    "language": "zh-CN",
    "mode": "chat_input",
    "target": "chat-input",
    "context": "",
    "insert_mode": "stream"
}
```

| 参数名        | 类型   | 必填 | 说明                                                      |
| ------------- | ------ | ---- | --------------------------------------------------------- |
| `type`        | string | 是   | 固定为 `start`                                            |
| `language`    | string | 否   | 语言代码，默认 `zh-CN`                                    |
| `mode`        | string | 否   | 模式：`chat_input`、`dictation`、`command`，默认 `chat_input` |
| `target`      | string | 否   | 前端输入框标识，后端透传                                  |
| `context`     | string | 否   | 可选上下文                                                |
| `insert_mode` | string | 否   | 固定传 `stream`                                           |

**2. 发送音频数据**

开始识别后，持续发送音频二进制数据：

```javascript
socket.send(pcm16ArrayBuffer);  // ArrayBuffer 类型
```

**3. 停止识别**

```json
{
    "type": "stop"
}
```

**4. 取消识别**

```json
{
    "type": "cancel"
}
```

#### 后端返回事件

WebSocket 返回 JSON 文本消息，包含以下事件类型：

| 事件类型              | 说明                           | 前端处理建议                                    |
| --------------------- | ------------------------------ | ----------------------------------------------- |
| `session.created`     | 会话创建成功                   | 记录 session_id                                 |
| `status`              | 状态更新                       | 可选处理                                        |
| `provider.task_started` | ASR 任务启动                 | 可选处理                                        |
| `audio.accepted`      | 音频分片已接收                 | 可选处理（用于监控传输状态）                    |
| `insert.delta`        | 增量文本片段                   | **核心**：追加 `delta` 到输入框，或用 `text` 覆盖 |
| `insert.replace`      | ASR 中间结果修正               | **核心**：必须用 `text` 覆盖当前识别文本        |
| `transcript.partial`  | 部分识别结果                   | 可选展示                                        |
| `transcript.final`    | 最终识别结果                   | 可选处理                                        |
| `insert.done`         | 识别完成                       | **核心**：最终文本确认完成                      |
| `error`               | 错误信息                       | **核心**：展示错误并允许重试                    |

### 事件数据结构

#### session.created

```json
{
    "type": "session.created",
    "session_id": "uuid-string",
    "provider": "dashscope_fun_asr",
    "insert_mode": "stream",
    "message": "会话已创建"
}
```

#### insert.delta

```json
{
    "type": "insert.delta",
    "session_id": "uuid-string",
    "delta": "新增的文字",
    "text": "完整的当前文本",
    "is_final": false
}
```

#### insert.replace

```json
{
    "type": "insert.replace",
    "session_id": "uuid-string",
    "text": "修正后的完整文本",
    "is_final": false
}
```

#### insert.done

```json
{
    "type": "insert.done",
    "session_id": "uuid-string",
    "text": "最终确认的完整文本",
    "language": "zh-CN",
    "chunk_count": 45,
    "total_bytes": 144000,
    "provider": "dashscope_fun_asr"
}
```

#### error

```json
{
    "type": "error",
    "code": "voice_input.provider_error",
    "message": "ASR 服务调用失败"
}
```

### 前端示例

```javascript
const socket = new WebSocket("ws://staic.evimed.com:8454/inputbase/voice-input/realtime");

let currentText = "";

// 1. 连接建立后发送开始消息
socket.onopen = () => {
  socket.send(JSON.stringify({
    type: "start",
    language: "zh-CN",
    mode: "chat_input",
    target: "chat-input",
    insert_mode: "stream",
  }));
};

// 2. 接收识别结果
socket.onmessage = (message) => {
  const event = JSON.parse(message.data);

  if (event.type === "insert.delta") {
    // 增量更新
    currentText = event.text;
    inputElement.value = currentText;
  }

  if (event.type === "insert.replace") {
    // 替换修正
    currentText = event.text;
    inputElement.value = currentText;
  }

  if (event.type === "insert.done") {
    // 识别完成
    inputElement.value = event.text;
  }

  if (event.type === "error") {
    // 错误处理
    console.error("语音识别错误:", event.message);
  }
};

// 3. 发送音频数据
function sendPcmChunk(pcm16ArrayBuffer) {
  if (socket.readyState === WebSocket.OPEN) {
    socket.send(pcm16ArrayBuffer);
  }
}

// 4. 停止识别
function stopRecording() {
  socket.send(JSON.stringify({ type: "stop" }));
}

// 5. 取消识别
function cancelRecording() {
  socket.send(JSON.stringify({ type: "cancel" }));
}
```

### 常见错误码

| code                              | 说明                         |
| --------------------------------- | ---------------------------- |
| `voice_input.bad_request`         | 请求参数错误                 |
| `voice_input.provider_unavailable` | ASR provider 未配置或不可用 |
| `voice_input.provider_error`      | ASR provider 调用失败       |

### 注意事项

1. **音频格式**：务必确保音频格式符合要求（16kHz 单声道 PCM16），否则识别效果会受影响
2. **分片大小**：建议每个音频分片时长 100-300ms，过短或过长都会影响识别效果
3. **网络稳定性**：WebSocket 连接需要保持稳定，建议实现断线重连机制
4. **事件处理**：前端必须处理 `insert.delta`、`insert.replace`、`insert.done` 和 `error` 四种核心事件
5. **资源释放**：识别完成后及时发送 `stop` 消息并关闭 WebSocket 连接
