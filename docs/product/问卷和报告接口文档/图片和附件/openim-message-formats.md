# OpenIM 文本、图片、文件消息格式

本文仅说明 **文本（`101`）**、**图片（`102`）** 和 **文件（`105`）** 消息。

> `content` 直接是纯文本或资源 URL 字符串，不需要 `JSON.parse`。

## 1. 消息位置与核心字段

消息数组位于 `results.data.chatLogs`，每条记录的结构为：

```text
{
  chatLog: { ... },
  isRevoked: boolean
}
```

| 字段 | 类型 | 说明 |
| --- | --- | --- |
| `chatLog.contentType` | number | 消息类型，决定 `content` 的解析方式。 |
| `chatLog.content` | string | 纯文本或资源 URL。 |
| `chatLog.serverMsgID` | string | 服务端消息 ID，跨页合并时优先使用。 |
| `chatLog.clientMsgID` | string | 客户端消息 ID，可用于辅助去重。 |
| `chatLog.sendTime` | number | 发送时间，Unix **毫秒时间戳**。 |
| `isRevoked` | boolean | 是否已撤回，位于 `chatLog` 外部。 |

## 2. 三类消息

| `contentType` | 类型 | `content` 格式 | 处理方式 |
| --- | --- | --- | --- |
| `101` | 文本 | 纯文本字符串 | 直接展示，保留换行。 |
| `102` | 图片 | 图片 URL 字符串 | 使用完整 URL 加载图片。 |
| `105` | 文件 | 文件 URL 字符串 | 使用完整 URL 下载或打开文件。 |

### 2.1 文本

```json
{
  "contentType": 101,
  "content": "你好"
}
```

### 2.2 图片

```json
{
  "contentType": 102,
  "content": "https://im.example.com/api/v1/file/download?objectName=demo.jpg"
}
```

### 2.3 文件

```json
{
  "contentType": 105,
  "content": "https://im.example.com/api/v1/file/download?objectName=report.pdf"
}
```

## 3. 图片和文件 URL

- 加载或下载时使用原始完整 `content`，不要去掉查询参数或重复编码整个 URL。
- 当前 URL 路径以 `/download` 结尾，文件名或存储对象名位于 **`objectName` 查询参数**中，不要从路径末段提取。
- 使用标准 URL 解析器读取 `objectName`，仅对参数值做一次百分号解码，用于展示或作为下载文件名建议。
- `objectName` 可能是服务端存储名，不一定是用户上传时的原始文件名。
- `content` 为空、URL 无法解析或资源加载失败时，应显示单条异常占位，不影响其他消息。

## 4. 解析注意事项

- 根据 `contentType` 进入对应的文本、图片或文件分支，不要根据 URL 后缀判断消息类型。
- `isRevoked=true` 时建议显示“消息已撤回”，不再展示正文或提供附件下载入口。
- 消息 ID 按字符串处理；跨页合并时优先使用 `serverMsgID`，`clientMsgID` 作为辅助键，不要使用资源 URL 作为消息 ID。

## 参考资料

- [OpenIM 消息内容类型](https://docs.openim.io/zh/platform-api/message/message-content-types)
