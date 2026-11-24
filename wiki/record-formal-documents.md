# Record 固定正式文档消费

本文对应 `record-document-formal-preparation.v2` 消费能力，更新日期为 2026-11-10。该能力已实现 Record 域适配器、创建 checkpoint 与后台恢复，并通过真实 Document Formal、归档及 Profile 公共组件的离线组合回归；[默认组合根](document-source-composition.md) 已将新受理选择设为 formal v2，历史 prepared v1 与 legacy 保留实际实现。旧消费者说明见 [Record 固定文档消费](record-document-consumer.md)。

## 明确版本与固定准备

[公开工厂](../adapters/record/formal_documents.py) `create_formal_record_document_port(compatibility_service, formal_service, *, previous_port)` 只组装对象，不访问存储或调用服务。`compatibility_service` 提供已登记实际准备绑定的 `ensure_self_scope / prepare_bundle / read_bundle`；`formal_service` 提供 Document 正式来源命令；`previous_port` 保留 prepared v1 和 legacy 的精确执行实现。组合根需显式安装新工厂，不能把缺少旧实现解释成采用当前新版本。

[Record 领域 checkpoint](../modules/record/documents.py) 在原固定 scope、文档来源、三个快照引用、输出选择 `legacy_understanding / 1 / legacy-understanding-v1`、`record-document-renderer.v1` 及真实准备 trace/reused 之外，增加 `formalCommand`。其中固定 `requestId / commandId / commandRevision / operation`，命令版本为 `document-formal-source-v1`，操作固定为单来源 `base`。这些值在首次正式命令执行前随 `on_prepared` 写入现有创建租约及 revision CAS；保存失败不会开始正式发布。命令身份由同一完整准备 checkpoint 确定，恢复必须复算匹配，不能换用另一次 prepare 或部署最新版本。

受理时的 `documentConsumerRevision` 明确选择 legacy、prepared v1 或 formal v2。缺少修订且缺少准备证据的历史任务固定使用 legacy；过渡任务可从已有准备 checkpoint 推断唯一版本，混合版本或与受理 marker 冲突时明确失败。旧 V1 checkpoint、发布算法和 renderer 保持原实现；恢复 formal v2 时只读固定 bundle 并重放同一正式命令，不重新 ensure 主体、prepare、OCR 或调用解读模型。已经保存 completedFile 的附件直接复用，不再发布；没有完成的 formal v2 不能回退旧链。

## 等待真实归档与首问恢复

[创建协调器](../modules/record/application/creation.py) 先持久准备证据，再调用有界的 Formal `execute`。若命令已受理但尚无真实 base 归档回执，适配器返回内部持久等待信号：创建保持原 `creating / processing_reports`，公开响应继续使用既有 `processing` 状态，不写 completedFile，也不生成首问。此时仍缺少原报告输出所需的真实正数 `legacySourceRevision`，不能用材料修订、零、空值或猜测值代替。

正常持久等待仅返还本轮普通 attempt，并保留既有下一次扫描间隔和总 TTL；它不变成可无限延期的新创建。后台 `recoverPendingCreations` 无需 GET 即可继续固定命令；网络异常、回执不确定、父任务或单个附件取消不返还 attempt。多个附件同时存在正常等待和真实异常时，真实异常优先结算，不因另一个附件等待掩盖失败预算。GET session/history 继续纯读，不创建任务、运行模型或推进 Formal。

获得真实 base 回执后，Record 使用原 renderer 和真实 canonical 身份、旧来源 hash 修订及摄取状态完成该附件，随后按原附件顺序组装首问输入。来源模式为 events 时归档回执明确为 `source_managed` 且无伪造的旧 job ID；Profile 事件独立受理和应用，首问无需等其 applied。命令整体或该 base 明确 `failed / not_published` 时，仅把该附件按既有内容不可用规则保存为 unavailable，保留其他附件及原无报告首问能力，不将该文件失败自动升级为整个创建失败，也不回退旧发布链。身份、版本、命令或回执结构冲突仍是恢复错误，不能伪装成“资料为空”。

首次建档附件准备需要真实归档元数据；这与问答完成后画像来源的异步摄取是两条独立流程。本次不改变 Record 完成业务先保存、后台再执行 Profile scope/reserve/receive 的隔离边界，Profile 可用性不成为已完成问答的提交条件。

## 兼容身份与恢复证据

Record 既有业务接口允许部分超过公共来源身份范围的 user/session。对于首次受理、尚未固定消费者的请求，若原身份非规范化或超过当前公共 256 字符范围，则显式选择 legacy 并持久保存，保持原值和原业务行为；不修改 HTTP 长度校验，也不悄悄修复旧路径原本会拒绝的空白身份。已经接受 prepared/formal 或新画像来源版本的任务不能因为当前输入、部署或依赖失败改选 legacy。未知版本仍明确失败。

Formal checkpoint 与 completedFile 内部元数据只用于固定执行和恢复，不增加 HTTP 字段。原 `report / document / processing` 输出与首次模型输入继续由 Record 私有 renderer 决定，Report 的投影或正式补充来源升级不能改变其固定选择。重复附件保留各自真实 preparation receipt 和顺序，同一 document 的首问报告仍按原规则去重。

## 验证范围

[领域与恢复回归](../tests/unit/agents/graphs/record/test_formal_documents.py) 覆盖持久准备先于发布、精确版本与 command、ACK 丢失、无 GET 后台推进、等待预算与 TTL、取消和混合异常、终态附件降级、首问保存重试、已完成投影复用、回退拒绝及首次长身份选择。[真实公共组合回归](../tests/unit/agents/graphs/record/test_formal_document_composition.py) 使用实际 Document Formal、兼容准备、归档和 Profile 公共能力与 BSON/Redis 存储替身，验证真实 archive 元数据在 Profile 应用前即可生成首问、重启仅重放固定命令，以及重复文档不同准备 trace 的顺序与去重。模型使用固定测试替身，没有访问真实 OCR、LLM、MongoDB、Redis 或生产服务；生产默认装配和部署验收由组合根独立交付。

相关提供方见 [Document 正式来源命令](document-formal-source.md)。
