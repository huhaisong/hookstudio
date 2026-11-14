# 远程执行命令的小工具：python ssh_run.py "命令"
# 连接信息存在 server.json，不要提交到公开仓库
import json
import os
import sys

import paramiko

HERE = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(HERE, "server.json"), encoding="utf-8"))

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(
    cfg["host"], port=cfg.get("port", 22),
    username=cfg.get("user", "root"), password=cfg["password"],
    timeout=30, banner_timeout=45, auth_timeout=90,
)
cmd = " ".join(sys.argv[1:])
stdin, stdout, stderr = cli.exec_command(cmd, timeout=600)
out = stdout.read().decode("utf-8", "replace")
err = stderr.read().decode("utf-8", "replace")
code = stdout.channel.recv_exit_status()
if out:
    print(out, end="")
if err:
    print("[stderr]", err, end="", file=sys.stderr)
sys.exit(code)
