#!/usr/bin/env python3
"""直接检查 lore HTTP 响应，排查搜索为空的原因"""
import urllib.request

url = "https://lore.kernel.org/all/?q=EEVDF&x=A&o=0"
print(f"请求: {url}")

req = urllib.request.Request(url, headers={
    "Accept": "text/plain, application/mbox",
    "User-Agent": "Mozilla/5.0 (lkml-knowledge-extractor/2.0)",
})

try:
    with urllib.request.urlopen(req, timeout=30) as resp:
        status = resp.status
        ct = resp.headers.get("Content-Type", "")
        body = resp.read().decode("utf-8", errors="replace")
        print(f"Status: {status}")
        print(f"Content-Type: {ct}")
        print(f"Body length: {len(body)} chars")
        print(f"Body preview (first 1500 chars):")
        print(body[:1500])
except urllib.error.HTTPError as e:
    print(f"HTTPError: {e.code} {e.reason}")
    print(e.read().decode("utf-8", errors="replace")[:1000])
except Exception as e:
    print(f"Error: {type(e).__name__}: {e}")