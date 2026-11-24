# Shulan 测试规范

本文是项目测试文件的统一组织与执行规范。所有新增测试都必须放在 `tests/` 下。

## 1. 目录职责

```text
tests/
├── unit/                         # 离线测试；pytest 唯一默认收集范围
│   ├── _support/
│   │   ├── agents/               # 假 LLM、知识库和内存上下文仓库
│   │   ├── analysis_benchmark.py  # Analysis 样例、纯质量检查和内存替身
│   │   └── harnesses/            # 共享验收过程；真实依赖由 API 入口注入
│   ├── tools/                    # 报告、QA 文档、Record 的离线验收入口
│   ├── agents/                   # Agent 核心、网关、持久化和图
│   ├── services/                 # 应用服务与文档解析流程
│   ├── server/                   # HTTP/SSE Handler 和入口边界契约
│   ├── web/                      # Web 中间件与生命周期
│   ├── runtime/                  # 资源、Worker 与回调调度契约
│   ├── bootstrap/                # 容器装配与资源注入
│   ├── architecture/             # 模块、路由与依赖边界
│   ├── report/                   # 风险报告、持久任务、回调与预览
│   ├── gut_barrier/              # 肠道屏障任务、回调与预览
│   ├── health_profile/           # 画像抽取、事实与证据
│   ├── health_analysis/          # 多卷、三源、循证/覆盖/发布、期限与追溯
│   ├── log/                      # 日志和文件轮转
│   └── api/                      # API 页面离线测试和浏览器 benchmark
├── api/                          # 手动 API 联调页面及配套验收工具
│   ├── pages/                    # 浏览器联调页面及资源
│   ├── scripts/                  # API、真实模型和 DocMind 验收入口
│   │   └── legacy/               # 原有遗留联调脚本
│   ├── integration/gut_barrier/   # 真实 MongoDB 验收，保留原门控和跳过条件
│   └── serve_pages.py            # 静态页面、同源代理与模拟回调接收端
├── loadtest/                     # 独立的性能与压力测试
│   └── scenarios/                # Locust 场景与辅助库
└── fixtures/analysis/             # 原有归档与恢复样例；位置不变
```

分类原则：

- `tests/unit/` 中的测试必须可重复执行，默认不得连接真实 LLM、MongoDB、Redis、文件服务或回调服务。
- `tests/unit/` 按生产代码的领域和模块组织，而不是按开发人员或迭代批次组织。
- `tests/api/` 调用正在运行的真实后端，可能产生数据库写入、模型费用和外部回调，必须由开发者主动运行。
- 浏览器页面是手工端到端联调工具，不以页面操作代替自动化契约测试。
- `tests/loadtest/` 存放独立的压力测试工具，通过 Locust 手动运行，不属于 pytest 默认收集范围。
- `legacy/` 只用于迁移期保留；新代码不得加入该目录。

用户健康画像还提供命令行真实联调入口：

```bash
python -m tests.api.scripts.health_profile
python -m tests.api.scripts.health_profile --help
```

默认命令会创建一个随机 `test-` 用户的空画像并执行读取检查。事实确认、更正或删除必须显式提供已有事实 ID、版本参数和 `--yes`，避免误修改真实用户数据。

### 验收工具迁移对照

| 原位置 | 当前实现与入口 |
|---|---|
| `api_test/run_followups.py` | `_support/harnesses/run_followups.py` 与 `followup_options.py` 保存共用过程；入口为 `tests.unit.tools.run_followups` 和 `tests.api.scripts.run_followups` |
| `scripts/analysis_first_pass_benchmark.py` | `tests/api/scripts/analysis_first_pass_benchmark.py`；纯辅助代码位于 `tests/unit/_support/analysis_benchmark.py` |
| `scripts/analysis_recovery_benchmark.py` | `tests/api/scripts/analysis_recovery_benchmark.py`，复用同一纯辅助模块 |
| `scripts/qa_document_acceptance.py` | `tests/unit/_support/harnesses/qa_document_acceptance.py`；入口为 `tests.unit.tools.qa_document_acceptance` 和 `tests.api.scripts.qa_document_acceptance` |
| `scripts/record_dialogue_acceptance.py` | `tests/unit/_support/harnesses/record_dialogue_acceptance.py`；入口为 `tests.unit.tools.record_dialogue_acceptance` 和 `tests.api.scripts.record_dialogue_acceptance` |
| `tests/integration/gut_barrier/test_real_mongo_acceptance.py` | `tests/api/integration/gut_barrier/test_real_mongo_acceptance.py` |
| `loadtest/README.md`、`loadtest/scenarios/*.py` | `tests/loadtest/README.md`、`tests/loadtest/scenarios/*.py` |
| `tests/api/scripts/stream_browser_benchmark.cjs` | `tests/unit/api/stream_browser_benchmark.cjs`，继续读取 `tests/api/pages` 的资源 |
| `agents/testing/{__init__,fake_llm,fake_knowledge,memory_context}.py` | `tests/unit/_support/agents/` 下同名四个文件 |

表中 `_support` 路径均位于 `tests/unit/`。旧位置不保留可执行兼容入口。文件定位与导入随代码迁移；`tests/fixtures/analysis`、可选 `api_test/callback.json`、报告默认输出 `api_test/out`（含 `gutBarrier` 子目录）保持原样。样例缺失时仍按原条件跳过或报错，不自动补齐。旧 `loadtest` 中的本地虚拟环境、缓存和已有结果保留原位；历史记录中的 `loadtest/results` 仍指原数据。

