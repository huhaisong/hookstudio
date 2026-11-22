/* 自测：发布页「局部重画」是否真的局部（2026-11-21 用户反馈：
   点右栏「文案」，左边 ②「这批视频的通用设置」整块刷一下）。
   根因是那几个按钮都走整页 render()。这里守四条：
   ① 文案/收起/队列切换/筛选 只调 paintRight()，不许调 render()
   ② 勾选候选只调 paintCand()（以前整页重画，② 里打的字会没）
   ③ paintRight 重建右栏后要把 #pub-plan 的排期摘要还回去
   ④ render() 的左栏 ① 走 candSec()、右栏走 rightHTML()，没有第二份拷贝 */
const fs = require('fs');
const path = require('path');
const src = fs.readFileSync(path.join(__dirname, '..', 'web', 'publish.js'), 'utf-8');

let pass = 0, fail = 0;
function ok(name, cond) { cond ? (pass++, console.log('  PASS', name)) : (fail++, console.log('  FAIL', name)); }
// 判断「有没有调用」必须去掉注释：注释里写「以前走 render()」不算调用
const noComment = (t) => t.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

// 取某个 if 分支的源码（从 `if (name === "x") {` 到下一个同级 `if (name === ` 或函数尾）
function branch(name) {
  const start = src.indexOf(`if (name === "${name}") {`);
  if (start < 0) return '';
  const rest = src.slice(start + 10);
  const next = rest.search(/\n      if \(name === "/);
  return next < 0 ? rest : rest.slice(0, next);
}

console.log('① 右栏四个动作不整页重画');
for (const act of ['editf', 'closef', 'qtab', 'qfilter']) {
  const b = noComment(branch(act));
  ok(`${act} 调 paintRight()`, /paintRight\(/.test(b));
  ok(`${act} 不再调 render()`, !/\brender\(\)/.test(b));
}

console.log('② 勾选候选只重画 ①');
const cbStart = src.indexOf('const cb = e.target.closest("[data-cand]");');
const cbEnd = src.indexOf('if (e.target.id === "pub-proj-sel")', cbStart);
const cbBlock = noComment(src.slice(cbStart, cbEnd > 0 ? cbEnd : cbStart + 800));
ok('勾选分支调 paintCand()', /paintCand\(\)/.test(cbBlock));
ok('勾选分支不再调 render()', !/\brender\(\)/.test(cbBlock));

console.log('③ 右栏重建后保住排期摘要');
const prStart = src.indexOf('function paintRight(');
const prBlock = src.slice(prStart, prStart + 1800);
ok('paintRight 先取出 #pub-plan 的内容', /querySelector\("#pub-plan"\)/.test(prBlock) && /planHTML/.test(prBlock));
ok('paintRight 写回 #pub-plan', /plan2\.innerHTML = planHTML/.test(prBlock));
ok('paintRight 找不到节点时退回整页 render()', /if \(!box \|\| !d\) \{ render\(\); return; \}/.test(prBlock));
// ② 右栏内部滚动：重建 .pub-right 会把 .pub-rows 的 scrollTop 归零，
// 在第 11 条点「文案」就跳回第 1 条（2026-11-21 用户反馈）
ok('paintRight 记住 .pub-rows 的滚动位置', /rowsEl\.scrollTop/.test(prBlock) && /keepTop/.test(prBlock));
ok('paintRight 重建后还原滚动位置', /rows2\.scrollTop = resetScroll \? 0 : keepTop/.test(prBlock));
ok('切队列/已提交、换筛选时回到顶部', /paintRight\(true\)/.test(src) && (src.match(/paintRight\(true\)/g) || []).length === 2);
ok('paintRight 还原整页滚动', /window\.scrollTo\(0, keepY\)/.test(prBlock));
ok('展开的那条会最小幅度滚进可视区', /scrollIntoView\(\{ block: "nearest" \}\)/.test(prBlock));

console.log('④ render() 只有一份左① 一份右栏');
ok('render 用 ${candSec(noAcc)}', /<div class="pub-left">\s*\n\s*\$\{candSec\(noAcc\)\}/.test(src));
ok('render 用 ${rightHTML(d)}', /<aside class="pub-right">\$\{rightHTML\(d\)\}<\/aside>/.test(src));
ok('① 有稳定 id pub-sec1（供局部替换）', /<section class="pub-sec" id="pub-sec1">/.test(src));
ok('paintCand 用 outerHTML 替换 ①', /sec\.outerHTML = candSec\(!currentAid\(\)\)/.test(src));
// 旧实现里 aside 内的队列片段若还留在 render 里，说明搬漏了
ok('render 里不再残留「删掉没安排的」模板', src.indexOf('删掉没安排的') === src.lastIndexOf('删掉没安排的'));

console.log(`\n${pass} PASS / ${fail} FAIL`);
if (fail) process.exitCode = 1;
