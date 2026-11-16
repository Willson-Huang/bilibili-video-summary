import argparse
import json
import os
import re
import sys
from pathlib import Path

SKILL_DIR = Path(__file__).resolve().parent
if str(SKILL_DIR) not in sys.path:
    sys.path.insert(0, str(SKILL_DIR))

import progress_hub as ph
import library_queue as lq

# 用法（本项目规范要求 Python 文件不加模块 docstring，说明放在 SKILL.md 与 main()）：
#   python notes_done.py --dir <进度运行目录>                      补发
#   python notes_done.py --dir <进度运行目录> --dry-run            只列出，不上报
#
# 转写脚本只报到「待生成纪要」，最后一步「已完成」要由写纪要的一方补发，实测累计漏了 7 次。
# 本脚本把这个动作改成对账：进度里还停在非终态的 BV 任务，只要它的标准纪要文件已经存在，
# 就补发一条完成事件。已标记完成的任务不在候选里，因此重复运行不会重复上报。

TERMINAL = ('done', 'fail')
PACK_SUBDIR = 'bili'                # 素材包缓存目录名
PACK_ARCHIVE_SUBDIR = 'bili_subs'   # 素材包归档目录名，与缓存目录同级
NOTE_SUFFIX = '_纪要.md'
# 归一化时一律忽略的标点：引号（直/弯两式）与斜杠类。素材包头部与磁盘文件名常一边用
# 直引号、一边用中文弯引号，逐字比对会漏配；`note_name()` 已经把直引号当非法字符剔除。
NOISE = str.maketrans('', '', '"\'“”‘’、/\\')
# 素材包文件名里的页号后缀（`bili_<BV>_p3.md`）。进度上多 P 合并成一行，
# 这里靠它把该 BV 的各页素材包找齐。
PART_RE = re.compile(r'_p(\d+)$')


def _norm(s):
    return re.sub(r'\s+', '', str(s)).translate(NOISE).lower()


def _first_dir(cands):
    for c in cands:
        if c and Path(c).is_dir():
            return Path(c)
    return None


def find_note_file(raw_dir, name):
    """先按标准文件名精确查；查不到再做标点归一化比对。

    归一化后命中多个时拒绝猜测 —— 补发完成信号不能认错文件。
    """
    exact = Path(raw_dir) / name
    if exact.is_file():
        return exact, None
    key = _norm(name)
    hits = [p for p in sorted(Path(raw_dir).glob('*' + NOTE_SUFFIX)) if _norm(p.name) == key]
    if len(hits) == 1:
        return hits[0], None
    if len(hits) > 1:
        return None, '归一化后有 %d 个候选，需人工确认：%s' % (
            len(hits), ' / '.join(h.name for h in hits[:3]))
    return None, '纪要文件不在目录里：' + name


def _candidate_caches(hint):
    """素材包目录的候选，按优先级：显式给的 → 环境变量 → 本模块的默认值 → 通用兜底。

    直接复用 `library_queue` 的默认值，安装副本里那几行是回填过的真实路径，
    在这里再写一遍既会重复也会走味。
    """
    return [hint, os.environ.get('BILI_CACHE_DIR'), lq.DEFAULT_CACHE,
            Path.home() / '.workbuddy' / 'cache' / PACK_SUBDIR]


def resolve_run_dir(d, cache_dir):
    """进度运行目录：显式给的优先，其次环境变量，最后从素材包目录同级推导。"""
    if d:
        return Path(d).resolve()
    got = ph.progress_dir()
    if got:
        return Path(got).resolve()
    cache = _first_dir(_candidate_caches(cache_dir))
    if cache is not None:
        cand = cache.parent / 'progress' / 'current'
        if cand.is_dir():
            return cand.resolve()
    return None


