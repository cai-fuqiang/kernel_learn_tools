# 并行采集功能说明

## 概述

我们实现了搜索和下载入库的并行处理架构，解决了以下问题：

1. **进程终止导致数据丢失**：通过持久化队列存储待下载的线程信息
2. **搜索和下载解耦**：采用生产者-消费者模式，搜索完成后立即保存结果
3. **超时保护**：为LoreClient搜索添加10分钟超时机制
4. **断点续传**：支持从上次中断的位置继续采集
5. **24小时持续采集**：支持循环运行，自动处理网络中断

## 架构设计

### 生产者-消费者模式

```
[搜索阶段] → [持久化队列] → [下载阶段]
   ↑              ↓              ↓
  Lore搜索    collect_queue    线程下载入库
```

### 数据库表结构

1. **collect_jobs**：采集任务记录，新增`last_search_time`字段用于断点续传
2. **collect_queue**：采集队列，存储待下载的线程信息，包含状态管理

### 队列状态

- `pending`：待下载
- `downloading`：下载中
- `completed`：已完成
- `failed`：失败（支持重试）

## 使用方式

### 1. 基本使用

```bash
# 方案B：话题配置驱动
python batch_collect.py \
  --topic-config topics/sched_latency.json \
  --date-from 2006-01-01 --date-to 2010-12-31 \
  --max-threads 100

# 方案C：纯AI精筛
python batch_collect.py \
  --keywords "fair sleeper,latency nice,SCHED_DEADLINE" \
  --date-from 2006-01-01 --date-to 2010-12-31 \
  --max-threads 100 --ai-only
```

### 2. 断点续传

```bash
# 继续未完成的采集任务
python batch_collect.py \
  --topic-config topics/sched_latency.json \
  --date-from 2006-01-01 --date-to 2010-12-31 \
  --resume
```

### 3. 24小时持续采集

```bash
# 持续运行，直到达到日期范围末尾
python batch_collect.py \
  --topic-config topics/sched_latency.json \
  --date-from 2006-01-01 --date-to 2024-12-31 \
  --continuous
```

## 技术特性

### 超时保护

- LoreClient搜索：10分钟超时
- 线程下载：5分钟超时
- 单个线程处理：30秒超时

### 错误处理

- 自动重试失败的下载（最多3次）
- 网络异常时优雅降级
- 详细的日志记录

### 性能优化

- 并发搜索和下载
- 数据库连接池
- 内存优化

## 监控和调试

### 查看队列状态

```bash
# 在Python中查询队列状态
from email_translator.knowledge_db import KnowledgeDB
db = KnowledgeDB()
stats = db.get_queue_stats(job_id)
print(stats)  # {'pending': 10, 'downloading': 2, 'completed': 50, 'failed': 3, 'total': 65}
```

### 日志级别

- INFO：基本进度信息
- WARNING：可恢复的错误
- ERROR：需要关注的异常

## 故障恢复

### 进程被强制终止

1. 重新运行相同的命令加上`--resume`参数
2. 系统会自动从队列中恢复未完成的任务
3. 已完成的任务不会重复处理

### 网络中断

1. 失败的队列项目会自动标记为`failed`
2. 系统会定期重试失败的项目（最多3次）
3. 重试间隔使用指数退避策略

## 扩展性

### 增加并发数

```bash
python batch_collect.py --workers 8  # 增加到8个并发工作进程
```

### 自定义超时

```bash
# 在代码中调整超时参数
# LoreClient(timeout=30)  # 30秒超时
# LoreThreadFetcher(timeout=60)  # 60秒超时
```

## 注意事项

1. **数据库锁定**：SQLite在高并发下可能有性能瓶颈，必要时可切换到PostgreSQL
2. **磁盘空间**：确保有足够的磁盘空间存储邮件数据
3. **网络带宽**：大量并发下载可能占用较多带宽
4. **API限制**：注意lore.kernel.org的访问频率限制

## 未来改进

1. **分布式采集**：支持多台机器协同采集
2. **优先级调度**：根据相关性分数动态调整下载优先级
3. **实时监控**：Web界面查看采集进度和状态
4. **智能重试**：根据错误类型调整重试策略