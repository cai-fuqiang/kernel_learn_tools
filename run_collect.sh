#!/bin/bash
# 采集 2017上半年 sched fair / sleep latency 相关内核邮件讨论
cd "$(dirname "$0")"

WORKERS=${1:-8}  # 默认8并发，建议不超过16

echo "=== 开始采集 (workers=$WORKERS) ==="
python3 batch_collect.py \
  --keywords 'sched fair,fair sleep,sleep latency' \
  --date-from 2017-01-01 \
  --date-to 2017-07-01 \
  --list linux-kernel \
  --max-emails 100 \
  --workers "$WORKERS" \
  --no-ai

echo ""
echo "=== 采集完成，查看知识库统计 ==="
python3 -c "
from email_translator.knowledge_db import KnowledgeDB
db = KnowledgeDB()
s = db.stats()
print(f'邮件: {s[\"emails\"]} 封')
print(f'线程: {s[\"threads\"]} 个')
print(f'报告: {s[\"reports\"]} 篇')
"
