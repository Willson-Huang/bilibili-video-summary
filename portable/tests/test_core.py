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
import os
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

try:
    import library_queue as lq       # 便携版不含该脚本（WorkBuddy 资料库专用）→ 对应用例跳过
except Exception:
    lq = None

NEED_LQ = ('test_index_heal', 'test_finish_guards', 'test_dedup_skip', 'test_audit_fixes',
           'test_notes_done')

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


def test_strip_prompt_leak():
    # 实测形态：whisper 把 initial_prompt 续写成第一段伪正文
    P = '以下是普通话的中文内容，包含专业术语。请使用正确的中文标点符号。'

    segs = [{'start': 0.0, 'end': 2.0, 'text': '请使用正确的中文标点符号。'},
            {'start': 2.1, 'end': 5.0, 'text': '大家好，今天我们来聊食堂。'}]
    out, hits = bili_asr.strip_prompt_leak(segs, P)
    check('泄漏: 首段假正文被剔除', len(out) == 1 and hits == 1, f'{len(out)} 段 / hits={hits}')
    check('泄漏: 真实内容与时间戳保留',
          bool(out) and out[0]['text'].startswith('大家好') and out[0]['start'] == 2.1)

    # 整段 prompt（多句叠在一段里）应被完整剥掉
    segs = [{'start': 0.0, 'end': 3.0, 'text': P},
            {'start': 3.1, 'end': 4.0, 'text': '正片开始。'}]
    out, hits = bili_asr.strip_prompt_leak(segs, P)
    check('泄漏: 整段 prompt 被剥离', len(out) == 1 and out[0]['text'] == '正片开始。',
          f'{len(out)} 段 / -> {out[0]["text"] if out else None}')

    # 假正文与真实内容同段 → 只剥前缀
    segs = [{'start': 0.0, 'end': 3.0, 'text': '请使用正确的中文标点符号。大家好。'}]
    out, hits = bili_asr.strip_prompt_leak(segs, P)
    check('泄漏: 同段只剥前缀', len(out) == 1 and out[0]['text'] == '大家好。',
          f'-> {out[0]["text"] if out else None}')

    # 截断续写：整段是 prompt 的连续子串
    segs = [{'start': 0.0, 'end': 1.0, 'text': '包含专业术语'},
            {'start': 1.1, 'end': 2.0, 'text': '正片'}]
    out, hits = bili_asr.strip_prompt_leak(segs, P)
    check('泄漏: 截断续写被剔除', len(out) == 1 and hits == 1, f'{len(out)} 段 / hits={hits}')

    # 正常正文不能被误伤；prompt 字样出现在后续段时也不动
    segs = [{'start': 0.0, 'end': 1.0, 'text': '大家好'},
            {'start': 1.1, 'end': 2.0, 'text': '以下是普通话的中文内容'}]
    out, hits = bili_asr.strip_prompt_leak(segs, P)
    check('泄漏: 正常正文不动', len(out) == 2 and hits == 0, f'{len(out)} 段 / hits={hits}')

    # 边界：空 prompt / 空 segs / prompt 无实质分句
    out, hits = bili_asr.strip_prompt_leak(segs, '')
    check('泄漏: 空 prompt 原样返回', len(out) == 2 and hits == 0)
    out, hits = bili_asr.strip_prompt_leak([], P)
    check('泄漏: 空 segs 不崩', out == [] and hits == 0)
    out, hits = bili_asr.strip_prompt_leak(segs, '　。 ')
    check('泄漏: prompt 无实质分句不崩', len(out) == 2 and hits == 0)

    # 不修改入参
    src = [{'start': 0.0, 'end': 1.0, 'text': '请使用正确的中文标点符号。'}]
    bili_asr.strip_prompt_leak(src, P)
    check('泄漏: 不修改入参', len(src) == 1 and src[0]['text'] == '请使用正确的中文标点符号。')


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
        # 「已耗时」＝各已完成任务的实测耗时合计。回归：它曾算成 eff_now − created
        # （运行目录的建立时刻），而目录不按批次隔离 → 界面上显示成「目录存在了多久」
        # （实盘 2026-10-02：415h06m41s，而任务实际耗时合计只有 3 小时多）。
        check('已耗时 = 已完成任务的耗时合计',
              o['elapsed'] == marks['elapsed'],
              f'-> {o["elapsed"]}（应为 {marks["elapsed"]}，不是目录年龄）')
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


def _fixture_video():
    """素材包测试共用元信息夹具（与真实 view 接口同构，不含网络依赖）"""
    return {
        'bvid': 'BV1TEST00001', 'aid': 111222333, 'cid': 444555666,
        'title': '测试视频：肇庆古城与利玛窦',
        'desc': '本集讲肇庆古城与利玛窦。',
        'pubdate': 1735689600, 'duration': 725,
        'owner': {'name': '测试UP', 'mid': 987654321},
        'stat': {'view': 12345, 'like': 678, 'coin': 90, 'favorite': 12,
                 'reply': 34, 'danmaku': 56},
        'videos': 1,
        'pages': [{'page': 1, 'cid': 444555666, 'part': '正片', 'duration': 725}],
    }


