#!/usr/bin/env python3
"""
GitHub Pages 自动部署脚本
每次录制完成后自动推送到 GitHub Pages

用法:
    python deploy.py           # 提交并推送
    python deploy.py --setup   # 首次设置远程仓库
    python deploy.py --status  # 查看状态
"""

import os
import subprocess
import sys
from pathlib import Path

DIR = Path(__file__).parent
REMOTE_NAME = "origin"
BRANCH = "main"


def run(cmd: str) -> tuple[int, str]:
    """执行 shell 命令。"""
    result = subprocess.run(
        cmd, shell=True, cwd=str(DIR),
        capture_output=True, text=True,
    )
    return result.returncode, (result.stdout + result.stderr).strip()


def setup_remote(repo_url: str) -> bool:
    """设置 GitHub 远程仓库地址。"""
    code, _ = run(f"git remote get-url {REMOTE_NAME}")
    if code == 0:
        print(f"远程仓库已存在，如需更改请手动执行:")
        print(f"  git remote set-url origin {repo_url}")
        return False

    code, out = run(f"git remote add {REMOTE_NAME} {repo_url}")
    if code == 0:
        print(f"[OK] 远程仓库已设置: {repo_url}")
        return True
    else:
        print(f"[ERROR] 设置失败: {out}")
        return False


def deploy(message: str | None = None) -> bool:
    """提交所有更改并推送到 GitHub Pages。"""
    if message is None:
        from datetime import datetime
        message = f"Update: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}"

    # 1. 检查远程仓库
    code, _ = run(f"git remote get-url {REMOTE_NAME}")
    if code != 0:
        print("[ERROR] 未设置远程仓库，请先执行: python deploy.py --setup <GitHub仓库URL>")
        return False

    # 2. 添加所有更改
    code, out = run("git add -A")
    if code != 0:
        print(f"[ERROR] git add 失败: {out}")
        return False

    # 3. 检查是否有更改
    code, out = run("git diff --cached --quiet")
    if code == 0:
        print("[INFO] 没有需要部署的更改")
        return True

    # 4. 提交
    code, out = run(f'git commit -m "{message}"')
    if code != 0 and "nothing to commit" not in out:
        print(f"[ERROR] git commit 失败: {out}")
        return False

    # 5. 推送
    print(f"[INFO] 正在推送到 GitHub Pages ...")
    code, out = run(f"git push {REMOTE_NAME} {BRANCH}")
    if code == 0:
        print(f"[OK] 部署成功！网站将在几秒后更新")
        return True
    else:
        print(f"[ERROR] 推送失败: {out}")
        print(f"[INFO] 可能需要先执行: git push -u {REMOTE_NAME} {BRANCH}")
        return False


def show_status():
    """显示 git 状态。"""
    code, remote = run(f"git remote get-url {REMOTE_NAME}")
    remote = remote if code == 0 else "未设置"

    code, status = run("git status --short")
    changed = len([l for l in status.split("\n") if l.strip()]) if status else 0

    code, log = run("git log --oneline -3")

    print("=" * 50)
    print("  GitHub Pages 部署状态")
    print("=" * 50)
    print(f"  远程仓库: {remote}")
    print(f"  待提交文件: {changed} 个")
    print(f"  最近提交:")
    for line in log.split("\n")[:3]:
        if line.strip():
            print(f"    {line.strip()}")
    print("=" * 50)


if __name__ == "__main__":
    if len(sys.argv) > 1 and sys.argv[1] == "--setup":
        if len(sys.argv) > 2:
            setup_remote(sys.argv[2])
            print("\n下一步:")
            print("  1. 在 GitHub 上进入仓库 Settings → Pages")
            print("  2. Source 选 'Deploy from a branch'")
            print("  3. Branch 选 'main'，点 Save")
            print("  4. 运行: python deploy.py  首次部署")
        else:
            print("用法: python deploy.py --setup <GitHub仓库URL>")
            print("示例: python deploy.py --setup https://github.com/用户名/mengdao-database.git")
    elif len(sys.argv) > 1 and sys.argv[1] == "--status":
        show_status()
    else:
        deploy()
