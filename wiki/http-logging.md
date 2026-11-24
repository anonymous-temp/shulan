# API 请求与响应日志

服务在 HTTP 边界记录请求和响应的数据预览，使用现有日志输出位置和 `request_uuid` 关联。此功能仅用于观察运行状态，不改变 API 返回内容、状态码、响应头、SSE 事件顺序、断连后生成与保存行为，也不记录内部模型调用、数据库操作或出站回调正文。

## 代码入口

日志工具统一放在 `utils/log/` 包内，业务代码继续使用 `from utils.log import getLogger, global_logger`。公开入口同时保留 `configure_tornado_logging`、`shutdown_logging` 和 `ContextFilter`。

`core.py` 管理 logger 和输出初始化；`context.py` 管理用户、请求及任务上下文；`files.py` 管理文件写入和轮转；`preview.py` 提供正文缩略与脱敏。上下文及预览工具分别从 `utils.log.context`、`utils.log.preview` 导入，单独导入这些工具不会加载服务配置或启动文件写入线程。HTTP 和 SSE 适配仍在 Web 层。

`bind_log_identity(body)` 在请求解析后补充当前日志的 `userId`、`sessionId`，并保存供独立回调使用的身份快照；它不创建用户专属 logger，不修改请求数据，也不承担认证或业务校验。

## 查看方式

### 日志文件与用户归属

默认保留控制台输出，并在项目根目录的 `logs/` 下保存 UTF-8 文本文件：

| 文件 | 内容 |
| --- | --- |
| `all/all.log` | 当前日志级别允许输出的全部已接入日志 |
| `system/system.log` | 无具体用户归属的日志，包括启动、健康检查、任务扫描 |
| `user/<user_id>/user.log` | 对应用户的请求、响应、业务处理、异常和后台任务 |

一条日志写入 `all/all.log`，再按用户归属写入系统或用户文件。用户请求中的数据库错误仍进入该用户文件。文件保留正文缩略和脱敏规则；“全量”不表示强制开启 DEBUG 或保存未缩略正文。接入范围是项目 `getLogger()`、Tornado 日志和启动脱敏配置摘要，不劫持第三方私有输出或任意 `print()`。

日志公共前缀包含 `seq` 和 `task_id`；未绑定任务时 `task_id` 显示 `-`。`user_id`、`session_id`、`request_uuid` 仍保留在内部上下文中，用于用户分流和关联，不在公共前缀重复输出；HTTP 日志消息中的 `request_uuid` 及正文已有的用户、会话字段保持原样。`seq` 是进程内日志序号，重启后重新计数，与 SSE 消息内的事件 `seq` 不同。磁盘拥堵时同步兜底可能改变文件中的物理顺序，可结合时间和前缀序号排查。

请求通过上下文关联用户，子协程和 `asyncio.to_thread()` 继承身份。定期扫描清空请求身份，Chat、QA、Report、Gut Barrier 及画像恢复任务逐项绑定持久化的用户和会话；没有原始请求 UUID 时显示 `-`，通过用户、会话和任务标识关联。SSE 断连后生成及保存仍使用原请求身份。无法解析身份的请求和超出日志解析上限的请求开始记录进入系统文件，后者在业务解析成功后补充用户身份。

正常用户 ID 原样用于用户目录名；非法字符、过长名称、Windows 保留名称及大小写冲突使用安全目录名和稳定摘要后缀。文件旁的 `.log.owner` 是归属标记，只存版本和身份摘要；请与日志一起保留。清理只处理带标记的日志系列，不接管已有的同名无标记文件；遇到冲突会告警。

Report 的生成、列表和详情 Handler 在业务解析后补齐身份，覆盖关闭正文日志与超过日志预解析上限的请求。生成及回调扫描的任务创建和完成回调登记使用系统上下文，逐批次和逐投递绑定持久化身份。接入范围和手动绑定示例参见 [公共日志工具](logging.md)。

当前文件和归档均位于各自目录，用户当前文件统一命名为 `user.log`。目录树与轮转示例见 [公共日志工具](logging.md)。旧布局 `logs/*.log`、`logs/users/*.log` 及其标记保留原样，不再追加或自动清理。

