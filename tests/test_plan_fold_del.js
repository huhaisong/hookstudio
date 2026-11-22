/* 自测：套太多时的三个口子（2026-11-22 用户要求）
   ① 按批折叠  ② 单套删除  ③ 批量删除
   前端这些东西全是 DOM 事件，一改就容易在「重建 / 轮询 / 全选」上踩回去，
   所以这里守几条硬约束（纯源码检查，不启服务）：
   ① 批标题行必须带 data-bn + foldable，卡片也带 data-bn（收起靠它加 .hidden）
   ② 折叠状态存 localStorage（1.5 秒轮询会重建 `#plan-list`，挂节点上一刷就没）
   ③ 默认只展开最后一批（用户 2026-11-22 定的口径）
   ④ 头部有两个勾选框，全选/取消必须点名 .plan-on-cb，别把「要删」的框也勾上
   ⑤ 单删和批删走同一个 askDelPlans，调的是 /plans-delete
   ⑥ 后端三个改动点都在：路由、审计动作、batch.drop_plans */
const fs = require('fs');
const path = require('path');
const read = (p) => fs.readFileSync(path.join(__dirname, '..', p), 'utf-8');
const js = read('web/app.js');
const html = read('web/index.html');
const css = read('web/style.css');
const mainPy = read('app/main.py');
const batchPy = read('app/batch.py');
const auditPy = read('app/audit.py');

let pass = 0, fail = 0;
const ok = (name, cond) => { cond ? (pass++, console.log('  PASS', name)) : (fail++, console.log('  FAIL', name)); };
// 判断「有没有这段代码」要先去注释，注释里写的旧行为不算实现
const code = js.replace(/\/\*[\s\S]*?\*\//g, '').replace(/^\s*\/\/.*$/gm, '');

console.log('① 按批折叠');
ok('planListHtml 收 fold 参数', /function planListHtml\(plans,\s*fold\)/.test(code));
ok('批标题走 planBatchHtml 且带 foldable/data-bn', /function planBatchHtml\(/.test(code)
  && /class="plan-batch foldable/.test(code) && /data-bn="\$\{esc\(key\)\}"/.test(code));
ok('卡片头部带 data-bn（收起靠它加 hidden）', /data-i="\$\{i\}" data-bn="\$\{bn \|\| ""\}"/.test(code)
  || /data-bn="\$\{bn \|\| ""\}" data-i/.test(code));
ok('重建后把批标题的点击挂上', /\.plan-batch\.foldable"\)\.forEach[\s\S]{0,160}togglePlanFold/.test(code));
ok('重建后应用折叠状态', /applyPlanFold\(\);/.test(code) && /function applyPlanFold\(/.test(code));
ok('折叠状态写 localStorage', /localStorage\.setItem\(planFoldStoreKey\(pid\)/.test(code)
  && /hf_plan_fold_/.test(code));
ok('默认只展开最后一批', /keys\.slice\(0, -1\)\.forEach/.test(code));
ok('新批出现时自动展开这一批', /if \(st\.last !== lastKey\)/.test(code));
ok('全部收起/全部展开按钮', /function setPlanFoldAll\(on\)/.test(code)
  && /#btn-plan-fold"\)\.addEventListener/.test(code) && /#btn-plan-unfold"\)\.addEventListener/.test(code));
ok('钩子模式不给折叠按钮', /el\.classList\.toggle\("hidden", hook \|\| !plans\.length\)/.test(code));

console.log('② 单套删除');
ok('头部有删除位（planDelBits / planDelX）', /function planDelBits\(pl\)/.test(code)
  && /function planDelX\(pl\)/.test(code));
ok('卡片和钩子卡都插了这两块', (code.match(/\$\{planDelBits\(pl\)\}/g) || []).length >= 2
  && (code.match(/\$\{planDelX\(pl\)\}/g) || []).length >= 2);
ok('× 点击不冒泡到「展开表」', /\.plan-del"\)[\s\S]{0,80}e\.stopPropagation\(\);[\s\S]{0,80}askDelPlans/.test(code));
ok('删除走 /plans-delete', /\/plans-delete`/.test(code));
ok('删前有确认弹层（danger）', /title: `删掉这 \$\{targets\.length\} \$\{unit\}？`, body, okText: "删掉", danger: true/.test(code));
ok('弹层说明「已出片的只移出列表、文件不删」', /mp4 文件留在保存目录，不会删/.test(code));

console.log('③ 批量删除');
ok('进入/退出删除模式', /function setPlanDelMode\(on\)/.test(code) && /#btn-plan-mdel"\)\.addEventListener/.test(code));
ok('删除模式给列表加 deleting（红勾靠 CSS 显形）', /list\.classList\.toggle\("deleting", on\)/.test(code));
ok('点整行 = 勾选这一套', /if \(ST\.planDel\) \{ togglePlanDelSel/.test(code));
ok('底部条显示已选几套', /已选 \$\{n\} \$\{hook \? "集" : "套"\}/.test(code));
ok('删除模式藏起出片按钮', /\["#plan-stat", "#btn-plan-all", "#btn-plan-none", "#btn-render-plans"\]/.test(code));
ok('重建后恢复临时勾选', /ST\.planDelSel\.forEach\(\(idx\) => \{/.test(code));

console.log('④ 两个勾选框不能串');
ok('全选/全不选只动 .plan-on-cb', /row\.querySelector\("\.plan-on-cb"\)/.test(code));
ok('「要出片」的勾选框带 plan-on-cb', (code.match(/class="plan-on-cb"/g) || []).length >= 2);
ok('没有残留的通配复选框选择器', !/querySelector\('input\[type="checkbox"\]'\)/.test(code));

console.log('⑤ 样式');
ok('批标题可点样式', /\.plan-batch\.foldable/.test(css) && /\.pb-arrow/.test(css));
ok('删除勾选框默认隐藏、deleting 时显形', /\.plan-del-cb \{ display: none/.test(css)
  && /\.plan-list\.deleting \.plan-del-cb \{ display: block/.test(css));
ok('选中的套有高亮', /\.plan\.picked \{ outline/.test(css));

console.log('⑥ 后端');
ok('路由存在', /@app\.post\("\/api\/projects\/\{pid\}\/plans-delete"\)/.test(mainPy));
ok('正在出片时 409 拒绝', /正在出片，等这一趟跑完再删/.test(mainPy));
ok('加了进程内锁', /_PLAN_DEL_LOCK = threading\.Lock\(\)/.test(mainPy));
ok('batch.drop_plans 存在', /def drop_plans\(p: dict, indexes: list\[int\]\) -> dict:/.test(batchPy));
ok('套号重排成 1..N', /for n, pl in enumerate\(keep, 1\):/.test(batchPy));
ok('成片记录跟着改号、被删的移出', /s\["index"\] = remap\.get\(i, i\)/.test(batchPy));
ok('套间指标重算', /pl\["overlap"\] = round\(ov, 3\)/.test(batchPy));
ok('审计动作 plan.delete', /"plan\.delete"/.test(auditPy));

console.log(`\n${fail ? '✗ 有问题' : '✓ 全部通过'}：${pass} 通过 / ${fail} 失败`);
process.exit(fail ? 1 : 0);
