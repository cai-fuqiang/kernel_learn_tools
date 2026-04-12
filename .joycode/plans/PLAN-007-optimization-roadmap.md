# PLAN-007: 项目优化路线图

## 现状概述
项目已完成核心功能：commit 驱动的 Lore 邮件抓取 → 预处理/分级 → 翻译 → HTML 线程树报告（v2 双栏对比布局 + 聚焦视图）。以下是按优先级排列的优化方向。

---

## 一、性能与效率优化（高优先级）

### 1. 翻译并发加速
- [ ] translate_context.py 当前逐封串行翻译，改为 `concurrent.futures.ThreadPoolExecutor` 并发
- [ ] Google/有道后端支持并发 3~5 路（加 rate-limit 保护）
- [ ] API 后端支持并发（用户可配置并发数）
- [ ] 预估收益：100 封邮件从 ~5min 缩短到 ~1.5min

### 2. 翻译缓存机制
- [ ] 基于 body hash 的本地翻译缓存（JSON/SQLite），避免重复翻译
- [ ] 缓存 key = sha256(backend + body_text)，value = translated_text
- [ ] `--no-cache` 参数强制刷新
- [ ] 预估收益：二次运行跳过已翻译内容，节省 80%+ 时间和 API 费用

### 3. 增量翻译支持
- [ ] 检测已有 `_translated.html` 输出，只翻译新增/变更的邮件
- [ ] 合并旧翻译结果 + 新翻译，避免全量重做

---

## 二、翻译质量优化（高优先级）

### 4. 代码/数据行误翻译防护加强
- [ ] `_is_code_or_data_line()` 增加内核常见模式：`dmesg` 输出、`ftrace` 输出、`perf report` 格式
- [ ] 增加单元测试：收集 10+ 真实误翻译样本作为回归测试用例
- [ ] 合并连续代码行为代码块后整体跳过，避免中间夹杂文字行被拆分翻译

### 5. 段落对齐准确性提升
- [ ] `_optimal_alignment` DP 中增加"连续匹配奖励"，鼓励相邻段落连续配对
- [ ] 处理翻译引擎合并段落的情况（1个CN段落对应2个EN段落的 N:M 匹配）
- [ ] 对齐失败时的 fallback：降级为简单顺序配对而非留空

---

## 三、用户体验优化（中优先级）

### 6. HTML 报告交互增强
- [ ] 邮件搜索/过滤功能：按作者、关键词、优先级筛选
- [ ] 全局目录/大纲：悬浮在侧边栏，点击跳转到对应线程
- [ ] 键盘快捷键：j/k 上下浏览邮件，Enter 展开/折叠
- [ ] 暗色/亮色主题切换

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
- [ ] 当前 1800 行单文件 → 拆分为模块：
  - `translate_context/parser.py`（解析 context_full.txt）
  - `translate_context/translator_pipeline.py`（翻译流水线）
  - `translate_context/html_renderer.py`（HTML 模板 + 渲染）
  - `translate_context/alignment.py`（段落对齐 DP 算法）
- [ ] 提取 `_HTML_TEMPLATE` 到独立 `.html` 模板文件

### 10. 测试覆盖
- [ ] 为核心函数添加 pytest 单元测试：
  - `_is_code_or_data_line()` / `_is_untranslatable()`
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