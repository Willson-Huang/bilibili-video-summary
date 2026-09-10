#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
资料库在线表队列处理：读台账 → 查重 → 补元信息/转写 → 回写状态 → 产物落盘

用法:
  python library_queue.py --token <op_token>                 完整处理（补信息 + 转写）
  python library_queue.py --token <op_token> --meta-only     只查重 + 补元信息，不转写
  python library_queue.py --token <op_token> --limit 3       本次最多处理 3 条
  python library_queue.py --token <op_token> --engine funasr-nano --hotwords <文件>
                                                             中文专名密集批次改用 Nano 引擎（不传则 whisper）
  python library_queue.py --token <op_token> --database-id <id>
  python library_queue.py --token <op_token> --raw-dir <path>      纪要落盘目录
  python library_queue.py --token <op_token> --cache-dir <path>    素材包缓存目录
  python library_queue.py --token <op_token> --note-name BVxxx     只输出该 BV 的标准纪要文件名
  python library_queue.py --token <op_token> --finish BVxxx \
      --summary <纪要路径> --ima 已转存                             收尾：回写台账 + 归档素材包

行为:
  1. 读取台账全部记录
  2. 对「状态为空 / 待处理」的行解析 BV号
  3. 查重：BV号 已出现在其它行 → 状态写「重复」并跳过，不再本地跑
  4. 未重复：补元信息 → 转写（可选）→ 回写字段与状态
  5. 素材包落 --cache-dir（默认 $BILI_CACHE_DIR 或 ~/.workbuddy/cache/bili），不进知识库
  6. AI 生成纪要后落 --raw-dir（默认 $BILI_RAW_DIR 或 ~/obsidian/raw），命名见 note_name()
  7. --finish 收尾：只有素材包归档成功（已在 bili_subs 或成功 move）才置「已完成」；归档失败不动台账
  8. 台账回写攒批提交（每 20 条或结尾 flush）；回写失败项进 report.write_failed 并以非零退出
  9. 输出 JSON，processed[].note_name 给出标准纪要文件名

