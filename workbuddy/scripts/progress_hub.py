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

STAGES = ['queued', 'download', 'convert', 'asr', 'notes', 'done', 'fail']
STAGE_LABEL = {
    'queued': '排队', 'download': '下载音频', 'convert': '转码',
    'asr': '转写中', 'notes': '生成纪要', 'done': '已完成', 'fail': '失败',
}
TERMINAL = ('done', 'fail')
# 转写倍率：把「已用时间」换算成百分比（见 references/perf-benchmark-2026-09-13.md）
RTF = {'funasr-nano': 5.08, 'whisper': 19.2}
DEFAULT_RTF = 5.08

# 退役文件在「无人写入」超过该秒数后才允许删除（避免删掉仍持有句柄的活文件）
COMPACT_SAFE_AGE = 300

# 看板默认端口；端口被占用时会向后回退（回退结果记在 hub.json 里供探测）
DEFAULT_PORT = 8765
PORT_SPAN = 24
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
def emit(task=None, stage=None, pct=None, title=None, up=None, duration_sec=None,
         elapsed=None, note=None, src=None, group=None, d=None, **extra):
    """追加一条进度事件。**永不抛异常** —— 进度上报绝不能拖垮主流程。"""
    try:
        d = run_dir(d)
        ev = {'ts': time.time(), 'task': task, 'stage': stage, 'pct': pct,
              'title': title, 'up': up, 'duration_sec': duration_sec,
              'elapsed': elapsed, 'note': note, 'group': group,
              'src': src or Path(sys.argv[0]).stem, 'pid': os.getpid()}
        ev = {k: v for k, v in ev.items() if v is not None}
        ev.update(extra)
        with open(_events_file(d), 'a', encoding='utf-8') as f:
            f.write(json.dumps(ev, ensure_ascii=False) + '\n')
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


def pct_from_elapsed(elapsed, duration_sec, engine='funasr-nano', cap=97.0):
    """按「已用时间 / 预测耗时」估算百分比，用于没有细粒度回调的阶段。"""
    try:
        pred = float(duration_sec) / RTF.get(engine, DEFAULT_RTF)
        if pred <= 0:
            return None
        return round(min(cap, max(0.0, elapsed / pred * 100.0)), 1)
    except Exception:
        return None


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
            sig.append((p.name, st.st_size, int(st.st_mtime * 1000)))
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
def snapshot(d=None):
    """把 run.json + 事件折叠成看板要的状态。"""
    d = run_dir(d)
    run, events = _load_raw(d)

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
            # 终态不回退（done/fail 之后同任务不再被后续事件覆盖）
            if t.get('stage') not in TERMINAL:
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
                             'src': e.get('src')})
        t['last_ts'] = e['ts']

    now = time.time()
    rows = []
    for tid in order:
        t = tasks[tid]
        st = t.get('stage') or 'queued'
        dur = t.get('duration_sec') or 0
        engine = t.get('engine') or run.get('engine') or 'funasr-nano'
        # 有音频时长就按时长÷实测倍率推算；非音频任务（如 wiki 编译）直接用自带的 pred_sec
        pred = t.get('pred_sec') or (dur / RTF.get(engine, DEFAULT_RTF) if dur else None)

        if st in TERMINAL:
            # 终态必须冻结：不能用 now - started，否则「已完成」的耗时还在无限增长
            elapsed = t.get('elapsed')
            if elapsed is None:
                if t.get('started') and t.get('last_ts'):
                    elapsed = t['last_ts'] - t['started']
                else:
                    elapsed = pred
        elif t.get('started'):
            elapsed = now - t['started']
        else:
            elapsed = t.get('elapsed')

        eta = None
        if st == 'done':
            pct, eta = 100.0, 0.0
        else:
            pct = t.get('pct') or 0.0
            if st == 'asr' and pred and elapsed is not None:
                eta = max(0.0, pred - elapsed)
            elif pred is not None:
                eta = pred * (1 - pct / 100.0)

        rows.append({
            'id': tid, 'title': t.get('title') or tid, 'up': t.get('up'),
            'group': t.get('group'),
            'duration_sec': dur, 'stage': st, 'stage_label': STAGE_LABEL.get(st, st),
            'pct': round(pct, 1), 'pred_sec': round(pred, 1) if pred else None,
            'elapsed': round(elapsed, 1) if elapsed else None,
            'eta': round(eta, 1) if eta is not None else None,
            'note': (t['log'][-1]['note'] if t['log'] else None),
            'engine': engine,
        })

    total = len(rows)
    done = sum(1 for r in rows if r['stage'] == 'done')
    failed = sum(1 for r in rows if r['stage'] == 'fail')
    active = [r for r in rows if r['stage'] not in ('done', 'fail', 'queued')]
    weights = [(r['pred_sec'] or 60.0) for r in rows]
    wsum = sum(weights) or 1.0
    overall = sum(w * (r['pct'] / 100.0) for w, r in zip(weights, rows)) / wsum * 100
    remaining = sum((r['pred_sec'] or 0) * (1 - r['pct'] / 100.0)
                    for r in rows if r['stage'] not in TERMINAL)
    log = [{'ts': e['ts'], 'task': e.get('task'), 'stage': e.get('stage'),
            'stage_label': STAGE_LABEL.get(e.get('stage'), e.get('stage')),
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
            'updated': now, 'elapsed': round(now - (run.get('created') or now), 1),
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

    return {'snapshot_events': len(events), 'retired': retired, 'kept': kept}


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
    emit(task=None, stage='notes', note='看板演示模式启动', d=d)
    for i, t in enumerate(tasks):
        if i < 2:
            emit(task=t['id'], stage='done', pct=100.0, elapsed=round(t['duration_sec'] / 5.08, 1),
                 note='已完成 %s' % t['title'][:18], d=d)
        elif i == 2:
            emit(task=t['id'], stage='asr', pct=63.4, started=time.time() - 180, d=d)
            emit(task=t['id'], stage='asr', pct=63.4, note='转写中 63%', d=d)
        elif i == 3:
            emit(task=t['id'], stage='download', pct=12.0, note='下载音频中', d=d)
        elif i == 4:
            emit(task=t['id'], stage='fail', pct=40.0, note='音频下载失败: HTTP 503', d=d)
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
        ok = emit(task=a.task, stage=a.stage, pct=a.pct, note=a.note,
                  group=a.group, d=a.dir)
        return print('emit', 'ok' if ok else 'failed')
    if a.serve:
        return serve(a.dir, a.port, a.host, a.open_window)
    ap.print_help()


if __name__ == '__main__':
    main()
