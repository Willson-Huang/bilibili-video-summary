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
import argparse, hashlib, json, os, re, shutil, subprocess, sys, tempfile, time, urllib.parse, urllib.request
from pathlib import Path

sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

# WBI 签名 / HTTP 公共能力抽到 bili_wbi.py，与 search_bili.py 共用
from bili_wbi import UA, http_json, wbi_url
HOME = Path.home()
# 路径全部可用环境变量覆盖，便于迁移到其他机器 / 便携部署
MODEL_DIR = Path(os.environ.get('BILI_MODEL_DIR',
                                str(HOME / '.workbuddy' / 'models' / 'whisper')))
AUDIO_DIR = Path(os.environ.get('BILI_TMP', str(HOME / '.workbuddy' / 'tmp' / 'bili_audio')))
COOKIE_FILE = Path(os.environ.get('BILI_COOKIE', str(HOME / '.workbuddy' / '.bilibili_cookie')))
# Nano 引擎跑在独立 venv（funasr 与 faster-whisper/CTranslate2 依赖冲突，绝不合并装）
PY_NANO = Path(os.environ.get('BILI_PYTHON_NANO', str(
    HOME / '.workbuddy' / 'binaries' / 'python' / 'envs' / 'asr_eval' / 'Scripts' / 'python.exe')))
NANO_ADAPTER = Path(__file__).resolve().parent / 'funasr_adapter.py'
UP_WHITELIST = Path(__file__).resolve().parent.parent / 'references' / 'engine_up_whitelist.txt'
# 分区映射表不随本仓库分发（上游文档因合规原因已关停）；缺失时该信号自动跳过
TID_V2_MAP = Path(os.environ.get('BILI_TID_MAP', str(
    Path(__file__).resolve().parent.parent / 'references' / 'bilibili_tid_v2_map.txt')))

# 关键词打分表：命中 NANO_HINTS +1，命中 WHISPER_HINTS -1，净分 >=1 建议 nano
NANO_HINTS = (
    # 地理/行政区划
    '省', '市', '县', '区', '半岛', '群岛', '山脉', '盆地', '平原', '流域', '古城', '古镇',
    '地理', '地图', '疆', '边境', '口岸', '迁徙', '民系', '方言',
    # 历史
    '朝', '皇帝', '王朝', '古国', '遗址', '文物', '考古', '科举', '封地', '避讳',
    '宋', '明', '唐', '汉', '秦', '元', '清', '周', '徽宗', '康熙', '乾隆',
    '史', '古代', '近代', '战争史', '起源', '变迁',
    # 政经/国际
    '地缘', '制裁', '关税', '财政', '化债', '选举', '战争', '条约', '联邦', '共和国',
    '霍尔木兹', 'G7', 'G20', '北约', '欧盟', '东盟',
)
# 专名模式（正则命中 +1）：避免穷举地名，靠构词规律召回未知地名/人名
NANO_PATTERNS = (
    r'[\u4e00-\u9fa5]{2,4}为什么',      # 「XX为什么」
    r'[\u4e00-\u9fa5]{2,6}是怎么',       # 「XX是怎么」
    r'一口气(了解|看懂|讲完)',            # 系列科普
    r'[\u4e00-\u9fa5]{2,3}(州|府|郡|城)的历史',
)
WHISPER_HINTS = (
    'AI', 'ai', '模型', 'GPU', '芯片', '手机', '算法', '代码', '编程', '系统', '软件',
    'OpenAI', 'Anthropic', 'Claude', 'GPT', '豆包', 'WorkBuddy', 'OpenClaw', 'Agent',
    '评测', '教程', '方法论', '效率',
)


def load_up_whitelist():
    """读 references/engine_up_whitelist.txt，返回 {UP主名: (引擎, 依据, 是否已实测)}"""
    out = {}
    if not UP_WHITELIST.is_file():
        return out
    for line in UP_WHITELIST.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = [p.strip() for p in line.split('|')]
        if len(parts) >= 2 and parts[1] in ('whisper', 'funasr-nano'):
            why = parts[2] if len(parts) > 2 else ''
            kind = parts[4] if len(parts) > 4 else ''
            out[parts[0]] = (parts[1], why, kind.startswith('实测'))
    return out


