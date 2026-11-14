# 一键部署（纯标准库版，不需要 pip/apt/venv）：python deploy.py
import json
import os
import secrets
import stat
import sys
import time

import paramiko

HERE = os.path.dirname(os.path.abspath(__file__))
cfg = json.load(open(os.path.join(HERE, "server.json"), encoding="utf-8"))

local_cfg = json.load(open(os.path.join(HERE, "..", "data", "config.json"),
                           encoding="utf-8"))
DASH_KEY = local_cfg["llm_key"].strip()

env_file = os.path.join(HERE, ".env.admin")
ADMIN = (open(env_file, encoding="utf-8").read().strip()
         if os.path.exists(env_file) else secrets.token_urlsafe(24))
if not os.path.exists(env_file):
    open(env_file, "w").write(ADMIN)
    print("生成新的 ADMIN_KEY，保存在 deploy/.env.admin")


def connect():
    last = None
    for i in range(5):
        try:
            cli = paramiko.SSHClient()
            cli.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            cli.connect(cfg["host"], username=cfg["user"], password=cfg["password"],
                        timeout=30, banner_timeout=60, auth_timeout=90)
            return cli
        except Exception as exc:  # noqa: BLE001
            last = exc
            print(f"连接失败({i+1}/5): {type(exc).__name__}，60 秒后重试")
            time.sleep(60)
    raise last


cli = connect()


def run(cmd, title=None, check=True, timeout=600):
    if title:
        print(f"== {title}")
    _, out, err = cli.exec_command(cmd, timeout=timeout)
    code = out.channel.recv_exit_status()
    o = out.read().decode("utf-8", "replace").strip()
    e = err.read().decode("utf-8", "replace").strip()
    if o:
        print("  " + o.replace("\n", "\n  ")[:1200])
    if e:
        print("  [stderr] " + e[:600])
    if check and code != 0:
        print(f"!! 命令失败（exit {code}）：{cmd[:120]}")
        sys.exit(code)
    return code, o, e


# 0) 确认没有卡死的 apt（有也不管了，我们不需要它）
run("pkill -9 apt-get 2>/dev/null; echo ok", "清理可能卡死的 apt", check=False)

# 1) 上传代码
# 先本地编译检查：把语法坏掉的 app.py 推上生产 = 服务起不来，整台机器的 AI 都断。
import py_compile
py_compile.compile(os.path.join(HERE, "proxy", "app.py"), doraise=True)
print("== app.py 本地语法检查通过")

run("mkdir -p /opt/hsproxy/proxy", "建目录")
sftp = cli.open_sftp()
sftp.put(os.path.join(HERE, "proxy", "app.py"), "/opt/hsproxy/proxy/app.py")
print("== 已上传 app.py（纯标准库）")

# 运营后台页面（2026-11-04）：GET /admin 直接读这个文件返回。
# 它只是个空壳——所有数据接口仍然要 X-Admin-Key，页面只是把手动 curl 变成点按钮。
admin_page = os.path.join(HERE, "proxy", "admin.html")
if os.path.isfile(admin_page):
    sftp.put(admin_page, "/opt/hsproxy/proxy/admin.html")
    print("== 已上传 admin.html（运营后台，/admin）")
else:
    print("!! 本地缺少 proxy/admin.html，线上 /admin 会返回「页面缺失」提示")

# IP 归属离线库（2026-11-07）：后台列表的 IP 后面显示中文地名。
# 纯静态数据、纯本地查询，不装依赖、不请求第三方、不占常驻内存。
# 只有本地文件存在才上传；库不在时服务端自动降级成只显示 IP，其余功能不受影响。
ip_geo_db = os.path.join(HERE, "proxy", "ip2region_v4.xdb")
if os.path.isfile(ip_geo_db):
    sftp.put(ip_geo_db, "/opt/hsproxy/proxy/ip2region_v4.xdb")
    print("== 已上传 ip2region_v4.xdb（IP 归属库，约 11MB）")
else:
    print("!! 本地缺少 IP 归属库，后台 IP 后面不显示地名（不影响其他功能）")

# 版本闸的判据（2026-11-04）：客户端问 /api/version，读的就是这个文件。
# 改服务器上的 /opt/hsproxy/release.json 即可推一次强制更新，不用重新部署。
release_local = os.path.join(HERE, "proxy", "release.json")
if os.path.isfile(release_local):
    sftp.put(release_local, "/opt/hsproxy/release.json")
    print("== 已上传 release.json（首次发布：min_supported 留空，不强制任何人）")
else:
    print("== 本地没有 release.json，保留服务器上已有的（升级时不覆盖运维手改的值）")

