"""RTMP 服务管理 — 通过 subprocess 启停 nginx-rtmp，HTTP 回调精准区分 publish/play

PS5 推流完整握手序列：
1. TCP Connect (先试 RTMPS 443 → RST → 降级 RTMP 1935)
2. RTMP Connect
3. Publish (推流) → on_publish 回调触发
4. Play (回显预览验证) → 必须 ACCEPT，拒绝会导致 PS5 超时
5. 视频数据流

关键：不要做 443→1935 端口转发。nginx 返回 RTMP 字节而非 TLS ServerHello，
会让 PS5 在 443 上反复重试而不降级，同样导致超时。
"""

import os
import socket
import subprocess
import threading
import time
import urllib.request
import urllib.parse
import xml.etree.ElementTree as ET
from http.server import HTTPServer, BaseHTTPRequestHandler

from config import NGINX_EXE, NGINX_DIR, RTMP_PORT, RTMP_APP, RTMP_STAT_URL, get_pull_url

CALLBACK_PORT = 8081


class _CallbackHandler(BaseHTTPRequestHandler):
    """nginx-rtmp notify 回调处理：仅 /publish 触发推流状态"""
    manager_ref = None  # 由外部注入

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        params = urllib.parse.parse_qs(parsed.query)

        if parsed.path == "/publish":
            name = params.get("name", [""])[0]
            if self.manager_ref:
                self.manager_ref._on_publish(name)
        elif parsed.path == "/publish_done":
            if self.manager_ref:
                self.manager_ref._on_publish_done()
        elif parsed.path == "/play":
            name = params.get("name", [""])[0]
            if self.manager_ref:
                self.manager_ref._on_play(name)
        elif parsed.path == "/play_done":
            if self.manager_ref:
                self.manager_ref._on_play_done()

        self.send_response(200)
        self.end_headers()
        self.wfile.write(b"ok")

    def log_message(self, format, *args):
        pass  # 静默 HTTP 日志


class RTMPManager:
    def __init__(self, log_callback=None, local_ip: str = "127.0.0.1", on_stream_change=None):
        self.log = log_callback or (lambda msg: None)
        self.local_ip = local_ip
        self.on_stream_change = on_stream_change  # 推流状态变化时回调 (streaming: bool)
        self.process: subprocess.Popen | None = None
        self.running = False
        self._current_stream_key: str = ""
        self._publishing: bool = False
        self._has_ever_streamed: bool = False
        self._callback_server: HTTPServer | None = None
        self._callback_thread: threading.Thread | None = None

    def _nginx_exists(self) -> bool:
        return os.path.isfile(NGINX_EXE)

    def _kill_orphans(self):
        try:
            result = subprocess.run(
                ["taskkill", "/f", "/im", "nginx.exe"],
                capture_output=True, timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except Exception:
            pass
        time.sleep(1.0)

    def _wait_listening(self, port: int, timeout: float = 5.0) -> bool:
        """等待端口被监听，超时返回 False"""
        deadline = time.time() + timeout
        while time.time() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", port), timeout=1):
                    return True
            except (ConnectionRefusedError, OSError):
                time.sleep(0.3)
        return False

    # ── nginx-rtmp notify 回调 ──

    def _on_publish(self, name: str):
        if name:
            self._current_stream_key = name
            self._publishing = True
            self._has_ever_streamed = True
            self._notify_stream_change(True)

    def _on_publish_done(self):
        self._publishing = False
        self._notify_stream_change(False)

    def _on_play(self, name: str):
        pass

    def _on_play_done(self):
        pass

    def _notify_stream_change(self, streaming: bool):
        if self.on_stream_change:
            try:
                self.on_stream_change(streaming)
            except Exception as e:
                self.log(f"[RTMP] 回调异常: {e}")

    def _start_callback_server(self):
        _CallbackHandler.manager_ref = self
        self._callback_server = HTTPServer(("127.0.0.1", CALLBACK_PORT), _CallbackHandler)
        self._callback_server.allow_reuse_address = True
        self._callback_thread = threading.Thread(target=self._callback_server.serve_forever, daemon=True)
        self._callback_thread.start()

    def _stop_callback_server(self):
        if self._callback_server:
            try:
                self._callback_server.shutdown()
            except Exception:
                pass
            self._callback_server = None
            self._callback_thread = None

    # ── 启停 ──

    def start(self) -> bool:
        if self.running:
            self.log("[RTMP] 服务已在运行")
            return True
        if not self._nginx_exists():
            self.log(f"[RTMP] nginx.exe 未找到，请确保 nginx 目录放置在 exe 同目录下")
            self.log(f"[RTMP] 期望路径: {NGINX_EXE}")
            return False

        self._kill_orphans()
        self._start_callback_server()

        try:
            self.process = subprocess.Popen(
                [NGINX_EXE, "-p", NGINX_DIR],
                cwd=NGINX_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )

            if not self._wait_listening(RTMP_PORT, timeout=8):
                stdout, stderr = self.process.communicate(timeout=2)
                self.log(f"[RTMP] 启动超时 — 端口 {RTMP_PORT} 未就绪")
                if self.process.poll() is not None:
                    self.log(f"[RTMP] nginx 进程已退出，返回码 {self.process.returncode}")
                    if stderr:
                        self.log(f"[RTMP] nginx stderr: {stderr.decode('utf-8', errors='replace').strip()}")
                self._stop_callback_server()
                self.process = None
                return False

            self.running = True
            self.log(f"[RTMP] 服务已就绪，监听 {RTMP_PORT} (RTMP) + 8080 (统计页) + 8081 (回调)，等待 PS5 连接...")
            return True
        except Exception as e:
            self.log(f"[RTMP] 启动失败: {e}")
            self._stop_callback_server()
            return False

    def stop(self):
        self._stop_callback_server()
        self._publishing = False
        if not self.running:
            return
        try:
            subprocess.run(
                [NGINX_EXE, "-p", NGINX_DIR, "-s", "quit"],
                cwd=NGINX_DIR,
                capture_output=True,
                timeout=5,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
        except subprocess.TimeoutExpired:
            if self.process and self.process.poll() is None:
                self.process.terminate()
                try:
                    self.process.wait(timeout=5)
                except subprocess.TimeoutExpired:
                    self.process.kill()
        except Exception:
            if self.process and self.process.poll() is None:
                self.process.kill()
        self.running = False
        self.process = None
        self._current_stream_key = ""
        self._has_ever_streamed = False

    def get_stream_key(self) -> str:
        """stat XML 兜底获取 stream key（_on_publish 回调已设，此方法仅备用）"""
        try:
            req = urllib.request.Request(RTMP_STAT_URL)
            with urllib.request.urlopen(req, timeout=2) as resp:
                xml_data = resp.read()
            root = ET.fromstring(xml_data)
            for app in root.iter("application"):
                if app.findtext("name", "") != RTMP_APP:
                    continue
                live_elem = app.find("live")
                if live_elem is None:
                    continue
                for stream in live_elem.findall("stream"):
                    name = stream.findtext("name", "")
                    if name and stream.find("publisher") is not None:
                        return name
            return ""
        except Exception:
            return ""

    def is_streaming(self) -> bool:
        return self._publishing

    @property
    def pull_url(self) -> str:
        return get_pull_url(self._current_stream_key, self.local_ip)

    @property
    def stream_key(self) -> str:
        return self._current_stream_key

    @property
    def has_ever_streamed(self) -> bool:
        return self._has_ever_streamed