def fetch_video_tags(bvid):
    """视频标签（官方 tag 接口）。实测比分区更能反映内容主题。"""
    try:
        j = http_json(f'https://api.bilibili.com/x/tag/archive/tags?bvid={bvid}', _H())
        return [t.get('tag_name', '') for t in (j.get('data') or []) if t.get('tag_name')]
    except Exception:
        return []


def load_tid_v2_map():
    """读 references/bilibili_tid_v2_map.txt → {tid_v2: (子分区, 主分区)}"""
    out = {}
    if not TID_V2_MAP.is_file():
        return out
    for line in TID_V2_MAP.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if not line or line.startswith('#'):
            continue
        parts = [p.strip() for p in line.split('|')]
        if len(parts) >= 3:
            out[parts[0]] = (parts[1], parts[2])
    return out


def classify_engine(meta, tags=None):
    """按 UP主白名单 → 标题/简介关键词打分 → 默认 whisper 给出引擎建议。

    代价不对称：漏切（仍是 whisper）= 维持现状；误切 = 慢 3-10 倍 + 英文质量下降。
    所以灰区（净分 0 或无信号）一律判 whisper。
    """
    title = meta.get('title') or ''
    desc = meta.get('desc') or ''
    up = (meta.get('owner') or {}).get('name') or ''
    text = f'{title} {desc}'

    wl = load_up_whitelist()
    if up in wl:
        engine, why, verified = wl[up]
        r = {'engine': engine, 'confidence': 'high', 'source': 'up_whitelist',
             'up': up, 'reason': why or '白名单命中', 'verified': verified}
        if not verified:
            r['verify_hint'] = ('该 UP 主为标题归类、未经实测。本次跑完后请顺带做一次专名核对，'
                                '结论写回 references/engine_up_whitelist.txt 并追加 engine_verify_log.txt')
        return r

    # 2) 视频 tag（官方公开标签接口，实测最准：地理类视频的 tag 多为地名）
    if tags:
        tag_text = ' '.join(tags)
        t_nano = [k for k in NANO_HINTS if k in tag_text]
        t_whisper = [k for k in WHISPER_HINTS if k in tag_text]
        # tag 里地名/机构名占比高 → 专名密集
        if len(t_nano) - len(t_whisper) >= 2:
            return {'engine': 'funasr-nano', 'confidence': 'high', 'source': 'video_tag',
                    'up': up, 'tags': tags[:10],
                    'reason': f'tag 命中专名信号 {len(t_nano)} 个（如 {t_nano[:3]}）'}
        if len(t_whisper) - len(t_nano) >= 2:
            return {'engine': 'whisper', 'confidence': 'high', 'source': 'video_tag',
                    'up': up, 'tags': tags[:10],
                    'reason': f'tag 偏科技/英文向（{t_whisper[:3]}）'}

    # 3) 分区（可选映射表，仅作辅助：实测地理类视频也被归到商业财经）
    zmap = load_tid_v2_map()
    tid_v2 = str(meta.get('tid_v2') or '')
    if tid_v2 in zmap:
        sub, main = zmap[tid_v2]
        if any(k in sub + main for k in ('财经', '时政', '历史', '社科', '人文', '军事')):
            return {'engine': 'funasr-nano', 'confidence': 'mid', 'source': 'zone',
                    'up': up, 'zone': f'{main}·{sub}',
                    'reason': f'分区 {main}·{sub} 属专名密集类（分区仅供辅助）'}
        if any(k in sub + main for k in ('数码', '软件', '电脑', '编程')):
            return {'engine': 'whisper', 'confidence': 'mid', 'source': 'zone',
                    'up': up, 'zone': f'{main}·{sub}',
                    'reason': f'分区 {main}·{sub} 偏技术向'}

    hits_nano = [k for k in NANO_HINTS if k in text]
    pat_hits = [p for p in NANO_PATTERNS if re.search(p, text)]
    hits_whisper = [k for k in WHISPER_HINTS if k in text]
    score = len(hits_nano) + len(pat_hits) - len(hits_whisper)
    if score >= 1:
        return {'engine': 'funasr-nano', 'confidence': 'mid' if score >= 2 else 'low',
                'source': 'keyword', 'up': up, 'score': score,
                'hits': (hits_nano + [f'pattern:{p[:12]}' for p in pat_hits])[:8],
                'reason': f'标题/简介命中专名信号 {len(hits_nano) + len(pat_hits)} 个'}
    return {'engine': 'whisper', 'confidence': 'default', 'source': 'default', 'up': up,
            'score': score, 'hits_whisper': hits_whisper[:5],
            'reason': '无明确专名信号或偏科技向，默认 whisper（漏切代价小于误切）'}


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
META_CACHE_DIR = HOME / '.workbuddy' / 'cache' / 'bili' / 'meta'
META_CACHE_TTL = 86400  # 秒；24h 内复用元信息缓存，避免队列阶段一/阶段二重复抓 view


