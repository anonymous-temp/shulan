# Tornado 安全停服与生产更新方案

> 状态：应用排空、Report 持久化受理与恢复已接线；生产部署及 Linux 真实信号验证待完成
>
> 更新日期：2026-10-10；源码基线 `develop@c9b8988`，本轮未执行 Linux/进程管理器或真实数据库验收
>
> 适用范围：`main.py`、Tornado HTTP/SSE 服务、进程内后台任务、周期 Worker 与生产发布流程

第 2—5 节说明现有排空机制；第 6 节区分 Report 当前持久化实现与进一步独立 Worker 进程的目标。正常 SIGTERM 排空不等于中断任务继续生成，恢复策略见 Report 指南第 14 节。旧迁移手册仅供历史参考，最新状态见 [架构导航](README.md)。

## 1. 结论

本方案的核心不是严格零停机，而是“在途工作无损排空”：Shulan 收到 `SIGTERM` 后停止接收新业务请求和领取新任务，已经进入 Handler 的 HTTP/SSE 请求、已经登记的最终报告任务以及正在执行的周期 Worker 必须继续自然完成。只有活跃计数全部归零后，旧实例才释放资源并退出。

单实例可以在旧实例排空退出后再更新；需要持续接收新流量时，可以先启动新实例并切流，再排空旧实例。两种发布方式使用同一应用协议，滚动或蓝绿发布不是应用正确性的前置条件。

正常发布没有“到达固定 deadline 后自动取消”的路径。等待时间超过预期时应持续记录进度、告警并把发布标记为未完成，旧实例继续排空。`SIGKILL`、OOM、宿主机掉电和解释器崩溃没有清理机会；Report 已接入持久化、幂等、租约和恢复机制，但恢复可能以失败产物收尾，仍需真实故障注入验收，不能等同于无损继续生成。

## 2. 当前实现与剩余风险

当前启动和退出链路为：

```text
main.py
  -> server.run()
  -> bootstrap 构造 AppContainer / ApplicationBundle
  -> 创建 ApplicationLifecycle、TaskSupervisor 与资源 Registry
  -> runtime.application.run_application()
  -> HTTPServer.listen()
  -> 启动 Outbox、画像、建档创建恢复及 Report/Gut Barrier 生成、恢复、投递周期任务
  -> IOLoop.start()
  -> SIGTERM/SIGINT 触发 DRAINING
  -> 停止监听、停止调度和领取新任务
  -> 请求、任务全部归零后关闭连接并停止 IOLoop
  -> ApplicationBundle.aclose() / ResourceRegistry 关闭托管资源
```

已经实现：

1. 注册 `SIGTERM/SIGINT`，重复信号只记录排空进度，不取消存量工作。
2. `ApplicationLifecycle` 原子协调 readiness、新请求登记和活跃请求计数。
3. `TaskSupervisor` 追踪最终报告、启动回调和周期 Worker，只等待自然完成。
4. Report 生产受理先确认持久化批次再响应，由受监督的生成 Worker 执行；显式旧 Pipeline 构造仍保留响应/执行顺序的兼容路径。
5. `HTTPServer.stop()`、keep-alive 新请求 503、`/health/live` 与 `/health/ready` 已接入。
6. 画像 Worker 收到排空指令后完成当前已领取任务，不再领取同一批次的后续任务。

剩余风险：

1. Report 已有持久化受理/恢复，真实 MongoDB 多进程、强退与回调接收端幂等尚需部署验收；中断恢复不重放模型调用。
2. 当前画像长任务 lease 没有自动续期，极长任务可能在另一实例被重复领取。
3. systemd、容器或发布脚本如果配置了有限且过短的停止超时，仍会提前发送 `SIGKILL`。
4. Linux 真实信号、长 SSE、长报告和生产负载均衡切流尚未完成部署环境演练。
5. Tornado 的 `HTTPServer.close_all_connections()` 不会关闭 WebSocket。当前 Tornado 应用没有 WebSocket 路由，语音 WebSocket 由独立 EviMed 服务提供；未来若在本进程新增 WebSocket，必须增加连接登记、停止新建会话和等待/通知存量会话退出的专用排空协议。