### 文件配置与轮转

| 配置 | 默认值 | 说明 |
| --- | --- | --- |
| `SHULAN_LOG_FILE_ENABLED` | `true` | 文件输出开关；关闭后不创建文件写入线程和目录 |
| `SHULAN_LOG_DIR` | `logs` | 相对路径基于项目根目录，也可指定绝对路径 |
| `SHULAN_LOG_MAX_BYTES` | `52428800` | 每个文件达到 50 MiB 或跨日时轮转 |
| `SHULAN_LOG_RETENTION_DAYS` | `14` | 按最近写入时间保留的天数 |
| `SHULAN_LOG_BACKUP_COUNT` | `20` | 每个日志系列最多保留的归档数量 |
| `SHULAN_LOG_USER_MAX_OPEN_FILES` | `128` | 同时打开的用户文件数量上限，不含全量和系统文件 |
| `SHULAN_LOG_QUEUE_CAPACITY` | `1000` | 后台文件写入队列容量，以记录数计 |

文件与控制台共用 `SHULAN_LOG_LEVEL`。环境配置示例可按部署需要开启或关闭文件输出；离线单元测试独立关闭默认文件输出，专门的文件测试使用临时目录。归档示例为 `all/all.2026-09-22.000001.log`，日期使用服务器本地时区；下一条写入触发跨日轮转，不在午夜主动生成空文件。大小轮转使用相同命名，同日编号递增；两个条件同时满足时只轮转一次，重启后不会覆盖现有归档。单条记录完整写入，不因轮转阈值再次截断，因此超大单条记录可以超过阈值。

保留天数与归档数量同时生效，达到任一限制便清理旧文件；数量对每个日志系列分别跨日期累计，不包含当前文件。启动和每小时检查新目录中不再活跃用户的过期文件，保留清理后的空用户目录，不跟随符号链接或目录联接；关闭闲置句柄不会删除文件。磁盘总占用仍与用户数量有关，本功能不设置全目录容量上限。

正常情况下后台线程写文件。队列满时临时同步写入，优先保存记录，极端磁盘拥堵可能增加请求耗时。单个目标写入失败不阻止尝试其他目标，控制台每分钟最多提示一次文件故障，不把文件异常传播给业务。正常关闭会排空队列并刷新文件；强制结束进程、磁盘写满或损坏时无法保证完整落盘。

每个日志目录只能有一个服务进程写入。多实例或发布期间新旧进程重叠时，应分别设置 `SHULAN_LOG_DIR`；不要让外部 logrotate 同时重命名本功能管理的文件。无需添加第三方日志依赖。

### HTTP 正文

`POST /api/v1/health-analysis` 的生成失败可用响应中的 `uuid` 查找 `Health Analysis generation failed` 日志。该记录包含 `request_uuid`、`stage` 和固定 `reason`。联合审核的协议原因分别为 `review_version_mismatch`、`review_questions_missing`、`review_verdicts_invalid`、`review_issues_invalid`、`review_rules_missing`、`review_rules_invalid` 和 `review_recommendations_invalid`。`survey_review_exhausted` 表示内容纠正预算用尽。模型适配器也区分可识别的超时、调用不可用与响应格式错误。诊断只记录这些固定字段，客户端沿用统一的 502 响应。

Gut Barrier 专属端点继续使用只记录访问摘要的 Handler，不调用开启正文预览的公共 `prepare()`。它保存请求 UUID 身份快照，在业务 JSON 解析后绑定用户与会话；小请求、大请求以及正文日志关闭时均可正确分流。该端点不输出 `HTTP 请求开始` 或请求、响应正文预览，`SHULAN_HTTP_LOG_BODY_ENABLED=true` 也不会开启这些正文。已有响应完成、异常和后台业务日志仍按其原有级别输出。

普通请求记录 `HTTP 请求开始`，其中 `request` 是包含 `query` 和 `body` 的单行 JSON；响应完成后输出一条 `HTTP 访问 / 响应完成`，其中 `body` 是响应正文预览。原来的响应简报和访问日志合并，避免重复打印响应。中文直接显示，文本换行转义为 `\n`。

