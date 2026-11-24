# Shulan 压力测试工具（tests/loadtest/）

本目录是与主项目代码完全隔离的压测脚手架：不 import 任何项目代码，不读取项目 `.env`。请求与 SSE 契约以 `docs/api/API.md` 为准内嵌为常量。方法论文档见 `wiki/load-testing.md`。压测依赖的安装入口统一收敛到根目录两处：`requirements.txt` 的 `# loadtest` 段与 `pyproject.toml` 的 `loadtest` extras，版本保持一致，按下方方式选择使用。

## 1. 环境准备（一次性）

压测依赖统一登记在根目录 `requirements.txt` 的 `# loadtest` 段，与 `pyproject.toml` 的 `loadtest` extras 版本一致（当前 `locust==2.46.6`；升级时两处同步修改）。按机器角色选择安装方式：

**方式一（推荐）：纯压测机独立 conda 环境，直接安装 locust，依赖最小。**

```bash
# 项目根目录执行；Python 3.12 与主项目 requires-python 及 ruff target-version 对齐
conda create -y -n shulan_loadtest python=3.12
conda run -n shulan_loadtest pip install locust==2.46.6
```

无 conda 的机器（如 Linux 压测机）可用 venv 替代：

```bash
python3.12 -m venv loadtest/.venv
loadtest/.venv/bin/pip install locust==2.46.6
```

**方式二：已安装项目依赖、又需要压测能力的环境（如测试服），用项目 extras。**

```bash
pip install ".[loadtest]"   # 随项目一并安装 locust==2.46.6
```

`pip install -r requirements.txt` 会安装全部段落（含 `# loadtest` 段）；正式部署用 `pip install .` 不含 extras、不带入 locust，部署依赖体积不变。

源码已移至 `tests/loadtest/`；原 `loadtest/.venv`、缓存和 `loadtest/results` 数据保留原位，不随源码移动。下列 venv 路径仍从项目根目录解析。

下文 Windows 示例默认已 `conda activate shulan_loadtest`（直接用 `python`），Linux venv 用户把 `python` 替换为 `loadtest/.venv/bin/python` 即可。

## 2. 五分钟上手（第一次跟着做）

压测需要两个终端：终端 A 跑被测服务，终端 B 跑压测。

```bash
# 终端 A：正常启动服务，保持运行
conda activate shulan
python main.py

# 终端 B：激活压测环境并进入压测目录
conda activate shulan_loadtest
cd tests/loadtest
```

被测服务不在本机时（如局域网开发/测试服），必须在命令中加 `--host` 指定目标地址，否则所有请求会以 `HTTP 0` 连接失败收场（默认目标只指向本机 9002）：

```bash
python -m locust -f scenarios/smoke.py --headless -u 1 -r 1 -t 30s \
    --host http://<服务器IP>:<端口>     # 端口以该服务器 .env 的实际配置为准
```

可先用浏览器访问 `http://<IP>:<端口>/health/live` 确认连通。

第一步冒烟：1 个虚拟用户跑 30 秒，验证服务在线、脚本与契约正确（不触 LLM、零成本）：

```bash
python -m locust -f scenarios/smoke.py --headless -u 1 -r 1 -t 30s
```

结束时打印统计表（下方为本机实测节选，数值因环境而异）：

```
Type  Name                        # reqs   # fails   p50    p95
GET   /health/ready                  18    0(0%)     4       7
POST  /api/v1/report/list            18    0(0%)     5     190
POST  /api/v1/health-profile/…       18    0(0%)     9      81
      Aggregated                     72    0(0%)     5      81
```

读表要点（单位毫秒）：

- `# reqs` / `# fails`：发出与失败的请求数。冒烟阶段失败必须为 0；负载阶段关注失败是否随并发增长。
- `p50 / p95`：分位延迟，一半/95% 的请求快于该值。p95 是最常引用的容量指标；p95 上涨而 p50 不变，说明尾部开始劣化。
- 失败原因对照：409 = 会话租约冲突、502 = LLM 上游失败、429 = 厂商限流；响应包络自带 `uuid`，可与 Tornado 访问日志逐请求对齐定位。