def test_pack_sections():
    """素材包分节纯函数：每条格式规则独立断言"""
    V = _fixture_video()
    pg = V['pages']
    url = 'https://www.bilibili.com/video/BV1TEST00001'
    head = bili_asr.pack_head(V, V['bvid'], 444555666, 1, pg, pg[0], url)
    check('头部含基本信息', '## 基本信息' in head and any('- BV号：' in x for x in head))
    check('单P不写分P列表', not any('## 分P列表' in x for x in head))
    check('头部含简介', '## 视频简介' in head)

    V2 = dict(V)
    V2['desc'] = '-'
    head2 = bili_asr.pack_head(V2, V2['bvid'], 444555666, 1, pg, pg[0], url)
    check('简介为空则不写该节', '## 视频简介' not in head2)

    pg2 = [{'page': 1, 'cid': 1, 'part': 'P1', 'duration': 60},
           {'page': 2, 'cid': 2, 'part': 'P2', 'duration': 60}]
    head3 = bili_asr.pack_head(V2, V2['bvid'], 2, 2, pg2, pg2[1], url)
    check('多P写分P列表并标当前', '## 分P列表' in head3 and any('← 当前' in x for x in head3))

    check('无章节返回空列表', bili_asr.pack_chapters([]) == [])
    ch = bili_asr.pack_chapters([{'from': 3725, 'content': '第三章'}])
    check('章节带时间戳', ch[:2] == ['', '## 章节（UP主标记）']
          and ch[2] == '- [01:02:05] 第三章', f'-> {ch}')

    check('force-asr 不写说明行', bili_asr.pack_asr_note(True, True) == [])
    check('未登录提示凭据', any('未配置登录凭据' in x for x in bili_asr.pack_asr_note(False, False)))
    check('已登录提示无字幕', any('未开放字幕' in x for x in bili_asr.pack_asr_note(True, False)))

    blocks = [{'start': 0.0, 'end': 1.0, 'text': '正片', 'is_ad': False},
              {'start': 5.0, 'end': 6.0, 'text': '本期广告', 'is_ad': True}]
    body = bili_asr.pack_asr_body('本地 Whisper large-v3-turbo', 'cuda', blocks, [], 0)
    check('正文首行带引擎与设备', body[1] == '## 转写全文（本地 Whisper large-v3-turbo · cuda）',
          f'-> {body[1]}')
    check('广告行加 [广告?] 前缀', '[00:00:05] [广告?] 本期广告' in body, f'-> {body[5:]}')
    check('无广告命中不写表', not any(x.startswith('| 时间戳') for x in body))

    hits = [{'start': 5.0, 'keywords': ['本期广告'], 'text': '本期广告'}]
    body2 = bili_asr.pack_asr_body('e', 'cuda', blocks, hits, 1)
    check('有广告命中写表格', '| 00:00:05 | 本期广告 | 本期广告 |' in body2)

    check('无评论返回空列表', bili_asr.pack_comments([]) == [])
    cs = bili_asr.pack_comments([{'member': {'uname': '甲'}, 'like': 12,
                                  'content': {'message': '很好\n不错'}}])
    check('评论序号与换行处理', cs[-1] == '1. @甲（12赞）：很好 不错', f'-> {cs[-1]}')

    meta = bili_asr.pack_meta({'route': 'asr:funasr-nano:Fun-ASR-Nano-2512@cuda',
                               'engine': 'funasr-nano', 'engine_model': 'Fun-ASR-Nano-2512',
                               'device': 'cuda', 'source': '本地 Fun-ASR-Nano（x）',
                               'ts_granularity': 'segment(token)', 'hotwords_count': 21,
                               'hotwords_sha256': 'a' * 40, 'audio_sec': 725.0})
    check('元信息块有标题与禁抄提示', meta[1] == '## 元信息' and '不要抄进正文' in meta[2], f'-> {meta[1:3]}')
    check('元信息 8 个字段齐全', len([x for x in meta if x.startswith('- ')]) == 8,
          f'-> {[x for x in meta if x.startswith("- ")]}')
    check('元信息记热词指纹', '- hotwords: 21 词 · sha256:' + 'a' * 16 in meta, f'-> {meta[-2]}')
    check('缺失字段回落为 -', '- device: -' in bili_asr.pack_meta({'engine': 'subtitle'}))
    check('元信息不含生成时间与墙钟耗时',
          not any(k in x for x in meta for k in ('generated', 'download_sec', 'asr_sec')), f'-> {meta}')


PACK_GOLDEN = {
    # 2026-09-16 M1：素材包新增「## 元信息」块，五个基线同步更新（每场景仅 +11 行，零删除）
    # 2026-09-28：正文里的「发布」时间由 fmt_date 渲染，而它走 time.localtime ——
    #   基线原是在 UTC+8 的机器上算的，换到 UTC 的 CI runner 上 5 个场景全红。
    #   现在测试把渲染时钟钉在 UTC（见下方 fmt_date 桩），基线按钉住后的文本重算。
    'S1_subtitle': 'f5a906fefd167556998d619fff18d40de18638b988022152348ab6bfa535db0a',
    'S2_whisper_anon': '3ea7f1e456b2a63ddec7c32d48a58388e2d46cbf3ab54bbf8c79c12233c64113',
    'S3_only_meta': '64a084a1c50214cbf8ef186ba871c8a8311a67de32b04fe257b615b020572ab6',
    'S4_force_asr_multipage': '643fd293b0316af05134bf632fdab70aa587c39ba6db7db8b9305636dcd6802b',
    'S5_nano_preset': '817464e0c67017b6fc71e1446fa159510f4f0c8028fa6ccb17cf41e73a132ff4',
}
_STUBBED = ('resolve_input', 'fetch_meta', 'fetch_player', 'fetch_subtitle_text',
            'fetch_comments', 'load_ad_keywords', 'load_ad_excludes', 'transcribe_with',
            'ModelHolder', 'download_audio', '_prog', 'fmt_date')