```text
INFO HTTP 请求开始: request_uuid=ABC method=POST path=/api/v1/qa/create remote_ip=127.0.0.1 request={"query":{},"body":{"userId":"u1","message":"希望改善睡眠"}}
INFO HTTP 访问 / 响应完成: request_uuid=ABC method=POST path=/api/v1/qa/create remote_ip=127.0.0.1 status_code=200 code=200 elapsed_ms=1250.00 body={"code":200,"message":"success","uuid":"ABC","timestamp":1234567890,"elapsed_ms":1248,"data":{"sessionId":"s1"}}
```

`status_code` 是 HTTP 状态，`code` 是业务响应中的整数状态码，两者分别展示。任一个达到 400 时完成记录使用 WARNING，达到 500 时使用 ERROR，其余使用 INFO。完成记录的耗时是 Tornado 请求耗时，正文中的 `elapsed_ms` 保持业务原值。HTTP 完成日志表示服务端完成响应处理，不保证客户端已读取全部字节，也不代表异步报告任务已完成。

流式正文逐事件使用 DEBUG；每条包含 `seq`、事件类型、`outcome` 和受限正文。`outcome=flushed` 表示本次刷新成功，`flush_failed` 表示连接关闭导致刷新失败，`skipped_disconnected` 表示断连后继续生成但跳过发送。成功结束时，INFO 完成日志除了事件数和连接状态，还包含 `body_format=sse_summary` 和汇总响应 `body`，因此关闭 DEBUG 后仍可查看响应的数据结构。

汇总仅发生在日志侧：将 `content` 增量合并到 `data.content`，将 `reasoning` 增量合并到 `data.reasoning`，保留 QA 的问题、选项和结果字段；不含结果的 `finish` 不清除先前结果。正文中的 `uuid` 保留服务实际产生的事件 UUID，与 HTTP 日志消息中的 `request_uuid` 可以不同。正常结束采用最后事件提供的元数据；错误码和错误内容不会被后续成功事件覆盖。裸错误未携带业务码时不虚构业务码，日志前缀显示 `code=-`。

`stream_state` 分为 `finished`（收到 finish）、`error`（存在错误）、`empty`（没有事件）、`incomplete`（有事件但没有 finish）。错误完成记录按实际业务码使用 WARNING／ERROR，没有错误码的错误使用 ERROR；HTTP 状态仍是实际状态，SSE 返回过 HTTP 200 后出现业务错误也能单独识别。完成记录每请求最多输出一次，包括断连后继续生成的路径。断连时汇总包括后台继续生成的内容，`disconnected` 和 `flushed_events` 用于区分这些内容与成功刷新的事件，不代表客户端收到全部内容。

汇总不查询历史记录或数据库，不缓存全部事件，也不为日志复制整个回复。每段增量文本只保留配置允许的首尾字符，其他字段保存数量、深度和字节预算限制下的快照；最终输出仍统一使用 `***` 缩略，并保持合法 JSON。累计文本不会因为每个片段都很短而绕过缩略，也不会对已缩略内容重复添加占位符。关闭正文日志开关时不建立正文汇总。

```text
DEBUG HTTP 流式事件: request_uuid=ABC seq=2 event="content" outcome=flushed body={"code":200,"data":{"type":"content","content":"建议保持规律作息"}}
INFO HTTP 访问 / 响应完成: request_uuid=ABC method=POST path=/api/v1/chat/send/stream remote_ip=127.0.0.1 status_code=200 code=200 elapsed_ms=2500.00 stream_events=3 flushed_events=3 disconnected=False stream_errors=0 stream_state=finished body_format=sse_summary body={"code":200,"message":"success","uuid":"SERVICE-UUID","timestamp":1234567890,"elapsed_ms":2498,"data":{"type":"finish","userId":"u1","sessionId":"s1","content":"建议保持规律作息"}}
```

日常使用 `SHULAN_LOG_LEVEL=INFO` 即可查看普通响应和流式汇总正文；需要查看每一个 SSE 事件时改为 `DEBUG` 并重启服务。`/health`、`/health/live`、`/health/ready` 与普通接口一样记录请求和响应正文；GET 没有请求体时显示 `{"format":"empty","bytes":0}`，响应保留实际业务 `code` 和健康状态数据。OPTIONS 只保留简短访问记录。查询同一次请求时，搜索 `request_uuid`；正文中的会话标识仍可用于跨请求排查。

