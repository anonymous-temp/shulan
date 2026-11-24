# Chat 自有画像上下文

本页描述 Chat 域 renderer、读取 Port、真实公共 Adapter 与离线组合。已接入固定版本的 Profile 公共分页和 Document 补充观察；默认 bootstrap 已独立装配，不改变来源发布或 HTTP 行为；真实部署验收另行完成。

## 归属与输入

`modules/chat/profile_context.py` 拥有 ChatProfileSubjectV1、ChatProfileFactV1、ChatProfilePageV1、ChatSupplementPageV1 和固定版本 `chat-profile-context.v1`。正文、优先级、配额及安全展示规则从共同基线 ab44768 的旧 memory 行为冻结；运行时不导入 Profile 或 Record 的 Prompt/renderer。后续修改本域展示不会影响其他消费者。

事实值进入消费 DTO 时按原展示限制复制并保存为 JSON，字段、来源类型和时间仅供上下文展示，不反写权威事实。默认 BSON 无时区日期解释为 UTC。数据中的文本仍按原提示作为外部材料处理，不能作为系统指令，也不将内部 factId 暴露给用户。

`modules/chat/profile_memory.py` 提供 ChatProfilePagePort、ChatSupplementPort 和 `ChatProfileMemory.build_context(user_id)`。构造和读取均不创建主体、补画像、建索引、排摄取或调用模型。真实 Adapter 只消费已合入共同基线的公共读取契约。

## 完整读取与预算

每主体默认每页 300 条，先完整读取本人及有效血亲的必要事实，再做 Chat 自己的排序与筛选。不能用公共单页最多 1000 条替代完整遍历，否则旧的高优先级事实可能漏选。

读取最多两轮（初次加一次因变化重启），总计最多 100 页、5 秒，页数和截止时间跨重启累加。页内/跨页 revision 或水位变化、subject 元信息不一致会丢弃整轮候选；第二次变化或预算耗尽明确失败，不能把前缀当成完整画像。读取当前有效主体缺失时直接返回无上下文，不创建本人。

每页校验 user/subject、状态、稳定版本 token、重复事实/游标/pageId、truncated 与 nextCursor 配对以及页大小。真实 Adapter 应以完整 scope、subject/version、profileRevision 和规范化来源水位生成 token；不得按数据库返回顺序制造版本变化。这是乐观一致性，不承诺跨主体事务快照。ready 但因来源 pending/applying 而 stale 的当前事实仍可使用，保持旧行为，不等同于指定来源已经完整应用。

事实读取失败由调用方按现有业务降级处理。可选补充观察失败、越界或耗尽剩余时间，只跳过补充，保留完整事实正文；默认接线保留既有 Chat 的异常边界与请求语义。

## 冻结展示规则

- 本人使用 active/pending/disputed，排除普通 family_history，仅保留明确的 family_history.reported。非本人只取有效且明确为血亲主体的 family_history，朋友和其他主体不会混入本人事实。
- 依次按 active/disputed/pending、本人优先、原类别优先级、有效时间降序、factType 和 factId 排序。总体最多 24 条、pending 最多 8 条、family_history 最多 8 条。
- laboratory.* 与固定人口学/身高体重项目按 subjectId/factType/status 保留最新值，不能用 active 覆盖另一状态的 disputed 或 pending。
- 保留原安全说明与逐行 JSON 正文，最多 6000 字符；选中但过长的事实被跳过后，不从配额之外回填事实，也不靠补充观察填补全部事实均放不下的情况。

Document 可选补充读取原顺序前 100 条，再选择日期未知或旧摄取尚未 completed、具有条目和结果值的最多 8 条。原始观察保持原字段，只在已选事实之后追加，共用字符预算。不存在事实时可以单独展示补充，已选事实不能被补充挤掉。

## 验证与剩余工作

离线测试对照原 memory 的完整正文，覆盖 1101 条事实先遍历再排序、最新值和各类配额、本人/血亲隔离、原值安全展示、两轮重启及总预算、作用域和不完整页拒绝、可选补充顺序/降级与模板独立。未连接真实 MongoDB、模型或回调。

实际 Adapter 位于 `adapters/chat/profile_context.py`：PublicChatProfilePages 消费 `profile-fact-page.v1` 与 `effective-start-desc-fact-id-asc.v1`，PublicChatSupplements 消费 `document.observation-read.v1`。不支持的版本明确失败，不跟随提供方的新默认版本。`create_chat_profile_context(profile_reader=..., observation_reader=...)` 只注入公共 reader，不创建数据库客户端或调用读取。默认 bootstrap 已独立装配，保留原模型请求、SSE 正文、会话落库和来源发布行为。

Profile 关系明确读取当前公共 v1 的 relation_type/is_blood_relative 字段；视图 token 包含完整 scope、subject/version、profileRevision、publicationStateAvailable 与按完整来源身份规范排序的水位。事实 Any 原值保留 Python 类型，交给本域旧安全展示函数后才转 JSON，避免提前 mode=json 改写深层值。Document 使用显式 scope 纯读，校验固定契约、首段大小及截断完整性，保持原字段插入顺序。

组合回归使用真实 ProfileSnapshotReader、DocumentObservationReader 和离线 Mongo 仓储，验证超过 1000 条的完整读取、合法血亲 snake 字段、原模型上下文逐字等价、页中修订变化、错作用域/版本拒绝、可选补充故障，以及真实 BSON 无时区 UTC 的稳定续页。不把离线组合或工厂可注入误写为真实部署验收。

## 默认生产装配

组合根已将本域独立画像上下文 provider 注入业务服务，默认不再构造跨 Record/Chat 的共享 renderer。两域读取同一权威 Profile 公共分页及 Document 纯读观察；构造无 I/O，执行时不触发 Profile 提取或模型调用。普通 HTTP 查询仍不经过该 AI 上下文路径。

历史 `health_profile_memory_provider` 显式注入仍供两域兼容；各域专属注入优先。默认情况下，旧应用 settings 中的该名称仅作为 Chat provider 的兼容别名，Record 使用自己独立的 provider。本次不切换任何正式来源 Publisher，也不改变已有 Prompt、配额或排序。
