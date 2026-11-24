# Record 独立画像上下文

更新日期：2026-11-10。Record 本域 Port、不可变输入 DTO、固定 renderer、完整分页读取及真实公共 Adapter 已实现并具备组合回归；默认组合根已独立装配；真实部署验收另行完成。本文不把离线验证写成实际部署完成。

## 本域契约

[profile_context.py](../modules/record/profile_context.py) 定义 Record 自有 subject、fact、page 与补充观察 DTO，以及固定 `record-profile-context.v1` renderer；[profile_memory.py](../modules/record/profile_memory.py) 定义 `RecordProfilePagePort`、`RecordSupplementPort` 和只读 `RecordProfileMemory.build_context(user_id)`。本域实现不引用 `health_profile.memory` 的可变模板、筛选函数或 Repository，不共享 Chat renderer，不创建主体、治理事实或调用模型。

Profile 读取 Port 返回完整分页所需的 subject 元数据、事实、view token、游标和完整性状态。[真实 Adapter](../adapters/record/profile_context.py) 校验公开 scope，并以固定契约/排序版本、subject/version、profile revision、来源状态可用性和规范化 source watermarks 形成 view token，再转换为本域输入；水位及 affected subject 数组仅顺序变化不会伪造版本变化。事实值在捕获时沿用原安全展示的深度、字符串和容器长度约束，返回副本；DTO 不构成新的画像真值。

## 完整读取与有界恢复

默认每页 300 条，整次上下文最多 100 页、5 秒，预算跨重启累计。必须读完本人和合法血亲各自的全部选定状态分页后，才执行本域排序和限额；不以每主体 1000 条截断替代旧全量排序。读取中出现 `restart_required`、subject/version 或水位变化时丢弃整轮候选，最多重新开始一次；再次变化、页数或时间预算耗尽、非完整末页、重复游标/事实等情况明确不可用，不能把前缀当成完整画像。

每页必须属于请求用户及当前选定主体，血亲还须与本轮主体列表元数据一致。完整输入记录本轮选定主体，保持单主体分页一致性；多个主体不是跨主体事务快照。`ready` 页上因来源 pending/applying 标记的 stale 保留其含义，不直接丢弃已经存在的当前事实，也不声称指定来源已经完整应用。缺少本人时返回空上下文，不创建本人主体或读取补充观察。调用方沿既有 Record 图片/画像容错契约处理读取不可用，已有创建 checkpoint 的上下文不重读。

## 固定 renderer 的兼容规则

本人仅接收 active/pending/disputed；排除泛化 family_history，只保留明确 `family_history.reported`。非本人只接收来自 active 且标记为合法 blood_relative 主体的 family_history 事实，事实本身仍按上述三种状态筛选；非血亲、朋友、未明确血亲关系和其它主体不进入本人上下文。

排序依次为状态 active/disputed/pending、本人优先、类别 allergy/medication/condition/laboratory/symptom/demographic/anthropometric/lifestyle/goal/family_history、有效时间降序、factType、factId。本人和合法血亲完成全量读取后共同应用最多 24 条事实、8 条 pending、8 条 family_history。laboratory.* 与固定人口学/体格单值项按 `(subjectId, factType, status)` 仅保留最新项，不能把 disputed 或 pending 当作 active 覆盖。

正文上限 6000 字符，维持原安全说明与 JSON 行形式，事实中的指令仅作为外部数据。字段、日期、来源类型、原值截断规则和输出顺序保持兼容；选中的超长行跳过后不从限额之外补入其他事实。renderer 在 Record 本域固定，Chat 以后调整模板不会无声改变建档首问输入。

Document 补充观察使用显式本人 scope，读取按原顺序的首 100 条，再保留日期未知或摄取尚未 completed 且有项目名和结果值的最多 8 条；原件观察保留原字段，不重写为画像事实。追加内容不能挤占已选事实，不能超过同一字符预算。补充查询缺失、错误、越权结果或超时只丢弃补充资料，保留完整事实上下文；不存在事实时仍可按原行为显示补充资料。

## 公开工厂与装配

`adapters.record.profile_context.create_record_profile_memory(profile_reader, *, document_observations=None)` 仅组合公开读取器与本域 renderer，不创建客户端、DDL、worker 或任何摄取任务。Profile 依赖只使用 `list_subjects`/`read_page`；Document 依赖只使用 `read(scope, limit=100)`。不会直接读取提供方私有 Repository、集合、任务或模板。

适配器明确固定 `profile-fact-page.v1`、`effective-start-desc-fact-id-asc.v1` 和 `document.observation-read.v1`，不导入提供方可变默认版本来自动跟随升级。血亲 relationship 按当前公开 v1 的 relation_type/is_blood_relative 字段读取；事实 value 保留 Python 类型后才使用 Record 自有安全展示规则，避免提前 JSON 转换改变旧正文。Document 观察保持字段插入顺序，分页范围或主体错误沿既有可选补充降级处理。

## 验证与剩余依赖

[离线回归](../tests/unit/agents/graphs/record/test_profile_context.py) 对照旧 `RepositoryHealthProfileChatMemoryProvider` 的完整正文，覆盖超过 1000 条候选、最新值与状态、血亲限额、安全原值、字数、整轮重启和预算、跨主体拒绝、补充首 100 条/最多 8 条及其降级；同时验证后续 Chat 模板变动不会影响 Record。

[真实公共提供方组合回归](../tests/unit/agents/graphs/record/test_public_profile_context.py) 使用已合入共同基线的 Profile 分页与 Document 观察读取实现，覆盖 1102 条事实、实际血亲关系字段、BSON UTC、原值冻结、来源水位排序/可用性、实际版本变化重启、旧补充正文逐字等价及无写入构造。默认 bootstrap 已独立装配，保留原建档上下文异常边界和 checkpoint；离线 Fake、完整 pytest 和静态检查均不替代真实 Mongo 并发、资料保留及模型质量验收。

## 默认生产装配

组合根已将本域独立画像上下文 provider 注入业务服务，默认不再构造跨 Record/Chat 的共享 renderer。两域读取同一权威 Profile 公共分页及 Document 纯读观察；构造无 I/O，执行时不触发 Profile 提取或模型调用。普通 HTTP 查询仍不经过该 AI 上下文路径。

历史 `health_profile_memory_provider` 显式注入仍供两域兼容；各域专属注入优先。默认情况下，旧应用 settings 中的该名称仅作为 Chat provider 的兼容别名，Record 使用自己独立的 provider。本次不切换任何正式来源 Publisher，也不改变已有 Prompt、配额或排序。
