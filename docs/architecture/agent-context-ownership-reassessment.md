# Agent 模型对话上下文所有权重评记录

状态：Deferred，当前不变更实现

记录日期：2026-08-11

复核日期：2026-09-03；源码基线：`6625647`。Deferred 决策仍有效，不启动上下文所有权迁移。当前入口为 `modules/chat/application/service.py`、`agents/graphs/chat/graph.py` 与 `agents/llm/aion_gateway.py`；最新整体状态见 [架构导航](README.md)。

适用范围：普通聊天、后续工具调用、Agent checkpoint、AION Context 和业务会话数据

## 1. 当前决策

Shulan 现阶段继续保持已经实现的上下文分工：

```text
LangGraph checkpoint
  -> 保存 Graph 执行状态、节点输出和恢复信息

AION Context + Shulan Context Repository
  -> 保存和恢复模型实际使用的多轮对话 Context

Shulan 业务 Session
  -> 保存用户可见消息和权威业务会话数据
```

本记录不要求修改现有 Agent Core、`chat.reply` Graph、AionLLMGateway 或持久化实现，也不阻塞已上线调用链的增量开发。长期健康记忆由独立的画像事实系统提供，经临时上下文注入；这不是把模型消息历史所有权迁给 LangGraph。

当前继续使用该设计的原因：

1. AION 已经提供模型 Client、Context 自动记录和 Context v1 导入导出。
2. AION 负责处理 DeepSeek、Qwen 等 OpenAI-compatible Provider 的差异。
3. 当前普通聊天 Graph 只需要提交本轮用户消息，能够较快验证真实业务链路。
4. 现有实现已经通过 Gateway 隔离 AION，未来仍可替换上下文策略而不直接修改业务 Handler。
5. 当前尚未出现工具循环、Graph 分支或 checkpoint 回放与 AION Context 冲突的真实业务案例。

## 2. 问题来源

LangGraph 本身不提供具体 LLM Provider 的网络 Client，但能够通过 Graph State 和 checkpointer 实现 thread 级短期记忆。

如果把 `messages` 定义为可累积的 Graph State，LangGraph 可以：

- 按 `thread_id` 保存多轮消息。
- 在下一次调用时恢复已有 State。
- 获取当前 State 和 checkpoint 历史。
- 更新已有 State。
- 从历史 checkpoint 回放或创建执行分支。

AION Context 同样能够：

- 自动记录成功的 user/assistant 轮次。
- 在下一轮模型调用前恢复历史。
- 导出和导入 Context v1。
- 保存 reasoning、usage、provider 和 model 等模型会话数据。

因此，AION Context 与“LangGraph State 中保存消息历史”存在能力重叠。当前设计选择让 AION 保存模型消息、让 LangGraph 保存工作流状态，而不是在两处保存同一份完整消息历史。

## 3. AION 仍然不可被 LangGraph 直接替代的职责

无论未来由谁管理消息历史，LangGraph 都不会替代以下能力：

- 创建并调用具体 LLM Provider 的 Client。
- 处理 OpenAI-compatible API 的厂商差异。
- 统一 thinking、reasoning、stream 和 usage。
- 保留第三方 Provider 的非标准字段。
- 提供 Shulan 与具体模型 SDK 之间的适配边界。

因此，未来可能重新评估的是“AION Context 是否继续作为 Shulan Agent 的模型历史所有者”，而不是“AION 是否继续作为 LLM 接入层”。

## 4. 当前设计的收益

当前的 AION Context 所有权方案具有以下收益：

1. Graph 每轮只传入当前用户消息，不需要立即建立完整消息类型体系。
2. AION 已有 Context 原子提交规则，失败或未完整消费的流式响应不会形成半轮历史。
3. Provider、model、thinking、reasoning 和 usage 可以按照 AION 的统一格式保存。
4. ContextPolicy 可以对不同 Profile 选择有状态或无状态调用。
5. AION 的厂商兼容逻辑不会进入 Graph 节点。

## 5. 当前设计的长期风险

### 5.1 多套持久化的一致性

一次完整聊天最终可能涉及：

```text
LangGraph checkpoint
AION Context 文档
业务 Session 文档
```

它们不属于同一个原子事务。任意一步失败都可能形成部分成功，需要通过 `requestId`、`runId`、Context revision 和业务幂等写入进行恢复或补偿。

### 5.2 checkpoint 回放与 Context 版本不一致

LangGraph 可能回到较旧 checkpoint，但 AION Context 已经推进到较新 revision：

```text
LangGraph checkpoint：第 3 轮
AION Context：第 6 轮
```

如果直接重放模型节点，模型可能读取第 6 轮 Context，而不是 checkpoint 当时对应的第 3 轮 Context。

### 5.3 Graph 分支

LangGraph 可以从历史 checkpoint 创建分支。若 AION Context 没有同步创建独立分支，两个 Graph 分支可能继续读写同一模型 Context。

### 5.4 工具调用状态分裂

未来的模型—工具—模型循环通常包含：

```text
assistant tool_call
tool result
assistant final response
```

这些消息既是模型上下文，也是 Graph 执行状态。如果只保存在 AION Context，Graph checkpoint 将不能完整表达模型做出工具决策时看到的消息序列。

### 5.5 双重版本体系