def resolve_dirs(raw_dir, cache_dir, run):
    """定位纪要目录与素材包目录。

    素材包缓存在知识库内部时（`<库>/.workbuddy/cache/bili`），两个目录都能从进度
    运行目录上溯推出，因此不要求调用方每次都写路径。推出哪个用哪个，且一律打印出来。
    """
    cache = _first_dir(_candidate_caches(cache_dir))
    if cache is None and run is not None:
        cache = _first_dir([run.parent.parent / PACK_SUBDIR])
    raw = _first_dir([raw_dir, os.environ.get('BILI_RAW_DIR')])
    if raw is None and cache is not None:
        # 素材包缓存在 <库>/.workbuddy/cache/bili 时，纪要在 <库>/raw（上溯三级）。
        # 这一条必须排在通用默认值前面：调用方给了素材包目录时，从它推出来的位置
        # 比「本机通用默认」更贴近实际情况。
        raw = _first_dir([cache.parents[2] / 'raw', cache.parent / 'raw'])
    if raw is None:
        raw = _first_dir([lq.DEFAULT_RAW, Path.home() / 'obsidian' / 'raw'])
    return raw, cache


def pack_paths(cache_dir, bv):
    """该任务的全部素材包路径，归档目录优先，其次缓存目录。

    归档目录与缓存目录同级（`cache/bili_subs`），与 `library_queue --finish` 的
    归档目标一致 —— 当成子目录会一条都对不上。

    进度上的一行＝一支视频，多 P 视频的 id 是裸 BV 号，而素材包按页分文件
    （`bili_<BV>_p<页>.md`）。只认 `bili_<BV>.md` 会让多 P 视频被判成
    「素材包不存在」而永远补发不出完成信号（2026-10-03 实测：ck3 五分P 全部落空）。
    """
    m = PART_RE.search(str(bv))
    stem = str(bv)[:m.start()] if m else str(bv)
    out = []
    for d in (Path(cache_dir).parent / PACK_ARCHIVE_SUBDIR, Path(cache_dir)):
        pages = sorted(d.glob(f'bili_{stem}_p*.md'),
                       key=lambda p: int(PART_RE.search(p.stem).group(1)))
        out.extend(pages)
        plain = d / f'bili_{stem}.md'
        if plain.is_file():
            out.append(plain)              # 单P 视频只有这一份
    return out


def note_for(bv, cache_dir, raw_dir):
    """返回 (纪要文件列表, 素材包列表, 跳过原因)。前两者只有一组非空。

    多 P 视频要每一页都配到纪要才算写完：进度只有一行，只配上一页就补发完成，
    会把还没写纪要的页一起报成已完成。
    """
    packs = pack_paths(cache_dir, bv)
    if not packs:
        return None, None, '素材包不存在'
    notes, why = [], None
    for p in packs:
        head = lq.parse_pack_header(p)
        missing = [k for k in ('pubdate', 'title', 'up') if not head.get(k)]
        if missing:
            return None, packs, '素材包头部缺字段：%s（%s）' % (' / '.join(missing), p.name)
        cand, w = find_note_file(raw_dir,
                                 lq.note_name(head['pubdate'], head['title'], head['up']))
        if cand is None:
            why = w
            break
        if cand not in notes:              # 各页标题相同时指向同一份纪要
            notes.append(cand)
    if why:
        return None, packs, why
    return notes, packs, None


def _last_ts(d, bv, stage=None):
    """该任务最后一条事件的时刻；给了 stage 就只看该阶段。

    事件里的任务名要按进度的口径归一（分P 后缀并到裸 BV），否则多 P 任务
    按裸 BV 查不到任何一条事件，已用时间就一直是空。
    """
    _run, events = ph._load_raw(d)
    hits = [e['ts'] for e in events
            if ph._norm_id(e.get('task')) == bv and e.get('ts')
            and (stage is None or e.get('stage') == stage)]
    return max(hits) if hits else None


def register_pending(name, d=None):
    """登记一条「待编译」条目，让「纪要写完了、wiki 还没编译」在进度上可见。

    不加这一步，进度会显示成整批全部完成 —— 转写侧的「已完成」只表示纪要写完，
    编译是另一件事（用户 2026-10-02 的裁决）。
    """
    task = {'id': name, 'title': '编译 ' + name, 'up': 'wiki 编译',
            'pred_sec': 90, 'engine': 'wiki'}
    ph.init_run([task], d=d, merge=True, group='wiki编译')
    return bool(ph.emit(task=name, stage='pending', pct=0.0, group='wiki编译',
                        note='纪要已写完，待编译进 wiki', d=d))


