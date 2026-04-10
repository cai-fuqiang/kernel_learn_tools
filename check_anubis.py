import requests, re

url = 'https://lore.kernel.org/all/20230531124604.274010996@infradead.org/t.mbox.gz'
sess = requests.Session()
sess.headers['User-Agent'] = 'b4/0.14.2'
resp = sess.get(url, timeout=15)

text = resp.text
# 找到所有 script src
for m in re.finditer(r'<script[^>]+src=["\']([^"\']+)["\']', text):
    print("Script src:", m.group(1))

# 找到所有链接中包含 anubis API
for m in re.finditer(r'href=["\']([^"\']*anubis[^"\']*)["\']', text):
    print("Anubis href:", m.group(1))

for m in re.finditer(r'["\']([^"\']*\.within\.website[^"\']*)["\']', text):
    print("API URL:", m.group(1)[:100])

print("\n所有外部 JS 链接:")
for m in re.finditer(r'src=["\']([^"\']+\.js[^"\']*)["\']', text):
    print(" ", m.group(1))