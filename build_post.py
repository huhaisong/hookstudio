"""打包后续处理：写分发配置、拷使用说明、做防呆检查。

### 为什么单独一个 .py 而不是在 build.bat 里写 `python -c "..."`

2026-11-04 实测：bat 里夹着长 Python 单行命令，cmd 的引号/代码页会把命令拆坏——
控制台冒出 `'sys' is not recognized as an internal or external command`，
但退出码还是 0，看上去像打包成功，实际那一步没执行。**编码与引号的事交给 Python
自己处理**（文件是 UTF-8，参数只有一个路径），bat 只负责调一行。

用法：`python build_post.py dist\\HookStudio`
"""
from __future__ import annotations

import json
import os
import shutil
import sys

# 分发版必须显式告诉后端「我是发给别人的」：
#   dist_mode    = 设置页不露 Key/模型，后端也拒绝改（终端用户不该自己填 Key）
#   report_audit = 把本机操作日志推到服务器（否则后台看不到用户在做什么）
#   llm_model    = 官方推荐的默认模型，写死不跟 DEFAULT_CONFIG 走：
#                  旧档 qwen-plus 实测三套结构重合 96%（等于把同一部剧重剪三遍），
#                  而分发版用户改不了模型，默认值给错就是整批人体验都错。
DIST_CONFIG = {"dist_mode": True, "report_audit": True, "llm_model": "qwen3.5-plus"}


def main(outdir: str) -> int:
    outdir = os.path.abspath(outdir)
    if not os.path.isdir(outdir):
        print(f"[错误] 目录不存在：{outdir}")
        return 1

    data = os.path.join(outdir, "data")
    os.makedirs(data, exist_ok=True)
    cfg_path = os.path.join(data, "config.json")
    # 只写这两个开关：**绝不拷贝开发机的 config.json**，那里有明文 Key
    with open(cfg_path, "w", encoding="utf-8") as f:
        json.dump(DIST_CONFIG, f, ensure_ascii=False, indent=2)
    print(f"[4/5] 分发配置已写入：{cfg_path}")

    # 防呆：源 data/config.json 里如果带着 Key，只在控制台提醒，不进包
    src = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "config.json")
    try:
        with open(src, encoding="utf-8") as f:
            s = json.load(f) or {}
        bad = [k for k in ("llm_key", "ds_key") if s.get(k)]
        if bad:
            print("[提醒] 开发机 data/config.json 里有 " + "、".join(bad)
                  + "，它不会进安装包，但别手滑把 data 整个拷进去")
    except Exception:  # noqa: BLE001
        pass

    # 使用说明放到根目录，用户第一眼能看到
    doc = os.path.join(os.path.dirname(os.path.abspath(__file__)), "docs", "使用说明.txt")
    if os.path.isfile(doc):
        shutil.copy(doc, os.path.join(outdir, "使用说明.txt"))
        print("[4/5] 使用说明.txt 已放入")

    # 体积摘要：923MB 这个数字用户确认过可接受，打出来便于每次比对有没有异常膨胀
    total = 0
    for root, _dirs, files in os.walk(outdir):
        for fn in files:
            try:
                total += os.path.getsize(os.path.join(root, fn))
            except OSError:
                pass
    print(f"[4/5] 安装包目录 {total / 1024 / 1024:.0f} MB")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1] if len(sys.argv) > 1 else r"dist\HookStudio"))
