# PLAN-013: 段落对齐翻译重构 + 性能优化

## 背景

翻译流程存在两个核心问题：
1. 旧方案按行翻译后拼字符串，再用 DP 猜测中英文对齐，常出错
2. 大线程（92封邮件）翻译时卡住、无进度日志、无超时保护

## 已完成的工作

### 1. translate_body_aligned 段落对齐重构 (translate_context.py)
- [x] 新增 `translate_body_aligned()` — 核心翻译函数，按段落翻译返回 `List[(en, cn_or_None)]`
- [x] 新增 `_render_bilingual_from_aligned()` — 直接渲染对齐列表，无需 DP 猜测
- [x] 新增 `_filter_aligned_for_text()` — 从对齐列表过滤 diff 段落
- [x] `_html_email_node` 支持新(list)/旧(str)两种 translated_bodies 格式
- [x] 保留旧 `translate_body()` 和 `_render_bilingual_body()` 做兼容
- [x] 支持 `progress_cb(done, total, is_translated)` 段落级进度回调
- [x] batch_process.py / kb_web.py / translate_context.py main 全部切换新接口

### 2. should_translate 修复 (translate_context.py)
- [x] 先调用 `_split_body_and_diff` 分离 diff 再判断正文部分
- [x] 修复含大量 diff 的 patch 邮件被误判为不需翻译（46封→90封需翻译）

### 3. _split_body_and_diff 优化 (translate_context.py)
- [x] diffstat 正则回溯优化：O(N) 向上回溯算法替代 `(pattern)+` 重复组正则
  - 104 封邮件：0.125s → 0.001s (125x 提升)
- [x] 向上吸收 git format-patch `---` 分隔线
- [x] `--- a/` 匹配时也回退到 diffstat 起始位置
- [x] diffstat + "create mode" 行不再出现在翻译对照区

### 4. content_parts.index O(N²) 优化 (translate_context.py)
- [x] `id()` 映射 + 滑动索引替代 `.index()` 线性搜索
- [x] translate_body_aligned (mock)：0.628s → 0.267s (2.4x 提升)

### 5. 翻译超时保护 (batch_process.py)
- [x] 单封邮件超时：SingleThreadPool + timeout (60s + 10s/KB, 上限 300s)
- [x] diff 注释超时：独立 timeout (60s + 10s/KB, 上限 180s)
- [x] diff 注释逐个进度日志
- [x] 大 diff (>8KB) 跳过注释翻译

### 6. 日志实时输出 (batch_process.py)
- [x] `_FlushHandler` 每条日志立即 flush
- [x] 支持 `LOG_FILE` 环境变量同时写文件
- [x] 每封邮件翻译前打印 body 大小和 timeout

### 7. Google 翻译参数优化 (translator.py)
- [x] timeout 30s → 15s, max_retries 3 → 2
- [x] 退避上限 `min(2**attempt, 4)` (最多 4 秒)