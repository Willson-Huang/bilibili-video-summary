#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""按关键词搜索 B站视频，输出简要清单（BV号 / UP主 / 标题 / 时长 / 播放）。

用法:
  python search_bili.py "关键词" [--limit N]

WBI 签名复用 bili_wbi.py，仅依赖标准库。
"""
import argparse
import re
import sys
import uuid

from bili_wbi import UA, http_json, wbi_url

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

H = {'User-Agent': UA, 'Referer': 'https://www.bilibili.com/',
     'Cookie': f'buvid3={str(uuid.uuid4()).upper()}infoc'}

SEARCH_URL = 'https://api.bilibili.com/x/web-interface/wbi/search/type'


def parse_duration(text):
    """'12:34' 或 '1:02:03' → 秒；无法解析返回 0。"""
    parts = (text or '').split(':')
    try:
        nums = [int(p) for p in parts]
    except ValueError:
        return 0
    sec = 0
    for n in nums:                      # 兼容 mm:ss 与 h:mm:ss 两种写法
        sec = sec * 60 + n
    return sec


def search(keyword, limit=12):
    """返回搜索结果列表。接口异常抛 RuntimeError。"""
    url = wbi_url(SEARCH_URL,
                  {'search_type': 'video', 'keyword': keyword, 'page': 1}, H)
    r = http_json(url, H)
    code = r.get('code')
    if code != 0:
        raise RuntimeError(f"搜索失败 [{code}] {r.get('message')}")
    out = []
    for v in ((r.get('data') or {}).get('result') or [])[:limit]:
        out.append({
            'bvid': v.get('bvid'),
            'author': v.get('author'),
            'title': re.sub('<[^>]+>', '', v.get('title', '')),
            'duration': v.get('duration', ''),
            'duration_sec': parse_duration(v.get('duration', '')),
            'play': v.get('play'),
        })
    return out


def main():
    ap = argparse.ArgumentParser(description='按关键词搜索 B站视频')
    ap.add_argument('keyword', help='搜索关键词')
    ap.add_argument('--limit', type=int, default=12, help='返回条数，默认 12')
    a = ap.parse_args()

    try:
        rows = search(a.keyword, a.limit)
    except Exception as e:
        print(f'[错误] {e}')
        sys.exit(1)

    if not rows:
        print('没有搜到结果')
        return
    for v in rows:
        print(f"{v['bvid']} | {v['author']} | {v['title']} | "
              f"{v['duration']} | {v['play']}播放")


if __name__ == '__main__':
    main()