### 验收工具命令

所有 Python 命令从项目根目录执行，输出路径继续遵循各工具原有限制。

```bash
# 离线入口：假模型、模拟目录、仅写文件
python -m tests.unit.tools.run_followups api_test/callback.json --mode fake
python -m tests.unit.tools.qa_document_acceptance --smoke --output /tmp/qa-smoke.json
python -m tests.unit.tools.record_dialogue_acceptance --output-dir C:/Temp/record-smoke

# 真实模型/API 入口：仅在明确需要真实调用时手动运行
python -m tests.api.scripts.run_followups api_test/callback.json --mode api
python -m tests.api.scripts.qa_document_acceptance --live --config .env --file-url PUBLIC_PDF_URL --output /tmp/qa-live.json
python -m tests.api.scripts.record_dialogue_acceptance --live --config .env --output-dir C:/Temp/record-live
python -m tests.api.scripts.analysis_first_pass_benchmark --help
python -m tests.api.scripts.analysis_recovery_benchmark --help

# 真实 MongoDB 验收：仍须设置原有 GUT_BARRIER_TEST_MONGO_URI 门控
python -m pytest tests/api/integration/gut_barrier/test_real_mongo_acceptance.py

# 离线 API 页面回归及浏览器 benchmark
node --test tests/unit/api/api_lab.test.cjs
node tests/unit/api/stream_browser_benchmark.cjs --output=/tmp/shulan-stream-current
```

报告工具原有 `mode`、`catalog`、`deliver` 组合完整保留。只要 `--mode llm/api`、`--catalog real`、`--deliver http` 任一启用，就使用 API 入口；其余组合使用离线入口。QA 的 `--smoke` 与 Record 默认/`--smoke` 使用离线入口，`--live` 使用 API 入口。进入错误类别时会提示对应新命令，退出前不会自动启用真实依赖。

浏览器 benchmark 的实际参数和资源位置见 [流式页面验证](api/STREAMING.md)；其原脚本不提供 argparse 风格的帮助，使用文档中的 Node 命令运行。压测从 `tests/loadtest` 目录运行，保持原 Locust 参数，详见 [压测运行说明](loadtest/README.md)。运维、清理、恢复、格式化和报告预览工具继续放在项目根 `scripts/`。

## 2. 自动化测试命名

- 测试模块使用 `test_<module>.py`。
- pytest 测试函数使用 `test_<behavior>`；一个测试只验证一个明确行为。
- 测试类使用 `Test<Subject>`，或继承 `unittest.TestCase` 时沿用现有类名。
- 不使用 `redis.py`、`server.py` 等可能遮蔽源码或三方包的名字。
- 共享 fixture 放在最接近使用范围的 `conftest.py`；只有跨多个顶层模块共享时才放到 `tests/unit/conftest.py`。

示例：

```text
agents/llm/aion_gateway.py
└── tests/unit/agents/llm/test_aion_llm_gateway.py

modules/chat/application/service.py
└── tests/unit/services/chat/test_chat_service_agent.py

web/handlers/chat.py 中的 Chat Handler
└── tests/unit/server/test_chat_api_contract.py

web/routes.py 与 server.py 兼容门面
└── tests/unit/architecture/test_web_architecture.py

bootstrap/factory.py 与 runtime/resources.py
├── tests/unit/bootstrap/test_factory.py
└── tests/unit/runtime/test_resources.py
```

## 3. 环境与运行方式

项目最终运行于 Linux；本地和远程测试统一使用 `shulan` Conda 环境，并从项目根目录执行。

```bash
conda activate shulan
pip install .[dev]

# 推荐：运行全部自动化测试
python -m pytest

# 指定领域、模块或用例
python -m pytest tests/unit/agents
python -m pytest tests/unit/bootstrap tests/unit/runtime
python -m pytest tests/unit/server/test_chat_api_contract.py
python -m pytest tests/unit/server/test_chat_api_contract.py::ChatApiContractTest
```

迁移期内，纯 `unittest` 模块也可以单独运行：

```bash
python -m unittest tests.unit.agents.core.test_agent_core -v
```

不要使用 `python tests/unit/.../test_xxx.py`。以文件路径直接启动会改变 Python 的模块搜索起点，容易导致项目包不可见或测试文件遮蔽同名依赖。

PyCharm 中应创建 pytest 配置：Target 指向 `tests/unit` 或具体测试文件，Working directory 指向项目根目录，解释器选择 `shulan`。远程开发时先确认测试文件已经同步。

## 4. 配置与数据安全

共享文件工作流的离线回归位于 `tests/unit/services/analysis/test_document_workflow.py`，覆盖全文中段、混合报告、独立子报告日期、异常模型输出、分段恢复、版本化扩展结果、原报告指标分页、画像补投和建档多附件上下文；第二阶段的内容身份、租约与任务恢复回归继续保留在 `test_document_reuse.py`。HTTP 查询契约位于 `tests/unit/server/test_health_profile_api_contract.py`。

这些用例使用合成文件及 MongoDB/DocMind/模型替身。Report 已由生产组合根接入共享文件服务，对应离线测试位于 `tests/unit/report/`；共享层和 HTTP 替身测试不等同于真实上传、画像摄取与回调的联合验收。运行全量测试时，若指定 `--basetemp`，应使用项目目录之外的临时路径，因为 Report 预览工具会拒绝在源码目录生成输出。

