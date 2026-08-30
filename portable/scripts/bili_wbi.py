#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""B站 WBI 签名与 HTTP 公共模块。

被 bili_asr.py 与 search_bili.py 共用——WBI 签名算法和 MIXIN 混洗表是 B站的
固定套路，之前在多个脚本里各存一份，B站一旦调整接口就要同步改多处，极易漂移。

仅依赖标准库，可单独 import 测试。

用法:
  from bili_wbi import UA, http_json, wbi_url
  http_json('https://...', headers)
  wbi_url('https://api.bilibili.com/x/player/wbi/v2', {'bvid': 'BVxxx', 'cid': 1}, headers)
"""
import hashlib
import json
import time
import urllib.parse
import urllib.request

UA = ('Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 '
      '(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36')

NAV_URL = 'https://api.bilibili.com/x/web-interface/nav'

# WBI 签名用的混洗表（B站固定常量，接口变更时需同步更新）
MIXIN = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
         33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
         26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
         20, 34, 44, 52]

_WBI = None


def http_json(url, headers=None, timeout=30):
    """GET 一个 URL 并解析 JSON。headers 缺省时只带 UA。"""
    req = urllib.request.Request(url, headers=headers or {'User-Agent': UA})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode('utf-8', 'replace'))


def _wbi_clean(v):
    """WBI 签名要求去掉参数值里的 !'()*"""
    s = str(v)
    for ch in "!'()*":
        s = s.replace(ch, '')
    return s


def wbi_keys(headers=None):
    """获取 wbi img/sub key，进程内缓存一次。"""
    global _WBI
    if _WBI:
        return _WBI
    j = http_json(NAV_URL, headers)
    if not isinstance(j, dict) or not (j.get('data') or {}).get('wbi_img'):
        raise RuntimeError(f'获取 wbi key 失败（接口返回异常）: code={j.get("code") if isinstance(j, dict) else "?"}')
    k = lambda u: u.split('/').pop().split('.')[0]
    _WBI = (k(j['data']['wbi_img']['img_url']), k(j['data']['wbi_img']['sub_url']))
    return _WBI


def wbi_sign(params, headers=None):
    """给参数加 wts 与 w_rid 签名。"""
    a, b = wbi_keys(headers)
    p = dict(params, wts=int(time.time()))
    q = '&'.join(urllib.parse.quote(str(k), safe='') + '='
                 + urllib.parse.quote(_wbi_clean(p[k]), safe='') for k in sorted(p))
    raw = a + b
    mk = ''.join(raw[i] for i in MIXIN)[:32]
    return dict(p, w_rid=hashlib.md5((q + mk).encode()).hexdigest())


def wbi_url(base, params, headers=None):
    """拼出带 WBI 签名的完整 URL。"""
    return base + '?' + urllib.parse.urlencode(wbi_sign(params, headers))
