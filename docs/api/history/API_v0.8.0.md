# API 文档

> **Version**: 0.8.0（test）
>
> **Last Updated**: 2026年6月22日
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
| 413  | 文件大小超过限制                | 文件大小超过限制 |
| 415  | 不支持的文件格式                | 不支持的文件格式 |
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

| 参数名   | 类型   | 必填 | 描述                       |
| :------- | :----- | :--- | :------------------------- |
| `fileId` | string | 是   | 为体检报告文件的网络地址。 |
| `userId` | string | 是   | 用户唯一ID。               |

#### 请求示例

```json
{
    "fileId": "https://m.slmc.top/api/v1/file/preview?objectName=5a1a42b1890056a1c8a28cef71b0a922.pdf",
    "userId": "用户唯一ID"
}
```

```bash
curl -X POST http://host:port/api/v1/interpret/health-report \
  -H "Content-Type: application/json" \
  -d '{"fileId": "https://m.slmc.top/api/v1/file/preview?objectName=5a1a42b1890056a1c8a28cef71b0a922.pdf", "userId": "用户唯一ID"}'
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
        "reportType": "lab | imaging | checkup | unknown",
        "title": "报告名称",
        "reportTime": "报告时间",
        "aiSummary": "AI 解读",
        "status": "normal | abnormal | pending",
        "expansion": "特殊结构，根据报告类型区分"
    }
}
```

##### data 字段说明（通用结构）

| 字段       | 类型              | 说明                                                         |
| ---------- | ----------------- | ------------------------------------------------------------ |
| reportType | string            | 报告类型：`lab`（检验报告）、`imaging`（影像报告）、`checkup`（综合体检报告）、`unknown`（无法确定）。 |
| title      | string or null    | 报告名称。                                                   |
| reportTime | string or null    | 报告时间，尽量保留原文格式。                                 |
| aiSummary  | string or null    | 对报告的整体 AI 解读。                                       |
| status     | string            | 总体状态：`normal`（正常）、`abnormal`（存在异常）、`pending`（无法判断）。 |
| expansion  | object/array/null | 报告类型对应的特殊结构，详情见下文。                         |

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



## AI问答接口

### 接口总览

| 接口                | 名称         | 说明                                             |
| ------------------- | ------------ | ------------------------------------------------ |
| `/api/v1/qa/create` | 会话创建接口 | 开始问答会话，并获取第一个问题                   |
| `/api/v1/qa/answer` | 答案提交接口 | 提交前一个问题的答案，并获取后续问题             |
| `/api/v1/qa/status` | 状态获取接口 | 获取当前会话状态，并返回后续问题（用于断点续答） |
| `/api/v1/qa/history` | 历史记录接口 | 获取已完成会话的完整问答历史记录                 |

### 问答流程

1. 请求“会话创建接口”，开始本轮简单问答，并获取第一个问题。
2. 请求“答案提交接口”，提交上一个问题的答案，并获取下一个问题。
3. 重复步骤二，直到问答结束。

> 问答中断，可以通过“状态获取接口”接着上一个问题继续问答，而不必重新开始创建会话。

### 会话创建接口

#### 接口信息

- **请求路径**：`/api/v1/qa/create`
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