- 测试不得读取 `.env`。需要了解配置项时，只阅读 `.env.example`、对应环境的 `.env.example.*`、`config/env.py` 和 `README.md`。
- 单元测试在导入会初始化外部连接的模块前，应注入 fake、stub 或 mock。
- 禁止在测试代码、页面、日志、fixture 或提交记录中写入 API Key、数据库 URI、密码和真实健康数据。
- 需要真实外部依赖的验证必须放在 `tests/api/`，并在文件开头说明前置条件和副作用。
- 自动生成的测试身份使用 `test-` 前缀和随机会话 ID。浏览器页面允许手工输入任意开发测试用户 ID，不以名称前缀限制接口调用；应通过后端地址选择正确的开发测试环境，不得写入或删除生产用户数据。
- 与操作系统强绑定的测试设计必须先评审，并在 Linux 环境完成最终验证。

## 5. 真实 API 页面

可视化参数表单分别展示字段名、含义、必填或可选以及输入控件。可选参数使用后端默认时，该字段从实际请求省略。「重置参数」恢复当前接口的表单草稿，保留公共用户与会话及手工 JSON 草稿。画像展示的循证和筛选参数在共享表单中编辑，主体选择可切回默认本人。

各页面通过「本页目录」直达连接与身份、请求编辑、业务展示和调试记录。网络记录默认收起，展开后可清空或导出。已有可视化预览的页面可按需展开原始响应 JSON。画像结果按综述、检查趋势、健康事实和循证分区，指标与事实按类别展开。整卷问卷按页折叠，页内选项自动换行。

健康分析新增 `/health-analysis.html`，公开接口为 `POST /api/v1/health-analysis`。运行页面服务后，可另开本机离线演示后端 `python -m tests.api.scripts.health_analysis_demo`，将页面后端设置为 `http://127.0.0.1:9081`，点击合成示例后再手工发送；示例不自动提交。演示运行真实 Handler、应用服务、生成器和文件追溯，只有授权、资料及模型使用明确合成依赖，不读取私有 `.env` 或连接实际在线服务。`python -m tests.api.scripts.health_analysis_demo --smoke` 执行10个本机HTTP场景并校验8个成功结果的追溯文件，文件默认位于仓库外临时目录。页面支持本地选项填写和显式追加历史，再次发送时执行去重；这些操作不调用旧问卷提交或因素轮询。正式提供方、模型效果、数据库多实例及实际代理等待链仍须真实验收。

推荐通过页面服务访问，避免 `file://` 的唯一源、安全策略和跨域差异：

```bash
conda activate shulan
python -m tests.api.serve_pages
```

然后打开 `http://127.0.0.1:9080/`。如页面需要供局域网其他设备访问，可显式指定监听地址：

```bash
python -m tests.api.serve_pages --host 0.0.0.0 --port 9080
```

当前 7 个页面入口覆盖以下领域的 27 个接口。`web/routes.py` 共注册 28 条本地路由；新增肠道屏障接口尚无专用联调页面，其操作与验收入口见 [肠道屏障 Wiki](../wiki/gut-barrier.md)。

| 页面 | 真实接口范围 | 主要副作用 |
|---|---|---|
| `health.html` | `GET /health`、`/health/live`、`/health/ready` | 无业务写入 |
| `chat.html` | 推荐问题、完整/SSE 对话、custom 卡片、历史 | 调用 LLM，写入会话数据；卡片不调用 LLM |
| `record.html` | 创建、完整/SSE 回答、动态答疑与自动完成、状态、历史（兼容接口可独立调试） | 调用 LLM，写入 Redis/MongoDB |
| `interpret.html` | 体检报告 AI 解读，支持 fileId 或 documentId | 下载或复用文件、调用 LLM、保存报告 |
| `report.html` | 最终报告生成、报告批次列表、指定产物详情 | 生成时启动后台任务、调用外部服务并回调；查询接口读取已存产物 |
| `health-profile.html` | 画像读取、主体管理、文档观测列表、事实查询/处理/删除、变更审计 | 写入测试画像、主体及事实变更事件；循证选项可能调用知识库 |
| `health-profile-view.html` | 按产品原型展示指定用户的当前事实、指标趋势和健康画像 | 读取不存在的用户时会初始化空画像；循证选项可能调用知识库 |

所有页面及首页提供公共后端地址、`userId`、`sessionId`。同一浏览器源（协议、主机、端口相同）下的页面通过 `localStorage` 保存并同步这些设置，切换、刷新或重新打开页面均沿用身份。原有后端地址缓存会自动迁移。只保存这三个公共设置，消息、文件、主体、事实和 JSON 草稿不持久化。

