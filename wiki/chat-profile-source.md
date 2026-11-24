# Chat 来源持久消费

## 当前边界

本域提供用户消息原始材料、独立持久任务、后台投递与补偿扫描、真实 Profile fresh 准入和旧模式路由。入口为 [草稿](../modules/chat/profile_source.py)、[发布工作流](../modules/chat/profile_publication.py)、[资源与路由](../adapters/chat/profile_source.py)、[存储和消息证明](../adapters/chat/source_store.py)、[公共材料](../adapters/chat/source_material.py) 与 [准入适配](../adapters/chat/source_admission.py)。[默认容器](chat-source-composition.md)已先注册固定 reader/fence，再注入同一 router 和唯一新来源后台周期；显式旧 source 或自定义 Session repository 保持调用方原路径。HTTP 路由未改变，代码默认接线不代表历史来源迁移或真实部署已验收。

每个用户消息是一个独立来源，sourceId 为服务端生成的 messageId，当前仅支持 sourceRevision=1、publicationRevision=1 的首个 publish。assistant、custom 消息和模型回答不成为来源。旧消息没有新模式时固定使用 legacy；已有来源的迁移、后续 correct/retract 命令另行交付，不通过清空任务或改写消息实现。

## 原始输入与业务隔离

`prepare_user_message_source` 同步把原 content、原 timestamp、userId/sessionId/messageId、角色和材料修订固定到 chat.profile-source-draft.v1。原文空白与原始时间文本保持不变；strip 和 naive 时间的 +08 解释归公开 profile.chat-source.v2 的固定 Profile 处理版本。Chat 不生成权威事实，也不把回复、画像上下文或附件投影重复发布。

新消息和 chatProfileIngestion.deliveryMode=profile-source-event.v1 在现有同一次 Session 保存中提交，consumerRevision 固定为 chat-profile-source.v1。首次策略、createdAt、draftDigest 和 deliveryManifestDigest 随消息保存，不随后台重启替换。准备失败或业务 save 失败不产生可发布来源。

普通回复和 SSE 的既有顺序保持：生成回复、保存消息与模式，然后 ensure_queued。新 ensure 只读取已保存的唯一消息并登记本域任务，不调用 Profile、不执行 worker、不启动模型。登记存储失败时响应仍正常返回，消息上的固定标记由后台扫描补登记。Profile 完全不可用也不会阻塞已经保存的回复。

## 独立任务与真实准入

Task 与原始材料、owner fence、authorization、discovery cursor 使用 Chat 自有集合中的不同 recordKind 和唯一 _id；任务 CAS 行和不可变证明行互相独立，不依赖 session 标记的可变投递状态。Session 后续整行 replace 不会覆盖后台租约、回执或失败结果。该集合不能配置 TTL；工厂使用 majority+j 写入和 PRIMARY 读取，无启动建索引。

后台依次执行以下步骤：

1. 领取任务 CAS 租约，持久消耗一次执行次数，再核 PRIMARY 唯一用户消息。角色、原文、时间、mode、consumer、修订及两个摘要须与首次保存一致；同 ID 出现在两个会话或携带旧 jobId 都拒绝。周围会话的 updatedAt 和其他消息不参与固定证明。
2. 通过公开 Profile 命令取得 self scope，一次保存后不随重试重新选择主体；创建并存储不可变 profile.chat-source.v2 材料与固定事件。
3. 核对本域已经准备的 exact task/material，以及本次首次新模式业务消息；没有 owner authorization 却已有本域 reservation、authorization、receipt 或 receive 派发次数的来源，不能被重新称为 fresh。
4. 独立持久 owner fence，再调用 Profile 公共全历史 presence，检查所有状态与所有目标主体的 jobs、事实贡献和 publication/cursor/alias/cutover。包括空 subject 的旧 job、亲属事实和已删除事实贡献；存在历史、截断、缺主体或存储错误均不能获得 fresh 授权。暂时不可用按持久预算重试，不回落 legacy。
5. 固定 authorization 及 presence proof 并持久保存后，才调用 reserve_fresh_source。Profile 再次核验当前 owner fence 与完整 absence；blocked 仅表示旧入口已被阻止，authorize_fresh_source 返回 ready 后才允许 receive。重试复用同一个 authorizationId、时间、scope/ref/digest；reserve 重放也可能已经是 ready，保持其真实状态。
6. 首次 receive 未在本地保存回执时，ACK 丢失必须重发同一事件，让 Profile 校验原 fingerprint；不能仅凭同 eventId 的查询结果推断已接收同一内容。保存真实回执后才允许纯轮询。

owner fence 的 chat-source-admission.v1 证明依赖新模式从该消息首次业务保存起存在。支持协议内的旧 Chat sender 在检查 Profile marker 前就拒绝新或未知 deliveryMode，因此 blocked 尚不存在时也无法排入旧 job。全部旧执行与正式贡献仍通过独立 Profile presence 检查；不是用“本域队列为空”声称排空。Chat session lease 并非旧 sender 使用的锁，不能作为其排空证据。

