# 整卷问卷 API Lab

整卷页面已接入现行 API Lab，通过公共后端地址调用正式生产问卷 API。原逐题 [Record 页面](pages/record.html) 保留；整卷入口仍为 [record-preview.html](pages/record-preview.html)，也可从 API Lab 首页和各页导航进入。

## 启动与身份

在项目根目录启动现有页面服务：

```powershell
python -B -m tests.api.serve_pages --host 127.0.0.1 --port 9080
```

打开 [API Lab](http://127.0.0.1:9080/record-preview.html)，填写公共后端地址、`userId` 和 `sessionId`。同源页面共享这些设置；新会话保留用户，新用户同时更换会话。切换任何公共设置都会清除当前页面的问卷、答案和附件引用，并阻止旧响应更新新身份的页面。

9080 只提供静态文件、同源 `/proxy` 和开发用 `/callback`。页面服务不加载模型或 DocMind 配置，不再提供临时 `/preview`、`/questionnaire` 业务接口，也不执行问卷生成。

## 使用流程

1. 可选填写最多 5 份报告引用：`{url, fileType}` 或 `{documentId}`。文件输入与参数 JSON 同步；已有文档必须属于当前用户。本页没有本地上传或拖拽入口，也不会先调用报告解读换取文档 ID。
2. 点击“生成整卷问卷”。202 表示已排队或正在生成，页面按服务端 `retryAfterSeconds` 通过 GET 查询，直到就绪或终止。`failed` 且 `retryable=true` 时按重试间隔继续查询；网络请求失败后停止自动查询，可以手动恢复。
3. 在整份问卷填写文本、单选和多选。所有题均可选填，不预选“不清楚”或“以上均无”；多选 `none` 与普通选项互斥。留空与明确选择 `none` 是不同结果。
4. 点击“提交整卷答案”。允许提交空的 `{answers:{}}`。同一实例重复提交相同答案可重试，提交不同答案返回冲突；出错时保留当前草稿，先恢复状态确认是否已提交。`submissionPending=true` 表示服务端已受理答案、正在恢复保存，此时页面暂停编辑和提交，继续查询到完成。
5. 服务端确认 `submitted` 后显示“继续 Chat（完整 / SSE）”，进入原 Chat 页面并保留同一公共身份。此跳转不创建新会话，也不会把问卷伪装成聊天消息。

刷新后点击“恢复当前问卷”读取服务端缓存的问卷与状态；已提交实例恢复服务端保存的 `answers`。尚未提交的草稿仅保留在当前页面，刷新后不保留。

已有 `documentId` 的报告可能已进入画像。整卷页面本身不调用旧报告解读接口做预处理，生成/提交阶段的资料保存、画像更新和会话衔接由生产问卷服务负责。

## HTTP 契约

| 操作 | 方法与路径 | 参数位置 |
| --- | --- | --- |
| 生成 | `POST /api/v1/record/questionnaires` | JSON：`userId`、`sessionId`、可选 `fileList` |
| 恢复 | `GET /api/v1/record/questionnaires` | query：`userId`、`sessionId` |
| 提交 | `POST /api/v1/record/questionnaires/{questionnaireId}/answers` | path：`questionnaireId`；query：`userId`；JSON 正文仅 `{answers:{...}}` |

响应使用现有 `code/message/data` 包装。`data` 包含实例 ID、会话 ID、状态、有效期，以及就绪后的 `questionnaire`、`guidance`、`reports`；已提交时返回 `answers`。状态包括 `queued`、`generating`、`ready`、`submitted`、`expired`、`failed`。

参数编辑器将提交接口的路径、查询参数和正文合并显示为可编辑的“请求参数 JSON”，发送时按字段声明分开编码。原始网络记录展示实际 URL 和实际正文。手工 JSON 模式暂停问卷快捷编辑，不会静默覆盖或修复手工输入；可独立验证非法参数与服务端错误。

## 问卷与下载

问卷遵循 [现有问卷 Schema](../../docs/survey-system-schema.md)，不增加条件题语法或修改规范。`description` 展示报告承接，`title` 是实际问句；风险评判、客户补查文案、报告结果和健管师访谈分别展示。健管师访谈需结合实际答案选择后续仍需了解的问题。

报告区以表格显示检查时间、指标、结果与单位、参考范围和异常标记，支持报告根节点及分段结果；影像所见和诊断按原文展示。原始报告 JSON 保留在折叠区。风险区不重复显示与现有评估相同的依据或汇总说明，独立补充说明仍会展示，原始数据和下载内容不变。

可下载原始问卷、答案、指导材料和报告 JSON。问卷文件直接为 `title/pages` 对象；答案文件仅为 `{answers}`；下载不混入网络响应包装。提交后答案下载使用服务器保存的答案。API Lab 原始网络记录仍可单独导出。

## 离线验证

```powershell
node --test tests/unit/api/api_lab.test.cjs
python -B -m pytest -q tests/unit/server/test_api_lab_pages.py
```

Node 用例验证请求参数位置、整卷交互、空答案提交、轮询与恢复、下载、身份隔离及旧 Record 行为。页面服务用例验证静态入口、临时业务路由已移除及代理转发。以上检查不调用真实模型、数据库或文件服务；生产画像/会话衔接需由生产服务集成测试验证。
