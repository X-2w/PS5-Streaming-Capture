"""全局配置常量"""

import os
import shutil
import subprocess
import sys
import tempfile


def _get_base_dir():
    """项目根目录：开发时为 streaming/，打包后为 exe 所在目录"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(sys.executable)
    return os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def _get_nginx_dir():
    """nginx 运行目录：开发时为 rtmp_server/nginx，打包后解压到 %TEMP% 并缓存复用"""
    if getattr(sys, "frozen", False):
        temp_dir = os.path.join(tempfile.gettempdir(), "ps5_nginx")
        nginx_exe = os.path.join(temp_dir, "nginx.exe")
        if not os.path.exists(nginx_exe):
            # 首次运行：从 MEIPASS 解压
            meipass_nginx = os.path.join(sys._MEIPASS, "nginx")
            shutil.copytree(meipass_nginx, temp_dir)
        # 每次启动都补齐 nginx 需要的 temp 子目录（copytree 可能跳过空目录，
        # 且上次崩溃留下的缓存可能缺少这些目录）
        for sub in ["client_body_temp", "proxy_temp", "fastcgi_temp", "uwsgi_temp", "scgi_temp"]:
            os.makedirs(os.path.join(temp_dir, "temp", sub), exist_ok=True)
        return temp_dir
    return os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "rtmp_server", "nginx")


def _get_default_gateway() -> str:
    """自动获取本机默认网关 IP。先解析 route print 0.0.0.0，再尝试 ipconfig，失败回退 192.168.1.1"""
    try:
        output = subprocess.run(
            ["route", "print", "0.0.0.0"],
            capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for line in output.stdout.splitlines():
            stripped = line.strip()
            if stripped.startswith("0.0.0.0"):
                parts = stripped.split()
                if len(parts) >= 3:
                    return parts[2]
    except Exception:
        pass

    try:
        output = subprocess.run(
            ["ipconfig"],
            capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        for line in output.stdout.splitlines():
            stripped = line.strip()
            if "Default Gateway" in stripped or "默认网关" in stripped:
                parts = stripped.split(":")
                if len(parts) >= 2:
                    gw = parts[-1].strip()
                    if gw and gw != "0.0.0.0":
                        return gw
    except Exception:
        pass

    return "192.168.1.1"


BASE_DIR = _get_base_dir()

# RTMP 服务
RTMP_PORT = 1935
RTMP_APP = "app"
# stream_key 不再硬编码，由 PS5 推流时动态获取
NGINX_DIR = _get_nginx_dir()
NGINX_EXE = os.path.join(NGINX_DIR, "nginx.exe")
NGINX_CONF = os.path.join(NGINX_DIR, "conf", "nginx.conf")

# nginx-rtmp HTTP 统计接口（用于获取 PS5 推流的实际 stream key）
RTMP_STAT_PORT = 8080
RTMP_STAT_URL = f"http://127.0.0.1:{RTMP_STAT_PORT}/stat"

# DNS 劫持
DNS_PORT = 53
DNS_UPSTREAM = _get_default_gateway()  # 自动检测默认网关作为上游 DNS
DNS_UPSTREAM_BACKUP = "114.114.114.114"  # 公共 DNS 兜底
DNS_TTL = 1  # 秒，避免缓存污染

# 劫持白名单（Twitch ingest 域名 — 具体的 RTMP 推流服务器，非 API）
HIJACK_DOMAINS = {
    "live.twitch.tv",
}

# 劫持域名后缀匹配（通配模式）
HIJACK_SUFFIXES = [
    ".contribute.live-video.net",
    ".live-video.net",
    "live-",                   # live-{区域}.twitch.tv（PS5 实际查的推流域名）
]

# 排除名单（不劫持，透明转发）
EXCLUDE_DOMAINS = {
    "ingest.twitch.tv",
    "live-api.twitch.tv",
    "irc.twitch.tv",
    "irc.chat.twitch.tv",
}

EXCLUDE_SUFFIXES = [
    ".api.twitch.tv",
]

# 拉流地址 — stream_key 和 IP 由运行时动态替换
def get_pull_url(stream_key: str = "", local_ip: str = "127.0.0.1") -> str:
    """构造拉流地址，stream_key 为空时仅给出 URL 前缀"""
    if stream_key:
        return f"rtmp://{local_ip}:{RTMP_PORT}/{RTMP_APP}/{stream_key}"
    return f"rtmp://{local_ip}:{RTMP_PORT}/{RTMP_APP}/<stream_key>"

# 日志最大行数
LOG_MAX_LINES = 500
