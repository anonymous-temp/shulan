# 公共日志工具

`utils.log` 提供 logger、文件分流、日志轮转和用户上下文工具，保留原有 `getLogger()` 入口。应用已接入 HTTP/SSE、Chat、QA、Report、Gut Barrier、画像任务及启动关闭流程。

## 入口与结构

```python
from utils.log import getLogger, global_logger

logger = getLogger(__name__)
logger.info("服务消息")
```

相同名称复用同一个 logger，初始化不会重复添加 Handler。`global_logger`、`configure_tornado_logging`、`shutdown_logging`、`ContextFilter` 也可从 `utils.log` 导入。

| 模块 | 职责 |
| --- | --- |
| `utils.log.core` | logger、格式、输出初始化与关闭 |
| `utils.log.context` | 用户、会话、请求及任务上下文 |
| `utils.log.files` | 后台写入、文件分流、轮转和清理 |
| `utils.log.preview` | 正文缩略与脱敏 |

包入口按需加载核心。单独导入 `context`、`files` 或 `preview` 不加载项目配置、不创建文件写入线程。访问 logger 公共入口时才初始化核心，读取 `config/env.py` 提供的配置。

## 日志归属

默认保留控制台输出，并向以下 UTF-8 文本文件写入当前日志级别允许的记录：

| 文件 | 内容 |
| --- | --- |
| `logs/all/all.log` | 全部已接入记录 |
| `logs/system/system.log` | 未绑定具体用户的记录 |
| `logs/user/<user_id>/user.log` | 绑定了对应用户的记录 |

每条记录写入全量文件，再写入系统或用户文件。普通用户 ID 原样用于用户目录名；非法字符、超长 ID、保留名称、大小写冲突采用安全目录名或稳定摘要后缀。内部日志上下文仍保留实际用户 ID。

目录按类型和用户隔离，当前文件与该系列的归档放在同一目录：

```text
logs/
├── all/
│   ├── all.log
│   ├── all.log.owner
│   ├── all.2026-09-22.000001.log
│   └── all.2026-09-22.000002.log
├── system/
│   ├── system.log
│   ├── system.log.owner
│   └── system.2026-09-22.000001.log
└── user/
    └── user_id/
        ├── user.log
        ├── user.log.owner
        └── user.2026-09-22.000001.log
```

升级后直接写入新目录。旧布局 `logs/*.log`、`logs/users/*.log` 及其归属标记不迁移、不追加、不自动清理，需自行保管或处理。

用户身份需要调用方明确提供，公共日志包不解析 HTTP 请求、不查询数据库，也不根据日志文字猜测用户。现有 `getLogger()` 调用可以直接获得文件输出；未接入身份绑定时，记录进入全量和系统文件。

```python
from utils.log import getLogger
from utils.log.context import bind_log_context

logger = getLogger(__name__)

with bind_log_context(user_id="u1", session_id="s1", request_uuid="request-1"):
    logger.info("开始处理")
    # 此作用域的日志进入 logs/all/all.log 和 logs/user/u1/user.log。

logger.info("系统任务")
```

上下文基于 `ContextVar`，退出作用域时恢复原值，包括异常路径。子协程和 `asyncio.to_thread()` 继承创建时的上下文；跨用户批量任务应在逐项处理时明确重新绑定。日志产生时固定身份和消息，后台写入线程不读取请求上下文。

日志前缀包含 `seq` 和 `task_id`，未绑定任务时 `task_id` 显示 `-`。`user_id`、`session_id`、`request_uuid` 保留在内部上下文中，用于用户分流和关联，不在公共前缀重复输出；日志正文已有的身份信息保持原样。前缀 `seq` 为进程内序号，重启后重新计数，与 HTTP/SSE 日志正文中的事件序号不同。文件没有控制台颜色转义。

## 配置与轮转

配置仍由 `config/env.py` 集中读取，示例位于四份 `.env.example*`。文件和控制台共用 `SHULAN_LOG_LEVEL`，不强制开启 DEBUG。