## 3. 保证边界

| 场景 | 应用能够提供的保证 | 必要条件 |
|---|---|---|
| 正常发布、`SIGTERM` | 停止接收新工作，等待存量请求与任务全部完成 | 生命周期管理、进程管理器不强杀 |
| 等待超过预期 | 保持 `DRAINING`、持续记录进度并告警 | 不把告警阈值当作取消期限 |
| `SIGINT` | 本地运行时与 `SIGTERM` 使用同一收尾流程 | 信号处理器 |
| 第二次停止信号 | 记录当前计数，继续等待存量工作 | 重复信号不降低保证 |
| `SIGKILL`、OOM、掉电、崩溃 | 进程内不可能执行清理 | 持久化任务、幂等与恢复 |

因此，当前第一阶段验收必须覆盖：

- 正常停止时不截断已经受理的工作；
- 排空开始后不再接收新业务工作；
- 活跃计数未归零时旧实例不退出。

非正常停止后的恢复和外部副作用幂等属于第二阶段可靠任务建设。

## 4. 目标生命周期

### 4.1 状态模型

应用生命周期包含四个状态：

```text
STARTING -> READY -> DRAINING -> STOPPED
```

| 状态 | 接收业务请求 | 领取后台任务 | readiness | liveness |
|---|---:|---:|---:|---:|
| `STARTING` | 否 | 否 | 503 | 200 |
| `READY` | 是 | 是 | 200 | 200 |
| `DRAINING` | 否 | 否 | 503 | 200 |
| `STOPPED` | 否 | 否 | 不可访问 | 不可访问 |

`liveness` 只表示进程和事件循环仍可工作；`readiness` 表示实例是否可以接收新业务。当前已经提供 `/health/live` 与 `/health/ready`；兼容 `/health` 始终返回 HTTP 200，并在响应字段中暴露 readiness 和活跃计数。

### 4.2 关闭顺序

收到第一次 `SIGTERM` 或 `SIGINT` 后必须按以下顺序执行：

1. 原子地把生命周期切换为 `DRAINING`，重复信号处理不得重复启动关闭协程。
2. readiness 立即失败，让负载均衡器停止选择该实例。
3. 调用 `HTTPServer.stop()` 停止监听新连接。
4. 对已有 keep-alive 连接上的新业务请求返回 `503 Service Unavailable`，并设置 `Connection: close` 与合理的 `Retry-After`。
5. 停止全部已登记的 PeriodicCallback，并通知各 Worker 停止领取新工作。
6. 等待已经进入 Handler 的普通 HTTP/SSE 请求结束。
7. 等待已经登记的进程内后台任务与正在执行的 Worker 回调结束。
8. 等待期间定期记录剩余请求/任务数量和任务名称；不自动取消。
9. 所有计数归零后调用 `HTTPServer.close_all_connections()` 清理空闲连接。
10. 按所有权关闭 AgentRuntime、HTTP Client、数据库 Client 和其它应用级资源。
11. 返回 `main()`，让进程以退出码 0 正常结束。

第二次收到停止信号只记录当前排空进度，不改变关闭流程。人工 `SIGKILL` 是放弃无损保证的应急操作，不属于正常发布协议。

## 5. 应用内组件设计

### 5.1 `ApplicationLifecycle`

现有应用级生命周期对象由组合根创建、Container 持有，并通过兼容 settings 提供给尚未迁移的 Handler 基类。它只管理状态和计数，不包含业务逻辑；类型化 WebRuntimeContext 仍待实现：

```python
class ApplicationLifecycle:
    @property
    def is_ready(self) -> bool: ...
    def try_begin_request(self) -> RequestLease | None: ...
    def begin_draining(self) -> bool: ...
    async def wait_for_requests(self) -> None: ...
```

要求：

