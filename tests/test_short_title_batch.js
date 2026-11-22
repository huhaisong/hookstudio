// ② 整批短标题（2026-11-21）
const fs = require("fs");
const path = require("path");
const src = fs.readFileSync(path.join(__dirname, "..", "web", "publish.js"), "utf8");
const noComment = (t) => t.replace(/\/\*[\s\S]*?\*\//g, "")
  .replace(/^\s*\/\/.*$/gm, "");

let pass = 0, fail = 0;
const ok = (n, c) => c ? (pass++, console.log("  PASS", n)) : (fail++, console.log("  FAIL", n));

const fStart = src.indexOf("function fieldsHTML(");
const fields = noComment(src.slice(fStart, fStart + 2200));
ok("② 有短标题输入框", /data-bf="short_title"/.test(fields));
ok("短标题 maxlength=16（平台上限）", /data-bf="short_title" maxlength="16"/.test(fields));
ok("短标题取模板值", /value="\$\{esc\(t\.short_title \|\| ""\)\}"/.test(fields));
ok("没有把短标题写死成逐条字段", !/data-jf="short_title"[\s\S]{0,80}data-bf/.test(fields));

const chStart = src.indexOf('const bf = e.target.closest("[data-bf]")');
const change = noComment(src.slice(chStart, chStart + 900));
ok("不满 6 字先提示、不发给后端", /短标题还差 \$\{6 - n\} 个字/.test(change));
ok("够 6 字才 doApply()", /applyBusy = doApply\(\);/.test(change));

const inStart = src.indexOf('VIEW().addEventListener("input"');
const input = noComment(src.slice(inStart, inStart + 700));
ok("短标题打字时防抖套用", /data-bf="short_title"/.test(input) && /autoApply\(600\)/.test(input));
ok("1~5 个字期间不自动保存", /if \(!n \|\| n >= 6\) autoApply\(600\)/.test(input));

ok("套用请求会带上短标题（遍历 data-bf）", /querySelectorAll\("\[data-bf\]"\)/.test(src));

console.log(`\n${pass} PASS / ${fail} FAIL`);
process.exit(fail ? 1 : 0);
