#!/bin/bash
# 并行采集功能使用示例

echo "=== 并行采集功能使用示例 ==="

# 1. 基本使用 - 方案B（话题配置驱动）
echo "1. 基本使用 - 方案B（话题配置驱动）"
echo "python batch_collect.py \\"
echo "  --topic-config topics/sched_latency.json \\"
echo "  --date-from 2006-01-01 --date-to 2010-12-31 \\"
echo "  --max-threads 100"
echo

# 2. 基本使用 - 方案C（纯AI精筛）
echo "2. 基本使用 - 方案C（纯AI精筛）"
echo "python batch_collect.py \\"
echo "  --keywords \"fair sleeper,latency nice,SCHED_DEADLINE\" \\"
echo "  --date-from 2006-01-01 --date-to 2010-12-31 \\"
echo "  --max-threads 100 --ai-only"
echo

# 3. 断点续传
echo "3. 断点续传"
echo "python batch_collect.py \\"
echo "  --topic-config topics/sched_latency.json \\"
echo "  --date-from 2006-01-01 --date-to 2010-12-31 \\"
echo "  --resume"
echo

# 4. 24小时持续采集
echo "4. 24小时持续采集"
echo "python batch_collect.py \\"
echo "  --topic-config topics/sched_latency.json \\"
echo "  --date-from 2006-01-01 --date-to 2024-12-31 \\"
echo "  --continuous"
echo

# 5. 增加并发数
echo "5. 增加并发数"
echo "python batch_collect.py \\"
echo "  --topic-config topics/sched_latency.json \\"
echo "  --date-from 2006-01-01 --date-to 2010-12-31 \\"
echo "  --workers 8 --max-threads 200"
echo

# 6. 跳过AI精筛（调试用）
echo "6. 跳过AI精筛（调试用）"
echo "python batch_collect.py \\"
echo "  --topic-config topics/sched_latency.json \\"
echo "  --date-from 2006-01-01 --date-to 2010-12-31 \\"
echo "  --no-ai"
echo

echo "=== 监控和调试 ==="
echo "# 查看队列状态（在Python中）"
echo "from email_translator.knowledge_db import KnowledgeDB"
echo "db = KnowledgeDB()"
echo "stats = db.get_queue_stats(job_id)"
echo "print(stats)"
echo

echo "# 查看数据库统计"
echo "python query_kb.py"
echo

echo "=== 故障恢复 ==="
echo "# 如果进程被强制终止，只需重新运行相同的命令加上 --resume 参数"
echo "# 系统会自动从队列中恢复未完成的任务"