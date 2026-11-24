# Report 与 Gut Barrier 日志接入审查

本次使两个模块已有日志接入公共输出，并绑定所属用户、会话与任务。用户日志进入 `logs/all/all.log` 及对应用户文件；扫描、领取失败、积压统计和扫描任务完成回调异常进入系统文件。未增加日志事件，也未为没有日志的位置新增 logger。

## 分支与基准

- 工作分支：`codex/logging-report-gut-barrier`。
- 业务基准：`develop@386a5c41f8e48ee69120d812dfb8b9faa01efcd9`；创建前已核对远端 develop 指向同一提交。
- 参考实现：`feature/logging-foundation@48674c37d0a35233067d7078c29b8d0d9361e504`。公共日志基础已进入 develop，本次只适配最新业务代码，未覆盖旧分支的整份业务文件。
- 验收后删除本地 `feature/logging-foundation`，远端分支保持不变。保留原有 `note.json` 及 `loadtest/results/` 文件；后者已由 develop 的忽略规则排除。
- 本次不暂存、不提交、不推送、不合并。

## 生产改动

| 文件 | 作用 |
| --- | --- |
| `adapters/report/document_source.py` | 已有文档验证警告改用公共 logger，接入级别控制、统一前缀及文件分流 |
| `web/handlers/report.py` | 业务 JSON 解析后补齐身份；创建生成扫描及登记完成回调时清空触发用户身份 |
| `web/handlers/report_query.py` | 列表、详情业务解析后补齐身份 |
| `modules/report/durable.py` | 生成、恢复和回调扫描清空身份；生成、结果补全、对账及投递按持久化记录绑定 |
| `bootstrap/factory.py` | 创建回调唤醒任务及登记完成回调时使用系统上下文 |
| `health_profile/report_source.py` | 画像补偿扫描使用系统上下文，逐记录投递绑定所属用户 |
| `modules/gut_barrier/service.py` | 已有日志使用公共 logger；扫描清空身份；生成及未启动任务释放绑定所属记录 |
| `web/handlers/gut_barrier.py` | 保存请求 UUID 快照，解析后补齐用户和会话；隔离生成扫描及回调唤醒 |
| `adapters/gut_barrier/store.py` | 内部回调投递视图透传已经持久化的 `sessionId` |

扩大回归发现：公共 logger 初始化会加载配置，而版本预检原先为读取常量导入 Gut Barrier 服务，导致现有 `--print-version` 的无配置运行断言失败。因此另有两项必要兼容调整：

| 文件 | 作用 |
| --- | --- |
| `modules/gut_barrier/policy.py` | 保存原值不变的 `SERVICE_VERSION`，供纯版本查询使用；服务仍导入并公开同名常量 |
| `scripts/check_gut_barrier_queue.py` | 从策略模块读取版本，避免仅查询版本时初始化日志及加载配置 |

共 11 个生产文件，另有 4 个专项测试文件及 3 份文档。

## 身份与行为边界

生成和恢复使用批次 ID；回调使用投递 ID。Gut Barrier 的任务作用域覆盖生成、续租、终态保存与后续 Report 解读，子协程及 `asyncio.to_thread()` 继承该身份。Report 的临床生成、解析和后续报告已有日志由外层任务绑定覆盖，无需逐处修改。

扫描既在协程内部清空身份，也在 HTTP 或结果触发的任务创建、完成回调登记时清空身份，避免扫描异常被归入触发用户。Report 画像记录没有会话或请求 UUID 时保持缺失；历史任务缺少身份字段时不增加查询、不伪造 UUID。

Gut Barrier 保留专属 Handler，不调用会开启正文预览的公共 `prepare()`。只保存 UUID 快照并补齐解析后的身份，小请求、大请求及正文日志关闭时均能分流；不会因此开启请求、响应正文预览。内部投递视图增加 `sessionId` 不改变数据库结构、冻结回调正文或对外协议。

对照 develop，修改文件中的 27 处已有日志调用、级别、参数和文案完全相同。去除新增日志装饰器及上下文作用域后，Report 生成服务、共享回调 Worker、Gut Barrier 服务与报告画像服务的业务语句 AST 均与 develop 相同；原有滚动并发、租约、取消和重试实现保留。

## 验证结果

验证日期：2026-10-01。使用本机 Python 3.12.12、离线替身、本机回环 HTTP 与临时日志目录。

修改前业务基线为 **189 项通过**，覆盖 Report 持久化与回调滚动并发，以及 Gut Barrier 服务、并发、解读交接、工厂和 API。移植并按最新报告类型调整 Report 的两份日志测试；Gut Barrier 新增两份专项测试。最终 **26 项日志专项均包含在扩大回归的通过结果中**。

专项覆盖：

- 交错用户的生成、续租、终态保存、恢复、对账和线程传播；取消后恢复调用方上下文。
- 未启动任务释放失败的所属用户归属，以及缺少持久化用户、会话的历史投递。
- 真实 Report 解读流程的已有开始、完成和回调日志，以及其子任务的身份继承。
- 回调确认、非成功响应、连接异常、确认保存异常与重试；积压统计使用系统身份。
- 生成领取失败、生成扫描失败、回调唤醒失败及托管任务完成回调的系统归属。
- 临时文件分流、请求 UUID、任务 ID 与统一前缀；两个原标准库 logger 的已有调用实际进入公共输出。
- Report 生成、列表及详情在超出日志预解析上限且关闭正文日志时补齐归属。
- Gut Barrier 小请求、超过 1 MiB 的请求、正文开关开启与关闭、非法 JSON 后的身份隔离；HTTP 日志不包含测试患者数据、签名 URL 或请求／响应正文。

最终扩大回归命令：

```bash
python -B -m pytest -q --tb=short -p no:cacheprovider tests/unit/report tests/unit/gut_barrier tests/unit/log tests/unit/web tests/unit/runtime tests/unit/health_profile tests/unit/bootstrap
```

结果：**4893 项通过、173 项跳过，0 失败**。跳过项遵守现有测试条件，包括要求显式 `HISTORICAL_ARCHIVE_DIR` 的历史模板回归。Windows 本机使用专用 pytest `--basetemp`；最初沙箱拒绝临时目录写入导致的 setup 错误，在授权重跑后已消除。版本预检兼容性断言在最终回归中通过，没有修改或弱化原断言。

修改文件的 Ruff 检查（`E9,F63,F7,F82`）及 `git diff --check` 通过。未连接真实模型、数据库或回调接收方，未验收线上磁盘性能。

公共工具和 HTTP 日志行为分别见 [公共日志工具](logging.md) 与 [API 请求与响应日志](http-logging.md)。
