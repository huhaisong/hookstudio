// 排期预览：请求序号 + 开始上传复用预览（2026-11-21）
// 现场：改时间等 10~15 秒、点开始上传再等一遍。后端不再解音轨，前端不再白算第二次。
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(path.join(__dirname, "..", "web", "publish.js"), "utf8");
const noComment = (t) => t.replace(/\/\*[\s\S]*?\*\//g, "")
  .replace(/^\s*\/\/.*$/gm, "");

let pass = 0, fail = 0;
function ok(name, cond) {
  cond ? (pass++, console.log("  PASS", name)) : (fail++, console.log("  FAIL", name));
}

function block(startMark, endMark) {
  const i = src.indexOf(startMark);
  if (i < 0) return "";
  const j = src.indexOf(endMark, i + startMark.length);
  return noComment(j > 0 ? src.slice(i, j) : src.slice(i, i + 4000));
}

const state = block("const S = {", "const S2");
ok("状态里有 planSeq", /\bplanSeq:\s*0\b/.test(state));
ok("状态里有 planFor", /\bplanFor:\s*null\b/.test(state));

const doPlan = block("async function doPlan()", "// ── 操作");
ok("doPlan 递增 seq", /const seq = \+\+S\.planSeq/.test(doPlan));
ok("成功分支丢弃过期响应", /if \(seq !== S\.planSeq\) return;/.test(doPlan));
ok("失败分支也丢弃过期响应",
  (doPlan.match(/if \(seq !== S\.planSeq\) return;/g) || []).length >= 2);
ok("空队列时作废在途预览", /S\.planSeq\+\+;/.test(doPlan));
ok("响应里记下这次预览的口径", /S\.planFor = \{ first: wantFirst, gap: wantGap, n: drafts \}/.test(doPlan));

const start = block('if (name === "start") {', 'if (name === "retry")');
const calls = [...start.matchAll(/await doPlan\(\);/g)];
const guardAt = start.indexOf("if (!(pf &&");
ok("start 只在口径不一致时补算（没有无条件那次）",
  calls.length === 1 && guardAt > 0 && calls[0].index > guardAt);
ok("start 按口径比对后复用", /pf\.first === S\.first && pf\.gap === wantGap && pf\.n === n/.test(start));
ok("口径不一致才补算", /if \(!\(pf && S\.plan &&[\s\S]{0,120}\)\) \{\s*await doPlan\(\);/.test(start));

console.log(`\n${pass} PASS / ${fail} FAIL`);
process.exit(fail ? 1 : 0);
