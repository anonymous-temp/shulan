# API Lab 流式页面

Record 与 Chat 逐个解析 SSE 事件，但把正文、推理的 DOM 更新合并为约 50ms 一批。
接收结束或出错时立即写入剩余文本；切换公共身份或后端会取消旧的显示任务。
Chat 仅在原本接近底部时自动跟随，用户向上阅读时保持位置。
流式期间的结果区域显示接收状态和事件计数，结束或出错后才生成完整 JSON 查看器。

网络日志约每 200ms 刷新一次，最多显示最近 200 条、256Ki 字符。
单条超长记录显示截断提示。点击“导出完整记录”下载 JSONL，每行包含时间、请求标识、
事件序号、标题和完整载荷，导出不会受展示上限影响。生成期间也可以导出当时已收到的记录。
“清空”会同时释放内存中的完整记录，刷新页面也会丢失记录；清空日志不会清空正文或中断请求。
清空后新收到的事件继续保留原请求标识和事件序号。

## 验证

无需后端的回归测试：

```powershell
node --test tests/unit/api/api_lab.test.cjs
```

真实浏览器压力测试需要 Node.js 能加载 `playwright`，以及本机 Chrome。
可以设置 `$env:BROWSER_CHANNEL='msedge'` 使用 Edge。
脚本启动临时本地 HTTP 服务，所有 API 响应均为模拟数据，不转发到业务后端。

```powershell
node tests/unit/api/stream_browser_benchmark.cjs --output=outputs/stream-current
node tests/unit/api/stream_browser_benchmark.cjs --baseline=HEAD --timeout=30000 --output=outputs/stream-baseline
```

默认每个页面接收 10,000 个文本事件，另有业务结果和结束事件。
`--events=1000` 可以缩小样本；`--baseline=<提交>` 从指定提交读取页面资产，不切换或修改工作区。
每个页面默认最多运行 120 秒。基线超时记录为 `timeout`，不会虚构完整耗时；当前版本超时视为失败。
输出目录包含指标 JSON 与成功场景的页面截图。

指标包括首事件解析、读取结束、最终 DOM 更新、随后绘制机会、长任务和定时器最大间隔。
读取结束时间受前端处理速度影响，不等于服务端生成完成或数据到达网络缓冲区的时间。
`mockProducerToDomMs` 使用本地模拟响应发出时间，不能用作真实服务端耗时。
两次动画帧回调仅用于观测绘制机会，不构成实际屏幕呈现时间的保证。

真实接口复测时，启动业务后端和 `python -m tests.api.serve_pages`，使用测试身份分别调用
Record、Chat 的 SSE 接口，并用浏览器 Performance 录制页面行为。
将后端生成完成时间与浏览器接收、显示时间分别记录；浏览器模拟测试不替代真实链路验证。