def _meta_cache_path(bvid):
    return META_CACHE_DIR / f'{bvid}.json'


def fetch_meta(bvid_or_url, page, aid=None):
    """取视频元信息（view 接口）。bvid 已知时优先读本地缓存，命中即跳过网络请求。"""
    m_bv = re.search(r'(BV[0-9A-Za-z]{10})', bvid_or_url) if isinstance(bvid_or_url, str) else None
    key = m_bv.group(1) if m_bv else None
    if key:
        cp = _meta_cache_path(key)
        try:
            if cp.is_file() and time.time() - cp.stat().st_mtime < META_CACHE_TTL:
                cached = json.loads(cp.read_text(encoding='utf-8'))
                if cached.get('bvid'):
                    return cached
        except Exception:
            pass
    q = f"?bvid={bvid_or_url}" if bvid_or_url and bvid_or_url.startswith('BV') else f"?aid={aid}"
    if bvid_or_url and not bvid_or_url.startswith('BV'):
        url = bvid_or_url
        m = re.search(r'(BV[0-9A-Za-z]{10})', url)
        q = f"?bvid={m.group(1)}" if m else f"?aid={re.search(r'av(\d+)', url).group(1)}"
    v = http_json('https://api.bilibili.com/x/web-interface/view' + q, _H())
    if v.get('code') != 0:
        raise RuntimeError(f"视频信息失败 [{v.get('code')}] {v.get('message')}")
    data = v['data']
    try:
        if data.get('bvid'):
            cp = _meta_cache_path(data['bvid'])
            cp.parent.mkdir(parents=True, exist_ok=True)
            cp.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
    except Exception:
        pass
    return data


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
    """选最优中文字幕并返回 (文本, 标签)；无中文字幕返回 (None, None) 走 ASR。
    不兜底取非中文字幕：中文视频拿英文 AI 字幕是回译，术语全部失真，宁可用本地 ASR。"""
    pick = (next((s for s in subs if re.match(r'^zh[-_]?CN$', s.get('lan', ''), re.I)), None)
            or next((s for s in subs if re.match(r'^ai[-_]?zh$', s.get('lan', ''), re.I)), None)
            or next((s for s in subs if re.search(r'zh|cn', s.get('lan', ''), re.I)), None))
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
    cf = None
    if cookie:
        # Cookie 是账号凭据；使用唯一临时文件，并在 yt-dlp 返回后立刻清理。
        cf = out_dir / f'_cookies_{os.getpid()}_{time.time_ns()}.txt'
        cf.write_text('# Netscape HTTP Cookie File\n' + '\n'.join(
            f".bilibili.com\tTRUE\t/\tFALSE\t0\t{c.split('=')[0].strip()}\t{c.split('=', 1)[1].strip()}"
            for c in cookie.split(';') if '=' in c), encoding='utf-8')
        cmd += ['--cookies', str(cf)]
    cmd.append(cu)
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8', errors='replace')
    finally:
        if cf:
            try:
                cf.unlink(missing_ok=True)
            except Exception:
                pass
    if p.returncode != 0:
        raise RuntimeError('音频下载失败: ' + (p.stderr or p.stdout)[-800:])
    line = (p.stdout or '').strip().splitlines()[-1]
    info = json.loads(line)
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


