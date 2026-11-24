# Analysis 固定正式来源用例

本文描述独立用例 `analysis-formal-source-v1`。它复用原独立 Analysis HTTP 的参数校验、文件处理、内容投影与错误转换，只在明确 AI 解读请求中接入 Document 正式发布。[默认 API factory 与后台周期](document-source-composition.md) 已接通；离线实现验收不代表真实环境已经部署。

依赖 [Document 正式编排](document-formal-source.md) 与 [受管理来源](document-managed-source.md)。旧 `HealthReportProcessingService.process/process_document` 默认实现仍可执行；新 wrapper 显式选择版本，没有新增 HTTP 参数、路由或模型调用。

## 原请求语义

新 wrapper 仍调用原 `processing.process`，DocumentReuse 的策略只允许明确 Analysis 版本。原 URL/documentId 校验、主体绑定、别名预留和指纹冲突检查继续执行；共享 Record/Report 的普通 process_document 不因该 wrapper 改变。

已有非 canonical 的历史 reportId 仍使用原真实 legacy 分支，返回其首次保存快照。新文件和 canonical 引用在原文件执行中只准备，不隐式调用旧发布；启动或等待原解析前，先持久保存 Analysis 意图，固定 scope、原件身份、trace、解释实现、目标轮次与别名。Document 在每轮完成时保存不可变归档清单；HTTP 或后台据此捕获同轮快照及原 preparation receipt，不重新从 URL 选择材料，不启动 OCR 或模型。后续解释轮次不能替代已接受输入。

内容型不可用、非技术超时且可展示的部分结果仍沿原独立 Analysis display/error 链返回；没有另写一套内容失败规则。技术失败仍按原 cause chain 交给 HTTP 适配层转换。内容不可用时仍保留原 alias 预留，不创建虚构事实或来源修订。

## 发布与别名

Analysis 在解析前接受固定意图。材料就绪后先持久保存不可变准备引用、经过最终展示投影的响应分块及就绪证据，并通过 PRIMARY 读回核对身份与摘要；HTTP 至此即可返回，不执行 Document base 或 alias。后台在每个可独立推进的交付阶段核验相同就绪证据后，才提交固定 Document base。命令固定原文件与解释版本/轮次、原请求 trace、reportId 和独立命令版本；不能把同一 ID 改为不同材料或别名。准备回执写入后确认丢失，也只重用原快照。

正文使用本次固定解读，不等待来源或画像回执，省略尚无依据的 sourceRevision；不填 1，不把 pending 当画像应用成功。已有历史别名继续按其首次保存快照读取。来源失败或不发布状态在持久任务中保留，不回旧正式写链。

独立别名步骤先检查真实基础回执与原文件绑定。legacy 来源经原 Gateway 保存别名；managed 来源经 Profile 公开 archive 接口保存历史，不入旧队列、不发布额外事实。已有别名只验证 scope、canonical、原件和指纹后保留首次快照，不覆盖、不迁入、不重新入队；新请求的正文仍按本次固定解析返回。

## 恢复与装配

`create_analysis_formal_processing_service` 返回 `service` 和 `delivery`。它使用同一处理器、compatibility、Formal、managed ledger 和 Profile archive，构造不访问存储或模型，不拥有新客户端。Analysis 命令使用单独永久集合，PRIMARY 与 majority+j 写入；唯一 `_id` 和租约 CAS 保护固定进度，不添加 TTL。

根分别注册 Document Formal 的 recover_pending 和 Analysis delivery 的 recover_pending。Analysis delivery 只等待原解释、读取固定轮次归档、捕获快照和就绪证据、提交 base 命令并处理 alias 归档，不生成、恢复 OCR 或从查询唤醒。HTTP 在 OCR 或解释期间取消、等待超时后，后台仍可继续已登记意图；base/alias 回执丢失时保持原身份重放。原解释明确不可重试时意图明确终止；内容不可用但已归档的响应同样必须先就绪，再以 not_published 结束交付。较早轮次有固定归档时可恢复其历史和别名，不能回退较新的基础来源；缺少原轮次证据时不借用后来结果。可重试失败可能是领取前观察到的旧尝试；没有本次终态证明时仅有界等待，到原持久期限后标记失败，不自行重开解析。已完成命令纯读，未知版本明确失败。

该执行器复用 Document 的有界领取、持久期限、累计传输失败和取消/lease CAS；每次执行至多两次领取、16 个阶段，推进计时不含受理及最终读取。达到期限或预算后保留失败进度，既有 Document outbox/Profile Inbox 不停止。没有通用重开失败命令能力。

离线组合使用实际 Document/Profile 准入、归档与发布实现及 BSON 存储替身，核对 legacy/fresh、别名首次快照、画像不可用、ACK 丢失、后台恢复、空 OCR 与技术错误、解析期间取消/超时、旧轮次拒绝和准备回执确认丢失。真实 Mongo 并发、模型质量、外部服务与部署仍须单独验收。

本轮两项处理、归档枚举与画像展示的边界见[体检解读当前实现](health-report-interpretation.md)。