冒烟通过后做第一次负载测试（L1，同样零成本）：

```bash
python -m locust -f scenarios/infra_read.py --headless -u 10 -r 5 -t 2m
```

按 10 → 50 → 100 并发各跑 2 分钟，对比 `# fails` 与 `p95` 的变化：p95 明显上涨或开始出现失败，就是容量拐点的信号。也可以去掉 `--headless` 用 Web UI 运行：浏览器打开 `http://localhost:8089`，在页面里填并发数与启动速率后点 Start，实时看曲线并可随时停止，适合探索阶段。

场景选择：日常验证用 `smoke`；测基建容量用 `infra_read`；测真实模型延迟用 `chat.py --tags stream`（看 TTFT 首字延迟）；测完整建档流程用 `qa_journey.py`。

## 3. 配置

配置全部通过命令行选项传入（在 locust 命令后追加，对当前次运行生效，不留全局状态）。

**本目录自定义选项**（由 `scenarios/_lib.py` 注册）：

| 命令行选项 | 默认值 | 说明 |
|---|---|---|
| `--enable-real-llm` | 关闭 | 成本熔断开关：加上才允许调用真实 LLM 的场景，防止误跑烧钱 |
| `--session-turns <N>` | `6` | 聊天会话轮换阈值，控制 AION 上下文增长（真实会话越久延迟越高） |
| `--loadtest-tag <标签>` | `adhoc` | 本次压测标签，仅提示用途 |