| 配置 | 默认值 | 含义 |
| --- | --- | --- |
| `SHULAN_LOG_FILE_ENABLED` | `true` | 文件输出开关；关闭后不创建文件线程和目录 |
| `SHULAN_LOG_DIR` | `logs` | 相对路径基于项目根目录，也接受绝对路径 |
| `SHULAN_LOG_MAX_BYTES` | `52428800` | 单文件轮转阈值，50 MiB |
| `SHULAN_LOG_RETENTION_DAYS` | `14` | 按最近写入时间保留的天数 |
| `SHULAN_LOG_BACKUP_COUNT` | `20` | 每个日志系列最多保留的归档数 |
| `SHULAN_LOG_USER_MAX_OPEN_FILES` | `128` | 同时打开的用户文件数，不含全量与系统文件 |
| `SHULAN_LOG_QUEUE_CAPACITY` | `1000` | 后台待写队列容量，按记录数计 |

环境配置示例可按部署需要开启或关闭文件输出；离线单元测试由 `tests/unit/conftest.py` 独立关闭默认文件输出，不依赖示例中的开关值。专门的文件测试使用临时目录。数字限制必须为正整数。

轮转由下一条写入触发，两种情况使用相同的命名格式 `名称.日期.六位编号.log`：

| 场景 | 行为 |
| --- | --- |
| 下一条记录会使当前文件超过大小阈值 | 归档旧文件，再写入新的当前文件；同一天可产生多个归档 |
| 服务器本地日期变化 | 当天首次写入时归档旧文件，不在午夜主动生成空文件 |
| 大小和日期条件同时满足 | 只轮转一次 |
| 单条记录超过阈值 | 完整写入，不拆分，允许当前文件临时超限 |

归档日期属于旧文件，编号按该日志系列、该日期已有归档接续。例如一天内先产生 `all.2026-09-22.000001.log`，9 月 23 日首次写入时再将剩余的前一日文件归档为 `all.2026-09-22.000002.log`。重启后继续追加当前文件，轮转不覆盖现存归档。

保留天数和归档数量同时生效，分别限制全量、系统及每个用户的日志系列；归档数量跨日期累计，不包含当前文件，达到数量上限时即使未过期也会删除较旧归档。启动和每小时只检查新布局的日志系列，覆盖不再活跃的用户，清理后保留空用户目录；关闭闲置句柄不会删除文件。文件旁的 `.log.owner` 为本工具的归属标记，应与文件一并保留；已有同名无标记文件或归档不会被接管或覆盖。不会跟随符号链接或 Windows 目录联接进行写入和清理。总磁盘占用仍与用户数量有关，本工具不设全目录容量上限。

每个日志目录仅允许一个服务进程写入，支持该进程内协程和线程并发。多个实例应配置独立目录，避免同时让外部 logrotate 轮转这些文件。

## 写入、关闭与故障

正常情况下，一个后台线程负责文件写入。队列满时临时同步写入，优先保存完整记录，极端磁盘拥堵可能增加调用耗时。同步兜底可能改变物理写入顺序，可结合时间和日志前缀序号排查。

单个目标写入失败后仍尝试其他目标，控制台每分钟最多输出一次文件故障提示；不向业务传播文件异常。文件异常、磁盘写满和强制结束进程不能保证完整落盘。

```python
from utils.log import shutdown_logging

# 应在业务任务全部结束之后调用。
shutdown_logging()
```

`shutdown_logging()` 排空队列、刷新并关闭文件，可重复调用。核心也注册了正常解释器退出时的清理函数。服务正常退出时先等待请求与托管任务结束，再关闭资源并刷新日志。启动失败和资源关闭异常也执行日志刷新；强制终止进程仍无法保证队列完整落盘。

`configure_tornado_logging()` 在应用启动时接入 Tornado 标准日志；启动脱敏配置摘要也通过 logger 归档。任意 `print()`、运维命令的 JSON 输出和第三方库私有输出不会自动归档。

## 正文预览

