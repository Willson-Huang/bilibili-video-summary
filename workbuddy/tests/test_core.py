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

try:
    import library_queue as lq       # 便携版不含该脚本（WorkBuddy 资料库专用）→ 对应用例跳过
except Exception:
    lq = None

NEED_LQ = ('test_index_heal', 'test_finish_guards', 'test_dedup_skip')

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
    'S1_subtitle': '5ef817e06c607312fbe85f03dd610501a033fe56fff41118738233cd682bdefe',
    'S2_whisper_anon': '6344533872c5683244f5ac78e3ddaeadc6d589a0eb23686bfb3f88bae61a0650',
    'S3_only_meta': '3cf4be8fb30b6df769b2da586670c1313f3659649da54cf24470dc538197c6bb',
    'S4_force_asr_multipage': 'c4053695935a34dea15c76a8f3bd8d03456fd8acb63fb1073eeec288df0960b4',
    'S5_nano_preset': '3051a5d0d057bf4d240bea8bad9dc456d3219367be66dae97e09446b1675a073',
}
_STUBBED = ('resolve_input', 'fetch_meta', 'fetch_player', 'fetch_subtitle_text',
            'fetch_comments', 'load_ad_keywords', 'load_ad_excludes', 'transcribe_with',
            'ModelHolder', 'download_audio', '_prog')


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


def main():
    print('=== bilibili-video-summary 自测 ===')
    for fn in (test_resolve_input, test_merge_segments, test_mark_ads,
               test_pack_sections, test_pack_pipeline, test_index_heal, test_verify_pack,
               test_finish_guards, test_dedup_skip, test_progress_hub, test_parse_duration):
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
