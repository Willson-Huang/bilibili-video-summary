#!/usr/bin/env node
/**
 * bilibili 视频内容提取器
 * 用法: node bili.mjs <url|bvid|aid> [--out FILE] [--max-chars N] [--no-comments] [--cookie "SESSDATA=...; ..."]
 * 输出: Markdown 素材包（元信息 / 章节 / 简介 / 字幕全文 / 官方AI摘要 / 热评）
 */
import { readFileSync, writeFileSync, existsSync, mkdirSync } from 'node:fs';
import { homedir } from 'node:os';
import { join, dirname } from 'node:path';
// WBI 签名 / HTTP 公共能力抽到 bili_wbi.mjs，避免 B站接口变更时要在多处同步修改
import { UA, jget as jgetRaw, wbiUrl, uuidUpper } from './bili_wbi.mjs';

// 便携版：凭据统一放用户级缓存目录（与 bili_asr.py 的 BILI_COOKIE 默认值一致）
const CACHE_DIR = process.env.BILI_CACHE || join(homedir(), '.cache', 'bilibili-video-summary');
const COOKIE_FILE = process.env.BILI_COOKIE || join(CACHE_DIR, '.bilibili_cookie');

/* ---------- args ---------- */
const argv = process.argv.slice(2);
const getArg = (name, dflt) => { const i = argv.indexOf(name); return i >= 0 && argv[i+1] ? argv[i+1] : dflt; };
const has = (n) => argv.includes(n);
const input = argv.find(a => !a.startsWith('--') && !['-o'].includes(a)) || argv[0];
const OUT = getArg('--out', null);
const MAX = parseInt(getArg('--max-chars', '200000'), 10);
const NO_COMMENTS = has('--no-comments');
const CLI_COOKIE = getArg('--cookie', null);

if (!input) { console.error('用法: node bili.mjs <bilibili链接|BV号|av号> [--out FILE] [--max-chars N] [--no-comments] [--cookie "..."]'); process.exit(1); }

/* ---------- cookie ---------- */
let COOKIE = CLI_COOKIE;
if (!COOKIE && existsSync(COOKIE_FILE)) {
  COOKIE = readFileSync(COOKIE_FILE, 'utf8').trim();
}
if (COOKIE && !/buvid3=/i.test(COOKIE)) COOKIE += `; buvid3=${uuidUpper()}infoc`;
if (!COOKIE) COOKIE = `buvid3=${uuidUpper()}infoc`;

const H = { 'User-Agent': UA, 'Referer': 'https://www.bilibili.com/', 'Cookie': COOKIE };
const LOGGED_IN = /SESSDATA=/i.test(COOKIE);

// 统一带上本文件的请求头（含 cookie），签名逻辑见 bili_wbi.mjs
const jget = (url) => jgetRaw(url, H);

/* ---------- link parsing ---------- */
async function parseInput(raw) {
  let s = raw.trim();
  // 短链
  if (/b23\.tv/i.test(s)) {
    const m = s.match(/https?:\/\/b23\.tv\/[A-Za-z0-9]+/i);
    const target = m ? m[0] : (s.startsWith('http') ? s : 'https://' + s);
    const r = await fetch(target, { headers: { 'User-Agent': UA }, redirect: 'follow' });
    s = r.url || target;
  }
  let p = 1;
  const pm = s.match(/[?&]p=(\d+)/);
  if (pm) p = parseInt(pm[1], 10);
  const bv = s.match(/(BV[0-9A-Za-z]{10})/)?.[1];
  if (bv) return { bvid: bv, page: p };
  const av = s.match(/(?:av|aid)[=\/]?(\d{1,12})/i)?.[1];
  if (av) return { aid: av, page: p };
  throw new Error('无法解析链接: ' + raw);
}

