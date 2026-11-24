# Document 当前基础信息来源

> 2026-11-13：本批同步 `develop@a3ccf12`，通过 Analysis 公开端口（!133）与 Profile 当前接收（!132）复验观察时间修复（!134）。API 以[当前契约](../docs/api/API.md)为准。

[实施说明](../docs/architecture/document-basic-info-v2.md)记录公开准备端口、固定原文、独立贡献、持久恢复和后续接线。

- 使用一个当前 Understanding/Source/Admission/Managed/Publisher 路径，没有双版本 reader、历史迁移或旧执行器回退。
- 只调用 Analysis 公开 `prepare_document`、`read_prepared_round`，不选择 v12/v13、不访问私有 reuse。实际抽取、校验及输出修订分别留档。
- 姓名、性别、年龄的 AI 中档/低档与原文证据原样保存；不生成 API 最高档，不决定 Profile 当前值。
- 基础字段和临床分别准入。完整与部分解读均可投递已提取内容，Document 保存原质量与固定证据。Profile 固定报告归属，逐条区分原件可核对的观察与待核实项。未知时间和值可以保留为空。
- 来源固定主体和生命周期，使用既有持久记录与 ledger CAS；无新授权、absence 扫描或投递队列。撤回正文携带原 `lifecycleEpoch`。
- 当前 `.v2` 是唯一来源协议标识。现用旧默认入口仅为串行合并暂留，最终须按[清理清单](../docs/architecture/document-basic-info-v2.md#阶段-4-依赖补齐与阶段-5-默认清理)删除。

工厂为 `adapters.analysis.basic_info`。先构造固定 current readers，再注册真实 `ProfileCurrentDocumentSourceResolver` 和公开 Profile 工厂，最后以相同 readers/ledger 构造 publication runtime；没有延迟代理或后补接线。当前 receiver 直接调用公开 Inbox，不申请 cutover。

Document 显式工厂已接入 !137 的 Report 原件投影。它使用当前固定材料和对应原件，保存实际投影、执行凭证、质量和逐文档临床依赖。完整质量且有原件出处的检查可通过现有来源计划发布。合成归档和 BSON 组合验证涵盖投影重读、追加、替换、退休与回执恢复。Report、Record 消费者和默认上传装配按后续阶段接入。[Report 接线边界](../docs/architecture/document-basic-info-v2.md#report-当前原件事实投影与发布依赖) · [I-048](../ISSUE.md#i-048-双来源基础信息与共享profile交接)