# 2) 环境文件（真实 Key 只存服务器，权限 600）
env = (f"DASHSCOPE_KEY={DASH_KEY}\n"
       f"ADMIN_KEY={ADMIN}\n"
       f"DB_PATH=/opt/hsproxy/data.sqlite\n"
       f"PORT=8000\n"
       f"RELEASE_FILE=/opt/hsproxy/release.json\n"
       # 每日额度按等级算（2026-11-07）：免费档沿用 FREE_CALLS_PER_DAY，
       # 进阶 / 内部档单独配。改完 systemctl restart hsproxy 生效，不用改代码重传。
       # 内部档默认 1000000 次/天 = 实际不限量，后台显示「不限量」。
       f"FREE_CALLS_PER_DAY={os.environ.get('FREE_CALLS_PER_DAY', '10')}\n"
       f"LEVEL_PRO_CALLS={os.environ.get('LEVEL_PRO_CALLS', '50')}\n"
       f"LEVEL_INTERNAL_CALLS={os.environ.get('LEVEL_INTERNAL_CALLS', '1000000')}\n"
       # 限流阈值放环境变量：调数值不用改代码重传，改完 systemctl restart 即可。
       # 注意国内运营商大量用户共用公网 IP（CGNAT），按 IP 限太严会误伤正常用户。
       f"IP_LIMIT_HOUR={os.environ.get('IP_LIMIT_HOUR', '30')}\n"
       f"IP_LIMIT_DAY={os.environ.get('IP_LIMIT_DAY', '200')}\n")
with sftp.open("/opt/hsproxy/.env", "w") as f:
    f.write(env)
sftp.chmod("/opt/hsproxy/.env", stat.S_IRUSR | stat.S_IWUSR)
print("== 已写入 /opt/hsproxy/.env（权限 600）")

# 3) swap（0.4G 内存保险）
code, o, _ = run("swapon --show=NAME --noheadings | wc -l", check=False)
if o == "0":
    run("fallocate -l 1G /swapfile && chmod 600 /swapfile && mkswap /swapfile && "
        "swapon /swapfile; grep -q '/swapfile' /etc/fstab || "
        "echo '/swapfile none swap sw 0 0' >> /etc/fstab", "创建 1G swap")
else:
    print("== swap 已存在，跳过")

# 4) systemd（直接用系统 python3，无 venv）
unit = """[Unit]
Description=HookStudio Key Proxy (stdlib)
After=network.target

[Service]
WorkingDirectory=/opt/hsproxy/proxy
EnvironmentFile=/opt/hsproxy/.env
ExecStart=/usr/bin/python3 /opt/hsproxy/proxy/app.py
Restart=always
RestartSec=3
MemoryMax=150M

[Install]
WantedBy=multi-user.target
"""
with sftp.open("/etc/systemd/system/hsproxy.service", "w") as f:
    f.write(unit)
# 为什么用 restart 而不是 enable --now：**升级部署**时服务已在跑旧代码，
# enable --now 对已启动的服务是 no-op，结果就是「部署完成了但线上还是旧版」——
# 2026-11-04 加 /api/version、/api/audit/ingest 时差点踩进去。
run("systemctl daemon-reload && systemctl enable hsproxy && systemctl restart hsproxy",
    "启动/重启服务")
time.sleep(2)
run("systemctl is-active hsproxy", "确认服务活跃")
run("curl -s -m 5 http://127.0.0.1:8000/health; echo", "自测 /health")
run("curl -s -m 5 http://127.0.0.1:8000/api/version; echo", "自测 /api/version（版本闸）")
run("curl -s -m 5 -H 'X-HS-Token: deploy-smoke' -H 'Content-Type: application/json' "
    "-d '{\"device\":\"deploy-smoke\",\"app\":\"deploy\",\"os\":\"linux\",\"items\":[]}' "
    "http://127.0.0.1:8000/api/audit/ingest; echo", "自测 /api/audit/ingest（日志上报）")
run("curl -s -m 5 -H 'X-HS-Token: smoke-test-token' http://127.0.0.1:8000/api/quota; echo",
    "配额接口（应回 401：匿名设备令牌已停用）")
run(f"curl -s -m 5 -H 'X-Admin-Key: {ADMIN}' http://127.0.0.1:8000/admin/stats; echo",
    "管理统计")
run(f"curl -s -m 5 -H 'X-Admin-Key: {ADMIN}' 'http://127.0.0.1:8000/admin/logs?limit=3'; echo",
    "客户端日志查询")
run(f"curl -s -m 5 -H 'X-Admin-Key: {ADMIN}' 'http://127.0.0.1:8000/admin/syslog?limit=3'; echo",
    "系统日志（服务端异常 / 运行事件）")
run("curl -s -m 5 -o /dev/null -w '%{http_code}\\n' http://127.0.0.1:8000/admin",
    "运营后台页面")
run(f"curl -s -m 5 -H 'X-Admin-Key: {ADMIN}' http://127.0.0.1:8000/admin/accounts | head -c 300; echo",
    "账号清单")

print("\n部署完成。ADMIN_KEY（同时保存在 deploy/.env.admin）：", ADMIN)
sftp.close()
cli.close()
