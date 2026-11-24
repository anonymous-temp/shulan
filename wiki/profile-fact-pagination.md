# Profile 公共事实分页

本页记录 I-035 的独立提供方增量。分页读取不改变唯一权威画像，不调用模型，也不替 Chat、Record 或 Report 渲染上下文。消费者正式迁入本域 renderer 和来源发布仍由各自模块单独完成。

## 公开入口与固定选择

从 `modules.health_profile` 获取 `ProfileSnapshotReader`，从 `modules.health_profile.contracts` 获取 `ProfileFactPageV1`。原有 `read` 与其最多 1000 条上限继续兼容；需要完整筛选的消费者使用：

```python
page = await reader.read_page(
    user_id,
    subject_id=subject_id,
    page_size=300,
    statuses=('active', 'pending', 'disputed'),
    categories=(),
    fact_types=(),
    effective_from=None,
    effective_to=None,
    cursor=None,
)
```

每页大小为 1—1000，内部只读取最多 `page_size + 1` 条以判断是否还有下一页。服务不设置全部事实数量上限；消费者必须自行限制总页数、耗时及重启次数，超预算时明确失败或降级，不能把未完成扫描当作完整快照。默认状态不包含已删除事实，显式查询也禁止 `deleted`；其他有效非删除状态可以显式选择。

结果固定 `contractVersion=profile-fact-page.v1`、`sortVersion=effective-start-desc-fact-id-asc.v1`，按照事实有效起始时间降序、相同时间按 factId 升序分页。此排序用于稳定遍历，不代表消费者的临床优先级或 Prompt 顺序。页内保留公开事实的值、来源、断言、验证状态、创建与更新时间；默认 BSON 解码的无时区时间明确解释为 UTC，不按运行机器本地时区解释。

`nextCursor` 非空表示还有下一页，`truncated=true` 也仅表示当前页未覆盖查询的全部事实。公开的 pageId 标识本页数据，不是持久历史快照 ID，不能凭 pageId 回读旧版本。

## 作用域与游标

入口每次都按调用方 userId 和可选 subjectId 重新读取已有主体，不创建本人、不补 view、不建索引、不排摄取任务、不修复数据。subjectId 缺省表示只读解析现有本人；消费者若从 `list_subjects` 选择血亲，应显式传入该主体 ID。跨用户主体查询按不存在处理。

游标绑定请求 userId、传入的 subjectId（包括缺省选择）、规范化过滤条件、page_size、契约和排序版本，以及实际解析 scope、主体公开版本/内容、profileRevision 和来源水位摘要。后续页必须保留相同请求选择；过滤项顺序和重复项不影响语义，时间范围统一为 UTC。更换作用域、过滤条件、page_size、格式或版本时在存储读取前抛出 ValueError。

游标是有长度限制的不透明继续读取参数，不是授权凭据或加密签名。消费者不要解析或修改游标；Profile 始终用当前调用方作用域过滤存储查询，游标不能授权访问其他用户。旧 HTTP 查询游标与本接口不互用。

## 缺失、变化与不可用

| status | 含义 | 消费者处理 |
|---|---|---|
| ready | 当前页已完成乐观修订核对 | 累积结构数据，继续读取 nextCursor |
| missing | 主体不存在或非有效状态 | 作为明确缺失处理，不能触发隐式建档 |
| unavailable | 存储异常，或主体已有但画像 view 不存在 | 保留错误码，按业务失败或降级；不是空画像 |
| restart_required | 页内或跨页主体、画像修订、来源水位发生变化 | 丢弃本轮全部已读页，从第一页有限重启 |

非 ready 状态不返回事实或继续游标。参数错误与不匹配游标属于 ValueError，不混入服务不可用状态。存储异常不泄露原异常文本或健康数据。

每页在事实读取前后比较画像修订、主体公开信息和来源水位；来源待应用或正在应用时，即使这些状态在本次读中未变，也返回 `stale=true`，并通过 coverage 与 sourceWatermarks 明确说明。未注入来源账本时 `publicationStateAvailable=false`，不得把缺少水位当成所有来源均已应用。等待指定来源应使用原有精确来源回执和 `read(required_publications=...)`；分页不会替消费者推进来源。

这仍是乐观检查，不是跨集合事务快照。旧链写事实与更新 view 之间存在窗口；单页成功也不承诺跨主体原子一致性。任务若需要可恢复的固定输入，应由本域保存所选事实、关系、来源水位与 renderer 版本，再开始模型调用。

## 消费者预算与迁移建议

Profile 不在内部重试页查询。建议消费者对一次请求最多启动两轮完整扫描（初次加一次 restart），并同时设置总页数和截止时间；再次发生 restart_required、来源仍在应用或预算耗尽时，返回可重试业务失败，或采用业务已经明确允许的无画像/上一次完整固定快照降级。不得循环直到“刚好成功”，也不能把已读前半部分悄悄冒充完整画像。

Chat/Record 的总条数、待确认和血亲配额、latest-only 选择、类别顺序与字符预算继续属于各自 renderer。要保持既有选择结果，应先得到完整、同一轮次的必要主体事实集，再按本域固定版本筛选；无需把 Prompt、24/8/8 配额或 6000 字符限制移入 Profile。本接口没有自动切换现有消费者。

## 验证范围

离线回归覆盖超过 1000 条事实的多页完整性、同时间稳定游标、筛选与原值、作用域及页大小绑定、错误格式/版本、页内和跨页变化、来源 pending/applying、缺失主体与缺失 view 区分、存储故障脱敏、越界仓储结果拒绝，以及真实 BSON 编解码后的 UTC 时间和续页键。未连接真实数据库或模型，也未执行部署或来源切换。