```bash
curl -X POST http://host:port/api/v1/qa/create \
  -H "Content-Type: application/json" \
  -d '{"userId": "userId", "sessionId": "sessionId"}'
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
        "userId": "userId",
        "sessionId": "sessionId",
        "status": "question",
        "progress": "10%",
        "questionId": "questionId",
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

**data 字段说明**

| 字段         | 类型                    | 说明                                  |
| ------------ | ----------------------- | ------------------------------------- |
| userId       | string                  | 用户ID                                |
| sessionId    | string                  | 会话ID                                |
| status       | string                  | 会话状态（`question`|`finished`）     |
| progress     | string                  | 建档进度                              |
| questionId   | string                  | 问题ID                                |
| question     | string                  | 问题                                  |
| questionType | string                  | 题目类型（`text`|`radio`|`checkbox`） |
| options      | object or array[string] | 可选答案                              |

1. `status`字段表示会话状态：`question`（继续问答）或 `finished`（问答结束）。

2. `questionType`字段表示题目类型，提供`text`（文本）、`radio`（单选）、`checkbox`（多选）三种类型。

   不同的题目类型使`options`字段的类型也不相同。详情见下文。

##### `questionType: text`

`questionType`字段值为`text`的情况下，`options`为键值对。其中 key 为字段名，value 为该字段的默认值，可供用户确认或修改，答案提交时须一并返回。

```json
{
    "userId": "userId",
    "sessionId": "sessionId",
    "status": "question",
    "progress": "10%",
    "questionId": "questionId",
    "questionType": "text",
    "question": "请确认您的基本信息是否正确？",
    "options": {
        "name": "张三",
        "gender": "男",
        "age": "25",
        "height": "175",
        "weight": "70"
    }
}
```

##### `questionType: radio|checkbox`

`questionType`字段值为`radio`或`checkbox`的情况下，`options`为字符串数组。

```json
{
    "userId": "userId",
    "sessionId": "sessionId",
    "status": "question",
    "progress": "26%",
    "questionId": "questionId",
    "questionType": "checkbox",
    "question": "识别到您的肠道菌群存在失衡的问题，请问您有没有以下肠道症状呢？",
    "options": [
        "腹痛",
        "腹泻",
        "便秘",
        "大便不成形",
        "反酸 / 嗳气"
    ]
}
```

##### `status: finished`

最后一个问题提交后，返回`status`字段为`finished`，表示问答结束。此状态下不再返回 `questionId`、`question`、`questionType`、`options` 字段。

```json
{
    "userId": "userId",
    "sessionId": "sessionId",
    "status": "finished",
    "progress": "100%"
}
```

### 答案提交接口

#### 接口信息

- **请求路径**：`/api/v1/qa/answer`
- **请求方法**：`POST`
- **Content-Type**：`application/json`

#### 请求参数

| 参数名       | 类型                    | 必填 | 描述                                               |
| :----------- | :---------------------- | :--- | :------------------------------------------------- |
| `userId`     | string                  | 是   | 用户ID                                             |
| `sessionId`  | string                  | 是   | 会话ID                                             |
| `questionId` | string                  | 是   | 问题ID                                             |
| `answer`     | object or array[string] | 否   | 用户回答。`answer` 和 `other` 至少提供其中一个。   |
| `other`      | string                  | 否   | 用户自定义回答。`answer` 和 `other` 至少提供其中一个。 |

1. `answer`字段类型同问题类型（`questionType`）相同。`questionType: text`为`object`类型，`questionType: radio|checkbox`为`array[string]`类型。其中，`radio` 类型的 `answer` 数组长度为 1，`checkbox` 类型可以为多个。
2. `other`字段为用户自定义回答。若同时提供 `answer` 和 `other`，则优先使用 `other`，AI 会忽略 `answer` 字段内容。

#### 请求示例

##### `questionType: text`

```json
{
    "userId": "userId",
    "sessionId": "sessionId",
    "questionId": "questionId",
    "answer": {
        "name": "张三",
        "gender": "男",
        "age": "18",
        "height": "180",
        "weight": "100"
    }
}
```

```shell
curl -X POST http://host:port/api/v1/qa/answer \
  -H "Content-Type: application/json" \
  -d '{"userId": "userId", "sessionId": "sessionId", "questionId": "questionId", "answer": {"name": "张三", "gender": "男", "age": "18", "height": "180", "weight": "100"}}'
