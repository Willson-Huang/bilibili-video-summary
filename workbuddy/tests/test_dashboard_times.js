/* 看板时间显示回归测试（需要 node）
 *
 * 为什么单独有这么一个 JS 测试：看板的时间显示踩过三次坑，都是浏览器里静默失效
 * —— `tick()` 漏写基准赋值、`ago()` 没定义、`renderTasks` 忘了给节点挂 `t`
 * （任务行的本地时钟推进整段失效）。表现都是数字冻结在旧值，页面上看不出报错。
 * Python 单测看不到 dashboard.html 里的 JS，所以这里用桩替换 DOM、用可控时钟驱动。
 *
 * 关键：桩必须让 **renderTasks / makeRow 真正跑完**。
 * 旧版的桩把 querySelector 写成恒返回 null，等于跳过整条渲染路径，
 * 于是「节点没挂 t」这类断线问题测不出来。下面用它自带的可写子元素代替。
 *
 * 跑法：node tests/test_dashboard_times.js
 *      node tests/test_dashboard_times.js <另一个 dashboard.html 路径>   # 用于对照验证
 */
'use strict';
const fs = require('fs');
const path = require('path');

const P = process.argv[2] || path.join(__dirname, '..', 'scripts', 'dashboard.html');
const src = fs.readFileSync(P, 'utf8').match(/<script>([\s\S]*?)<\/script>/)[1];

let NOW = 1750000000000;
Date.now = () => NOW;                       /* 可控“现在”，不依赖真实等待 */

/* ---------- 可写 DOM 桩 ---------- */
function mkEl(tag) {
  const kids = {};
  const e = {
    tag, textContent: '', className: '', title: '', innerHTML: '',
    style: {}, children: [], parent: null, _attrs: {}, hidden: false,
    /* contains 必须给：已完成分区折叠时脚本要读它判断当前状态 */
    classList: {add() {}, remove() {}, toggle() {}, contains() { return false; }},
    addEventListener() {},
    setAttribute(k, v) { this._attrs[k] = v; },
    getAttribute(k) { return k in this._attrs ? this._attrs[k] : null; },
    querySelector(sel) { return kids[sel] = kids[sel] || mkEl('q'); },
    querySelectorAll() { return []; },
    appendChild(c) { this.children.push(c); c.parent = this; return c; },
    insertBefore(c, at) {
      const i = at ? this.children.indexOf(at) : -1;
      if (i < 0) this.children.push(c); else this.children.splice(i, 0, c);
      c.parent = this; return c;
    },
    remove() {
      const p = this.parent;
      if (p) { const i = p.children.indexOf(this); if (i >= 0) p.children.splice(i, 1); }
    },
  };
  return e;
}
const byId = {};
const doc = {
  hidden: false,
  getElementById: id => (byId[id] = byId[id] || mkEl('#' + id)),
  createElement: t => mkEl(t),
  addEventListener() {}, querySelector: () => null,
};
const noop = () => 0;
/* 求值整段脚本：任何未定义符号（如漏掉的 helper）都会在这里暴露 */
const fn = new Function('document', 'fetch', 'setInterval', 'setTimeout', 'clearTimeout',
                        'addEventListener', 'console',
  src + '\n;return {renderTasks,renderGroups,renderLog,paintTopTimes,paintTaskTimes,'
      + 'taskTimeText,topEtaText,nodes,fmt,ago,drift,dataAge,UI_TICK,cfg,place,'
      + 'getBase:()=>base};');
const api = fn(doc, () => Promise.reject(new Error('stub')), noop, noop, noop, noop, console);

let pass = 0, fail = 0;
function check(name, cond, extra) {
  if (cond) { pass++; console.log('  ok   ' + name); }
  else { fail++; console.log('  FAIL ' + name + (extra !== undefined ? '  -> ' + extra : '')); }
}

/* ---------- 夹具 ---------- */
const B = api.getBase();
const TASKS = [
  {id: 'BV1', title: '甲', stage: 'done', stage_label: '已完成',
   pct: 100, elapsed: 238, eta: 0, overrun: false},
  {id: 'BV2', title: '乙', stage: 'asr', stage_label: '转写中',
   pct: 50, elapsed: 100, eta: 200, overrun: false, duration_sec: 1300},
  {id: 'BV3', title: '丙', stage: 'fail', stage_label: '失败',
   pct: 40, elapsed: 90, eta: null, overrun: false},
];
const OVERALL = () => ({
  total: 40, done: 2, failed: 1, active: 1, eta: 100, elapsed: 354.7,
  updated: 0, stale_sec: 0, eta_complete: true, overrun: false, rate: null,
});
function fresh() {
  B.at = NOW; B.o = OVERALL(); B.run = {engine: 'funasr-nano'};
}

