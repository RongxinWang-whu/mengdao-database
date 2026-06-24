#!/usr/bin/env python3
"""
抖音直播自动录制工具
=====================
用法:
    python recorder.py <房间号或URL> [-o 输出文件] [-d 时长(分钟)] [--hls] [--headless]

示例:
    python recorder.py 123456789                    # 录制 FLV 流，手动 Ctrl+C 停止
    python recorder.py 123456789 -d 60              # 录制 60 分钟后自动停止
    python recorder.py 123456789 --hls -o my.mp4    # 优先 HLS 流，指定输出文件名
    python recorder.py https://live.douyin.com/123456789  # 支持完整 URL

依赖安装:
    pip install -r requirements.txt
    playwright install chromium

前置条件:
    系统已安装 ffmpeg 并添加到 PATH
"""

import argparse
import asyncio
import json
import os
import re
import signal
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from playwright.async_api import async_playwright

# ============================================================================
# 常量 & 模式
# ============================================================================

API_PATH_PATTERN = "webcast/room/web/enter/"  # 直播间信息 API 路径

# CDN 流地址特征模式（按优先级排序）
CDN_URL_PATTERNS = [
    "pull-flv-",   # FLV 拉流 CDN
    "pull-hls-",   # HLS 拉流 CDN
    ".flv?",       # FLV 文件（带参数）
    ".m3u8?",      # HLS 文件（带参数）
    "/flv?",       # FLV 路径
    "/m3u8?",      # HLS 路径
]

# 流 URL 正则（宽松匹配）
STREAM_URL_REGEX = re.compile(
    r"https?://[^\s\"'<>]+\.(?:flv|m3u8|ts)[^\s\"'<>]*",
    re.IGNORECASE,
)

# 房间号提取正则
ROOM_ID_REGEX = re.compile(r"live\.douyin\.com/(\d+)")

# 用户数据目录（持久化 cookie，避免频繁登录）
USER_DATA_DIR = Path(__file__).parent / ".browser-data"

# 关注列表文件
WATCHLIST_FILE = Path(__file__).parent / "watchlist.json"

# 监控检测间隔（秒）
WATCH_CHECK_INTERVAL = 60


# ============================================================================
# 关注列表管理
# ============================================================================


