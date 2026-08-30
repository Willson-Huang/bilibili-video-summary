#!/usr/bin/env node
/**
 * 保存 B站登录 Cookie（解锁 AI 字幕 / 官方 AI 摘要）
 * 用法:
 *   node set-cookie.mjs --cookie "SESSDATA=xxx; bili_jct=xxx; ..."
 *   node set-cookie.mjs --sessdata "xxx" [--bili-jct "xxx"]
 *   node set-cookie.mjs --check      # 检查当前凭据是否有效
 *   node set-cookie.mjs --clear      # 清除
 */
import { readFileSync, writeFileSync, existsSync, unlinkSync, chmodSync } from 'node:fs';
import { homedir } from 'node:os';
import { join } from 'node:path';

const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';
// 便携版：凭据改存用户级缓存目录（与 bili_asr.py 的 BILI_COOKIE 一致）
const CACHE_DIR = process.env.BILI_CACHE
  ? join(process.env.BILI_CACHE)
  : join(homedir(), '.cache', 'bilibili-video-summary');
const FILE = process.env.BILI_COOKIE
  ? join(process.env.BILI_COOKIE)
  : join(CACHE_DIR, '.bilibili_cookie');
const argv = process.argv.slice(2);
const get = (n) => { const i = argv.indexOf(n); return i >= 0 ? argv[i + 1] : null; };

if (argv.includes('--clear')) {
  if (existsSync(FILE)) unlinkSync(FILE);
  console.log(JSON.stringify({ ok: true, action: 'cleared' }));
  process.exit(0);
}

let cookie = get('--cookie');
const sd = get('--sessdata');
if (!cookie && sd) {
  cookie = `SESSDATA=${sd}` + (get('--bili-jct') ? `; bili_jct=${get('--bili-jct')}` : '');
}
if (!cookie && !argv.includes('--check')) {
  console.log(JSON.stringify({ ok: false, error: '缺少 --cookie 或 --sessdata' }));
  process.exit(1);
}

if (cookie) {
  cookie = cookie.replace(/^["']|["']$/g, '').replace(/\s*document\.cookie\s*/i, '').trim();
  if (!/SESSDATA=/i.test(cookie)) {
    console.log(JSON.stringify({ ok: false, error: 'Cookie 中未找到 SESSDATA，请确认复制完整' }));
    process.exit(1);
  }
  writeFileSync(FILE, cookie, 'utf8');
  try { chmodSync(FILE, 0o600); } catch {}
}

const current = existsSync(FILE) ? readFileSync(FILE, 'utf8').trim() : '';
if (!current) {
  console.log(JSON.stringify({ ok: false, error: '未配置凭据' }));
  process.exit(1);
}

const H = { 'User-Agent': UA, 'Referer': 'https://www.bilibili.com/', 'Cookie': current + ( /buvid3=/i.test(current) ? '' : `; buvid3=${crypto.randomUUID().toUpperCase()}infoc` ) };
const j = await (await fetch('https://api.bilibili.com/x/web-interface/nav', { headers: H })).json();
const ok = j.code === 0 && j.data?.isLogin === true;
console.log(JSON.stringify({
  ok: true, saved: !!cookie, file: FILE,
  logged_in: ok,
  user: ok ? j.data.uname : null,
  message: ok ? '凭据有效' : '凭据无效或已过期（SESSDATA 有效期约 1 个月，需重新获取）'
}, null, 2));
