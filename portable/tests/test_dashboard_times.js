/* 看板时间显示回归测试（可选，需要 node）
 *
 * 为什么单独有这么一个 JS 测试：看板的时间显示踩过两次坑，都是浏览器里静默失效
 * —— 一次 `tick()` 漏写基准赋值、一次 `ago()` 没定义，表现都是数字冻结在旧值上，
 * 页面上看不出任何报错。Python 单测看不到 dashboard.html 里的 JS，所以这里用桩
 * 替换 DOM、用可控时钟驱动，把时间逻辑钉住。
 *
 * 跑法：node tests/test_dashboard_times.js
 */
'use strict';
const fs = require('fs');
const path = require('path');

const P = path.join(__dirname, '..', 'scripts', 'dashboard.html');
const src = fs.readFileSync(P, 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];

let NOW = 1750000000000;
Date.now = () => NOW;                       /* 可控“现在”，不依赖真实等待 */

const store = {};
function el(id) {
  if (!store[id]) store[id] = {
    id, textContent: '', className: '', title: '', style: {},
    classList: {add() {}, remove() {}, toggle() {}},
    setAttribute() {}, getAttribute() { return null; },
    innerHTML: '', children: [], querySelector() { return null; }, querySelectorAll() { return []; },
    appendChild() {}, insertBefore() {}, remove() {},
  };
  return store[id];
}
const doc = {hidden: false, getElementById: el, createElement: () => el('n' + Math.random()),
             addEventListener() {}, querySelector: () => null};
const noop = () => 0;
/* 求值整段脚本：任何未定义符号（如漏掉的 helper）都会在这里暴露 */
const fn = new Function('document', 'fetch', 'setInterval', 'setTimeout', 'clearTimeout',
                        'addEventListener', 'console',
  src + '\n;return {paintTopTimes,paintTaskTimes,nodes,getBase:()=>base};');
const api = fn(doc, () => Promise.reject(new Error('stub')), noop, noop, noop, noop, console);

let pass = 0, fail = 0;
function check(name, cond, extra) {
  if (cond) { pass++; console.log('  ok   ' + name); }
  else { fail++; console.log('  FAIL ' + name + (extra ? '  -> ' + extra : '')); }
}

const B = api.getBase();
Object.assign(B, {
  at: NOW,
  o: {total: 40, done: 2, active: 0, failed: 1, eta: 100, elapsed: 354.7, updated: 0},
  run: {engine: 'funasr-nano'},
});
api.nodes.set('done1', {el: el('r1'), sub: el('s1'), t: {stage: 'done', elapsed: 238}});
api.nodes.set('asr1', {el: el('r2'), sub: el('s2'), t: {stage: 'asr', elapsed: 100, eta: 200}});
api.nodes.set('fail1', {el: el('r3'), sub: el('s3'), t: {stage: 'fail', elapsed: 90}});

console.log('-- 刚收到数据（age≈0）--');
api.paintTopTimes(); api.paintTaskTimes();
check('最后更新 = 刚刚', el('sub').textContent.includes('最后更新 刚刚'), el('sub').textContent);
check('无过期着色', el('sub').className === 'sub', el('sub').className);
check('总耗时取服务端基准', el('sElapsed').textContent === '5m55s', el('sElapsed').textContent);
check('剩余时间', el('sEta').textContent === '1m40s', el('sEta').textContent);
check('done 任务冻结真实耗时', el('s1').textContent === '3m58s', el('s1').textContent);
check('fail 任务显示已中断', el('s3').textContent === '已中断', el('s3').textContent);
check('active 任务 已用/剩余', el('s2').textContent === '1m40s / 剩 3m20s', el('s2').textContent);

console.log('-- 数据龄 12s：本地时钟推进，且外推被 STALE_CAP=5 截住 --');
NOW += 12000; api.paintTopTimes(); api.paintTaskTimes();
check('显示相对时间', el('sub').textContent.includes('12 秒前'), el('sub').textContent);
check('进入 warn 着色', el('sub').className === 'sub stale-warn', el('sub').className);
check('总耗时 = 基准 + 上限 5', el('sElapsed').textContent === '6m00s', el('sElapsed').textContent);
check('active 已用 100+5', el('s2').textContent === '1m45s / 剩 3m15s', el('s2').textContent);

console.log('-- 数据龄 39s --');
NOW += 27000; api.paintTopTimes(); api.paintTaskTimes();
check('显示相对时间', el('sub').textContent.includes('39 秒前'), el('sub').textContent);
check('进入 bad 着色', el('sub').className === 'sub stale-bad', el('sub').className);
check('done 仍冻结', el('s1').textContent === '3m58s', el('s1').textContent);

console.log('-- 页面切到后台（轮询降频不代表异常，不应着色）--');
doc.hidden = true; api.paintTopTimes();
check('后台不着色', el('sub').className === 'sub', el('sub').className);
doc.hidden = false;

console.log('-- 陈旧数据被新数据替换后立刻复位 --');
NOW += 1000; B.at = NOW; api.paintTopTimes();
check('回到 刚刚', el('sub').textContent.includes('最后更新 刚刚'), el('sub').textContent);
check('着色复位', el('sub').className === 'sub', el('sub').className);

console.log(`\n结果：${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
