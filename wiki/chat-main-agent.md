# Chat 主 Agent 与旧会话兼容

Chat 应用服务的 AI 路径使用本域 `ChatAgent`，内部复用平台 `WorkflowAgentPort`。主入口是固定流程，不增加路由模型调用，不启用自主工具循环，也不修改已有 Prompt、知识查询策略或 SSE 正文。普通历史、custom 消息、会话数据操作和匿名通用建议不调用主 Agent。

## 1. 完整任务边界

| 操作 | 主入口覆盖的流程 | 子任务 |
|---|---|---|
| 完整回复 | 报告上下文、业务会话 lease、QA/Questionnaire 交接、旧上下文恢复、回复、消息保存、画像任务投递、lease 释放与 custom 队列排空 | `chat.reply` |
| 流式回复 | 同一业务任务的既有流式实现，保持 start/reasoning/content/finish/error 映射、正文拼接和保存顺序 | `chat.reply` |
| 个性化推荐 | 查找已完成 QA、构造既有输入、生成与解析问题、无资料或失败时回退通用建议 | `chat.suggestions` |

应用服务通过可注入的 `AgentPort` 调用主 Agent；默认构造本域 `ChatAgent`。主 Agent 再调用本域私有 workflow runner，子图状态仍由本域管理。主入口不对外暴露 Graph State，也不自动重试整个业务流程。个性化推荐虽然通过 GET 提供，但本来就是生成操作；无用户的随机推荐与历史查询不会进入 Agent。

## 2. 固定版本与运行身份

当前执行清单固定为 `chat.main/chat-main-v1/1`、`chat.reply/chat-reply-v5/1` 与 `chat.suggestions/chat-suggestions-v1/1`。尚无清单的旧会话明确映射到这组兼容版本；即使部署另一个 legacy 别名也不改变选择。已经保存的未知清单会明确失败，不能自动改成当前版本或 latest。

成功保存的 Chat 会话增加 `chatAgentManifest` 与 `chatAgentExecution`。后者记录 mainThreadId、runId、requestId 与操作；这些字段不进入消息正文、画像候选、模型输入或 SSE。既有 `agentThreadId` 保留，回复子图仍用相同 thread ID，AION Context 仍以 `chat.reply:<agentThreadId>` 恢复。主 Agent 的隔离 thread 只表示本次主任务的身份，不替代子图 thread，也不宣称新增了主工作流 checkpoint。

QA/Questionnaire 交接字段、业务 sessionId、会话 lease 和缺失 AION Context 时从业务消息重建的逻辑保持原样。执行元数据在本次请求内显式传递，不能在并发用户之间共享可变运行状态。失败请求仅有运行日志，不保证已保存一条可恢复的主任务；该阶段没有添加新的持久 Chat 队列或请求重放语义。

## 3. 流式所有权与失败

主入口只转发既有公开响应包络，不把内部工具、检索或主任务事件混入 SSE。成功时正文与推理仍按原顺序拼接并保存。业务错误和系统错误保持既有 error 映射，取消不会被伪装为成功；关闭内部生成器时逐层关闭其下游并释放 lease。

客户端断开连接与内部取消不是同一操作：现有 Web Handler 会继续消费生成任务并完成保存，本次不改变这一策略。客户端仍通过历史获取最终结果，不能重放一次发送来恢复 SSE。系统停服或显式关闭内部任务时，关闭传播依赖平台 `WorkflowAgentPort`、`AgentHub` 的流所有权支持。

本轮保留既有流式保存失败时记录日志的行为；主 Agent 封装没有新增跨 Context 与业务会话的事务或 exactly-once 保证。恢复、幂等或交付协议的升级需要独立业务变更及 API 契约评审。

## 4. 组合根与可用性

模块公开 `ChatSessionManager.agent_port`、`CHAT_MAIN_SELECTION` 和 `install_chat_agent(runtime, manager)`。组合根在构造 Manager 后调用安装函数，检查两个精确子能力，再通过平台 `install_module('chat.main', ...)` 原子发布主入口。安装失败将该模块标为 unavailable，并禁止服务的全部 AI 入口及原主端口执行 workflow；失败发生在 Report 上下文等前置模型调用之前。普通历史、custom 消息和匿名通用建议仍可调用。模块可用时推荐问题保留原有通用建议降级，但模块安装失败不能降级成一次成功的 AI 任务。

本域提交提供安装接线点；组合根的实际调用由集成任务统一交付。安装函数不能代替 HTTP 错误映射或全局 readiness 策略。主入口默认是现有固定流程，预算和权限沿用各既有确定性调用边界，没有新增可由模型任意选择的工具。

## 5. RAG 与画像边界

回复子任务继续通过现有公共 `KnowledgeGateway` 使用按需 RAG，检索资料只影响本轮模型请求，不增加跨域私有表访问或新的联网搜索。Report 上下文、会话资料与长期画像仍各自分区。

画像读取暂保留本域 `HealthProfileMemory` 端口。后续迁入公共结构快照时，由 Chat 自有 Adapter/renderer 实现该端口，保留 Chat 的字段选择和文字预算；不复制 Profile 提供方实现，不建立第二份治理真值。已保存用户消息仍由既有 `ChatProfileSource` 异步投递，AI 回复与 custom 卡片不作为患者事实。

## 6. 离线验证

本域回归覆盖完整任务进入 main、主子 runId 关联、旧 thread/Context 键兼容、固定 v5 与新别名并存、未知清单拒绝、SSE 拼接与保存一致、模型异常、普通查询绕过、模块依赖失败、提前关闭与等待中取消。继续执行现有 Chat、QA 交接、API/SSE 与全量离线测试。离线通过不代表真实模型、MongoDB/Redis、浏览器断连或部署环境已验收。

相关说明：[Agent 平台](agent-tool-runtime.md)、[Chat 健康引导](chat-health-guidance.md)、[当前 API](../docs/api/API.md)、[目标架构](../docs/architecture/modular-multi-agent-architecture.md)。
