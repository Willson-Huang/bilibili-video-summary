#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""最小自测：纯本地、不联网、不需要任何第三方依赖。

运行:
  python tests/test_core.py

覆盖四组最容易回归的逻辑：
  1. 链接解析（BV / av / ?p=N）
  2. 转写段落合并
  3. 广告标记（含"撞车词"回归：夸克 vs 夸克App）
  4. 时长解析（mm:ss 与 h:mm:ss）
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

import bili_asr                      # noqa: E402
from search_bili import parse_duration  # noqa: E402

FAILS = []


def check(name, cond, detail=''):
    if cond:
        print(f'  PASS  {name}')
    else:
        print(f'  FAIL  {name} {detail}')
        FAILS.append(name)


def test_resolve_input():
    cases = [
        ('https://www.bilibili.com/video/BV1xx411c7mD', 'BV1xx411c7mD', 1),
        ('https://www.bilibili.com/video/BV1xx411c7mD?p=3', 'BV1xx411c7mD', 3),
        ('BV1xx411c7mD', 'BV1xx411c7mD', 1),
    ]
    for raw, bv, page in cases:
        try:
            _url, pg, got = bili_asr.resolve_input(raw)
        except Exception as e:
            check(f'解析 {raw}', False, f'异常: {e}')
            continue
        check(f'解析 {raw}', got == bv and pg == page, f'-> {got} / P{pg}')


def test_merge_segments():
    segs = [
        {'start': 0.0, 'end': 1.0, 'text': '你好'},
        {'start': 1.2, 'end': 2.0, 'text': '世界'},
        {'start': 30.0, 'end': 31.0, 'text': '另起一段'},
    ]
    blocks = bili_asr.merge_segments(segs)
    check('短句应合并', len(blocks) == 2, f'实际 {len(blocks)} 段')
    check('保留首段时间戳', blocks and blocks[0]['start'] == 0.0)
    check('长间隔不合并', len(blocks) == 2 and blocks[1]['start'] == 30.0)


def test_mark_ads():
    kws = ['夸克App', '拼多多']
    blocks = [
        {'start': 0, 'end': 1, 'text': '他讲了物理里的夸克'},
        {'start': 2, 'end': 3, 'text': '现在打开夸克App搜索'},
        {'start': 4, 'end': 5, 'text': '这是一段正常内容'},
    ]
    _hits, n = bili_asr.mark_ads(blocks, kws)
    check('广告命中数为 1', n == 1, f'实际 {n}')
    check('撞车词不误标（物理夸克）', not blocks[0]['is_ad'])
    check('产品全称应命中', blocks[1]['is_ad'])
    check('正常段落不标记', not blocks[2]['is_ad'])


def test_parse_duration():
    check('mm:ss 解析', parse_duration('12:34') == 754, f"实际 {parse_duration('12:34')}")
    check('h:mm:ss 解析', parse_duration('1:02:03') == 3723, f"实际 {parse_duration('1:02:03')}")
    check('非法输入返回 0', parse_duration('') == 0)


def main():
    print('=== bilibili-video-summary 自测 ===')
    for fn in (test_resolve_input, test_merge_segments, test_mark_ads, test_parse_duration):
        print(f'-- {fn.__name__}')
        try:
            fn()
        except Exception as e:
            check(fn.__name__, False, f'抛出异常: {e!r}')
    print()
    if FAILS:
        print(f'FAIL 失败 {len(FAILS)} 项: {FAILS}')
        return 1
    print('全部通过')
    return 0


if __name__ == '__main__':
    sys.exit(main())
