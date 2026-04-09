# LKML 内核邮件列表知识提取工具 - 重新规划

## 需求背景
用户想学习 Linux 内核某个模块的代码（如调度器 FAIR_SLEEPING），但：
- 大量有价值的设计讨论散布在内核邮件列表中
- 邮件列表庞大、内容繁杂，信息获取效率低
- 英语阅读困难，需要翻译辅助

**核心目标：从海量 LKML 邮件中精准提取某个内核话题的技术讨论，翻译后生成结构化的学习资料。**

## 当前项目现状
- [x] lkml_client.py - 按日期遍历 lkml.org，主题关键词匹配 + 多关键词 AND/OR 组合
- [x] lore_client.py - lore.kernel.org 全文搜索（mbox 解析）
- [x] translator.py - 4种翻译后端（google/youdao/api/openclaw）
- [x] output_generator.py - 中英文对照 Markdown 生成 + 总览统计 + 代码关联
- [x] thread_builder.py - Message-ID/In-Reply-To/References 线程构建
- [x] summarizer.py - OpenClaw 智能摘要 + 交互式问答
- [x] code_analyzer.py - 代码关联分析（提取 patch/diff/函数名）
- [x] main.py - 完整命令行入口

## 改进方案

### TODO 1: 增加高效搜索源 ✅
- [x] 接入 lore.kernel.org 全文搜索 API（支持正文搜索，非仅标题）
- [x] 保留 lkml.org 作为备选源
- [x] 搜索模块抽象为接口，方便扩展新数据源

### TODO 2: 支持邮件线程（Thread） ✅
- [x] 解析 Message-ID / In-Reply-To / References 头构建回复链
- [x] 按线程分组展示（一个讨论主题 = 一组关联邮件）
- [x] 线程内按时间排序，展示完整讨论脉络
- [x] 输出时保留线程层级关系

### TODO 3: 改进搜索策略 ✅
- [x] 支持正文全文搜索（利用 lore.kernel.org 能力）
- [x] 支持多关键词组合（"sched AND fair", "CFS OR vruntime"）
- [x] 增加 --list 参数指定子列表（如 linux-kernel, linux-mm 等）

### TODO 4: 优化输出格式 ✅
- [x] 按线程分组的 Markdown 报告
- [x] 邮件列表总览统计（共N个线程/M封邮件/主要参与者）
- [x] 增加源链接方便跳转原文
- [x] 代码关联分析（涉及的源码文件/函数/Patch 邮件统计）

### TODO 5: main.py 参数优化 ✅
- [x] --source 参数选择数据源 (lore/lkml)
- [x] --list 指定邮件子列表 (linux-kernel, linux-mm 等)
- [x] --interactive 交互式问答模式
- [x] --code-analysis / --kernel-src 代码关联分析
- [x] 保留现有参数 (topic/days/date-from/date-to/author/backend/max)

### TODO 6: OpenClaw 深度配合

**阶段一：智能摘要 ✅**
- [x] --summarize 参数：将翻译后的线程喂给 OpenClaw，生成摘要
- [x] 输出结构：线程摘要 + 逐封翻译详情

**阶段二：知识问答 ✅**
- [x] --interactive 参数：将搜索到的邮件注入 OpenClaw 记忆
- [x] 用户可以追问邮件内容相关问题
- [x] 实现方式：将邮件内容写入临时文件 → openclaw chat --file

**阶段三：代码关联 ✅**
- [x] 从邮件中自动提取 patch/diff 中的文件路径
- [x] 如果本地有内核源码，验证文件是否存在
- [x] 生成代码关联分析报告

## 使用示例
```bash
# 基础：搜索 + 翻译
python main.py --topic "sched/fair" --source lkml --max-emails 5 --translator google

# 简化版报告（无翻译）
python main.py --topic "sched/fair" --source lkml --max-emails 5 --simple

# 多关键词组合搜索
python main.py --topic "sched AND fair" --source lkml --max-emails 10

# 进阶：搜索 + 翻译 + OpenClaw 智能摘要
python main.py --topic "FAIR_SLEEPING" --summarize --translator google

# 代码关联分析
python main.py --topic "sched/fair" --source lkml --code-analysis --kernel-src /path/to/linux

# 高级：搜索后进入 OpenClaw 交互问答
python main.py --topic "FAIR_SLEEPING" --source lkml --interactive

# 完整流程：搜索 + 翻译 + 代码分析 + 保存邮件
python main.py --topic "scheduler vruntime" --author "Peter Zijlstra" --code-analysis --save-emails
```

## 技术要点
- lore.kernel.org 提供 mbox 格式搜索结果（注意 Anubis 反爬虫验证）
- lkml.org 作为备选源，通过 HTML 解析获取邮件
- 邮件线程关系通过 Message-ID / In-Reply-To / References 三个头字段构建
- OpenClaw 通过 `openclaw chat --prompt` 或 `--file` 调用
- 继续使用纯标准库（urllib/email/html.parser），不引入第三方依赖
- 代码关联通过正则从 diff/patch 格式中提取文件路径和函数名