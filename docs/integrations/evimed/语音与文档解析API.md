# 语音输入与文档解析接口文档

> **Status**: 部署与联调参考，不作为当前 API 契约
>
> **Current Contract**: `docs/api/API.md` 0.13.0

当前接口的完整公开字段、事件和错误语义已经并入 `docs/api/API.md`。本文保留独立服务的部署与联调信息；调用方完成接口接入只需阅读 `API.md`，两份文档不一致时以 `API.md` 为准。

## 1. 基础信息

当前部署服务器：

```text
43.248.117.129
```

前端正式调用建议走 Nginx：

```text
HTTP Base URL: http://staic.evimed.com:8454/inputbase/voice-input
WS Base URL:   ws://staic.evimed.com:8454/inputbase/voice-input
```

服务端本机自测地址：

```text
http://127.0.0.1:8788/inputbase/voice-input
```

说明：

- 前端不要直接调用 `127.0.0.1:8788`，这个地址只给服务器本机自测使用。
- 如果后续运维配置了域名或 HTTPS，把 `http` 改成 `https`，WebSocket 地址同步从 `ws` 改成 `wss`。
- 当前接口本身未内置业务登录鉴权，正式接入时应由上层网关、Nginx 或业务后端统一处理鉴权。

## 2. 接口总览

| 能力 | 方法 | 路径 | 前端是否推荐 |
|---|---|---|---|
| 健康检查 | `GET` | `/health` | 推荐 |
| 实时语音输入 | `WS` | `/realtime` | 推荐主流程 |
| 文档解析 | `POST` | `/documents/extract` | 推荐 |

推荐前端优先接：

```text
WS /realtime
POST /documents/extract
GET /health
```

## 3. 通用错误格式

所有 HTTP 错误统一返回 JSON：

```ts
type ApiError = {
  code: string;
  message: string;
  details?: Record<string, unknown>;
};
```

示例：

```json
{
    "code": "document_parse.unsupported_file_type",
    "message": "不支持的文档类型：.doc",
    "details": {
        "supported_extensions": [
            ".docx",
            ".markdown",
            ".md",
            ".pdf",
            ".txt"
        ]
    }
}
```

## 4. 健康检查

### 请求

```text
GET /health
```

完整地址：

```text
GET http://staic.evimed.com:8454/inputbase/voice-input/health
```

### 返回

```ts
type VoiceInputHealth = {
  ok: boolean;
  provider: string;
  realtime_provider?: string | null;
  message?: string | null;
};
```

示例：

```json
{
    "ok": true,
    "provider": "mock",
    "realtime_provider": "dashscope_fun_asr",
    "message": "Mock 语音提供方已就绪"
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `provider` | 保留字段，前端主流程不依赖 |
| `realtime_provider` | WebSocket 实时识别 provider |

只要 `ok=true` 且 `realtime_provider=dashscope_fun_asr`，就说明实时语音接口已经是真实识别模式。

## 5. 实时语音输入

### 5.1 说明

实时语音输入使用 WebSocket。前端边录音边发送音频二进制分片，后端持续返回识别文本，前端把文本插入输入框。

```text
WS /realtime
```

完整地址：

```text
ws://staic.evimed.com:8454/inputbase/voice-input/realtime
```

### 5.2 音频格式要求

当前真实识别模式使用阿里云 DashScope Fun-ASR，默认音频格式：

```text
16kHz
单声道
PCM16
二进制 ArrayBuffer 分片
```

前端不要直接发送 `MediaRecorder` 生成的 `audio/webm` 分片给实时接口。浏览器麦克风音频需要通过 `AudioContext` 或 `AudioWorklet` 转成 16k 单声道 PCM16 后再发送。

建议每个音频分片时长：

```text
100ms 到 300ms
```

### 5.3 前端发送消息

第一条消息必须是 JSON 文本：

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

字段说明：

| 字段 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `type` | string | 是 | 固定为 `start` |
| `language` | string | 否 | 默认 `zh-CN` |
| `mode` | string | 否 | `chat_input`、`dictation`、`command`，默认 `chat_input` |
| `target` | string | 否 | 前端输入框标识，后端透传 |
| `context` | string | 否 | 可选上下文 |
| `insert_mode` | string | 否 | 固定传 `stream` |

开始后发送音频：

```ts
socket.send(pcm16ArrayBuffer);
```

停止识别：

```json
{
    "type": "stop"
}
```

取消识别：

```json
{
    "type": "cancel"
}
```

### 5.4 后端返回事件

WebSocket 返回 JSON 文本消息：

```ts
type RealtimeVoiceInputEvent =
  | { type: "session.created"; session_id: string; provider?: string; insert_mode: "stream"; message?: string }
  | { type: "status"; session_id: string; status: string; provider?: string; [key: string]: unknown }
  | { type: "provider.task_started"; session_id: string; task_id: string; provider: string }
  | { type: "audio.accepted"; session_id: string; sequence: number; bytes: number }
  | { type: "insert.delta"; session_id: string; delta: string; text: string; is_final: false }
  | { type: "insert.replace"; session_id: string; text: string; is_final: false }
  | { type: "transcript.partial"; session_id: string; text: string; is_final: false }
  | { type: "transcript.final"; session_id: string; text: string; language: string; provider?: string }
  | { type: "insert.done"; session_id: string; text: string; language: string; chunk_count: number; total_bytes: number; provider?: string }
  | { type: "error"; code: string; message: string };
