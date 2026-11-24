# Chat / Record SSE 文本合并

两个流式 Handler 通过 `web/stream_batch.py` 在发送前合并相邻文本增量。
业务层生成、会话持久化及 SSE JSON 结构不变。Chat 合并 `content` 与 `reasoning`，
Record 合并 `reasoning`；正文与推理从不混合。

## 环境变量

项目根目录 `.env` 中配置（Chat / Record 共用）：

```dotenv
SHULAN_SSE_TEXT_BATCH_CHARS=512
SHULAN_SSE_TEXT_BATCH_SECONDS=0.05
```

`CHARS` 是累计文本的 Python `len()` 字符数，必须为正整数；不是事件条数或字节数。
`SECONDS` 单位为秒，必须为有限正数，`0.05` 即 50ms。未配置或留空时使用上述默认值。
例如 `1024` 与 `0.1` 表示累计 1,024 字符或等待 100ms，任一满足即发送。
配置由 `config/env.py` 集中读取并在启动时校验；真实系统环境变量优先于 `.env`。
修改后重启后端生效，不支持请求中途热更新。四份 `.env.example*` 已包含默认配置。

## 发送规则

- 每种文本类型的首个非空片段立即发送，包括推理后的首个正文片段。
- 后续相邻同类文本累计达到字符阈值，或自本批首片段起达到等待时限，就发送（默认 512 字符 / 50ms）。
  新片段不重置期限。先加入新片段再判断长度，因此整批可能超过字符阈值。
- 定时发送与下一片段的到达无关：上游异步等待期间，已有缓存仍能发送。
  事件循环被同步代码阻塞、网络背压时，50ms 不是严格的端到端保证。
- 类型或业务元数据变化时先排空缓存。`start`、`question`、`finish`、`error`、未知事件、
  空文本等保持原事件，发送前排空文本。正常 EOF 与生成异常也先发送尾部有效文本。
- 合并事件使用本批最后一个片段的 `timestamp`、`elapsed_ms`，其余元数据必须一致才能合并。
  仅拼接原文，不插入换行、不切割输入片段；单个超过阈值的输入整体发送。
- 一个请求最多有一次上游读取在等待，网络写入串行执行，没有无限生产队列。
  客户端断开后停止积累发送批次，继续消费业务生成器和记录跳过发送事件，保持后台保存行为。
  外部取消请求任务时，取消并回收在途读取，关闭生成器，不遗留发送任务。

事件数量和单个事件时间含义会变化；正文、推理全文及业务事件顺序不变。
HTTP 流式日志计数及浏览器 JSONL 导出对应发送/接收事件，不再逐一对应模型原始片段。
后端合并减少逐事件开销，仍需配合前端批量显示及长文本分块排版。

## 验证

```powershell
python -m pytest -q tests/unit/web/test_stream_batch_config.py tests/unit/web/test_stream_batch.py tests/unit/web/test_stream_batch_http.py tests/unit/web/test_stream_log.py tests/unit/server/test_chat_api_contract.py tests/unit/server/test_record_api_contract.py
```

覆盖 Unicode 原文、突发输出、数量阈值、独立定时发送、持续到达不延期期限、业务边界、
错误尾部、取消清理、慢写入、断开后保存，以及本地真实 HTTP 连接上游暂停时的及时交付。
测试使用模拟业务服务，不连接真实模型或数据库。

上线前需用同一模型输出比较关闭合并、50ms/512 字符、100ms/1024 字符三组。
分别记录原始/发送事件数、首字延迟、生成完成、网络发送完成、浏览器接收结束与最终显示时间，
检查页面长任务、交互响应及全文一致性。本地契约测试不代表真实模型链路的性能提升。
