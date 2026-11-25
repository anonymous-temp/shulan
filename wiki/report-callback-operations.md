# 最终报告回调运维指南

> 状态：当前实现；命令与源码已核对，实际迁移由运维人员在目标部署环境执行。
>
> 更新日期：2026-11-25

回调地址在报告受理时保存为批次快照。修改配置并重启服务，只会改变之后新建批次的地址；迁移已有积压使用 `migrate-pending`，无需逐个查找用户和报告 ID。命令会调整投递任务，后台 Worker 使用已保存的报告正文发送，不重新生成报告。

| 报告类型 | 新批次的回调地址 |
| --- | --- |
| 评估报告 `evaluate` | 优先使用 `SHULAN_REPORT_EVALUATE_CALLBACK_URL`。未设置且公共路径为 `/api/basic-health-assessment/ai-full-back/callback` 时，使用该环境同域的 `/api/basic-health-assessment/ai-full-back-2026-11/callback`。自定义公共接收端继续使用原地址。 |
| 后续报告及肠道屏障报告 | 使用公共地址 `SHULAN_REPORT_CALLBACK_URL`。 |

生产部署设置 `SHULAN_ENVIRONMENT=production`。启动和发送前均校验 HTTPS 地址，并拦截 `test.slmc.top`（含子域）与回环地址。旧任务若保存了测试地址，发送时会被拦截并保留重试状态。HTTP 重定向不会被跟随。下述命令只维护普通 Report 队列。切换环境前需单独核对肠道屏障队列 `gut_barrier_report_jobs` 的存量任务。

## 1. 执行环境与命令选择

在后端项目根目录、与服务相同的 Python 环境和部署配置下执行以下命令，确认连接的是需要维护的 MongoDB。使用 Conda 时可先执行 `conda activate shulan`；也可以把下文的 `python` 替换为 `conda run --no-capture-output -n shulan python`。

配置从项目根目录 `.env` 和进程环境变量加载，进程环境变量优先。如果服务由容器或服务管理器注入配置，执行命令的环境也必须使用相同配置；仅修改另一份本地 `.env` 不会改变运行中服务或命令实际读取的值。

| 场景 | 命令 | 是否修改记录 |
| --- | --- | --- |
| 整体切换到当前配置的回调地址 | `migrate-pending` | 是，更新匹配批次的地址并安排未确认任务投递 |
| 预览整体迁移范围 | `migrate-pending --dry-run` | 否，只读取 |
| 查询一个批次的生成及投递状态 | `status --user-id ... --final-report-id ...` | 否，只读取 |
| 立即补投一个批次，保留原地址 | `retry --user-id ... --final-report-id ... --reason ...` | 是，安排未确认任务投递 |
| 单独修正一个批次的地址并补投 | `retry --user-id ... --final-report-id ... --target-url ... --reason ...` | 是，修改未确认任务的目标 |

表中命令均接在 `python -m scripts.report_callbacks` 后。完整参数帮助：

```bash
python -m scripts.report_callbacks --help
```

## 2. 整体迁移积压到当前配置地址

### 2.1 修改配置并重启服务

仅切换评估报告接收端时，保留公共地址的当前配置，设置评估专用地址。以下示例展示开发环境的两个接收端，实际部署使用接收方提供的完整 URL。

```dotenv
SHULAN_REPORT_CALLBACK_URL=http://192.168.20.100:9080/callback
SHULAN_REPORT_EVALUATE_CALLBACK_URL=http://192.168.20.100:9081/callback
```