/* ---------- helpers ---------- */
const fmtDur = (sec) => {
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), ss = sec % 60;
  return h ? `${h}:${String(m).padStart(2,'0')}:${String(ss).padStart(2,'0')}`
           : `${m}:${String(ss).padStart(2,'0')}`;
};
const ts = (sec) => {
  const h = Math.floor(sec / 3600), m = Math.floor(sec % 3600 / 60), ss = Math.floor(sec % 60);
  return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${String(ss).padStart(2,'0')}`;
};
const fmtDate = (u) => new Date(u * 1000).toISOString().replace('T', ' ').slice(0, 16) + ' (UTC)';

// AI 字幕滚动去重：后句是前句扩展 → 保留后句
function dedupLines(lines) {
  const out = [];
  for (const l of lines) {
    const prev = out[out.length - 1];
    if (prev && (l.content.startsWith(prev.content) || prev.content.startsWith(l.content))) {
      if (l.content.length > prev.content.length) out[out.length - 1] = l;
      continue;
    }
    out.push(l);
  }
  return out;
}

/* ---------- main ---------- */
(async () => {
  const { bvid: bvidIn, aid: aidIn, page } = await parseInput(input);

  const viewQ = bvidIn ? `?bvid=${bvidIn}` : `?aid=${aidIn}`;
  const view = await jget(`https://api.bilibili.com/x/web-interface/view${viewQ}`);
  if (view.code !== 0) throw new Error(`视频信息获取失败 [${view.code}] ${view.message}`);
  const V = view.data;
  const bvid = V.bvid;

  // 分P
  const pageInfo = V.pages?.find(x => x.page === page) || V.pages?.[0];
  const cid = pageInfo?.cid || V.cid;

  const L = [];
  L.push(`# B站视频素材包`);
  L.push('');
  L.push(`## 基本信息`);
  L.push(`- 标题：${V.title}`);
  L.push(`- BV号：${bvid} ｜ av号：${V.aid} ｜ cid：${cid} ｜ 分P：${page}/${V.videos}`);
  L.push(`- UP主：${V.owner.name}（mid:${V.owner.mid}）`);
  L.push(`- 发布：${fmtDate(V.pubdate)} ｜ 时长：${fmtDur(pageInfo?.duration || V.duration)}`);
  L.push(`- 分区：${V.tname_v2 || V.tname}`);
  const st = V.stat || {};
  L.push(`- 数据：播放 ${st.view} ｜ 点赞 ${st.like} ｜ 投币 ${st.coin} ｜ 收藏 ${st.favorite} ｜ 分享 ${st.share} ｜ 评论 ${st.reply} ｜ 弹幕 ${st.danmaku}`);
  if (V.desc_v2?.length) {
    const d = V.desc_v2.map(x => x.raw_text).join('\n').trim();
    if (d && d !== '-') { L.push(''); L.push('## 视频简介'); L.push(d); }
  } else if (V.desc && V.desc !== '-') { L.push(''); L.push('## 视频简介'); L.push(V.desc); }

  if (V.pages?.length > 1) {
    L.push(''); L.push('## 分P列表');
    V.pages.forEach(p => L.push(`- P${p.page} ${p.part}（${fmtDur(p.duration)}）${p.page === page ? '  ← 当前' : ''}`));
  }

  /* player/v2 —— 章节 + 字幕清单 */
  let player = null;
  try {
    player = await jget(await wbiUrl('https://api.bilibili.com/x/player/wbi/v2',
                                     { bvid, cid }, H));
  } catch (e) { /* ignore */ }

  const vp = player?.data?.view_points || [];
  if (vp.length) {
    L.push(''); L.push('## 章节（UP主标记）');
    vp.forEach(c => L.push(`- [${ts(c.from)}] ${c.content}`));
  }

  const subs = player?.data?.subtitle?.subtitles || [];
  const pick = subs.find(s => /^zh[-_]?CN$/i.test(s.lan))
            || subs.find(s => /^ai[-_]?zh$/i.test(s.lan))
            || subs.find(s => /zh|cn/i.test(s.lan))
            || subs[0];

  let transcript = '';
  let subLabel = '';
  if (pick) {
    try {
      const url = pick.subtitle_url.startsWith('//') ? 'https:' + pick.subtitle_url : pick.subtitle_url;
      const sj = await (await fetch(url, { headers: { 'User-Agent': UA } })).json();
      const body = dedupLines(sj.body || []);
      subLabel = `${pick.lan_doc || pick.lan}${/^ai/i.test(pick.lan) ? '（自动生成，可能有错字）' : ''}`;
      transcript = body.map(b => `[${ts(b.from)}] ${b.content}`).join('\n');
    } catch (e) { transcript = ''; }
  }

  if (transcript) {
    L.push(''); L.push(`## 字幕全文（来源：${subLabel}）`);
    L.push(transcript);
  } else {
    L.push(''); L.push('## 字幕');
    L.push(LOGGED_IN
      ? '> 未获取到字幕：该视频无 CC 字幕，且未开放 AI 字幕。以下总结仅基于元信息/简介/评论，深度有限。'
      : '> 未获取到字幕。当前**未配置 B站登录凭据**，CC 字幕与 AI 字幕接口均需登录态。请配置 Cookie 后重试。');
  }

  /* 官方 AI 摘要 */
  if (LOGGED_IN) {
    try {
      const c = await jget(await wbiUrl('https://api.bilibili.com/x/web-interface/view/conclusion/get',
        { bvid, cid, up_mid: V.owner.mid }, H));
      const mr = c?.data?.model_result;
      if (mr) {
        L.push(''); L.push('## B站官方 AI 摘要（仅供参考，需自行校验）');
        if (mr.summary) L.push(mr.summary);
        (mr.outline || []).forEach(o => {
          if (o.title) L.push(`- ${o.title}（${ts(o.from)}）`);
          (o.part_outline || []).forEach(x => x.content && L.push(`  - ${x.content}`));
        });
      }
    } catch (e) { /* ignore */ }
  }

  /* 热评 */
  if (!NO_COMMENTS) {
    try {
      const c = await jget(`https://api.bilibili.com/x/v2/reply?type=1&oid=${V.aid}&sort=2&ps=20&pn=1&nohot=0`);
      const rs = c?.data?.replies || [];
      if (rs.length) {
        L.push(''); L.push('## 热门评论 Top20');
        rs.forEach((r, i) => {
          const t = (r.content?.message || '').replace(/\n+/g, ' ').slice(0, 200);
          L.push(`${i + 1}. @${r.member?.uname || '?'}（${r.like}赞）：${t}`);
        });
      }
    } catch (e) { /* ignore */ }
  }

  let text = L.join('\n');
  if (text.length > MAX) text = text.slice(0, MAX) + `\n\n...[已截断，原长 ${L.join('\n').length} 字符]`;

  const outPath = OUT || join('outputs', `bili_${bvid}${page > 1 ? '_p' + page : ''}.md`);
  if (dirname(outPath) !== '.') mkdirSync(dirname(outPath), { recursive: true });
  writeFileSync(outPath, text, 'utf8');

  console.log(JSON.stringify({
    ok: true, bvid, aid: V.aid, cid, page, title: V.title, up: V.owner.name,
    duration: pageInfo?.duration || V.duration,
    logged_in: LOGGED_IN,
    subtitle: pick ? (pick.lan_doc || pick.lan) : null,
    subtitle_chars: transcript.length,
    chapters: vp.length,
    out: outPath, chars: text.length
  }, null, 2));
})().catch(e => { console.error(JSON.stringify({ ok: false, error: String(e.message || e) }, null, 2)); process.exit(1); });
