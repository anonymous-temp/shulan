# Profile 当前 Document 来源接收

状态：2026-11-13，Profile 当前接收提供方 `feature/profile-document-basic-info@b8fea47` 已由用户通过 !132 合入 `develop@054a2fc`；19d1f48 审核整改已完成。用户确认无生产数据、不保留旧数据兼容；当前单流程目标、具名分工及分批切换见 [实施计划](../docs/architecture/basic-info-single-pipeline-plan.md) 与 [I-048](../ISSUE.md#i-048-双来源基础信息与共享profile交接)。Analysis 当前公开端口已通过 !133 合入，Document 阶段以公开端口验证当前投递闭环。

## 1. 唯一当前语义与模块边界

Profile 负责当前材料验证、来源固定绑定、候选映射、字段可信度选择、治理和幂等恢复。Analysis 负责 AI 抽取与报告分档，Document 负责原文、固定归档、贡献和持久发布；Profile 不硬编码选择它们的私有实现修订，不复制其算法。

当前契约复用已完成的 basicInfo、clinicalContributions、贡献索引、材料引用、库存及 lifecycleEpoch 结构；既有 `.v2` 等固定标识用于材料校验和追溯，不向调用方提供版本开关。当前结构不携带 legacyState、legacySourceRevision 或旧贡献 carry，不建设历史资料解码、迁移、旧执行器或失败 fallback。

当前入口为公开的 `modules.health_profile.ProfileCurrentDocumentSourceResolver(reader)`，通过 `registrations()` 注册至 Profile 公共组合工厂；接收使用 `ProfileSourceInbox.receive(event, scope=...)`。工厂从当前注册项注入 Inbox 的 `current_document_source`，不更改既有工厂调用签名。公共当前 DTO 从 `modules.health_profile.contracts` 导入，包含 `DocumentCompleteSource`、`DocumentSourceInventory`、`DocumentBasicInfo`、`DocumentCurrentFence` 与 `ProfileDocumentSourceReadiness`。当前材料读取后只生成同一 ProfileSourceMaterial，所有来源进入共同 planner/apply；不再因 V1/V2 业务分支绕过共同规则。串行合并前仅暂留现有默认入口及其原有必要实现，不新增旧格式 wire 或历史解码模块；最终默认切换后由 Profile 删除，不作为历史兼容能力。

## 2. 首次来源绑定与可靠恢复

当前库存 fence 为 `{admissionKind: current, evidenceRef: planRef}`，activeSetEvidenceRef 指同一当前持久计划。该证据说明材料和计划的固定归属，不是调用方登录凭据或授权。Profile receive 先验证实际 reader、声明、内容摘要、范围和生命周期，再在既有 publication store 的 CAS 固定来源主体、sourceId、epoch 及当前契约，随后使用既有 alias/publication/cursor 接收流程。

首次材料引用、摘要和计划可保存作受理审计；后续合法更正允许新的计划与内容，不把首次绑定当成禁止更新的 grant。每条 publication 的 fingerprint 及事件别名继续固定相应内容。当前材料不要求旧表 absence 扫描、旧 writer drain 或旧数据迁移证明；正常并发、主体归属、材料证据和生命周期仍需校验。

当前撤回沿用既有固定契约标识，原文必须携带原持久计划的正整数 lifecycleEpoch。即使撤回先到、发布尚未到达，也能凭真实材料绑定原生命周期并建立撤回屏障；后到发布不能复活。不能从当前主体推断原 epoch，也不因缺证先占住来源。

原 lifecycleEpoch 贯通来源绑定、publication、固定计划与基础字段回执。归档恢复后旧任务不能重新绑定新 epoch；临床及基础字段物化保留原生命周期，概览、事实分页、公共快照和记忆统一排除跨 epoch 残留。接收中断的旧 epoch staged 事件以既有失败回执终结，不永久占用有界扫描。

正常恢复只重放当前固定请求与计划。回执丢失、重启和并发不重新解析文件、调用模型、重新领字段水位或重算已完成的分析。重复完整文件仍复用当前结果，部分执行按既有机制恢复；没有新增上传或强制重解析入口。

## 3. 独立贡献与共同应用规则

报告字段只接受 AI 给出的 low/medium、可确定目标主体、合法值、非空理由及匹配原文证据。报告年龄表示报告时点，可为 0–150 有限数值；客户端明确确认仍要求整数周岁并固定 very_high。当前值、同档保留、同值升档、缺失不清空及治理屏障沿用共享 Profile 服务。

活动基础字段或明确退休的基础贡献才应用完整字段库存。临床资料不完整或临床库存未变化时保留原临床事实身份；明确退休可形成空活动集。临床撤回不移除人口学独立依据，基础字段变化不触发临床或 Report 重建。

共同应用入口在读取、恢复或执行计划及基础字段动作前检查同来源前驱材料修订，保留临床内容的优化也不能绕过。材料回退产生确定失败，不写事实或当前值，不推进应用/结算水位；已受理审计记录不回滚。内部材料修订与外部不透明报告 ID 分开，不把外部 ID 数值用于排序。

当前事实游标无论基础信息修订是否为 0，都绑定实际修订和生命周期；同一范围未变化可连续翻页。有效游标遇到真实修订/epoch 改变返回 409；缺必要绑定、类型错误或查询不符返回 422，不猜缺省 epoch。对外说明见 [当前 API](../docs/api/API.md#用户健康画像接口)。

## 4. 审核回应、验收与交付

人工拒绝按原主体、生命周期和事实身份保留在画像治理中。显式退休后，只从画像仍认可的依据中回选。已拒绝报告贡献的固定身份和来源证据仍可追溯，报告库存更新不会恢复该候选或物理事实。

已有固定基础动作的在途更正，可使用原事件的固定材料、内容摘要、处理选择及生命周期补齐退休证明。补证与原动作绑定并持久保存，应用沿用原计划、动作摘要和发布修订。已提交但尚未写入回执的动作按原提交身份恢复，终态失败沿用显式重驱，整份撤回仍按既有屏障优先。

Document 显式退休固定库存中的基础贡献时，Profile 在应用计划保存退休证据引用与贡献指纹。当前库存必须精确等于前序库存减去本次已验证的退休贡献，方可在同一次画像 CAS 中退休对应依据并重选。历史退休记录保持可追溯，后续普通更正沿用原有可信度比较。应用重放按固定摘要和发布修订核对。合入候选使用 !135 已交付的 Document 工厂验证回选、重放及后续更正，证据见 I-048。

F1：共同入口防材料回退，并覆盖两条保留临床路径和持久计划恢复；必要前驱材料缺失沿既有可恢复错误等待，不当作版本 0 继续。F2：零修订新生命周期正常翻页，同时保留真实变化拒绝。F3：错误声明/引用/处理标识在任何准入持久记录之前拒绝，正确重试不被错误输入锁死。均作为本次合入前必要整改，不推迟到消费者或改文档消解。

本轮 Profile 作者为 `/root/document_plan_review`；Analysis 为 `/root/profile_plan_review`，Document 为 `/root/document_provider`。三方已确认本轮端口与职责，作者定向执行完整 Profile 域及两份真实 HTTP 测试为 1427 项通过，Ruff/格式检查通过；这些专项不替代全项目回归。后续非作者复核绑定新候选。此前 19d1f48 的 12579 passed / 275 skipped、旧 12568 项及历史 R1–R5 复核均不能代替整改后验收。

最终 Ruff/check、format、文档示例/链接、完整离线 pytest、受测文件指纹与提交对应关系统一记录在 `D:/996-work/shulan-test-artifacts/profile-single-pipeline-review-final.{log,xml,json}`。未退出 0、文件发生变化或与最终提交不符的记录不作为通过。仅使用合成资料，未验证真实数据库、模型、多进程或部署。

本 Profile 批次已由用户合入 develop，后续模块同步后实际接线；最终还须由各模块删除过渡入口，再完成默认单装配组合验收。主智能体不推送、创建 PR 或自行合入 develop。平台 PR、I-048/!131 关联、最新源 SHA、正式批准及合并由用户或维护者处理。临时人工审核文档保持原样、不入库。