def load_watchlist() -> dict:
    """加载关注列表。"""
    if WATCHLIST_FILE.exists():
        try:
            with open(WATCHLIST_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except (json.JSONDecodeError, IOError):
            pass
    return {"streamers": [], "settings": {"check_interval": 60}}


def save_watchlist(data: dict) -> None:
    """保存关注列表。"""
    WATCHLIST_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(WATCHLIST_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def add_to_watchlist(url_or_id: str) -> bool:
    """添加主播到关注列表。返回 True=新增, False=已存在。"""
    room_id = extract_room_id(url_or_id)
    wl = load_watchlist()
    for s in wl["streamers"]:
        if s["id"] == room_id:
            return False
    wl["streamers"].append({
        "id": room_id,
        "url": f"https://live.douyin.com/{room_id}",
        "added": datetime.now().strftime("%Y-%m-%d %H:%M"),
    })
    save_watchlist(wl)
    return True


def remove_from_watchlist(room_id: str) -> bool:
    """从关注列表移除主播。"""
    room_id = extract_room_id(room_id)
    wl = load_watchlist()
    before = len(wl["streamers"])
    wl["streamers"] = [s for s in wl["streamers"] if s["id"] != room_id]
    if len(wl["streamers"]) < before:
        save_watchlist(wl)
        return True
    return False


def list_watchlist() -> list[dict]:
    """列出所有关注的主播。"""
    return load_watchlist()["streamers"]


# ============================================================================
# 工具函数
# ============================================================================


def extract_room_id(input_str: str) -> str:
    """从用户输入中提取抖音直播间房间号。

    支持:
        - 纯数字房间号: "123456789"
        - 完整 URL: "https://live.douyin.com/123456789"
        - 带路径 URL: "https://live.douyin.com/123456789?xxx"
    """
    match = ROOM_ID_REGEX.search(input_str)
    if match:
        return match.group(1)
    if input_str.isdigit():
        return input_str
    raise ValueError(
        f"无法从输入 '{input_str}' 中提取房间号。"
        f"请使用格式: 123456789 或 https://live.douyin.com/123456789"
    )


def check_ffmpeg() -> None:
    """检查 ffmpeg 是否在系统 PATH 中可用。"""
    try:
        subprocess.run(
            ["ffmpeg", "-version"],
            capture_output=True,
            check=True,
            creationflags=(
                subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0
            ),
        )
    except (subprocess.CalledProcessError, FileNotFoundError):
        print("[ERROR] 未找到 ffmpeg，请先安装 ffmpeg 并添加到系统 PATH")
        print("   下载地址: https://ffmpeg.org/download.html")
        print("   Windows 用户建议: winget install ffmpeg  或  choco install ffmpeg")
        sys.exit(1)


def now_str() -> str:
    """返回当前时间字符串，用于默认文件名。"""
    return datetime.now().strftime("%Y%m%d_%H%M%S")


def extract_stream_urls_from_json(obj, urls: list | None = None) -> list[str]:
    """递归遍历 JSON 对象，提取所有看起来像流地址的字符串。

    处理两层 JSON 嵌套（Douyin API 的 stream_data 字段是 JSON 字符串）。
    """
    if urls is None:
        urls = []

    if isinstance(obj, dict):
        for key, value in obj.items():
            # 某些字段的值本身就是 JSON 字符串（如 stream_data）
            if isinstance(value, str) and value.strip().startswith("{"):
                try:
                    inner = json.loads(value)
                    extract_stream_urls_from_json(inner, urls)
                except (json.JSONDecodeError, TypeError):
                    pass
            else:
                extract_stream_urls_from_json(value, urls)
    elif isinstance(obj, list):
        for item in obj:
            extract_stream_urls_from_json(item, urls)
    elif isinstance(obj, str):
        for match in STREAM_URL_REGEX.finditer(obj):
            u = match.group(0)
            # 清理可能的尾部标点
            u = u.rstrip(".,;:'\"")
            if u not in urls:
                urls.append(u)

    return urls


def pick_best_url(urls: list[str], prefer_hls: bool = False) -> str | None:
    """从候选流 URL 列表中选择画质最优的。

    优先级: FULL_HD > HD > SD > 其他
    若 prefer_hls=True，优先返回 HLS 流，否则优先 FLV。
    """
    if not urls:
        return None

    # 去重保序
    seen = set()
    unique = []
    for u in urls:
        if u not in seen:
            seen.add(u)
            unique.append(u)

    # 画质排序
    quality_keywords = [
        "full_hd", "fullhd", "uhd", "4k",
        "hd2", "hd1", "hd",
        "sd2", "sd1", "sd",
        "origin", "source",
    ]

    def quality_rank(url: str) -> int:
        url_lower = url.lower()
        for i, kw in enumerate(quality_keywords):
            if kw in url_lower:
                return i
        return len(quality_keywords)

    unique.sort(key=quality_rank)

    # 按类型偏好筛选
    if prefer_hls:
        hls = [u for u in unique if ".m3u8" in u.lower()]
        if hls:
            return hls[0]

    flv = [u for u in unique if ".flv" in u.lower() or "flv" in u.lower()]
    if flv:
        return flv[0]

    return unique[0]


def matches_cdn_pattern(url: str) -> bool:
    """检查 URL 是否匹配 CDN 流地址特征。"""
    url_lower = url.lower()
    return any(p.lower() in url_lower for p in CDN_URL_PATTERNS)


# ============================================================================
# 核心逻辑
# ============================================================================


async def capture_stream_url(
    page,
    room_id: str,
    prefer_hls: bool = False,
    verbose: bool = False,
    poll_interval: int = 10,
) -> str | None:
    """打开抖音直播间页面，通过拦截网络请求获取拉流地址。

    双路径策略:
        Path A (主): 拦截 webcast/room/web/enter/ API 响应，解析 JSON 提取流 URLs
        Path B (备): 直接匹配 CDN 响应 URL (pull-flv, pull-hls, .flv, .m3u8)

    如果直播间未开播，每 poll_interval 秒刷新等待。
    """
    stream_url: str | None = None
    found_event = asyncio.Event()

    async def on_response(response):
        nonlocal stream_url
        if found_event.is_set():
            return

        url = response.url

        # ── Path A: API 响应 ──
        if API_PATH_PATTERN in url:
            try:
                body = await response.text()
                if verbose:
                    print(f"[verbose] 拦截到 API: {url[:80]}...")
                data = json.loads(body)
                urls = extract_stream_urls_from_json(data)
                if urls and verbose:
                    print(f"[verbose] 从 API 提取到 {len(urls)} 个流地址候选")
                filtered = [u for u in urls if matches_cdn_pattern(u)]
                if not filtered:
                    filtered = urls  # 如果模式匹配没命中，全部保留
                if filtered:
                    stream_url = pick_best_url(filtered, prefer_hls)
                    if stream_url:
                        found_event.set()
                        return
            except Exception as e:
                if verbose:
                    print(f"[verbose] API 解析失败: {e}")

        # ── Path B: CDN 直接匹配 ──
        if matches_cdn_pattern(url):
            if verbose:
                print(f"[verbose] CDN 匹配: {url[:120]}...")
            # 取第一个匹配的 CDN URL 作为备选
            if not stream_url:
                stream_url = url
                found_event.set()

    page.on("response", on_response)

    live_url = f"https://live.douyin.com/{room_id}"

    # 循环等待，直到找到流地址
    attempt = 0
    while not found_event.is_set():
        attempt += 1
        print(f"[{attempt}] 正在加载直播间 {room_id} ...")

        try:
            await page.goto(live_url, wait_until="domcontentloaded", timeout=30000)
        except Exception as e:
            print(f"  [WARN] 页面加载异常: {e}")

        # 等待一小段时间让 API 请求发出
        try:
            await asyncio.wait_for(found_event.wait(), timeout=15)
        except asyncio.TimeoutError:
            pass

        if found_event.is_set():
            break

        print(f"  [WAIT] 未检测到直播流，{poll_interval} 秒后重试... (Ctrl+C 取消)")
        try:
            await asyncio.sleep(poll_interval)
        except asyncio.CancelledError:
            break

    page.remove_listener("response", on_response)
    return stream_url


async def record_stream(
    stream_url: str,
    output_path: str,
    duration_minutes: float | None = None,
    until_offline: bool = False,
) -> None:
    """使用 ffmpeg 下载并录制直播流。

    - 使用 -c copy 流拷贝模式，零画质损失
    - 自动重连断开的流
    - FLV 流存为 .flv（不怕中断，即使杀进程也能播放）
    - HLS 流存为 .mp4
    - 支持设置录制时长上限
    - until_offline 模式：检测到下播自动停止
    """
    # 根据流类型确定输出格式
    is_hls = ".m3u8" in stream_url.lower()
    is_flv = ".flv" in stream_url.lower() or "/flv" in stream_url.lower()

    if is_flv:
        fmt = "flv"
        output_path = re.sub(r"\.mp4$", ".flv", output_path)
    elif is_hls:
        fmt = "mp4"
    else:
        fmt = "mp4"

    ffmpeg_cmd = [
        "ffmpeg",
        "-reconnect", "1",
        "-reconnect_streamed", "1",
        "-reconnect_delay_max", "10",
        "-i", stream_url,
        "-c", "copy",           # 流拷贝，不重编码
        "-f", fmt,
        "-y",                    # 覆盖已有文件
    ]

    # HLS 流追加参数
    if is_hls:
        ffmpeg_cmd[1:1] = [
            "-reconnect_at_eof", "1",
            "-reconnect_on_network_error", "1",
            "-rw_timeout", "10000000",
        ]

    # 下播自动停模式：减少超时等待
    if until_offline:
        ffmpeg_cmd[1:1] = [
            "-timeout", "15000000",       # socket 超时 15 秒
            "-reconnect_on_http_error", "1",
        ]

    ffmpeg_cmd.append(output_path)

    mode_label = "下播自动停" if until_offline else "手动停止"
    print(f"\n[REC] 开始录制 → {output_path}")
    print(f"   流地址: {stream_url[:100]}...")
    print(f"   模式: {mode_label}  |  按 Ctrl+C 停止录制\n")

    creationflags = subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0

    proc = await asyncio.create_subprocess_exec(
        *ffmpeg_cmd,
        stdin=subprocess.PIPE,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        creationflags=creationflags,
    )

    start_time = time.time()
    last_status_time = start_time

    # 下播检测关键词
    STREAM_END_PATTERNS = [
        "404 Not Found",
        "Server returned 4",
        "Server returned 5",
        "403 Forbidden",
    ]
    stream_ended = False

    async def read_output():
        """读取 ffmpeg 输出并选择性打印进度 / 检测下播。"""
        nonlocal last_status_time, stream_ended
        while True:
            line = await proc.stdout.readline()
            if not line:
                break
            text = line.decode("utf-8", errors="replace").strip()
            now = time.time()
            if any(kw in text for kw in ["frame=", "time=", "speed="]):
                if now - last_status_time >= 2:
                    parts = [p.strip() for p in text.split() if "=" in p]
                    short = "  ".join(p for p in parts if p.startswith(("time=", "speed=", "bitrate=")))
                    if short:
                        print(f"  [{short}]", end="\r")
                    last_status_time = now
            elif any(kw in text for kw in ["error", "Error", "fail", "Invalid"]):
                print(f"  [WARN] {text}")
                if until_offline:
                    for pat in STREAM_END_PATTERNS:
                        if pat in text:
                            stream_ended = True
                            break

    output_task = asyncio.create_task(read_output())

    try:
        while True:
            await asyncio.sleep(1)

            # 检测到下播
            if stream_ended:
                print("\n[INFO] 检测到直播已结束，正在停止录制...")
                break

            # 检查 ffmpeg 是否已退出
            if proc.returncode is not None:
                code = proc.returncode
                if until_offline or code != 0:
                    print(f"\n  ffmpeg 已退出 (code={code})，直播可能已结束")
                break

            # 检查时长限制
            if duration_minutes:
                elapsed_min = (time.time() - start_time) / 60
                if elapsed_min >= duration_minutes:
                    print(f"\n[TIME] 已达到录制时长上限 {duration_minutes} 分钟，正在停止...")
                    break

    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[STOP] 收到停止信号，正在优雅结束录制（等待 ffmpeg 封包）...")
    finally:
        # ── 优雅停止 ffmpeg ──
        if proc.returncode is None:
            try:
                # 发送 'q' 命令让 ffmpeg 优雅退出（写完 moov atom）
                proc.stdin.write(b"q")
                await proc.stdin.drain()
                # 等待最多 30 秒
                await asyncio.wait_for(proc.wait(), timeout=30)
            except (asyncio.TimeoutError, ProcessLookupError, BrokenPipeError):
                print("  ffmpeg 未在 30 秒内退出，强制终止...")
                try:
                    proc.kill()
                except ProcessLookupError:
                    pass
                await proc.wait()

        # 等待输出读取完成
        output_task.cancel()
        try:
            await output_task
        except asyncio.CancelledError:
            pass

        elapsed = time.time() - start_time
        size_mb = 0
        if os.path.exists(output_path):
            size_mb = os.path.getsize(output_path) / (1024 * 1024)

        print(f"\n[OK] 录制完成")
        print(f"   文件: {output_path}")
        print(f"   时长: {elapsed / 60:.1f} 分钟")
        print(f"   大小: {size_mb:.1f} MB")

        final_path = output_path  # 默认为原始输出

        # 自动转 MP4
        if output_path.endswith(".flv"):
            mp4_path = output_path[:-4] + ".mp4"
            print(f"   正在自动转换为 MP4...")
            conv_cmd = [
                "ffmpeg", "-i", output_path, "-c", "copy", mp4_path, "-y",
            ]
            conv_proc = await asyncio.create_subprocess_exec(
                *conv_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                creationflags=creationflags,
            )
            await conv_proc.wait()
            if conv_proc.returncode == 0 and os.path.exists(mp4_path):
                os.remove(output_path)  # 删除临时 flv
                mp4_size = os.path.getsize(mp4_path) / (1024 * 1024)
                print(f"   MP4 文件: {mp4_path}")
                print(f"   MP4 大小: {mp4_size:.1f} MB")
                final_path = mp4_path

        # 保存录制元数据
        save_recording_metadata(final_path, elapsed, stream_url)


# ============================================================================
# 录制元数据管理
# ============================================================================

RECORDING_LOG_FILE = Path(__file__).parent / "recording_log.json"
RECORDING_JS_FILE = Path(__file__).parent / "recording_data.js"


def save_recording_metadata(filepath: str, duration_sec: float, stream_url: str = "") -> None:
    """保存录制记录到日志文件，并同步生成 JS 数据文件供网页读取。"""
    filename = os.path.basename(filepath)
    # 从文件名提取房间号: douyin_live_388670993636_20260623_191018
    room_match = re.search(r"douyin_live_(\d+)_(\d{8}_\d{6})", filename)
    room_id = room_match.group(1) if room_match else "unknown"
    timestamp_str = room_match.group(2) if room_match else now_str()
    # 解析时间戳: 20260623_191018 → 2026-06-23 19:10:18
    try:
        dt = datetime.strptime(timestamp_str, "%Y%m%d_%H%M%S")
        date_str = dt.strftime("%Y-%m-%d")
        time_str = dt.strftime("%H:%M:%S")
    except ValueError:
        date_str = datetime.now().strftime("%Y-%m-%d")
        time_str = datetime.now().strftime("%H:%M:%S")

    size_mb = os.path.getsize(filepath) / (1024 * 1024) if os.path.exists(filepath) else 0
    duration_min = duration_sec / 60

    entry = {
        "date": date_str,
        "start_time": time_str,
        "duration_min": round(duration_min, 1),
        "file": filename,
        "file_size_mb": round(size_mb, 1),
        "room_id": room_id,
        "stream_url": stream_url[:80] if stream_url else "",
    }

    # 更新 JSON 日志
    log = []
    if RECORDING_LOG_FILE.exists():
        try:
            with open(RECORDING_LOG_FILE, "r", encoding="utf-8") as f:
                log = json.load(f)
        except (json.JSONDecodeError, IOError):
            log = []
    log.append(entry)
    with open(RECORDING_LOG_FILE, "w", encoding="utf-8") as f:
        json.dump(log, f, ensure_ascii=False, indent=2)

    # 生成 JS 数据文件（供 HTML 网页直接引用）
    with open(RECORDING_JS_FILE, "w", encoding="utf-8") as f:
        f.write("// 自动生成，请勿手动编辑\n")
        f.write("// 由 recorder.py 在每次录制完成后自动更新\n")
        f.write(f"// 最后更新: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write("window.__RECORDING_DATA__ = ")
        f.write(json.dumps(log, ensure_ascii=False, indent=2))
        f.write(";\n")
    print(f"   [INFO] 录制记录已同步到数据库")


# ============================================================================
# 监控模式（开播自动录）
# ============================================================================


async def capture_once(
    page,
    room_id: str,
    prefer_hls: bool = False,
    timeout: int = 20,
    verbose: bool = False,
) -> str | None:
    """单次尝试获取流地址（不循环等待），用于监控模式。"""
    stream_url: str | None = None
    found_event = asyncio.Event()

    async def on_response(response):
        nonlocal stream_url
        if found_event.is_set():
            return
        url = response.url
        if API_PATH_PATTERN in url:
            try:
                body = await response.text()
                data = json.loads(body)
                urls = extract_stream_urls_from_json(data)
                filtered = [u for u in urls if matches_cdn_pattern(u)]
                if not filtered:
                    filtered = urls
                if filtered:
                    stream_url = pick_best_url(filtered, prefer_hls)
                    if stream_url:
                        found_event.set()
                        return
            except Exception:
                pass
        if matches_cdn_pattern(url):
            if not stream_url:
                stream_url = url
                found_event.set()

    page.on("response", on_response)

    live_url = f"https://live.douyin.com/{room_id}"
    try:
        await page.goto(live_url, wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass

    try:
        await asyncio.wait_for(found_event.wait(), timeout=timeout)
    except asyncio.TimeoutError:
        pass

    page.remove_listener("response", on_response)
    return stream_url


async def watch_streamers(
    context,
    prefer_hls: bool = False,
    check_interval: int = 60,
    verbose: bool = False,
) -> None:
    """持续监控关注列表中的主播，开播自动录制。

    对每个关注的主播循环检查；一旦检测到开播就录制到下播，
    然后回到监控状态。Ctrl+C 退出整个监控。
    """
    streamers = list_watchlist()
    if not streamers:
        print("[ERROR] 关注列表为空，请先用 --add 添加主播")
        return

    # 显示监控目标
    print("=" * 60)
    print("  抖音直播自动监控模式")
    print("=" * 60)
    for i, s in enumerate(streamers, 1):
        print(f"  [{i}] 房间 {s['id']}  |  添加于 {s.get('added', '未知')}")
    print(f"  检测间隔: {check_interval} 秒  |  开播后自动录制到下播")
    print("=" * 60)
    print()

    page = context.pages[0] if context.pages else await context.new_page()

    # 记录每个房间的连续未开播次数（减少刷屏）
    offline_count = {s["id"]: 0 for s in streamers}

    try:
        while True:
            for s in streamers:
                room_id = s["id"]
                print(f"[{datetime.now().strftime('%H:%M:%S')}] 检查房间 {room_id} ...", end=" ")

                stream_url = await capture_once(
                    page, room_id, prefer_hls=prefer_hls, verbose=verbose
                )

                if stream_url:
                    offline_count[room_id] = 0
                    print("开播了！开始录制...")
                    output = f"douyin_live_{room_id}_{now_str()}.mp4"
                    await record_stream(
                        stream_url,
                        output,
                        until_offline=True,
                    )
                    print(f"[INFO] 录制结束，继续监控...\n")
                    await asyncio.sleep(30)  # 下播后冷却 30 秒
                else:
                    offline_count[room_id] += 1
                    # 仅每 10 次或第一次打印，减少刷屏
                    if offline_count[room_id] <= 1 or offline_count[room_id] % 10 == 0:
                        print(f"未开播")
                    else:
                        print(f".")

            await asyncio.sleep(check_interval)

    except (KeyboardInterrupt, asyncio.CancelledError):
        print("\n[STOPPED] 监控已停止")


# ============================================================================
# CLI & Main
# ============================================================================


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="抖音直播自动录制工具 - 自动抓取纯净流地址并用 ffmpeg 录制",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
示例:
    python recorder.py 123456789                  # 录制，Ctrl+C 停止
    python recorder.py 123456789 -d 60            # 录制 60 分钟
    python recorder.py 123456789 --until-offline  # 下播自动停
    python recorder.py --add https://live.douyin.com/123456789  # 添加关注
    python recorder.py --list                     # 查看关注列表
    python recorder.py --watch                    # 监控所有关注，开播自动录
        """,
    )
    parser.add_argument(
        "room",
        nargs="?",
        default=None,
        help="抖音直播间房间号或 URL（监控模式可不填）",
    )
    parser.add_argument(
        "-o", "--output",
        default=None,
        help="输出文件路径（默认: douyin_live_YYYYMMDD_HHMMSS.mp4）",
    )
    parser.add_argument(
        "-d", "--duration",
        type=float,
        default=None,
        help="录制时长上限（分钟），不指定则手动 Ctrl+C 停止",
    )
    parser.add_argument(
        "--hls",
        action="store_true",
        help="优先使用 HLS (m3u8) 流，默认优先 FLV",
    )
    parser.add_argument(
        "--headless",
        action="store_true",
        help="无头模式（不显示浏览器窗口），不推荐",
    )
    parser.add_argument(
        "--until-offline",
        action="store_true",
        help="自动检测下播并停止录制",
    )

    # ── 关注列表管理 ──
    parser.add_argument(
        "--add",
        metavar="URL_OR_ID",
        default=None,
        help="添加主播到关注列表",
    )
    parser.add_argument(
        "--remove",
        metavar="URL_OR_ID",
        default=None,
        help="从关注列表移除主播",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        help="查看关注列表",
    )
    parser.add_argument(
        "--watch",
        action="store_true",
        help="持续监控关注列表，开播自动录制（Ctrl+C 停止）",
    )
    parser.add_argument(
        "--interval",
        type=int,
        default=60,
        help="监控检测间隔秒数（默认 60）",
    )
    parser.add_argument(
        "--verbose",
        action="store_true",
        help="输出调试信息",
    )
    return parser.parse_args()


async def main() -> None:
    args = parse_args()

    # ── 关注列表管理（不需要 ffmpeg / 浏览器）──
    if args.add:
        ok = add_to_watchlist(args.add)
        if ok:
            print(f"[OK] 已添加 {extract_room_id(args.add)} 到关注列表")
        else:
            print(f"[INFO] {extract_room_id(args.add)} 已在关注列表中")
        return

    if args.remove:
        ok = remove_from_watchlist(args.remove)
        if ok:
            print(f"[OK] 已从关注列表移除 {extract_room_id(args.remove)}")
        else:
            print(f"[INFO] {extract_room_id(args.remove)} 不在关注列表中")
        return

    if args.list:
        streamers = list_watchlist()
        if not streamers:
            print("关注列表为空。用 --add <URL> 添加主播")
        else:
            print("=" * 40)
            print("  关注列表")
            print("=" * 40)
            for i, s in enumerate(streamers, 1):
                print(f"  [{i}] 房间 {s['id']}  |  {s.get('added', '未知')}")
            print("=" * 40)
            print(f"  共 {len(streamers)} 个主播")
            print(f"  运行: python recorder.py --watch  开始自动监控")
        return

    # ── 监控模式 ──
    if args.watch:
        check_ffmpeg()
        USER_DATA_DIR.mkdir(parents=True, exist_ok=True)
        async with async_playwright() as p:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(USER_DATA_DIR),
                headless=args.headless,
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                locale="zh-CN",
                args=["--disable-blink-features=AutomationControlled"],
            )
            await watch_streamers(
                context,
                prefer_hls=args.hls,
                check_interval=args.interval,
                verbose=args.verbose,
            )
            await context.close()
        return

    # ── 单次录制模式（需要 room 参数）──
    if not args.room:
        print("[ERROR] 请提供房间号/URL，或使用 --add / --list / --watch")
        print("  示例: python recorder.py 123456789")
        sys.exit(1)

    check_ffmpeg()
    room_id = extract_room_id(args.room)

    if args.output is None:
        args.output = f"douyin_live_{room_id}_{now_str()}.mp4"

    print("=" * 60)
    print("  抖音直播自动录制工具")
    print("=" * 60)
    print(f"  房间号:   {room_id}")
    print(f"  直播间:   https://live.douyin.com/{room_id}")
    print(f"  输出文件: {args.output}")
    print(f"  流类型:   {'HLS 优先' if args.hls else 'FLV 优先'}")
    mode = "下播自动停" if args.until_offline else ("定时" if args.duration else "手动停止")
    print(f"  录制时长: {f'{args.duration} 分钟' if args.duration else mode}")
    print(f"  浏览器:   {'无头模式' if args.headless else '可见模式'}")
    print("=" * 60)

    USER_DATA_DIR.mkdir(parents=True, exist_ok=True)

    async with async_playwright() as p:
        try:
            context = await p.chromium.launch_persistent_context(
                user_data_dir=str(USER_DATA_DIR),
                headless=args.headless,
                viewport={"width": 1920, "height": 1080},
                user_agent=(
                    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                    "AppleWebKit/537.36 (KHTML, like Gecko) "
                    "Chrome/125.0.0.0 Safari/537.36"
                ),
                locale="zh-CN",
                args=["--disable-blink-features=AutomationControlled"],
            )
        except Exception as e:
            msg = str(e)
            if "Executable doesn't exist" in msg or "not found" in msg.lower():
                print("[ERROR] 浏览器未安装，请运行: playwright install chromium")
                sys.exit(1)
            raise

        page = context.pages[0] if context.pages else await context.new_page()

        try:
            stream_url = await capture_stream_url(
                page, room_id,
                prefer_hls=args.hls,
                verbose=args.verbose,
            )

            if not stream_url:
                print("\n[ERROR] 未能获取直播流地址。可能原因:")
                print("   1. 直播间未开播")
                print("   2. 需要登录才能观看")
                print("   3. 房间号错误")
                await context.close()
                sys.exit(1)

            print(f"\n[OK] 已获取流地址")
            if args.verbose:
                print(f"   {stream_url}")

            await context.close()

            await record_stream(
                stream_url, args.output,
                duration_minutes=args.duration,
                until_offline=args.until_offline,
            )

        except KeyboardInterrupt:
            print("\n[STOPPED] 已取消")
        except Exception as e:
            print(f"\n[ERROR] 发生错误: {e}")
            if args.verbose:
                import traceback
                traceback.print_exc()
            sys.exit(1)
        finally:
            try:
                await context.close()
            except Exception:
                pass


if __name__ == "__main__":
    # Windows 下 Ctrl+C 处理优化
    if sys.platform == "win32":
        signal.signal(signal.SIGINT, lambda s, f: sys.exit(0))

    asyncio.run(main())
