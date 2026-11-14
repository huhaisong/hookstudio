"""部署前体检：只看不改。

跑法：`runtime/Scripts/python.exe deploy/probe.py`
输出：服务状态、端口、目录内容、证书、系统版本——用来判断是「首次部署」还是「升级」。
"""
import json
import os

import paramiko

HERE = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(HERE, "server.json"), encoding="utf-8"))

CMDS = [
    ("系统", "cat /etc/os-release | head -3; python3 -V; free -m | head -2; df -h / | tail -1"),
    ("服务状态", "systemctl is-active hsproxy 2>&1; systemctl is-enabled hsproxy 2>&1"),
    ("监听端口", "ss -lntp 2>/dev/null | grep -E ':(80|443|8000|8080) ' || echo '(无 80/443/8000)'"),
    ("代理目录", "ls -la /opt/hsproxy/ 2>&1 | head -20"),
    ("代码标识", "grep -c 'api/version' /opt/hsproxy/proxy/app.py 2>/dev/null || echo 0"),
    ("版本文件", "cat /opt/hsproxy/release.json 2>/dev/null || echo '(没有 release.json)'"),
    ("本机自测", "curl -s -m 5 http://127.0.0.1:8000/health; echo"),
    ("版本接口", "curl -s -m 5 http://127.0.0.1:8000/api/version; echo"),
    ("nginx", "command -v nginx >/dev/null && nginx -v 2>&1 && systemctl is-active nginx || echo '(没装 nginx)'"),
    ("证书", "ls -la /etc/letsencrypt/live/ 2>/dev/null | head -10 || echo '(无 letsencrypt)'"),
    ("服务日志", "journalctl -u hsproxy -n 8 --no-pager 2>/dev/null | tail -8"),
]

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(cfg["host"], username=cfg["user"], password=cfg["password"],
            timeout=30, banner_timeout=60, auth_timeout=90)
for title, cmd in CMDS:
    print(f"\n== {title}")
    _, out, err = cli.exec_command(cmd, timeout=60)
    out.channel.recv_exit_status()
    o = out.read().decode("utf-8", "replace").strip()
    e = err.read().decode("utf-8", "replace").strip()
    print(o or "(空)")
    if e:
        print("[stderr]", e[:300])
cli.close()
