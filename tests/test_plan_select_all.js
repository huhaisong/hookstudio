/* 验证 planSelectAll 会同步所有复选框（2026-11-21「全选/全选不起作用」）。
   从真实 app.js 抽取函数体在 mock DOM 下执行，测的是线上代码本身。 */
const fs = require("fs");
const path = require("path");
const vm = require("vm");

const src = fs.readFileSync(path.join(__dirname, "..", "web", "app.js"), "utf8");
const m = src.match(/async function planSelectAll\(on\) \{[\s\S]*?\n\}/);
if (!m) throw new Error("没找到 planSelectAll 函数");
const fnSrc = m[0];

function makeCtx(plans, matchCount) {
  const rows = [];
  for (let i = 0; i < matchCount; i++) {
    rows.push({
      _checked: plans[i].on !== false,
      _off: plans[i].on === false,
      _wildcard: false,
      querySelector(sel) {
        // 2026-11-22 起头部有两个勾选框：「要出片」(.plan-on-cb) 和「批量删除」(plan-del-cb)。
        // 这里同时守两件事：① 必须点名 .plan-on-cb；② 走了通配选择器就标红——
        // 通配会勾到第一个框（删除框），全选就变成了「全勾上要删」。
        if (sel === ".plan-on-cb") {
          const row = this;
          return { set checked(v) { row._checked = v; }, get checked() { return row._checked; } };
        }
        if (sel === 'input[type="checkbox"]') { this._wildcard = true; return null; }
        return null;
      },
      classList: {
        toggle(cls, force) { row._off = force; },
      },
    });
  }
  // classList.toggle 里的 this 指向 row 需要绑定
  rows.forEach((r) => { r.classList = { toggle: (cls, force) => { r._off = force; } }; });

  const calls = { api: null, stat: 0 };
  const ctx = {
    document: { querySelectorAll: (sel) => (sel === "#plan-list .plan" ? rows : []) },
    updatePlanStat: () => { calls.stat++; },
    api: async (url, opts) => { calls.api = { url, body: JSON.parse(opts.body) }; return { ok: true }; },
    toast: () => {},
    ST: { proj: { id: "pid1", plans } },
    JSON,
    calls,
    rows,
  };
  vm.createContext(ctx);
  vm.runInContext(fnSrc + "\nthis.__fn = planSelectAll;", ctx);
  return ctx;
}

(async () => {
  // 场景1：全选 —— 所有复选框变勾、off 去掉、API 全 true、统计刷新
  {
    const plans = [{ on: false }, { on: true }, { on: false }];
    const ctx = makeCtx(plans, 3);
    await ctx.__fn(true);
    const checked = ctx.rows.map((r) => r._checked);
    const offs = ctx.rows.map((r) => r._off);
    if (!checked.every(Boolean)) throw new Error("全选后仍有未勾: " + checked);
    if (offs.some(Boolean)) throw new Error("全选后仍有 off: " + offs);
    if (ctx.rows.some((r) => r._wildcard)) throw new Error("用了通配复选框选择器，会勾到「要删」那个框");
    if (!ctx.calls.api.body.on.every(Boolean)) throw new Error("API 未全 true");
    if (ctx.calls.stat < 1) throw new Error("统计未刷新");
    console.log("PASS 全选：3/3 复选框勾上、off 清除、API 全 true、统计刷新");
  }
  // 场景2：全不选 —— 所有复选框取消、off 加上、API 全 false
  {
    const plans = [{ on: false }, { on: true }, { on: true }];
    const ctx = makeCtx(plans, 3);
    await ctx.__fn(false);
    const checked = ctx.rows.map((r) => r._checked);
    const offs = ctx.rows.map((r) => r._off);
    if (checked.some(Boolean)) throw new Error("全不选后仍有勾: " + checked);
    if (!offs.every(Boolean)) throw new Error("全不选后仍有未 off: " + offs);
    if (ctx.calls.api.body.on.some(Boolean)) throw new Error("API 未全 false");
    console.log("PASS 全不选：3/3 复选框取消、off 加上、API 全 false");
  }
  console.log("ALL PASS");
})().catch((e) => { console.error("FAIL:", e.message); process.exit(1); });