console.log('-- renderTasks 与时间显示的接线 --');
fresh();
api.renderTasks(TASKS);
const n2 = api.nodes.get('BV2');
/* 这一条是本次新增的关键断言：旧实现没有 n.t，任务行的本地时钟推进是无效代码 */
check('renderTasks 后节点挂上了 t', !!(n2 && 't' in n2), n2 && Object.keys(n2).join(','));
/* 进行中与已完成拆成两个分区：完成项不再把正在跑的挤到下面 */
check('进行中分区只有 2 行', byId.list.children.length === 2, byId.list.children.length);
check('已完成分区只有 1 行', byId.listDone.children.length === 1,
      byId.listDone.children.length);
check('完成项落在已完成分区', api.nodes.get('BV1').el.parent === byId.listDone,
      api.nodes.get('BV1').el.parent === byId.list);
check('失败项留在进行中分区', api.nodes.get('BV3').el.parent === byId.list,
      api.nodes.get('BV3').el.parent === byId.listDone);
const clsOf = id => String(api.nodes.get(id).el.className).split(' ');
check('完成项带 .sm（默认收起为摘要行）', clsOf('BV1').includes('sm'), clsOf('BV1').join(' '));
check('进行中项不带 .sm', !clsOf('BV2').includes('sm'), clsOf('BV2').join(' '));
check('分区计数与行数一致',
      byId.cLive.textContent === '2' && byId.cDone.textContent === '1',
      byId.cLive.textContent + ' / ' + byId.cDone.textContent);
check('两侧都有内容时显示分区标题', byId.hLive.hidden === false, byId.hLive.hidden);
check('已完成分区显示', byId.zoneDone.hidden === false, byId.zoneDone.hidden);
/* 完成项跨轮次不重复插入（place 只在位置不对时才搬） */
api.renderTasks(TASKS);
check('重复渲染不产生重复节点', byId.listDone.children.length === 1,
      byId.listDone.children.length);
/* 任务从进行中变为完成时要跨分区搬动，且不留残影 */
const moved = TASKS.map(t => t.id === 'BV2' ? Object.assign({}, t, {stage: 'done'}) : t);
api.renderTasks(moved);
check('状态翻转后搬到已完成分区', api.nodes.get('BV2').el.parent === byId.listDone,
      api.nodes.get('BV2').el.parent === byId.list);
check('搬走后进行中分区只剩 1 行', byId.list.children.length === 1,
      byId.list.children.length);
check('搬走后已完成分区 2 行', byId.listDone.children.length === 2,
      byId.listDone.children.length);
check('翻转项重新带上 .sm', clsOf('BV2').includes('sm'), clsOf('BV2').join(' '));
api.renderTasks(TASKS);
check('翻转回来后从 .sm 摘掉', !clsOf('BV2').includes('sm'), clsOf('BV2').join(' '));
/* place 直接对 box.children 调 indexOf 会让整个列表渲染抛错、页面空白，
   而桩里的 children 是普通数组（有 indexOf），所以只靠渲染路径测不出来。
   这里用「有 length 和数字下标、没有 indexOf」的类 HTMLCollection 兜住这个缺口。 */
const noProtoColl = Object.assign(Object.create(null), {0: {}, length: 1});
check('place 不依赖 HTMLCollection 的 indexOf',
      (() => {
        try { api.place({children: noProtoColl, insertBefore() {}}, {remove() {}}, null); return 'ok'; }
        catch (e) { return e.message; }
      })() === 'ok');

console.log('-- 刚收到数据（数据龄 0）--');
api.paintTopTimes(); api.paintTaskTimes();
check('最后更新 = 刚刚', byId.sub.textContent.includes('最后更新 刚刚'), byId.sub.textContent);
check('无过期着色', byId.sub.className === 'sub', byId.sub.className);
check('总耗时取服务端基准', byId.sElapsed.textContent === '5m55s', byId.sElapsed.textContent);
check('剩余时间', byId.sEta.textContent === '1m40s', byId.sEta.textContent);
check('done 任务冻结真实耗时', n2 && api.nodes.get('BV1').sub.textContent === '3m58s',
      api.nodes.get('BV1').sub.textContent);
check('fail 任务显示已中断', api.nodes.get('BV3').sub.textContent === '已中断',
      api.nodes.get('BV3').sub.textContent);
