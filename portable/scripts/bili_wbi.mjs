/**
 * B站 WBI 签名与 HTTP 公共模块（ESM）
 *
 * 被 bili.mjs 复用——WBI 签名算法与 MIXIN 混洗表是 B站固定套路，
 * 抽出来后接口变更只需改这一处。
 *
 * 说明：用 node:crypto 的 randomUUID 而不是全局 crypto，
 * 这样 Node 14.17+ 即可运行（全局 crypto 要 Node 19+）。
 */
import { createHash, randomUUID } from 'node:crypto';

export const UA = 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36';

export const NAV_URL = 'https://api.bilibili.com/x/web-interface/nav';

/** WBI 签名用的混洗表（B站固定常量，接口变更时需同步更新） */
export const MIXIN = [46, 47, 18, 2, 53, 8, 23, 32, 15, 50, 10, 31, 58, 3, 45, 35, 27, 43, 5, 49,
  33, 9, 42, 19, 29, 28, 14, 39, 12, 38, 41, 13, 37, 48, 7, 16, 24, 55, 40, 61,
  26, 17, 0, 1, 60, 51, 30, 4, 22, 25, 54, 21, 56, 59, 6, 63, 57, 62, 11, 36,
  20, 34, 44, 52];

export const md5 = (s) => createHash('md5').update(s).digest('hex');

/** 生成一个大写 UUID，用于构造 buvid3 匿名标识 */
export const uuidUpper = () => randomUUID().toUpperCase();

/** GET 一个 URL 并解析 JSON。headers 必传（通常含 UA/Referer/Cookie）。 */
export async function jget(url, headers) {
  const r = await fetch(url, { headers, redirect: 'follow' });
  return r.json();
}

let wbiCache = null;

export async function wbiKeys(headers) {
  if (wbiCache) return wbiCache;
  const j = await jget(NAV_URL, headers);
  const img = j?.data?.wbi_img;
  if (!img) throw new Error('获取 wbi key 失败（接口返回异常）: ' + (j?.message ?? JSON.stringify(j)?.slice(0, 120)));
  const k = (u) => u.split('/').pop().split('.')[0];
  return (wbiCache = [k(img.img_url), k(img.sub_url)]);
}

export async function wbiSign(params, headers) {
  const [a, b] = await wbiKeys(headers);
  const p = { ...params, wts: Math.floor(Date.now() / 1000) };
  const q = Object.keys(p).sort()
    .map(k => `${encodeURIComponent(k)}=${encodeURIComponent(String(p[k]).replace(/[!'()*]/g, ''))}`)
    .join('&');
  const raw = a + b;
  const mk = MIXIN.map(i => raw[i]).join('').slice(0, 32);
  return { ...p, w_rid: md5(q + mk) };
}

export async function wbiUrl(base, params, headers) {
  return `${base}?${new URLSearchParams(await wbiSign(params, headers)).toString()}`;
}