- “生成新会话”只更新 `sessionId`；“生成新用户”同时更新两者。重置参数表单不更换身份；参数表单中的身份来自上方公共设置，可选身份字段可勾选不发送。
- 各业务页顶部提供“请求参数与 JSON”：左侧为可视化参数表单，右侧实时展示请求地址和可直接编辑的 JSON，发送按钮紧邻 JSON。长表单在左侧独立滚动；窄屏时上下排列。JSON 支持四空格格式化和复制。
- 选择接口后可编辑全部公开参数。普通参数使用表单，对象和数组使用 JSON 输入框；“载入示例”仅填写该字段，不发送请求。可选参数留空不发送，可选布尔值区分“不发送 / true / false”。显式填写的 `0`、`false`、`[]`、`{}` 和 JSON `null` 会保留。
- 在实时 JSON 中直接键入会自动切到“完整 JSON”，暂停左侧表单操作，并按手工草稿发送。此模式仅检查 JSON 对象语法，可删除必填字段或填写特殊类型测试后端校验；临时覆盖的身份不会写回公共设置。JSON 或表单格式错误会在右侧提示并禁用发送，修正后恢复。
- 切回“参数表单”后继续实时生成 JSON，同时保留先前手工草稿；再次选择“完整 JSON”会恢复草稿。切换接口、表单回填或公共身份变化不会覆盖手工草稿；需要以表单替换它时点击“从表单重新生成 JSON”。
- 表单快捷输入、建档选项及结果选择会回填参数。完整响应和 SSE 共用对应参数草稿；独立发送接口不要求先运行页面流程。完整 JSON 模式始终以 JSON 为准。
- 右侧“实际请求”的地址、JSON 与实际发送参数一致，GET 使用查询字符串；原始日志保留请求、HTTP 错误和 SSE 事件。公共设置变化后，旧响应只保留日志，不回填新身份的界面。
- 画像展示页使用与其他页面一致的导航、公共设置、接口参数和结果面板，保留健康总览展示。`?userId=...&subjectId=...` 入口继续有效：显式 `userId` 更新公共用户，`subjectId` 仅用于当前页面；“接口参数”中可修改全部读取选项。
- 接口结果支持按对象或数组折叠、展开全部、折叠全部和复制完整 JSON；键名、字符串、数字、布尔值和 `null` 分色显示。JSON 展示、复制、请求草稿和原始日志均采用四空格缩进，折叠不修改实际数据。复制在非安全上下文（如局域网普通 HTTP 地址）自动降级为 `execCommand("copy")`，仅当降级也失败时才提示手动复制。

Record页面先显示 `assistantMessage`，再显示当前问题。新会话正常10–15题，尽量充分至15题；前三题依次覆盖主诉、疾病史、血亲家族史，已知主题用自然关联连接补充题，后段从五大类24个受控细项选择不同生活习惯。只有适用且有价值的新题耗尽才提前收束，不反复确认凑数；历史原策略最多10题，显式短策略保留原4–6题预算，已发布题原样保留。提问阶段及无正式答复的轮次通常用一至两句简洁交流，允许面向用户表达帮助意图并自然延续前文，不设最低字数，不介绍采集流程或泛主题重要性；个人史主要承接主诉，家族史不绑定具体指标，生活题依照真实关联展开，明确引用且关联相当时交替关注，不要求每轮重述主诉；有正式答复的前置简答及QA直接进入解释，不另加病史复述或客套引入，不复述用户选项、行为、诉求或思考流程；无报告无疾病线索时自然关心日常生活，不预设患病。选项只含明确答案，保留文字或语音自由输入；radio单选、checkbox多选，普通与SSE均回填共享answer，非空other继续优先。正常补充邀请全文为“您还有其他想了解的问题吗？可以通过文字或语音补充描述”，唯一按钮为“没有了”，不叠加按钮说明或第二段自由回答提示。前置简答免费，随后实际问题当轮直接一般解释、生活场景和建议，逐句避免强调资料不足，不要求补资料；只答本轮问题及真正尚未答的问题，主动重问时简短补充、不照搬旧文案；第5轮简答后结束，连续含糊两次也收尾。点击“没有了”后等待finished并显示最终答复。历史及旧文本题不重生成，只在AI展示副本中移除内部处理提示；计数和阶段可在JSON检查；progress只是阶段估计。

首次询问个人疾病史、血亲家族史时展示具体疾病多选，不只显示“有”“没有”：个人史可选高血压、糖尿病、血脂异常、冠心病、脑卒中、慢性肾病、脂肪肝，家族史可选高血压、糖尿病、冠心病、脑卒中、肺癌、结直肠癌、地中海贫血、血友病，均保留“没有”“不清楚”，不提供“其他疾病”。“没有”“不清楚”应单独选择，疾病可多选，未列出的疾病和具体血亲可通过文字或语音补充；家族史选项不代表本人确诊或全部疾病均为遗传病。已知主题仍仅承接后询问补充、更正，固定使用 `radio` 和“没有需要补充的内容”“目前记不清”两个明确状态，具体补充、更正直接自由输入。联调应新建会话，同时检查正常及兜底发布的选项，以及仅勾选疾病时的主题和亲属归属，不额外增加追问题位；已有会话已发布或缓存的问题按原题恢复，不就地修改选项或问题 ID。

快捷选项只含可直接提交的明确答案，开场、病史、生活及兜底不提供“其他／其它”或混合占位；“阅读”“听音乐”等行为分别列出。题干、承接、理由和每项答复不能提示在按钮中填写或点击后输入、展开输入框。普通题统一“也可以通过文字或语音补充描述。”，固定邀请使用上文精简全文且不重复hint。回归覆盖规范化后仍非法的其他占位、正常与局部修复失败、完整兜底、旧答复版本限制，以及最终选项变化后重新检查组件；正常医学正文、用户原话和疑问引用不误改。普通/SSE、恢复与重复提交保持原题、选项、ID和计数，旧缓存存储不被清理，AI展示副本统一移除内部处理提示。

全量具体选项验收覆盖三个固定主题、五大类24个生活细项及状态题：检查按钮简短、独立、明确，无“某个/某种”、泛健康方向、括号解释或多个答案拼接；数量范围保留单位，不换算杯瓶容量。主诉checkbox最多12项，未知和暂无需求与具体项互斥；other可表达候选库外的真实内容。分别输入“身体不舒服”“有病史但忘了病名”“不清楚有没有病史”“没有糖尿病”“想了解糖尿病”，确认不追加追问、否认不扩大、目标不变诊断。完成后检查 `condition.history_status` 和 `family_history.reported` 能按原意保存、由Chat读取；未知亲属不生成虚构主体或本人疾病，不明确观察即使确认后也不进入肯定性临床依据。离线回归不证明真实模型措辞质量，另审全部题目、自由回答与画像完整性。

