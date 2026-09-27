#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
任务进度看板（stdlib only，零第三方依赖）

设计要点
--------
写入端：管线任意进程调用 emit()，把自己进程的进度事件追加到
        <run_dir>/events_<pid>.jsonl。**每进程独立文件**，不抢锁、不会交错损坏，
        进程崩了也只丢自己那一份。
读取端：--serve 起一个 HTTP 服务，聚合 run.json + 未退役的 events_*.jsonl +
        state_snapshot.json，输出 /api/state 给 dashboard.html 轮询。

为什么不用现成方案：GitHub 上的进度项目（tqdm / rich / enlighten / textual）
都是"终端里的进度条"，成不了独立窗口；引入 textual 还要在有依赖冲突风险的
venv 里装包。本方案零依赖、样式自由，且能同时承载转写与纪要两类任务。

用法
----
  python progress_hub.py --init --tasks tasks.json --title "批次二"
  python progress_hub.py --serve --port 8765 --open
  python progress_hub.py --emit --task BV1xxx --stage asr --pct 42.5
  python progress_hub.py --emit --task BV1xxx --stage notes --note 生成纪要中
  python progress_hub.py --emit --task BV1xxx --stage done --elapsed 812.4
  python progress_hub.py --compact            # 折叠历史事件，退役旧文件
  python progress_hub.py --reset              # 清空

环境变量
--------
  BILI_PROGRESS_DIR   运行目录，默认 ~/obsidian/progress/current
