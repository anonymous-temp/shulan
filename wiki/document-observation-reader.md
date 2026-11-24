# Document 补充观察纯读契约

状态：公共读取组件已实现；生产消费者接线等待独立 Profile 状态读取能力和组合验收。本次不改变 HTTP 路由、原报告存储或画像事实。

## 读取与归属

`DocumentObservationReader.read(scope=DocumentScope(...), limit=100)` 返回 `document.observation-read.v1`。Document 拥有原始观察资料，Profile 拥有摄取状态，各消费者自行决定选择和渲染方式。此处不把原始观察升级成权威健康事实，不提供 Prompt，不运行模型或 Agent。

`MongoDocumentObservationSource` 在已有集合按明确的 `userId/subjectId` 过滤并按 `_id` 升序读取首段，最多取 `limit + 1` 条，用额外一条标记 `truncated`。该首段契约不承诺全量扫描，也不引入游标；现有 Chat/Record 补充规则只消费前 100 条。读取和构造不执行 `ensure_indexes`、主体创建、状态推进或任务恢复。

结果保留旧 `list_observations` 的观察身份、Document 引用、canonical 来源、原 sourceRevision、sourcePointer、观察正文、日期状态、摄取状态和资格提示。冻结数据以保留原字段插入顺序的 JSON 保存，返回属性为副本；输入值在等待状态读取前复制，之后底层记录变化不会修改本次结果。snapshot_id 表示这次返回内容，不表示数据库事务快照或新的业务版本。

## 摄取状态边界

Document 只依赖 `DocumentObservationIngestionReader.read_status(scope, source_id, source_revision)`，通过注入能力取得状态，不直接查询 Profile 私有任务集合。相同来源和旧修订在一次读取中只查询一次状态。合法主体但缺少旧摄取 job 的 `pending` 语义由 Profile 公共 Reader 明确返回；主体错配、存储异常或不可用状态不能伪装成 `pending`。

Profile 独立提供旧来源摄取状态读取，适配器只能消费其公开结果。Document 的工厂 `create_document_observation_reader(collection=..., ingestion_reader=...)` 不自建客户端，使用原集合的 primary 读取。生产组合根负责注入该能力；本组件的契约测试使用状态 Port Fake。

## 消费者与失败

Chat、Record 继续分别拥有筛选、排序、最多 8 条补充以及总字符预算。读取不筛除已知日期的 completed 数据，也不取代主画像事实；消费者按原业务规则选择。读取失败由各域按可选补充资料策略处理，不破坏已经取得的主画像上下文。

跨 scope、无效来源修订、非递增或重复的观察身份、非法日期状态、超出首段预算及状态不可用都会明确失败。BSON Int64 的旧 hash 修订保持精确，bool、浮点和字符串不冒充修订。此组件仍对应旧来源摄取语义；新版来源正式切换必须另行验收撤回、修订水位及补充观察的生命周期过滤，不能让已撤回的来源重新成为上下文。

## 离线验收

测试对照旧 `MongoDocumentObservationStore` 的真实投影，验证完整字段、条目顺序和实际补充 Prompt 序列化文本逐字等价；新读取零 DDL、按 scope 查询；前 100 条与截断；源数据及返回属性修改隔离；状态按来源去重；范围/顺序/版本错误以及失败不伪装成功。没有访问真实 MongoDB、Profile 摄取、OCR 或模型。

相关说明：[公共材料读取](document-material-reader.md)、[Record 文档消费](record-document-consumer.md)、[Profile 公共契约](profile-public-contracts.md)。

## 公共契约装配

`bootstrap.document_contracts.build_document_observation_reader` 将 Document 的观察集合与 Profile 的公开旧摄取状态 reader 组合；适配器只消费 `profile-legacy-ingestion-status.v1`，不查询 Profile 私有 jobs。状态的主体、来源 ID、旧修订及契约版本必须与原请求一致；缺失、不可用或越界结果不会伪装为 `pending`。现有活动主体确实没有旧任务时，沿用公开契约返回的 `pending`。

装配复用已配置集合和客户端，构造时不读写数据库、不建索引、不启动模型。该工厂供 Record、Chat 各自的上下文适配器注入；正式生产安装随各域验收单独完成，观察读取不能代替 Profile 权威事实。
