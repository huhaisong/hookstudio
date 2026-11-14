"""把打包目录压成发布 zip，**压缩前强制重置分发配置**。

### 为什么单独一个脚本

2026-11-04 踩到的坑：打完包为了验证，双击跑了一次 exe——程序启动时会把完整的
config.json 写回 `data/`，里面有它自己生成的 `jwt_secret`。之后直接压缩发布，
这个密钥就跟着安装包发给了所有人：**所有用户共用同一个 JWT 签名密钥**。
虽然每台机器的用户库是独立的、且只监听 127.0.0.1，但这仍然是不该出现在分发包里的东西。

所以「先测再压」这个顺序天然危险。这里把顺序固定成：
    压缩 → 之前一定调 build_post（重写三行配置）→ 再核对配置 → 再压。
人工记流程靠不住，让脚本记。

用法：`python pack_zip.py dist2\\HookStudio dist\\钩子工坊-0.1.0-测试版.zip`
"""
from __future__ import annotations

import json
import os
import sys
import zipfile

import build_post

# 分发配置允许出现的键，多一个都说明被程序跑过（有密钥泄露风险）
ALLOWED = {"dist_mode", "report_audit", "llm_model"}


def main(outdir: str, zippath: str) -> int:
    outdir = os.path.abspath(outdir)
    zippath = os.path.abspath(zippath)
    if not os.path.isdir(outdir):
        print(f"[错误] 目录不存在：{outdir}")
        return 1

    # 1) 重置配置（这一步同时保证"绝不含开发机 Key"）
    if build_post.main(outdir) != 0:
        return 1

    # 2) 核对：配置里只能有这三个键
    cfg_path = os.path.join(outdir, "data", "config.json")
    with open(cfg_path, encoding="utf-8") as f:
        cfg = json.load(f)
    extra = sorted(set(cfg) - ALLOWED)
    if extra:
        print(f"[错误] 分发配置里有不该出现的键：{extra}（程序跑过？先重置再压）")
        return 1
    for k in ("dist_mode", "report_audit"):
        if cfg.get(k) is not True:
            print(f"[错误] {k} 不是 true，分发版会退回开发行为")
            return 1
    print("[核对] 配置干净：只有 dist_mode / report_audit / llm_model，无任何密钥")

    # 3) data 目录里只能有 config.json（app.db、projects、output 都是测试残留）
    leftovers = sorted(set(os.listdir(os.path.join(outdir, "data"))) - {"config.json"})
    if leftovers:
        print(f"[错误] data 里有测试残留：{leftovers}")
        return 1
    print("[核对] data 目录只有 config.json")

    # 4) 压缩（用标准库，路径统一用 /，Windows 解压也认）
    os.makedirs(os.path.dirname(zippath), exist_ok=True)
    if os.path.exists(zippath):
        os.remove(zippath)
    n = 0
    with zipfile.ZipFile(zippath, "w", zipfile.ZIP_DEFLATED, compresslevel=6) as z:
        for root, _dirs, files in os.walk(outdir):
            for fn in files:
                full = os.path.join(root, fn)
                rel = os.path.relpath(full, outdir).replace(os.sep, "/")
                z.write(full, rel)
                n += 1
    size = os.path.getsize(zippath) / 1024 / 1024
    print(f"[完成] {n} 个文件 -> {zippath}（{size:.0f} MB）")
    return 0


if __name__ == "__main__":
    if len(sys.argv) < 3:
        print(__doc__)
        raise SystemExit(2)
    raise SystemExit(main(sys.argv[1], sys.argv[2]))
