# Questionnaire 主 Agent 与后台恢复

Questionnaire 的生成与答后因素分析通过平级 `QuestionnaireAgent` 执行，公开选择固定为 `questionnaire.main/questionnaire-main-v1/1`。主入口包装原有完整业务工作流，没有新增路由模型、Graph、Prompt 或额外模型调用。原有 planner、factor pipeline 和模型适配器仍是本域私有依赖，不宣称它们已经变成可自由调度的自主子 Agent。

## 执行范围

| 操作 | 主 Agent 覆盖范围 | 主入口以外的确定性工作 |
|---|---|---|
| generate | 原 `_generate` 的任务读取、业务租约、重试检查、资料准备、问卷生成、发布校验、归档、保存及失败处理 | 创建受理、普通读取、过期清理 |
| factor_assessment | 原 `_run` 的输入/版本检查、checkpoint、模型评估、心跳、超时、结果保存与失败结算 | 提交答案、持久任务投递、纯结果读取 |

主 Agent 使用平台 `WorkflowAgentPort`；生成任务和因素任务各有自己的 runId、requestId 和主入口 thread 标识。记录在内部 `questionnaireAgentExecution`，不进入公开问卷、答案回执或因素结果。原 userId/sessionId、questionnaireId、assessmentId、业务租约、任务次数、600 秒执行期限和因素 checkpoint 继续按原逻辑工作；主包装不重置这些预算，也不添加一次新的生成循环。

公开主入口同时核对调用上下文与已持久的问卷归属。因素任务的执行绑定、续租、checkpoint、失败与成功保存共用包含 userId/sessionId/questionnaireId/assessmentId、输入指纹、pipelineVersion 和实际清单的租约条件；即使提供真实租约 token，也不能替换这些身份或版本执行其他任务。

## 固定版本与旧任务

新问卷受理时保存 `questionnaireAgentManifest`；提交答案时把同一清单复制进因素任务意图，投递后固定在任务记录。新清单使用 `questionnaire-agent-manifest.v3`，包含主 Agent、生成器、报告规划器、因素流程、因素模型适配器版本，以及必填的 `generationPolicySelection`。生产 factory 从实际依赖对象读取这些版本，执行前核对保存清单；不同版本不能在恢复时隐式替换旧任务。提供方改变语义实现时必须维护对应 VERSION 并另行处理旧任务兼容。

既有无清单任务只映射到完整 `LEGACY_QUESTIONNAIRE_MANIFEST`；旧 v1 和缺少政策字段的已知 v2 仅映射到本域可执行的 `questionnaire-generation-policy-v1`，保留原持久清单，不重新选择当前部署的任意版本。新 v3 的缺失/null/未知政策，或旧 v2 显式提供的不支持政策，均在资料准备及模型前拒绝。旧生成任务在原租约内保存映射和执行标识后才进入模型；旧因素任务在既有有效任务租约内写入映射后才评估。旧任务的输入指纹和 assessmentId 不因新增内部清单而改变。

版本不可用时保留既有公开失败分类：生成进入不可重试的 QUESTIONNAIRE_GENERATION_FAILED，因素任务进入不可重试的 FACTOR_EVALUATION_INVALID；内部日志或保存清单可用于定位。普通读取仍能查看已有结果。中断后沿用原剩余次数和 checkpoint，不能把主入口元数据理解为新增的逐模型步骤恢复能力。

## 纯读与无人轮询恢复

`get` 不调度生成、不获取业务租约、不持久化过期状态、不清理归档；Mongo 普通读取也不创建索引。达到截止时间的 ready 快照可以即时投影为 expired，并隐藏问卷正文，但持久清理交给后台。查询不延长有效期；已经在期限内受理的 pendingSubmission 不会被错误投影成过期。

既有 runtime 周期任务继续驱动 `recover_pending`、`recover_factor_assessments` 和 `drain_factor_assessments`。即使没有 GET，已受理生成、到期清理、提交保存中断及因素任务仍可恢复。创建命令与提交命令保留原有显式写入职责；submitted 结果不会因清理或 Chat 接管而被重写。

## 组合根与模块失败

组合根构造服务后调用 `install_questionnaire_agent(runtime, service)`，以原子模块注册方式发布 `service.agent_port`。注册失败会禁用新生成入口和因素派发，旧 port 引用也不能绕过 runner 的就绪检查；查询、已生成问卷提交、已保存结果和独立投递仍按原入口工作。组合根的实际安装及 HTTP 错误映射由集成任务统一完成，本域不改 bootstrap 或 runtime。

`build_questionnaire_service` 可注入 `profile_source_guard(user_id=..., questionnaire_id=...)`。该只读公开能力判断来源是否已经由新 Profile 发布链管理；命中后，本域 store 按 userId、questionnaireId、sourceRevision 和 pending 状态 CAS 退休旧意图，写入 source_managed 与原因，不伪造 queued 或 jobId。固定批次因而能继续处理后续来源，因素任务投递不受影响。这个可选 guard 仍不默认注入，不用于自动迁移旧来源。新问卷的固定原始材料与正式发布已经由独立的 `profile_source_delivery` 接入[默认来源链](questionnaire-source-composition.md)，并在持久标记、旧扫描和 sender 边界隔离新旧路径；业务提交与画像摄取分开。

## 验证与限制

离线测试覆盖主入口整体执行、真实依赖清单、缺字段旧映射、未知版本拒绝、无 GET 的生成/过期/提交恢复、模块注册失败、查询无写、固定 pending 批次退休及 revision 竞态，并保留既有因素模型/搜索、checkpoint、超时、停止接单和结果查询回归。未执行真实模型、MongoDB/Redis、多进程切换或外部来源发布验收。

相关入口：[问卷业务流程](record-questionnaire.md)、[Agent 平台](agent-tool-runtime.md)、[当前 API](../docs/api/API.md#建档问卷接口)、[目标架构](../docs/architecture/modular-multi-agent-architecture.md)。

选题与问卷呈现规则的所有权、固定内容和隔离验收见 [Questionnaire 自有生成政策](questionnaire-owned-policy.md)。