**locust 原生常用选项**（完整列表见 `python -m locust --help`；[官方文档](https://docs.locust.io/en/stable/configuration.html)）：

| 命令行选项 | 含义 | 示例值 |
|---|---|---|
| `-f <文件>` | 场景文件（locustfile）路径 | `scenarios/smoke.py` |
| `--headless` | 无界面纯命令行模式；不加则启动 Web UI（浏览器 `http://localhost:8089`） | — |
| `-u <N>` | 并发虚拟用户数（模拟同时在线的用户数） | `10` / `50` / `100` |
| `-r <N>` | 启动速率：每秒拉起多少个虚拟用户（爬坡速度，避免瞬间打死） | `5` / `10` |
| `-t <时长>` | 测试时长，到时自动结束；支持 s/m/h | `30s` / `2m` |
| `--host <URL>` | 被测服务地址（locust 原生选项，不传时用场景内置的本机默认值；格式校验见 `_lib`） | `http://192.168.20.100:9002` |
| `--tags <标签>` | 只运行带该标签的任务（chat.py 的 complete/stream/suggestions） | `stream` |
| `--csv <前缀>` | 导出 CSV 统计到 `results/<前缀>_stats.csv` 等多个文件（汇总+逐秒历史+失败+异常） | `results/infra-10u` |
| `--html <文件>` | 导出 HTML 图表报告（含交互式曲线） | `results/infra-10u.html` |
| `--loglevel <级别>` | 日志级别（排查脚本问题时用 DEBUG） | `INFO` |

压测机必须与被测服务分开部署（同机互相抢 CPU 会污染数据）；压测流量必须指向测试环境的独立 MongoDB/Redis，严禁指向生产。

## 4. 场景与命令

所有命令在 `tests/loadtest/` 目录执行，选项含义见第 3 节配置表。冒烟通过后再加压；加压曲线建议见 `wiki/load-testing.md`。

| 场景文件 | 层 | 是否 LLM | 说明 |
|---|---|---|---|
| `scenarios/smoke.py` | L0 | 否 | 探活 + 只读接口单发，验证环境与脚本 |
| `scenarios/infra_read.py` | L1 | 否 | 画像/报告读接口加权混合，可高压 |
| `scenarios/chat.py` | L2/L3 | 是 | `--tags complete` 完整回复、`--tags stream` SSE 采集 TTFT、`--tags suggestions` 推荐问题 |
| `scenarios/qa_journey.py` | L2 | 是 | 建档 create → 多轮 answer → 完整旅程 |

```bash
# L0 冒烟（先确认服务在线、契约正确）
cd tests/loadtest
python -m locust -f scenarios/smoke.py --headless -u 1 -r 1 -t 30s \
    --host http://<IP>:<端口> --csv results/smoke --html results/smoke.html

# L1 基建层：阶梯加压 10 → 50 → 100（分三次执行，对比拐点）
python -m locust -f scenarios/infra_read.py --headless -u 10 -r 5 -t 2m \
    --host http://<IP>:<端口> --csv results/infra-10u --html results/infra-10u.html
python -m locust -f scenarios/infra_read.py --headless -u 50 -r 5 -t 2m \
    --host http://<IP>:<端口> --csv results/infra-50u --html results/infra-50u.html
python -m locust -f scenarios/infra_read.py --headless -u 100 -r 10 -t 3m \
    --host http://<IP>:<端口> --csv results/infra-100u --html results/infra-100u.html

# L2/L3 真实 LLM：加 --enable-real-llm 熔断开关；成本纪律：并发 ≤ 10，时长 ≤ 15 分钟
python -m locust -f scenarios/chat.py --headless --tags stream -u 5 -r 1 -t 5m \
    --enable-real-llm --host http://<IP>:<端口> \
    --csv results/chat-stream-5u --html results/chat-stream-5u.html

# 建档完整旅程（最贵的真实场景，并发从 2 开始）
python -m locust -f scenarios/qa_journey.py --headless -u 2 -r 1 -t 10m --enable-real-llm \
    --host http://<IP>:<端口> --csv results/qa-2u --html results/qa-2u.html
```

Web UI 模式（交互式调并发、实时看图）：去掉 `--headless`，浏览器打开 `http://localhost:8089`。导出文件说明：`--csv` 生成多个文件（`_stats.csv` 汇总、`_stats_history.csv` 逐秒历史、`_failures.csv` 失败、`_exceptions.csv` 异常），做档位对比用 `_stats_history.csv` 最方便；`--html` 是含交互曲线的图表报告。`tests/loadtest/results/` 已在根 `.gitignore` 忽略，产物不入库。

## 5. 指标口径

- 常规接口：locust 内置 avg / p95 / RPS / 失败率。
- SSE 流式：`[first content]` 行是 **TTFT（首个非空 content 事件）**，与 `wiki/qa-response-latency.md` 口径一致（排除 start、reasoning、心跳）；同名主行是流总时长。
- 服务端对照：每个响应包络自带 `elapsed_ms`（服务端处理耗时）与 `uuid`，可与 Tornado 访问日志按 uuid 对齐，区分“慢在服务端还是慢在链路”。

## 6. 压测期间监控清单（开压前就位）

- `/health/ready` 返回的 `activeRequests` / `activeTasks`（进程侧请求水位）；
- 进程 CPU / 内存 / 句柄数；Linux 上 `pidstat -p <pid> 1`；
- MongoDB 连接数与慢查询、Redis 秒延迟（`redis-cli --latency`）；
- 错误分类：409（会话租约冲突）、502（LLM 上游失败）、429（厂商限流）；
- Tornado 日志中的 5xx 与 SSE error 事件比例。

## 7. 纪律与红线

1. 真实 LLM 场景默认熔断；放开后限并发（≤10）、限时长（≤15min）、thinking 关闭。
2. 压测身份（userId/sessionId）每轮独立生成，严禁指向真实用户数据。
3. 容量结论必须在与生产同规格的 Linux 测试服得出；本地 Windows 数字只做相对对比。
4. 结果归档：`results/` 只留每次压测的一页汇总（日期、环境、并发、p95、结论）；该目录已被 git 忽略，原始产物不入库。