```

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

用于获取已完成会话的完整问答历史记录。只有状态为 `finished` 的会话才能获取历史记录。

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
                "questionId": "basic_info_confirm",
                "question": "请确认或补充您的基本信息",
                "questionType": "text",
                "answer": {
                    "name": "张三",
                    "gender": "男",
                    "age": "30",
                    "height": "175",
                    "weight": "70"
                },
                "answeredAt": "2026-06-17 10:01:23"
            },
            {
                "questionId": "q_2",
                "question": "您平均每天睡眠时间是多少？",
                "questionType": "radio",
                "answer": "7-8小时",
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

#### 响应字段说明

| 字段            | 类型   | 说明                                               |
| :-------------- | :----- | :------------------------------------------------- |
| `userId`        | string | 用户ID                                             |
| `sessionId`     | string | 会话ID                                             |
| `status`        | string | 会话状态，固定为 `finished`                        |
| `createdAt`     | string | 会话创建时间                                       |
| `completedAt`   | string | 会话完成时间                                       |
| `history`       | array  | 问答历史记录数组                                   |
| `history[].questionId`   | string | 问题ID                              |
| `history[].question`     | string | 问题内容                            |
| `history[].questionType` | string | 问题类型：`text`/`radio`/`checkbox` |
| `history[].answer`       | object/string/array | 用户的答案           |
| `history[].answeredAt`   | string | 回答时间                            |

#### 答案格式说明

根据 `questionType` 不同，`answer` 字段的格式也不同：

1. **text 类型**：`answer` 为对象（基本信息）或字符串（其他补充信息）
   ```json
   // 基本信息问题

   {
       "answer": {
           "name": "张三",
           "gender": "男",
           "age": "30",
           "height": "175",
           "weight": "70"
       }
   }

   // 其他文本问题

   {
       "answer": "用户填写的内容"
   }
   ```

2. **radio 类型**：`answer` 为字符串（单选项）
   ```json
   {
       "answer": "7-8小时"
   }
   ```

3. **checkbox 类型**：`answer` 为数组（多个选项）
   ```json
   {
       "answer": [
           "头痛",
           "失眠"
       ]
   }
   ```

#### 错误响应

**会话不存在或未完成**：

```json
{
    "code": 404,
    "message": "会话不存在或未完成",
    "uuid": "A1B2C3D4-E5F6-7890-ABCD-EF1234567890",
    "timestamp": 1781167890,
    "elapsed_ms": 10,
    "data": null
}
```

#### 注意事项

1. 只有 `status` 为 `finished` 的会话才能获取历史记录。
2. 如果会话正在进行中，请使用 `/api/v1/qa/status` 接口。
3. 历史记录按问答顺序排列，第一条记录为基本信息确认问题。



## 最终报告接口

本接口用于生成用户的最终健康报告。接口接收请求后立即返回响应（状态码 200），报告生成工作在后台异步进行。生成完成后，系统会主动调用回调接口发送完整报告数据。

#### 接口信息

- **请求路径**：`/api/v1/report`
- **请求方法**：`POST`
- **Content-Type**：`application/json`

#### 请求参数

 参数名                | 类型          | 必填 | 描述
 :-------------------- | :------------ | :--- | :---------------
 userId                | string        | 是   | 用户ID
 name                  | string        | 否   | 患者真实姓名
 fileList              | array[object] | 是   | 体检图片/报告
 hospitalData          | array[object] | 是   | 院内数据
 weRunData             | array[object] | 是   | 微信运动数据格式
 doctorProductPackages | array[object] | 是   | 医生产品
 smartDeviceData       | object        | 是   | 华为智能设备


> 请求数据详情见 *灵犀树兰对接文档*。

#### 请求示例

```json
{
    "userId": "user_12345",
    "sessionId": "session_67890",
    "name": "张三",
    "fileList": [
        {
            "url": "https://m.slmc.top/api/v1/file/preview?objectName=5a1a42b1890056a1c8a28cef71b0a922.pdf",
            "fileType": "pdf"
        }
    ],
    "hospitalData": [],
    "weRunData": [],
    "doctorProductPackages": [],
    "smartDeviceData": {
        "temperature": []
    }
}
```

```bash
curl -X POST http://host:port/api/v1/report \
  -H "Content-Type: application/json" \
  -d '{"userId": "user_12345", "sessionId": "session_67890"}'
