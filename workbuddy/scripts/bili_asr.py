#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
B站视频 → 本地 Whisper 转写 → 结构化素材包
用法:
  python bili_asr.py <B站链接|BV号> [--model large-v3] [--device auto] [--p N]
                     [--out FILE] [--cookie "..."] [--keep-audio] [--no-comments]
                     [--hf-mirror] [--lang zh]
依赖: faster-whisper, yt-dlp, imageio-ffmpeg
"""
import argparse, hashlib, json, os, re, shutil, subprocess, sys, time, urllib.parse, urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

# WBI 签名 / HTTP 公共能力抽到 bili_wbi.py，与 search_bili.py 共用，
# 避免 B站调整接口时要在多处同步修改
from bili_wbi import UA, http_json, wbi_url
HOME = Path.home()
MODEL_DIR = HOME / '.workbuddy' / 'models' / 'whisper'
AUDIO_DIR = Path(os.environ.get('BILI_TMP', str(HOME / '.workbuddy' / 'tmp' / 'bili_audio')))
COOKIE_FILE = HOME / '.workbuddy' / '.bilibili_cookie'


# ---------------- 凭据 ----------------
def cookie_header(extra=None):
    c = (extra or '').strip()
    if not c and COOKIE_FILE.exists():
        c = COOKIE_FILE.read_text(encoding='utf-8').strip()
    if c and 'buvid3=' not in c.lower():
        c += f'; buvid3={_uuid()}infoc'
    return c or f'buvid3={_uuid()}infoc'


def _uuid():
    import uuid
    return str(uuid.uuid4()).upper()


_H = lambda: {'User-Agent': UA, 'Referer': 'https://www.bilibili.com/', 'Cookie': COOKIE_HEADER}
COOKIE_HEADER = ''




# ---------------- utils ----------------
def fmt_ts(sec):
    h, m, s = int(sec // 3600), int(sec % 3600 // 60), int(sec % 60)
    return f"{h:02d}:{m:02d}:{s:02d}"


def fmt_dur(sec):
    h, m, s = int(sec // 3600), int(sec % 3600 // 60), int(sec % 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def fmt_date(u):
    return time.strftime('%Y-%m-%d %H:%M', time.localtime(u))


def resolve_input(raw):
    """解析链接/BV/av，返回 (canonical_url, page)"""
    s = raw.strip()
    if re.search(r'b23\.tv', s, re.I):
        m = re.search(r'https?://b23\.tv/\S+', s, re.I)
        url = m.group(0) if m else ('https://' + s if not s.startswith('http') else s)
        req = urllib.request.Request(url, headers={'User-Agent': UA})
        with urllib.request.urlopen(req, timeout=20) as r:
            s = r.geturl()
    page = int(re.search(r'[?&]p=(\d+)', s).group(1)) if re.search(r'[?&]p=(\d+)', s) else 1
    bv = re.search(r'(BV[0-9A-Za-z]{10})', s)
    if bv:
        return f"https://www.bilibili.com/video/{bv.group(1)}", page, bv.group(1)
    av = re.search(r'(?:av|aid)[=/]?(\d{1,12})', s, re.I)
    if av:
        return f"https://www.bilibili.com/video/av{av.group(1)}", page, None
    raise ValueError(f'无法解析链接: {raw}')


# ---------------- 元信息 ----------------
def fetch_meta(bvid_or_url, page, aid=None):
    q = f"?bvid={bvid_or_url}" if bvid_or_url and bvid_or_url.startswith('BV') else f"?aid={aid}"
    if bvid_or_url and not bvid_or_url.startswith('BV'):
        url = bvid_or_url
        m = re.search(r'(BV[0-9A-Za-z]{10})', url)
        q = f"?bvid={m.group(1)}" if m else f"?aid={re.search(r'av(\d+)', url).group(1)}"
    v = http_json('https://api.bilibili.com/x/web-interface/view' + q, _H())
    if v.get('code') != 0:
        raise RuntimeError(f"视频信息失败 [{v.get('code')}] {v.get('message')}")
    return v['data']


def fetch_comments(aid, n=20):
    try:
        j = http_json(f"https://api.bilibili.com/x/v2/reply?type=1&oid={aid}&sort=2&ps={n}&pn=1", _H())
        return j.get('data', {}).get('replies') or []
    except Exception:
        return []


def fetch_player(bvid, cid):
    """返回 (chapters, subtitles)。未登录时字幕恒为空列表。"""
    try:
        j = http_json(wbi_url('https://api.bilibili.com/x/player/wbi/v2',
                              {'bvid': bvid, 'cid': cid}, _H()), _H())
        d = j.get('data') or {}
        return (d.get('view_points') or []), ((d.get('subtitle') or {}).get('subtitles') or [])
    except Exception:
        return [], []


def dedup_lines(lines):
    """AI 字幕滚动式去重：后句是前句的扩展则保留更长者"""
    out = []
    for l in lines:
        prev = out[-1] if out else None
        if prev and (l['content'].startswith(prev['content']) or prev['content'].startswith(l['content'])):
            if len(l['content']) > len(prev['content']):
                out[-1] = l
            continue
        out.append(l)
    return out


def fetch_subtitle_text(subs):
    """选最优中文字幕并返回 (文本, 标签)；无可用字幕返回 (None, None)"""
    pick = (next((s for s in subs if re.match(r'^zh[-_]?CN$', s.get('lan', ''), re.I)), None)
            or next((s for s in subs if re.match(r'^ai[-_]?zh$', s.get('lan', ''), re.I)), None)
            or next((s for s in subs if re.search(r'zh|cn', s.get('lan', ''), re.I)), None)
            or (subs[0] if subs else None))
    if not pick:
        return None, None
    try:
        u = pick['subtitle_url']
        u = 'https:' + u if u.startswith('//') else u
        sj = http_json(u, {'User-Agent': UA})
        body = dedup_lines(sj.get('body') or [])
        if not body:
            return None, None
        label = pick.get('lan_doc') or pick.get('lan')
        if re.match(r'^ai', pick.get('lan', ''), re.I):
            label += '（AI 生成，可能有错字）'
        text = '\n'.join(f"[{fmt_ts(b['from'])}] {b['content']}" for b in body)
        return text, label
    except Exception:
        return None, None


# ---------------- 音频 ----------------
def ffmpeg_path():
    try:
        import imageio_ffmpeg
        return imageio_ffmpeg.get_ffmpeg_exe()
    except Exception:
        return shutil.which('ffmpeg') or 'ffmpeg'


def download_audio(url, out_dir, cookie=None, page=1, stem='audio'):
    out_dir.mkdir(parents=True, exist_ok=True)
    # 清理同名旧文件，避免 yt-dlp 跳过下载导致 filepath 与实际文件名不符
    for f in out_dir.glob(stem + '.*'):
        try:
            f.unlink()
        except Exception:
            pass
    tpl = str(out_dir / (stem + '.%(ext)s'))
    # 不转码：B站音轨本身就是 m4a(AAC)，PyAV 可直接解码，省掉 ffmpeg 转码开销
    cmd = [sys.executable, '-m', 'yt_dlp', '-f', 'bestaudio/best',
           '--ffmpeg-location', ffmpeg_path(),
           '--no-playlist', '-o', tpl, '--no-warnings', '--print-json']
    cu = url if 'p=' in url else f"{url}?p={page}"
    # 明文 cookie 只在下载期间临时落盘，finally 里必定删除
    cf = None
    if cookie:
        cf = out_dir / 'cookies.txt'
        cf.write_text('# Netscape HTTP Cookie File\n' + '\n'.join(
            f".bilibili.com\tTRUE\t/\tFALSE\t0\t{c.split('=')[0].strip()}\t{c.split('=', 1)[1].strip()}"
            for c in cookie.split(';') if '=' in c), encoding='utf-8')
        cmd += ['--cookies', str(cf)]
    cmd.append(cu)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           encoding='utf-8', errors='replace')
        if p.returncode != 0:
            raise RuntimeError('音频下载失败: ' + (p.stderr or p.stdout)[-800:])
        line = (p.stdout or '').strip().splitlines()[-1]
        info = json.loads(line)
    finally:
        # 登录凭据不留盘：Windows 上 chmod 权限不可靠，只能靠删除兜底
        if cf is not None:
            try:
                cf.unlink()
            except Exception:
                pass
    # requested_downloads 可能是空列表，不能直接 [0]
    rd = info.get('requested_downloads') or []
    fp = (rd[0].get('filepath') if rd else '') or info.get('_filename')
    # 兜底：filepath 与实际落盘文件名不一致时，按 stem 找回最新产物
    exts = ('.mp3', '.m4a', '.webm', '.wav', '.opus', '.flac', '.aac')
    if not fp or not os.path.exists(fp):
        cands = [q for q in out_dir.glob(stem + '.*') if q.suffix.lower() in exts]
        if not cands:
            raise RuntimeError('音频下载完成但找不到产物: ' + str(out_dir / stem))
        fp = str(max(cands, key=lambda q: q.stat().st_mtime))
    return fp, info


# ---------------- ASR ----------------
def _setup_cuda_dlls():
    """pip 安装的 nvidia-* 包把 DLL 放在 site-packages/nvidia/*/bin，需显式注册"""
    if os.name != 'nt':
        return
    import glob, site, sysconfig
    try:
        bases = list(site.getsitepackages())
    except Exception:
        bases = []
    bases.append(sysconfig.get_paths().get('purelib', ''))
    for base in bases:
        if not base or not os.path.isdir(base):
            continue
        for d in glob.glob(os.path.join(base, 'nvidia', '*', 'bin')):
            if os.path.isdir(d):
                if hasattr(os, 'add_dll_directory'):
                    try:
                        os.add_dll_directory(d)
                    except Exception:
                        pass
                os.environ['PATH'] = d + os.pathsep + os.environ.get('PATH', '')


def _load_model(model_size, device, compute, hf_mirror):
    if hf_mirror:
        os.environ['HF_ENDPOINT'] = 'https://hf-mirror.com'
    os.environ['HF_HUB_DISABLE_XET'] = '1'
    os.environ.setdefault('HF_HUB_DISABLE_SYMLINKS', '1')
    _setup_cuda_dlls()
    from faster_whisper import WhisperModel
    import ctranslate2
    if device == 'auto':
        device = 'cuda' if ctranslate2.get_cuda_device_count() > 0 else 'cpu'
    if device == 'cpu':
        compute = 'int8'
    MODEL_DIR.mkdir(parents=True, exist_ok=True)
    model = WhisperModel(model_size, device=device, compute_type=compute,
                         download_root=str(MODEL_DIR))
    return model, device


class ModelHolder:
    """惰性加载 Whisper 模型。批量处理时整批只加载一次（实测每次加载约 4s）。"""

    def __init__(self, model_size, device, compute, hf_mirror=True):
        self.model_size = model_size
        self.device = device
        self.compute = compute
        self.hf_mirror = hf_mirror
        self._model = None
        self._device = None

    def get(self):
        if self._model is None:
            self._model, self._device = _load_model(
                self.model_size, self.device, self.compute, self.hf_mirror)
        return self._model, self._device


def transcribe_with(model, device, audio, lang, prompt):
    segs, info = model.transcribe(
        audio, language=lang, beam_size=5, vad_filter=True,
        vad_parameters=dict(min_silence_duration_ms=500),
        initial_prompt=prompt, condition_on_previous_text=False, temperature=0)
    out = []
    for s in segs:
        out.append({'start': s.start, 'end': s.end, 'text': s.text.strip()})
    return out, {'language': info.language, 'duration': info.duration, 'device': device}


def transcribe(audio, model_size, device, compute, lang, hf_mirror, prompt):
    model, dev = _load_model(model_size, device, compute, hf_mirror)
    return transcribe_with(model, dev, audio, lang, prompt)


def merge_segments(segs, max_chars=90, max_gap=1.2, max_len=25.0):
    """合并短句为可读段落，保留时间戳"""
    blocks = []
    cur = None
    for s in segs:
        t = s['text'].strip()
        if not t:
            continue
        if cur is None:
            cur = {'start': s['start'], 'end': s['end'], 'text': t}
            continue
        gap = s['start'] - cur['end']
        if (len(cur['text']) + len(t) <= max_chars and gap <= max_gap
                and s['end'] - cur['start'] <= max_len):
            cur['text'] += t
            cur['end'] = s['end']
        else:
            blocks.append(cur)
            cur = {'start': s['start'], 'end': s['end'], 'text': t}
    if cur:
        blocks.append(cur)
    return blocks


# ---------------- 广告识别 ----------------
AD_KEYWORDS_FILE = Path(__file__).resolve().parent.parent / 'references' / 'ad_keywords.txt'


def load_ad_keywords():
    if not AD_KEYWORDS_FILE.exists():
        return []
    out = []
    for line in AD_KEYWORDS_FILE.read_text(encoding='utf-8').splitlines():
        s = line.strip()
        if s and not s.startswith('#'):
            out.append(s)
    return out


def mark_ads(blocks, keywords):
    """标记疑似广告段落。返回 (广告段落列表, 命中数)。blocks 原地加 is_ad / ad_hits。"""
    hits = []
    for b in blocks:
        found = [k for k in keywords if k in b['text']]
        b['ad_hits'] = found
        b['is_ad'] = bool(found)
        if found:
            hits.append({'start': b['start'], 'end': b['end'],
                         'keywords': found, 'text': b['text'][:60]})
    return hits, len(hits)


# ---------------- main ----------------
def build_parser():
    ap = argparse.ArgumentParser()
    ap.add_argument('url', nargs='?', default=None)
    ap.add_argument('--model', default='large-v3-turbo',
                    help='large-v3-turbo（默认，速度/精度平衡）/ large-v3（最准）/ medium（最快）')
    ap.add_argument('--device', default='auto')
    ap.add_argument('--compute', default='int8_float16')
    ap.add_argument('--lang', default='zh')
    ap.add_argument('--prompt', default='以下是普通话的中文内容，包含专业术语。请使用正确的中文标点符号。')
    ap.add_argument('--p', type=int, default=1)
    ap.add_argument('--out', default=None)
    ap.add_argument('--cookie', default=None)
    ap.add_argument('--keep-audio', action='store_true')
    ap.add_argument('--no-comments', action='store_true')
    ap.add_argument('--hf-mirror', action='store_true', default=True)
    ap.add_argument('--no-hf-mirror', dest='hf_mirror', action='store_false')
    ap.add_argument('--only-meta', action='store_true', help='仅抓元信息，不取字幕不转写')
    ap.add_argument('--force-asr', action='store_true', help='跳过 B站字幕直取，强制本地转写')
    ap.add_argument('--batch-file', default=None,
                    help='批量模式：JSON 文件 [{"url":..., "out":...}]，整批只加载一次模型')
    return ap


def process_video(a, url_arg, out_path, holder=None):
    """处理单个视频，返回结构化结果（不打印）。holder 非空时复用其模型实例。"""
    url, page, bvid = resolve_input(url_arg)
    V = fetch_meta(url, page)
    bvid = V['bvid']
    pages = V.get('pages') or [{'page': 1, 'cid': V['cid'], 'part': '', 'duration': V['duration']}]
    pinfo = next((x for x in pages if x['page'] == page), pages[0]) if page <= len(pages) else pages[0]
    cid = pinfo['cid']

    L = ['# B站视频素材包（本地 ASR 转写）', '', '## 基本信息',
         f"- 标题：{V['title']}",
         f"- BV号：{bvid} ｜ av号：{V['aid']} ｜ cid：{cid} ｜ 分P：{page}/{V.get('videos',1)}",
         f"- UP主：{V['owner']['name']}（mid:{V['owner']['mid']}）",
         f"- 发布：{fmt_date(V['pubdate'])} ｜ 时长：{fmt_dur(pinfo.get('duration') or V['duration'])}",
         f"- 链接：{url}"]
    st = V.get('stat') or {}
    L.append(f"- 数据：播放 {st.get('view')} ｜ 点赞 {st.get('like')} ｜ 投币 {st.get('coin')} ｜ "
             f"收藏 {st.get('favorite')} ｜ 评论 {st.get('reply')} ｜ 弹幕 {st.get('danmaku')}")
    desc = '\n'.join(d['raw_text'] for d in (V.get('desc_v2') or [])) or V.get('desc', '')
    if desc.strip() and desc.strip() != '-':
        L += ['', '## 视频简介', desc.strip()]
    if len(pages) > 1:
        L += ['', '## 分P列表'] + [f"- P{p['page']} {p.get('part','')}（{fmt_dur(p.get('duration',0))})"
                                   + ('  ← 当前' if p['page'] == page else '') for p in pages]
    ch, subs = fetch_player(bvid, cid)
    if ch:
        L += ['', '## 章节（UP主标记）'] + [f"- [{fmt_ts(c['from'])}] {c['content']}" for c in ch]

    logged_in = 'SESSDATA=' in COOKIE_HEADER.upper()
    route = None
    asr_info = None
    if not a.only_meta:
        # 路由 1：B站字幕直取（秒级，需登录态且视频开放字幕）
        if not a.force_asr and subs:
            sub_text, sub_label = fetch_subtitle_text(subs)
            if sub_text:
                route = f'subtitle:{sub_label}'
                L += ['', f'## 字幕全文（来源：{sub_label}）', '', sub_text]

        # 路由 2：本地 ASR 转写
        if route is None:
            if not a.force_asr and not logged_in:
                L += ['', '## 字幕',
                      '> 未配置登录凭据，B站 CC/AI 字幕接口不可用，已直接走本地 ASR 转写。']
            elif not a.force_asr:
                L += ['', '## 字幕',
                      '> 该视频未开放字幕，已降级为本地 ASR 转写。']
            t0 = time.time()
            AUDIO_DIR.mkdir(parents=True, exist_ok=True)
            fp, info = download_audio(url, AUDIO_DIR, COOKIE_HEADER, page, stem=f'{bvid}_p{page}')
            dl = time.time() - t0
            t1 = time.time()
            if holder is None:
                holder = ModelHolder(a.model, a.device, a.compute, a.hf_mirror)
            model, dev = holder.get()
            segs, meta = transcribe_with(model, dev, fp, a.lang, a.prompt)
            asr_info = {'audio_sec': round(meta['duration'], 1), 'download_sec': round(dl, 1),
                        'asr_sec': round(time.time() - t1, 1), 'device': meta['device'],
                        'model': a.model, 'segments': len(segs), 'lang': meta['language']}
            blocks = merge_segments(segs)
            ad_kw = load_ad_keywords()
            ad_hits, ad_count = mark_ads(blocks, ad_kw)
            asr_info['ad_segments'] = ad_count
            route = f'asr:{a.model}@{meta["device"]}'
            L += ['', f"## 转写全文（本地 Whisper {a.model} · {meta['device']}）",
                  '> 标注 `[广告?]` 的行为疑似带货口播，生成纪要时跳过，不写入结论。', '']
            L += [f"[{fmt_ts(b['start'])}] {'[广告?] ' if b.get('is_ad') else ''}{b['text']}"
                  for b in blocks]
            if ad_hits:
                L += ['', f'## 疑似广告段落（{ad_count} 段，生成纪要时跳过）', '']
                L += ['| 时间戳 | 命中词 | 内容 |', '|---|---|---|']
                L += [f"| {fmt_ts(h['start'])} | {'/'.join(h['keywords'])} | {h['text']} |"
                      for h in ad_hits]
            if not a.keep_audio:
                try:
                    os.remove(fp)
                except Exception:
                    pass

    if not a.no_comments:
        rs = fetch_comments(V['aid'])
        if rs:
            L += ['', '## 热门评论 Top20']
            for i, r in enumerate(rs, 1):
                L.append(f"{i}. @{r['member']['uname']}（{r['like']}赞）："
                         f"{r['content']['message'].replace(chr(10),' ')[:200]}")

    text = '\n'.join(L)
    out = Path(out_path)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(text, encoding='utf-8')
    return {'ok': True, 'bvid': bvid, 'title': V['title'], 'up': V['owner']['name'],
            'duration': fmt_dur(pinfo.get('duration') or V['duration']),
            'route': route, 'logged_in': logged_in,
            'out': str(out).replace('\\', '/'), 'chars': len(text),
            'asr': asr_info}


def main():
    a = build_parser().parse_args()

    global COOKIE_HEADER
    COOKIE_HEADER = cookie_header(a.cookie)

    if a.batch_file:
        items = json.loads(Path(a.batch_file).read_text(encoding='utf-8'))
        if not items:
            print(json.dumps({'ok': True, 'batch': True, 'count': 0, 'results': []}))
            return
        holder = ModelHolder(a.model, a.device, a.compute, a.hf_mirror)
        results = []
        for it in items:
            try:
                results.append(process_video(a, it['url'], it['out'], holder))
            except Exception as e:
                results.append({'ok': False, 'url': it.get('url'), 'error': str(e)})
        print(json.dumps({'ok': True, 'batch': True, 'count': len(results),
                          'results': results}, ensure_ascii=False))
        return

    if not a.url:
        print(json.dumps({'ok': False, 'error': '缺少视频链接或 --batch-file'}))
        sys.exit(1)
    url, page, bvid = resolve_input(a.url)
    default_out = Path('outputs') / f"bili_{bvid}{'_p'+str(page) if page>1 else ''}.md"
    r = process_video(a, a.url, a.out or str(default_out))
    print(json.dumps(r, ensure_ascii=False, indent=2))


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(json.dumps({'ok': False, 'error': str(e)}, ensure_ascii=False, indent=2))
        sys.exit(1)