LangGraph 使用 `thread_id/checkpoint_id`，AION Context Repository 使用 `context_key/revision`。如果需要精确回放，就必须建立两套版本之间的对应关系。

### 5.6 可观测性和问题排查

排查一次异常调用时，需要同时查询 Graph checkpoint、AION Context 和业务 Session，才能还原完整运行过程。

## 6. 未来可选方案

### 6.1 方案 A：LangGraph 管理模型短期消息

```text
LangGraph State
  -> 保存模型使用的 messages
  -> checkpointer 持久化
  -> 节点把显式 messages 交给 AION

AION
  -> 以无状态方式调用 Provider
```

优点：

- checkpoint 与模型实际上下文一致。
- 更适合回放、分支、中断恢复和工具循环。
- 可以移除独立的 AION Context Repository 和 revision 协调。
- LangGraph 能完整观察模型决策所依赖的消息。

限制：

- AION 需要支持显式消息列表输入，并保持厂商兼容能力。
- 上下文裁剪、摘要和检索需要在 Graph 或专门组件中实现。
- 长消息 State 会增加 checkpoint 存储量。
- 需要设计业务 Session 与 Graph messages 的同步和重建规则。

这是未来引入复杂 Agent 能力时优先评估的方案。

### 6.2 方案 B：继续由 AION 管理模型消息

```text
LangGraph
  -> 只保存工作流状态和 Context 引用

AION Context
  -> 保存模型完整消息历史
```

优点：

- 延续当前实现，迁移成本最低。
- 直接利用 AION Context v1。
- 当前简单聊天每轮只提交本轮输入。

限制：

- 需要解决多存储一致性。
- 回放、分支和工具循环会逐渐复杂。
- Graph 无法直接观察模型完整消息历史。

这是当前采用的方案。

### 6.3 方案 C：业务 Session 管理模型历史

```text
业务 Session
  -> 作为唯一原始消息来源
  -> 每轮选择、裁剪、摘要或检索上下文
  -> 交给无状态 AION

LangGraph
  -> 保存工作流执行状态
```

优点：

- 用户可见数据、删除和审计边界最明确。
- 可以为健康数据实现严格的上下文筛选。
- 不需要独立 AION Context 持久化。

限制：

- 上下文管理开发量最大。
- 必须自行确保消息协议、Token 预算和工具消息完整性。
- 如果缺少统一组件，容易重新出现各业务手动拼接 Prompt 的问题。

该方案可以与方案 A 组合：业务 Session 是权威原始数据，LangGraph State 保存一次 Agent thread 实际使用的短期上下文。

## 7. 触发重新评审的条件

出现以下任一情况时，应重新打开本设计：

1. 正式实现模型工具调用循环。
2. 需要从历史 checkpoint 精确回放模型调用。
3. 需要从一个历史会话创建多个 Agent 分支。
4. 出现 checkpoint、AION Context 和业务 Session 之间的真实一致性故障。
5. Context revision 冲突需要复杂重试或补偿。
6. AION 提供稳定的显式消息列表输入接口。
7. 需要在 Graph 中实现 Token 裁剪、摘要、相关性筛选或上下文压缩。
8. checkpoint 和 Context 的重复存储成本达到不可接受水平。
9. 需要完整审计“模型在某个节点实际看到了哪些消息”。
10. 需要对单条消息进行更正、删除或健康隐私清除，并保证所有运行数据同步生效。

如果这些条件均未出现，不应仅因为理论上的简化而提前重构已经工作的上下文链路。

## 8. 重评时的验收指标

未来比较方案时至少评估：

| 维度 | 需要回答的问题 |
|---|---|
| 正确性 | checkpoint 恢复后模型看到的上下文是否与原运行一致？ |
| 一致性 | 部分写入失败能否检测、恢复或补偿？ |
| 工具能力 | tool call 和 tool result 是否完整进入可恢复状态？ |
| 会话隔离 | 不同用户、主体、会话和分支是否绝对隔离？ |
| 数据治理 | 消息更正和删除能否传播到所有派生数据？ |
| 成本 | checkpoint、Context 和业务消息是否存在不可接受的重复？ |
| 性能 | 长会话加载、序列化和模型调用延迟是否可接受？ |
| 可观测性 | 能否还原某次模型调用的输入、输出和版本？ |
| 兼容性 | DeepSeek、Qwen 等 Provider 的 reasoning/usage 是否完整保留？ |
| 迁移成本 | 已有 Context 和业务会话如何迁移或重建？ |

## 9. 当前实施约束

在本记录被正式重新评审前：

1. `chat.reply` 继续使用 `ContextPolicy.AION_FULL`。
2. AionLLMGateway 继续负责 AION Context 的加载、导入、导出和保存。
3. LangGraph State 不复制完整、持续增长的 AION Context。
4. 业务 Session 继续作为用户可见聊天历史的权威来源。
5. 新增的简单无状态任务优先使用 `ContextPolicy.STATELESS`，不要无条件开启模型历史。
6. 不为了假设中的未来场景提前修改 AION 公共接口。
7. 如果工具调用等真实需求触发本记录，应先完成设计评审，再修改代码。

## 10. 相关文档

- [AI Agent 目标架构](./agent-target-architecture.md)
- [普通对话 LangGraph POC 设计评审](./chat-langgraph-poc-review.md)
- [用户健康画像设计方案](./user-health-profile-design.md)
