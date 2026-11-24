# Record 来源公共装配

本文描述 `bootstrap/record_sources.py` 的两阶段装配辅助接口。它已将真实 Record 来源存储、完成证据和 Profile 公共端口组合起来，并由默认组合根注入成对业务 Adapter 和单一新来源后台周期；真实部署仍待环境验收。这里的 Record 来源是建档原始问答与明确用户输入；文档附件仍由独立的 [Document 正式来源编排](document-formal-source.md) 管理。

## 资源先于 Profile

`build_record_source_resources` 接收已配置的完成会话 collection、实际 session cache 和运行时 session lease manager。`create_application_record_source_resources` 只读取现有 `collection_sessions` 句柄；两者均不建立新客户端。

在同一数据库中，从完成会话集合名派生两个永久集合：`<sessions>_record_profile_sources` 保存固定材料及投递意图，`<sessions>_record_source_admissions` 保存来源所有者的不可变准入证明。公开提供方使用 PRIMARY 读取，写入采用 majority、journal 和有界确认超时。构造不查询数据库、不建索引、不设 TTL、不执行模型或来源命令。

资源返回同一个 `store`、纯 `material_reader` 与 `owner`。先将材料 reader 注册到 Profile 的 `record / profile.record-source.v2` resolver，并将 owner 注册为 `record` 的 fresh fence reader，然后建立 Profile Inbox、presence 与 fresh admission。owner 验证实际完成会话、完整原始材料、来源意图及共享会话租约，不能替换成只核 completion ID 的浅证据。

## 业务与后台成对注入

`build_record_source_runtime` 接收上述资源、原 legacy source、原 completion store，以及实际 Profile Inbox、presence、fresh admission。它显式将新受理版本设为 `record-profile-source.v1`，返回三个对象：

- `router` 用于业务版本选择和本域意图登记。
- `completion_store` 包装原完成存储，先保存本域固定来源意图，再提交原业务快照。
- `worker` 只处理新来源的有界恢复，不重复执行旧来源扫描。

组合根必须同时注入 router 和 completion store，再独立调度 worker；不能只替换其中一个。完成桥与 worker 使用 `owner.evidence`，纯完成确认不申请第二把会话锁，因此可在业务已持有会话租约时重放。材料 reader、owner 和 worker 始终共享同一 store 实例；没有可变代理，也没有另建的来源存储。

业务提交不调用 Profile。Profile 不可用或 fresh 能力缺失时，已完成建档保留，本域任务明确等待或失败；后台依固定原包恢复，不需要 GET 或重新完成建档。`received` 与 `applied` 分开记录，不把准入 ready 或投递确认当作事实应用完成。

历史受理清单继续选择实际 legacy 实现；未知修订明确拒绝。原 Profile 摄取 Worker 继续负责旧来源恢复，新返回 worker 不承担旧扫描。默认容器在默认 Document 图且未显式注入 Record source/completion store 时启用；任一显式旧注入保持调用方原语义，不混用新的来源 store 和旧完成存储。本辅助接口不引入新的 API 开关。

## 验证范围

`tests/unit/bootstrap/test_record_sources.py` 使用实际公共 Record/Profile 工厂、Inbox、fresh admission、resolver 与治理 Worker；存储采用离线 BSON 往返替身，模型输出采用固定替身。覆盖资源纯构造与配置命名、同 store 与完整 owner 证据、提交后异步应用、Profile 失联、回执丢失后的完整重装配、未知版本及实际 legacy 队列。它不代表真实 Mongo 多进程并发或部署验收。

来源契约、实际准入与恢复规则见 [Record 来源持久消费](record-profile-source.md)。

## 默认容器接线

组合根提前构造实际 Record service 将使用的同一 session cache，并用同一运行时 lease manager 创建来源 owner。材料 resolver 与 owner fence 在 Profile 构造前注册，之后才用实际 inbox、presence、fresh admission 构造投递 runtime。原完成存储由原公开工厂创建，再注入返回的 wrapper 与 router；不会把完成证明换成独立的缓存或浅 ID 查询。

独立 `record-source` worker 只扫描新模式任务，旧来源仍由既有 Profile worker 恢复。业务保存只提交本域固定原始材料和意图，不调用 Profile 或增加模型；普通 status/history 不触发恢复。根接线验证见[默认装配回归](../tests/unit/bootstrap/test_record_source_default.py)。