```

#### 响应示例

**成功响应（接收成功，后台生成中）**：

```json
{
    "code": 200,
    "message": "报告生成任务已启动",
    "uuid": "10FC484B-2F47-48D1-8ADB-BA463B140E6A",
    "timestamp": 1781167812,
    "elapsed_ms": 50,
    "data": {
        "userId": "user_12345",
        "sessionId": "session_67890",
        "status": "processing",
        "message": "报告正在后台生成中，完成后将通过回调接口发送"
    }
}
```

**data 字段说明**

| 字段        | 类型   | 说明                                               |
| :---------- | :----- | :------------------------------------------------- |
| `userId`    | string | 用户ID。                                           |
| `sessionId` | string | 会话ID。                                           |
| `status`    | string | 任务状态（`processing`: 处理中）。                 |
| `message`   | string | 状态描述信息。                                     |

**错误响应示例**：

```json
{
    "code": 400,
    "message": "参数错误",
    "uuid": "10FC484B-2F47-48D1-8ADB-BA463B140E6A",
    "timestamp": 1781167812,
    "elapsed_ms": 10,
    "data": null
}
```

```json
{
    "code": 404,
    "message": "会话不存在或未完成",
    "uuid": "10FC484B-2F47-48D1-8ADB-BA463B140E6A",
    "timestamp": 1781167812,
    "elapsed_ms": 20,
    "data": null
}
```

```json
{
    "code": 500,
    "message": "报告生成失败",
    "uuid": "10FC484B-2F47-48D1-8ADB-BA463B140E6A",
    "timestamp": 1781167812,
    "elapsed_ms": 100,
    "data": null
}
```

#### 回调接口

> 请求数据详情见 *灵犀树兰对接文档*。

#### 注意事项

1. 接口接收请求后立即返回，不等待报告生成完成。
2. 报告生成在后台异步进行，耗时可能较长（通常 30 秒至 2 分钟）。
3. 报告生成完成后，系统会向 `callbackUrl` 发送完整报告数据。
4. 请确保 `callbackUrl` 可访问，并能正确处理 POST 请求。
5. `sessionId` 必须对应一个已完成的建档问答会话（`status: finished`）。
6. `doctorProductPackages` 参数为可选，如不提供则报告中不包含营养补充方案。



## AI对话接口

提供普通的 AI 聊天对话功能。与 AI 问答接口不同，本接口是自由对话，没有固定的问题流程。

### 接口总览

| 接口                      | 名称         | 说明                                         |
| ------------------------- | ------------ | -------------------------------------------- |
| `/api/v1/chat/suggestions`| 推荐问题接口 | 获取推荐的对话起始问题                       |
| `/api/v1/chat/send`       | 消息发送接口 | 发送消息并获取 AI 回复                       |
| `/api/v1/chat/history`    | 历史记录接口 | 获取对话历史记录                             |

### 对话流程

1. 使用新的 `sessionId` 开始新对话，或使用已完成的问答 `sessionId` 继续对话。
2. 反复调用"消息发送接口"进行自由对话。

### 会话关系说明

1. **新建对话会话**：使用全新的 `sessionId`，系统会自动创建普通对话会话。
2. **问答转对话**：在 AI 问答完成后（`status: finished`），可以使用相同的 `sessionId` 进行普通对话。系统会自动将问答历史作为上下文，继续对话。
3. **限制**：问答未完成时（`status: question`），不能使用该 `sessionId` 进行普通对话。
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

### 消息发送接口

#### 接口信息

- **请求路径**：`/api/v1/chat/send`
- **请求方法**：`POST`
- **Content-Type**：`application/json`

#### 请求参数

| 参数名      | 类型   | 必填 | 描述                         |
| :---------- | :----- | :--- | :--------------------------- |
| `userId`    | string | 是   | 用户ID                       |
| `sessionId` | string | 是   | 会话ID（可以是新的或已存在的）|
| `message`   | string | 是   | 用户消息内容                 |

#### 请求示例

```json
{
    "userId": "user123",
    "sessionId": "chat_session_001",
    "message": "你好，我想咨询一下健康问题"
}
```

```bash
curl -X POST http://host:port/api/v1/chat/send \
  -H "Content-Type: application/json" \
  -d '{"userId": "user123", "sessionId": "chat_session_001", "message": "你好，我想咨询一下健康问题"}'
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
        "timestamp": "2026-06-22T10:30:45"
    }
}
```

**data 字段说明**

| 字段        | 类型   | 说明                     |
| :---------- | :----- | :----------------------- |
| `userId`    | string | 用户ID                   |
| `sessionId` | string | 会话ID                   |
| `message`   | string | 用户发送的消息           |
| `reply`     | string | AI 的回复内容            |
| `timestamp` | string | 回复时间                 |

#### 注意事项

1. 使用问答接口相同的session对话，需要问答完成方可使用。

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
                "role": "user",
                "content": "你好，我想咨询一下健康问题",
                "timestamp": "2026-06-22T10:30:00"
            },
            {
                "role": "assistant",
                "content": "你好！我是健康助手，很高兴为您服务。请告诉我您想咨询什么健康问题？",
                "timestamp": "2026-06-22T10:30:15"
            },
            {
                "role": "user",
                "content": "我最近睡眠不太好",
                "timestamp": "2026-06-22T10:35:00"
            },
            {
                "role": "assistant",
                "content": "睡眠问题确实会影响健康。请问您具体是入睡困难，还是容易醒来？",
                "timestamp": "2026-06-22T10:35:10"
            }
        ]
    }
}
```

**从问答转换的对话会话**：

```json
{
    "code": 200,
    "message": "success",
    "uuid": "A1B2C3D4-E5F6-7890-ABCD-EF1234567890",
    "timestamp": 1781167890,
    "elapsed_ms": 50,
    "data":{
        "userId": "user123",
        "sessionId": "session456",
        "type": "chat",
        "createdAt": "2026-06-22T09:00:00",
        "lastMessageAt": "2026-06-22T10:35:00",
        "messages":[
            {
                "role": "system",
                "content": "以下是之前的问答建档信息：\n用户基本信息：name=张三、gender=男、age=30...\n\n现在用户希望与你进行自由对话。",
                "timestamp": "2026-06-22T09:15:00"
            },
            {
                "role": "user",
                "content": "根据我的体检报告，有什么建议吗？",
                "timestamp": "2026-06-22T10:30:00"
            },
            {
                "role": "assistant",
                "content": "根据您的体检报告和问答信息，我建议...",
                "timestamp": "2026-06-22T10:30:20"
            }
        ],
        "qaHistory":[
            {
                "question":{
                    ...
                },
                "answer":{
                    ...
                },
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
| `messages[].role` | string | 消息角色：`user`（用户）/ `assistant`（AI）/ `system`（系统）|
| `messages[].content` | string | 消息内容                                         |
| `messages[].timestamp` | string | 消息时间                                       |
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

