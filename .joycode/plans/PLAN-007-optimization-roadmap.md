# PLAN-007: 项目优化路线图

## 现状概述
项目已完成核心功能：commit 驱动的 Lore 邮件抓取 → 预处理/分级 → 翻译 → HTML 线程树报告（v2 双栏对比布局 + 聚焦视图）。以下是按优先级排列的优化方向。

**最后更新**: 2026-04-13

---

## 一、性能与效率优化（高优先级）

### 1. 翻译并发加速 ✅
- [x] translate_context.py 当前逐封串行翻译，改为 `concurrent.futures.ThreadPoolExecutor` 并发
- [x] Google/有道后端支持并发 3~5 路（加 rate-limit 保护）
- [x] API 后端支持并发（用户可配置并发数 `--workers`）
- [x] 实际收益：100 封邮件从 ~480s 缩短到 ~216s（4线程）

### 2. 翻译缓存机制 ✅
- [x] 基于 body hash 的本地翻译缓存（SQLite），避免重复翻译
- [x] 缓存 key = sha256(backend + body_text)，value = translated_text
- [x] `--no-cache` 参数强制刷新
- [x] 实际收益：二次运行缓存命中率 99.5%，从 ~216s 缩短到 ~40s

### 3. 增量翻译支持
- [ ] 检测已有 `_translated.html` 输出，只翻译新增/变更的邮件
- [ ] 合并旧翻译结果 + 新翻译，避免全量重做

---

## 二、翻译质量优化（高优先级）

### 4. 代码/数据行误翻译防护加强 ✅
- [x] `_is_code_or_data_line()` 增加内核常见模式：`dmesg` 输出、`ftrace` 输出、`perf report` 格式
- [x] 增加单元测试：`tests/test_code_detection.py` 收集 60+ 真实样本（positive + negative）作为回归测试用例
- [ ] 合并连续代码行为代码块后整体跳过，避免中间夹杂文字行被拆分翻译

### 5. 段落对齐准确性提升 ✅（核心已完成）
- [x] `_optimal_alignment` DP 中增加"连续匹配奖励"（`CONSECUTIVE_BONUS = 0.08`），鼓励相邻段落连续配对
- [ ] 处理翻译引擎合并段落的情况（1个CN段落对应2个EN段落的 N:M 匹配）
- [x] 对齐失败时的 fallback：降级为简单顺序配对而非留空（匹配率 <50% 时自动降级）

---

## 三、用户体验优化（中优先级）

### 6. HTML 报告交互增强 ✅
- [x] 邮件搜索/过滤功能：按作者、关键词、优先级筛选
- [x] 全局目录/大纲：悬浮在侧边栏，点击跳转到对应线程
- [x] 键盘快捷键：j/k 上下浏览邮件，Enter 展开/折叠
- [x] 暗色/亮色主题切换

### 7. 翻译进度可视化
- [ ] 翻译过程中实时显示进度条（tqdm 或自制）
- [ ] 显示预估剩余时间（基于已翻译速率）
- [ ] 翻译失败的邮件汇总提示

### 8. 命令行体验改进
- [ ] `config.json` 支持持久化配置（默认后端、API key、并发数等）
- [ ] `--resume` 参数：中断后从上次位置继续翻译
- [ ] `--preview` 参数：快速预览前 N 封邮件的翻译效果

---

## 四、代码质量优化（中优先级）

### 9. translate_context.py 拆分重构
- [ ] 当前 2077 行单文件 → 拆分为模块：
  - `translate_context/parser.py`（解析 context_full.txt）
  - `translate_context/translator_pipeline.py`（翻译流水线）
  - `translate_context/html_renderer.py`（HTML 模板 + 渲染）
  - `translate_context/alignment.py`（段落对齐 DP 算法）
- [ ] 提取 `_HTML_TEMPLATE` 到独立 `.html` 模板文件

### 10. 测试覆盖（部分完成）
- [x] `_is_code_or_data_line()` 回归测试（`tests/test_code_detection.py`，60+ 样本）
- [ ] 为其他核心函数添加 pytest 单元测试：
  - `_is_untranslatable()`
  - `_build_thread_tree()` 线程树构建
  - `_optimal_alignment()` 段落对齐
  - `_split_body_and_diff()` diff 分离
- [ ] 集成测试：用 `doc/example_output/` 做端到端回归

---

## 五、功能扩展（低优先级）

### 11. 多 commit 批量处理
- [ ] 支持一次传入多个 commit hash，批量生成报告
- [ ] 生成索引页汇总多个 commit 的报告链接

### 12. 输出格式扩展
- [ ] PDF 导出（基于 HTML → weasyprint/wkhtmltopdf）
- [ ] Markdown 格式恢复（当前 `--format md` 实际仍输出 HTML）

### 13. Lore 抓取鲁棒性
- [ ] Anubis PoW 求解超时后自动降级到 raw 单封抓取
- [ ] 支持代理配置（`HTTP_PROXY`/`HTTPS_PROXY`）
- [ ] mbox 解析失败的容错处理（跳过损坏邮件，不中断整个线程）

---

## 六、新增规划

### 14. 多邮件缓存可视化阅读器（PLAN-008）✅
- [x] `build_dashboard.py` 数据收集与元数据提取
- [x] Dashboard HTML 生成（自包含单文件，内嵌 JSON + JS 交互）
- [x] 集成到 main.py / translate_context.py / pack_for_openclaw.py 工作流

### 15. 分析报告质量提升（PLAN-009）
- [ ] 模板结构增强：Patchset 全景、相关 Commit 索引、讨论点扩展
- [ ] Prompt 优化：patchset 全局视角、maintainer 讨论深度、关联 commit 追溯
- [ ] 性能数据结构化输出

---

## 推荐执行顺序

| 优先级 | 任务 | 状态 | 预计工作量 |
|--------|------|------|-----------|
| P0 | 1. 翻译并发加速 | ✅ 已完成 | - |
| P0 | 2. 翻译缓存机制 | ✅ 已完成 | - |
| P0 | 4. 代码行误翻译防护 | ✅ 核心完成 | - |
| P0 | 5. 段落对齐准确性 | ✅ 核心完成 | - |
| P1 | 15. 分析报告质量提升 | 🔲 待开始 | 1-2d |
| P1 | 7. 翻译进度可视化 | 🔲 待开始 | 0.5d |
| P1 | 3. 增量翻译支持 | 🔲 待开始 | 1d |
| P2 | 6. HTML 报告交互增强 | ✅ 已完成 | - |
| P2 | 9. translate_context.py 拆分重构 | 🔲 待开始 | 2d |
| P2 | 14. Dashboard 阅读器 | ✅ 已完成 | - |
| P3 | 8/10/11/12/13 其余优化 | 🔲 待开始 | 各0.5-1d |