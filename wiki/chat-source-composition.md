# Chat 来源的独立装配支持

本文记录 `bootstrap/chat_sources.py` 的公开装配能力及默认容器接线。根装配依次验收 Document、Questionnaire、Record 后接入 Chat；代码默认启用不表示真实部署已完成迁移。本 helper 使用已经交付的 [Chat 来源消费者](chat-profile-source.md)，不改变原文、来源处理版本、HTTP、模型调用或旧来源行为。

## 资源先行

`build_chat_source_resources(sessions=...)` 使用传入 Session 集合的现有 database，以 `sessions.name + '_chat_profile_sources'` 派生一个 Chat 专属永久集合，再调用公开 `create_chat_source_resources`。业务 Session 只读使用 PRIMARY；任务、不可变材料、准入证明和扫描游标在专属集合中使用 PRIMARY 与 majority+j 写入。`create_application_chat_source_resources()` 仅从已有配置模块取得 `collection_sessions`，不另建客户端。

返回既有 `store`、`owner`、`materials`、`snapshot_reader`、`fence_reader`。组合根先将 snapshot reader 交给公开 Profile resolver，固定注册 `('chat', 'profile.chat-source.v2')`，并将 fence reader 注册为 `owner_fence_readers['chat']`，再构造 Profile 服务。两个 reader 不依赖发布 worker，无需可变代理或事后补绑。

本模块不执行数据库读写、建索引、TTL、主体创建、模型调用、授权或任务调度。集合资源由组合根持有并关闭；持久历史与准入证明不能使用 TTL 清除。

## 后置发布与唯一后台入口

`build_chat_source_runtime(resources=..., inbox=..., presence=..., fresh_admission=..., legacy=..., policy=..., clock=...)` 接收已构造的公共 Profile 服务与真实旧 Chat source，返回不可变 `ChatSourceRuntime`：

- `router` 注入 Chat 应用服务，保留同步准备与保存后本域登记接口。
- `worker` 严格等于 `router.publication`，组合根仅周期调用该对象的 `recover_pending`。

不能把 `router.recover_pending` 再注册为周期任务，因为它还会扫描旧来源，而既有 Profile 摄取 Worker 已负责旧链路。这里只返回后台入口，不启动循环；进程内防重入、优雅关闭和统一调度由组合根负责。

业务同次保存用户原文、原始时间和固定 intent。保存后的 `ensure_queued` 只登记 Chat 本域任务；本域登记或 Profile 故障不取消已保存消息。独立补偿扫描和任务重试在后台完成 scope、真实 absence、owner fence、fresh reserve/authorize 与事件投递。ACK 丢失后重放同一事件、材料和授权，不能仅凭 eventId 把异内容视为相同受理。

缺失或显式 legacy 的既有模式继续旧实现；首次超出公开来源容量的合法旧输入固定 legacy，不截断原身份。新或未知模式在 Profile marker 出现之前也被旧 sender 拒绝；未知模式不会降级为旧队列。新旧完整边界仍以 Chat 来源契约为准。

## 离线验证范围

[组合测试](../tests/unit/bootstrap/test_chat_sources.py) 使用实际 Chat 公共工厂、BSON 存储替身、真实 Profile Inbox、fresh admission、resolver 与治理 worker，模型只使用离线替身。验证配置前缀、构造零 I/O、reader/fence/worker 的同一存储身份、原始消息提交、Profile 故障和本域登记丢失的后台恢复、失 ACK 后固定重放、长身份 legacy 队列、新未知模式隔离，以及返回 worker 不重复扫描旧链。

[默认装配回归](../tests/unit/bootstrap/test_chat_source_default.py)另核对根接线的 reader 顺序、真实 router/store、唯一新 worker、构造零 I/O 与显式旧注入。离线结果不代表真实 MongoDB 并发、模型或部署验收。

## 默认启用条件

默认 Document 图、默认 Chat source 和默认 Chat Session repository 同时存在时，由组合根注册新材料 reader/fence 并注入新的 router。显式 source 或自定义 repository 的调用方保持原有装配，避免使用默认数据库去证明另一个存储中的消息。代码不新增 HTTP 参数或单接口配置开关。

独立周期命名为 `chat-source`，只调用 `runtime.worker.recover_pending`，通过已有 runner 防重叠并参与关闭排空。Chat 历史查询不会启动恢复或模型；保存失败不会发成功 finish 或登记来源任务，保存成功后的画像故障不推翻消息保存结果。
## 启动扫描的异步驱动契约

Chat 来源扫描使用锁定的 PyMongo 4.16.0 异步集合：先 `await collection.aggregate(...)` 取得游标，再 `await cursor.to_list(limit)` 读取这一页。`find()` 则直接返回异步游标，不在 `find()` 本身加 `await`。两者不能使用同一套同步集合替身假设；[来源扫描回归](../tests/unit/services/chat/test_profile_source_publication.py) 使用实际 SDK 的 autospec 校验异步调用，并让共享 BSON 替身的 `aggregate` 保持异步。

若日志出现 `worker:chat-source` 的 `coroutine has no attribute to_list`，表示扫描未取得游标；本轮 `recover_pending` 会在任务领取前退出，即使已有待投递任务也无法继续。该缺陷不撤销已保存的业务消息，也不等于 Web 监听失败。修复版本重启后由原后台周期继续扫描和领取；保留持久来源、扫描游标及固定预算，不通过删除任务或改回 legacy 处理。已经超过原期限或进入失败终态的任务，仍遵循[显式重投规则](chat-profile-source.md)，不会因本次修复自动重置预算。真实 MongoDB 和部署恢复仍需独立验收。
