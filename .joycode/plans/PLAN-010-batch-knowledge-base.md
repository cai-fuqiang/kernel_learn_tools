# 内核邮件知识库 - 批量采集+AI筛选+摘要反哺

## 需求
按时间段+子系统批量采集内核邮件，AI判断相关性后入库，生成摘要反哺知识库。
示例场景：2017-2018年 scheduler 延迟敏感/QoS 相关邮件。

## 架构（基于现有代码增量开发，零新依赖）

```
batch_collect.py (新)        knowledge_db.py (新)        batch_process.py (新)
┌──────────────────┐    ┌─────────────────────┐    ┌──────────────────────┐
│ 1.粗筛:Lore搜索  │    │ SQLite 知识库        │    │ 遍历未处理邮件       │
│ 2.规则预筛:关键词 │───▶│ emails/threads/tags  │◀───│ 翻译+摘要+代码分析   │
│ 3.AI精筛:DeepSeek │    │ FTS5全文搜索         │    │ 跨线程综合分析       │
│ 4.下载完整线程    │    │ knowledge_reports    │    │ 反哺入库             │
└──────────────────┘    └─────────────────────┘    └──────────────────────┘
```

## TODO 1: knowledge_db.py — 知识库存储层 ✅
- [x] KnowledgeDB 类，复用 translation_cache.py 的 SQLite 模式
- [ ] emails 表：message_id, subject, from_name, from_email, date, body, list_name, thread_id, in_reply_to, priority, relevance_score, raw_json_path
- [ ] threads 表：id, root_message_id, subject, start_date, end_date, email_count, summary_zh, key_points, consensus, design_decisions, related_files, tags
- [ ] knowledge_reports 表：id, topic, report_type, content, source_thread_ids, created_at
- [ ] collect_jobs 表：id, keywords, date_from, date_to, list_name, status, created_at（记录采集任务，支持增量）
- [ ] FTS5 虚拟表：email_fts(subject, body, summary)
- [ ] 去重：insert_email() 按 message_id 去重
- [ ] 查询接口：search_fts(), get_thread(), get_unprocessed_emails()

## TODO 2: batch_collect.py — 批量采集脚本 ✅
- [x] CLI 参数：--keywords, --date-from, --date-to, --list, --max-emails, --api-key, --api-provider
- [ ] 第1步-粗筛：复用 LoreClient.search_emails()，按子系统+时间范围拉邮件元数据
- [ ] 第2步-规则预筛：subject/body 关键词匹配过滤（可配置关键词列表）
- [ ] 第3步-AI精筛：复用 translator.py 的 API 调用，DeepSeek 判断相关性 YES/NO
- [ ] 第4步-下载完整线程：精筛通过的邮件，用 LoreThreadFetcher 拉完整线程
- [ ] 第5步-入库：调用 knowledge_db 存入 SQLite + 保存原始 JSON
- [ ] 断点续传：collect_jobs 记录进度，中断后可继续
- [ ] 并发控制：AI精筛可并发（线程池），Lore下载需限速

## TODO 3: batch_process.py — 批量处理+摘要反哺 ✅
- [x] CLI 参数：--topic, --backend, --api-key, --workers, --summarize, --cross-analysis
- [ ] 遍历知识库中未处理的线程
- [ ] 单线程摘要：复用 OpenClawSummarizer 或 API 调用，生成结构化摘要
- [ ] 摘要入库：summary_zh, key_points, consensus, tags 写回 threads 表
- [ ] 翻译：复用现有翻译模块，结果关联到邮件
- [ ] 跨线程综合分析：每 N 个线程打包，AI生成时间线+核心矛盾+主题索引
- [ ] 综合报告入库：存入 knowledge_reports 表
- [ ] 生成 HTML 报告 + 更新 Dashboard

## TODO 4: --export-html 静态知识库页面 ✅
- [x] 在 batch_process.py 中新增 --export-html 选项
- [x] 从 SQLite 读取所有线程摘要 + 综合报告
- [x] 生成单文件自包含 HTML（复用现有 translate_context.py 的暗色/亮色主题风格）
- [x] 页面结构：综合报告区 + 线程摘要卡片列表 + 前端搜索过滤
- [x] 线程卡片展示：subject、时间、参与者、摘要、标签、关键要点
- [x] 综合报告区：跨线程分析的完整内容
- [x] 前端交互：搜索框过滤、按标签筛选、按时间排序
- [x] 输出到 data/output/knowledge_base.html，浏览器直接打开

## TODO 5: 验证
- [ ] 用 "sched latency" + 2017-2018 做端到端测试
- [ ] 验证去重、断点续传、FTS5搜索
- [ ] 验证 HTML 导出页面正常显示