def nano_language(lang):
    """bili_asr 的 --lang 默认 zh；Nano 侧用 auto 更安全（强制中文会让英文歌变错字）。"""
    return 'auto' if (not lang or lang == 'zh') else lang


def transcribe_nano(audio, lang='zh', hotwords_file=None, max_hotwords=80, timeout=7200):
    """经独立 adapter 调 Fun-ASR-Nano。返回 (segments, meta)。

    segments 结构与 faster-whisper 一致（start/end 秒 + text），供下游 merge/mark_ads 复用。
    adapter 由 asr_eval 解释器运行，避免 funasr 依赖污染 whisper venv。
    """
    if not PY_NANO.is_file():
        raise RuntimeError(f'Nano 引擎解释器不存在：{PY_NANO}')
    if not NANO_ADAPTER.is_file():
        raise RuntimeError(f'Nano adapter 缺失：{NANO_ADAPTER}')
    # adapter 的 stdout 会混入 funasr 进度条/日志，必须走文件读结果，不能解析 stdout
    out_json = Path(tempfile.gettempdir()) / f'_nano_out_{os.getpid()}.json'
    if out_json.exists():
        try:
            out_json.unlink()
        except Exception:
            pass
    cmd = [str(PY_NANO), str(NANO_ADAPTER), '--audio', str(audio), '--out', str(out_json),
           '--quiet',
           '--language', nano_language(lang)]
    if hotwords_file:
        cmd += ['--hotwords-file', str(hotwords_file), '--max-hotwords', str(max_hotwords)]
    env = dict(os.environ)
    env['PYTHONPATH'] = ''
    env.setdefault('HF_ENDPOINT', 'https://hf-mirror.com')
    p = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                       errors='replace', env=env, timeout=timeout)
    if not out_json.is_file():
        raise RuntimeError('Nano adapter 未产出结果文件: ' + (p.stderr or '')[-500:])
    try:
        j = json.loads(out_json.read_text(encoding='utf-8'))
    except Exception:
        raise RuntimeError('Nano adapter 结果非 JSON: ' + out_json.read_text(encoding='utf-8')[-300:])
    finally:
        try:
            out_json.unlink()
        except Exception:
            pass
    if not j.get('ok'):
        raise RuntimeError(j.get('error') or 'Nano 转写失败')
    # 有正文却解析不出时间戳 = 异常素材，不能进知识库（知识库依赖 [hh:mm:ss] 引用）
    if j.get('timestamp_status') == 'no_ts':
        raise RuntimeError('Nano 未返回可用时间戳（timestamp_status=no_ts），拒绝生成无时间戳素材包')
    segs = [{'start': float(s.get('start') or 0.0),
             'end': float(s.get('end') or 0.0),
             'text': (s.get('text') or '').strip()} for s in (j.get('segments') or [])]
    meta = {'language': lang or 'zh', 'duration': (segs[-1]['end'] if segs else 0.0),
            'device': 'cuda', 'engine': 'funasr-nano',
            'model': j.get('model'), 'load_sec': j.get('load_sec'),
            'asr_sec': j.get('asr_sec'), 'hotwords_count': j.get('hotwords_count'),
            'hotwords_sha256': j.get('hotwords_sha256'),
            'hotwords_source_sha256': j.get('hotwords_source_sha256'),
            'hotwords_truncated': j.get('hotwords_truncated'),
            'hotwords_total': j.get('hotwords_total'),
            'hotwords_status': j.get('hotwords_status')}
    return segs, meta


