# 给代理套 HTTPS（Caddy 自动申请 Let's Encrypt 证书）：python tls.py
import json
import os
import time

import paramiko

HERE = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(HERE, "server.json"), encoding="utf-8"))
DOMAIN = "api.hookstudio.cn"

cli = paramiko.SSHClient()
cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
cli.connect(cfg["host"], port=cfg.get("port", 22), username=cfg.get("user", "root"),
            password=cfg["password"], timeout=30, banner_timeout=45, auth_timeout=90)


def run(cmd, title=None, timeout=300):
    if title:
        print(f"== {title}")
    _, out, err = cli.exec_command(cmd, timeout=timeout)
    code = out.channel.recv_exit_status()
    o = out.read().decode("utf-8", "replace").strip()
    e = err.read().decode("utf-8", "replace").strip()
    if o:
        print("  " + o.replace("\n", "\n  ")[:1500])
    if e:
        print("  [stderr] " + e[:800])
    return code, o, e


run("mkdir -p /etc/caddy /var/lib/caddy", "建 Caddy 目录")

sftp = cli.open_sftp()

caddyfile = f"""{DOMAIN} {{
	encode gzip
	reverse_proxy 127.0.0.1:8000 {{
		header_up X-Real-IP {{remote_host}}
	}}
	log {{
		output file /var/log/caddy/access.log
		format json
	}}
}}
"""
with sftp.open("/etc/caddy/Caddyfile", "w") as f:
    f.write(caddyfile)
print("== 已写入 /etc/caddy/Caddyfile")

unit = """[Unit]
Description=Caddy (HookStudio TLS front)
After=network.target

[Service]
User=root
Environment=XDG_DATA_HOME=/var/lib/caddy
Environment=XDG_CONFIG_HOME=/var/lib/caddy
ExecStart=/usr/local/bin/caddy run --config /etc/caddy/Caddyfile --adapter caddyfile
ExecReload=/usr/local/bin/caddy reload --config /etc/caddy/Caddyfile --adapter caddyfile
Restart=always
RestartSec=5
LimitNOFILE=1048576
MemoryMax=120M

[Install]
WantedBy=multi-user.target
"""
with sftp.open("/etc/systemd/system/caddy.service", "w") as f:
    f.write(unit)
print("== 已写入 caddy.service")
sftp.close()

run("mkdir -p /var/log/caddy", "建日志目录")
run("/usr/local/bin/caddy validate --config /etc/caddy/Caddyfile --adapter caddyfile 2>&1 | tail -5",
    "校验 Caddyfile")
run("systemctl daemon-reload && systemctl enable --now caddy", "启动 Caddy")
time.sleep(12)
run("systemctl is-active caddy", "确认 Caddy 活跃")
run("ss -ltnp | grep -E ':80|:443' || echo 'no listener'", "查看 80/443 监听")
run("curl -s -m 20 https://" + DOMAIN + "/health; echo", "本机 https 自测", timeout=60)
print("\n查看证书（若失败请稍等 30 秒再看）：")
run("ls -1 /var/lib/caddy/certificates/*/ 2>/dev/null || echo '证书目录还没生成'", "证书目录")
run("journalctl -u caddy -n 20 --no-pager", "Caddy 最近日志")
cli.close()
