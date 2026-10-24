#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成一份**虚构**的浏览历史，配合 demo/bookmarks.json 出演示页面。

真实使用时浏览历史来自浏览器自己，这个脚本只为了让没装扩展的人
也能看到「常用」区块长什么样 —— 数据全部是本地合成的，不读任何真实记录。

用法:
    python demo/build_demo.py                 # 生成 demo/index.html
    python demo/build_demo.py --no-icons      # 不抓图标（离线、体积小）
"""
import argparse
import datetime
import json
import os
import random
import sqlite3
import subprocess
import sys
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
BOOKMARKS = os.path.join(HERE, "bookmarks.json")
WKE_OFFSET = 11644473600

# 热度分布：少数站点被反复打开，多数只碰过一两次 —— 这样「常用」排出来才像真的
HOT_WEIGHTS = {
    "github.com": 38, "chat.deepseek.com": 31, "mail.google.com": 26,
    "www.bilibili.com": 22, "developer.mozilla.org": 19, "www.zhihu.com": 17,
    "claude.ai": 15, "www.notion.so": 12, "stackoverflow.com": 11,
    "www.youtube.com": 10, "music.163.com": 9, "juejin.cn": 8,
    "calendar.google.com": 7, "www.figma.com": 7, "www.npmjs.com": 6,
    "chat.openai.com": 6, "juejin.cn": 5, "caniuse.com": 5,
    "translate.google.com": 4, "todoist.com": 4, "www.douban.com": 3,
}


def walk_urls(node):
    for c in node.get("children") or []:
        if c.get("type") == "url":
            yield c
        elif c.get("type") == "folder":
            yield from walk_urls(c)


def host_of(url):
    import re
    m = re.match(r"^[a-zA-Z][a-zA-Z0-9+.\-]*://([^/?#]+)", url or "")
    return (m.group(1).split("@")[-1].split(":")[0].lower() if m else "")


def build_history(db_path):
    """造一张最小可用的 Chromium History 库：只需要 urls 和 visits 两张表。

    关键点：**visits.url 存的是 urls.id 这个整数外键，不是 URL 字符串**
    （新版 Chromium schema 改的）。建表时照这个来，否则 JOIN 一条都对不上。
    """
    data = json.load(open(BOOKMARKS, encoding="utf-8"))
    urls = [u for r in data["roots"].values() if isinstance(r, dict) for u in walk_urls(r)]
    urls.sort(key=lambda u: u["url"])          # 固定顺序，保证可复现

    rnd = random.Random(20261024)
    now = datetime.datetime.now()

    if os.path.exists(db_path):
        os.remove(db_path)
    con = sqlite3.connect(db_path)
    cur = con.cursor()
    cur.execute("CREATE TABLE urls (id INTEGER PRIMARY KEY, url TEXT, title TEXT, visit_count INTEGER)")
    cur.execute("CREATE TABLE visits (id INTEGER PRIMARY KEY, url INTEGER, visit_time INTEGER, transition INTEGER)")

    vid, total = 1, 0
    for i, u in enumerate(urls, start=1):
        weight = HOT_WEIGHTS.get(host_of(u["url"]), rnd.choice([0, 1, 1, 2, 2, 3]))
        cur.execute("INSERT INTO urls VALUES (?,?,?,?)", (i, u["url"], u.get("name", ""), weight))
        for _ in range(weight):
            # 一周内散点：越热的站最近一次访问越靠前，排序才稳定
            minutes = rnd.randint(5, 7 * 24 * 60)
            t = now - datetime.timedelta(minutes=minutes)
            cur.execute("INSERT INTO visits VALUES (?,?,?,?)",
                        (vid, i, int((t.timestamp() + WKE_OFFSET) * 1_000_000), 0))
            vid += 1
            total += 1

    con.commit()
    con.close()
    return len(urls), total


def main():
    ap = argparse.ArgumentParser(description="用虚构数据生成演示页面")
    ap.add_argument("--out", default=os.path.join(HERE, "index.html"))
    ap.add_argument("--no-icons", action="store_true")
    args = ap.parse_args()

    tmpdir = tempfile.mkdtemp(prefix="nav_demo_")
    db = os.path.join(tmpdir, "History")
    n_url, n_visit = build_history(db)
    print(f"虚构历史: {n_url} 个网址 / {n_visit} 条访问记录")

    cmd = [sys.executable, os.path.join(ROOT, "gen_nav.py"),
           "--src", BOOKMARKS, "--history", db, "--out", args.out]
    if args.no_icons:
        cmd.append("--no-icons")
    raise SystemExit(subprocess.call(cmd))


if __name__ == "__main__":
    main()
