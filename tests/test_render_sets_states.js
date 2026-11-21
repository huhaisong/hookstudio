/* 出片进度列表的三段口径（2026-11-21 用户定稿）：
   ① 已出片 → ② 本趟排队/正在出（只写文案，不给按钮）→ ③ 还没出片（排最后，带「出片」）。
   用假项目跑真实 renderSets，覆盖：队列里正在出 2 套、排队 6 套、队列里有结构表
   没有的套号、失败套、未勾选的套也照常出现在「还没出片」里。 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");
const src = fs.readFileSync(path.join(__dirname, "..", "web", "app.js"), "utf8");
const start = src.indexOf("function renderSets() {");
const end = src.indexOf("/* ---------- 第 3 步：引流分镜", start);
if (start < 0 || end < 0) { console.error("FAIL: 找不到 renderSets"); process.exit(1); }
const fnSrc = src.slice(start, end);

const proj = {
  id: "t1", name: "测试剧", plan_gen: 2,
  batch: { gen: 2, save_dir: "F:/out", failures: [{ index: 22, error: "视频处理没成功" }] },
  // 1..29：注意 30 不在这张结构表里，但队列里有它（出片中场改过结构）
  plans: Array.from({ length: 29 }, (_, i) => ({
    index: i + 1, on: (i + 1) <= 13, mode: "mix", ai_name: "主题" + (i + 1),
  })),
  sets: Array.from({ length: 13 }, (_, i) => ({
    index: i + 1, gen: 2, output: "set.mp4", output_size: 80 * 1048576,
    output_duration: 300, edl: [1, 2, 3],
  })),
  task: {
    kind: "batch", pct: 13.6,
    msg: "已完成 0/8 套（同时 2 套） · 第23套 切第 16/25 段（第 30 集 41.9s）",
    data: {
      total: 8, conc: 2,
      queue: [
        { index: 23, state: "run", pct: 45, msg: "切第 16/25 段" },
        { index: 24, state: "run", pct: 12, msg: "切第 3/25 段" },
        ...Array.from({ length: 6 }, (_, k) => ({ index: 25 + k, state: "wait", pct: 0, msg: "排队中" })),
      ],
    },
  },
};

const els = {};
function el(id) {
  if (!els[id]) {
    els[id] = {
      style: {}, classList: { toggle() {}, add() {}, remove() {} },
      textContent: "", innerHTML: "", scrollTop: 0,
      addEventListener() {}, querySelector() { return null; },
    };
  }
  return els[id];
}
let html = "";
const ctx = {
  ST: { proj },
  $: el,
  patchHtml: (node, sig, build) => { html = build(); node.innerHTML = html; return true; },
  Number, Math, JSON, Array, Object, String,
  esc: (s) => String(s == null ? "" : s).replace(/&/g, "&amp;").replace(/</g, "&lt;"),
  fmt: (s) => String(s),
  joinDir: (a, b) => (a || "") + "/" + (b || ""),
  console,
};
vm.createContext(ctx);
vm.runInContext(fnSrc + "\nthis.__r = renderSets;", ctx);
ctx.__r();

const cnt = (re) => (html.match(re) || []).length;
const nDone = cnt(/✓ 已出片<\/span>/g);
const nRun = cnt(/正在出片 \d+%<\/span>/g);
const nWait = cnt(/等待出片<\/span>/g);
const nBtn = cnt(/data-render=/g);
const nPlay = cnt(/data-play=/g);
const nFail = cnt(/失败：/g);
const tally = els["#b3-tally"].textContent;

const fail = (m) => { console.error("FAIL: " + m); process.exitCode = 1; };
console.log("已出片:", nDone, "| 正在出:", nRun, "| 等待:", nWait, "| 出片按钮:", nBtn,
  "| 播放按钮:", nPlay, "| 失败行:", nFail);
console.log("顶部口径:", JSON.stringify(tally));

if (nDone !== 13) fail("已出片应 13 行，实际 " + nDone);
if (nPlay !== 13) fail("播放按钮应 13 个，实际 " + nPlay);
if (nRun !== 2) fail("「正在出片」应 2 行（2 路并发都要标出来），实际 " + nRun);
if (nWait !== 6) fail("「等待出片」应 6 行，实际 " + nWait);
if (nBtn !== 9) fail("出片按钮只该给还没出片的 9 套（14-22），实际 " + nBtn);
if (nFail !== 1) fail("失败套应 1 行，实际 " + nFail);

// 顺序：已出片 → 正在出/等待 → 还没出片
const iWait = html.indexOf("等待出片"), iTodo = html.indexOf("还没出片");
if (!(html.indexOf("✓ 已出片") < iWait && iWait < iTodo)) fail("三段顺序不对");
// 在途的行绝不能带按钮：最后一个按钮必须落在「还没出片」之后
const lastBtn = html.lastIndexOf("data-render=");
if (lastBtn < iTodo) fail("排队的行里混进了出片按钮");
// 队列里结构表没有的套号（30）也要出现
if (!/第 30 套/.test(html)) fail("队列里 30 套没画出来（结构表里已没有它）");
if (!/项目已出片 13\/30 套/.test(tally)) fail("顶部没写「项目已出片 13/30 套」");
if (!/本趟 8 套：正在出 2、排队 6/.test(tally)) fail("顶部没写本趟在途分解");
if (!/还没出 9 套/.test(tally)) fail("顶部没写还没出多少套");

if (!process.exitCode) console.log("PASS: 三段口径、在途文案、按钮归属都正确");
