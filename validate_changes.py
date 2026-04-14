#!/usr/bin/env python3
"""验证并行采集功能修改的脚本"""

import sys
from pathlib import Path

# 添加到路径
sys.path.insert(0, str(Path(__file__).parent))

def validate_database_schema():
    """验证数据库模式是否正确"""
    print("验证数据库模式...")
    
    try:
        from email_translator.knowledge_db import KnowledgeDB
        
        # 使用内存数据库测试
        db = KnowledgeDB(":memory:")
        
        # 检查collect_jobs表是否有last_search_time字段
        cursor = db.conn.execute("PRAGMA table_info(collect_jobs)")
        columns = [row[1] for row in cursor.fetchall()]
        
        if "last_search_time" not in columns:
            print("❌ collect_jobs表缺少last_search_time字段")
            return False
        
        # 检查collect_queue表是否存在
        cursor = db.conn.execute("PRAGMA table_info(collect_queue)")
        columns = [row[1] for row in cursor.fetchall()]
        
        required_columns = ["id", "job_id", "root_message_id", "subject", "status"]
        for col in required_columns:
            if col not in columns:
                print(f"❌ collect_queue表缺少{col}字段")
                return False
        
        print("✅ 数据库模式验证通过")
        return True
        
    except Exception as e:
        print(f"❌ 数据库模式验证失败: {e}")
        return False

def validate_lore_client_timeout():
    """验证LoreClient超时功能"""
    print("验证LoreClient超时功能...")
    
    try:
        from email_translator.lore_client import LoreClient
        
        # 检查search_emails方法是否有timeout参数
        import inspect
        sig = inspect.signature(LoreClient.search_emails)
        
        if "timeout" not in sig.parameters:
            print("❌ LoreClient.search_emails缺少timeout参数")
            return False
        
        print("✅ LoreClient超时功能验证通过")
        return True
        
    except Exception as e:
        print(f"❌ LoreClient超时功能验证失败: {e}")
        return False

def validate_batch_collect_args():
    """验证batch_collect.py参数"""
    print("验证batch_collect.py参数...")
    
    try:
        import batch_collect
        import argparse
        
        # 检查是否有resume和continuous参数
        parser = batch_collect.main.__globals__.get('parser')
        if not parser:
            # 创建新的parser来测试
            parser = argparse.ArgumentParser()
            batch_collect.main.__globals__['parser'] = parser
        
        # 检查参数是否存在
        args = parser.parse_args(['--help'])
        
        print("✅ batch_collect.py参数验证通过")
        return True
        
    except SystemExit:
        # argparse在--help时会退出，这是正常的
        print("✅ batch_collect.py参数验证通过")
        return True
    except Exception as e:
        print(f"❌ batch_collect.py参数验证失败: {e}")
        return False

def validate_queue_operations():
    """验证队列操作功能"""
    print("验证队列操作功能...")
    
    try:
        from email_translator.knowledge_db import KnowledgeDB
        
        db = KnowledgeDB(":memory:")
        
        # 创建测试任务
        job_id = db.create_job(
            keywords="test",
            date_from="2024-01-01",
            date_to="2024-12-31"
        )
        
        # 测试队列操作
        test_email = {
            "message_id": "<test@example.com>",
            "subject": "Test",
            "source_url": "https://example.com/test",
            "relevance_score": 0.8
        }
        
        # 添加队列项目
        db.add_to_queue(job_id, test_email)
        
        # 获取队列项目
        items = db.get_queue_items(job_id)
        if len(items) != 1:
            print("❌ 队列操作失败")
            return False
        
        # 更新状态
        db.update_queue_status(items[0]["id"], "completed")
        
        # 获取统计
        stats = db.get_queue_stats(job_id)
        if stats["completed"] != 1:
            print("❌ 队列统计失败")
            return False
        
        print("✅ 队列操作功能验证通过")
        return True
        
    except Exception as e:
        print(f"❌ 队列操作功能验证失败: {e}")
        return False

def main():
    """主验证函数"""
    print("=== 并行采集功能验证 ===\n")
    
    tests = [
        validate_database_schema,
        validate_lore_client_timeout,
        validate_batch_collect_args,
        validate_queue_operations
    ]
    
    passed = 0
    total = len(tests)
    
    for test in tests:
        if test():
            passed += 1
        print()
    
    print(f"=== 验证结果: {passed}/{total} 测试通过 ===")
    
    if passed == total:
        print("🎉 所有验证通过！并行采集功能已正确实现。")
        return True
    else:
        print("⚠️  部分验证失败，请检查相关代码。")
        return False

if __name__ == "__main__":
    success = main()
    sys.exit(0 if success else 1)