## TODO 6: 采集过滤增强 — 标题黑名单 + AI 精筛清洗
> 目标: 解决当前 1819 线程中大量无关邮件的问题 (drm/Xen/Qemu/[PULL] 等)

- [ ] batch_collect.py 添加标题黑名单机制：
  - 正则匹配跳过: `[PULL]`, `Recent changes`, `[pull]`, `drm-intel-next`
  - 前缀黑名单: `[Qemu-devel]`, `[media]`, `[drm`, `[xen`, `[kernel-hardening]`, `[Bug ` 等与调度无关的子系统
  - 黑名单可配置 (常量列表)，在规则预筛阶段生效，跳过的邮件不下载线程
- [ ] batch_process.py 新增 `--ai-clean` 模式：
  - 对已入库但 relevance_score=0.5 (rule_match_only) 的线程做 AI 精筛
  - AI 判断不相关的线程标记 relevance_score=0 + hidden=1 (软删除)
  - 网页端默认隐藏 hidden=1 的线程，提供"显示全部"开关
- [ ] knowledge_db.py threads 表添加 `hidden` 列 (INTEGER DEFAULT 0)
- [ ] kb_web.py api_threads() 默认过滤 hidden=1

## TODO 7: 话题标签 + AI 摘要 — 知识库核心价值
> 目标: 让每个线程有结构化的话题归属、标签、摘要，支撑话题检索

- [ ] knowledge_db.py 新增 `topics` 表:
  - id, name (如 "fair sleeper"), description, keywords, created_at
  - 一个线程可属于多个话题 → `thread_topics` 关联表 (thread_id, topic_id)
- [ ] batch_process.py `--summarize` 增强:
  - AI 为每个线程生成: summary_zh, key_points, tags
  - AI 同时判断线程所属话题 (从已有 topics 列表中匹配，或建议新话题)
  - 结果写回 threads 表 (summary_zh, key_points, tags) + thread_topics 关联
- [ ] batch_process.py 新增 `--add-topic "fair sleeper"`:
  - 注册话题到 topics 表
  - 可选: 自动扫描现有线程，AI 判断哪些属于该话题
- [ ] 支持从网页端添加/管理话题

## TODO 8: 网页端话题视图 — 知识库交互入口
> 目标: 输入 "fair sleeper" → 直接看到相关线程+摘要

- [ ] 侧边栏改造:
  - Dashboard / Topics(话题) / Threads(全部线程) / Search
  - Topics 页: 展示所有话题卡片 (名称、线程数、描述)
  - 点击话题 → 该话题下的线程列表 (含摘要、标签、翻译状态)
- [ ] Threads 页增强:
  - 顶部筛选栏: 按话题/作者/时间/翻译状态过滤
  - 点击线程可展开查看邮件列表
  - 搜索整合: FTS5 + tags + topic 综合排序
- [ ] API 新增:
  - GET /api/topics — 话题列表
  - GET /api/topics/:id/threads — 话题下的线程
  - POST /api/topics — 创建话题
- [ ] 页面删除功能: 软删除线程/邮件 (hidden=1)，确认对话框

## 已完成
- [x] 网页端翻译按钮 + 翻译对话框 + 后台翻译 + 进度轮询
- [x] batch_process.py 多线程翻译超时保护

## 复用现有模块
| 需求 | 复用模块 |
|------|----------|
| Lore搜索 | lore_client.py LoreClient |
| 完整线程下载 | lore_thread_fetcher.py LoreThreadFetcher |
| 邮件预处理 | email_preprocessor.py |
| 线程构建 | thread_builder.py |
| 翻译 | translator.py + translation_cache.py |
| AI摘要 | summarizer.py OpenClawSummarizer |
| API调用 | translator.py 的 _translate_via_api() |
| 报告生成 | translate_context.py + build_dashboard.py |

## 用法示例
```bash
# 第1步：批量采集
python batch_collect.py \
  --keywords "latency,QoS,deadline,SCHED_DEADLINE,latency-nice,interactive" \
  --date-from 2017-01-01 --date-to 2018-12-31 \
  --list linux-kernel --max-emails 5000 \
  --api-key sk-xxx --api-provider deepseek

# 第2步：批量处理+摘要反哺
python batch_process.py \
  --topic "scheduler latency QoS" \
  --backend api --api-key sk-xxx \
  --summarize --cross-analysis

# 第3步：导出知识库 HTML 页面
python batch_process.py --export-html

# 第4步：查询知识库
python batch_process.py --query "SCHED_DEADLINE 设计决策"

# 查看统计
python batch_process.py --stats
```