生活习惯按五大类24个受控细项验收，不要求覆盖所有方向。真实相关性优先，同档先避开上一题大类、再优先累计题数少的大类、最后模型原顺序；五类均出现后仍需交错，强相关同类可胜过一般背景异类。一次自由回答覆盖运动频次和时长时两项都跳过；饮品种类不等于饮水量已知。不饮酒后不追问饮酒量，拒绝或未知只封实际范围，明确拒绝整类时尊重范围。无报告不虚构异常或病人身份。正常、部分和整体兜底共用规则，临时预览不计已问。新细项归既有生活画像类型，保留最小原话依据和user_reported/pending状态；AI分析、建议、关联或精神疾病推断不能写成用户事实。

Record 选择“创建建档”时，左侧表单顶部显示“本次建档附件”，可输入公网文件 URL（PDF 或图片），也可引用当前用户本人主体的 `documentId`，最多 5 份。输入地址、修改文件类型会立即同步到 `fileList` 表单及右侧 JSON，直接点击“创建建档”或“发送当前接口”即可携带附件，无需再点击添加；“添加下一份”保留当前附件并清空输入框，供继续填写其他文件。列表中可编辑或移除已有附件，清空当前地址也会移除对应附件。未完整填写的地址保留在 JSON 中并显示提示，便于检查实际参数。URL 无法识别格式时需选择文件类型；本地文件须先上传到文件服务取得公网直链，当前后端没有接收本地文件二进制的上传路由。完整 JSON 模式下附件区展示 JSON 中的文件并停止表单操作，手工字段、空值和身份保持原样；可切回参数表单继续添加。公共身份切换会清空表单附件。

Record SSE 的 `reasoning` 按首次分析产生的顺序实时原样返回零个或多个片段；分析 JSON 修复仍保留首次推理，不新增推理或在末尾重复发送。普通和 SSE 成功结果中的完整 reasoning 均原样保留，不因字符质量或正式内容纠错被改写、清空；它不是正式答复或事实依据。`assistantMessage` 始终独立展示，正文经完整校验并保存成功后一次返回。Record SSE 结果区保留最后一份带 `status=question/finished` 的业务结果及事件数，尾随的空 `finish` 不会覆盖问题详情；原始网络记录保留全部 SSE 事件。失败可能先收到推理再收到 error；现有页面保留临时片段和当前问题，手动恢复或后续成功结果会刷新展示，不能把推理片段视为提交成功。

对话承接验收使用合成资料分别覆盖：报告含多个关注点、只有具体主诉、模糊主诉、无疾病健康目标、自由文本提前提供多个主题。通常一至两句，体现理解用户并主动帮助分析困扰、了解改善方向的意图；连续问题可依靠上下文自然延续，不要求每轮重提主诉或帮助目的。逐轮检查与题目连读自然，个人史承接主诉、家族史不限定指标，生活题仅在真实关联下交替关注；不介绍采集流程、泛主题重要性，不承诺疗效或确定病因。内部rationale可空且不拼接公开正文，明确引用具体关注时focusRef及来源必须有效；未明确引用、依靠上下文延续时允许focusRef=null，不强迫重述主诉。关注计数只来自成功发布问题，刷新、修复、重放不增加次数；无效引用及无来源引入修复失败时，仍保留有效问题和答复。普通/SSE、缓存恢复及旧会话均回归，表达元数据不进入公开字段或画像。

本轮真实模型至少分别执行single_goal_dialogue、report_focus_rotation、wellness_goals三个隔离场景，逐段人工审阅完整对话并保存帮助意图、关联准确性、重复程度和自然度结论。连续三轮或更多沿用同一表达骨架、只替换名词时，表达验收判为未通过；不能以替换同义词或重复“为了更好地帮您”代替自然交流。验收脚本的reviewRubric和对话文件均提示此人工标准，不新增模型评审调用或生产关键词检查。离线测试及脚本退出码0只证明结构、状态和自动检查通过，不能替代真实措辞验收。

同时检查自然延续是否退化成上题回执：“我知道了”“心里有数了”“这部分说清楚了”及反复安慰记不清都不合格；泛泛说明“某主题很重要”“主诉与某习惯有关”也不能代替主动交流。一至两句不表示必须写两句，未达到连续三轮同骨架阈值不代表其他表达要求自动通过。保留各轮真实结果及修复统计，区分模型表达与中性兜底，失败样本不得被后续运行覆盖。

`record-response-v19`进一步检查直接交流：不用“我们一起看看”“和您一起了解”等空泛陪伴话术，减少无实际递进含义的“也”，不以“一并”“共同”或每句“我想”替换成另一套模板。“把两种情况放在一起考虑”等必要合并语义及正常语义中的“也”仍合法，不增设关键词黑名单或全局文字清理。普通题固定提示“也可以通过文字或语音补充描述。”保持原样；QA正文、用户原话和已发布历史不进行词语清理。三个合成场景须分别记录空泛陪伴、赘余连接、重复句式、复述评价及未经提供的行为描述；措辞改善不自动关闭I-029，也不以没有命中某个词作为表达通过的证据。