- `begin_draining()` 幂等，并与新请求登记使用同一同步边界，不能出现请求已经通过检查但没有被计入的窗口。
- Request lease 必须在所有正常、异常和客户端断连路径释放。
- readiness、请求拒绝和关闭日志都读取同一个状态源。
- 健康检查和必要的内部诊断请求可以在 `DRAINING` 状态继续访问，业务路由不可以。

### 5.2 Handler 请求追踪

所有 Handler 使用 `LifecycleRequestHandler` 统一登记和释放请求租约。非 Report Handler 通过 BaseHandler 接入；已迁入 `web/handlers/report.py` 的 ReportGenerateHandler 也使用同一生命周期基类，其业务依赖已显式注入。后续改造运行时上下文时必须保持该请求租约语义，并删除从 Tornado settings 读取 lifecycle 的兼容路径。

SSE Handler 的活跃期是服务端生成与业务持久化全部结束，而不是客户端 socket 断开。当前 Chat 流式接口在客户端断开后会继续消费模型输出并保存会话，生命周期计数必须覆盖这段后台执行时间。

### 5.3 可排空的任务管理器

项目已经实现 TaskSupervisor，其正常关闭等待自然完成而不是 cancel-first：

```python
class TaskSupervisor:
    def create_task(self, coro, *, name: str) -> asyncio.Task: ...
    def stop_accepting(self) -> None: ...
    async def wait_idle(self) -> None: ...
```

约束：

- 周期调度在 `DRAINING` 后立即停止。已经进入 Handler 的请求仍可登记其后台任务，因此 Supervisor 要在请求归零后才关闭准入。
- 关闭准入后拒绝新任务并关闭传入的 coroutine，避免未 await 警告。
- `wait_idle()` 只等待自然完成，不提供自动 `cancel()` 路径。
- 任务执行期间派生的子任务也必须通过同一 Supervisor 登记；等待逻辑应持续检查任务集合，不能只对开始关闭时的快照等待一次。
- 完成回调只记录任务标识、耗时和异常类型，不记录健康正文或报告正文。
- 任务应在外部成功响应前完成登记，消除“已经返回受理，但停服时任务尚未进入追踪集合”的竞态。

下列现有任务必须纳入 Supervisor：

- `/api/v1/report` 生成与回调任务；
- 启动阶段通过 runtime.application 调度的两次初始队列扫描；
- 每次 Outbox 周期投递；
- 每次用户画像摄取周期执行；
- 后续新增的所有 fire-and-forget 协程。

### 5.4 Worker 停止协议

周期 Worker 的停止由两层协作完成：

```python
worker.stop_accepting()
# 当前 callback 由 TaskSupervisor.wait_idle() 统一等待
```

`PeriodicCallback.stop()` 禁止下一次调度；`worker.stop_accepting()` 让正在运行的画像批次在完成当前已领取任务后退出，不再领取下一项。持久化队列任务写入、完成和失败转换必须继续保持幂等。

当前画像队列具备 `pending/retry_wait/running/completed/dead_letter`、原子领取和过期租约重领；画像长任务续租仍是独立待办。Report 已有自己的生成/投递状态、领取、生成续租和中断恢复，不能套用画像的死信状态或把画像续租缺口描述为 Report 尚未实现。

### 5.5 资源所有权

只有创建资源的组件负责关闭资源：

- 组合根创建的 AgentRuntime 由应用生命周期关闭；测试注入的外部 Runtime 不自动关闭。
- 应用级 HTTP Client、MongoDB Client、Redis Client 和线程/进程池需要显式生命周期接口。
- Handler 不创建需要跨请求复用的 Client，也不在 `on_finish()` 关闭共享资源。
- 关闭顺序必须在任务排空之后，避免任务仍在运行时先关闭数据库或模型 Client。

## 6. 最终报告任务可靠化

### 6.1 当前生产实现