def _pack_scenarios():
    """5 个场景覆盖全部分支：字幕直取 / whisper / only-meta / force-asr / Nano 批处理"""
    V = _fixture_video()
    v_desc2 = dict(V)
    v_desc2['desc_v2'] = [{'raw_text': '第一段简介'}, {'raw_text': '第二段简介'}]
    v_multi = dict(V)
    v_multi['videos'] = 2
    v_multi['pages'] = [{'page': 1, 'cid': 444555666, 'part': 'P1 正片', 'duration': 725},
                        {'page': 2, 'cid': 444555667, 'part': 'P2 番外', 'duration': 300}]
    ch = [{'from': 12, 'content': '开场'}, {'from': 3725, 'content': '第三章：端王'}]
    subs = [{'lan': 'ai-zh', 'lan_doc': '中文（AI 生成，可能有错字）', 'subtitle_url': '//x/y.json'}]
    sub_text = '[00:00:12] 第一句。\n[00:00:15] 第二句。'
    sub_label = '中文（AI 生成，可能有错字）'
    cmts = [{'member': {'uname': '甲'}, 'like': 12, 'content': {'message': '很好\n不错'}},
            {'member': {'uname': '乙'}, 'like': 3, 'content': {'message': '沙发'}}]
    segs = [{'start': 0.0, 'end': 2.0, 'text': '大家好'},
            {'start': 2.2, 'end': 5.0, 'text': '本期讲肇庆'},
            {'start': 5.5, 'end': 9.0, 'text': '本期广告由某某赞助'},
            {'start': 60.0, 'end': 63.0, 'text': '我们下期再见'}]
    nano_meta = {'model': 'Fun-ASR-Nano-2512', 'load_sec': 51.2, 'asr_sec': 88.0,
                 'engine': 'funasr-nano', 'device': 'cuda', 'language': 'zh', 'duration': 725.0,
                 'hotwords_count': 21, 'hotwords_sha256': 'a' * 16,
                 'hotwords_source_sha256': 'b' * 16, 'hotwords_truncated': False,
                 'hotwords_total': 21, 'hotwords_status': 'loaded'}
    base = dict(ch=(), subs=(), sub=None, cmts=(), logged_in=True, engine='whisper',
                segs=segs, meta={'duration': 725.0, 'device': 'cuda',
                                 'model': 'large-v3-turbo', 'language': 'zh'})
    scen = {
        'S1_subtitle': dict(base, V=v_desc2, ch=ch, subs=subs, sub=(sub_text, sub_label), cmts=cmts),
        'S2_whisper_anon': dict(base, V=V, cmts=cmts, logged_in=False),
        'S3_only_meta': dict(base, V=V, ch=ch, subs=subs, cmts=cmts, only_meta=True),
        'S4_force_asr_multipage': dict(base, V=v_multi, ch=ch, subs=subs, cmts=(),
                                       sub=(sub_text, sub_label), force_asr=True),
        'S5_nano_preset': dict(base, V=V, ch=ch, cmts=cmts, engine='funasr-nano',
                               preset={'audio': 'x', 'segments': segs, 'engine_meta': nano_meta}),
    }
    return scen


def test_pack_pipeline():
    """端到端：桩掉网络与模型，比对素材包全文哈希（格式回归网，M1 改格式时会在这里红）"""
    import hashlib
    import shutil
    import types
    saved = {n: getattr(bili_asr, n, None) for n in _STUBBED}
    saved_dir = bili_asr.AUDIO_DIR
    tmp = Path(tempfile.mkdtemp(prefix='bili_pack_test_'))
    try:
        bili_asr.AUDIO_DIR = tmp
        audio = tmp / '_audio.m4a'
        audio.write_bytes(b'x')
        bili_asr._prog = lambda: None
        # 「发布」时间由 fmt_date 渲染，取的是本机时区 —— 拿全文哈希当基线就必须先把时钟钉住，
        # 否则换一台机器（或 UTC 的 CI）跑，5 个场景会一起红。格式仍走同一条代码路径，只是取值不再随机器的时区变。
        bili_asr.fmt_date = lambda u: time.strftime('%Y-%m-%d %H:%M', time.gmtime(u))
        bili_asr.resolve_input = lambda raw: ('https://www.bilibili.com/video/BV1TEST00001', 1,
                                              'BV1TEST00001')
        bili_asr.fetch_player = lambda bvid, cid: ([], [])
        bili_asr.load_ad_keywords = lambda: ['本期广告', '带货链接']
        bili_asr.load_ad_excludes = lambda: []
        bili_asr.ModelHolder = lambda *a, **k: types.SimpleNamespace(get=lambda: ('fake', 'cuda'))
        bili_asr.download_audio = lambda url, d, cookie=None, page=1, stem='audio': (str(audio), {})
        for name, sc in _pack_scenarios().items():
            bili_asr.fetch_meta = lambda url, page, _V=sc['V']: _V
            bili_asr.fetch_player = lambda bvid, cid, _s=sc: (list(_s['ch']), list(_s['subs']))
            bili_asr.fetch_subtitle_text = lambda subs, _s=sc: _s['sub']
            bili_asr.fetch_comments = lambda aid, _s=sc: list(_s['cmts'])
            bili_asr.transcribe_with = lambda *a, _s=sc: (list(_s['segs']), dict(_s['meta']))
            bili_asr.COOKIE_HEADER = 'SESSDATA=test' if sc['logged_in'] else ''
            args = types.SimpleNamespace(
                url=None, model='large-v3-turbo', device='auto', compute='int8_float16',
                lang='zh', prompt='p', p=1, out=None, cookie=None, keep_audio=False,
                no_comments=False, hf_mirror=True, only_meta=sc.get('only_meta', False),
                force_asr=sc.get('force_asr', False), engine=sc['engine'], hotwords=None,
                classify=False, batch_file=None)
            out = tmp / f'{name}.md'
            bili_asr.process_video(args, 'BV1TEST00001', str(out), preset=sc.get('preset'))
            txt = out.read_text(encoding='utf-8')
            got = hashlib.sha256(txt.encode('utf-8')).hexdigest()
            check(f'素材包格式 {name}', got == PACK_GOLDEN[name],
                  f'sha256={got[:16]} 期望 {PACK_GOLDEN[name][:16]}；文件 {out}')
    finally:
        for n, v in saved.items():
            setattr(bili_asr, n, v)
        bili_asr.AUDIO_DIR = saved_dir
        shutil.rmtree(tmp, ignore_errors=True)