check('active 任务 已用/剩余', n2.sub.textContent === '1m40s / 剩 3m20s', n2.sub.textContent);

console.log('-- 数据龄 12s：本地时钟推进，且外推被 STALE_CAP=5 截住 --');
NOW += 12000; api.paintTopTimes(); api.paintTaskTimes();
check('显示相对时间', byId.sub.textContent.includes('12 秒前'), byId.sub.textContent);
check('进入 warn 着色', byId.sub.className === 'sub stale-warn', byId.sub.className);
check('总耗时取服务端基准，不随本地时钟推进（它是累加值，不是秒表）',
      byId.sElapsed.textContent === '5m55s', byId.sElapsed.textContent);
check('active 已用 100+5', n2.sub.textContent === '1m45s / 剩 3m15s', n2.sub.textContent);

console.log('-- 数据龄 39s --');
NOW += 27000; api.paintTopTimes(); api.paintTaskTimes();
check('显示相对时间', byId.sub.textContent.includes('39 秒前'), byId.sub.textContent);
check('进入 bad 着色', byId.sub.className === 'sub stale-bad', byId.sub.className);
check('done 仍冻结', api.nodes.get('BV1').sub.textContent === '3m58s',
      api.nodes.get('BV1').sub.textContent);

console.log('-- 页面切到后台（轮询降频不代表异常，不应着色）--');
doc.hidden = true; api.paintTopTimes();
check('后台不着色', byId.sub.className === 'sub', byId.sub.className);
doc.hidden = false;

console.log('-- 陈旧数据被新数据替换后立刻复位 --');
NOW += 1000; fresh(); api.paintTopTimes();
check('回到 刚刚', byId.sub.textContent.includes('最后更新 刚刚'), byId.sub.textContent);
check('着色复位', byId.sub.className === 'sub', byId.sub.className);

console.log('-- 顶部副标题要报出「按什么倍率算的」--');
fresh(); B.o = Object.assign(OVERALL(), {rate: 2.4, rate_src: 'observed'});
api.paintTopTimes();
check('实测倍率标注', byId.sub.textContent.includes('倍率 2.4x 实测'), byId.sub.textContent);
fresh(); B.o = Object.assign(OVERALL(), {rate: 2.1, rate_src: 'batch'});
api.paintTopTimes();
check('批量兜底标注', byId.sub.textContent.includes('倍率 2.1x 批量参考'), byId.sub.textContent);
fresh(); B.o = Object.assign(OVERALL(), {rate: 5.08, rate_src: 'constant'});
api.paintTopTimes();
check('单条常量标注', byId.sub.textContent.includes('倍率 5.1x 单条'), byId.sub.textContent);

console.log('-- 数据龄必须含服务端给出的 stale_sec（只看本地收包时刻是错的）--');
fresh(); NOW += 1000; B.at = NOW;            /* 刚刚收到响应，本地龄≈0 */
B.o = Object.assign(OVERALL(), {stale_sec: 40});
api.paintTopTimes(); api.paintTaskTimes();
check('刚收到数据也报 40 秒前', byId.sub.textContent.includes('40 秒前'), byId.sub.textContent);
check('停更 40 秒转红', byId.sub.className === 'sub stale-bad', byId.sub.className);
check('停更后不再外推（drift=0）', api.drift() === 0, api.drift());
check('停更后已耗时不再增长', byId.sElapsed.textContent === '5m55s', byId.sElapsed.textContent);

console.log('-- 超预估与「没有时间依据」的三种表达 --');
check('行：超预估显示已超预估',
      api.taskTimeText({stage: 'asr', elapsed: 300, eta: 0, overrun: true}, 0) === '5m00s / 已超预估',
      api.taskTimeText({stage: 'asr', elapsed: 300, eta: 0, overrun: true}, 0));
check('行：无 eta 只报已用，不出现多余分隔符',
      api.taskTimeText({stage: 'transcribed', elapsed: 500, eta: null}, 0) === '8m20s',
      api.taskTimeText({stage: 'transcribed', elapsed: 500, eta: null}, 0));
check('行：排队且无已用 → 只报剩余',
      api.taskTimeText({stage: 'queued', elapsed: null, eta: 200}, 0) === '剩 3m20s',
      api.taskTimeText({stage: 'queued', elapsed: null, eta: 200}, 0));
check('行：两者都没有 → 破折号',
      api.taskTimeText({stage: 'queued', elapsed: null, eta: null}, 0) === '—',
      api.taskTimeText({stage: 'queued', elapsed: null, eta: null}, 0));