Report Handler 注入 DurableReportService，受理记录确认保存后才返回 processing；任务无法持久化时返回 503。批次、产物和回调记录分别存储，runtime 周期调度生成、恢复及补投。旧 Pipeline/协调器仅供显式兼容构造。

重启后待开始任务继续调度；已开始但租约过期的任务保留既有产物，为缺失类型保存失败结构，不重新解析或调用模型。产物保存及入队的中间失败通过补偿扫描收敛。回调持续退避补投、没有死信终止或自动过期，接收端按稳定 Idempotency-Key 去重。

### 6.2 状态与故障边界

内部 generation/delivery 状态分别管理，不能与公开 success/partial_success/failed 混用。批次记录规范化请求、来源、回调目标、生成状态及租约；投递记录保存目标、摘要、尝试次数和下次投递时间。实际字段、续租、补偿与 redrive 以 [Report 指南第 14 节](report-agent-integration-guide.md#14-持久化受理生成中断恢复与长期回调) 和当前端口/存储实现为准，不执行旧 activation/release 或死信状态草案。

肠道屏障使用独立 GutBarrierService 和任务集合，同样接入运行时的生成、恢复与主报告投递调度；解读委托给原报告模块，不由肠道屏障新建解读队列，见 [肠道屏障 Wiki](../../wiki/gut-barrier.md)。这些机制需要真实数据库和故障注入验收，源码接线不表示生产保障已经验证。

### 6.3 Web 与 Worker 分离

长期目标将 Web 接入与报告 Worker 分成独立进程：

```text
Tornado Web：校验请求 -> 持久化受理 -> 返回 processing
Report Worker：领取/续租 -> 生成并保存产物；独立投递 Worker 读取固定产物补投
```

这样 Web 更新不需要等待长报告任务；Worker 更新时只停止领取新任务并排空当前任务。发布不同版本 Worker 时，任务中必须记录 pipeline 版本，或者保证新版本能够兼容旧任务快照。

## 7. 等待、告警与强制退出边界

当前正常关闭不配置应用级强制 deadline。排空协程每 30 秒记录一次阶段、活跃数量和非敏感任务名；这个周期只是可观测性阈值，不会触发任务取消。

生产平台可以根据 p95/p99 和业务上限设置“排空过慢”告警，但告警动作应是暂停发布、排查卡住的上游或任务，而不是自动杀死旧实例。只有运维人员明确决定放弃无损保证时，才允许使用 `SIGKILL` 等应急手段，并记录受影响任务以便补偿。

后续将 Worker 拆成独立进程后，可分别制定 Web 与 Worker 停止预算。当前 Report 虽已持久受理，生成仍在 Web 进程内，强退恢复也不重放模型调用；进程管理器的停止上限必须覆盖合法在途工作，要求自然完成的环境应禁用自动强杀。

## 8. 生产更新流程

### 8.1 单实例排空升级

1. 从上游流量入口摘除实例，或接受升级期间新请求暂时返回失败。
2. 向旧实例发送 `SIGTERM`。
3. 观察旧实例进入 `DRAINING`，等待请求和任务计数归零并正常退出。
4. 更新代码并启动新实例。
5. `/health/ready` 返回 HTTP 200 后恢复流量。

该模式满足“存量工作不受影响”，但不保证升级窗口持续接收新请求。

### 8.2 双实例滚动或蓝绿发布

1. 在新端口或新容器启动新版本。
2. 等待新实例完成依赖初始化且 readiness 为 200。
3. 将新流量切换至新实例。
4. 把旧实例从负载均衡 upstream 移除。
5. 向旧实例发送 `SIGTERM`。
6. 观察旧实例进入 `DRAINING`，活跃请求和任务数逐步归零。
7. 旧实例正常退出后完成发布；等待异常时暂停发布并排查，不自动强制结束。
8. 新版本异常时，在数据兼容允许的前提下把流量切回旧版本。

旧实例排空期间不得与新实例重复领取同一任务。画像、Report 和肠道屏障均已有各自 MongoDB 租约；Report 生成同时续租，并按 owner/token 隔离过期执行者。部署仍需验证多实例领取、续租失败和旧 owner 迟到提交。

### 8.3 systemd

生产 Linux 建议由 systemd 管理进程，使用 `systemctl restart shulan`，不再人工寻找 PID。关键约束如下：

```ini
[Service]
ExecStart=/path/to/conda/envs/shulan/bin/python /path/to/shulan/main.py
KillSignal=SIGTERM
TimeoutStopSec=infinity
Restart=on-failure
```

`ExecStart` 必须让 Python 成为 systemd 直接管理的主进程，不能用不转发信号的 shell 包装。发布脚本必须检查新进程 readiness，不能把“进程已经启动”当作“服务已经可接流量”。

### 8.4 Docker/Compose

容器入口使用 exec form，确保 Python 为 PID 1 或由能够转发信号的 init 管理：

```dockerfile
CMD ["python", "main.py"]
```

容器运行时通常在停止宽限期后发送 SIGKILL。即使报告已有持久受理，正常发布仍需等待在途请求和生成自然结束：evaluate 上限与后续报告独立预算不能合成一个固定 30 分钟总时限。强退恢复可能以失败结果结束缺失产物，不等同于无损继续生成；旧 35m 示例不能作为安全值，本仓库尚无经生产压测验证的停止预算。

在实际进程管理器中如何等待无固定应用 deadline 的排空，需要部署负责人结合观测与人工应急策略确认。本文不自动变更 systemd、容器或操作系统参数；操作系统强绑定方案仍需与维护者评审。

### 8.5 禁止操作

- 不使用 `kill -9`、`pkill -9` 或容器强制删除作为正常发布命令。
- 不先关闭 MongoDB/Redis/AgentRuntime，再等待业务任务。
- 不通过公开、无鉴权的 HTTP `/shutdown` 接口代替进程信号。
- 不在单实例上执行“先停旧版本、再启动新版本”并宣称零停机。
- 不把告警阈值直接配置成自动取消或 `SIGKILL` 时限；卡住时暂停发布并人工决策。

## 9. 可观测性与告警

关闭过程使用结构化日志，至少记录：

- 关闭原因与信号；
- 生命周期状态变化；
- 活跃请求、SSE、后台任务和 Worker 数量；
- 每个阶段耗时；
- 长时间未完成任务的名称、类别和运行时长；
- 正常退出或人工强制终止的最终结果。

建议指标：

```text
shulan_lifecycle_state
shulan_active_requests
shulan_active_sse_requests
shulan_background_tasks
shulan_worker_jobs_active
shulan_shutdown_duration_seconds
shulan_shutdown_forced_total
shulan_job_recovered_total
```

不得在指标、任务名或关闭日志中放入用户健康正文、报告内容、Prompt、API Key 或完整 URL 查询参数。

## 10. 测试与验收

### 10.1 无网络单元测试

- 生命周期只允许 `STARTING -> READY -> DRAINING -> STOPPED`。
- `begin_draining()` 幂等，并与新请求登记无竞态。
- draining 后业务请求返回 503，健康诊断仍可访问。
- TaskSupervisor 只等待自然完成，关闭准入后拒绝新任务。
- 任务完成、抛错、被取消时计数都能归零。
- 重复停止信号不取消已有任务。
- 资源关闭顺序为任务在前、依赖在后。

### 10.2 Tornado 集成测试

- 长 HTTP 请求执行期间发送 `SIGTERM`，请求成功返回后进程退出。
- SSE 客户端连接期间发送 `SIGTERM`，已经开始的服务端生成和落库完成。
- keep-alive 连接在 draining 后的新请求收到 503。
- 周期 Worker 正在执行时停止调度，当前任务完成但不领取下一项。
- 生产 Report 持久化失败返回 503；持久受理后生成 Worker 受监督，排空停止新领取并等待在途工作。
- 显式旧 Pipeline 的响应/执行顺序作为兼容测试保留，不把其 0.01 秒让出事件循环视为生产持久化协议。

### 10.3 Linux 进程测试

- 使用真实 `SIGTERM`，验证退出码、总耗时与日志顺序。
- 使用第二次 `SIGTERM` 验证仍继续排空。
- 使用进程管理器验证其不会在合法排空期间强制结束进程。
- 强制 SIGKILL 后，验证待开始 Report 重新调度、已开始任务补齐失败产物、旧 owner 迟到结果受隔离和回调至少一次交付；画像、建档与卡片 Outbox 按各自机制分别验证。
- 双实例切流时验证新请求只进入新实例，旧 SSE/请求在旧实例完成。

### 10.4 验收标准

- 正常 `SIGTERM` 不截断请求和已登记任务。
- draining 后不再受理新的业务工作或领取新队列任务。
- 活跃计数未归零时进程不自动退出，全部归零后正常退出。
- 全部资源关闭一次且没有“事件循环已关闭”类退出日志异常。

任务异常恢复、重试去重和幂等回调是阶段 B 的验收标准。

## 11. 实施计划

### 阶段 A：应用级无损排空（已实现，待生产验证）

- [x] 引入 `ApplicationLifecycle` 与 `TaskSupervisor`。
- [x] 注册 `SIGTERM/SIGINT` 并实现请求在前、任务在后、资源最后的关闭顺序。
- [x] 追踪 Handler、SSE、最终报告、启动回调和周期任务。
- [x] 增加 liveness/readiness、请求拒绝、任务排空和关闭顺序测试。
- [ ] 在 Linux 生产等价环境完成真实信号和长任务演练。

### 阶段 B：最终报告持久化（源码已接线，待部署验收）

- [x] 持久化受理、请求复用、原子领取与租约续期。
- [x] 已生成产物保留、运行中断补齐失败结构及补偿扫描。
- [x] 生成和回调投递分离，稳定幂等键、持续补投及运维 redrive。
- [ ] 真实 MongoDB 多实例、主节点切换、强退、重复领取与接收端幂等验证。
- [ ] 独立 Worker 进程及完整模块资源契约按后续架构切片评审。

### 阶段 C：生产发布接入

- 根据可用性要求选择 systemd 单实例排空、双实例或容器编排。
- 配置 readiness、禁止提前强杀和发布健康门禁。
- 完成 Linux 故障注入、压测与回滚演练。

## 12. 文件日志部署约束

服务默认在项目根目录的 `logs/all/`、`logs/system/`、`logs/user/<user_id>/` 分别保存全量、系统及用户日志，各自的当前文件和归档放在同一目录，配置和归属规则见 [API 请求与响应日志](../../wiki/http-logging.md)。`SHULAN_LOG_DIR` 可指定绝对路径，运行账号需要创建、追加、重命名和清理该目录内日志的权限。

升级后旧布局 `logs/*.log`、`logs/users/*.log` 不迁移、不再写入或自动清理，需自行保管。新布局不跟随符号链接或 Windows 目录联接；请使用实际目录。

每个日志目录只允许一个服务进程写入；多实例和发布时的新旧实例必须使用不同目录。文件层支持本进程的协程及线程并发，不提供多个进程共同轮转同一文件的协调机制，也不要与外部 logrotate 同时轮转这些文件。

正常关闭先完成请求、后台任务和资源释放，再排空文件写入队列并关闭文件。不要在排空期间强杀进程。文件队列拥堵时同步写入以优先保存记录，磁盘故障会在控制台产生限频提示。

## 13. 参考资料

- [Tornado HTTPServer](https://www.tornadoweb.org/en/stable/httpserver.html)
- [Tornado TCPServer.stop](https://www.tornadoweb.org/en/stable/tcpserver.html)
- [Docker Compose `stop_grace_period`](https://docs.docker.com/reference/compose-file/services/)
- [Kubernetes Pod termination](https://kubernetes.io/docs/concepts/workloads/pods/pod-lifecycle/#pod-termination)
- [统一应用分层与目录治理方案](./application-architecture.md)