def test_index_heal():
    """索引自愈：索引缺行/字段不全时从素材包反解补齐（直跑 --batch-file 的善后）"""
    import shutil
    tmp = Path(tempfile.mkdtemp(prefix='bili_index_test_'))
    try:
        cache = tmp / 'cache' / 'bili'
        arch = tmp / 'cache' / 'bili_subs'
        cache.mkdir(parents=True)
        arch.mkdir(parents=True)
        bv = 'BV1TEST0000X'
        pack = arch / f'bili_{bv}.md'
        pack.write_text('# B站视频素材包（本地 ASR 转写）\n\n## 基本信息\n'
                        '- 标题：测试（上）：带括号的标题\n'
                        f'- BV号：{bv} ｜ av号：1 ｜ cid：2 ｜ 分P：1/1\n'
                        '- UP主：某某UP（mid:123）\n'
                        '- 发布：2026-09-02 19:00 ｜ 时长：24:25\n', encoding='utf-8')

        hdr = lq.parse_pack_header(pack)
        check('反解得标题（含括号不误截）', hdr.get('title') == '测试（上）：带括号的标题', f'-> {hdr}')
        check('反解得 UP主（以（mid: 为界）', hdr.get('up') == '某某UP', f'-> {hdr}')
        check('反解得发布日', hdr.get('pubdate') == '2026-09-02', f'-> {hdr}')
        check('非素材包/不存在返回空', lq.parse_pack_header(cache / 'nope.md') == {})

        item, healed = lq.heal_index(cache, bv)
        check('索引缺行时自愈成功', healed and item.get('pubdate') == '2026-09-02', f'-> {item} / {healed}')
        check('自愈后 transcript 留空', item.get('transcript') == '', f'-> {item}')
        check('自愈结果已落盘',
              json.loads((cache / 'index.json').read_text(encoding='utf-8'))[bv]['up'] == '某某UP')
        check('自愈后能生成标准纪要名',
              lq.note_name(item['pubdate'], item['title'], item['up'])
              == '2026-09-02_测试（上）：带括号的标题_某某UP_纪要.md',
              f'-> {lq.note_name(item["pubdate"], item["title"], item["up"])}')

        lq.save_index(cache, {bv: {'transcript': 'D:/keep/me.md'}})
        item2, healed2 = lq.heal_index(cache, bv)
        check('字段不全时补齐', healed2 and item2.get('title') == '测试（上）：带括号的标题', f'-> {item2}')
        check('不覆盖已有 transcript', item2.get('transcript') == 'D:/keep/me.md', f'-> {item2}')
        item2b, healed2b = lq.heal_index(cache, bv)
        check('已完整时不重复改写', healed2b is False, f'-> {healed2b}')

        lq.save_index(cache, {})
        item3, healed3 = lq.heal_index(cache, 'BV1NOPACK000')
        check('无素材包不编造字段', (not item3) and healed3 is False, f'-> {item3} / {healed3}')
        check('无素材包不新建索引行',
              'BV1NOPACK000' not in json.loads((cache / 'index.json').read_text(encoding='utf-8')))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_notes_done():
    """纪要写完自动补发完成信号：配对、幂等、标点差异、拒绝猜测。

    这一步原先靠人工执行 --emit，实测累计漏了 7 次；脚本化之后必须保证它既会补，
    也不会乱补（配不上就跳过，绝不为「让数字好看」编造一条完成）。
    """
    import shutil
    import notes_done as nd

    def fresh(d):
        getattr(ph, '_RAW_CACHE', {}).clear()

    tmp = Path(tempfile.mkdtemp(prefix='bili_notes_done_test_'))
    try:
        lib = tmp / 'lib'
        cache = lib / '.workbuddy' / 'cache' / 'bili'
        arch = lib / '.workbuddy' / 'cache' / 'bili_subs'
        raw = lib / 'raw'
        run = lib / '.workbuddy' / 'cache' / 'progress' / 'current'
        for d in (cache, arch, raw, run):
            d.mkdir(parents=True, exist_ok=True)

        def pack(bv, title):
            (arch / f'bili_{bv}.md').write_text(
                '# B站视频素材包（本地 ASR 转写）\n\n## 基本信息\n'
                f'- 标题：{title}\n'
                f'- BV号：{bv} ｜ av号：1 ｜ cid：2 ｜ 分P：1/1\n'
                '- UP主：某某UP（mid:123）\n'
                '- 发布：2026-09-02 19:00 ｜ 时长：24:25\n', encoding='utf-8')

        def stage_of(bv):
            """按编号取状态：run.json 会累积多批任务，按下标取会取到别的批次。"""
            fresh(run)
            return next(t['stage'] for t in ph.snapshot(run)['tasks'] if t['id'] == bv)

        # 1 号：素材包头部用直引号，磁盘文件名用中文弯引号（实测里的真实差异）
        bv1, title1 = 'BV1DONETEST1', '会议纪要"上"：测试'
        pack(bv1, title1)
        (raw / '2026-09-02_会议纪要“上”：测试_某某UP_纪要.md').write_text('正文', encoding='utf-8')
        ph.init_run([{'id': bv1, 'title': title1, 'up': '某某UP', 'duration_sec': 1465}],
                    title='测试批次', d=run)
        ph.emit(task=bv1, stage='asr', pct=50.0, d=run)
        ph.emit(task=bv1, stage='transcribed', pct=100.0, elapsed=300.0, d=run)
        check('补发前停在待生成纪要', stage_of(bv1) == 'transcribed')

        got = nd.reconcile(run, str(raw), str(cache))
        row = (got.get('items') or [{}])[0]
        check('标点差异仍能配对', got.get('done') == 1 and row.get('note_file'), f'-> {row}')
        check('真的写入了完成事件', row.get('emitted') is True, f'-> {row}')
        check('补发后状态为已完成', stage_of(bv1) == 'done')
        check('耗时取自纪要文件时间与转写结束之差',
              row.get('elapsed') is not None and row['elapsed'] >= 0, f'-> {row.get("elapsed")}')

        # 顺带把「待编译」挂上看板：转写侧的完成只到纪要，编译是另一件事
        check('补发时顺带登记了待编译', row.get('pending') is True, f'-> {row.get("pending")}')
        fresh(run)
        comp = [t for t in ph.snapshot(run)['tasks'] if t.get('stage') == 'pending']
        check('待编译条目进了看板', len(comp) == 1, f'-> {len(comp)} 条')
        check('待编译条目落在编译组、名字是纪要文件名',
              bool(comp) and comp[0].get('group') == 'wiki编译'
              and comp[0]['id'].endswith('_某某UP_纪要'),
              f'-> {comp[0] if comp else None}')

        again = nd.reconcile(run, str(raw), str(cache))
        check('幂等：已完成的任务不再是候选',
              again.get('done') == 0 and not again.get('items'), f'-> {again}')

        # 2 号：素材包在，纪要没写 → 跳过而不是编造
        bv2 = 'BV1DONETEST2'
        pack(bv2, '只有素材包没有纪要')
        ph.init_run([{'id': bv2, 'title': '只有素材包没有纪要', 'up': '某某UP'}],
                    title='测试批次', d=run)
        ph.emit(task=bv2, stage='transcribed', pct=100.0, d=run)
        got2 = nd.reconcile(run, str(raw), str(cache), only=[bv2])
        check('纪要不存在时跳过', got2['skipped'] == 1 and '不在目录里' in got2['items'][0]['skipped'],
              f'-> {got2["items"]}')
        check('跳过时不写入完成事件', stage_of(bv2) != 'done')

        # 3 号：两个文件名都不是标准名、却归一化到同一个键 → 拒绝猜测
        #（直引号在 Windows 上不能出现在文件名里，所以用「带弯引号」与「夹空格」两种写法；
        #  标准名本身不存在，精确匹配落空后才会进入归一化比对）
        bv3, title3 = 'BV1DONETEST3', '会议纪要"钢之谋断"'
        pack(bv3, title3)
        (raw / '2026-09-02_会议纪要“钢之谋断”_某某UP_纪要.md').write_text('甲', encoding='utf-8')
        (raw / '2026-09-02_会议纪要 钢之谋断_某某UP_纪要.md').write_text('乙', encoding='utf-8')
        ph.init_run([{'id': bv3, 'title': title3, 'up': '某某UP'}], title='测试批次', d=run)
        ph.emit(task=bv3, stage='transcribed', pct=100.0, d=run)
        got3 = nd.reconcile(run, str(raw), str(cache), only=[bv3])
        check('归一化撞名时拒绝猜测',
              got3['skipped'] == 1 and '个候选' in got3['items'][0]['skipped'],
              f'-> {got3["items"]}')

        # 4 号：素材包缺失 → 跳过
        bv4 = 'BV1DONETEST4'
        ph.init_run([{'id': bv4, 'title': '没有素材包', 'up': '某某UP'}], title='测试批次', d=run)
        ph.emit(task=bv4, stage='transcribed', pct=100.0, d=run)
        got4 = nd.reconcile(run, str(raw), str(cache), only=[bv4])
        check('素材包缺失时跳过', '素材包不存在' in got4['items'][0]['skipped'], f'-> {got4["items"]}')

        # 5 号：编译条目已存在 → 不重复登记（只补完成）
        bv5b, title5b = 'BV1DONETEST5', '已经登记过编译'
        pack(bv5b, title5b)
        (raw / '2026-09-02_已经登记过编译_某某UP_纪要.md').write_text('正文', encoding='utf-8')
        ph.init_run([{'id': bv5b, 'title': title5b, 'up': '某某UP'}], title='测试批次', d=run)
        ph.emit(task=bv5b, stage='transcribed', pct=100.0, d=run)
        fresh(run)
        nd.register_pending('2026-09-02_已经登记过编译_某某UP_纪要', d=run)
        got5b = nd.reconcile(run, str(raw), str(cache), only=[bv5b])
        check('编译任务已存在时不重复登记',
              got5b['items'][0].get('pending') == '已登记', f'-> {got5b["items"]}')

        # 6 号：关掉开关 → 只补完成，不登记待编译
        bv6, title6 = 'BV1DONETEST6', '不登记待编译'
        pack(bv6, title6)
        (raw / '2026-09-02_不登记待编译_某某UP_纪要.md').write_text('正文', encoding='utf-8')
        ph.init_run([{'id': bv6, 'title': title6, 'up': '某某UP'}], title='测试批次', d=run)
        ph.emit(task=bv6, stage='transcribed', pct=100.0, d=run)
        fresh(run)
        got6 = nd.reconcile(run, str(raw), str(cache), only=[bv6], pend=False)
        check('关闭开关时不登记待编译',
              'pending' not in got6['items'][0], f'-> {got6["items"]}')

        # 目录推导：只给素材包目录，也能定位运行目录与纪要目录
        got5 = nd.reconcile(None, None, str(cache), dry=True)
        check('只给素材包目录也能定位运行目录', got5.get('run_dir') == str(run.resolve()),
              f'-> {got5.get("run_dir")}')
        check('只给素材包目录也能定位纪要目录', got5.get('raw_dir') == str(raw.resolve()),
              f'-> {got5.get("raw_dir")}')

        # 未启用进度上报且没给运行目录 → 明确跳过，不误写
        keep = os.environ.pop('BILI_PROGRESS_DIR', None)
        try:
            check('无运行目录时不误写', 'skipped' in nd.reconcile(None, str(raw), str(cache)))
        finally:
            if keep is not None:
                os.environ['BILI_PROGRESS_DIR'] = keep
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_batch_rotate():
    """批次隔离：一个批次一个运行目录（2026-10-02）。

    回归：所有批次曾共用一个 run.json，`created` 被首个写入者锁住，任务越滚越多
    —— 看板的「已耗时」因此量成了目录年龄（415 小时）。
    """
    import shutil
    tmp = Path(tempfile.mkdtemp(prefix='bili_batch_test_'))
    try:
        cur = tmp / 'progress' / 'current'
        cur.mkdir(parents=True)

        # 空目录不该轮转，否则连续开批会留下一串空目录
        check('空目录不轮转', ph.rotate_batch(cur, group='B站转写') is None)
        check('空目录连归档根都不建', not (tmp / 'progress' / 'batches').exists())

        # 写入一批后再轮转
        ph.init_run([{'id': 'BV1BATCH0001', 'title': '甲'}], title='第一批',
                    d=cur, group='B站转写')
        ph.emit(task='BV1BATCH0001', stage='asr', pct=50.0, d=cur)
        (cur / 'hub.json').write_text('{"port":8765}', encoding='utf-8')
        dst = ph.rotate_batch(cur, group='B站转写')
        check('轮转返回归档目录', dst is not None and dst.is_dir(), f'-> {dst}')
        check('归档目录名带上来源', dst.name.endswith('-B站转写'), dst.name)
        check('清单与事件都随批次搬走',
              (dst / 'run.json').is_file() and any(dst.glob('events_*.jsonl')),
              f'-> {sorted(p.name for p in dst.iterdir())}')
        check('hub.json 留在原地（服务状态不算批次数据）',
              (cur / 'hub.json').is_file() and not (dst / 'hub.json').exists())
        check('当前目录里已无清单', not (cur / 'run.json').exists())

        # 目录已空，再轮转不该产生第二个批次
        check('空目录二次轮转不产生新批次',
              ph.rotate_batch(cur, group='B站转写') is None
              and len(list((tmp / 'progress' / 'batches').iterdir())) == 1)

        rows = ph.list_batches(cur)
        check('list_batches 列出归档批次',
              len(rows) == 1 and rows[0]['total'] == 1, f'-> {rows}')

        # 先往当前目录写一批（不开新批）
        ph.init_run([{'id': 'BV1BATCH0002', 'title': '乙'}], title='第二批',
                    d=cur, group='B站转写')

        # 再开新批：上一批应当被自动归档
        ph.init_run([{'id': 'BV1BATCH0003', 'title': '丙'}], title='第三批',
                    d=cur, group='B站转写', new_batch=True)
        check('开新批会把上一批归档',
              len(list((tmp / 'progress' / 'batches').iterdir())) == 2)
        fresh_doc = json.loads((cur / 'run.json').read_text(encoding='utf-8'))
        ids = [t['id'] for t in fresh_doc['tasks']]
        check('新批只含本批任务', ids == ['BV1BATCH0003'], f'-> {ids}')

        # 追加任务（补数据、登记待编译）不该触发轮转
        ph.init_run([{'id': 'BV1BATCH0004', 'title': '丁'}], d=cur, group='wiki编译')
        check('追加任务不触发轮转',
              len(list((tmp / 'progress' / 'batches').iterdir())) == 2)
        rest = json.loads((cur / 'run.json').read_text(encoding='utf-8'))['tasks']
        check('追加后两份任务并存', len(rest) == 2, f'-> {[t["id"] for t in rest]}')

        # 批次名解析：只有 batches 下的直接子目录能被选中
        good = ph.list_batches(cur)[0]['name']
        check('合法批次名解析到该目录', ph.resolve_view_dir(good, cur).name == good)
        for bad in ('../../secret', '../secret', 'a/b', '', None, 'current', '不存在'):
            check('非法批次名回退当前批：%r' % bad,
                  ph.resolve_view_dir(bad, cur) == ph.run_dir(cur))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_verify_pack():
    """素材包校验：三种合法形态不误伤，六类破坏必须被拦"""
    import shutil
    import verify_pack as vpk
    BV = 'BV1TEST00001'
    tmp = Path(tempfile.mkdtemp(prefix='bili_vpack_test_'))
    try:
        head = ['# B站视频素材包（本地 ASR 转写）', '', '## 基本信息',
                '- 标题：测试视频：肇庆古城与利玛窦',
                f'- BV号：{BV} ｜ av号：1 ｜ cid：2 ｜ 分P：1/1',
                '- UP主：某某UP（mid:123）',
                '- 发布：2026-09-02 19:00 ｜ 时长：24:25',
                f'- 链接：https://www.bilibili.com/video/{BV}']
        meta = bili_asr.pack_meta({'route': 'subtitle:中文（AI 生成，可能有错字）',
                                   'engine': 'subtitle', 'source': '中文（AI 生成，可能有错字）',
                                   'ts_granularity': 'cue'})
        meta_only = bili_asr.pack_meta({'engine': 'meta-only'})
        subsec = ['', '## 字幕全文（来源：中文（AI 生成，可能有错字））', '',
                  '[00:00:12] 第一句。', '[00:00:15] 第二句。']
        good = '\n'.join(head + meta + subsec)

        def chk(name, text, want_err=None, want_warn=None):
            p = tmp / f'{name}.md'
            p.write_text(text, encoding='utf-8')
            e, w, _i = vpk.check_pack(p)
            ok = (not e) if want_err is None else any(want_err in x for x in e)
            if want_err is None and e:
                ok = False
            if want_warn is not None:
                ok = ok and any(want_warn in x for x in w)
            check(f'素材包校验 {name}', ok, f'-> errors={e} warnings={w}')

        chk('合格新包', good)
        chk('历史包（无元信息块）', good.replace('\n'.join(meta), ''), want_warn='缺 ## 元信息 块')
        chk('预扫描包', '\n'.join(head + meta_only))
        chk('无正文且无元信息块', '\n'.join(head), want_warn='无法区分预扫描包')
        chk('正文一行坏时间戳', good.replace('[00:00:12]', '[xx:xx:12]'),
            want_err='缺 [hh:mm:ss] 前缀')
        chk('engine 非法', good.replace('- engine: subtitle', '- engine: gpt4'),
            want_err='engine 取值非法')
        chk('元信息缺字段', good.replace('- audio_sec: -\n', ''),
            want_err='元信息缺字段: audio_sec')
        chk('墙钟耗时入包', good.replace('- audio_sec: -', '- audio_sec: -\n- asr_sec: 88.0'),
            want_warn='墙钟耗时')
        p = tmp / 'bili_BV9WRONG0001.md'
        p.write_text(good, encoding='utf-8')
        _e, _w, _i = vpk.check_pack(p)
        check('素材包校验 文件名 BV 不一致', any('不一致' in x for x in _w), f'-> {_w}')
        chk('两个正文节', good + '\n## 转写全文（本地 Whisper x）\n[00:00:01] 重复',
            want_err='个正文节')
        # ASR 路由（2026-09-16 补）：route=asr:<引擎>:<模型>@<设备>，第一段是路由类型不是引擎名
        asr_meta = bili_asr.pack_meta({'route': 'asr:funasr-nano:FunAudioLLM/Fun-ASR-Nano-2512@cuda',
                                       'engine': 'funasr-nano',
                                       'source': '本地 Fun-ASR-Nano（FunAudioLLM/Fun-ASR-Nano-2512）',
                                       'ts_granularity': 'segment(token)'})
        asr_body = ['', '## 转写全文（本地 Fun-ASR-Nano · cuda）', '', '[00:00:00] 第一句。']
        asr_good = '\n'.join(head + asr_meta + asr_body)
        chk('合格 ASR 包（asr 路由形态）', asr_good)
        chk('ASR 包引擎名不符', asr_good.replace('asr:funasr-nano', 'asr:whisper'),
            want_err='route 与 engine 不一致')
        chk('基本信息缺字段', good.replace('- 链接：', '- 链接X：'), want_err='基本信息缺字段')
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def test_finish_guards():
    """--finish 集成：归档前校验（坏包拒绝闭环且不动台账；好包回填并报 pack_warnings）"""
    import contextlib
    import io
    import shutil
    BV = 'BV1TEST00001'
    tmp = Path(tempfile.mkdtemp(prefix='bili_finish_test_'))
    saved_api = lq.lib_api
    try:
        cache = tmp / 'cache' / 'bili'
        raw = tmp / 'raw'
        cache.mkdir(parents=True)
        raw.mkdir(parents=True)
        row = {'record_id': 'r1', 'BV号': BV, '状态': '已转写',
               '视频链接': f'https://www.bilibili.com/video/{BV}'}
        calls = []

        def fake_api(token, script, args):
            if script == 'query_database_record.py':
                return {'results': [row]}
            calls.append(script)
            return {'ok': True}

        lq.lib_api = fake_api
        V = _fixture_video()
        pg = V['pages']
        head = bili_asr.pack_head(V, BV, 444555666, 1, pg, pg[0],
                                  f'https://www.bilibili.com/video/{BV}')
        meta = bili_asr.pack_meta({'route': 'subtitle:x', 'engine': 'subtitle',
                                   'source': 'x', 'ts_granularity': 'cue'})
        body = ['', '## 字幕全文（来源：x）', '', '[00:00:12] 第一句。']
        cache.joinpath(f'bili_{BV}.md').write_text('\n'.join(head + meta + body),
                                                   encoding='utf-8')

        def run_finish():
            calls.clear()
            old = sys.argv
            sys.argv = ['library_queue.py', '--token', 'tok', '--database-id', 'db',
                        '--raw-dir', str(raw), '--cache-dir', str(cache), '--finish', BV]
            buf = io.StringIO()
            try:
                with contextlib.redirect_stdout(buf):
                    lq.main()
            finally:
                sys.argv = old
            return buf.getvalue()

        out = run_finish()
        check('合格包：回填台账', 'batch_update_database_records.py' in calls, f'-> {out[:120]}')
        check('合格包：输出带 index_healed', 'index_healed' in out, f'-> {out[:200]}')
        check('合格包：已归档', (tmp / 'cache' / 'bili_subs' / f'bili_{BV}.md').is_file())
        check('合格包：pack_check=ok', '"pack_check": "ok"' in out, f'-> {out[:200]}')

        ap = tmp / 'cache' / 'bili_subs' / f'bili_{BV}.md'
        ap.write_text(ap.read_text(encoding='utf-8').replace('[00:00:12]', '[xx:xx:12]'),
                      encoding='utf-8')
        out = run_finish()
        check('坏包：拒绝闭环', '素材包校验未通过' in out, f'-> {out[:200]}')
        check('坏包：台账未被回填', calls == [], f'-> {calls}')

        # P0-3（2026-09-16）：verify_pack 不可用时必须显式声明，不得静默跳过校验
        saved_vp = lq.verify_pack
        try:
            lq.verify_pack = None
            out = run_finish()
            check('verify_pack 缺失：输出标记 pack_check=skipped',
                  '"pack_check": "skipped"' in out, f'-> {out[:220]}')
            check('verify_pack 缺失：降级不阻断主流程（台账仍回填）',
                  'batch_update_database_records.py' in calls, f'-> {calls}')
        finally:
            lq.verify_pack = saved_vp

    finally:
        lq.lib_api = saved_api
        shutil.rmtree(tmp, ignore_errors=True)


