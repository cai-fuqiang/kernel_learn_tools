# PLAN-012: 翻译超时保护 — 90封邮件不能卡住

## 问题根因

92封邮件10分钟未完成，核心原因：

1. **无单封邮件超时** — `translate_body` 按段落逐段翻译，一封长邮件可能几十段，每段 Google 请求 timeout=30s + 重试 2^n 退避，单封最坏十几分钟
2. **diff 翻译无保护** — `_translate_diff_comments` 逐条注释调用翻译，大 patch 可能几十个注释，无日志无限流无超时
3. **`future.result(timeout=N)` 假超时** — 只放弃等结果，底层线程继续跑，进程永远不退出
4. **Google 重试退避太长** — 失败后 sleep 2/4/8 秒，3次重试一段就耗 14 秒

## 实施步骤

### TODO: 1. 单封邮件翻译超时 (batch_process.py)
- [ ] 用 `concurrent.futures.ThreadPoolExecutor` 包装单封邮件翻译，设 `timeout=60s`
- [ ] 超时则跳过该邮件，保留原文 body，记录 WARNING 日志
- [ ] 每封邮件翻译后打印进度 `[done/total]`（INFO 级别，不需 debug）

### TODO: 2. diff 翻译超时+日志 (batch_process.py)
- [ ] diff 翻译整体加 timeout（30s），超时则保留原始 diff
- [ ] diff 翻译前后加日志：邮件数、耗时

### TODO: 3. 降低 Google 翻译重试开销 (translator.py)
- [ ] `_call_with_retry` 退避改为 `min(2 ** attempt, 4)`，上限 4 秒
- [ ] Google timeout 从 30s 降到 15s（免费接口超过 15s 基本就是被限流）
- [ ] max_retries 从 3 降到 2

### TODO: 4. 线程级真正超时 (batch_process.py)
- [ ] 串行模式也加线程级超时保护（用 ThreadPoolExecutor 单 worker 包装）
- [ ] 超时日志明确打印哪个线程、已完成多少封、卡在哪封

### TODO: 5. 验证
- [ ] 用 `--batch-size 1` 翻译一个大线程，观察日志和超时行为
- [ ] 确认 92 封邮件线程不会卡住超过 3-5 分钟