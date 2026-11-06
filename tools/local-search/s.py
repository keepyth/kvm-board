#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""本地多引擎搜索（无需 API key）。给 PicoClaw 兜底用。

用法:
  python3 /opt/search/s.py "关键词" [条数]
输出:
  纯文本，每条 3 行：标题 / URL / 摘要
退出码:
  0 有结果；2 全部引擎失败
"""
import gzip
import html
import re
import sys
import time
import urllib.parse
import urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/131.0.0.0 Safari/537.36")
TAG = re.compile(r"<[^>]+>")
WS = re.compile(r"\s+")


def fetch(url, timeout=8):
    req = urllib.request.Request(url, headers={
        "User-Agent": UA,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.6",
        "Accept-Encoding": "gzip",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        data = r.read()
        if (r.headers.get("Content-Encoding") or "").lower() == "gzip":
            data = gzip.decompress(data)
        enc = "utf-8"
        ct = r.headers.get("Content-Type") or ""
        m = re.search(r"charset=([\w-]+)", ct, re.I)
        if m:
            enc = m.group(1)
        return data.decode(enc, "replace")


def clean(s, limit=220):
    s = TAG.sub(" ", s or "")
    s = html.unescape(s)
    s = WS.sub(" ", s).strip()
    return s[:limit]


def abs_url(u):
    if u.startswith("//"):
        return "https:" + u
    return u


def bing(q, n):
    url = "https://cn.bing.com/search?q=%s&setlang=zh-CN" % urllib.parse.quote(q)
    h = fetch(url)
    out = []
    for blk in re.findall(r'<li class="b_algo".*?</li>', h, re.S)[:n]:
        m = re.search(r'<h2[^>]*>\s*<a[^>]*href="([^"]+)"[^>]*>(.*?)</a>', blk, re.S)
        if not m:
            continue
        link = abs_url(m.group(1))
        title = clean(m.group(2), 120)
        p = re.search(r"<p[^>]*>(.*?)</p>", blk, re.S)
        desc = clean(p.group(1)) if p else ""
        if title:
            out.append((title, link, desc))
    return out


def so360(q, n):
    url = "https://www.so.com/s?q=%s" % urllib.parse.quote(q)
    h = fetch(url)
    out = []
    for blk in re.findall(r'<li class="res-list.*?</li>', h, re.S)[:n * 2]:
        m = re.search(r'data-mdurl="(https?://[^"]+)"', blk)
        t = re.search(r"<h3[^>]*>(.*?)</h3>", blk, re.S)
        if not t:
            continue
        title = clean(t.group(1), 120)
        link = m.group(1) if m else ""
        if not link:
            a = re.search(r'href="(https?://[^"]+)"', blk)
            link = a.group(1) if a else ""
        p = re.search(r'<p[^>]*class="res-desc"[^>]*>(.*?)</p>', blk, re.S) \
            or re.search(r"<p[^>]*>(.*?)</p>", blk, re.S)
        desc = clean(p.group(1)) if p else ""
        if title and link:
            out.append((title, link, desc))
    return out


ENGINES = [("bing", bing), ("360", so360)]


def run(q, n=8):
    merged, errs = [], []
    for name, fn in ENGINES:
        got = []
        for attempt in (1, 2):
            try:
                got = fn(q, n)
            except Exception as e:  # noqa: BLE001
                errs.append("%s#%d:%s" % (name, attempt, type(e).__name__))
                time.sleep(0.5)
                continue
            break
        merged.extend(got)
        if len(merged) >= n:
            break
    seen, out = set(), []
    for t, u, d in merged:
        k = u.split("?")[0]
        if k in seen:
            continue
        seen.add(k)
        out.append((t, u, d))
        if len(out) >= n:
            break
    return out, errs


def main():
    if len(sys.argv) < 2:
        print("用法: s.py \"关键词\" [条数]")
        return 64
    q = sys.argv[1]
    n = int(sys.argv[2]) if len(sys.argv) > 2 else 8
    res, errs = run(q, n)
    if not res:
        print("SEARCH_FAILED 所有引擎均不可用: %s" % (", ".join(errs) or "empty"))
        return 2
    print("搜索「%s」共 %d 条：" % (q, len(res)))
    for i, (t, u, d) in enumerate(res, 1):
        print("%d. %s" % (i, t))
        print("   %s" % u)
        if d:
            print("   %s" % d)
    return 0


if __name__ == "__main__":
    sys.exit(main())