def test_dedup_skip():
    """查重命中 → 直接跳过，且不写台账（2026-09-16 起：状态列无「重复」选项）。"""
    import contextlib
    import io
    BV = 'BV1TESTDEDUP'
    tmp = Path(tempfile.mkdtemp(prefix='bili_dedup_test_'))
    saved_api = lq.lib_api
    try:
        cache = tmp / 'cache' / 'bili'
        raw = tmp / 'raw'
        cache.mkdir(parents=True)
        raw.mkdir(parents=True)
        link = f'https://www.bilibili.com/video/{BV}'
        rows = [
            {'record_id': 'r-done', 'BV号': BV, '状态': '已完成', '视频链接': link},
            {'record_id': 'r-again', '状态': '待处理', '视频链接': link},
        ]
        calls = []

        def fake_api(token, script, args):
            if script == 'query_database_record.py':
                return {'results': rows}
            calls.append(script)
            return {'ok': True}

        lq.lib_api = fake_api
        old = sys.argv
        sys.argv = ['library_queue.py', '--token', 'tok', '--database-id', 'db',
                    '--raw-dir', str(raw), '--cache-dir', str(cache)]
        buf = io.StringIO()
        rc = None
        try:
            with contextlib.redirect_stdout(buf):
                lq.main()
        except SystemExit as e:   # main() 以 sys.exit() 收尾，必须就地捕获
            rc = e.code
        finally:
            sys.argv = old
        out = buf.getvalue()
        rep = json.loads(out)
        check('查重命中：正常退出（无回写失败）', rc == 0, f'-> {rc}')
        dups = rep.get('duplicates') or []
        check('查重命中：进入 duplicates', len(dups) == 1, f'-> {rep}')
        check('查重命中：标出与哪条重复', dups and dups[0].get('dup_of') == 'r-done',
              f'-> {dups}')
        check('查重命中：不写台账（无 update 调用）', calls == [], f'-> {calls}')
        check('查重命中：不进 processed', not rep.get('processed'), f'-> {rep}')
    finally:
        lq.lib_api = saved_api
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)


