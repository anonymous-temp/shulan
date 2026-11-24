# 压力测试方法与运行

> 文档状态：当前实现；方法与指标口径基线：2026-09-24；适用环境：`tests/loadtest/` 脚手架（Locust），压测机与被测服务分离；生产目标 Linux。本地实测结果见 [chat-stream 压力测试报告](./loadtest-report.md)。

## 1. 目标与边界

本文档定义 Shulan 后端压力测试的分层策略、场景、指标口径、成本纪律和已知限制。运行入口、命令和环境搭建见 `tests/loadtest/README.md`；本文不替代它。

压测目标是回答三个问题：基础设施层（Tornado、MongoDB、Redis）的容量拐点在哪里；真实 LLM 并发下首字延迟相对单并发基线劣化多少；长时间运行是否出现资源泄漏或租约连锁失败。压力测试不验证功能正确性——那是 `tests/unit/` 与 `tests/api/` 的职责。

## 2. 分层策略

服务是 LLM 瓶颈型 + SSE 长连接 + 会话级 Redis 租约串行的系统，不能对全部接口一锅端高压。场景按成本与依赖分层，见下表；每层使用独立 locustfile，物理隔离便宜场景与烧钱场景。

| 层 | 场景文件 | 是否触 LLM | 压法 | 回答的问题 |
|---|---|---|---|---|
| L0 冒烟 | `scenarios/smoke.py` | 否 | 1 用户单发 | 环境在线、脚本与契约正确 |
| L1 基建读 | `scenarios/infra_read.py` | 否 | 阶梯加压 10→50→100 | 事件循环、Mongo 连接池、画像读路径容量 |
| L2 完整回复 | `scenarios/chat.py`（tags: complete / suggestions） | 是 | 小并发画像 | 真实模型下完整回复延迟分布 |
| L3 SSE 流式 | `scenarios/chat.py`（tags: stream） | 是 | 小并发画像 | TTFT 与流总时长的并发劣化 |
| L2 旅程 | `scenarios/qa_journey.py` | 是 | 低并发完整旅程 | 建档多轮全链路（含租约、checkpoint 写入） |

## 3. 指标口径

- **TTFT（首字延迟）**：从发出 HTTP 请求到收到首个非空 `content`（聊天）或 `question`（建档）事件。`start`、`reasoning`、心跳不计入。该口径与 [问答响应延迟优化](./qa-response-latency.md) 的既有实测一致，压测报表中对应 `* [first content]` 行。
- **流总时长**：SSE 从连接到 `finish` 事件的完整时长，对应 locust 同名主行。
- **服务端对照**：每个响应包络自带 `elapsed_ms` 与 `uuid`，可与 Tornado 访问日志按 `uuid` 对齐，区分客户端观测延迟与服务端处理耗时。
- **错误分类**：409（会话租约冲突，压测身份独立时出现即为异常发现）、502（LLM 上游失败）、429（厂商限流）、5xx。

## 4. 成本纪律

所有触 LLM 场景受 `--enable-real-llm` 成本熔断保护，默认停用；显式开启后仍须遵守：并发 ≤ 10、单轮时长 ≤ 15 分钟、`thinking` 显式关闭、聊天会话每 6 轮轮换（控制 AION 上下文增长）。严禁对生产 MongoDB/Redis 压测；压测身份（userId/sessionId）每轮独立生成。

## 5. 环境策略

结论分级：本地 Windows 数字只做改动前后相对比较；容量结论必须在与生产同规格的 Linux 测试服得出，并经真实公网入口（含 Nginx 与安全组）验证——本地直连会绕过代理缓冲、TLS、限流等真实链路因素，SSE 的 `X-Accel-Buffering` 行为尤其需要在真实代理下复核。

## 6. 已知限制

1. `/api/v1/report` 与 `/api/v1/interpret/health-report` 未纳入场景：涉及 DocMind 解析成本与进程内任务（见 [Agent Core Wiki](./agents.md) 已知限制 18），列入路线图。
2. 未做 mock LLM 满压：AION 网关 base URL 当前硬编码，注入假客户端需要修改正式代码，与脚手架隔离原则冲突；待后续单独评审。
3. 建档旅程使用 answer/complete（非 stream）；stream 变体与 continue/complete 恢复路径列入路线图。
4. `qa/complete` 的 `round` 语义按 1 提交，若服务端校验变化需同步调整场景。
5. `/health` 处理器固定 `sleep(1)`，冒烟与探活一律使用 `/health/live` 与 `/health/ready`；该发现是否登记 ISSUE 由维护者决定。

## 7. 路线图

1. report/interpret 场景（需先评估 DocMind 成本控制与进程内任务的压测语义）。
2. mock LLM 满压方案（AION 出口可配置化或本地 mock 服务）。
3. 建档 stream 旅程与断连场景（验证后台完成与租约释放）。
4. 长时间浸泡测试指引（内存、句柄、checkpoint 增长观测）。