对外AI正文还需检查内部处理提示：正常生成、修复、局部保留、完整兜底、状态、历史、幂等重放和Chat建档回放都不展示“尚未完成可靠分析”“原文已保留”“待回应”等处理说明。旧数据只清理返回副本，不回写历史，不改用户原话、reasoning、问题ID、选项、答案指纹或计数；错误响应、SSE error及独立documentProcessing附件状态不参与AI正文清理。隔离验收工具保存公开状态/历史/重放，并检查提示是否泄漏及读取是否改变存储；原始模型和内部快照保留用于诊断。真实主动降级只在合成内存gateway注入指定分析失败，分别记录真实调用与注入次数；人工检查自然引入、控制继续/结束及有效答复，不把无提示误判为每轮分析完整成功。

最终报告页的 `fileList` 表单支持直接粘贴 HTTPS 文件 URL，多份文件每行一个，最多 10 份。右侧 JSON 自动生成 `[{"url":"…","fileType":"pdf"}]` 或图片类型 `image`，发送时使用同一份参数。默认根据 URL 路径或 `objectName/fileName/filename/name` 查询参数识别 PDF、JPG、JPEG、PNG、BMP、GIF；无可识别扩展名时，在“URL 文件类型”中选择 PDF 或图片。该选项可统一指定所有快捷 URL 的类型；混合类型可自动识别或逐项填写 JSON 数组。原有 JSON 数组、`documentId`、显式空值及完整 JSON 手工模式保持原样。最终报告后端要求 HTTPS 且 URL 不含凭据和片段，快捷输入按此规则校验。

`/qa/continue`、`/qa/complete` 仍可在“选择接口”中独立发送：前者已弃用，有效请求返回 409；后者仅幂等读取已完成结果，不能结束活动会话。旧版未完成会话返回 409 时，使用公共设置中的“生成新会话”后重新创建，用户 ID 保持不变。

页面参数描述位于 `tests/api/pages/api-contracts.js`，状态与序列化逻辑位于 `lab-core.js`，编辑器和 HTTP/SSE 请求位于 `common.js`，可折叠 JSON 展示位于 `json-viewer.js`。修改后使用 Node.js 运行无网络回归（无额外 npm 依赖；pytest 不自动收集此 JavaScript 测试）：

```bash
node --test tests/unit/api/api_lab.test.cjs
```

该测试检查路由覆盖、页面资源、公共身份、参数序列化和受控 HTTP/SSE 响应，不启动业务后端、不读取 `.env`。页面服务会在刷新时重新校验静态文件内容；编辑 HTML、CSS 或 JavaScript 后直接刷新即可。

Record 页面收到创建接口的 202 后保留 processing 状态，不自动轮询或刷新；failed 状态也不自动查询。需要更新进度时，点击“恢复当前问题”或手动发送状态接口，沿用当前 userId/sessionId。普通/SSE 命令行脚本仍先等待创建完成，再开始问答。网络中断时保留当前 sessionId，恢复方法见 [创建故障恢复指南](../docs/architecture/qa-creation-recovery.md)。

Record命令行联调先校验新首题为checkbox且有2–12个非空、不重复选项，再完成正常10–15题，在QA入口默认答“没有了”。`--intake-policy auto`为默认，兼容新旧题数；`new`验收15题及25次实质交互保护，允许实际题库耗尽后提前收束；`legacy`检查历史策略最多10题和20次保护（兼容显式6题短策略），不凭当前题数推断策略。请求防御独立为64次，以容纳不计QA额度的控制邀请。用 `--supplement` 多次添加实际疑问，脚本只通过答案提交推进。真实模型还需分别审阅有报告、无报告无疾病、自由文本跨细项场景的承接自然度、重复程度、选题价值及QA实用性；不能以JSON合法或轮数通过代替语义验收。以下命令调用真实模型并写测试会话，只由开发者在测试后端主动执行：

```bash
conda activate shulan
# 完整响应：初始建档 + 一批补充疑问
python -m tests.api.scripts.record --base-url http://127.0.0.1:9002 --intake-policy new --supplement "饮食和运动需要注意什么？"
# SSE：初始建档 + 两批补充疑问
python -m tests.api.scripts.record_stream --base-url http://127.0.0.1:9002 --intake-policy new --supplement "排便问题需要关注什么？" --supplement "还有睡眠问题"
```

脚本不读取 `.env`，后端地址通过 `--base-url` 指定；`--help` 不触发外部请求。

### 5.1 用户画像摄取与跨会话记忆验收

先在任意页面填写公共 `userId` 或生成新用户；报告、建档、Chat 和画像页面自动沿用同一组 `userId` / `sessionId`，无需手工复制。`subjectId`、`reportId` 等业务引用按本次测试输入。需要验证跨会话记忆时，明确点击“生成新会话”。推荐按以下顺序验收：

1. 在 `health-profile.html` 读取共享用户，创建本人主体并记录返回的 `subjectId`。
2. 在 `record.html` 使用共享 `userId` 完成建档，确保响应已经返回 `status=finished`，并展示最终答复。
3. 画像摄取为后台异步任务。重复读取画像事实，直到性别、观察时年龄、身高或体重等已明确填写的基础事实出现；不得用固定等待时间判断成功或失败。
4. 在 `chat.html` 使用同一 `userId` 发送包含明确本人事实和明确亲属关系的消息，等待 AI 完整回复保存，再重复读取画像事实；Chat 自述默认显示为 `pending`，父亲等血亲事实应属于独立主体。
5. 点击 Chat 页的“生成新会话”。该按钮只生成新的 `sessionId`、保留当前 `userId`；在新会话提出相关问题，验证已摄取画像能够用于本轮回答。相同 `sessionId` 验证的是会话上下文，不属于跨会话长期记忆验收。
6. 如需验证报告，在 `interpret.html` 沿用公共用户，填写第 1 步的本人 `subjectId` 和报告 `fileId`，或使用该主体已有的 `documentId`；等待摄取完成后，在画像事实和展示页确认受支持检验指标以 `active` 状态出现。

