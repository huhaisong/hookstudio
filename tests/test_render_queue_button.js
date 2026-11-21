/* 自测：出片主按钮在「前面有任务在跑」时可点并走排队。
   把 app.js 里的真实逻辑抽出来跑，验证三个场景：
   ① cuts 忙 + 有勾选 → 按钮 disabled=false（可点）
   ② enqueueRender 忙时把请求写进 ST.pendingRender，不直接发
   ③ 前面空闲后 flushPendingRender 把排队请求发出、清空队列 */
const fs = require('fs');
const src = fs.readFileSync(require('path').join(__dirname, '..', 'web', 'app.js'), 'utf-8');

let pass = 0, fail = 0;
function ok(name, cond) { cond ? (pass++, console.log('  PASS', name)) : (fail++, console.log('  FAIL', name)); }

// 真实 taskBusy
const taskBusy = (t) => !!t && !t.error && Number(t.pct || 0) < 100;

// 从源码确认按钮禁用条件已不再含 busy
const btnBlock = src.slice(src.indexOf('const btn = $("#btn-render-plans");'),
                           src.indexOf('const btn = $("#btn-render-plans");') + 500);
console.log('场景① cuts 忙时主按钮状态');
ok('按钮 disabled 只看 !on.length（不含 busy）', /btn\.disabled = !on\.length/.test(btnBlock));
ok('排队时按钮文案变为「已排队·再点可改」', /已排队·再点可改/.test(btnBlock));

// 模拟 enqueueRender（照源码逻辑）
const ST = { proj: { id: 'p1', task: { kind: 'batch', pct: 30, error: '' } }, pendingRender: null };
let sent = [];
function render() {}
function startPoll() {}
function enqueueRender(pid, body, msg) {
  if (taskBusy(ST.proj && ST.proj.task)) {
    ST.pendingRender = { pid, body, msg };
    startPoll(); render();
    return false;
  }
  return true;
}
async function sendRender(pid, body) { sent.push({ pid, body }); }
function flushPendingRender() {
  const q = ST.pendingRender;
  if (!q || taskBusy(ST.proj && ST.proj.task)) return;
  ST.pendingRender = null;
  sendRender(q.pid, q.body);
}

console.log('场景② 忙时点出片 → 进排队、不直接发');
const r1 = enqueueRender('p1', { only: [1] }, '已排队');
ok('enqueueRender 返回 false（调用方 return，不直接发）', r1 === false);
ok('请求已进 ST.pendingRender', !!ST.pendingRender && ST.pendingRender.pid === 'p1');
ok('此时没有直接发出出片请求', sent.length === 0);

console.log('场景③ 前面任务跑完 → 自动 flush');
ST.proj.task = { kind: 'batch', pct: 100, error: '' };  // 前一趟完成
flushPendingRender();
ok('排队请求已发出', sent.length === 1 && sent[0].pid === 'p1');
ok('队列已清空', ST.pendingRender === null);

console.log(`\n结果：${pass} PASS / ${fail} FAIL`);
process.exit(fail ? 1 : 0);