生产环境使用正式接收方地址。地址须能从后端访问；`localhost` 指后端所在机器或容器。模拟接收端的启动方式见 [最终报告回调联调](../tests/README.md#最终报告持久化与回调联调)。

重启所有使用该配置的服务实例，确保后续新建批次使用新地址。部署流程见 [安全停服与发布](../docs/architecture/graceful-shutdown-and-deployment.md)。迁移命令只处理开始执行前已受理的范围，不会持续追踪旧配置实例新建的任务。

### 2.2 可选：只读预览

```bash
python -m scripts.report_callbacks migrate-pending --dry-run
```

预览只读取数据。检查 `matchedBatches` 和公共目标 `targetOrigin`。评估与公共目标 URL 不同时还会返回 `evaluateTargetOrigin`。两个字段只显示协议、主机和端口。若两个目标仅路径不同，显示的来源地址相同，完整路径以部署配置为准。后台投递仍在运行，实际执行时匹配数量可能变化。

### 2.3 执行迁移

```bash
python -m scripts.report_callbacks migrate-pending
```

命令读取部署中的两个地址，按报告类型选择目标。迁移覆盖普通 Report 队列中的匹配批次，`--user-id`、`--final-report-id` 和 `--target-url` 仅用于单批操作。操作人默认使用当前系统用户名，原因默认为“积压报告迁移到当前服务回调配置”，可通过 `--operator`、`--reason` 覆盖。

迁移范围包括当前数据库中已启用回调、目标需要改变且仍有未确认任务的批次，也包括已受理但尚未生成全部投递任务的批次。批次快照按类型更新，之后生成的任务据此取得各自的地址。已存正文及累计尝试次数保留，改址的未确认任务设为立即到期，等待运行中的 Worker 领取。

已确认投递的结果保持成功；批次及未确认任务都已使用当前目标时跳过，不重置等待时间。未曾启用回调的历史报告不纳入批量迁移。迁移不能撤回已经发出的 HTTP 请求，接收方仍应按 `finalReportId + type` 去重，五类报告分别确认。

### 2.4 检查执行结果

| 输出字段 | 含义 |
| --- | --- |
| `operationId` | 本次操作标识，写入每个实际迁移批次的审计记录 |
| `status` | `preview` 为预览完成，`scheduled` 为迁移调度完成，`partial_failure` 表示部分批次失败 |
| `scannedBatches` | 已扫描的批次数量 |
| `matchedBatches` | 需要迁移的批次数量 |
| `pendingDeliveries` | 已存在、尚未确认且目标地址需要改变的投递任务数量 |
| `migratedBatches` | 已完成迁移调度的批次数量；预览时为 0 |
| `failedBatches`、`errors` | 失败数量及批次 ID、异常类型摘要 |

`scheduled` 表示任务已经安排，不表示回调已收到 2xx 确认；实际结果通过下节的 `status`、报告详情中的 `delivery` 或接收端日志检查。仍在生成的批次可能没有投递任务，因此 `pendingDeliveries=0` 时仍可能有需要迁移的批次。

部分失败时命令以非零状态退出，其余批次继续处理。修复问题后可重复执行；中断前已经持久化的批次迁移意图由后台补偿，尚未处理的批次由重跑命令继续迁移。已迁移到当前地址的批次会跳过。

## 3. 单批次查询、补投与改址

以下 `<userId>`、`<finalReportId>` 须替换为该批次真实值，并保留引号。查询生成状态及五类报告的投递状态：

```bash
python -m scripts.report_callbacks status --user-id "<userId>" --final-report-id "<finalReportId>"
```

接收端已恢复，希望保留批次原地址并立即安排未确认结果补投：

```bash
python -m scripts.report_callbacks retry --user-id "<userId>" --final-report-id "<finalReportId>" --reason "接收端已恢复"
```

只修正一个批次的地址并安排补投，例如改为开发模拟接收端：

```bash
python -m scripts.report_callbacks retry --user-id "<userId>" --final-report-id "<finalReportId>" --target-url http://192.168.20.100:9080/callback --reason "开发联调切换到模拟回调"
```

`retry` 必须填写原因，可通过 `--operator` 指定操作人。省略 `--target-url` 时，各类型沿用批次保存的各自地址。显式提供 `--target-url` 会把该批全部未确认类型统一改投到指定地址。历史批次首次启用补投需要显式提供 `--target-url`，只投递已经保存的正文。已确认任务不会重新开启。

## 4. 重试配置与积压判断

默认失败后基础等待为 30、120、600、3600 秒，此后重复最后一项；默认加入最多 10% 的随机提前错峰，到期扫描周期为 5 秒。包括 4xx、5xx 和网络异常在内，未收到 2xx 的结果持续重试，不设置最大尝试次数。合法 `Retry-After` 可以进一步延后发送。全部配置、取值范围和计算规则见 [回调调度配置](../docs/architecture/report-agent-integration-guide.md#14-持久化受理生成中断恢复与长期回调)。

修改重试配置后重启生效，已保存的 `nextRetryAt` 和尝试次数不重算。整体迁移仅为需要改址的批次安排立即投递；地址本就正确而需要立即尝试的指定批次，使用 `retry`。

积压日志的 `pending` 合计尚未确认的 `pending`、`retry_wait`、`sending` 任务，默认每个回调 Worker 实例每 300 秒至多输出一次。`callback_scope=report` 表示 Report 队列，`callback_scope=gut_barrier` 表示 Gut Barrier 队列，两者分别统计和计时；多个进程也各自输出。它不是重试次数，`oldest_pending_at` 也不会随每次重试刷新；五类报告都未确认时，重试多次仍可能一直显示 `pending=5`。

判断是否重试应查看 `delivery.status`、`attempts`、`nextRetryAt`、`lastError`，并核对接收端日志。只有收到 2xx 且确认状态成功保存，任务才变为 `delivered` 并退出积压统计；命令完成或发起 HTTP 请求本身都不等于交付完成。
