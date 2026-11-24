# Profile 旧来源摄取状态纯读

本页记录 I-035 的兼容读取增量，供 Document 原始观察补充查询判断旧摄取任务是否完成。它只投影现有 Profile job 状态，不从报告记录的 `ingestion_status` 推断，也不触发任务恢复或正式来源切换。

## 公共契约

`modules.health_profile.ProfileLegacyIngestionStatusReader` 接受既有 Profile repository；生产组合使用 `create_application_profile_public_contracts().legacy_ingestion`，构造不执行读写。公开值对象从 `modules.health_profile.contracts` 导入：

```python
result = await legacy_ingestion.read_health_report(
    scope,
    canonical_report_id=canonical_report_id,
    legacy_source_revision=legacy_source_revision,
)
```

scope 必须是已解析的 `ProfileScope`。canonical_report_id 指规范健康报告来源，legacy_source_revision 必须是明确的正整数旧摄取修订，可以是旧链使用的哈希派生整数；不能替换为新材料 sourceRevision 或 publicationRevision。该方法按旧幂等键 `report:{canonical_report_id}:{legacy_source_revision}` 精确读取，不查最新版本。

`ProfileLegacyIngestionStatusV1` 固定 `contractVersion=profile-legacy-ingestion-status.v1`，只返回 scope、规范来源 ID、明确旧修订、readAt、status、ingestionStatus、jobPresent 与稳定 errorCode。不会返回任务 ID、租约、错误正文、内部模型版本、指纹或业务健康内容。

| status | ingestionStatus / jobPresent | 含义 |
|---|---|---|
| ready | 原 pending/running/retry_wait/completed/dead_letter；true | 存在且身份绑定匹配的旧任务 |
| ready | pending；false | 主体存在且有效，但该旧任务尚不存在，保留原观察查询语义 |
| missing | null；false | 主体不存在、无权访问、非有效，或任务与请求绑定不匹配 |
| unavailable | null；false | 仓储读取或存量任务解码失败，不能伪装成 pending |

方法先按 userId/subjectId 读取有效主体，再按 userId 和旧幂等键查任务。已有任务必须同时匹配 userId、subjectId、health_report 类型、规范来源 ID、旧修订和幂等键；历史 null-subject 任务不能推断成属于本次主体。非法参数在仓储读取前报错；返回 unavailable 不暴露底层异常文本。

## 模块装配与失败处理

Document 定义本域状态查询 Port，由组合根注入调用上述公共 reader 的 adapter。Document 不读取 Profile 私有 job 集合，也不复制其模型或治理逻辑。可选补充材料遇 missing/unavailable 时应按其业务策略跳过补充或明确失败，不能当成“任务尚未开始”，也不能影响已有权威事实的读取。每批相同规范来源和旧修订可以在本次请求内复用结果，不能把结果跨请求缓存成永久完成状态。

该能力不创建主体、补画像 view、建索引、排队、启动模型或推进摄取；Document 原始观察的前 100 条查询和最多 8 条选择仍由对应提供方和消费者分别维护。本接口没有替消费者执行任何选择或 Prompt 渲染。

## 离线验证

回归覆盖五种真实任务状态、精确旧哈希修订与另一修订并存、缺任务 pending 兼容、缺失/归档/跨用户主体、错来源/修订/主体与 null-subject 拒绝、存储异常脱敏、非法参数无 I/O，以及公共 factory 构造和读取无持久写入。未连接真实数据库或运行模型。