def run_nano_batch(batch_json, lang='zh', hotwords_file=None, timeout=21600):
    """一次加载 Nano 模型处理整批音频，返回 adapter JSON（含 items[].segments）。"""
    # 同 transcribe_nano：走文件读结果，避开 stdout 污染
    out_json = Path(tempfile.gettempdir()) / f'_nano_batch_out_{os.getpid()}.json'
    if out_json.exists():
        try:
            out_json.unlink()
        except Exception:
            pass
    cmd = [str(PY_NANO), str(NANO_ADAPTER), '--batch-json', str(batch_json),
           '--out', str(out_json), '--quiet',
           '--language', nano_language(lang)]
    if hotwords_file:
        cmd += ['--hotwords-file', str(hotwords_file)]
    env = dict(os.environ)
    env['PYTHONPATH'] = ''
    env.setdefault('HF_ENDPOINT', 'https://hf-mirror.com')
    p = subprocess.run(cmd, capture_output=True, text=True, encoding='utf-8',
                       errors='replace', env=env, timeout=timeout)
    if not out_json.is_file():
        raise RuntimeError('Nano adapter 未产出结果文件: ' + (p.stderr or '')[-500:])
    try:
        j = json.loads(out_json.read_text(encoding='utf-8'))
    except Exception:
        raise RuntimeError('Nano adapter 结果非 JSON: ' + out_json.read_text(encoding='utf-8')[-300:])
    finally:
        try:
            out_json.unlink()
        except Exception:
            pass
    if not j.get('ok'):
        raise RuntimeError(j.get('error') or 'Nano 批量转写失败')
    return j


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
    ap.add_argument('--engine', default='whisper', choices=['whisper', 'funasr-nano'],
                    help='本地 ASR 引擎：whisper(默认) / funasr-nano(中文专名更强，独立 venv)')
    ap.add_argument('--hotwords', default=None,
                    help='热词文件路径（仅 --engine funasr-nano 生效）；每行一词，去重后硬上限 80')
    ap.add_argument('--classify', action='store_true',
                    help='只输出引擎建议（whisper/funasr-nano）+ 理由 + 置信度；'
                         '配合 --only-meta 使用，走 view 缓存，零额外网络开销')
    ap.add_argument('--batch-file', default=None,
                    help='批量模式：JSON 文件 [{"url":..., "out":...}]，整批只加载一次模型')
    return ap


