# -*- coding: utf-8 -*-
"""发布前断言扫描（防破解方案第三批）。

对打包产物目录做硬检查，任一不满足直接非零退出，杜绝「打完包没检查就发出去」。
检查项对应方案 §5 验证清单：
  1. 无 app/*.py 源码明文
  2. 无 prompts/*.txt 明文
  3. config 无 llm_key / ds_key / api_key / jwt_secret
  4. 版本号两处一致
  5. 无 .env / server.json / t2.sqlite 等敏感文件
  6. data/ 无运行残留（app.db / instance.lock / projects / output …）
  7. 业务二进制（app 目录下 .pyc/.pyd）无源文件路径残留（2026-11-22 增：
     零界 471 个函数被一晚全还原，源路径/私服地址全在明文里——它就栽在这）
  8. 明文 URL 提示（不阻断）：白名单外的域名列出来给人工过目

用法：
  python check_release.py <产物目录>          # 扫产物 + 版本号
  python check_release.py --src               # 只扫源码版本号（开发期自检）

由 build.bat（第 4 步后）与 build_installer.py（压 payload 前）自动调用，
不要只靠人记得跑——2026-11-22 复查发现它此前从未被接入任何构建流程。
"""
from __future__ import annotations

import json
import os
import re
import sys

# 中文 Windows 的控制台默认 GBK，print("✓") 直接 UnicodeEncodeError——
# 2026-11-22 复查实测：扫描在第 53 行就崩了，prompts/config 检查从来没跑过。
# 输出统一转 UTF-8（带替换兜底），✓/✗ 无论如何都不再让脚本崩掉。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except Exception:  # noqa: BLE001 —— 老版本 Python 没有 reconfigure 就算了
        pass

BASE = os.path.dirname(os.path.abspath(__file__))

# 产物 data/ 里只允许存在的文件（build_post.py 写入的分发配置 + 使用说明旁的空目录约定）
DATA_ALLOWED = {"config.json"}
# data/ 里出现即泄露/残留的运行时文件与目录
DATA_RESIDUE = ("app.db", "instance.lock", ".shortcut_done", "resume_pending.json",
                "audit_logs", "projects", "output", "logs", "queue_backup")
# 产物任何位置都不该出现的文件名
FORBIDDEN_FILES = (".env", "server.json", "t2.sqlite", "t2.sqlite-journal")


def fail(msg: str) -> None:
    print(f"  [X] {msg}")
    raise SystemExit(1)


def ok(msg: str) -> None:
    print(f"  [OK] {msg}")