## 缩略规则与配置

所有选项由 `config/env.py` 统一读取，修改后重启生效，四份 `.env.example*` 均有示例。未配置时使用下表默认值。

| 配置项 | 默认值 | 含义 |
|---|---|---|
| `SHULAN_HTTP_LOG_BODY_ENABLED` | `true` | 请求、响应、SSE 正文预览开关；关闭后保留摘要 |
| `SHULAN_HTTP_LOG_STRING_CHARS` | `2000` | 长字符串保留的首尾字符合计数量，中间仅插入 `***`，占位符不计入字符配额 |
| `SHULAN_HTTP_LOG_LIST_ITEMS` | `20` | 每个数组最多保留前 20 项，超出部分在末尾以 `"***"` 代替，不保留尾部元素；占位符不计入配额 |
| `SHULAN_HTTP_LOG_DICT_ITEMS` | `50` | 每个字典最多保留前 50 个字段，超出部分在末尾以 `"***":"***"` 代替；占位字段不计入配额 |
| `SHULAN_HTTP_LOG_DEPTH` | `8` | 从预览根节点开始最多展开的容器层数，达到限制的容器替换为 `"***"` |
| `SHULAN_HTTP_LOG_TOTAL_BYTES` | `32768` | 每份 JSON 预览的 UTF-8 总字节预算，不含日志固定前缀 |
| `SHULAN_HTTP_LOG_PARSE_BYTES` | `1048576` | 请求体超过此大小不为日志完整解析 JSON，改为有限的文本首尾预览与原始字节数 |

总大小预算优先于单个字段的字符数和容器数量；预算不足时可能提前缩短当前值或省略后续字段。文本、容器数量、嵌套深度和总大小造成的省略均只使用 `***`，不附加原因或数量，日志仍保持合法 JSON。字典占位字段若与已显示的真实字段重名，会追加 `*`，例如 `"****":"***"`，避免覆盖真实字段。空正文显示 `format=empty`；JSON 的 `null`、空字符串、空列表分别保留。分多次普通 `write()` 输出时，使用 `body_format=chunks` 展示受限的写入片段，未保留的片段用末尾的 `"***"` 表示，不宣称它是合并后的完整 JSON 响应。

例如将 `STRING_CHARS` 设为 `32`，超过 32 字符的字符串保留前 16 和后 16 字符，显示为 `前16字符***后16字符`，不超过阈值时完整显示。将 `LIST_ITEMS` 和 `DICT_ITEMS` 分别设为 `2`，输出示例如下（为说明结构，这里使用缩进；实际日志仍为单行）：

```json
{
    "history": [
        1,
        2,
        "***"
    ],
    "data": {
        "userId": "u1",
        "status": "finished",
        "***": "***"
    }
}
```

字符串以外的容器只保留开头部分，不按首尾各一半处理；列表或字典恰好达到数量上限时不会追加占位符。以 `{"data":{"profile":{"age":30}}}` 为例，`DEPTH=2` 时预览为 `{"data":{"profile":"***"}}`。请求日志的 `query`/`body` 包装层与响应的 `data` 包装层均参与深度计算。原始文本和超大请求的首尾采样也只使用 `***` 连接，独立的原始字节数元数据仍保留。

文件上传只记录字段、文件名、类型、大小和文件总数，二进制正文不展开。JSON、查询参数和表单中的常见密码、令牌、密钥字段使用 `[REDACTED]`；不输出全部请求头。无效或过大的 JSON 降级为文本预览时，不保证按字段脱敏；业务自由文本也不自动识别个人信息。正文日志的用途是诊断，不作为原始数据存档。

预览操作不修改原对象，不消费生成器，不调用任意对象的 `repr()`；深度、遍历数量和输出大小均有限制。关闭日志级别或正文开关时跳过对应预览。预览或日志输出失败时采用尽力记录，不阻止接口响应或生命周期租约释放。
