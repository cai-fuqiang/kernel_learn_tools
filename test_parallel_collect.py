#!/usr/bin/env python3
"""测试并行采集功能的简单脚本"""

import sys
from pathlib import Path

# 添加到路径
sys.path.insert(0, str(Path(__file__).parent))

from email_translator.knowledge_db import KnowledgeDB

def test_queue_operations():
    """测试采集队列操作"""
    print("测试采集队列功能...")
    
    db = KnowledgeDB(":memory:")  # 使用内存数据库测试
    
    # 创建测试任务
    job_id = db.create_job(
        keywords="test,scheduling",
        date_from="2024-01-01",
        date_to="2024-12-31",
        list_name="all"
    )
    print(f"创建测试任务: {job_id}")
    
    # 测试添加队列项目
    test_email = {
        "message_id": "<test123@example.com>",
        "subject": "Test Thread Subject",
        "source_url": "https://lore.kernel.org/test/123/",
        "relevance_score": 0.9,
        "relevance_reason": "test relevance"
    }
    
    success = db.add_to_queue(job_id, test_email, priority=1)
    print(f"添加队列项目: {success}")
    
    # 测试获取队列项目
    queue_items = db.get_queue_items(job_id)
    print(f"队列项目数量: {len(queue_items)}")
    
    if queue_items:
        item = queue_items[0]
        print(f"队列项目详情: {item}")
        
        # 测试更新状态
        db.update_queue_status(item["id"], "downloading")
        updated_items = db.get_queue_items(job_id, status="downloading")
        print(f"下载中项目数量: {len(updated_items)}")
        
        # 测试获取统计
        stats = db.get_queue_stats(job_id)
        print(f"队列统计: {stats}")
    
    print("测试完成！")

if __name__ == "__main__":
    test_queue_operations()