"""
import argparse
import json
import os
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_DIR = Path(os.environ.get(
    'BILI_PROGRESS_DIR',
    str(Path.home() / 'obsidian' / 'progress' / 'current')))

STAGE_LABEL = {
    'queued': '排队', 'download': '下载音频', 'convert': '转码',
    'asr': '转写中', 'transcribed': '待生成纪要', 'notes': '生成纪要',
    'done': '已完成', 'fail': '失败',
}
# 阶段序：只允许向前推进，迟到的旧阶段事件不会把显示拉回去。
# done 与 fail 同阶。转写脚本只发到 transcribed，「已完成」由写完纪要的一方发出 ——
# 若让转写脚本直接发 done，终态锁会把后到的 notes 吞掉，看板会提前宣布完成。
STAGE_RANK = {'queued': 0, 'download': 1, 'convert': 2, 'asr': 3,
              'transcribed': 4, 'notes': 5, 'done': 8, 'fail': 8}
TERMINAL = ('done', 'fail')
# 进行中阶段：这些阶段的「已用时间」可以从事件时刻外推
ACTIVE_STAGES = ('download', 'convert', 'asr', 'notes')
# 停更超过该秒数就不再外推时间（前端用同一个值，见 dashboard.html 的 STALE_CAP）
STALE_CAP_SEC = 5.0
# 转写倍率常量：按单条标定（见 references/perf-benchmark-2026-09-13.md）。
# 批量实测只有约 2.1x，照常量推算会把「预计剩余」压到 0，故仅在样本不足时兜底。
RTF = {'funasr-nano': 5.08, 'whisper': 19.2}
DEFAULT_RTF = 5.08
# 自适应倍率：从「转写完成」事件推算「音频秒数 ÷ 实际耗时秒数」，样本达到该数才采用
RTF_MIN_SAMPLES = 2
# 批量兜底倍率：单条标定值在批量下偏乐观（实测 5 条 / 3225.5 秒音频 / 1555 秒 ≈ 2.1x）。
# 只在「音频任务 ≥ BATCH_AUDIO_MIN 条」且尚无实测样本时使用；有样本后一律以实测为准。
RTF_BATCH_FALLBACK = 2.1
BATCH_AUDIO_MIN = 2
# 倍率的指数平滑系数：与 tqdm 的 EMA 同源（它默认 0.3）。转写耗时受音频长度与内容
# 影响，波动比逐条迭代的任务大，需要更平滑时可下调到 0.2。
RTF_EMA_ALPHA = 0.3
# 各阶段的预期心跳间隔（秒）：该阶段内这么久没有事件属于正常。
# 过期阈值由它推出（转黄 = 心跳，转红 = 3 倍心跳）并随状态下发给前端。
# 「生成纪要」由主 agent 在写完时上报一次，中间几分钟没有事件是正常的，
# 沿用单一阈值会让它一直被标成过期。
STAGE_HEARTBEAT = {
    'queued': 30, 'download': 10, 'convert': 10, 'asr': 10,
    'transcribed': 300, 'notes': 300, 'done': 3600, 'fail': 3600,
}
# 兜底阈值（秒）：阶段不在上表里时使用，前端也保留同一组兜底值
STALE_WARN_SEC = 10
STALE_BAD_SEC = 30


def stale_thresholds(stage):
    """按阶段给出 (转黄秒数, 转红秒数)；阶段未知时用兜底值。"""
    hb = STAGE_HEARTBEAT.get(stage)
    if not hb:
        return STALE_WARN_SEC, STALE_BAD_SEC
    return hb, hb * 3
# 跨批次历史：只用来在顶部并排显示「本批实测 vs 历史中位」，不参与计算
RTF_HISTORY_FILE = 'rtf_history.jsonl'
RTF_HISTORY_MAX = 50
# 事件角色（借 LSP Work Done Progress 的三段式）。**纯附加元信息**：
# 状态仍从事件内容推导，不依赖事件顺序，乱序容错保持不变。
KINDS = ('begin', 'report', 'end')

# 退役文件在「无人写入」超过该秒数后才允许删除（避免删掉仍持有句柄的活文件）
COMPACT_SAFE_AGE = 300

# 看板端口：一次探测只认「hub.json 里记录的实际端口」，不做端口段扫描
DEFAULT_PORT = 8765
# 服务启动时把实际端口写这里，供 ensure_serving 一次探测就定位（避免端口扫描）
HUB_STATE = 'hub.json'


# --------------------------------------------------------------------------- #
# 路径与标识
# --------------------------------------------------------------------------- #
def run_dir(d=None):
    p = Path(d) if d else DEFAULT_DIR
    p.mkdir(parents=True, exist_ok=True)
    return p


_BV_RE = None


def task_of(path):
    """从素材包路径里取任务 ID（BV 号）；取不到就退回文件名。"""
    global _BV_RE
    if _BV_RE is None:
        import re
        _BV_RE = re.compile(r'(BV[0-9A-Za-z]{10})')
    s = str(path or '')
    m = _BV_RE.search(s)
    if m:
        return m.group(1)
    return Path(s).stem or s


def progress_dir():
    """返回启用中的进度目录；未启用返回 None（管线据此跳过全部上报）。"""
    d = os.environ.get('BILI_PROGRESS_DIR')
    return Path(d) if d else None


def _events_file(d):
    return d / ('events_%d.jsonl' % os.getpid())


# --------------------------------------------------------------------------- #
# 写入端
# --------------------------------------------------------------------------- #
def _engine_of(d):
    """取运行目录里登记的引擎名（run.json 的 engine）；取不到给默认值。"""
    try:
        p = run_dir(d) / 'run.json'
        if p.is_file():
            v = json.loads(p.read_text(encoding='utf-8')).get('engine')
            if v:
                return str(v)
    except Exception:
        pass
    return 'funasr-nano'


def emit(task=None, stage=None, pct=None, title=None, up=None, duration_sec=None,
         elapsed=None, note=None, src=None, group=None, kind=None, d=None, **extra):
    """追加一条进度事件。**永不抛异常** —— 进度上报绝不能拖垮主流程。

    kind 是可选的事件角色（begin / report / end），借 LSP 的 Work Done Progress：
    它只让事件流自描述，**不参与状态推导** —— 状态仍从事件内容算，乱序容错不变。
    取值不在 KINDS 里就当没传（静默丢弃，不报错）。
    """
    try:
        d = run_dir(d)
        ev = {'ts': time.time(), 'task': task, 'stage': stage, 'pct': pct,
              'title': title, 'up': up, 'duration_sec': duration_sec,
              'elapsed': elapsed, 'note': note, 'group': group,
              'kind': kind if kind in KINDS else None,
              'src': src or Path(sys.argv[0]).stem, 'pid': os.getpid()}
        ev = {k: v for k, v in ev.items() if v is not None}
        ev.update(extra)
        with open(_events_file(d), 'a', encoding='utf-8') as f:
            f.write(json.dumps(ev, ensure_ascii=False) + '\n')
        # 转写完成的事件同时带着音频时长与实际耗时 —— 顺手记一条跨批次样本。
        # 记在这里而不是轮询侧：轮询是只读的，而这里是「每个任务正好一次」的时机。
        if stage == 'transcribed' and duration_sec and elapsed and elapsed > 0:
            _record_rtf_history(d, _engine_of(d), float(duration_sec) / float(elapsed))
        return True
    except Exception:
        return False


def auto(task=None, stage=None, pct=None, group=None, **kw):
    """仅当 BILI_PROGRESS_DIR 已设置时上报。管线里统一走这个入口。

    group 未显式给时读环境变量 BILI_PROGRESS_GROUP —— 这样子进程（如
    funasr_adapter 跑在另一个 venv）不用改代码也能继承任务组归属。
    """
    if not os.environ.get('BILI_PROGRESS_DIR'):
        return False
    if group is None:
        group = os.environ.get('BILI_PROGRESS_GROUP') or None
    try:
        return emit(task=task, stage=stage, pct=pct, group=group, d=progress_dir(), **kw)
    except Exception:
        return False


def pct_from_elapsed(elapsed, duration_sec, engine='funasr-nano', cap=97.0,
                     rate=None):
    """按「已用时间 / 预测耗时」估算百分比，用于没有细粒度回调的阶段。

    rate 给定时用它替代常量 —— 常量按单条标定，批量实测慢得多，
    照常量算会在转写中途直接顶到 cap（表现为「97% 且剩 0s」）。
    """
    try:
        r = float(rate) if rate else RTF.get(engine, DEFAULT_RTF)
        pred = float(duration_sec) / r
        if pred <= 0:
            return None
        return round(min(cap, max(0.0, elapsed / pred * 100.0)), 1)
    except Exception:
        return None


def _ema(values, alpha=RTF_EMA_ALPHA):
    """一阶指数平滑 + 误差修正，返回最后一个平滑值。

    算式与 tqdm 的 EMA 同源：last = a·x + (1-a)·last，再除以 (1-(1-a)^n)。
    分母把前几个样本从初值 0 拉回来（否则头两条会偏小）。它给近期样本更高的
    权重，所以「批次突然变慢」比中位数反映得更快 —— 这也是换掉中位数的原因。
    """
    last, n = 0.0, 0
    for x in values:
        last = alpha * x + (1 - alpha) * last
        n += 1
    if not n:
        return None
    return last / (1 - (1 - alpha) ** n)


def _median(values):
    s = sorted(values)
    mid = len(s) // 2
    return s[mid] if len(s) % 2 else (s[mid - 1] + s[mid]) / 2


def rtf_samples(d=None):
    """按时间顺序取「转写完成」事件的倍率样本（音频秒数 ÷ 实际耗时秒数）。

    取样点必须落在**转写结束**那一刻：那时的事件同时带着音频时长与实际耗时。
    不能拿终态任务的 elapsed —— 转写结束后还有生成纪要，
    而阶段切换时已用时间会重新起算，用它当样本会把倍率算小。
    """
    try:
        d = run_dir(d)
        _, events = _load_raw(d)          # 已按 ts 排序；顺序对 EMA 是必需的
        out = []
        for e in events:
            if e.get('stage') != 'transcribed':
                continue
            dur, el = e.get('duration_sec'), e.get('elapsed')
            if dur and el and el > 0:
                out.append(float(dur) / float(el))
        return out
    except Exception:
        return []


def observed_rtf(d=None, min_samples=RTF_MIN_SAMPLES):
    """本批实测倍率：对样本按时间顺序做指数平滑。样本不足返回 None。

    常量 5.08 按单条标定，批量实测只有约 2.1；照常量推算会让「预计剩余」
    在批量场景下直接归零。样本不足时由 effective_rtf() 兜底。
    """
    s = rtf_samples(d)
    if len(s) < min_samples:
        return None
    v = _ema(s)
    return round(v, 3) if v else None


def load_rtf_history(d=None, engine=None):
    """读跨批次的历史倍率样本（只读，失败返回空表）。

    历史**不参与计算**，只用于在顶部把「本批实测」与「历史中位」并排显示，
    让人自己判断本批是否异常（换机器 / 换模型版本 / GPU 被占用）。
    与 Airflow 的 Task Duration 视图同一个思路：给参照，不做自动采用。
    """
    try:
        p = run_dir(d).parent / RTF_HISTORY_FILE
        if not p.is_file():
            return {}
        vals = []
        for line in p.read_text(encoding='utf-8').splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                o = json.loads(line)
            except Exception:
                continue
            if engine and o.get('engine') != engine:
                continue
            r = o.get('rate')
            if isinstance(r, (int, float)) and r > 0:
                vals.append(float(r))
        if not vals:
            return {}
        vals = vals[-RTF_HISTORY_MAX:]
        return {'n': len(vals), 'median': round(_median(vals), 3)}
    except Exception:
        return {}


def _record_rtf_history(d, engine, rate):
    """把一个样本追加进跨批次历史（append-only，一行一条）。

    为什么用 jsonl 而不是 json 字典：上报方是多进程，读改写会有丢更新；
    追加一行更安全，且与事件文件同一套思路。**只写引擎名与倍率两个数** ——
    不写素材名也不写时长，避免把内容信息带到一个会被复用的位置。
    """
    try:
        if not (isinstance(rate, (int, float)) and rate > 0):
            return
        p = run_dir(d).parent / RTF_HISTORY_FILE
        with open(p, 'a', encoding='utf-8') as f:
            f.write(json.dumps({'engine': engine or 'funasr-nano',
                                'rate': round(float(rate), 4),
                                'ts': round(time.time(), 1)},
                               ensure_ascii=False) + '\n')
    except Exception:
        pass


def trim_rtf_history(d=None, keep=RTF_HISTORY_MAX, dry=False):
    """把跨批次历史裁到每引擎最近 keep 条（append-only 文件会一直长）。"""
    try:
        p = run_dir(d).parent / RTF_HISTORY_FILE
        if not p.is_file():
            return {'before': 0, 'after': 0}
        rows = [ln.strip() for ln in p.read_text(encoding='utf-8').splitlines() if ln.strip()]
        by_engine = {}
        for ln in rows:
            try:
                eng = json.loads(ln).get('engine') or 'funasr-nano'
            except Exception:
                eng = 'funasr-nano'
            by_engine.setdefault(eng, []).append(ln)
        kept = []
        for eng in sorted(by_engine):
            kept += by_engine[eng][-keep:]
        if not dry and len(kept) != len(rows):
            tmp = p.with_name(p.name + '.tmp')
            tmp.write_text('\n'.join(kept) + '\n', encoding='utf-8')
            os.replace(tmp, p)
        return {'before': len(rows), 'after': len(kept)}
    except Exception:
        return {'before': 0, 'after': 0}


def effective_rtf(d=None, engine='funasr-nano'):
    """当前该用的转写倍率，返回 (倍率, 来源)。

    来源三档，越靠前越可信：observed（本批实测）→ batch（批量兜底）→ constant（单条常量）。
    前端会把来源显示出来 —— 让人知道这个「预计剩余」是按什么算的。
    """
    obs = observed_rtf(d)
    if obs:
        return obs, 'observed'
    try:
        d = run_dir(d)
        run, events = _load_raw(d)
        tasks = _fold_tasks(run, events)[0]
        audio = sum(1 for t in tasks.values() if t.get('duration_sec'))
        if audio >= BATCH_AUDIO_MIN:
            return RTF_BATCH_FALLBACK, 'batch'
    except Exception:
        pass
    return RTF.get(engine, DEFAULT_RTF), 'constant'


def init_run(tasks, title='任务批次', d=None, meta=None, merge=True, group=None):
    """写入任务清单。

    merge=True（默认）时**并入**已有清单而不是替换 —— 两个 skill（转写 / wiki 编译）
    共用同一个运行目录，谁都不能把对方的任务冲掉。同 id 的任务按新值更新
    （重跑时刷新标题与时长）。
    """
    d = run_dir(d)
    rp = d / 'run.json'
    doc = {'title': title, 'created': time.time(), 'tasks': []}
    if merge and rp.is_file():
        try:
            old = json.loads(rp.read_text(encoding='utf-8'))
            doc['created'] = old.get('created') or doc['created']
            doc['title'] = old.get('title') or title   # 标题取首次写入者，避免互相覆盖
            doc['tasks'] = [t for t in (old.get('tasks') or []) if isinstance(t, dict)]
        except Exception:
            pass
    tasks = [dict(t) for t in tasks]
    if group:
        for t in tasks:
            t.setdefault('group', group)
    index = {}
    for t in doc['tasks']:
        if t.get('id') or t.get('task'):
            index[t.get('id') or t.get('task')] = t
    for t in tasks:
        tid = t.get('id') or t.get('task')
        if tid and tid in index:
            index[tid].update({k: v for k, v in t.items() if v is not None})
        else:
            doc['tasks'].append(t)
    if meta:
        doc.update(meta)
    tmp = d / 'run.json.tmp'
    tmp.write_text(json.dumps(doc, ensure_ascii=False, indent=1), encoding='utf-8')
    os.replace(tmp, rp)
    with _RAW_LOCK:
        _RAW_CACHE.pop(str(d), None)
    return d


# --------------------------------------------------------------------------- #
# 读取端：原始数据加载（带签名缓存，避免每次轮询重解析全部事件）
# --------------------------------------------------------------------------- #
_RAW_CACHE = {}
_RAW_LOCK = threading.Lock()


def _signature(d):
    """运行目录里所有输入文件的 (名字, 大小, mtime) 指纹。"""
    sig = []
    for p in sorted(d.glob('*.json')) + sorted(d.glob('*.jsonl')):
        try:
            st = p.stat()
            sig.append((p.name, st.st_size, st.st_mtime_ns))
        except OSError:
            continue
    return tuple(sig)


def _read_jsonl(p):
    out = []
    try:
        with open(p, encoding='utf-8', errors='replace') as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass  # 写入端正在追加时的半行，跳过即可
    except Exception:
        pass
    return out


def _load_raw(d):
    """返回 (run, events)。命中签名缓存则直接复用已解析的事件。"""
    key = str(d)
    sig = _signature(d)
    with _RAW_LOCK:
        hit = _RAW_CACHE.get(key)
        if hit and hit[0] == sig:
            return hit[1], hit[2]

    run = {}
    rp = d / 'run.json'
    if rp.is_file():
        try:
            run = json.loads(rp.read_text(encoding='utf-8'))
        except Exception:
            run = {}

    consumed = set()
    cp = d / 'consumed.json'
    if cp.is_file():
        try:
            consumed = set(json.loads(cp.read_text(encoding='utf-8')))
        except Exception:
            consumed = set()

    events = []
    snap = d / 'state_snapshot.json'
    if snap.is_file():
        try:
            doc = json.loads(snap.read_text(encoding='utf-8'))
            events += doc.get('events', [])
            if doc.get('run') and not run:
                run = doc['run']
        except Exception:
            pass
    for p in sorted(d.glob('events_*.jsonl')):
        if p.name in consumed:
            continue
        events += _read_jsonl(p)
    events.sort(key=lambda e: e.get('ts', 0))

    with _RAW_LOCK:
        _RAW_CACHE[key] = (sig, run, events)
    return run, events


# --------------------------------------------------------------------------- #
# 读取端：折叠成看板状态
# --------------------------------------------------------------------------- #
def _fold_tasks(run, events):
    """把任务清单与事件流折叠成 {任务ID: 状态} 与出现顺序。"""
    tasks, order = {}, []
    for t in run.get('tasks', []):
        tid = t.get('id') or t.get('task')
        if not tid:
            continue
        tasks[tid] = dict(t, id=tid, stage='queued', pct=0.0, events=0, log=[])
        order.append(tid)

    for e in events:
        tid = e.get('task')
        if not tid:
            continue
        if tid not in tasks:
            tasks[tid] = {'id': tid, 'title': e.get('title') or tid,
                          'up': e.get('up'), 'duration_sec': e.get('duration_sec'),
                          'stage': 'queued', 'pct': 0.0, 'events': 0, 'log': []}
            order.append(tid)
        t = tasks[tid]
        t['events'] += 1
        for k in ('title', 'up', 'duration_sec', 'engine', 'group'):
            if e.get(k) and not t.get(k):
                t[k] = e[k]
        stage = e.get('stage')
        if stage:
            # 按阶段序推进：迟到的旧阶段事件（如 done 之后又到的 asr）不会把显示拉回去
            if STAGE_RANK.get(stage, 0) >= STAGE_RANK.get(t.get('stage'), 0):
                if stage != t.get('stage'):
                    # 进入新阶段：已用时间重新起算，沿用上一阶段的耗时是误导
                    t['stage_since'] = e['ts']
                    t.pop('elapsed', None)
                t['stage'] = stage
        pct = e.get('pct')
        if isinstance(pct, (int, float)):
            if t.get('stage') == 'done':
                t['pct'] = 100.0
            else:
                # 失败也保留中断时的百分比，别让用户以为是 0 进度就断了
                t['pct'] = max(t.get('pct') or 0.0, float(pct))
        if e.get('elapsed') is not None:
            t['elapsed'] = e['elapsed']
        if e.get('started'):
            t['started'] = e['started']
        if e.get('note'):
            t['log'].append({'ts': e['ts'], 'note': e['note'], 'stage': stage,
                             'kind': e.get('kind'), 'src': e.get('src')})
        if e.get('kind'):
            t['last_kind'] = e['kind']        # 仅用于展示，不参与状态推导
        t['last_ts'] = e['ts']
    return tasks, order


def snapshot(d=None):
    """把 run.json + 事件折叠成看板要的状态。"""
    d = run_dir(d)
    run, events = _load_raw(d)
    tasks, order = _fold_tasks(run, events)

    now = time.time()
    # 数据时刻 = 最后一条事件的时刻（不是快照时刻）。停更判定必须基于它，
    # 否则「轮询成功」会被当成「数据在更新」，管线卡住也会一直显示「刚刚」。
    data_ts = max([e.get('ts') or 0 for e in events] + [run.get('created') or 0])
    stale_sec = max(0.0, now - data_ts) if data_ts else 0.0
    # 停更超过 STALE_CAP_SEC 之后不再外推：管线崩了数字不该继续涨
    eff_now = min(now, data_ts + STALE_CAP_SEC) if data_ts else now
    # 当前阶段 = 最近有事件的任务所在阶段；过期阈值按它取
    cur_stage, newest = None, -1.0
    for t in tasks.values():
        ts = t.get('last_ts') or 0
        if ts > newest:
            newest, cur_stage = ts, (t.get('stage') or 'queued')
    stale_warn, stale_bad = stale_thresholds(cur_stage)
    # 倍率：本批实测优先 → 批量兜底 → 单条常量（来源一并报给前端）
    rate, rate_src = effective_rtf(d, run.get('engine') or 'funasr-nano')
    rows = []
    for tid in order:
        t = tasks[tid]
        st = t.get('stage') or 'queued'
        dur = t.get('duration_sec') or 0
        engine = t.get('engine') or run.get('engine') or 'funasr-nano'
        # 有音频时长就按时长÷倍率推算；非音频任务（如 wiki 编译）直接用自带的 pred_sec
        if t.get('pred_sec'):
            pred = t['pred_sec']
        elif dur:
            pred = dur / rate
        else:
            pred = None

        # 已用时间 = 当前阶段的已用秒数（阶段切换时归零，见 _fold_tasks）
        if st in TERMINAL:
            # 终态必须冻结：不能用 eff_now - started，否则「已完成」的耗时还在涨
            elapsed = t.get('elapsed')
            if elapsed is None:
                if t.get('started') and t.get('last_ts'):
                    elapsed = t['last_ts'] - t['started']
                else:
                    elapsed = pred
        elif st == 'asr':
            ref = t.get('started') or t.get('stage_since')
            elapsed = (eff_now - ref) if ref else t.get('elapsed')
        elif st in ACTIVE_STAGES:
            ref = t.get('stage_since')
            elapsed = (eff_now - ref) if ref else t.get('elapsed')
        else:                       # queued / transcribed：等待态，不外推
            elapsed = t.get('elapsed')

        # 预计剩余：只有「转写中」和「还没开始」两种情形有依据。
        # 下载 / 转码 / 生成纪要都没有各自的实测样本，宁可不给数字，
        # 也不套用转写的预测值（那是别的阶段的时间）。
        eta, overrun = None, False
        if st == 'done':
            pct, eta = 100.0, 0.0
        else:
            pct = t.get('pct') or 0.0
            if pred and st == 'asr' and elapsed is not None:
                eta = max(0.0, pred - elapsed)
                overrun = elapsed >= pred
            elif pred and st == 'queued':
                eta = pred

        rows.append({
            'id': tid, 'title': t.get('title') or tid, 'up': t.get('up'),
            'group': t.get('group'),
            'duration_sec': dur, 'stage': st, 'stage_label': STAGE_LABEL.get(st, st),
            'pct': round(pct, 1), 'pred_sec': round(pred, 1) if pred else None,
            'elapsed': round(elapsed, 1) if elapsed else None,
            'eta': round(eta, 1) if eta is not None else None,
            'overrun': bool(overrun),
            # 转写中的实测速率（音频秒数 ÷ 已用秒数）：只有这一阶段能算出来。
            # 前端用它解释「这个预计剩余是按什么速度推的」；算不出来就不给。
            'rate': (round(float(dur) / float(elapsed), 2)
                     if st == 'asr' and dur and elapsed and elapsed > 0 else None),
            'kind': t.get('last_kind'),
            'note': (t['log'][-1]['note'] if t['log'] else None),
            'engine': engine,
        })

    total = len(rows)
    done = sum(1 for r in rows if r['stage'] == 'done')
    failed = sum(1 for r in rows if r['stage'] == 'fail')
    active = [r for r in rows if r['stage'] not in ('done', 'fail', 'queued')]
    # 总进度的分母排除失败行：失败行会残留中断时的百分比，
    # 算进分母就永远到不了 100%（同屏出现「80% + 已完成 2/3 + 剩 0s」）
    progresses = [r for r in rows if r['stage'] != 'fail']
    weights = [(r['pred_sec'] or 60.0) for r in progresses]
    wsum = sum(weights) or 1.0
    overall = sum(w * (r['pct'] / 100.0) for w, r in zip(weights, progresses)) / wsum * 100
    open_rows = [r for r in rows if r['stage'] not in TERMINAL]
    remaining = sum((r['pred_sec'] or 0) * (1 - r['pct'] / 100.0) for r in open_rows)
    log = [{'ts': e['ts'], 'task': e.get('task'), 'stage': e.get('stage'),
            'stage_label': STAGE_LABEL.get(e.get('stage'), e.get('stage')),
            'kind': e.get('kind'),
            'note': e.get('note') or (e.get('title') or ''), 'src': e.get('src')}
           for e in events if e.get('note') or e.get('stage')][-14:]

    # 任务组汇总：两个 skill 共用一个看板时，用组区分"谁的任务"
    groups = {}
    for r in rows:
        g = r.get('group') or '未分组'
        s = groups.setdefault(g, {'name': g, 'total': 0, 'done': 0, 'failed': 0,
                                  'pct': 0.0, '_num': 0.0, '_w': 0.0})
        w = r['pred_sec'] or 60.0
        s['total'] += 1
        s['done'] += r['stage'] == 'done'
        s['failed'] += r['stage'] == 'fail'
        s['_num'] += w * r['pct'] / 100.0
        s['_w'] += w
    for s in groups.values():
        num, w = s.pop('_num'), s.pop('_w')
        s['pct'] = round(num / w * 100, 1) if w else 0.0
    groups = [groups[k] for k in sorted(groups, key=lambda x: -groups[x]['total'])]

    return {
        'run': {'title': run.get('title') or '任务进度', 'created': run.get('created'),
                'engine': run.get('engine') or 'funasr-nano'},
        'overall': {
            'pct': round(overall, 2), 'total': total, 'done': done, 'failed': failed,
            'active': len(active), 'eta': round(remaining, 1) if total else None,
            # eta_complete=False：还有任务没有可用的时间依据，前端应标注为「≥」
            'eta_complete': all(r['eta'] is not None for r in open_rows),
            'overrun': any(r['overrun'] for r in open_rows),
            'rate': rate,
            'rate_src': rate_src,           # observed / batch / constant
            # 跨批次历史：只显示、不参与计算 —— 让「本批 2.1x」有个参照，
            # 换机器 / 换模型版本 / GPU 被占用这类变化由人判断（同 Airflow 的 Task Duration）
            'rate_history': load_rtf_history(d, run.get('engine')),
            'updated': data_ts or now,      # 数据时刻：最后一条事件的时刻
            'stale_sec': round(stale_sec, 1),
            # 阈值随状态下发：前端不再各写一份，改一处即生效；取值随阶段变化
            'stale_warn': stale_warn,
            'stale_bad': stale_bad,
            'stale_cap': STALE_CAP_SEC,
            'stale_stage': cur_stage,       # 上面两个阈值取自哪个阶段，便于排查
            'elapsed': round(eff_now - (run.get('created') or eff_now), 1),
        },
        'groups': groups,
        'tasks': rows,
        'log': list(reversed(log)),
    }


# --------------------------------------------------------------------------- #
# 事件归档：折叠成快照并退役旧文件
# --------------------------------------------------------------------------- #
def compact(d=None, safe_age=COMPACT_SAFE_AGE, dry=False):
    """把当前全部事件折叠成 state_snapshot.json，并退役（删除）已无人写入的旧文件。

    **只删 mtime 超过 safe_age 的文件** —— 正在跑的进程仍持有 append 句柄，
    删掉会让它的后续写入落进已被 unlink 的 inode（Windows 上则直接删除失败）。
    """
    d = run_dir(d)
    run, events = _load_raw(d)
    tmp = d / 'state_snapshot.json.tmp'
    tmp.write_text(json.dumps({'run': run, 'events': events}, ensure_ascii=False),
                   encoding='utf-8')
    os.replace(tmp, d / 'state_snapshot.json')

    consumed = set()
    cp = d / 'consumed.json'
    if cp.is_file():
        try:
            consumed = set(json.loads(cp.read_text(encoding='utf-8')))
        except Exception:
            consumed = set()

    now = time.time()
    retired, kept = [], []
    for p in sorted(d.glob('events_*.jsonl')):
        age = now - p.stat().st_mtime
        if age >= safe_age:
            retired.append(p.name)
            if not dry:
                try:
                    p.unlink()
                except OSError:
                    retired.pop()
                    kept.append((p.name, '删除失败'))
        else:
            kept.append((p.name, '仍在写入（%.0fs 前）' % age))
    consumed |= set(retired)
    if not dry:
        cp.write_text(json.dumps(sorted(consumed), ensure_ascii=False), encoding='utf-8')

    hist = trim_rtf_history(d, dry=dry)
    return {'snapshot_events': len(events), 'retired': retired, 'kept': kept,
            'rtf_history': hist}


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = 'ProgressHub/2.0'

    def log_message(self, *a):  # 静音
        pass

    def _send(self, code, body, ctype='application/json; charset=utf-8'):
        if isinstance(body, str):
            body = body.encode('utf-8')
        try:
            self.send_response(code)
            self.send_header('Content-Type', ctype)
            self.send_header('Content-Length', str(len(body)))
            self.send_header('Cache-Control', 'no-store')
            self.end_headers()
            if body:
                self.wfile.write(body)
        except Exception:
            pass

    def do_GET(self):
        path = self.path.split('?')[0]
        if path in ('/', '/index.html', '/dashboard'):
            f = HERE / 'dashboard.html'
            if not f.is_file():
                return self._send(500, '{"error":"dashboard.html 缺失"}')
            return self._send(200, f.read_bytes(), 'text/html; charset=utf-8')
        if path == '/api/state':
            return self._send(200, json.dumps(snapshot(self.server.run_dir),
                                              ensure_ascii=False))
        if path == '/api/health':
            return self._send(200, '{"ok":true}')
        if path == '/favicon.ico':
            return self._send(204, b'')   # 静音，别往日志里灌 404
        return self._send(404, '{"error":"not found"}')

    def do_POST(self):
        if self.path.split('?')[0] != '/api/emit':
            return self._send(404, '{"error":"not found"}')
        try:
            n = int(self.headers.get('Content-Length') or 0)
            doc = json.loads(self.rfile.read(n).decode('utf-8') or '{}')
        except Exception as e:
            return self._send(400, json.dumps({'error': str(e)}))
        ok = emit(d=self.server.run_dir, **doc)
        return self._send(200, json.dumps({'ok': bool(ok)}))


# --------------------------------------------------------------------------- #
# 自启与单实例
# 关键：**绝不能每跑一条任务就弹一个窗**。所以先探端口，已在跑就只上报、不再拉起。
# --------------------------------------------------------------------------- #
def _probe(host, port, timeout=0.3):
    """该端口上是否已有本看板在跑（认 /api/health 里的 ok:true）。

    两处必须注意（都踩过）：
    1. **不能用 urllib**：它读系统代理设置，探测 127.0.0.1 时请求可能被代理劫持，
       关闭端口要等满超时才返回，而且服务真在跑时也会误判为"没跑"。
    2. **不能只 recv 一次**：TCP 会把响应头与 body 拆成两个段，单次 recv 可能只拿到
       头（实测三次里两次如此），于是 "ok" 检查落空 -> 误判没在跑 -> 重复拉起窗口。
       必须累积读到出现 "ok" 或超时为止。
    """
    import socket
    try:
        with socket.create_connection((host, port), timeout=timeout) as s:
            s.settimeout(timeout)
            s.sendall(b'GET /api/health HTTP/1.0\r\nHost: %s:%d\r\n\r\n'
                      % (host.encode(), port))
            buf = b''
            try:
                while b'"ok"' not in buf and len(buf) < 4096:
                    chunk = s.recv(512)
                    if not chunk:
                        break
                    buf += chunk
            except Exception:
                pass
        return b'"ok"' in buf and b'true' in buf
    except Exception:
        return False


def find_running(d=None, host='127.0.0.1', port=DEFAULT_PORT, span=0):
    """找出已在运行的看板端口，找不到返回 None。

    优先读 `<run_dir>/hub.json` —— 服务启动时自报端口，只需 **1 次**探测。
    `span=0`（默认）不做端口扫描：本机探测一个关闭端口要等满超时（实测 0.3s/个），
    扫 24 个就是 7 秒，绝不能放进常规路径。需要时显式传 span。
    """
    cands = []
    try:
        hp = run_dir(d) / HUB_STATE
        if hp.is_file():
            info = json.loads(hp.read_text(encoding='utf-8'))
            if isinstance(info.get('port'), int):
                cands.append(info['port'])
    except Exception:
        pass
    if port not in cands:
        cands.append(port)
    if span:
        cands += [port + i for i in range(1, span)]
    for p in cands:
        if _probe(host, p):
            return p
    return None


def progress_mode(mode=None):
    """auto（默认，自动拉起并开窗）/ manual（只在已开时上报）/ off（完全关闭）。"""
    m = (mode or os.environ.get('BILI_PROGRESS') or 'auto').strip().lower()
    return m if m in ('auto', 'manual', 'off') else 'auto'


def _spawn_hub(args):
    """脱离父进程启动看板，让它在流水线结束后继续存活。

    Windows 上光有 DETACHED_PROCESS 不够：若宿主把进程树放进 Job Object 且设了
    "关闭即杀"，脱离旗标也保不住——实测被宿主回收过。故再叠加
    CREATE_BREAKAWAY_FROM_JOB 逃出 Job；该标志要求 Job 允许 breakaway，
    被拒时（ERROR_ACCESS_DENIED）退回不带它的组合，保证至少能起来。
    """
    import subprocess
    base = {'close_fds': True, 'stdout': subprocess.DEVNULL,
            'stderr': subprocess.DEVNULL, 'stdin': subprocess.DEVNULL}
    if os.name != 'nt':
        return subprocess.Popen(args, start_new_session=True, **base)
    DETACHED, NEW_GROUP, BREAKAWAY = 0x00000008, 0x00000200, 0x01000000
    try:
        return subprocess.Popen(args, creationflags=DETACHED | NEW_GROUP | BREAKAWAY, **base)
    except OSError:
        return subprocess.Popen(args, creationflags=DETACHED | NEW_GROUP, **base)


def ensure_serving(run_dir_=None, host='127.0.0.1', port=DEFAULT_PORT,
                   open_window=True, mode=None, wait=10.0):
    """保证看板在跑，返回访问用的 URL（拿不到就返回 None）。

    - 已在跑 → 直接返回 URL，**不再开窗**（单实例，避免弹一堆窗口）
    - 没在跑且 mode=auto → 以脱离进程启动看板并开窗
    - mode=manual / off → 不自动拉起
    """
    m = progress_mode(mode)
    if m == 'off':
        return None
    found = find_running(run_dir_, host, port)
    if found:
        return 'http://%s:%d' % (host, found)
    if m != 'auto':
        return None

    args = [sys.executable, str(Path(__file__).resolve()), '--serve',
            '--port', str(port), '--host', host]
    if run_dir_:
        args += ['--dir', str(run_dir_)]
    if open_window:
        args += ['--open']
    try:
        _spawn_hub(args)
    except Exception:
        return None
    # 等子进程就绪：靠它自报的 hub.json 定位端口（端口可能因占用而回退）
    deadline = time.time() + wait
    while time.time() < deadline:
        time.sleep(0.25)
        found = find_running(run_dir_, host, port)
        if found:
            return 'http://%s:%d' % (host, found)
    return None


def serve(d=None, port=DEFAULT_PORT, host='127.0.0.1', open_window=False):
    rd = run_dir(d)
    if host not in ('127.0.0.1', 'localhost', '::1'):
        print('[警告] 绑定到 %s：看板无鉴权，同一网络内任何人都能读取任务信息与标题。'
              % host, flush=True)
    httpd = _bind(host, port)
    httpd.run_dir = rd
    # 必须用**实际绑定**的端口：端口回退后请求端口可能与真实监听端口不一致，
    # 用请求端口拼 URL 会让 --open 打开一个空地址。
    actual = httpd.server_address[1]
    url = 'http://%s:%d' % (host, actual)
    # 自报端口：让 ensure_serving 一次探测即可定位，不必扫描端口段
    hp = rd / HUB_STATE
    try:
        hp.write_text(json.dumps({'port': actual, 'pid': os.getpid(),
                                  'started': time.time(), 'url': url},
                                 ensure_ascii=False),
                      encoding='utf-8')
    except Exception:
        pass
    print('看板已启动: %s   (运行目录 %s)' % (url, rd), flush=True)
    if open_window:
        threading.Thread(target=lambda: (_open_app_window(url), None)[1],
                         daemon=True).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        # 只清理自己写的那份，别把后来者的记录删掉
        try:
            if hp.is_file() and json.loads(hp.read_text(encoding='utf-8')).get('pid') == os.getpid():
                hp.unlink()
        except Exception:
            pass


class _Server(ThreadingHTTPServer):
    # Windows 上 SO_REUSEADDR 的语义与 POSIX 不同：它**允许两个进程绑同一端口**，
    # 后绑的会悄悄抢走连接而不报错——于是"端口被占用"永远检测不到，
    # 可能同时跑起两个看板互相抢请求。所以 Windows 必须关掉。
    # POSIX 保持开启，进程重启后能立即复用端口。
    allow_reuse_address = os.name != 'nt'


def _bind(host, port, tries=12):
    """端口被占用时自动向后找可用端口，而不是抛栈退出。"""
    last = None
    for i in range(tries):
        p = port + i
        try:
            return _Server((host, p), Handler)
        except OSError as e:
            # 不按错误码判断：Windows 的报错文案会随系统语言变化
            last = e
            print('端口 %d 不可用（%s），尝试 %d…' % (p, e, p + 1), flush=True)
            continue
    raise SystemExit('连续 %d 个端口都不可用，最后错误：%s' % (tries, last))


def _open_app_window(url):
    """用 Chromium 系浏览器的 --app 模式开一个无地址栏的独立窗口。

    这样不用装 pywebview 也能得到"原生窗口"观感；找不到浏览器则退回默认浏览器开标签页。
    """
    import shutil
    import subprocess
    # PATH 里通常没有 msedge/chrome（Windows 不把它们加进 PATH），
    # 所以要同时探常见安装路径，否则 --open 会静默退化成标签页。
    known = {
        'msedge': [r'C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe',
                   r'C:\Program Files\Microsoft\Edge\Application\msedge.exe'],
        'chrome': [r'C:\Program Files\Google\Chrome\Application\chrome.exe',
                   r'C:\Program Files (x86)\Google\Chrome\Application\chrome.exe'],
    }
    exe = None
    for name in ('msedge', 'chrome', 'chromium'):
        exe = shutil.which(name) or next(
            (p for p in known.get(name, []) if os.path.exists(p)), None)
        if exe:
            break
    if exe:
        # 独立 profile：否则 --app 会挂进已有浏览器进程，--window-size 会被忽略
        prof = Path.home() / '.workbuddy' / 'tmp' / 'dash_profile'
        try:
            subprocess.Popen([exe, '--app=%s' % url, '--window-size=1280,900',
                              '--user-data-dir=%s' % prof], close_fds=True)
            print('已用独立窗口打开（%s --app）' % Path(exe).name, flush=True)
            return True
        except Exception:
            pass
    try:
        import webbrowser
        webbrowser.open(url)
        print('已用默认浏览器打开（未找到 Chromium 系，退化为标签页）', flush=True)
    except Exception:
        pass
    return False


# --------------------------------------------------------------------------- #
# CLI
# --------------------------------------------------------------------------- #
def _demo(d, n=6):
    """造一份假数据，用于预览外观。"""
    tasks = []
    names = ['示例：一座城市的产业迁移史', '示例：从零读懂一项公共政策',
             '示例：三十分钟讲清一个技术概念', '示例：一场自然灾害的应急复盘',
             '示例：职业教育与就业市场', '示例：药品定价是怎么形成的']
    for i in range(n):
        tasks.append({'id': 'BV%d' % (1000000000 + i * 137), 'title': names[i % len(names)],
                      'up': '示例UP主（演示数据）', 'duration_sec': 1300 + i * 90, 'engine': 'funasr-nano'})
    init_run(tasks, title='演示：批次一 · 全 Nano 转写', d=d)
    for i, t in enumerate(tasks):
        if i < 2:
            # 刻意不带 duration_sec：跨批次观察倍率取自这个字段，
            # 带上就会把演示用的 5.08 记进实盘的历史文件（两者共用上一级目录）
            emit(task=t['id'], stage='transcribed', pct=100.0, kind='end',
                 elapsed=round(t['duration_sec'] / 5.08, 1),
                 note='转写完成 %s' % t['title'][:18], d=d)
        elif i == 2:
            emit(task=t['id'], stage='asr', pct=63.4, started=time.time() - 180, d=d)
            emit(task=t['id'], stage='asr', pct=63.4, note='转写中 63%', d=d)
        elif i == 3:
            emit(task=t['id'], stage='download', note='下载音频中', d=d)
        elif i == 4:
            emit(task=t['id'], stage='notes', started=time.time() - 95,
                 note='生成纪要中', d=d)
        elif i == 5:
            emit(task=t['id'], stage='done', pct=100.0, elapsed=305.4,
                 note='已完成（含纪要）', d=d)
    print('已生成演示数据 ->', d)


def main():
    ap = argparse.ArgumentParser(description='任务进度看板')
    ap.add_argument('--dir', default=None, help='运行目录（默认 $BILI_PROGRESS_DIR）')
    ap.add_argument('--serve', action='store_true', help='启动看板服务')
    ap.add_argument('--ensure', action='store_true',
                    help='确保看板在跑（已在跑则只返回地址、不重复开窗）；配合 --open 可开窗')
    ap.add_argument('--port', type=int, default=DEFAULT_PORT)
    ap.add_argument('--host', default='127.0.0.1')
    ap.add_argument('--open', dest='open_window', action='store_true',
                    help='用 Chromium 系浏览器的 --app 模式开独立窗口（无地址栏）')
    ap.add_argument('--demo', action='store_true', help='生成演示数据')
    ap.add_argument('--init', action='store_true', help='用 --tasks 初始化一次运行')
    ap.add_argument('--tasks', help='任务清单 JSON 文件或 JSON 字符串')
    ap.add_argument('--title', default='任务批次')
    ap.add_argument('--engine', default='funasr-nano')
    ap.add_argument('--group', default=None, help='任务组名（两个 skill 共用一个看板时区分来源）')
    ap.add_argument('--emit', action='store_true', help='上报一条事件')
    ap.add_argument('--task')
    ap.add_argument('--stage')
    ap.add_argument('--pct', type=float)
    ap.add_argument('--elapsed', type=float,
                    help='本阶段已用秒数（生成纪要这类阶段必传，否则看板没有依据）')
    ap.add_argument('--kind', choices=list(KINDS),
                    help='事件角色（可选）：begin 阶段开始 / report 进行中 / end 阶段结束')
    ap.add_argument('--duration-sec', type=float, dest='duration_sec',
                    help='音频时长（秒），用于推算预计剩余')
    ap.add_argument('--note')
    ap.add_argument('--progress-mode', default=None,
                    help='auto（默认，自动拉起）/ manual（只在已开时上报）/ off（关闭）')
    ap.add_argument('--compact', action='store_true',
                    help='折叠历史事件为快照，并退役（删除）已停止写入的旧事件文件')
    ap.add_argument('--safe-age', type=int, default=COMPACT_SAFE_AGE,
                    help='退役文件的最小静默秒数（默认 %d）' % COMPACT_SAFE_AGE)
    ap.add_argument('--dry-run', action='store_true', help='配合 --compact 只预演不删除')
    ap.add_argument('--reset', action='store_true', help='清空运行目录里的事件与快照')
    a = ap.parse_args()

    if a.reset:
        d = run_dir(a.dir)
        n = 0
        for p in list(d.glob('events_*.jsonl')) + list(d.glob('*.json')) + list(d.glob('*.tmp')):
            if p.exists():
                p.unlink()
                n += 1
        with _RAW_LOCK:
            _RAW_CACHE.pop(str(d), None)
        print('已清理 %d 个文件 (%s)' % (n, d))
        return
    if a.demo:
        return _demo(a.dir)
    if a.compact:
        r = compact(a.dir, a.safe_age, a.dry_run)
        print(('预演' if a.dry_run else '完成') + '：快照含 %d 条事件' % r['snapshot_events'])
        print('  退役 %d 个: %s' % (len(r['retired']), ', '.join(r['retired']) or '无'))
        for name, why in r['kept']:
            print('  保留 %s（%s）' % (name, why))
        h = r.get('rtf_history') or {}
        print('  跨批次历史 %s -> %s 条' % (h.get('before', 0), h.get('after', 0)))
        return
    if a.init:
        if not a.tasks:
            return print('--init 需要 --tasks')
        src = a.tasks
        if os.path.isfile(src):
            tasks = json.loads(Path(src).read_text(encoding='utf-8'))
        else:
            tasks = json.loads(src)
        d = init_run(tasks, title=a.title, d=a.dir, group=a.group,
                     meta={'engine': a.engine})
        print('已并入 %d 个任务 -> %s' % (len(tasks), d))
        return
    if a.ensure:
        url = ensure_serving(a.dir, a.host, a.port, a.open_window, a.progress_mode)
        print(url or '未能启动看板（mode=%s）' % progress_mode(a.progress_mode))
        return
    if a.emit:
        ok = emit(task=a.task, stage=a.stage, pct=a.pct, elapsed=a.elapsed,
                  duration_sec=a.duration_sec, note=a.note, group=a.group,
                  kind=a.kind, d=a.dir)
        return print('emit', 'ok' if ok else 'failed')
    if a.serve:
        return serve(a.dir, a.port, a.host, a.open_window)
    ap.print_help()


if __name__ == '__main__':
    main()