fresh(); B.o = Object.assign(OVERALL(), {eta: 0, overrun: true, eta_complete: true});
api.paintTopTimes();
check('顶部：已超预估', byId.sEta.textContent === '已超预估', byId.sEta.textContent);
fresh(); B.o = Object.assign(OVERALL(), {eta: 0, overrun: false, eta_complete: false});
api.paintTopTimes();
check('顶部：有依据缺失且无剩余 → 破折号（不能显示 0s）',
      byId.sEta.textContent === '—', byId.sEta.textContent);
fresh(); B.o = Object.assign(OVERALL(), {eta: 200, overrun: false, eta_complete: false});
api.paintTopTimes();
check('顶部：部分任务无依据 → 标注下界', byId.sEta.textContent === '≥ 3m20s', byId.sEta.textContent);
fresh(); B.o = Object.assign(OVERALL(), {eta: 0, done: 40, failed: 0, eta_complete: false});
api.paintTopTimes();
check('顶部：全部结束 → 0s', byId.sEta.textContent === '0s', byId.sEta.textContent);

console.log('-- fmt 的时间格式（跨小时不能丢秒）--');
check('59m59s', api.fmt(3599) === '59m59s', api.fmt(3599));
check('1h00m00s', api.fmt(3600) === '1h00m00s', api.fmt(3600));
check('1h01m01s', api.fmt(3661) === '1h01m01s', api.fmt(3661));
check('null → 破折号', api.fmt(null) === '—', api.fmt(null));
/* 跨过一天要拆出「天」：415h06m41s 这种读法看不出量级 */
check('刚好一天', api.fmt(86400) === '1天0h00m', api.fmt(86400));
check('17 天 7 小时 16 分', api.fmt(1494980) === '17天7h16m', api.fmt(1494980));
check('一天差一秒仍走小时制', api.fmt(86399) === '23h59m59s', api.fmt(86399));

console.log('-- 事件流与任务组能正常渲染（顺带覆盖 renderLog/renderGroups）--');
api.renderGroups([{name: '转写', total: 3, done: 1, failed: 0, pct: 33.3}]);
api.renderLog([{ts: NOW / 1000, task: 'BV1', stage_label: '转写中', note: '转写中 63%'}]);
check('任务组 chip 渲染', byId.groups.children.length === 1, byId.groups.children.length);
check('事件流渲染', String(byId.log.innerHTML).includes('转写中 63%'));

console.log('-- 过期阈值由服务端下发，前端只留兜底 --');
check('刷新节流改成 500ms', api.UI_TICK === 500, api.UI_TICK);
fresh(); NOW += 12000; api.paintTopTimes();          /* 数据龄 12s */
check('兜底阈值：12s 转黄', byId.sub.className === 'sub stale-warn', byId.sub.className);
fresh(); B.o = Object.assign(OVERALL(), {stale_warn: 5, stale_bad: 8});
NOW += 12000; api.paintTopTimes();
check('服务端阈值优先：bad=8 时 12s 就转红',
      byId.sub.className === 'sub stale-bad', byId.sub.className);
fresh(); NOW += 12000; B.o = Object.assign(OVERALL(), {stale_cap: 2});
check('外推额度也用服务端的 stale_cap', api.drift() === 2, api.drift());

console.log('-- 实测速率与跨批次历史（历史只显示，不参与计算）--');
check('行：有速率时显示倍数',
      api.taskTimeText({stage: 'asr', elapsed: 100, eta: 200, rate: 2.43}, 0)
        === '1m40s / 剩 3m20s / 2.4x',
      api.taskTimeText({stage: 'asr', elapsed: 100, eta: 200, rate: 2.43}, 0));
check('行：拿不到速率时不显示',
      api.taskTimeText({stage: 'notes', elapsed: 100, eta: null}, 0) === '1m40s',
      api.taskTimeText({stage: 'notes', elapsed: 100, eta: null}, 0));
fresh(); B.o = Object.assign(OVERALL(),
  {rate: 2.1, rate_src: 'observed', rate_history: {n: 5, median: 2.03}});
api.paintTopTimes();
check('顶部显示实测倍率', byId.sub.textContent.includes('倍率 2.1x 实测'), byId.sub.textContent);
check('顶部并排显示历史中位与样本数',
      byId.sub.textContent.includes('历史 2.0x / 5 条'), byId.sub.textContent);
fresh(); B.o = Object.assign(OVERALL(), {rate: 2.1, rate_src: 'batch'});
api.paintTopTimes();
check('没有历史时不显示历史段', !byId.sub.textContent.includes('历史'), byId.sub.textContent);

console.log(`\n结果：${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
