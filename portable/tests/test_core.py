#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""最小自测：纯本地、不联网、不需要任何第三方依赖。

运行:
  python tests/test_core.py

覆盖五组最容易回归的逻辑：
  1. 链接解析（BV / av / ?p=N）
  2. 转写段落合并
  3. 广告标记（含"撞车词"回归：夸克 vs 夸克App）
  4. 时长解析（mm:ss 与 h:mm:ss）
  5. 进度看板折叠（终态冻结、阶段不回退、加权总进度）
"""
import json
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

import bili_asr                      # noqa: E402
import progress_hub as ph            # noqa: E402
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


def test_progress_hub():
    """进度看板折叠逻辑：终态冻结、阶段不回退、插值与任务 ID 提取。"""
    import shutil
    d = Path(tempfile.mkdtemp(prefix='ph_test_'))
    try:
        # 任务 ID 提取
        check('task_of 从路径取 BV', ph.task_of('x/bili_BV0000000000.md') == 'BV0000000000')
        check('task_of 从 URL 取 BV',
              ph.task_of('https://www.bilibili.com/video/BV1111111111') == 'BV1111111111')

        # 插值：180s 音频在 Nano 5.08x 下预测约 35.4s
        pct = ph.pct_from_elapsed(17.7, 180.0, 'funasr-nano')
        check('插值 ≈50%', pct is not None and 49 <= pct <= 51, f'实际 {pct}')
        check('插值有上限（不越 97%）', ph.pct_from_elapsed(1e6, 180.0) == 97.0)
        check('无时长返回 None', ph.pct_from_elapsed(10, None) is None)

        ph.init_run([{'id': 'BVa', 'title': 'A', 'duration_sec': 1000},
                     {'id': 'BVb', 'title': 'B', 'duration_sec': 1000},
                     {'id': 'BVc', 'title': 'C', 'duration_sec': 1000}], d=d)
        ph.emit(task='BVa', stage='asr', pct=40.0, started=time.time() - 100,
                duration_sec=1000, d=d)
        marks = {'elapsed': 197.0}   # 真实转写耗时（预测 1000/5.08≈196.9，故意给个整值）
        ph.emit(task='BVa', stage='done', pct=100.0, elapsed=marks['elapsed'], d=d)
        ph.emit(task='BVb', stage='download', note='下载中', d=d)

        snap = ph.snapshot(d)
        rows = {r['id']: r for r in snap['tasks']}
        a = rows['BVa']
        check('done 阶段不回退（后续 asr 事件不覆盖）', a['stage'] == 'done')
        # 关键回归：done 后 elapsed 必须冻结在事件里的 197，不能是 now-started
        ph_elapsed = a['elapsed']
        check('done 后已用时间冻结', ph_elapsed == marks['elapsed'],
              f'实际 {ph_elapsed}（应恒为 {marks["elapsed"]}）')
        ph.emit(task='BVa', stage='asr', pct=10.0, started=time.time() - 9999, d=d)
        a2 = {r['id']: r for r in ph.snapshot(d)['tasks']}['BVa']
        check('终态不被迟到事件改写', a2['stage'] == 'done' and a2['elapsed'] == marks['elapsed'],
              f'stage={a2["stage"]} elapsed={a2["elapsed"]}')

        o = snap['overall']
        check('总数为 3', o['total'] == 3, f'实际 {o["total"]}')
        check('done / active 计数正确', o['done'] == 1 and o['active'] == 1, f'{o}')
        # 无任何事件的任务必须落在 queued（不能被静默丢掉，否则总进度分母会缩水）
        c = rows['BVc']
        check('无事件任务为排队且 0%', c['stage'] == 'queued' and c['pct'] == 0.0,
              f'stage={c["stage"]} pct={c["pct"]}')
        check('排队任务计入预计剩余', o['eta'] and o['eta'] > 0, f'eta={o["eta"]}')

        # 归档：折叠事件后快照可读，且旧文件按静默时长退役
        r = ph.compact(d, safe_age=0)
        check('compact 折叠出事件', r['snapshot_events'] >= 4, f'实际 {r["snapshot_events"]}')
        check('compact 退役旧文件', len(r['retired']) >= 1, f'{r}')
        snap2 = ph.snapshot(d)
        check('折叠后仍能读出同一状态',
              {x['id']: x for x in snap2['tasks']}['BVa']['stage'] == 'done')
        check('consumed.json 已落盘', (d / 'consumed.json').is_file())

        # 两个 skill 共用一个看板：init_run 必须是并入而非替换
        ph.init_run([{'id': 'BVa', 'title': 'A2', 'duration_sec': 2000}], d=d)
        ids = {x['id'] for x in ph.snapshot(d)['tasks']}
        check('init_run 并入不冲掉他人任务', {'BVa', 'BVb', 'BVc'} <= ids, f'{ids}')
        ph.init_run([{'id': 'BVd', 'title': 'D', 'duration_sec': 100,
                      'group': 'wiki编译', 'pred_sec': 90}], d=d)
        rows2 = {x['id']: x for x in ph.snapshot(d)['tasks']}
        check('同 id 更新而非重复', len(rows2) == 4, f'{sorted(rows2)}')
        check('非音频任务用自带 pred_sec', rows2['BVd']['pred_sec'] == 90,
              f'{rows2["BVd"]["pred_sec"]}')
        check('任务组进入 rows', rows2['BVd']['group'] == 'wiki编译')
        gs = {g['name']: g for g in ph.snapshot(d)['groups']}
        check('任务组汇总正确', gs.get('wiki编译', {}).get('total') == 1, f'{gs}')

        # 运行模式开关
        check('mode 默认 auto', ph.progress_mode(None) in ('auto',),
              ph.progress_mode(None))
        check('mode=off 被识别', ph.progress_mode('off') == 'off')
        check('非法 mode 回落 auto', ph.progress_mode('banana') == 'auto')
        check('off 模式不拉起服务',
              ph.ensure_serving(d, port=59999, mode='off') is None)
        check('manual 模式不拉起服务',
              ph.ensure_serving(d, port=59999, mode='manual') is None)
    finally:
        shutil.rmtree(d, ignore_errors=True)


def test_parse_duration():
    check('mm:ss 解析', parse_duration('12:34') == 754, f"实际 {parse_duration('12:34')}")
    check('h:mm:ss 解析', parse_duration('1:02:03') == 3723, f"实际 {parse_duration('1:02:03')}")
    check('非法输入返回 0', parse_duration('') == 0)


def main():
    print('=== bilibili-video-summary 自测 ===')
    for fn in (test_resolve_input, test_merge_segments, test_mark_ads,
               test_progress_hub, test_parse_duration):
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