def process_video(a, url_arg, out_path, holder=None, preset=None):
    """处理单个视频，返回结构化结果（不打印）。holder 非空时复用其模型实例。

    preset: dict，批量预转写模式下传入 {'audio': 已下载音频路径, 'segments': 已转写段落,
                                        'engine_meta': {...}}
            传入时跳过下载与转写，直接组装素材包（避免 Nano 每条重复冷启动）。
    """
    t_meta = time.time()
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

    # 预扫描模式（--only-meta）只取 view 接口信息：fetch_meta 已含标题/简介/分P/统计。
    # 不再请求 player(wbi) 与评论接口——省掉 /nav 与 reply 两个往返，判断"值不值得细看"够用。
    ch, subs = [], []
    if not a.only_meta:
        ch, subs = fetch_player(bvid, cid)
        if ch:
            L += ['', '## 章节（UP主标记）'] + [f"- [{fmt_ts(c['from'])}] {c['content']}" for c in ch]
    meta_sec = round(time.time() - t_meta, 2)

    logged_in = 'SESSDATA=' in COOKIE_HEADER.upper()
    route = None
    asr_info = None
    if not a.only_meta:
        # 路由 1：B站字幕直取（秒级，需登录态且视频开放字幕）
        if not a.force_asr and subs:
            t_sub = time.time()
            sub_text, sub_label = fetch_subtitle_text(subs)
            if sub_text:
                route = f'subtitle:{sub_label}'
                L += ['', f'## 字幕全文（来源：{sub_label}）', '', sub_text]
                meta_sec += round(time.time() - t_sub, 2)

        # 路由 2：本地 ASR 转写
        if route is None:
            if not a.force_asr and not logged_in:
                L += ['', '## 字幕',
                      '> 未配置登录凭据，B站 CC/AI 字幕接口不可用，已直接走本地 ASR 转写。']
            elif not a.force_asr:
                L += ['', '## 字幕',
                      '> 该视频未开放字幕，已降级为本地 ASR 转写。']
            if preset and preset.get('segments') is not None:
                # 批量预转写：已下载并转写好的结果直接组装，不再下载/转写
                segs = preset['segments']
                meta = preset.get('engine_meta') or {}
                dl, load_sec = 0.0, meta.get('load_sec') or 0.0
                asr_sec = meta.get('asr_sec') or 0.0
                engine_label = f'本地 Fun-ASR-Nano（{meta.get("model")}）'
            else:
                t_dl = time.time()
                AUDIO_DIR.mkdir(parents=True, exist_ok=True)
                fp, info = download_audio(url, AUDIO_DIR, COOKIE_HEADER, page, stem=f'{bvid}_p{page}')
                dl = time.time() - t_dl
                t_asr = time.time()
                if a.engine == 'funasr-nano':
                    segs, meta = transcribe_nano(fp, a.lang, a.hotwords)
                    load_sec = meta.get('load_sec') or 0.0
                    engine_label = f'本地 Fun-ASR-Nano（{meta.get("model")}）'
                else:
                    t_load = time.time()
                    if holder is None:
                        holder = ModelHolder(a.model, a.device, a.compute, a.hf_mirror)
                    model, dev = holder.get()
                    load_sec = round(time.time() - t_load, 2)
                    segs, meta = transcribe_with(model, dev, fp, a.lang, a.prompt)
                    engine_label = f'本地 Whisper {a.model}'
                asr_sec = round(time.time() - t_asr, 2)
            asr_sec = round(asr_sec, 2)
            asr_info = {'audio_sec': round(meta['duration'], 1), 'download_sec': round(dl, 1),
                        'model_load_sec': load_sec, 'asr_sec': asr_sec, 'device': meta['device'],
                        'model': meta.get('model') or a.model, 'segments': len(segs),
                        'lang': meta['language'],
                        'engine': meta.get('engine', 'whisper'),
                        'engine_model': meta.get('model')}
            if a.engine == 'funasr-nano':
                asr_info['hotwords_count'] = meta.get('hotwords_count')
                asr_info['hotwords_sha256'] = meta.get('hotwords_sha256')
                asr_info['hotwords_source_sha256'] = meta.get('hotwords_source_sha256')
                asr_info['hotwords_truncated'] = meta.get('hotwords_truncated')
                asr_info['hotwords_total'] = meta.get('hotwords_total')
                asr_info['hotwords_status'] = meta.get('hotwords_status')
            blocks = merge_segments(segs)
            ad_kw = load_ad_keywords()
            ad_hits, ad_count = mark_ads(blocks, ad_kw)
            asr_info['ad_segments'] = ad_count
            route = f'asr:{a.engine}:{a.model if a.engine == "whisper" else meta.get("model")}@{meta["device"]}'
            L += ['', f"## 转写全文（{engine_label} · {meta['device']}）",
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

    if not a.only_meta and not a.no_comments:
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
    res = {'ok': True, 'bvid': bvid, 'title': V['title'], 'up': V['owner']['name'],
           'duration': fmt_dur(pinfo.get('duration') or V['duration']),
           'route': route, 'logged_in': logged_in,
           'out': str(out).replace('\\', '/'), 'chars': len(text),
           'asr': asr_info, 'timing': {'meta_sec': meta_sec}}
    if a.classify:
        # tag 是最准的信号；只在显式 --classify 时多调一次 tag 接口（很轻）
        res['engine_suggestion'] = classify_engine(V, tags=fetch_video_tags(bvid))
    return res


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

        # Nano 引擎：先下载全部音频，再一次性调 adapter（避免每条重复 50-70s 冷启动）
        if a.engine == 'funasr-nano':
            # outcomes 始终按输入顺序保存。不能把下载失败项先 append 到 results，
            # 再 append 成功项；上游队列按顺序 zip 回填，乱序会串台账。
            outcomes = []
            prepped = []
            for it in items:
                try:
                    url, page, _b = resolve_input(it['url'])
                    AUDIO_DIR.mkdir(parents=True, exist_ok=True)
                    fp, _info = download_audio(url, AUDIO_DIR, COOKIE_HEADER, page,
                                               stem=f'{_b}_p{page}')
                    rec = {'url': it['url'], 'out': it['out'], 'audio': fp, 'error': None}
                    prepped.append(rec)
                    outcomes.append(rec)
                except Exception as e:
                    outcomes.append({'url': it.get('url'), 'out': it.get('out'), 'audio': None,
                                     'error': str(e)})
            if prepped:
                batch_json = Path(tempfile.gettempdir()) / '_nano_batch.json'
                batch_json.write_text(json.dumps(
                    [{'key': p['out'], 'audio': p['audio']} for p in prepped],
                    ensure_ascii=False), encoding='utf-8')
                try:
                    j = run_nano_batch(batch_json, a.lang, a.hotwords)
                    by_out = {x.get('key'): x for x in (j.get('items') or [])}
                    first_nano_result = True
                    for rec in outcomes:
                        if rec['error']:
                            results.append({'ok': False, 'url': rec['url'], 'error': rec['error']})
                            continue
                        item_result = by_out.get(rec['out'])
                        segs = (item_result or {}).get('segments')
                        if not segs:
                            results.append({'ok': False, 'url': rec['url'],
                                            'error': 'Nano 未返回带时间戳的正文（timestamp_status=empty），'
                                                     '拒绝生成空素材包'})
                            continue
                        if (item_result or {}).get('timestamp_status') == 'no_ts':
                            results.append({'ok': False, 'url': rec['url'],
                                            'error': 'Nano 未返回可用时间戳（timestamp_status=no_ts），'
                                                     '拒绝生成无时间戳素材包'})
                            continue
                        # load 是批级开销，仅记在首条；asr 是本条实际 generate 用时。
                        emeta = {
                            'model': j.get('model'),
                            'load_sec': j.get('load_sec') if first_nano_result else 0.0,
                            'asr_sec': item_result.get('asr_sec'),
                            'engine': 'funasr-nano', 'device': 'cuda',
                            'language': a.lang,
                            'duration': item_result.get('audio_sec') or segs[-1].get('end', 0.0),
                            'hotwords_count': j.get('hotwords_count'),
                            'hotwords_sha256': j.get('hotwords_sha256'),
                            'hotwords_source_sha256': j.get('hotwords_source_sha256'),
                            'hotwords_truncated': j.get('hotwords_truncated'),
                            'hotwords_total': j.get('hotwords_total'),
                            'hotwords_status': j.get('hotwords_status'),
                        }
                        try:
                            results.append(process_video(
                                a, rec['url'], rec['out'], holder,
                                preset={'audio': rec['audio'], 'segments': segs,
                                        'engine_meta': emeta}))
                            first_nano_result = False
                        except Exception as e:
                            results.append({'ok': False, 'url': rec['url'], 'error': str(e)})
                except Exception as e:
                    for rec in outcomes:
                        results.append({'ok': False, 'url': rec['url'],
                                        'error': rec['error'] or str(e)})
                finally:
                    # preset 路径绕过 process_video 的默认清理，须在批量收口处理。
                    if not a.keep_audio:
                        for rec in prepped:
                            try:
                                Path(rec['audio']).unlink(missing_ok=True)
                            except Exception:
                                pass
                    try:
                        batch_json.unlink(missing_ok=True)
                    except Exception:
                        pass
            else:
                results = [{'ok': False, 'url': rec['url'], 'error': rec['error']}
                           for rec in outcomes]
            print(json.dumps({'ok': True, 'batch': True, 'count': len(results),
                              'engine': 'funasr-nano', 'results': results}, ensure_ascii=False))
            return

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
