#!/usr/bin/env python3
"""
萌刀数据汇总库 - 本地 Web 服务器
===============================

用法:
    python serve.py                 # 仅局域网可访问
    python serve.py --public        # 生成公网链接，外网可访问（免费，无需注册）
    python serve.py --open          # 启动并自动打开浏览器
    python serve.py --port 8080     # 指定端口
"""

import http.server
import os
import socketserver
import subprocess
import sys
import threading
import time
import webbrowser
from pathlib import Path

PORT = 8765
DIR = Path(__file__).parent

# 免费 SSH 隧道服务（无需注册，任选一个）
TUNNEL_SERVICES = [
    {
        "name": "serveo.net",
        "cmd": f"ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -R 80:localhost:{PORT} serveo.net",
        "url_pattern": r"Forwarding HTTP traffic from (https?://[^\s]+)",
    },
    {
        "name": "localhost.run",
        "cmd": f"ssh -o StrictHostKeyChecking=no -o ServerAliveInterval=30 -R 80:localhost:{PORT} nokey@localhost.run",
        "url_pattern": r"(https?://[^\s]+\.lhr\.life[^\s]*)",
    },
]


class Handler(http.server.SimpleHTTPRequestHandler):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, directory=str(DIR), **kwargs)

    def log_message(self, format, *args):
        if "/recording_data.js" not in str(args[0]):
            print(f"  [{self.log_date_time_string()}] {args[0]}")


def start_tunnel(port: int) -> str | None:
    """尝试启动免费 SSH 隧道，返回公网 URL。"""
    for svc in TUNNEL_SERVICES:
        print(f"  正在尝试 {svc['name']} ...")
        try:
            proc = subprocess.Popen(
                svc["cmd"],
                shell=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
            )
            # 等待隧道 URL 出现
            deadline = time.time() + 20
            public_url = None
            while time.time() < deadline:
                line = proc.stdout.readline()
                if not line:
                    break
                line = line.strip()
                if line and ("Forwarding" in line or "https://" in line or "http://" in line):
                    import re
                    match = re.search(svc["url_pattern"], line)
                    if match:
                        public_url = match.group(1)
                        break
                if "failed" in line.lower() or "error" in line.lower():
                    print(f"    {svc['name']}: {line[:120]}")
            if public_url:
                print(f"  [OK] {svc['name']} 隧道已建立")
                return public_url
            else:
                print(f"    {svc['name']} 连接失败，尝试下一个...")
                proc.terminate()
        except FileNotFoundError:
            print(f"    SSH 客户端未找到，跳过 {svc['name']}")
        except Exception as e:
            print(f"    {svc['name']} 错误: {e}")
    return None


def main():
    port = PORT
    auto_open = False
    public_mode = False

    args = sys.argv[1:]
    i = 0
    while i < len(args):
        if args[i] == "--port" and i + 1 < len(args):
            port = int(args[i + 1])
            i += 2
        elif args[i] == "--open":
            auto_open = True
            i += 1
        elif args[i] == "--public":
            public_mode = True
            i += 1
        else:
            i += 1

    local_url = f"http://localhost:{port}"

    print("=" * 55)
    print("  萌刀数据汇总库 - Web 服务器")
    print("=" * 55)
    print(f"  本地地址: {local_url}")
    print(f"  文件目录: {DIR}")
    print("=" * 55)

    # 公网模式
    public_url = None
    if public_mode:
        print()
        print("  正在建立公网隧道（免费，无需注册）...")
        public_url = start_tunnel(port)
        print("=" * 55)
        if public_url:
            print(f"  >> 公网地址: {public_url}")
            print(f"  >> 任何设备浏览器打开即可访问")
        else:
            print("  [FAIL] 隧道建立失败")
            print("  可能是 SSH 不可用，请安装 Git Bash 或 OpenSSH")
            print("  备选: 用 ngrok / frp / natapp 等隧道工具")
        print("=" * 55)

    print(f"\n  按 Ctrl+C 停止服务器\n")

    if auto_open:
        url_to_open = public_url or local_url
        webbrowser.open(url_to_open)

    with socketserver.TCPServer(("", port), Handler) as httpd:
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n服务器已停止")


if __name__ == "__main__":
    main()
