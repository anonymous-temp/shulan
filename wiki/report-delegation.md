# Report 公开持久后续任务

本页记录 refactor/agent 重构中的 Report 提供方与 GutBarrier 消费者实现，尚不表示已经上线。生产装配已使用公开持久受理；正式上线前仍需真实 Mongo 并发及外部回调验收。

## 公开入口与所有权

`modules.report.delegation.ReportFollowupService.submit(GutBarrierFollowupRequest)` 接收已保存的 GutBarrier 主报告固定信封，校验 userId、sessionId、finalReportId、type=gutBarrier 与明确契约版本。受理返回 FollowupReceipt；重复受理始终返回同一持久任务，输入不同则显式冲突，不沿用普通报告的短时复用窗口。普通 get_receipt 仅读取本域任务，不启动模型或恢复。

Report 拥有 explain_gutBarrier、opinion、instruction 三类后续产物及交付；内部任务 ID 与主报告 finalReportId 分开，回调正文保留原 finalReportId/type。三组独立集合 report_followup_jobs、report_followup_artifacts、report_followup_deliveries 不进入普通 Report 列表或 Chat 自动选择。数据库连接由组合根提供，不新增 HTTP 路由或消息中间件。

## 固定版本、恢复与交付

首次受理前重新校验并隔离调用方可变输入，保存完整输入、摘要、执行版本和独立生成超时预算；新服务不支持已固定版本时明确封口，不能改用当前实现重新生成。当前生成复用既有 Report 后续流程和 Prompt，不增加主控模型调用。进程中断后保留已冻结产物，只为缺失的三类后续产物生成失败终态，不生成 evaluate 或普通 explain，也不重新调用模型。

产物首先在任务文档内通过租约令牌和修订 CAS 固定，再投影到产物及交付集合。冻结成功后投影失败可由后台恢复补齐；旧租约不能写入新结果，同任务同类不同正文冲突。生成、交付状态独立，回调采用至少一次语义和稳定幂等键；所有非 2xx 持续退避补投，不因生成完成或预算耗尽停止。

## 后续接线与验证

GutBarrier 必须先持久记录委派意图，再通过此公开入口受理，主回调不等待后续生成。生产接线还需注册此服务的生成、恢复和回调 Worker，并复用应用排空与资源所有权；仅调用 submit 不等于后台调度已完成。委派传输次数/截止时间与此服务的回调补投分别管理。

离线测试覆盖永久幂等、身份隔离、三个产物及原信封、部分失败、冻结后投影故障恢复、旧租约写入、版本不可用和非 2xx 补投。真实 MongoDB、多进程故障及外部回调尚未验收。

## GutBarrier 接入与生命周期

生产装配通过 `PublicReportDelegationAdapter` 调用公开受理服务，不再跨域调用 `_push_report`。GutBarrier 主报告完成时，在同一个 Mongo 文档内同时冻结主报告、主回调意图与 Report 委派意图。独立 Worker 从持久意图领取任务；主回调失败不阻塞委派，后续生成或投递失败不覆盖主报告。

委派的 v1 策略在受理时固定：最多 8 次受理尝试、完成主报告后 300 秒期限、单次调用最多 15 秒。默认策略是内部配置，不新增 HTTP 参数。发送前先查询同一幂等 ID 的回执；超时或丢失确认保留未知结果，预算耗尽后进入 `needs_reconciliation`，不会以新 ID 重试。受理已成功时，即使 GutBarrier 委派预算耗尽，Report 的生成和持续回调仍独立执行。主回调及后续回调所有非 2xx 均继续按既有政策补投。

旧版已完成主任务不会因升级而再次委派。旧版未完成主任务若由本版完成或中断封口，按显式 `legacy-unfinished-main.v1` 兼容映射创建一次持久委派；中断主任务仍返回原失败封口，不重新运行临床生成。已有 `delegationPolicy` 的任务使用其保存版本，不静默选择新策略。首期没有逐步续跑生成或自动重放未知外部副作用。

启动扫描、周期扫描和关闭停止领取均已装配；不依赖任何查询请求。主报告、主回调、委派受理和 Report 后续生成/回调分别拥有状态。尚未新增 GutBarrier 查询路由，也未改变 Report 列表和 Chat 自动选择范围。离线原子集合 Fake 可验证状态机，真实 Mongo 多进程和外部回调联调仍需单独验收。
