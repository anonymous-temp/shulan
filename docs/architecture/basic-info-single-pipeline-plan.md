# 双来源基础信息单契约流程收敛

状态：2026-11-13，用户确认无生产数据、不考虑旧数据兼容，并批准按模块统一后分批交付。本页取代本轮此前要求保留双版本历史材料读取和旧执行器的决策。正常在途任务恢复、材料固定、幂等和生命周期治理继续保留。

当前进度：截至 `develop@eea8890` 已合入 Profile、Analysis、Document、Report 的前置提供方及固定材料消费。统一集成分支完成 Report、Record、Health Analysis 消费者接线、双来源原件观察去重与默认装配，审核整改及实际验证见 [I-048](../../ISSUE.md#i-048-双来源基础信息与共享profile交接)。

## 1. 目标与唯一处理链

现有上传入口 → Analysis 完整解读 → Document 固定归档与贡献发布 → Profile 统一验证、登记和应用。客户端明确填报仍以 very_high 调用 Profile 确认服务。报告字段由 AI 逐字段给 medium 或 low；程序验证值、原文及证据，不重新决定档位。

复用已完成的当前材料结构：固定材料引用、贡献索引、clinicalContributions、basicInfo 和生命周期。只有一份当前语义，不增加第三套协议；契约标识和真实实现修订只用于校验与追溯，不作为调用方选择历史算法的开关。缺失基础字段的临床材料是同一流程的正常输入。

不建设历史迁移、旧快照解码、旧执行器选择或失败后回退路径。不自动删除或迁移开发数据库。姓名、性别、年龄逐字段可信度与当前选择由 Profile 唯一负责；固定分析与已有请求的原始输入不因补偿重算。

### 当前材料与首次来源绑定

当前来源继续使用已有新增契约标识，不另建协议。source payload 保留 basicInfo、clinicalContributions、clinicalAvailable、固定 contribution 索引及 coverage；coverage 的 lifecycleEpoch 与 inventory 必须相同。删除当前结构中的 legacyState、legacySourceRevision 和旧贡献 carry。

inventory 的 fence 固定为 `{admissionKind: current, evidenceRef: planRef}`，activeSetEvidenceRef 绑定同一持久计划。它是材料归属与固定计划证据，不是调用方授权。Document 在自身 ledger 的现有 CAS 固定准备输入、主体生命周期、命令和计划；Profile 在统一 receive 完整验证实际材料之后，在既有 publication store 固定首次来源绑定，再进行 alias、publication 与 cursor 接收。重复请求复用原绑定，不能重新领取当前主体或生命周期。

当前撤回沿用既有固定契约标识，原文必须携带原持久计划的正整数 lifecycleEpoch。即使撤回先到、发布尚未到达，也能凭真实材料绑定原生命周期并建立撤回屏障；后到发布不能复活。不能从当前主体推断原 epoch，也不因缺证先占住来源。

当前路径不要求扫描旧来源表证明 absence，不做旧 writer 排空或旧数据迁移。正常并发冲突、固定命令、材料摘要、退休证明及主体生命周期仍必须验证。现用默认接线的过渡限制不传播到新当前材料入口。

## 2. 审核整改

| 审核项 | 本轮处理及验收 |
| --- | --- |
| F1：preserve_clinical 绕过材料回退检查 | 在共同应用入口、读取或执行持久计划及基础字段动作前验证同来源前驱材料修订。仅基础字段、临床摘要未变和恢复计划都不能绕过；拒绝后不写画像，不推进 applied/settled 等应用水位，保留 accepted 受理记录。外部不透明报告 ID 不用作内部修订排序。 |
| F2：零修订新生命周期游标自拒绝 | 新游标始终绑定实际基础信息修订和生命周期，包括 revision=0。同生命周期正常翻页；真实变化返回 409；缺失必要绑定、错误类型或查询条件不符返回 422，不猜 epoch。 |
| F3：错配材料先占住来源 | 在准入记录持久化之前验证声明、处理标识与材料引用自一致。错误输入不留下阻塞正确重试的记录；所有入口复用共同校验，不由 facade 动态切换执行器。 |

这些是本次合入前的必要修复，不以改文档或推迟到消费者接线替代。审核原文保持原样，不纳入提交。原 19d1f48 的 12579 passed / 275 skipped 只证明旧候选，不作为整改后验收。

## 3. 当前负责人及交接

| 模块 | 实现负责人 | 当前职责与依赖 |
| --- | --- | --- |
| Profile | `/root/document_plan_review` | 在主目录 feature/profile-document-basic-info 修复 F1–F3，统一当前端口、验证和 planner；不改其他模块生产代码。 |
| Analysis | `/root/profile_plan_review` | 在 refactor/analysis 交付唯一 current 抽取与公开准备、固定轮次读取；不修改 Document/Profile。 |
| Document | `/root/document_provider` | 在 feature/document-basic-info 交付唯一 current 材料归档和独立贡献发布；消费 Analysis 公开端口，不调用其私有对象。 |
| 协调与交付 | `/root` | 维护依赖、文档和验收，串行执行本地 Git；不代替其他模块实现。非作者复核独立执行并绑定新提交。 |

2026-11-13 本轮交接：三位实现负责人已在当前会话确认模块范围，Analysis 与 Document 直接确认公开 prepare_document 和固定轮次读取的 I/O；Profile 与 Document 确认共享当前结构及不硬编码 Analysis 私有实现修订。上述是本轮智能体实施交接，不援引旧 2c618ef 的复核作为本轮确认，也不代表平台 PR 获得批准。

## 4. 分批交付和默认切换

本次由 `feature/basic-info-integration` 汇总既有模块提交并完成组合验收。以下分批记录保留已实施的调度顺序，后续交付以该集成分支整体对 `develop` 的差异为准。

1. Profile 共同治理、三项修复和唯一当前端口已由用户通过 !132 合入 `develop@054a2fc`，原验收记录保持不变。
2. Analysis 当前抽取和公开准备、固定轮次读取端口已通过 !133 合入 `develop@d0e1f9c`。
3. Document 已通过 !135 合入当前归档、独立贡献与发布，Profile !136 补齐显式退休和在途恢复。
4. Report 先交付当前原件事实投影。合入后，Document 固定真实投影及发布回执，并支持 Report/Record 消费。各消费者与组合根在所属分支接线，依赖齐备后切换当前装配与 Analysis 默认输出。
5. Profile、Analysis、Document 各自删除过渡门面、旧默认实现和双注册，再进行最终实际组合验收。

历史接线约束（默认切换前）：当时的 Document 完整来源服务硬性要求原 Analysis 输出修订。不能提前切 Analysis 默认输出，也不能把新增产物改标为旧修订规避检查。串行合并期间只暂留现用默认入口与原有必要实现，不新增旧格式 wire、历史解码或旧来源迁移模块，并明确列为阶段 5 删除项；它们只保障当前源码依赖，不提供历史数据兼容、客户端版本选择或失败回退。第一批提交不能被表述为全项目已经单流程。

默认切换前的安排由 Report 所属分支完成固定贡献及依赖声明的当前投递接线；Report 当前来源投影端口由 Report 所属分支交付；Document 若缺固定回执、追加/替换能力，由 Document 分支提供。双方在真实接口明确后冻结契约，不新增悬空实现；现用 `legacy_report_source` 中仍承担原件事实投影的能力不因名称删除，不把生成报告正文或简单原文共现当成原件事实。该项不扩大 AI 生成产物进入健康事实的范围，不允许在最终组合时遗漏现有 Report 功能。

每个模块只修改所属代码，真实依赖通过用户合入 develop 后接入，不复制跨分支实现。主协调者不推送、不创建 PR、不自行合并 develop；平台目标、最新源 SHA、I-048 及本批依赖 PR 关联及正式批准由用户或维护者核对。

## 5. 验证及记录

每个生产候选均执行 Ruff check、format --check、相关文档格式/JSON/链接检查、完整 python -m pytest 和非作者复核。记录命令、环境、完成时间、结果、跳过原因、受测文件 SHA256/Git blob 与最终提交；仅创建内容相同的 commit 不重复测试。若源内容改变，重新完成完整回归和复审。

专项覆盖材料回退两条 preserve 路径、同修订/新修订、乱序和固定计划恢复，零修订连续翻页与真实冲突，错配准入拒绝后正确重试，以及临床/基础独立应用、撤回、原生命周期、回执丢失和无额外模型调用。对因新需求取消的旧版本切换专用测试明确记录替换原因，保留有效业务回归，不放宽断言或新增无依据跳过。

本轮 Profile 最终证据统一保存为 `D:/996-work/shulan-test-artifacts/profile-single-pipeline-review-final.{log,xml,json}`；只有最终退出 0 且指纹对应提交才作为验收。此前证据不覆盖本轮代码。仅使用合成资料，不读取私有配置，不进行真实数据库、模型、回调或部署验收。当前总体状态见 [I-048](../../ISSUE.md#i-048-双来源基础信息与共享profile交接)。