def reconcile(d=None, raw_dir=None, cache_dir=None, dry=False, only=None,
              pend=True):
    """把「纪要已写完」的任务补发成已完成，返回对账结果。"""
    hint = cache_dir or os.environ.get('BILI_CACHE_DIR')
    run = resolve_run_dir(d, hint)
    if run is None:
        return {'skipped': '找不到进度运行目录：设 BILI_PROGRESS_DIR，或用 --dir 指定'}
    raw, cache = resolve_dirs(raw_dir, hint, run)
    if raw is None or cache is None:
        return {'error': '目录无法定位', 'run_dir': str(run),
                'raw_dir': str(raw), 'cache_dir': str(cache),
                'hint': '用 --raw-dir / --cache-dir 指定，或设 BILI_RAW_DIR / BILI_CACHE_DIR'}

    wanted = set(only) if only else None
    snap = ph.snapshot(run)
    listed = {str(t['id']) for t in snap['tasks']}   # 已登记的任务名（含编译侧）
    items = []
    for t in snap['tasks']:
        bv = str(t['id'])
        if t['stage'] in TERMINAL or not bv.startswith('BV'):
            continue
        if wanted is not None and bv not in wanted:
            continue
        row = {'task': bv, 'title': t.get('title'), 'stage': t['stage']}
        notes, packs, why = note_for(bv, cache, raw)
        if packs:
            row['pack_files'] = [str(p) for p in packs]
        if notes is None:
            row['skipped'] = why
            items.append(row)
            continue
        row['note_files'] = [str(n) for n in notes]
        end = _last_ts(run, bv, 'transcribed') or _last_ts(run, bv)
        last = max(notes, key=lambda n: n.stat().st_mtime)
        row['elapsed'] = round(max(0.0, last.stat().st_mtime - end), 1) if end else None
        if not dry:
            row['emitted'] = bool(ph.emit(
                task=bv, stage='done', pct=100.0, kind='end', elapsed=row['elapsed'],
                duration_sec=t.get('duration_sec'),
                note='已完成（依纪要文件自动补发）', d=run))
            # 顺带把「待编译」挂上进度：这条只走到纪要，编译是另一件事
            if row['emitted'] and pend:
                reg = []
                for n in notes:
                    name = n.stem
                    if name in listed:
                        reg.append(name + '（已登记）')
                    elif register_pending(name, d=run):
                        listed.add(name)
                        reg.append(name)
                row['pending'] = reg
        items.append(row)
    return {'run_dir': str(run), 'raw_dir': str(raw), 'cache_dir': str(cache),
            'dry_run': dry,
            'done': sum(1 for r in items if r.get('note_files')),
            'skipped': sum(1 for r in items if r.get('skipped')), 'items': items}


def main():
    ap = argparse.ArgumentParser(description='按纪要文件补发进度完成信号')
    ap.add_argument('--dir', default=None, help='进度运行目录（默认 $BILI_PROGRESS_DIR）')
    ap.add_argument('--raw-dir', default=None, help='纪要落盘目录（默认 $BILI_RAW_DIR）')
    ap.add_argument('--cache-dir', default=None, help='素材包目录（默认 $BILI_CACHE_DIR）')
    ap.add_argument('--dry-run', action='store_true', help='只列出会补哪些，不上报')
    ap.add_argument('--only', default=None, help='只处理指定的 BV 号，逗号分隔')
    ap.add_argument('--no-pending', action='store_true',
                    help='不登记「待编译」条目（默认会登记，未编译的才看得见）')
    a = ap.parse_args()
    only = [x.strip() for x in a.only.split(',') if x.strip()] if a.only else None
    out = reconcile(a.dir, a.raw_dir, a.cache_dir, dry=a.dry_run, only=only,
                    pend=not a.no_pending)
    print(json.dumps(out, ensure_ascii=False, indent=2))
    return 1 if out.get('error') else 0


if __name__ == '__main__':
    sys.exit(main())