增量指标验收还需覆盖：仅一份报告即展示结果；血常规各项目不再受旧 13 项白名单截断；菌群测量和报告评分分别展示；定性/未标准化结果显示原文且无伪造趋势；原单位不同、方法不同或带比较符时不生成数值趋势。明确填写的身高/体重在指标区可见；建档/Chat 原文提取的检验值仍为 pending，只出现在事实区。展示页按返回的分组循环渲染，不硬编码 laboratory；原有结果/来源对话框保留完整返回数据。

运维环境获得授权后可执行 `python -m health_profile.maintenance_cli inspect --job-id profile_job_example`，只读查看任务完成计数和原因，不读取健康事实值或重跑任务；命令使用该环境现有连接配置，不能在无网络单元测试中直接执行。旧任务没有 `processingSummary` 时须结合来源检查，不能将 null 当作零产出。相关自动化回归位于 `tests/unit/health_profile/test_indicator_expansion.py`，使用合成报告且不读取 `.env`。

刚发送的 Chat 消息如果仍在进行画像 LLM 提取，随后立即发起的请求暂时读取不到该条事实，这是异步派生窗口的预期行为。判断闭环是否完成应以画像事实已经出现为准；旧的 `completed` 或 `dead_letter` 任务需要按照用户画像架构文档中的内部 redrive 流程显式恢复。

直接双击 HTML 仍可尝试调用允许跨域的后端，但不作为标准测试方式。页面服务会通过 `/proxy` 将请求流式转发给填写的后端地址，使静态资源、按钮、普通请求和 SSE 都处于正常 HTTP 页面环境。

新增路由时：

1. 在 `web/routes.py` 显式注册命名路由，通过 kwargs 注入最小应用服务依赖。
2. 在 `tests/unit/server/` 增加无网络 Handler 契约测试，并确保 `tests/unit/architecture/test_web_architecture.py` 的集中路由和依赖门禁继续通过。
3. 如果需要真实联调，在 `tests/api/pages/` 增加或扩展对应领域页面。
4. 在 `index.html` 增加入口，并复用 `common.css`、`common.js` 和 `ApiLab.request()`。
5. 页面中必须标注真实调用的副作用；高成本或异步任务应要求显式确认。
6. 命令行脚本不得以 `test_` 开头，避免被 pytest 误收集。

### 最终报告持久化与回调联调

最终报告请求先保存受理记录，再返回 `processing`。页面分别展示生成状态和回调投递状态；生成失败的结果也需要回调。未收到 2xx 的回调会长期保留，服务重启后继续补投，重试不再次生成报告。直接重复点击生成不用于重投已确认的结果。

生产与开发环境的配置切换、迁移预览、单批次查询和补投命令统一见 [最终报告回调运维指南](../wiki/report-callback-operations.md)。本节保留模拟接收端和联调步骤。

#### 启动模拟接收端

页面服务自带模拟接收端，无需另外启动回调进程：

```bash
conda run --no-capture-output -n shulan python -m tests.api.serve_pages --host 0.0.0.0 --port 9080
```

最终报告页提供当前页面服务的 `/callback` 地址和复制按钮。后端设置 `SHULAN_REPORT_CALLBACK_URL=http://127.0.0.1:9080/callback` 后重启，再提交新报告；如果后端在另一台机器或容器中，使用它能访问的页面服务地址，例如 `http://192.168.20.100:9080/callback`。`localhost` 指后端所在机器或容器。该地址是后端配置项，不是报告生成接口的请求参数；纯静态托管页面不提供接收端。

`GET /callback` 返回就绪信息。`POST /callback` 接受 JSON 对象，对五类报告的成功、部分成功、失败结果和重复回调均返回 HTTP 200；非 JSON 或非对象返回 400。接收端仅确认接收，不执行报告业务逻辑。页面服务默认不落盘、不记录完整正文，报告仍可通过详情接口查询。如需保存调试副本，可追加 `--callback-save-dir <目录>`；目录应位于代码仓库之外。本地副本保存失败会记录简短警告，仍确认收到回调。

也可使用独立接收端，默认端口为 10002：

```bash
conda run --no-capture-output -n shulan python -m tests.api.scripts.report_callback_server --host 0.0.0.0 --port 10002 --no-save
```

独立模式移除 `--no-save` 后，会将每次接收的 JSON 保存到系统临时目录下的 `shulan-received-reports/`；可用 `--save-dir <目录>` 指定位置。使用独立模式时，后端回调地址的端口也要改为 10002。

回调地址在受理时保存快照，修改配置不影响已受理批次。已有待投递任务可使用下面的 `migrate-pending` 命令整体迁移到当前配置地址，或使用 `retry --target-url` 单独修正一个批次，无需重新生成报告。若验证失败后恢复，可在首次投递前停止接收端，确认报告已保存且回调等待重试；恢复接收端后，等待到期投递，确认同一批次交付成功。真实业务接收端需按 `finalReportId + type` 去重，并识别 `status`、`error` 字段。

#### 调整开发重试间隔

开发时可在后端部署配置中缩短等待时间，重启生效。例如失败后依次等待 5、10、30 秒，此后固定 30 秒，不加随机错峰：