def scan_dir(target: str) -> None:
    print(f"扫描产物目录: {target}")
    if not os.path.isdir(target):
        fail(f"产物目录不存在: {target}")

    py_files, prompt_files = [], []
    for dp, dns, fns in os.walk(target):
        for fn in fns:
            p = os.path.join(dp, fn)
            rel = os.path.relpath(p, target)
            # app 源码 .py（混淆/编译后不该有）
            if fn.endswith(".py") and (os.sep + "app" + os.sep) in (os.sep + rel):
                py_files.append(rel)
            # prompts 明文
            if fn.endswith(".txt") and ("prompts" in rel.lower()):
                prompt_files.append(rel)

    # pyarmor/Cython 产物里会有运行时的 __init__.py（pyarmor_runtime 包自带），
    # 这是正常的；真正不该出现的是业务模块的源码
    biz_py = [p for p in py_files
              if not re.search(r"pyarmor_runtime|__init__", p, re.I)]
    if biz_py:
        fail(f"产物里有业务源码 .py（{len(biz_py)} 个）: {biz_py[:5]}")
    ok(f"无业务源码 .py（运行时 __init__.py {len(py_files) - len(biz_py)} 个属正常）")

    if prompt_files:
        fail(f"产物里有 prompts 明文（{len(prompt_files)} 个）: {prompt_files[:5]}")
    ok("无 prompts 明文")

    # 敏感文件：任何位置都不该出现（红线：.env / server.json 永不进产物）
    hit = [rel for dp, dns, fns in os.walk(target) for fn in fns
           if fn.lower() in FORBIDDEN_FILES
           for rel in [os.path.relpath(os.path.join(dp, fn), target)]]
    if hit:
        fail(f"产物里有敏感文件: {hit[:5]}")
    ok("无 .env / server.json / t2.sqlite")

    # data/ 运行残留：打完包为验证双击跑一次 exe，程序会把 jwt_secret 写回
    # config、建 app.db / instance.lock（2026-11-04、2026-11-22 两次实锤）。
    data = os.path.join(target, "data")
    if os.path.isdir(data):
        leftovers = sorted(set(os.listdir(data)) - DATA_ALLOWED)
        bad = [x for x in leftovers if x.lower() in DATA_RESIDUE]
        if bad:
            fail(f"产物 data/ 里有运行残留（打包后跑过 exe？重跑 build_post.py 复位）: {bad}")
        # config 的敏感键：jwt_secret 是 2026-11-04 真实泄露过的那个，必须查
        cfg = os.path.join(data, "config.json")
        if os.path.isfile(cfg):
            text = open(cfg, encoding="utf-8", errors="replace").read()
            # jwt_secret：2026-11-04 真实泄露过的那个键，必须查。
            # account_token / device_token：开发机自己的账号令牌，打进包里等于
            # 把自己的 AI 额度发给所有客户。admin_init_password 同理。
            for key in ("llm_key", "ds_key", "api_key", "jwt_secret",
                        "account_token", "device_token", "admin_init_password"):
                # 允许空字符串 / 占位，但不允许有真实值
                m = re.search(rf'"{key}"\s*:\s*"([^"]+)"', text)
                if m and m.group(1) not in ("", "YOUR_KEY", "none", "null"):
                    fail(f"产物 data/config.json 泄露 {key}")
            ok("config 无 Key / secret")
        else:
            ok("data/ 里没有 config.json（build_post 还没跑？）")
    else:
        ok("无 data/ 目录")

    # 业务二进制里的源文件路径残留：Cython 编译的 .pyd / PyInstaller 收录的 .pyc
    # 会把编译时的源文件路径嵌进常量。绝对路径泄露的是开发机的目录布局与用户名；
    # 竞品零界的核心引擎连「D:\工作区\去重\…」都带着，拆包的人顺着路径就把
    # 作者的一切摸清了。只扫 app 目录下的产物——运行时库（pyarmor/标准库）本来
    # 就带第三方构建机的路径，扫了全是噪音。
    print("业务二进制源路径残留:")
    rx_path = re.compile(rb"[A-Za-z]:\\[^\x00\r\n\"'<>]{2,110}")
    rx_noise = re.compile(rb"python3\d|site-packages|pyarmor|Windows\\|Program Files"
                          rb"|anaconda|\\Python3|Users\\runneradmin")
    hits: list[str] = []
    for dp, dns, fns in os.walk(target):
        for fn in fns:
            if not fn.lower().endswith((".pyc", ".pyd")):
                continue
            rel = os.path.relpath(os.path.join(dp, fn), target)
            if (os.sep + "app" + os.sep) not in (os.sep + rel) \
                    and not rel.startswith("app" + os.sep):
                continue
            try:
                blob = open(os.path.join(dp, fn), "rb").read()
            except OSError:
                continue
            for m in rx_path.finditer(blob):
                s = m.group()
                if rx_noise.search(s):
                    continue
                hits.append(f"{rel} → {s.decode('utf-8', 'replace')[:110]}")
                break           # 一个文件报一处就够定位，别刷屏
    if hits:
        fail(f"业务二进制里嵌着源文件路径（{len(hits)} 个，等于把开发机布局发给用户）: "
             + "；".join(hits[:5]))
    ok(f"无源文件路径残留（扫描 app 产物 {sum(1 for dp, _, fns in os.walk(target) for fn in fns if fn.lower().endswith(('.pyc', '.pyd')))} 个 pyc/pyd）")

    # 明文 URL 提示（⑧，不阻断）：web 资源与说明文本里白名单外的域名列出来
    # 给人工过目——历史上真正出过事的不是技术泄露，是「不小心把内网地址写了进说明」。
    print("明文 URL 提示（不阻断，人工过目）:")
    rx_url = re.compile(rb"https?://[A-Za-z0-9._\-]+(?:\.[a-zA-Z]{2,})+(?:[/\w\-._?=&%]*)")
    whitelist = ("hookstudio.cn", "github.com/huhaisong", "w3.org", "ffmpeg.org",
                 "opensource.org", "alibabacloud.com", "aliyun.com", "deepseek.com",
                 "python.org", "mozilla.org", "w3c.org")
    found: dict[str, str] = {}
    for dp, dns, fns in os.walk(target):
        for fn in fns:
            if not fn.lower().endswith((".js", ".html", ".json", ".txt", ".css", ".xml")):
                continue
            p = os.path.join(dp, fn)
            try:
                blob = open(p, "rb").read()
            except OSError:
                continue
            if len(blob) > 30 * 1024 * 1024:      # 大文件（source map 等）跳过
                continue
            for m in rx_url.finditer(blob):
                u = m.group().decode("utf-8", "replace")
                if any(w in u.lower() for w in whitelist):
                    continue
                found.setdefault(u, os.path.relpath(p, target))
    for u, rel in sorted(found.items()):
        print(f"  [?] {u}   ({rel})")
    if found:
        print(f"  [OK?] {len(found)} 个白名单外 URL 已列出，请人工确认后再发布")
    else:
        ok("无白名单外明文 URL")


def check_version() -> None:
    print("版本号一致性:")
    v1 = re.search(r'VERSION\s*=\s*"([^"]+)"',
                   open(os.path.join(BASE, "app", "buildstamp.py"), encoding="utf-8").read())
    v2 = json.load(open(os.path.join(BASE, "deploy", "proxy", "release.json"), encoding="utf-8"))
    if not v1 or not v2.get("version"):
        fail("版本号读取失败")
    if v1.group(1) != v2["version"]:
        fail(f"版本号不一致: buildstamp={v1.group(1)} release.json={v2['version']}")
    ok(f"两处一致: {v1.group(1)}")


if __name__ == "__main__":
    if "--src" in sys.argv:
        check_version()
    else:
        if len(sys.argv) < 2:
            fail("用法: python check_release.py <产物目录>  或  --src")
        scan_dir(sys.argv[1])
        check_version()
    print("全部通过")