`utils.log.preview` 保留现有 `***` 缩略、字段脱敏、数量、深度和总大小限制，不修改原始数据。文件分流不会再次缩略消息，也不会自动把任意业务消息转换成受限 JSON；调用方需要使用预览工具构造正文。HTTP/SSE 已有预览行为参见 [API 请求与响应日志](http-logging.md)。

## 应用接入与边界

- HTTP 请求进入独立上下文，从顶层 `userId`、`sessionId` 识别日志归属，并携带请求 UUID；这不是身份认证，不改变业务校验。正文开关关闭时仍识别用户。
- 请求开始阶段遵守 JSON 解析大小上限；超大请求由业务解析后的 `bind_log_identity(body)` 补齐身份，早期日志仍归入系统文件。无法识别用户的请求也进入系统文件。
- SSE 断连回调和最终汇总使用请求身份快照；后台继续生成和保存时保留原用户归属，正文缩略与发送行为保持原有规则。
- 请求派生的托管任务继承上下文，完成回调保留关联；周期扫描清空继承身份，Chat 待发送消息、QA 创建恢复、画像摄取及 Chat/QA 来源补偿逐项绑定用户与任务。无原请求 UUID 时显示 `-`。
- 画像人工重驱和运维列表中的逐任务日志使用任务所属用户，列表统计归入系统文件；运维退出也刷新日志。

Report 生成、列表及详情在业务 JSON 解析后补齐身份。Report 与 Gut Barrier 的生成、恢复和回调扫描使用系统上下文，任务创建及完成回调登记也清空触发请求的身份；逐任务使用持久化的用户、会话和任务标识。生成与对账使用批次 ID，回调使用投递 ID；领取失败和积压统计进入系统文件。Report 画像补偿逐记录绑定用户，缺少会话或原始请求 UUID 时不补查询、不伪造字段。

Gut Barrier 的生成任务绑定覆盖续租、终态保存及后续 Report 解读，子协程和 `asyncio.to_thread()` 继承相同身份。其专属 HTTP Handler 只补齐请求 UUID 快照及解析后的用户、会话，保留不记录请求、响应正文的行为，正文开关不改变这一边界。Gut Barrier 内部回调投递视图透传已有 `sessionId`，不改变数据库结构和对外协议。

Report 文档适配器与 Gut Barrier 服务的已有日志使用公共 `getLogger()`，进入统一级别控制、前缀及文件输出；本次接入保留全部已有日志调用、级别与文案，没有为其他位置新增日志事件。具体变更和验证结果见 [Report 与 Gut Barrier 日志接入审查](report-logging-review.md)。

## 离线验证

公共测试独立位于 `tests/unit/log/`：

```bash
python -m pytest tests/unit/log -q
```

测试不读取真实 `.env`、不连接外部服务，文件测试使用临时目录；覆盖入口兼容、按需导入、上下文隔离、分流、轮转、清理、并发、队列拥堵、故障处理及关闭刷新。

应用接入回归按 Web、Runtime 和画像模块分类：

```bash
python -m pytest tests/unit/log tests/unit/web tests/unit/runtime tests/unit/health_profile/test_profile_ingestion.py tests/unit/health_profile/test_profile_maintenance_cli.py -q
```

覆盖交错用户请求、线程传播、非法请求、正文关闭、超大请求、SSE 断连和只输出一次的汇总、后台异常归属、周期扫描隔离及退出刷新。这些测试使用离线替身，不代表生产服务和线上磁盘性能已经验证。

Report 与 Gut Barrier 的专项用例位于各自单测目录：

```bash
python -m pytest tests/unit/report/test_log_context.py tests/unit/report/test_log_files.py tests/unit/gut_barrier/test_log_context.py tests/unit/gut_barrier/test_log_files.py -q
```

覆盖生成、恢复、对账、未启动任务释放、后续解读、回调确认与重试、扫描及完成回调异常归属，并使用临时日志目录验证真实 Tornado 请求的分流和 Gut Barrier 正文边界。最新实际结果记录在接入审查文档中。