def test_audit_fixes():
    """B/C 类修复的回归：失败判据、临时文件唯一性、术语表替换边界、YAML 形态、常量一致。"""
    class _P:
        def __init__(self, out, rc):
            self.stdout, self.stderr, self.returncode = out, '', rc

    # P0-1：插件把错误 JSON 写到 stdout 且退出码是 0，只看 stdout 是否为空会把失败当成功
    saved_run = lq.subprocess.run
    try:
        lq.subprocess.run = lambda *a, **k: _P('{"error": "查询记录请求失败"}', 0)
        caught = ''
        try:
            lq.lib_api('t', 'query_database_record.py', [])
        except Exception as e:
            caught = str(e)
        check('P0-1 错误 JSON + 退出码 0 判为失败', '调用失败' in caught, caught[:80])
        lq.subprocess.run = lambda *a, **k: _P('{"results": [], "has_more": false}', 0)
        check('P0-1 正常返回仍可解析',
              lq.lib_api('t', 'x.py', []) == {'results': [], 'has_more': False})
        lq.subprocess.run = lambda *a, **k: _P('not json', 0)
        caught = ''
        try:
            lq.lib_api('t', 'x.py', [])
        except Exception as e:
            caught = str(e)
        check('P0-1 非 JSON 输出判为失败', '不是 JSON' in caught, caught[:80])
    finally:
        lq.subprocess.run = saved_run

    # P0-2：批量描述文件必须带进程号，且跑完即删
    saved_batch = lq.run_bili
    seen = {}
    try:
        def _fake(args, timeout=7200):
            seen['path'] = Path(args[1])
            seen['existed'] = Path(args[1]).is_file()
            return {'results': []}
        lq.run_bili = _fake
        lq.run_bili_batch([{'url': 'u', 'out': 'o'}], 'large-v3-turbo')
        check('P0-2 批量描述文件带进程号',
              str(os.getpid()) in seen['path'].name and seen['existed'], seen['path'].name)
        check('P0-2 批量描述文件跑完即删', not seen['path'].exists(), seen['path'])
    finally:
        lq.run_bili = saved_batch

    # P0-3：术语表替换的三档行为 + 逐行豁免 + 备份
    import check_glossary as cg
    tmp = Path(tempfile.mkdtemp(prefix='cg_test_'))
    try:
        rows = [('耳塞', 'RSI', 'AI 语境', False), ('田园栋', '田渊栋', 'ALL', True)]
        p = tmp / 'a.md'
        p.write_text('今天试了三款降噪耳塞。田园栋发过论文。\n', encoding='utf-8')
        hits, _ = cg.scan_file(p, rows, fix=True)
        text = p.read_text(encoding='utf-8')
        check('P0-3 --fix 只替换 ALL 条目', '耳塞' in text and '田园栋' not in text, text)
        check('P0-3 未替换的条目仍报为待修',
              any(h['wrong'] == '耳塞' and h['action'] == '未替换' for h in hits))
        cg.scan_file(p, rows, fix=True, force=['耳塞'])
        check('P0-3 点名后才替换', 'RSI' in p.read_text(encoding='utf-8'))
        check('P0-3 改写前留了 .bak', p.with_name('a.md.bak').is_file())
        p2 = tmp / 'b.md'
        p2.write_text('耳塞本就该是耳塞。<!-- glossary:ignore -->\n', encoding='utf-8')
        hits2, changed2 = cg.scan_file(p2, rows, fix=True, force_all=True)
        check('P0-3 逐行豁免生效',
              not hits2 and not changed2 and 'RSI' not in p2.read_text(encoding='utf-8'))
    finally:
        import shutil as _sh
        _sh.rmtree(tmp, ignore_errors=True)

    # P1-6：frontmatter 的两种列表形态都要能读出来
    import verify_structure as vs
    inline = 'tags: [a, b, c, d, e, f]\nentities: [a, b, c, d, e, f, g, h]\n'
    block = 'tags:\n  - a\n  - b\n  - c\nentities:\n  - x\n  - y\n'
    check('P1-6 内联数组形态', vs._yaml_list(inline, 'tags') == (['a', 'b', 'c', 'd', 'e', 'f'], 'inline'),
          vs._yaml_list(inline, 'tags'))
    check('P1-6 块状列表形态', vs._yaml_list(block, 'entities') == (['x', 'y'], 'block'),
          vs._yaml_list(block, 'entities'))
    check('P1-6 读不出时标 missing（不再静默跳过）',
          vs._yaml_list('tags:\n', 'tags') == ([], 'missing'))

    # P2-5：WBI 混洗表在 Python 与 JS 各一份，必须逐项一致
    import re as _re
    sp = Path(__file__).resolve().parent.parent / 'scripts'
    py_src = (sp / 'bili_wbi.py').read_text(encoding='utf-8')
    js_src = (sp / 'bili_wbi.mjs').read_text(encoding='utf-8')
    m_py = _re.search(r'MIXIN\s*=\s*\[([^\]]+)\]', py_src)
    m_js = _re.search(r'MIXIN\s*=\s*\[([^\]]+)\]', js_src)
    check('P2-5 两份 MIXIN 都能读到', bool(m_py and m_js))
    if m_py and m_js:
        py_nums = [int(x) for x in _re.findall(r'\d+', m_py.group(1))]
        js_nums = [int(x) for x in _re.findall(r'\d+', m_js.group(1))]
        check('P2-5 两份 MIXIN 逐项一致', py_nums == js_nums,
              f'py={len(py_nums)} js={len(js_nums)}')

    # C3：过期阈值按阶段取值
    check('C3 转写阶段仍是 10/30', ph.stale_thresholds('asr') == (10, 30),
          ph.stale_thresholds('asr'))
    check('C3 生成纪要阶段放宽到 300/900', ph.stale_thresholds('notes') == (300, 900),
          ph.stale_thresholds('notes'))
    check('C3 未知阶段回落兜底值',
          ph.stale_thresholds('no-such') == (ph.STALE_WARN_SEC, ph.STALE_BAD_SEC))


def main():
    print('=== bilibili-video-summary 自测 ===')
    for fn in (test_resolve_input, test_merge_segments, test_strip_prompt_leak,
               test_mark_ads, test_pack_sections, test_pack_pipeline, test_index_heal,
               test_verify_pack, test_finish_guards, test_dedup_skip, test_progress_hub,
               test_parse_duration, test_notes_done, test_batch_rotate, test_audit_fixes):
        if lq is None and fn.__name__ in NEED_LQ:
            print(f'-- {fn.__name__}  [跳过：本副本无 library_queue.py（便携版）]')
            continue
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