状态列取值：待处理 / 已转写 / 已完成 / 失败 / 重复
"""
import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
from datetime import datetime
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

# 路径从本文件推导 + 环境变量覆盖，避免写死机器相关路径
SKILL_DIR = Path(__file__).resolve().parent
BILI_ASR = SKILL_DIR / 'bili_asr.py'
PY_ASR = Path(os.environ.get('BILI_PYTHON_ASR', sys.executable))
PY_LIB = Path(os.environ.get('BILI_PYTHON_LIB', sys.executable))


def _pick_lib_dir():
    """定位资料库插件目录：自动取 skill-library 下最新版本，避免写死版本号。"""
    base = Path(os.environ.get('BILI_LIBRARY_BASE', str(
        Path.home() / '.workbuddy' / 'plugins' / 'cache' / 'workbuddy-builtin' / 'skill-library')))
    if not base.is_dir():
        return base
    cands = [d for d in base.iterdir() if d.is_dir()]
    if not cands:
        return base

    def _ver(p):
        nums = re.findall(r'\d+', p.name)
        return tuple(int(n) for n in nums) if nums else (0,)

    return max(cands, key=_ver)


LIB = Path(os.environ.get('BILI_LIBRARY_PLUGIN', str(_pick_lib_dir())))

# 台账 database_id 属私有资源，用环境变量注入，仓库内只留占位符
DEFAULT_DB = os.environ.get('BILI_DATABASE_ID', '<YOUR_DATABASE_ID>')
DEFAULT_RAW = Path(os.environ.get('BILI_RAW_DIR', str(Path.home() / 'obsidian' / 'raw')))
# 素材包（转写全文）不进知识库，落缓存目录，--finish 收尾时归档到 bili_subs
DEFAULT_CACHE = Path(os.environ.get('BILI_CACHE_DIR', str(
    Path.home() / '.workbuddy' / 'cache' / 'bili')))

ST_PENDING = '待处理'
ST_TRANSCRIBED = '已转写'
ST_DONE = '已完成'
ST_DUP = '重复'

INVALID_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]')


def note_name(pubdate, title, up):
    """标准纪要文件名：<发布日>_<完整标题>_<UP主>_纪要.md

    标题原文照抄，只剔除 Windows 非法字符与换行空白，全角标点（，？【】）保留。
    """
    base = f'{pubdate}_{title}_{up}_纪要'
    base = INVALID_CHARS.sub('', base)
    base = re.sub(r'\s+', ' ', base).strip()
    return base + '.md'


def cache_index(cache_dir):
    return Path(cache_dir) / 'index.json'


def load_index(cache_dir):
    p = cache_index(cache_dir)
    if p.exists():
        try:
            return json.loads(p.read_text(encoding='utf-8'))
        except Exception:
            return {}
    return {}


def save_index(cache_dir, data):
    p = cache_index(cache_dir)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding='utf-8')


def extract_pubdate(md_path):
    """从素材包正文里取发布日，失败返回空串。"""
    try:
        txt = Path(md_path).read_text(encoding='utf-8', errors='replace')
    except Exception:
        return ''
    m = re.search(r'发布：(\d{4}-\d{2}-\d{2})', txt)
    return m.group(1) if m else ''


def lib_api(token, script, args):
    cmd = [str(PY_LIB), str(LIB / 'database' / script), '--token-stdin'] + args
    p = subprocess.run(cmd, input=token, capture_output=True,
                       text=True, encoding='utf-8', errors='replace', timeout=120)
    out = (p.stdout or '').strip()
    if not out:
        raise RuntimeError(f'{script} 无输出: ' + (p.stderr or '')[-400:])
    return json.loads(out)


def engine_args(a):
    """把 --engine / --hotwords 透传给 bili_asr.py（避免队列悄悄跑回 whisper）"""
    out = []
    if getattr(a, 'engine', 'whisper') and a.engine != 'whisper':
        out += ['--engine', a.engine]
    if getattr(a, 'hotwords', None):
        out += ['--hotwords', a.hotwords]
    return out


def run_bili(args, timeout=3600):
    e = dict(os.environ)
    e['PYTHONPATH'] = ''
    e['HF_ENDPOINT'] = 'https://hf-mirror.com'
    e['HF_HUB_DISABLE_SYMLINKS'] = '1'
    e['HF_HUB_DISABLE_XET'] = '1'
    p = subprocess.run([str(PY_ASR), str(BILI_ASR)] + args,
                       capture_output=True, text=True, encoding='utf-8',
                       errors='replace', env=e, timeout=timeout)
    out = (p.stdout or '').strip()
    if not out:
        raise RuntimeError('无输出: ' + (p.stderr or '')[-500:])
    j = json.loads(out)
    if not j.get('ok'):
        raise RuntimeError(j.get('error', '未知错误'))
    return j


def run_bili_batch(items, model, timeout=7200, extra=None):
    """items: [{'url':..., 'out':...}]。整批只加载一次模型。extra 为透传参数（engine/hotwords）。"""
    tmp = Path(tempfile.gettempdir()) / '_bili_batch.json'
    tmp.write_text(json.dumps(items, ensure_ascii=False), encoding='utf-8')
    j = run_bili(['--batch-file', str(tmp), '--model', model] + (extra or []), timeout=timeout)
    return j.get('results', [])


def update(token, database_id, records):
    """records: [{'record_id':..., 'properties': {...}}]"""
    return lib_api(token, 'batch_update_database_records.py',
                   ['--database-id', database_id, '--records',
                    json.dumps(records, ensure_ascii=False)])


def extract_bvid(link):
    if not link:
        return None
    m = re.search(r'(BV[0-9A-Za-z]{10})', link)
    if m:
        return m.group(1)
    m = re.search(r'(?:av|aid)[=/]?(\d{1,12})', link, re.I)
    return 'av' + m.group(1) if m else None


def field_text(row, name):
    """从 query 结果的一行里取字段纯文本"""
    v = row.get(name)
    if v is None:
        return ''
    if isinstance(v, dict):
        for k in ('text', 'value', 'name', 'content'):
            if k in v and isinstance(v[k], str):
                return v[k]
        return ''
    return str(v)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--token', required=True)
    ap.add_argument('--database-id', default=DEFAULT_DB)
    ap.add_argument('--raw-dir', default=str(DEFAULT_RAW), help='纪要落盘目录')
    ap.add_argument('--cache-dir', default=str(DEFAULT_CACHE), help='素材包缓存目录')
    ap.add_argument('--meta-only', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--up', help='只处理指定 UP主（按 UP主 列精确匹配）')
    ap.add_argument('--model', default='large-v3-turbo')
    ap.add_argument('--engine', default='whisper', choices=['whisper', 'funasr-nano'],
                    help='本地 ASR 引擎，透传给 bili_asr.py；Nano 适合专名密集内容')
    ap.add_argument('--hotwords', default=None, help='热词文件路径，透传给 bili_asr.py（仅 Nano 生效）')
    ap.add_argument('--finish', help='收尾模式：指定 BV号，回填纪要、状态置已完成、归档素材包到 bili_subs')
    ap.add_argument('--summary', help='纪要路径或链接，配合 --finish 使用')
    ap.add_argument('--ima', help='IMA转存状态：已转存 / 失败 / 不适用，配合 --finish 使用')
    ap.add_argument('--note-name', help='只输出该 BV号 的标准纪要文件名，不改动台账')
    a = ap.parse_args()

    raw_dir = Path(a.raw_dir)
    raw_dir.mkdir(parents=True, exist_ok=True)
    cache_dir = Path(a.cache_dir)
    cache_dir.mkdir(parents=True, exist_ok=True)

    # 1. 读台账
    res = lib_api(a.token, 'query_database_record.py',
                  ['--database-id', a.database_id, '--page-size', '200'])
    results = res.get('results', [])

    # 2. 建立已处理索引：BV号 -> record_id
    handled = {}
    for r in results:
        rid = r.get('record_id')
        bv = field_text(r, 'BV号') or extract_bvid(field_text(r, '视频链接'))
        st = field_text(r, '状态')
        if bv and st in (ST_TRANSCRIBED, ST_DONE, ST_DUP):
            handled.setdefault(bv, rid)

    # 2.4 只查标准纪要文件名（不碰台账）
    if a.note_name:
        idx = load_index(cache_dir)
        item = idx.get(a.note_name.strip())
        if not item:
            print(json.dumps({'error': f'缓存索引里没有 {a.note_name}，'
                                       f'请先转写或手工补 index.json'},
                             ensure_ascii=False))
            return
        name = note_name(item.get('pubdate', ''), item.get('title', ''),
                         item.get('up', ''))
        print(json.dumps({'bvid': a.note_name.strip(), 'note_name': name,
                          'note_path': str(raw_dir / name)},
                         ensure_ascii=False, indent=2))
        return

    # 2.5 收尾模式：AI 生成纪要后回填 + 归档素材包
    if a.finish:
        want = a.finish.strip()
        hits = [r for r in results
                if (field_text(r, 'BV号') or extract_bvid(field_text(r, '视频链接'))) == want]
        if not hits:
            print(json.dumps({'error': f'未找到 {want}'}, ensure_ascii=False))
            return
        today = datetime.now().strftime('%Y-%m-%d')

        # 素材包归档保留：从 cache/bili 挪到 cache/bili_subs（误识修正唯一依据，删除即放弃修正能力）
        # 归档目录固定在 cache_dir 同级 bili_subs，不改动 index 结构
        # 只有「已在归档目录」或「成功移动到归档目录」才算归档成功，才允许置「已完成」。
        archive_dir = cache_dir.parent / 'bili_subs'
        archive_dir.mkdir(parents=True, exist_ok=True)
        idx = load_index(cache_dir)
        item = idx.get(want, {})
        dst = archive_dir / f'bili_{want}.md'
        errs = []
        archived = None
        if dst.is_file():
            # 已在归档目录（历史批次或此前已归档）
            archived = str(dst)
        else:
            for p in dict.fromkeys([item.get('transcript', ''), str(cache_dir / f'bili_{want}.md')]):
                if not p:
                    continue
                fp = Path(p)
                if not fp.is_file():
                    continue
                if fp.resolve() == dst.resolve():
                    archived = str(dst)
                    break
                try:
                    shutil.move(str(fp), str(dst))
                    archived = str(dst)
                    break
                except Exception as e:
                    errs.append(f'{fp.name}: {e}')
            if archived is None and not errs:
                errs.append('素材包不存在（缓存与归档目录都没有）')

        if archived is None:
            # 归档失败：不动台账，保留原状态，等人工处理后重跑 --finish
            print(json.dumps({'error': f'素材包归档失败：{want}',
                              'archive_errors': errs,
                              'hint': '请检查 cache/bili 或 bili_subs 下的素材包后重跑 --finish；台账状态未改动'},
                             ensure_ascii=False, indent=2))
            return

        props = {'状态': {'select': ST_DONE}, '处理时间': {'date': today},
                 '素材包': {'text': archived}}
        if a.summary:
            props['纪要'] = {'text': a.summary}
        if a.ima:
            props['IMA转存'] = {'select': a.ima}
        item['transcript'] = archived
        idx[want] = item
        save_index(cache_dir, idx)

        update(a.token, a.database_id,
               [{'record_id': hits[0]['record_id'], 'properties': props}])
        print(json.dumps({'finished': want, 'archived': archived,
                          'properties': props}, ensure_ascii=False, indent=2))
        return

    # 3. 挑出待处理行
    targets = []
    for r in results:
        rid = r.get('record_id')
        st = field_text(r, '状态')
        link = field_text(r, '视频链接').strip()
        if not link or st in ('已完成', '失败', ST_DUP):
            continue
        if st == ST_TRANSCRIBED and not a.meta_only:
            continue
        if a.up and field_text(r, 'UP主').strip() != a.up:
            continue
        targets.append((rid, link, st))

    if a.limit:
        targets = targets[:a.limit]

    report = {'total': len(results), 'pending': len(targets),
              'duplicates': [], 'processed': [], 'failed': []}

    if not targets:
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return

    print(f'待处理 {len(targets)} 条，转写={"关" if a.meta_only else "开"}\n', file=sys.stderr)

    probe = Path(tempfile.gettempdir()) / '_bili_probe.md'
    today = datetime.now().strftime('%Y-%m-%d')
    stage1 = []  # [(rid, link, bv, meta)] 待转写

    # 台账回写攒批：每 20 条 flush 一次，减少 batch_update API 往返
    # flush 返回失败 record_id；失败项会从 processed 剔除并写入 report.write_failed，
    # 避免「转写完成但台账未回填」被误判闭环。
    pending = []
    PENDING_MAX = 20
    write_failed_ids = set()

    def queue_update(rec):
        pending.append(rec)
        if len(pending) >= PENDING_MAX:
            write_failed_ids.update(flush_updates())

    def flush_updates():
        if not pending:
            return []
        failed = []
        try:
            update(a.token, a.database_id, pending)
        except Exception as e:
            # 整批失败时逐条重试，坏记录单独失败不影响其余
            print(f'    批量回写失败（{len(pending)}条）：{str(e)[:120]}，逐条重试', file=sys.stderr)
            for rec in pending:
                try:
                    update(a.token, a.database_id, [rec])
                except Exception:
                    failed.append(rec['record_id'])
        pending.clear()
        return failed

    # 阶段一：查重 + 补元信息（不加载模型，每条 1-2s）
    for n, (rid, link, st) in enumerate(targets, 1):
        bv = extract_bvid(link)
        print(f'--- [{n}/{len(targets)}] {link}', file=sys.stderr)

        if bv and bv in handled:
            rec = {
                'record_id': rid,
                'properties': {
                    'BV号': {'text': bv},
                    '状态': {'select': ST_DUP},
                    '处理时间': {'date': today},
                    '备注': {'text': '与已有记录重复，未重复处理'},
                },
            }
            queue_update(rec)
            report['duplicates'].append({'record_id': rid, 'bvid': bv, 'link': link})
            print('    重复，已标记，跳过', file=sys.stderr)
            continue

        try:
            meta = run_bili([link, '--only-meta', '--out', str(probe)])
            bv = meta['bvid']
            print(f"    {meta['up']} | {meta['title']} | {meta['duration']}", file=sys.stderr)
            if a.meta_only:
                props = {'BV号': {'text': bv}, 'UP主': {'text': meta['up']},
                         '视频标题': {'text': meta['title']}, '时长': {'text': meta['duration']},
                         '处理时间': {'date': today}, '状态': {'select': ST_PENDING}}
                queue_update({'record_id': rid, 'properties': props})
                report['processed'].append({'record_id': rid, 'bvid': bv,
                                            'title': meta.get('title'),
                                            'up': meta.get('up'), 'note_name': ''})
                print('    台账已更新', file=sys.stderr)
            else:
                stage1.append((rid, link, bv, meta))
        except Exception as e:
            msg = str(e)[:120]
            try:
                queue_update({'record_id': rid, 'properties': {
                    '状态': {'select': '失败'}, '备注': {'text': msg}}})
            except Exception:
                pass
            report['failed'].append({'record_id': rid, 'link': link, 'error': msg})
            print(f'    失败: {msg}', file=sys.stderr)

    # 阶段二：批量转写，整批只加载一次模型
    if stage1:
        batch = [{'url': link, 'out': str(cache_dir / f'bili_{bv}.md')}
                 for rid, link, bv, meta in stage1]
        print(f'\n批量转写 {len(batch)} 条（模型只加载一次，engine={a.engine}）...', file=sys.stderr)
        t0 = time.time()
        results = run_bili_batch(batch, a.model, extra=engine_args(a))
        print(f'批量转写完成，用时 {round(time.time() - t0)}s\n', file=sys.stderr)

        for (rid, link, bv, meta), r in zip(stage1, results):
            if not r.get('ok'):
                msg = str(r.get('error', '转写失败'))[:120]
                try:
                    queue_update({'record_id': rid, 'properties': {
                        '状态': {'select': '失败'}, '备注': {'text': msg}}})
                except Exception:
                    pass
                report['failed'].append({'record_id': rid, 'link': link, 'error': msg})
                print(f'    {bv} 转写失败: {msg}', file=sys.stderr)
                continue

            out_md = Path(r['out'])
            props = {'BV号': {'text': bv}, 'UP主': {'text': meta['up']},
                     '视频标题': {'text': meta['title']}, '时长': {'text': meta['duration']},
                     '处理时间': {'date': today},
                     # 字幕路由下 asr 为 null（未跑 Whisper），必须容错，否则回填崩溃
                     '转写耗时(秒)': {'number': round((r.get('asr') or {}).get('asr_sec', 0))},
                     '素材包': {'text': str(out_md)},
                     '状态': {'select': ST_TRANSCRIBED}}
            pubdate = extract_pubdate(out_md) or meta.get('pubdate', '') or today
            idx = load_index(cache_dir)
            idx[bv] = {'pubdate': pubdate, 'title': meta.get('title', ''),
                       'up': meta.get('up', ''), 'transcript': str(out_md)}
            save_index(cache_dir, idx)
            nm = note_name(pubdate, meta.get('title', ''), meta.get('up', ''))
            queue_update({'record_id': rid, 'properties': props})
            handled[bv] = rid
            report['processed'].append({'record_id': rid, 'bvid': bv,
                                        'title': meta.get('title'),
                                        'up': meta.get('up'), 'note_name': nm})
            print(f"    {bv} 转写完成 -> {out_md.name}（缓存，--finish 收尾时归档）", file=sys.stderr)

    write_failed_ids.update(flush_updates())
    # 回写失败的项从 processed 剔除，进 write_failed——台账没回填 ≠ 闭环成功
    if write_failed_ids:
        report['processed'] = [p for p in report['processed']
                               if p.get('record_id') not in write_failed_ids]
        report['write_failed'] = sorted(write_failed_ids)
    else:
        report['write_failed'] = []
    print(json.dumps(report, ensure_ascii=False, indent=2))
    # 有回写失败时以非零退出，提示调用方（转写产物已落盘，台账需补回填）
    sys.exit(1 if write_failed_ids else 0)


if __name__ == '__main__':
    main()
