# 健康分析基础信息消费者

统一集成分支 `feature/basic-info-integration` 通过 Profile 公开确认端口为六入参接入真实持久写入。接口、业务规则和验收范围分别见 [当前 API](../docs/api/API.md#生成健康分析)、[需求](../docs/product/health-analysis-requirements.md)和[实施计划](../docs/architecture/health-analysis-implementation-plan.md)。

`ConfirmedBasicInfo` 固定本次姓名、年龄和性别。适配器将 Profile 的类型化收据转换为 `profile-basic-info-command.v1`，保留命令、主体、生命周期、逐字段版本、内容指纹及原确认时间。受理回执丢失或写关注无法确认时按原操作查询，确认受理后应用暂不可用可交后台补偿。Profile 负责共享当前值、年龄依据、应用及重启恢复。

默认组合把同一确认端口注入 HTTP 和 `SourceRecoveryRunner`。后台每轮有界读取 `health-analysis:` 的待补偿命令，推进公开游标。重启从持久命令继续，逐字段应用由 Profile 的原主体、原生命周期及版本控制。临时错误在后续扫描恢复，归档或字段变化阻止旧命令回写。冻结的 `health-analysis-trace-v2` 保留当次回执，补偿只应用确认命令。

当次effectiveDemographics仅含必要年龄、性别、确认时点、最高可信度和请求指针，传入所有模型阶段；姓名仅用于确认和受控追溯，不加入医学查询。仅有基础信息仍走无临床资料基础分支。年龄排除只使用真实医学原文明确范围；程序要求引文明确支持同一单位、上下界和开闭，再计算年龄适用性。好发年龄、历史和亲属范围只保留为相关资料。当次姓名在模型的报告、聊天、答案及题干正文中按 Unicode 等价和大小写折叠识别，再替换原文对应区间。协议键、引用标识和受控枚举保留。原句和对应指针仅进入受控追溯，模型引用按相同投影核验。

本轮移除AccessAuthorizer、VerifiedAccessScope和调用上下文resolver，不增加身份系统。ChatHistoryMaterialsAdapter可单独注入patient_sender_reader.read(user_id, group_id)返回发送人IDs，用于患者资料归属；没有映射时保留senderMappingAvailable=false，未知发送人不变成患者陈述。这个端口不验证访问权限或群组关系。

本地使用合成资料验证成功受理、回执丢失、补偿、首次建档、年龄与姓名更正，以及健康分析请求和 Profile HTTP 查询读取同一持久基础信息。固定报告按确认的本人和生命周期读取。具备固定归属和已核实临床观察的部分结果可作为原报告资料。读取前或读取中发生固定本人生命周期冲突时返回 HTTP 409。真实 MongoDB、模型、知识与聊天服务和部署另行验收。
