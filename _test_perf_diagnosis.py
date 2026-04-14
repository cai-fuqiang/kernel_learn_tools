#!/usr/bin/env python3
"""诊断翻译 CPU 密集性能瓶颈"""
import time
import sys
import sqlite3
import os

sys.path.insert(0, os.path.dirname(__file__))

from translate_context import (
    should_translate, _split_body_and_diff, _is_code_or_data_line,
    _is_untranslatable, translate_body_aligned,
)


def get_emails_from_thread(thread_id):
    """从 knowledge.db 获取线程邮件"""
    db_path = os.path.join("data", "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT message_id, subject, body FROM emails WHERE thread_id = ?",
        (thread_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def find_large_thread():
    """找邮件数接近 92 的线程"""
    db_path = os.path.join("data", "knowledge.db")
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("""
        SELECT t.id, t.subject, t.email_count
        FROM threads t
        ORDER BY t.email_count DESC
        LIMIT 10
    """).fetchall()
    conn.close()
    for r in rows:
        print(f"  {r['email_count']:4d} 封 | {r['id'][:30]}... | {r['subject'][:60]}")
    return [dict(r) for r in rows]


def benchmark_should_translate(emails):
    """测试 should_translate 性能"""
    print(f"\n=== should_translate 性能 ({len(emails)} 封邮件) ===")
    t0 = time.time()
    results = []
    for i, em in enumerate(emails):
        body = em.get("body", "")
        t1 = time.time()
        result = should_translate(body)
        elapsed = time.time() - t1
        results.append((i, result, elapsed, len(body)))
        if elapsed > 0.1:
            print(f"  !! 慢: email[{i}] {elapsed:.3f}s body长度={len(body)}")
    total = time.time() - t0
    print(f"  总耗时: {total:.3f}s, 平均: {total/len(emails):.3f}s/封")

    # 找最慢的 5 封
    results.sort(key=lambda x: x[2], reverse=True)
    print("  Top5 最慢:")
    for i, result, elapsed, blen in results[:5]:
        print(f"    email[{i}] {elapsed:.3f}s body={blen}字符 translate={result}")


def benchmark_split_body_and_diff(emails):
    """测试 _split_body_and_diff 性能"""
    print(f"\n=== _split_body_and_diff 性能 ({len(emails)} 封邮件) ===")
    t0 = time.time()
    results = []
    for i, em in enumerate(emails):
        body = em.get("body", "")
        t1 = time.time()
        text, diff = _split_body_and_diff(body)
        elapsed = time.time() - t1
        results.append((i, elapsed, len(body), len(text), len(diff)))
        if elapsed > 0.1:
            print(f"  !! 慢: email[{i}] {elapsed:.3f}s body={len(body)} text={len(text)} diff={len(diff)}")
    total = time.time() - t0
    print(f"  总耗时: {total:.3f}s, 平均: {total/len(emails):.3f}s/封")
    results.sort(key=lambda x: x[1], reverse=True)
    print("  Top5 最慢:")
    for i, elapsed, blen, tlen, dlen in results[:5]:
        print(f"    email[{i}] {elapsed:.3f}s body={blen} text={tlen} diff={dlen}")


def benchmark_is_code_or_data(emails):
    """测试 _is_code_or_data_line 在全部邮件行上的性能"""
    print(f"\n=== _is_code_or_data_line 性能 ===")
    all_lines = []
    for em in emails:
        body = em.get("body", "")
        all_lines.extend(body.splitlines())
    print(f"  总行数: {len(all_lines)}")
    t0 = time.time()
    for line in all_lines:
        _is_code_or_data_line(line)
    total = time.time() - t0
    print(f"  总耗时: {total:.3f}s, 平均: {total/max(len(all_lines),1)*1000:.3f}ms/行")


def benchmark_translate_body_aligned_dry(emails):
    """测试 translate_body_aligned 的段落处理（不实际翻译），用 mock 翻译器"""
    print(f"\n=== translate_body_aligned (mock翻译) 性能 ===")

    class MockTranslator:
        def translate_email(self, email_dict):
            return {"body_cn": "模拟翻译结果"}

    mock = MockTranslator()
    t0 = time.time()
    results = []
    for i, em in enumerate(emails):
        body = em.get("body", "")
        if not should_translate(body):
            continue
        t1 = time.time()
        aligned = translate_body_aligned(mock, body)
        elapsed = time.time() - t1
        results.append((i, elapsed, len(body), len(aligned)))
        if elapsed > 0.5:
            print(f"  !! 慢: email[{i}] {elapsed:.3f}s body={len(body)} 段落={len(aligned)}")
    total = time.time() - t0
    print(f"  总耗时: {total:.3f}s (含 should_translate 过滤)")
    results.sort(key=lambda x: x[1], reverse=True)
    print("  Top5 最慢:")
    for i, elapsed, blen, pcount in results[:5]:
        print(f"    email[{i}] {elapsed:.3f}s body={blen} 段落={pcount}")


if __name__ == "__main__":
    print("=== 查找大线程 ===")
    threads = find_large_thread()
    if not threads:
        print("没有线程数据！")
        sys.exit(1)

    # 使用最大线程
    tid = threads[0]["id"]
    count = threads[0]["email_count"]
    print(f"\n使用线程: {tid[:50]}... ({count} 封)")

    emails = get_emails_from_thread(tid)
    print(f"实际获取: {len(emails)} 封邮件")
    if not emails:
        print("无邮件！")
        sys.exit(1)

    # 性能基准
    benchmark_split_body_and_diff(emails)
    benchmark_should_translate(emails)
    benchmark_is_code_or_data(emails)
    benchmark_translate_body_aligned_dry(emails)

    print("\n=== 完成 ===")