```

前端重点处理：

| 事件 | 前端处理 |
|---|---|
| `insert.delta` | 把 `delta` 追加到输入框，或直接用 `text` 覆盖当前识别文本 |
| `insert.replace` | ASR 中间结果被修正，必须用 `text` 覆盖当前识别文本 |
| `insert.done` | 最终文本确认完成 |
| `error` | 展示错误并允许重试 |

### 5.5 前端示例

```ts
const socket = new WebSocket("ws://staic.evimed.com:8454/inputbase/voice-input/realtime");

let currentText = "";

socket.onopen = () => {
  socket.send(JSON.stringify({
    type: "start",
    language: "zh-CN",
    mode: "chat_input",
    target: "chat-input",
    insert_mode: "stream",
  }));
};

socket.onmessage = (message) => {
  const event = JSON.parse(message.data);

  if (event.type === "insert.delta") {
    currentText = event.text;
    inputElement.value = currentText;
  }

  if (event.type === "insert.replace") {
    currentText = event.text;
    inputElement.value = currentText;
  }

  if (event.type === "insert.done") {
    inputElement.value = event.text;
  }
};

function sendPcmChunk(pcm16ArrayBuffer: ArrayBuffer) {
  socket.send(pcm16ArrayBuffer);
}

function stopRecording() {
  socket.send(JSON.stringify({ type: "stop" }));
}
```

## 6. 文档解析

### 6.1 请求

```text
POST /documents/extract
Content-Type: multipart/form-data
```

完整地址：

```text
POST http://staic.evimed.com:8454/inputbase/voice-input/documents/extract
```

表单字段：

| 字段名 | 类型 | 必填 | 说明 |
|---|---|---|---|
| `file` | File | 是 | 用户上传的文档文件 |

后端兼容字段名 `document`，但前端统一使用 `file`。

支持格式：

```text
.pdf
.docx
.txt
.md
.markdown
```

暂不支持 `.doc`。

默认文件大小限制：

```text
20MB
```

### 6.2 返回

```ts
type DocumentParseResult = {
  filename: string;
  mime_type: string;
  extension: string;
  text: string;
  char_count: number;
  byte_size: number;
  text_ratio: number;
  text_source: "native" | "ocr" | string;
  ocr_required: boolean;
  ocr_used: boolean;
  metadata: Record<string, unknown>;
};
```

字段说明：

| 字段 | 说明 |
|---|---|
| `text` | 最终提取出的文本，前端主要使用这个字段 |
| `text_ratio` | 文字占比，默认小于等于 `0.2` 会触发 OCR 判断 |
| `text_source` | `native` 表示原生文本，`ocr` 表示 OCR 结果 |
| `ocr_required` | 后端是否判断需要 OCR |
| `ocr_used` | 是否实际调用 OCR 并使用 OCR 结果 |
| `metadata` | PDF 页数、DOCX 图片数、OCR 状态等元数据 |

### 6.3 返回示例

普通 PDF：

```json
{
    "filename": "合同.pdf",
    "mime_type": "application/pdf",
    "extension": ".pdf",
    "text": "第一条 合同目的...",
    "char_count": 3280,
    "byte_size": 512300,
    "text_ratio": 1,
    "text_source": "native",
    "ocr_required": false,
    "ocr_used": false,
    "metadata": {
        "page_count": 6,
        "pages_with_text": 6,
        "text_ratio_threshold": 0.2,
        "ocr_decision": "skipped_text_ratio_above_threshold"
    }
}
```

扫描件 PDF 且 OCR 成功：

```json
{
    "filename": "扫描件.pdf",
    "mime_type": "application/pdf",
    "extension": ".pdf",
    "text": "识别出的扫描件文字内容...",
    "char_count": 1450,
    "byte_size": 1834200,
    "text_ratio": 0,
    "text_source": "ocr",
    "ocr_required": true,
    "ocr_used": true,
    "metadata": {
        "ocr_decision": "required",
        "ocr": {
            "used": true,
            "provider": "qwen_ocr",
            "model": "qwen3.5-ocr",
            "image_count": 3
        }
    }
}
```

### 6.4 前端示例

```ts
async function extractDocumentText(file: File) {
  const form = new FormData();
  form.append("file", file);

  const response = await fetch(
    "http://staic.evimed.com:8454/inputbase/voice-input/documents/extract",
    {
      method: "POST",
      body: form,
    },
  );

  const result = await response.json();

  if (!response.ok) {
    throw new Error(result.message || "文档解析失败");
  }

  return result;
}
```

前端处理建议：

- 上传时展示加载状态，触发 OCR 时耗时会更长。
- 成功后使用 `result.text` 进入编辑器或后续业务流程。
- `ocr_required=true` 且 `ocr_used=false` 时，提示“文档可能是扫描件，暂未识别到文字”。

## 7. SDK 接入

项目内提供 TypeScript SDK：

```text
client/voiceInputClient.ts
```

初始化：

```ts
import { VoiceInputClient } from "./voiceInputClient";