```dotenv
SHULAN_REPORT_CALLBACK_RETRY_DELAYS_SECONDS=5,10,30
SHULAN_REPORT_CALLBACK_POLL_INTERVAL_SECONDS=1
SHULAN_REPORT_CALLBACK_RETRY_JITTER_RATIO=0
```

默认重试间隔仍是 30、120、600、3600 秒，最后一项无限重复；默认扫描周期为 5 秒。`报告回调积压` 汇总日志默认每个回调 Worker 实例每 300 秒至多输出一次，通过 `callback_scope=report` / `callback_scope=gut_barrier` 区分独立队列；多个进程各自计时，不代表每 300 秒才重试。并发数、请求超时、每轮领取数量和日志间隔也可配置，详见 [回调调度配置](../docs/architecture/report-agent-integration-guide.md#14-持久化受理生成中断恢复与长期回调)。已有 `nextRetryAt` 不会因重启而改变；若希望指定批次立即按新配置尝试，可使用运维指南中的单批次 `retry` 命令。合法 `Retry-After` 仍能延后发送。

#### 将已有积压切换到模拟接收端

整体迁移回调地址时，先修改后端部署配置的 `SHULAN_REPORT_CALLBACK_URL`，重启使用该配置的服务实例，再在后端项目目录和同一运行环境执行：

```bash
python -m scripts.report_callbacks migrate-pending
```

无需提供用户、报告 ID 或目标 URL。命令读取当前配置地址，分页扫描当前数据库中已启用回调的批次，将旧地址下尚未确认的任务改投新地址并设为立即到期；已受理、尚未生成全部回调任务的批次也会更新，以保证后续类型使用新地址。原始正文和累计尝试次数保留，已成功投递及已经使用当前地址的任务跳过。未曾启用回调的历史报告不进入迁移范围。

也可先只读预览迁移范围：

```bash
python -m scripts.report_callbacks migrate-pending --dry-run
```

输出字段、审计信息、中断后重跑，以及 `status`、`retry --target-url` 等完整示例见 [整体迁移与单批次运维](../wiki/report-callback-operations.md)。命令返回 `scheduled` 只表示投递已安排；使用最终报告页的详情查询检查 `delivery`，并核对模拟接收端日志，确认收到回调。

扫描范围以命令开始时间为界，新受理批次应由重启后的服务使用新配置；不要继续让旧配置实例受理新任务。迁移不能撤回已发出的 HTTP 请求，旧确认写入受版本及领取令牌限制；接收方仍需幂等处理。

#### 离线验证入口

离线验证入口：`tests/unit/server/test_report_callback_receiver.py`、`tests/unit/report/test_durable_reports.py`、`test_durable_api.py`、`test_callback_migration.py`。覆盖模拟接收端、批量迁移与只读预览、可控时钟、原子集合模型和本机 HTTP 接收端的失败与恢复，不连接真实 MongoDB 或模型。生产 MongoDB 多实例、进程强退和接收方幂等需在部署环境另行验收。

## 6. 测试质量要求

上传文件专项测试包括 `tests/unit/services/analysis/test_document_provenance.py`、`test_document_reuse.py`、`test_document_download.py` 和 `tests/unit/server/test_document_reference_api.py`，覆盖完整 SDK 分块归档、内容去重、主动引用、不同服务实例共享执行、租约和阶段恢复、下载边界、画像补偿与建档快照。Mongo 协调测试使用内存原子集合模型，不能替代真实 MongoDB 多进程联调；三种内部入口标签也不能替代最终报告 HTTP 接线。Report 协作者须按 [接入指南](../docs/architecture/shared-document-ingestion-guide.md) 另行验证。

- 正常路径、输入边界、错误映射和兼容契约至少各有针对性覆盖。
- 异步测试必须等待自身创建的任务，不依赖固定 `sleep` 猜测完成时间。
- 每个测试自行准备并清理状态，不依赖执行顺序或其他用例残留。
- 对时间、UUID、网络响应等不稳定输入使用可控替身。
- 修复缺陷时先增加能稳定复现问题的测试，再修改业务实现。
- PR 中记录执行命令、通过数量，以及因环境限制未运行的测试。

建档创建恢复的离线用例位于 `tests/unit/agents/graphs/record/test_creation_recovery.py`；受控文件重提及 DocMind 有界调用位于 `tests/unit/services/analysis/test_submission_recovery.py`，运维工具默认为只读预览，不在单元测试中连接真实服务。

首问校验诊断与定向纠错用例位于 `tests/unit/agents/graphs/record/test_generation_validation.py`，覆盖首轮报告背景事实归属、内部依据忠实概括、脱敏诊断与修复上下文，以及创建重试复用已解析报告。`test_natural_responses.py`等用例继续覆盖来源、兼容分类、枚举纠错、长度与转义、错误映射和原状态保留；新回归另检查提问和无正式答复轮的承接不复述、不提问且非空，正式答复轮直接进入解释，历史AI说明只作去重参考，QA不生成补资料题。`test_qa_copy_guards.py`覆盖截图中的资料不足句与有效建议混合、背景罗列、明显长句重复及合理医学边界；`test_qa_current_concerns.py`覆盖旧已答疑问本轮依据、主动相似重问、多问逐项响应和停用旧文案原样复用。正常发布、修复组件保留、兜底、普通/SSE、刷新与重复提交需沿同一策略，已发布历史不回写。离线模型用于验证传递、选择、修复和保存契约；真实表达质量和修复成功率仍需部署后验收。
