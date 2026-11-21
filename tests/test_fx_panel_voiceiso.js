/* 出片面板的两处改动（2026-11-21 用户定）：
   ① 片头组（看点字幕 / 开场钩子卡）整组删掉 —— 混剪结构只交 from/to，AI 不再给
      块标题和钩子文案，勾了只烧一句固定的「精彩片段」或者干脆不出卡。
   ② 一键全自动面板「配乐与声音」补上「去掉原片自带音乐」勾选，并真的传进 cfg.fx。

   这里不跑浏览器，只做两件事：查源码里那两个控件是否真没了、分组编号是否还连续；
   再把 autoParamsHTML / autoReadCfg 单独摘出来用假 DOM 跑一遍，确认勾/不勾能落到
   voice_iso 上（写成硬编码 true 就等于用户勾了没用）。 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const src = fs.readFileSync(path.join(__dirname, "..", "web", "app.js"), "utf8");
const ok = [];
const fail = [];
function check(name, cond, extra) {
  (cond ? ok : fail).push(extra ? `${name}（${extra}）` : name);
}

/* ---------- ① 片头组必须整组消失 ---------- */
check("源码里没有 id=\"${p}title\"", src.indexOf('id="${p}title"') < 0);
check("源码里没有 id=\"${p}hook\"", src.indexOf('id="${p}hook"') < 0);

const fxStart = src.indexOf("function fxPanelHTML(p) {");
const fxEnd = src.indexOf("function readFx(", fxStart);
check("找得到 fxPanelHTML", fxStart > 0 && fxEnd > fxStart);
const panel = fxStart > 0 && fxEnd > fxStart ? src.slice(fxStart, fxEnd) : "";
// 只看会渲染出去的内容：函数体里的 // 注释是给人看的，说的是「这两项为什么删了」
const panelCode = panel.split("\n").filter((l) => !/^\s*\/\//.test(l)).join("\n");
check("面板里没有「看点字幕」", panelCode.indexOf("看点字幕") < 0);
check("面板里没有「开场钩子卡」", panelCode.indexOf("开场钩子卡") < 0);

// 分组编号：删掉第 1 组之后必须重排成 1、2、3，否则界面会出现「1 之后直接 3」
const gno = [...panelCode.matchAll(/<i class="fx-gno">(\d)<\/i>/g)].map((m) => Number(m[1]));
check("分组编号连续", JSON.stringify(gno) === JSON.stringify([1, 2, 3]), JSON.stringify(gno));

/* ---------- ② 一键全自动：勾选要落到 voice_iso ---------- */
const aStart = src.indexOf("const AUTO_DEF_JOBS");
const aEnd = src.indexOf("/* 绑定面板交互", aStart);
check("找得到全自动参数区", aStart > 0 && aEnd > aStart);
const autoSrc = aStart > 0 && aEnd > aStart ? src.slice(aStart, aEnd) : "";

const inputs = {};
function mkInput(v, checked) {
  const ev = { fire: [] };
  return {
    value: v == null ? "" : String(v),
    checked: !!checked,
    addEventListener: (t, f) => ev.fire.push([t, f]),
    dispatchEvent: () => {},
  };
}
const sandbox = {
  PM_MODELS: [["deepseek-v4.1-flash", "flash"], ["deepseek-v4-pro", "pro"]],
  esc: (s) => String(s == null ? "" : s),
  // autoFillCfg 回填配乐会 dispatchEvent(new Event("change"))，vm 里没有 Event
  Event: class { constructor(t) { this.type = t; } },
  console,
  document: {
    getElementById: (id) => inputs[id] || null,
    querySelector: () => null,
  },
};
vm.createContext(sandbox);
try {
  vm.runInContext(autoSrc, sandbox);
} catch (e) {
  fail.push("全自动参数区能单独执行（" + e.message + "）");
}

if (typeof sandbox.autoParamsHTML === "function") {
  const html = sandbox.autoParamsHTML("ap_");
  check("面板里有「去掉原片自带音乐」", html.indexOf("去掉原片自带音乐") >= 0);
  check("控件 id 带前缀", html.indexOf('id="ap_voiceiso"') >= 0);
  check("默认勾上", /id="ap_voiceiso"[^>]*checked/.test(html));
} else {
  fail.push("autoParamsHTML 能被取出");
}

if (typeof sandbox.autoReadCfg === "function") {
  // 假面板：三个模型各 5 套、存片目录有、配乐关
  for (let i = 0; i < 3; i++) {
    inputs["ap_model" + i] = mkInput("deepseek-v4-pro");
    inputs["ap_count" + i] = mkInput(5);
  }
  inputs["ap_minmin"] = mkInput(AUTO_MIN_MIN());
  inputs["ap_maxmin"] = mkInput(AUTO_MAX_MIN());
  inputs["ap_savedir"] = mkInput("F:/out");
  inputs["ap_bgm"] = mkInput("", false);

  function AUTO_MIN_MIN() { return 3; }
  function AUTO_MAX_MIN() { return 7; }

  inputs["ap_voiceiso"] = mkInput("", false);
  const off = sandbox.autoReadCfg("ap_", false);
  check("取消勾选 → voice_iso=false", off && off.fx && off.fx.voice_iso === false,
    off && off.fx ? String(off.fx.voice_iso) : "null");

  inputs["ap_voiceiso"] = mkInput("", true);
  const on = sandbox.autoReadCfg("ap_", false);
  check("勾上 → voice_iso=true", on && on.fx && on.fx.voice_iso === true,
    on && on.fx ? String(on.fx.voice_iso) : "null");

  // 回填：老配置没有 voice_iso 字段时必须默认勾上（跟出片面板一致）
  if (typeof sandbox.autoFillCfg === "function") {
    inputs["ap_voiceiso"] = mkInput("", false);
    sandbox.autoFillCfg("ap_", { jobs: [{ model: "deepseek-v4-pro", count: 5 }], fx: {} });
    check("老配置回填默认勾上", inputs["ap_voiceiso"].checked === true);
    inputs["ap_voiceiso"] = mkInput("", true);
    sandbox.autoFillCfg("ap_", { jobs: [{ model: "deepseek-v4-pro", count: 5 }],
      fx: { voice_iso: false } });
    check("存过 false 的回填成不勾", inputs["ap_voiceiso"].checked === false);
  } else {
    fail.push("autoFillCfg 能被取出");
  }
} else {
  fail.push("autoReadCfg 能被取出");
}

/* ---------- ③ 一键全自动启动前必须确认 ---------- */
const hStart = src.indexOf('$("#btn-autopipe").addEventListener');
const hEnd = src.indexOf("/* ---------- 项目 ----------", hStart);
const hSrc = hStart > 0 && hEnd > hStart ? src.slice(hStart, hEnd) : "";
check("找得到一键全自动按钮处理", hStart > 0 && hEnd > hStart);
if (hSrc) {
  const cAt = hSrc.indexOf("askConfirm(");
  const apiAt = hSrc.indexOf("/autopipe");
  check("启动前弹确认框", cAt >= 0 && apiAt > cAt, `askConfirm@${cAt} api@${apiAt}`);
  check("取消后不发请求", /if\s*\(!\s*go\s*\)\s*return/.test(hSrc));
  check("确认框写了模型与套数", hSrc.indexOf("${jobLine}") >= 0 && hSrc.indexOf("${total}") >= 0);
}

ok.forEach((t) => console.log("PASS  " + t));
fail.forEach((t) => console.log("FAIL  " + t));
console.log(`\n${ok.length} passed, ${fail.length} failed`);
process.exit(fail.length ? 1 : 0);