const client = new VoiceInputClient({
  baseUrl: "http://staic.evimed.com:8454",
});
```

实时语音：

```ts
const connection = client.startRealtime(
  {
    language: "zh-CN",
    mode: "chat_input",
    target: "chat-input",
  },
  {
    onDelta(delta, text) {
      inputElement.value = text;
    },
    onReplace(text) {
      inputElement.value = text;
    },
    onFinal(text) {
      inputElement.value = text;
    },
    onError(error) {
      console.error(error);
    },
  },
);

connection.sendAudio(pcm16ArrayBuffer);
connection.stop();
```

文档解析：

```ts
const result = await client.extractDocumentText(file);
editorElement.value = result.text;
```

## 8. curl 自测

完整 curl 命令清单见 [curl-commands-43.248.117.129.md](curl-commands-43.248.117.129.md)。

健康检查：

```bash
curl http://staic.evimed.com:8454/inputbase/voice-input/health
```

文档解析：

```bash
curl -X POST "http://staic.evimed.com:8454/inputbase/voice-input/documents/extract" \
  -F "file=@/path/to/demo.pdf"
```

服务器本机自测，如果 Nginx 尚未配置：

```bash
curl http://127.0.0.1:8788/inputbase/voice-input/health
```


## 9. 常见错误码

| code | 说明 |
|---|---|
| `voice_input.bad_request` | 请求参数错误 |
| `voice_input.provider_unavailable` | ASR provider 未配置或不可用 |
| `voice_input.provider_error` | ASR provider 调用失败 |
| `document_parse.bad_request` | 文档上传参数错误 |
| `document_parse.file_too_large` | 文档文件过大 |
| `document_parse.unsupported_file_type` | 文档类型不支持 |
| `document_parse.parse_failed` | 文档解析失败 |
| `document_parse.ocr_render_failed` | 文档转图片失败，无法 OCR |
| `document_parse.dependency_missing` | 服务端缺少解析依赖 |
| `document_parse.unsupported_ocr_provider` | OCR provider 配置不支持 |
