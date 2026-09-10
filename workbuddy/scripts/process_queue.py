#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
B站视频转录队列处理

扫描台账 CSV，自动补全视频元信息并转写，回写状态。

用法:
  python process_queue.py --init              初始化台账（含一条示例行）
  python process_queue.py --status            查看队列状态（只读）
  python process_queue.py --meta-only         只补元信息，不转写（每个几秒）
  python process_queue.py                     补元信息 + 转写（默认）
  python process_queue.py --limit 3           本次最多处理 3 条
  python process_queue.py --bvid BV1xx        只处理指定视频
  python process_queue.py --model large-v3    指定转写模型
  python process_queue.py --engine funasr-nano --hotwords <文件>   用 Nano 引擎 + 热词（中文专名更准）
  python process_queue.py --csv <path>        指定台账路径

状态流转:
  (空)/待处理 → 已转写 → 已完成 ；异常为 失败:<原因>
"""
import argparse
import csv
import json
import os
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
PY = Path(os.environ.get('BILI_PYTHON', sys.executable))

DEFAULT_CSV = Path(os.environ.get(
    'BILI_CSV', str(Path.home() / 'obsidian' / 'bilibili_queue.csv')))

COLS = ['序号', '视频链接', 'BV号', 'UP主', '视频标题', '时长', '状态',
        '转写耗时(秒)', '素材包', '纪要', '处理时间', '备注']

ST_PENDING = '待处理'
ST_TRANSCRIBED = '已转写'
ST_DONE = '已完成'


def env():
    e = dict(os.environ)
    e['PYTHONPATH'] = ''
    e['HF_ENDPOINT'] = 'https://hf-mirror.com'
    e['HF_HUB_DISABLE_SYMLINKS'] = '1'
    e['HF_HUB_DISABLE_XET'] = '1'
    return e


def run_bili(args, timeout=3600):
    cmd = [str(PY), str(BILI_ASR)] + args
    p = subprocess.run(cmd, capture_output=True, text=True,
                       encoding='utf-8', errors='replace',
                       env=env(), timeout=timeout)
    out = (p.stdout or '').strip()
    if not out:
        raise RuntimeError('无输出: ' + (p.stderr or '')[-500:])
    j = json.loads(out)
    if not j.get('ok'):
        raise RuntimeError(j.get('error', '未知错误'))
    return j


def load(csv_path):
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    if not csv_path.exists():
        return []
    with open(csv_path, encoding='utf-8-sig', newline='') as f:
        return list(csv.DictReader(f))


def save(csv_path, rows):
    csv_path.parent.mkdir(parents=True, exist_ok=True)
    with open(csv_path, 'w', encoding='utf-8-sig', newline='') as f:
        w = csv.DictWriter(f, fieldnames=COLS)
        w.writeheader()
        for i, r in enumerate(rows, 1):
            r['序号'] = str(i)
            w.writerow({k: r.get(k, '') for k in COLS})


def init_csv(csv_path):
    transcript_dir = csv_path.parent / 'transcripts'
    note = (transcript_dir / 'bili_BV0000000000.md').as_posix()
    rows = [{
        '序号': '1',
        '视频链接': 'https://www.bilibili.com/video/BV0000000000',
        'BV号': 'BV0000000000',
        'UP主': '示例UP主',
        '视频标题': '示例视频标题',
        '时长': '0:00:00',
        '状态': ST_PENDING,
        '转写耗时(秒)': '0',
        '素材包': note,
        '纪要': (csv_path.parent / 'notes' / '示例纪要.md').as_posix(),
        '处理时间': '',
        '备注': '示例行，可删除',
    }]
    save(csv_path, rows)
    print(f'台账已初始化: {csv_path}')


def show_status(csv_path):
    rows = load(csv_path)
    if not rows:
        print('台账为空或不存在，先跑 --init')
        return
    stat = {}
    for r in rows:
        s = (r.get('状态') or '').strip() or '（新链接）'
        stat[s] = stat.get(s, 0) + 1
    print(f'共 {len(rows)} 条')
    for s, n in stat.items():
        print(f'  {s}: {n}')
    print()
    for r in rows:
        print(f"  [{r.get('序号')}] {r.get('状态') or '（新链接）':8s} "
              f"{r.get('UP主') or '-':16s} {(r.get('视频标题') or r.get('视频链接') or '-')[:40]}")


def process(csv_path, do_transcribe, limit, only_bvid, model, engine='whisper', hotwords=None):
    rows = load(csv_path)
    if not rows:
        print('台账为空，先跑 --init 或手动粘贴链接')
        return

    targets = []
    for idx, r in enumerate(rows):
        st = (r.get('状态') or '').strip()
        link = (r.get('视频链接') or '').strip()
        if not link:
            continue
        if only_bvid and only_bvid not in link:
            continue
        if st in (ST_DONE,):
            continue
        # 已转写的行不重复转写：等 AI 写完纪要再收尾，避免白白重跑 ASR
        if st == ST_TRANSCRIBED:
            continue
        if st.startswith('失败'):
            continue
        targets.append((idx, r))

    if limit:
        targets = targets[:limit]

    if not targets:
        print('没有需要处理的条目')
        return

    print(f'待处理 {len(targets)} 条，转写={"开" if do_transcribe else "关"}\n')
    transcript_dir = csv_path.parent / 'transcripts'
    transcript_dir.mkdir(parents=True, exist_ok=True)
    notes_dir = csv_path.parent / 'notes'
    notes_dir.mkdir(parents=True, exist_ok=True)

    tmp_dir = Path(tempfile.gettempdir())

    for n, (idx, r) in enumerate(targets, 1):
        link = r['视频链接'].strip()
        print(f'--- [{n}/{len(targets)}] {link}')
        t0 = time.time()
        try:
            meta = run_bili([link, '--only-meta', '--out', str(tmp_dir / '_bili_meta_probe.md')])
            bvid = meta['bvid']
            rows[idx]['BV号'] = bvid
            rows[idx]['UP主'] = meta['up']
            rows[idx]['视频标题'] = meta['title']
            rows[idx]['时长'] = meta['duration']
            rows[idx]['处理时间'] = datetime.now().strftime('%Y-%m-%d %H:%M')
            print(f"    {meta['up']} | {meta['title']} | {meta['duration']}")

            if do_transcribe:
                out_md = transcript_dir / f'bili_{bvid}.md'
                # 引擎/热词透传：不传则队列会悄悄跑回 whisper
                extra = []
                if engine and engine != 'whisper':
                    extra += ['--engine', engine]
                if hotwords:
                    extra += ['--hotwords', hotwords]
                print(f'    转写中（{engine}/{model}）...')
                res = run_bili([link, '--model', model, '--out', str(out_md)] + extra)
                rows[idx]['转写耗时(秒)'] = str(round(res.get('asr', {}).get('asr_sec', 0)))
                rows[idx]['素材包'] = out_md.as_posix()
                rows[idx]['状态'] = ST_TRANSCRIBED
                print(f"    转写完成 {res.get('asr', {}).get('asr_sec')}s -> {out_md.name}")
            else:
                rows[idx]['状态'] = ST_PENDING

            # 备注列由用户维护，成功时不得清空——否则批量跑一轮就把手填内容全抹了
        except Exception as e:
            rows[idx]['状态'] = '失败:' + str(e)[:60]
            # 失败记录追加而非覆盖，保留用户原有备注
            prev_note = (rows[idx].get('备注') or '').strip()
            err = '失败原因: ' + str(e)[:120]
            rows[idx]['备注'] = f'{prev_note} | {err}' if prev_note else err
            print(f'    失败: {e}')

        save(csv_path, rows)
        print(f'    台账已更新（{round(time.time() - t0)}s）\n')

    print('本轮处理结束，状态：')
    show_status(csv_path)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--csv', default=str(DEFAULT_CSV))
    ap.add_argument('--init', action='store_true')
    ap.add_argument('--status', action='store_true')
    ap.add_argument('--meta-only', action='store_true')
    ap.add_argument('--limit', type=int, default=0)
    ap.add_argument('--bvid', default=None)
    ap.add_argument('--model', default='large-v3-turbo')
    ap.add_argument('--engine', default='whisper', choices=['whisper', 'funasr-nano'],
                    help='本地 ASR 引擎，透传给 bili_asr.py')
    ap.add_argument('--hotwords', default=None, help='热词文件路径，透传给 bili_asr.py（仅 Nano 生效）')
    a = ap.parse_args()

    csv_path = Path(a.csv)
    if a.init:
        init_csv(csv_path)
    elif a.status:
        show_status(csv_path)
    else:
        process(csv_path, not a.meta_only, a.limit, a.bvid, a.model,
                getattr(a, 'engine', 'whisper'), getattr(a, 'hotwords', None))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print('ERROR:', e)
        sys.exit(1)