所有部署中的旧 sender 必须已经支持该 deliveryMode guard。绕过协议的旧二进制或数据库直接改写不属于本次离线证明范围。新 source 的固定业务标记不提供改回 legacy 的命令；根装配回退时应继续保留已受理新任务的恢复能力，不能把已保存新消息交给 legacy 重发。

历史 read_admission_fence 仅读取固定证明、材料和摘要；is_admission_fence_held 另读当前那一条消息，检查持久绑定是否仍成立。消息删除、原文/角色/时间或固定消费模式变更会拒绝当前准入；正常追加新消息不会使旧证明失效。已正式接收的材料读取只依赖不可变材料，不读取 mutable session、不创建主体、不启动摄取。

## 有界重试与扫描

首次冻结 ChatProfileDeliveryPolicy 包含 max_attempts、retry_seconds、max_age_seconds 和 lease_seconds。每次领取任务先持久增加 attempts，进程中断或取消也消耗预算；receive 派发前另持久增加 dispatchAttempts。每个外部调用先续租，I/O 等待取 30 秒、半个租约和剩余总期限的最小值，返回后再校验 owner/revision/lease；过期结果不能提交，新调用不能越过总期限。旧 owner 不能覆盖或释放新 owner 的租约。

received/waiting/processing/retry_wait 只表示接收或等待；partial、failed、superseded、applied 分别保留。完整 applied 还要求同一 scope/source/event 和完整应用水位。superseded 还必须给出严格整数 supersededByPublicationRevision，且大于当前事件的 publicationRevision；appliedRetractionRevision 与 settledPublicationRevision 都必须覆盖该后续撤回修订。仅有当前修订的 settled 水位不能证明来源已撤回；不完整回执保存为可观察的协议失败，保留原已接收回执和业务消息，不伪装退休。首次 ACK 和后续轮询使用同一校验。后台持久失败不撤销业务回复；显式 redrive_transport 需要原因及完整新策略，保留累计次数与旧策略审计，不改变材料。Profile 已返回 partial/failed 等治理终态时，不能用传输重试绕过治理命令。

补偿扫描按 (userId, sessionId, messageId) 的固定字符串顺序 seek，每次最多指定批量，持久 cursor 通过 CAS 推进。发现已登记或失败任务不阻止游标前移；非法来源保存有界单条 discovery_error，不一直占批首。扫描到末页后将 cursor 归零，因此新会话以及已扫描旧会话后续新增的消息会在下一轮覆盖。任务执行队列另按 nextAttemptAt 排序；来源身份无有效字符串的破损记录不进入可投递来源，不能转 legacy。

recover_pending 是显式后台命令；普通 getSession/getHistory 和材料/证明读取不驱动补偿扫描。单源失租或失败不阻断其余任务。没有事务性跨文档承诺：业务标记补登记、不可变保存、owner 授权和外部 ACK 都通过同一固定身份重放补偿。

## 显式装配与验证

先调用 create_chat_source_resources(collection=..., sessions=...)，使用实际业务 Session 集合与一个无 TTL 的 Chat 来源集合，取得 store、owner、materials、snapshot_reader 和 fence_reader。将 snapshot_reader 作为公开 profile.chat-source.v2 解析依赖、fence_reader 作为 Profile owner_fence_readers['chat'] 注册，再调用 create_chat_profile_source(resources=..., inbox=..., presence=..., fresh_admission=..., legacy=...)。返回对象实现原 ChatProfileSource 的同步 prepare_user_message 与异步 ensure_queued，并提供独立 recover_pending；组合根负责安装和后台生命周期。两个工厂均无 I/O。

离线回归覆盖原文和 naive 时间保留、真实公开 Resolver/Inbox/fresh/application、scope/ref 冻结、多个 ACK 丢失与 ready 重放、全历史/跨主体/截断/查询失败、旧 sender 在 marker 前后拒绝、普通和 SSE 保存后零 Profile 调用、本域登记故障补偿、租约失效、取消预算、显式 redrive、坏记录跳过及扫描归零。外部存储和模型为离线替身，真实 MongoDB 并发、部署和生产切换仍待专门验收。

## 既有输入容量兼容

新消息首次固定来源模式时，若既有业务接受的 userId、sessionId、messageId 超过公开来源的 256 字符，或完整内容超过 128 Ki 字符、时间表示超过 128 字符，Router 明确选择 `deliveryMode=legacy`、`consumerRevision=chat-profile-legacy-v1` 并调用现有旧实现。原身份、原文与时间均不截断、不改写；没有新增 HTTP 校验或模型调用。该选择只在同消息尚无来源 marker 时进行，已有新模式或未知模式不能因输入变化或画像故障退回旧发布者。已持久旧模式继续原路径，默认根接线已独立完成离线验收，真实部署与历史来源迁移仍分别验收。
