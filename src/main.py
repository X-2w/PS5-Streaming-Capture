"""PS5 直播截获工具 — 主入口 + GUI 面板"""

import os
import sys
import queue
import socket
import threading
import subprocess
import webbrowser
import ctypes
import tkinter as tk
from tkinter import ttk, messagebox
from collections import deque
from datetime import datetime
import urllib.request
import xml.etree.ElementTree as ET

from config import BASE_DIR, DNS_UPSTREAM, DNS_UPSTREAM_BACKUP
from ip_detect import get_preferred_ip
from dns_hijack import DNSHijacker
from rtmp_manager import RTMPManager


def _check_port(port: int, udp: bool = False) -> tuple[bool, str]:
    """检查端口是否空闲，返回 (空闲, 占用者PID)"""
    if udp:
        s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            s.bind(("0.0.0.0", port))
            s.close()
            return True, ""
        except OSError:
            s.close()
    else:
        s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        s.settimeout(0.5)
        if s.connect_ex(("127.0.0.1", port)) != 0:
            s.close()
            return True, ""
        s.close()
    # 端口被占用，查 PID
    try:
        out = subprocess.run(
            ["netstat", "-ano"], capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        proto = "UDP" if udp else "TCP"
        for line in out.stdout.splitlines():
            if f":{port}" in line and proto in line.upper() and "LISTENING" in line.upper():
                parts = line.split()
                if parts:
                    return False, parts[-1]
    except Exception:
        pass
    return False, "未知"


def _pid_to_name(pid: str) -> str:
    """PID → 进程名"""
    try:
        out = subprocess.run(
            ["tasklist", "/fi", f"PID eq {pid}", "/fo", "csv", "/nh"],
            capture_output=True, text=True,
            creationflags=subprocess.CREATE_NO_WINDOW,
        )
        parts = out.stdout.strip().strip('"').split('","')
        if parts:
            return parts[0]
    except Exception:
        pass
    return f"PID {pid}"


def _resource_path(relative_path):
    """获取资源绝对路径，兼容 PyInstaller 打包"""
    if getattr(sys, "frozen", False):
        return os.path.join(sys._MEIPASS, relative_path)
    return os.path.join(os.path.dirname(__file__), relative_path)


class App:
    def __init__(self):
        self.root = tk.Tk()
        self.root.withdraw()  # 先隐藏，管理员检测通过后才显示

        # 管理员检测前置：不通过则直接弹窗退出，不构建 UI
        if not ctypes.windll.shell32.IsUserAnAdmin():
            messagebox.showwarning(
                "权限不足",
                "此工具需要管理员权限才能运行。\n\n"
                "请右键点击程序 → 以管理员身份运行。"
            )
            self.root.destroy()
            os._exit(0)

        self.root.deiconify()
        self.root.title("PS5 直播截获工具")
        self.root.geometry("580x560")
        self.root.resizable(True, True)
        self.root.minsize(480, 440)

        self.root.protocol("WM_DELETE_WINDOW", self._on_close)

        self.log_queue = queue.Queue()
        self._poll_interval = 300  # ms

        self.local_ip = tk.StringVar(value="检测中...")
        self.dns_status = tk.StringVar(value="未启动")
        self.rtmp_status = tk.StringVar(value="未启动")
        self.stream_status = tk.StringVar(value="未检测到")
        self.pull_url_var = tk.StringVar(value="")

        self.dns_hijacker: DNSHijacker | None = None
        self.rtmp_manager: RTMPManager | None = None
        self._monitoring = False
        self._last_log_stats = 0
        self._auto_started = False

        # 流量图
        self._traffic_history = deque([0] * 60, maxlen=60)
        self._traffic_graph_active = False

        self._build_ui()
        self._detect_ip()
        self._start_log_poller()

        # 管理员已确认，自动启动服务
        self.root.after(300, self._start_all)

    def _safe_log(self, msg: str):
        self.log_queue.put(msg)

    def _build_ui(self):
        header = ttk.Frame(self.root)
        header.pack(fill="x", padx=10, pady=(10, 4))
        ttk.Label(header, text="PS5 直播截获工具", font=("Microsoft YaHei UI", 14, "bold")).pack(side="left")
        ttk.Label(header, text="v1.2", foreground="gray").pack(side="right", padx=4)

        # 本机 IP
        ip_frame = ttk.LabelFrame(self.root, text="本机 IP（PS5 需将此 IP 设为 DNS 服务器）", padding=6)
        ip_frame.pack(fill="x", padx=10, pady=(4, 2))
        ip_row = ttk.Frame(ip_frame)
        ip_row.pack(fill="x")
        self.ip_label = ttk.Label(ip_row, textvariable=self.local_ip, font=("Consolas", 13, "bold"))
        self.ip_label.pack(side="left")
        ttk.Button(ip_row, text="刷新", width=6, command=self._detect_ip).pack(side="right")

        # PS5 操作教程
        tutorial = ttk.Label(
            ip_frame,
            text=(
                "PS5 操作步骤：① 主界面进入「设置」→ ② 选择「网络」→「设定」→「设定互联网连接」\n"
                "③ 选中当前网络（Wi-Fi 或有线）→ ④ 按手柄 Options 键 → 选择「高级设定」\n"
                "⑤ DNS 设置选「手动」→ ⑥ 首选 DNS 填入上方显示的 IP 地址 → ⑦ 确认保存\n"
                "⑧ 返回主界面，按 Share 键或进入游戏后按 Share →「开始直播」即可"
            ),
            foreground="#888",
            font=("Microsoft YaHei UI", 8),
            wraplength=540,
            justify="left",
        )
        tutorial.pack(fill="x", pady=(6, 0))

        # 服务状态
        status_frame = ttk.LabelFrame(self.root, text="运行状态", padding=6)
        status_frame.pack(fill="x", padx=10, pady=(4, 2))

        # 左栏：状态标签
        left_status = ttk.Frame(status_frame)
        left_status.grid(row=0, column=0, sticky="w")

        self.status_labels: dict[str, tk.Label] = {}
        items = [
            ("DNS 接管", self.dns_status, "gray"),
            ("RTMP 接收", self.rtmp_status, "gray"),
            ("直播状态", self.stream_status, "gray"),
        ]
        for label_text, var, default_color in items:
            row = ttk.Frame(left_status)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=label_text, width=10, anchor="e").pack(side="left", padx=(0, 6))
            lbl = tk.Label(
                row, textvariable=var,
                foreground=default_color,
                font=("Microsoft YaHei UI", 9, "bold"),
                anchor="w",
            )
            lbl.pack(side="left")
            self.status_labels[label_text] = lbl

        # 中栏：实时数据
        self.bw_var = tk.StringVar(value="— MB/s")
        self.dns_total_var = tk.StringVar(value="0 次")
        self.dns_hit_var = tk.StringVar(value="0 次")

        mid_info = ttk.Frame(status_frame)
        mid_info.grid(row=0, column=1, sticky="ew", padx=(16, 8))
        status_frame.columnconfigure(1, weight=1)
        mid_items = [
            ("实时带宽", self.bw_var),
            ("DNS 查询", self.dns_total_var),
            ("拦截命中", self.dns_hit_var),
        ]
        for label_text, var in mid_items:
            row = ttk.Frame(mid_info)
            row.pack(fill="x", pady=1)
            ttk.Label(row, text=label_text, width=7, anchor="e").pack(side="left", padx=(0, 6))
            ttk.Label(row, textvariable=var, font=("Consolas", 9, "bold"), anchor="w").pack(side="left", fill="x", expand=True)

        # 右栏：流量图
        self.traffic_canvas = tk.Canvas(
            status_frame, width=220, height=85,
            bg="#e8e8e8", highlightthickness=1, highlightbackground="#ccc",
        )
        self.traffic_canvas.grid(row=0, column=2, sticky="e")

        # 拉流地址
        url_frame = ttk.LabelFrame(self.root, text="拉流地址（复制到 OBS / VLC 中打开）", padding=6)
        url_frame.pack(fill="x", padx=10, pady=(4, 2))
        self.url_entry = ttk.Entry(url_frame, textvariable=self.pull_url_var, font=("Consolas", 10), state="readonly")
        self.url_entry.pack(side="left", fill="x", expand=True, padx=(0, 6))
        self.url_entry.bind("<Button-1>", lambda e: self._open_url())
        self.copy_btn = ttk.Button(url_frame, text="复制", width=6, command=self._copy_url)
        self.copy_btn.pack(side="right")
        open_btn = ttk.Button(url_frame, text="打开", width=6, command=self._open_url)
        open_btn.pack(side="right", padx=(0, 4))

        # 控制按钮 — 始终启用，handler 内部判断状态
        btn_frame = ttk.Frame(self.root)
        btn_frame.pack(fill="x", padx=10, pady=(4, 2))
        self.start_btn = ttk.Button(btn_frame, text="重新启动", command=self._start_all, width=14)
        self.start_btn.pack(side="left", padx=(0, 6))
        self.stop_btn = ttk.Button(btn_frame, text="停止服务", command=self._stop_all, width=14)
        self.stop_btn.pack(side="left")

        # 日志
        log_frame = ttk.LabelFrame(self.root, text="运行日志", padding=4)
        log_frame.pack(fill="both", expand=True, padx=10, pady=(4, 10))
        self.log_text = tk.Text(log_frame, height=10, font=("Consolas", 9), wrap="word", state="disabled")
        scrollbar = ttk.Scrollbar(log_frame, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        scrollbar.pack(side="right", fill="y")

    def _set_status_color(self, key: str, color: str):
        """更新状态颜色（文字由 StringVar 控制，configure(text=...) 在 textvariable 绑定时无效）"""
        lbl = self.status_labels.get(key)
        if lbl:
            lbl.configure(foreground=color)

    def _detect_ip(self):
        def _run():
            ip = get_preferred_ip()
            self.root.after(0, lambda: self.local_ip.set(ip))
        threading.Thread(target=_run, daemon=True).start()

    def _copy_url(self):
        url = self.pull_url_var.get()
        if not url:
            return
        self.root.clipboard_clear()
        self.root.clipboard_append(url)
        self._safe_log("拉流地址已复制到剪贴板")

    def _open_url(self):
        url = self.pull_url_var.get()
        if not url:
            return

        try:
            webbrowser.open(url)
        except Exception:
            try:
                os.startfile(url)
            except Exception:
                subprocess.Popen(
                    ["cmd", "/c", "start", "", url],
                    creationflags=subprocess.CREATE_NO_WINDOW,
                )
        self._safe_log("正在用系统默认程序打开拉流地址...")

    def _start_all(self):
        if not ctypes.windll.shell32.IsUserAnAdmin():
            messagebox.showwarning(
                "权限不足",
                "此工具需要管理员权限才能运行。\n\n"
                "请右键点击程序 → 以管理员身份运行。\n"
                "然后程序会自动启动所有服务。"
            )
            return

        # 清理旧自身实例，防止多开
        current_pid = os.getpid()
        try:
            result = subprocess.run(
                ["tasklist", "/fi", "imagename eq PS5StreamCapture.exe", "/fo", "csv", "/nh"],
                capture_output=True, text=True,
                creationflags=subprocess.CREATE_NO_WINDOW,
            )
            for line in result.stdout.strip().splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = [p.strip().strip('"') for p in line.split('","')]
                if len(parts) >= 2:
                    try:
                        pid = int(parts[1])
                        if pid != current_pid:
                            subprocess.run(
                                ["taskkill", "/f", "/pid", str(pid)],
                                capture_output=True,
                                creationflags=subprocess.CREATE_NO_WINDOW,
                            )
                    except (ValueError, IndexError):
                        pass
        except Exception:
            pass

        # 端口冲突检测
        port_labels = {53: "DNS劫持 (UDP)", 1935: "RTMP", 8080: "统计页", 8081: "回调"}
        conflicts = []
        for port, label in port_labels.items():
            free, pid = _check_port(port, udp=(port == 53))
            if not free:
                proc_name = _pid_to_name(pid)
                conflicts.append(f"{label} 被 {proc_name} 占用")
        if conflicts:
            self._safe_log("[警告] 端口冲突检测：")
            for c in conflicts:
                self._safe_log(f"  - {c}")
            self._safe_log("如果服务启动失败，请关闭占用端口的程序后重试")

        # 先停旧服务
        if self.dns_hijacker or (self.rtmp_manager and self.rtmp_manager.running):
            self._stop_all(silent=True)

        local_ip = self.local_ip.get()
        no_ip = local_ip in ("检测中...", "127.0.0.1", "")
        if no_ip:
            self._detect_ip()

        self.rtmp_manager = RTMPManager(log_callback=self._safe_log, local_ip=local_ip, on_stream_change=self._on_stream_change)
        self._safe_log("─" * 40)
        self._safe_log(f"本机 IP: {local_ip}")
        self._safe_log(f"请将 PS5 的 DNS 设置为: {local_ip}")

        # DNS 劫持
        self.dns_hijacker = DNSHijacker(local_ip=local_ip, log_callback=self._safe_log)
        self.dns_hijacker.start()
        self.root.after(800, lambda: self._update_dns_status())

        # RTMP 启动移到后台线程，避免阻塞 UI
        threading.Thread(target=self._start_rtmp_worker, daemon=True).start()

    def _start_rtmp_worker(self):
        try:
            rtmp_ok = self.rtmp_manager.start()
        except Exception as e:
            self._safe_log(f"[错误] RTMP 启动异常: {e}")
            rtmp_ok = False
        self.root.after(0, lambda: self._on_rtmp_started(rtmp_ok))

    def _on_rtmp_started(self, rtmp_ok: bool):
        self._update_rtmp_status(rtmp_ok)
        if rtmp_ok:
            self._monitoring = True
            self._last_log_stats = datetime.now().timestamp()
            self._monitor_stream()
            self._traffic_graph_active = True
            self._traffic_history.clear()
            self._update_traffic_graph()
            self._safe_log("所有服务已启动，正在等待 PS5 开播...")
        else:
            self._safe_log("启动失败，请检查 nginx 是否被占用")

    def _update_dns_status(self):
        ok = self.dns_hijacker and self.dns_hijacker.running
        if ok:
            self.dns_status.set("运行中")
            self._set_status_color("DNS 接管", "green")
        else:
            self.dns_status.set("启动失败")
            self._set_status_color("DNS 接管", "red")

    def _update_rtmp_status(self, ok: bool):
        if ok:
            self.rtmp_status.set("运行中")
            self._set_status_color("RTMP 接收", "green")
        else:
            self.rtmp_status.set("启动失败")
            self._set_status_color("RTMP 接收", "red")

    def _stop_all(self, silent=False):
        self._monitoring = False
        self._traffic_graph_active = False
        if self.dns_hijacker:
            self.dns_hijacker.stop()
            self.dns_hijacker = None
        if self.rtmp_manager:
            self.rtmp_manager.stop()
            self.rtmp_manager = None

        self.dns_status.set("未启动")
        self._set_status_color("DNS 接管", "gray")
        self.rtmp_status.set("未启动")
        self._set_status_color("RTMP 接收", "gray")
        self.stream_status.set("未检测到")
        self._set_status_color("直播状态", "gray")
        self.pull_url_var.set("")
        self.bw_var.set("— MB/s")
        self.dns_total_var.set("0 次")
        self.dns_hit_var.set("0 次")
        if not silent:
            self._safe_log("所有服务已停止")

    def _on_stream_change(self, streaming: bool):
        """推流状态变化回调（从 HTTP 回调线程触发，通过 root.after 回到主线程）"""
        self.root.after(0, lambda: self._update_stream_ui(streaming))

    def _update_stream_ui(self, streaming: bool):
        if not self.rtmp_manager:
            return
        if streaming:
            self.stream_status.set("正在直播")
            self._set_status_color("直播状态", "green")
            self.pull_url_var.set(self.rtmp_manager.pull_url)
        else:
            self.stream_status.set("未检测到")
            self._set_status_color("直播状态", "gray")
            self.pull_url_var.set("")

    def _monitor_stream(self):
        if not self._monitoring or not self.rtmp_manager:
            return

        is_streaming = self.rtmp_manager.is_streaming()
        if is_streaming:
            self.stream_status.set("正在直播")
            self._set_status_color("直播状态", "green")
            self.pull_url_var.set(self.rtmp_manager.pull_url)
        else:
            if self.rtmp_manager.has_ever_streamed:
                self.stream_status.set("等待重连")
            else:
                self.stream_status.set("等待连接")
            self._set_status_color("直播状态", "orange")

        # 定期统计（每 10 秒）
        now = datetime.now().timestamp()
        if now - self._last_log_stats >= 10:
            self._last_log_stats = now
            if self.dns_hijacker and self.dns_hijacker.running:
                s = self.dns_hijacker.stats
                self._safe_log(f"已处理 {s['total']} 次 DNS 查询，其中 {s['matched']} 次成功拦截")

        self.root.after(2000, self._monitor_stream)

    def _update_traffic_graph(self):
        """实时 RTMP 带宽折线图 + 中间栏数据刷新"""
        if not self._traffic_graph_active:
            return
        bw = 0
        if self.rtmp_manager and self.rtmp_manager.running:
            try:
                req = urllib.request.Request("http://127.0.0.1:8080/stat")
                with urllib.request.urlopen(req, timeout=0.3) as resp:
                    root = ET.fromstring(resp.read())
                for app in root.iter("application"):
                    for stream in app.findall("live/stream"):
                        bw_in = stream.findtext("bw_in", "0")
                        try:
                            bw = int(bw_in)
                        except ValueError:
                            bw = 0
                        break
            except Exception:
                bw = 0
        self._traffic_history.append(bw)

        # 中栏：实时数据
        mbps = bw / 1_000_000
        self.bw_var.set(f"{mbps:.2f} MB/s")
        if self.dns_hijacker and self.dns_hijacker.running:
            s = self.dns_hijacker.stats
            self.dns_total_var.set(f"{s['total']} 次")
            self.dns_hit_var.set(f"{s['matched']} 次")

        # 右栏：折线图
        canvas = self.traffic_canvas
        canvas.delete("all")
        w, h = 220, 85
        m = 6

        values = list(self._traffic_history)
        peak = max(values) if values else 0
        if peak <= 0:
            peak = 1_000_000  # 无推流时最小量程 1Mbps

        # 网格 — 浅灰虚线
        for i in range(4):
            y = m + i * (h - 2 * m) / 3
            canvas.create_line(m, y, w - m, y, fill="#bbb", dash=(2, 4))

        # 折线 — 黑色实线
        points = []
        for i, v in enumerate(values):
            x = m + i * (w - 2 * m) / 59
            y = h - m - (v / peak) * (h - 2 * m)
            points.extend([x, y])
        if len(points) >= 4:
            canvas.create_line(*points, fill="#1a1a1a", width=2)

        # Y 轴标注
        if peak >= 1_000_000:
            peak_str = f"{peak / 1_000_000:.1f} MB/s"
        else:
            peak_str = f"{peak / 1000:.0f} K"
        canvas.create_text(3, m - 2, text=peak_str, anchor="nw", fill="#666", font=("Consolas", 7))
        canvas.create_text(3, h - m + 1, text="0", anchor="sw", fill="#666", font=("Consolas", 7))

        # 无推流提示
        if max(values) == 0:
            canvas.create_text(w / 2, h / 2, text="等待推流", fill="#999",
                               font=("Microsoft YaHei UI", 9))

        self.root.after(1000, self._update_traffic_graph)

    def _start_log_poller(self):
        while not self.log_queue.empty():
            msg = self.log_queue.get_nowait()
            timestamp = datetime.now().strftime("%H:%M:%S")
            self.log_text.configure(state="normal")
            self.log_text.insert("end", f"[{timestamp}] {msg}\n")
            self.log_text.see("end")
            self.log_text.configure(state="disabled")
        self.root.after(self._poll_interval, self._start_log_poller)

    def _on_close(self):
        self._stop_all()
        self.root.destroy()

    def run(self):
        self.root.mainloop()


if __name__ == "__main__":
    App().run()
