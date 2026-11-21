/* 用真实 API 快照跑真实 renderSets，确认当前数据会画出 13 套（2026-11-21）。 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const src = fs.readFileSync(path.join(__dirname, "..", "web", "app.js"), "utf8");
const snap = JSON.parse(fs.readFileSync(path.join(__dirname, "..", "logs", "_proj_snapshot.json"), "utf8"));

// 抽取 renderSets 函数体（到下一个顶层函数注释前）
const start = src.indexOf("function renderSets() {");
const end = src.indexOf("/* ---------- 第 3 步：引流分镜", start);
const fnSrc = src.slice(start, end);

const els = {};
function el(id) {
  if (!els[id]) els[id] = { style: {}, classList: { toggle(){}, add(){}, remove(){} }, textContent: "", innerHTML: "", addEventListener(){}, querySelector(){ return null; } };
  return els[id];
}
let builtHtml = "";
const ctx = {
  ST: { proj: snap },
  $: el,
  document: { querySelector: (s) => el(s) },
  patchHtml: (node, sig, build) => { builtHtml = build(); node.innerHTML = builtHtml; return true; },
  Number, Math, JSON, Array, Object, String, Boolean, isNaN, parseInt, parseFloat,
  esc: (s) => String(s == null ? "" : s),
  fmt: (s) => String(s),
  joinDir: (a, b) => (a || "") + "/" + (b || ""),
  taskBusy: () => false,
  console,
};
vm.createContext(ctx);
vm.runInContext(fnSrc + "\nthis.__renderSets = renderSets;", ctx);
ctx.__renderSets();

const readyBadges = (builtHtml.match(/已出片<\/span>/g) || []).length;
const renderBtns = (builtHtml.match(/data-render=/g) || []).length;
const playBtns = (builtHtml.match(/data-play=/g) || []).length;
const folderBtn = els["#btn-open-folder"].textContent;
console.log("列表 HTML 长度:", builtHtml.length);
console.log("✓已出片 徽章数:", readyBadges, "(应=13)");
console.log("用播放器打开按钮:", playBtns, "(有output的套)");
console.log("待生成出片按钮:", renderBtns, "(没output的套)");
console.log("文件夹按钮文案:", folderBtn);
if (readyBadges !== 13) { console.error("FAIL: 应渲染13套已出片，实际", readyBadges); process.exit(1); }
if (!/13 套/.test(folderBtn)) { console.error("FAIL: 文件夹按钮应显示13套，实际:", folderBtn); process.exit(1); }
console.log("PASS: 当前数据会正确渲染13套已出片");
